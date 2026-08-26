"""URL/file pins (approved design 2026-08-25, release-asset wheel
class): 'name @ <url>#sha256=<hex64>' in deps.pypi. Contracts pinned
here: ONE grammar owner refusing typed at intake; the URL never enters
the canonical (identity is the sha); the manifest never carries the
pin (the site never fetches); ensure CAS-carries with fail-fast verify;
realize stages from CAS and installs --no-deps --no-index; a pin delta
disqualifies overlay eligibility (it would silently skip the wheel)."""

import hashlib
import textwrap
import zipfile
from pathlib import Path

import pytest

from weft.api import Weft
from weft.errors import WeftError
from weft.ids import env_id, sha256_bytes
from weft.lock import canonicalize_lock, render_pixi_manifest
from weft.spec import EnvSpec, parse_direct_ref

HEX = "a" * 64
LOCK = textwrap.dedent("""\
    version: 6
    environments:
      default:
        channels:
        - url: https://conda.anaconda.org/conda-forge/
        packages:
          linux-64:
          - conda: https://conda.anaconda.org/conda-forge/linux-64/python-3.12.4-h194c7f8_0.conda
    packages:
    - conda: https://conda.anaconda.org/conda-forge/linux-64/python-3.12.4-h194c7f8_0.conda
      sha256: bbb222
    """)


def _spec(pypi):
    return EnvSpec.from_dict({"name": "t", "platforms": ["linux-64"],
                              "deps": {"conda": ["python =3.12"],
                                       "pypi": pypi}})


# ── intake: the grammar owner refuses typed ──────────────────────────

def test_missing_fragment_refused_naming_package_and_syntax():
    with pytest.raises(WeftError) as ei:
        _spec(["demo @ https://x.example/demo-1.0-py3-none-any.whl"])
    assert ei.value.code == "task.invalid"
    assert "demo" in ei.value.detail and "sha256" in ei.value.detail
    assert "compute" in ei.value.hints


def test_bad_hex_is_the_missing_fragment_case():
    with pytest.raises(WeftError):
        _spec([f"demo @ https://x.example/d.whl#sha256={'a' * 63}"])


def test_non_wheel_refused_naming_the_boundary():
    with pytest.raises(WeftError) as ei:
        _spec([f"demo @ https://x.example/demo-1.0.tar.gz#sha256={HEX}"])
    assert "wheels only" in ei.value.detail
    assert "build_deps" in ei.value.detail


def test_relative_file_url_refused_controller_realm():
    with pytest.raises(WeftError) as ei:
        _spec([f"demo @ file://rel/d.whl#sha256={HEX}"])
    assert "CONTROLLER-realm" in ei.value.detail


def test_unsupported_scheme_refused():
    with pytest.raises(WeftError) as ei:
        _spec([f"demo @ ftp://x.example/d.whl#sha256={HEX}"])
    assert ei.value.hints["supported"] == ["https", "http", "file"]


def test_direct_ref_collides_with_registry_entry_of_same_name():
    with pytest.raises(WeftError) as ei:
        _spec(["demo",
               f"demo @ https://x.example/d.whl#sha256={HEX}"])
    assert "duplicate" in ei.value.detail


def test_registry_entries_still_parse_untouched():
    s = _spec(["requests >=2.31"])
    assert s.pypi == ["requests >=2.31"]
    assert parse_direct_ref("requests >=2.31") is None


# ── manifest + canonical: URL is display, sha is identity ────────────

def test_manifest_never_carries_the_pin():
    s = _spec(["requests",
               f"demo @ https://x.example/demo-1.0-py3-none-any.whl"
               f"#sha256={HEX}"])
    m = render_pixi_manifest(s)
    assert "requests" in m
    assert "demo" not in m and "x.example" not in m


