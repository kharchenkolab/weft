"""ensure_available P2: the realize postcondition — an env spec's
verify block is identity-neutral, composes along the extends chain, and
is proven every time the env realizes (ready MEANS verified)."""

import json

import pytest

from weft.adapters.base import ShimResult
from weft.errors import WeftError
from weft.spec import EnvSpec, merge_verify
from weft.verify import MARKER

SPEC = {"name": "x", "deps": {"conda": ["python =3.12"]},
        "verify": {"import": ["numpy"], "versions": {"numpy": ">=2.0"}}}


# ── identity: a postcondition never forks the EnvID ────────────────────────

def test_verify_block_is_hash_neutral():
    with_v = EnvSpec.from_dict(SPEC).spec_hash()
    without = EnvSpec.from_dict(
        {k: v for k, v in SPEC.items() if k != "verify"}).spec_hash()
    assert with_v == without


def test_verify_block_validated_at_intake():
    with pytest.raises(WeftError) as ei:
        EnvSpec.from_dict({"deps": {"conda": ["python"]},
                           "verify": {"min_versions": {"x": "1"}}})
    assert ei.value.code == "task.invalid"
    s = EnvSpec.from_dict(SPEC)
    assert s.to_dict()["verify"]["import"] == ["numpy"]
    assert "verify" not in EnvSpec.from_dict(
        {"deps": {"conda": ["python"]}}).to_dict()


# ── composition along the identity chain ───────────────────────────────────

def test_verify_composes_base_union_child():
    base = {"loads": ["alpha"], "versions": {"alpha": ">=1"}}
    child = {"loads": ["beta"], "versions": {"alpha": ">=2"}}
    got = merge_verify(base, child)
    assert got["loads"] == ["alpha", "beta"]
    assert got["versions"]["alpha"] == ">=2"        # child overrides
    assert merge_verify(None, child) == child
    assert merge_verify(base, None) == base


def test_merged_onto_carries_composed_verify():
    parent = EnvSpec.from_dict({"deps": {"conda": ["r-base"]},
                                "verify": {"loads": ["alpha"]}})
    child = EnvSpec.from_dict({"deps": {"cran": ["beta"]},
                               "verify": {"loads": ["beta"]}})
    merged = child.merged_onto(parent)
    assert merged.verify["loads"] == ["alpha", "beta"]


# ── build-time enforcement: ready MEANS verified ───────────────────────────

ENV_ROW = {"spec_hash": "spec_x", "canonical": {
    "platforms": {"linux-64": [
        {"kind": "conda", "name": "python", "version": "3.12"},
        {"kind": "pypi", "name": "numpy", "version": "2.0"}]},
    "extras": {}}}


class _Ad:
    """Fake adapter: activation spot-checks pass; the verify oracle
    answers from `oracle`; writes recorded."""
    name = "fake"

    def __init__(self, oracle):
        self.oracle = oracle
        self.wrote = []

    def path(self, rel):
        return f"/site/{rel}"

    def run_activated(self, script, timeout=120.0):
        # every oracle script CONTAINS the marker literal; the spot
        # check does not — route on that, never on interpreter names
        if "WEFT-VERIFY" in script:
            return self.oracle
        return ShimResult(0, "", "")

    def run_cmd(self, script, timeout=120.0):
        return ShimResult(0, "d" * 64, "")

    def write_file(self, rel, data, mode=None):
        self.wrote.append(rel)


def test_build_postcondition_failure_blocks_ready():
    from weft.realize import _spot_check_and_mark
    ad = _Ad(ShimResult(0, MARKER + json.dumps(
        {"name": "numpy", "kind": "metadata", "ok": True,
         "got": "1.9"}), ""))
    with pytest.raises(WeftError) as ei:
        _spot_check_and_mark("env:v1:x", ENV_ROW, ad, "envs/x", "prefix",
                             verify_block={"versions": {"numpy": ">=2.0"}})
    assert ei.value.code == "env.realize_failed"
    assert ei.value.hints["postcondition"] is True
    assert ei.value.hints["verified"]["numpy"]["got"] == "1.9"
    assert not any(".weft-ready" in w for w in ad.wrote)   # NOT marked


def test_build_unknown_is_retryable_not_ready():
    from weft.realize import _spot_check_and_mark
    ad = _Ad(ShimResult(139, "", "Segmentation fault"))
    with pytest.raises(WeftError) as ei:
        _spot_check_and_mark("env:v1:x", ENV_ROW, ad, "envs/x", "prefix",
                             verify_block={"import": ["numpy"]})
    assert ei.value.code == "env.realize_failed" and ei.value.retryable
    assert "could not be VERIFIED" in ei.value.detail
    assert not any(".weft-ready" in w for w in ad.wrote)