def test_canonical_carries_sha_not_url_and_identity_follows():
    url_a = f"demo @ https://a.example/demo-1.0-py3-none-any.whl#sha256={HEX}"
    url_b = f"demo @ https://b.example/demo-1.0-py3-none-any.whl#sha256={HEX}"
    ca = canonicalize_lock(LOCK, _spec([url_a]))
    cb = canonicalize_lock(LOCK, _spec([url_b]))
    assert ca["url_pins"] == [{"name": "demo", "sha256": HEX,
                               "filename": "demo-1.0-py3-none-any.whl"}]
    assert "a.example" not in str(ca)
    # same bytes via different URLs => the SAME EnvID
    assert env_id(ca) == env_id(cb)
    # a different sha => a different EnvID
    cc = canonicalize_lock(LOCK, _spec(
        [url_a.replace(HEX, "b" * 64)]))
    assert env_id(cc) != env_id(ca)
    # and no pins at all => different again
    assert env_id(canonicalize_lock(LOCK, _spec([]))) != env_id(ca)


# ── ensure-time carry: fetch once, verify, fail fast ─────────────────

def _wheel_bytes(name="weftpin_demo"):
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{name}/__init__.py", "TAG = 'weft-url-pin'\n")
        z.writestr(f"{name}-1.0.dist-info/METADATA",
                   f"Metadata-Version: 2.1\nName: {name}\n"
                   "Version: 1.0\n")
        z.writestr(f"{name}-1.0.dist-info/WHEEL",
                   "Wheel-Version: 1.0\nGenerator: weft-test\n"
                   "Root-Is-Purelib: true\nTag: py3-none-any\n")
        z.writestr(f"{name}-1.0.dist-info/RECORD", "")
    return buf.getvalue()


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def _pin_spec_and_canonical(tmp_path, data, sha=None):
    whl = tmp_path / "weftpin_demo-1.0-py3-none-any.whl"
    whl.write_bytes(data)
    sha = sha or sha256_bytes(data)
    spec = _spec([f"weftpin-demo @ file://{whl}#sha256={sha}"])
    canonical = {"url_pins": [
        {"name": "weftpin-demo", "sha256": sha,
         "filename": whl.name}]}
    return spec, canonical


def test_carry_ingests_by_content_and_is_idempotent(w, tmp_path):
    data = _wheel_bytes()
    spec, canonical = _pin_spec_and_canonical(tmp_path, data)
    w.envman._carry_url_pins(spec, canonical)
    sha = canonical["url_pins"][0]["sha256"]
    assert w.cas._blob_path(sha).exists()
    w.envman._carry_url_pins(spec, canonical)   # second call: no-op


def test_carry_refuses_on_sha_mismatch_naming_both(w, tmp_path):
    data = _wheel_bytes()
    spec, canonical = _pin_spec_and_canonical(tmp_path, data,
                                              sha="c" * 64)
    with pytest.raises(WeftError) as ei:
        w.envman._carry_url_pins(spec, canonical)
    assert ei.value.code == "data.verify_failed"
    assert ei.value.hints["expected"] == "c" * 64
    assert ei.value.hints["got"] == sha256_bytes(data)
    assert ei.value.hints["package"] == "weftpin-demo"
    assert not w.cas._blob_path("c" * 64).exists(), \
        "a mismatched artifact must never land in the CAS"


def test_carry_refuses_missing_file_typed(w, tmp_path):
    spec = _spec([f"weftpin-demo @ file:///no/such/dir/x.whl"
                  f"#sha256={HEX}"])
    canonical = {"url_pins": [{"name": "weftpin-demo", "sha256": HEX,
                               "filename": "x.whl"}]}
    with pytest.raises(WeftError) as ei:
        w.envman._carry_url_pins(spec, canonical)
    assert ei.value.code == "data.transfer_failed"
    assert ei.value.hints["package"] == "weftpin-demo"


# ── realize: staged from CAS, installed offline ──────────────────────