def test_build_postcondition_pass_marks_ready():
    from weft.realize import _spot_check_and_mark
    ad = _Ad(ShimResult(0, MARKER + json.dumps(
        {"name": "numpy", "kind": "metadata", "ok": True,
         "got": "2.1"}), ""))
    _spot_check_and_mark("env:v1:x", ENV_ROW, ad, "envs/x", "prefix",
                         verify_block={"versions": {"numpy": ">=2.0"}})
    assert any(".weft-ready" in w for w in ad.wrote)


def test_no_verify_block_is_verbatim_previous_behavior():
    from weft.realize import _spot_check_and_mark
    ad = _Ad(ShimResult(1, "", "oracle must never run"))
    _spot_check_and_mark("env:v1:x", ENV_ROW, ad, "envs/x", "prefix")
    assert any(".weft-ready" in w for w in ad.wrote)


# ── adopt-time enforcement (default ON, policy opt-out) ────────────────────

def _laid_env(tmp_path, pixi_bin, verify_block):
    from weft.api import Weft
    env_id = "env:v1:feedfacecafe"
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    body = {"name": "x", "deps": {"conda": ["python"]}}
    if verify_block:
        body["verify"] = verify_block
    w.store.put_spec("spec_x", "x", body)
    recs = [{"kind": "pypi", "name": "numpy", "version": "2.0"}]
    plats = ["linux-64", "osx-arm64", "linux-aarch64"]
    w.store.put_env(env_id, "spec_x", {
        "extras": {},
        "platforms": {pl: recs for pl in plats},
    }, "lock: {}", "[project]", plats)
    from weft.realize import env_dir_rel
    rel = env_dir_rel(env_id)
    d = tmp_path / "site" / rel
    (d / "bin").mkdir(parents=True)
    (d / "activate.sh").write_text("true\n")
    (d / ".weft-ready").write_text(json.dumps({"strategy": "prefix"}))
    w.store.set_realization(env_id, "local", "prefix", rel, "ready")
    return w, env_id


def _oracle(monkeypatch, w, resp):
    ad = w.adapters["local"]
    orig = ad.run_cmd

    def route(script, timeout=120.0):
        if "WEFT-VERIFY" in script:
            return resp
        return orig(script, timeout=timeout)

    monkeypatch.setattr(ad, "run_cmd", route)
    monkeypatch.setattr(ad, "run_activated", route)


def test_adopt_postcondition_failure_rejects_adoption(tmp_path, pixi_bin,
                                                      monkeypatch):
    from weft.realize import ensure_realization
    w, env_id = _laid_env(tmp_path, pixi_bin,
                          {"versions": {"numpy": ">=2.0"}})
    _oracle(monkeypatch, w, ShimResult(0, MARKER + json.dumps(
        {"name": "numpy", "kind": "metadata", "ok": True,
         "got": "1.9"}), ""))
    with pytest.raises(WeftError):
        # adoption rejected -> rebuild path -> fails in this fake env;
        # what matters is the adopt did NOT return ready
        ensure_realization(env_id, w.store.get_env(env_id),
                           w.adapters["local"], w.store,
                           site_config={})
    kinds = [e["kind"] for e in w.store.events_since(0, 200)]
    assert "realize.postcondition_failed" in kinds


def test_adopt_policy_opt_out_skips_the_oracle(tmp_path, pixi_bin,
                                               monkeypatch):
    from weft.realize import ensure_realization
    w, env_id = _laid_env(tmp_path, pixi_bin,
                          {"versions": {"numpy": ">=2.0"}})
    _oracle(monkeypatch, w, ShimResult(1, "", "oracle must never run"))
    got = ensure_realization(
        env_id, w.store.get_env(env_id), w.adapters["local"], w.store,
        site_config={"policy": {"verify_on_adopt": False}})
    assert got["state"] == "ready"


def test_adopt_unknown_adopts_with_event(tmp_path, pixi_bin, monkeypatch):
    from weft.realize import ensure_realization
    w, env_id = _laid_env(tmp_path, pixi_bin, {"import": ["numpy"]})
    _oracle(monkeypatch, w, ShimResult(139, "", "Segmentation fault"))
    got = ensure_realization(env_id, w.store.get_env(env_id),
                             w.adapters["local"], w.store, site_config={})
    assert got["state"] == "ready"                # can't-verify != broken
    kinds = [e["kind"] for e in w.store.events_since(0, 200)]
    assert "realize.verify_unknown" in kinds