def test_realize_stages_and_installs_offline(w, tmp_path):
    """Drives the REAL _install_url_pins on a local site: the wheel
    reaches .weft-wheels/ through the transfer plane and the pip line
    runs --no-deps --no-index against the LOCAL file (a fake python in
    activate.sh captures the argv — the contract under test is weft's,
    not pip's)."""
    from weft.realize import _install_url_pins
    data = _wheel_bytes()
    sha = sha256_bytes(data)
    w.cas.put_bytes(data)
    adapter = w.adapters["local"]
    rel = "envs/urlpin-test"
    adapter.write_file(
        f"{rel}/activate.sh",
        b'python() { printf \'%s \' "$@" > pip-args.txt; }\n')
    env_row = {"canonical": {"url_pins": [
        {"name": "weftpin-demo", "sha256": sha,
         "filename": "weftpin_demo-1.0-py3-none-any.whl"}]}}
    pack_tools = {"dataman": w.dataman, "transfers": w.transfers,
                  "cas": w.cas, "store": w.store}
    _install_url_pins(env_row, adapter, rel, pack_tools)
    root = tmp_path / "site" / rel
    staged = root / ".weft-wheels" / "weftpin_demo-1.0-py3-none-any.whl"
    assert staged.exists() and staged.read_bytes() == data
    args = (root / "pip-args.txt").read_text()
    assert "--no-deps" in args and "--no-index" in args
    assert ".weft-wheels/weftpin_demo-1.0-py3-none-any.whl" in args


def test_realize_no_pins_is_a_no_op(w):
    from weft.realize import _install_url_pins
    _install_url_pins({"canonical": {}}, w.adapters["local"],
                      "envs/none", {})   # no pack_tools needed


# ── overlay eligibility: a pin delta needs a full prefix ─────────────

def test_pin_delta_disqualifies_overlay():
    from weft.overlay import classify_delta
    parent = {"platforms": {"linux-64": []}}
    child = {"platforms": {"linux-64": []},
             "url_pins": [{"name": "demo", "sha256": HEX,
                           "filename": "d.whl"}]}
    out = classify_delta(parent, child)
    assert out["layerable"] is False
    assert "url pins" in out["why"]


def test_identical_pins_keep_overlay_eligibility():
    from weft.overlay import classify_delta
    pins = [{"name": "demo", "sha256": HEX, "filename": "d.whl"}]
    parent = {"platforms": {"linux-64": []}, "url_pins": pins}
    child = {"platforms": {"linux-64": []}, "url_pins": pins,
             "layers": {"cran": {"records": [{"name": "jsonlite",
                                              "version": "1"}]}}}
    out = classify_delta(parent, child)
    assert out["layerable"] is True


# ── end-to-end acceptance (real solve): the aba viewer-arc shape ─────

@pytest.mark.solver
def test_end_to_end_pin_ensure_realize_import(w, tmp_path):
    """The full lane: ensure with a file:// direct ref (crafted valid
    wheel), realize on a local site, run a task that IMPORTS the
    pinned package. Identity: re-ensure with the wheel at a DIFFERENT
    path but the same bytes yields the SAME EnvID."""
    data = _wheel_bytes()
    sha = sha256_bytes(data)
    whl = tmp_path / "weftpin_demo-1.0-py3-none-any.whl"
    whl.write_bytes(data)
    spec = {"name": "pinned", "deps": {
        "conda": ["python =3.12"],
        "pypi": [f"weftpin-demo @ file://{whl}#sha256={sha}"]}}
    env = w.env_ensure(spec)
    assert "env_id" in env, env
    j = w.runner.wait(w.task_submit({
        "command": "python -c 'import weftpin_demo; "
                   "print(weftpin_demo.TAG)' > results/tag.txt",
        "env": env["env_id"], "outputs": ["results/"],
        "site": "local"}, force=True)["job_id"], 1200)
    assert j["state"] == "DONE", j["error"]
    out = next(o for o in j["manifest"]["outputs"]
               if o["path"] == "results/tag.txt")
    assert "weft-url-pin" in out["preview"]["lines"][0]
    # identity is content: same bytes, different URL -> same EnvID
    whl2 = tmp_path / "elsewhere" / whl.name
    whl2.parent.mkdir()
    whl2.write_bytes(data)
    spec2 = {"name": "pinned", "deps": {
        "conda": ["python =3.12"],
        "pypi": [f"weftpin-demo @ file://{whl2}#sha256={sha}"]}}
    assert w.env_ensure(spec2)["env_id"] == env["env_id"]
