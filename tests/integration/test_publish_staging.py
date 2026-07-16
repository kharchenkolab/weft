"""Publish staging (weft-ui ask): build STORAGE decoupled from build
PATH. The prefix churn lands in a staging dir bind-mounted at the tree
path inside each build command's userns; the slow tree receives one
sequential image write. Fast-lane contract tests — the fake adapter
records the command stream; reality (cbe/clip) proves the bind."""

import hashlib

import pytest

from weft.errors import WeftError
from weft.publish import _staging_plan
from weft.realize import _StagedBuild, _bind_wrap_cmd, _build_squashfs

CAPS_USERNS = {"internet": True,
               "squashfs": {"mksquashfs": "/usr/bin/mksquashfs",
                            "squashfuse": "/usr/bin/squashfuse",
                            "dev_fuse": True, "userns": True}}
CAPS_DIRECT = {"internet": True,
               "squashfs": {"mksquashfs": "/usr/bin/mksquashfs",
                            "squashfuse": "/usr/bin/squashfuse",
                            "dev_fuse": True, "userns": False}}
TREE = "/groups/lab/weft-envs"
HASH = "c" * 64


def _sub(tree=TREE, h=HASH):
    return f"{h[:24]}-{hashlib.sha256(tree.encode()).hexdigest()[:8]}"


# -- the plan: where does the churn land? --------------------------------

def test_plan_default_is_under_the_site_root():
    rel, why = _staging_plan(CAPS_USERNS, {}, None, HASH, TREE)
    assert rel == f"stage/publish/{_sub()}" and why is None


def test_plan_needs_userns_and_says_so():
    rel, why = _staging_plan(CAPS_DIRECT, {}, None, HASH, TREE)
    assert rel is None and "user namespaces" in why


def test_plan_none_disables_even_with_userns():
    rel, why = _staging_plan(CAPS_USERNS, {}, "none", HASH, TREE)
    assert rel is None and "disabled" in why


def test_plan_site_config_and_call_arg_precedence():
    site = {"config": {"publish_staging": "/fast/scratch"}}
    rel, _ = _staging_plan(CAPS_USERNS, site, None, HASH, TREE)
    assert rel == f"/fast/scratch/{_sub()}"
    # the call arg is the stronger lever
    rel, _ = _staging_plan(CAPS_USERNS, site, "/tmp/other", HASH, TREE)
    assert rel == f"/tmp/other/{_sub()}"
    rel, why = _staging_plan(CAPS_USERNS, site, "none", HASH, TREE)
    assert rel is None and "disabled" in why


def test_plan_refuses_relative_staging():
    with pytest.raises(WeftError) as e:
        _staging_plan(CAPS_USERNS, {}, "rel/path", HASH, TREE)
    assert e.value.code == "task.invalid"


def test_plan_distinct_trees_do_not_collide():
    a, _ = _staging_plan(CAPS_USERNS, {}, None, HASH, TREE)
    b, _ = _staging_plan(CAPS_USERNS, {}, None, HASH, "/other/tree")
    assert a != b


# -- the wrap and the proxy ----------------------------------------------

def test_bind_wrap_shape():
    c = _bind_wrap_cmd("pixi install", "/fast/stage", "/tree/envs/x/mnt")
    assert c.startswith("unshare -rm ")
    assert "mount --bind /fast/stage /tree/envs/x/mnt" in c
    assert "exit 97" in c            # diagnosable bind failure
    assert "command -v bash" in c    # el7 bash-4.2-as-sh lesson


class FakeShim:
    def __init__(self, rc=0, out="", err=""):
        self.rc, self.out, self.err = rc, out, err


class FakeAdapter:
    """Records every call; canned answers keyed by substring."""
    name, root = "fake", "/site/root"

    def __init__(self, answers=None):
        self.commands, self.files, self.shims = [], {}, []
        self.answers = answers or {}

    def path(self, rel):
        return rel if rel.startswith("/") else f"{self.root}/{rel}"

    @property
    def pixi_bin(self):
        return self.path("bin/pixi")

    def run_cmd(self, script, *, timeout=120.0):
        self.commands.append(script)
        for key, resp in self.answers.items():
            if key in script:
                return resp() if callable(resp) else resp
        return FakeShim()

    def run_activated(self, script, *, timeout=120.0):
        return self.run_cmd(script, timeout=timeout)

    def write_file(self, rel, data, mode=0o644):
        self.files[self.path(rel)] = data

    def read_file(self, rel, max_bytes=None):
        p = self.path(rel)
        if p not in self.files:
            raise WeftError("data.missing", f"no such file: {rel}",
                            stage="infra")
        return self.files[p]

    def file_exists(self, rel):
        return self.path(rel) in self.files

    def shim(self, argv, *, timeout=60.0):
        self.shims.append(list(argv))
        return FakeShim()


def test_proxy_wraps_commands_and_redirects_files():
    a = FakeAdapter()
    p = _StagedBuild(a, "/tree/envs/x/mnt", "/site/root/stage/publish/x")
    p.run_cmd("echo hi")
    assert "unshare -rm" in a.commands[-1]
    assert "mount --bind /site/root/stage/publish/x /tree/envs/x/mnt" \
        in a.commands[-1]
    p.run_activated("echo act")
    assert "unshare -rm" in a.commands[-1]
    # file helpers: bytes land in staging, mount path never touched
    p.write_file("/tree/envs/x/mnt/pixi.toml", b"m")
    assert a.files == {"/site/root/stage/publish/x/pixi.toml": b"m"}
    assert p.read_file("/tree/envs/x/mnt/pixi.toml") == b"m"
    assert p.file_exists("/tree/envs/x/mnt/pixi.toml") is True
    # non-mount paths pass through untouched
    p.write_file("etc/x", b"y")
    assert a.files["/site/root/etc/x"] == b"y"
    # shim path args are rewritten (adapters exec it outside any ns)
    p.shim(["materialize", "--dir", "/tree/envs/x/mnt",
            "--plan", "/tree/envs/x/mnt/post-inputs.tsv",
            "--cas", "/site/root/cas"])
    assert a.shims[-1] == ["materialize",
                           "--dir", "/site/root/stage/publish/x",
                           "--plan",
                           "/site/root/stage/publish/x/post-inputs.tsv",
                           "--cas", "/site/root/cas"]
    # everything else delegates (path stays the MOUNT view for commands)
    assert p.path("/tree/envs/x/mnt/pixi.toml") == "/tree/envs/x/mnt/pixi.toml"
    assert p.pixi_bin == "/site/root/bin/pixi"


# -- staged _build_squashfs orchestration --------------------------------

ENV_ROW = {"manifest": "[project]\n", "native_lock": "lock: 1\n",
           "canonical": {"layers": {}, "extras": {},
                         "platforms": {"linux-64": []}},
           "platforms": ["linux-64"]}
REL = f"{TREE}/envs/{HASH}"


def _events():
    ev = []
    return ev, lambda name, **kw: ev.append((name, kw))


def _meta_answer():
    return FakeShim(out="deadbeef 4096")


def test_staged_build_command_stream():
    a = FakeAdapter(answers={"printf '%s %s'": _meta_answer()})
    ev, emit = _events()
    meta = _build_squashfs("env:v1:" + HASH, ENV_ROW, a, REL, [], "",
                           CAPS_USERNS, {}, emit,
                           staging_rel=f"stage/publish/{_sub()}")
    staging_abs = f"/site/root/stage/publish/{_sub()}"
    mount_abs = f"{REL}/mnt"
    # probe ran, and the pixi install went through the bind wrap with
    # the MOUNT manifest path (that's what gets baked)
    wrapped = [c for c in a.commands if "unshare -rm" in c]
    assert any("pixi" in c and f"{mount_abs}/pixi.toml" in c
               for c in wrapped)
    assert all(staging_abs in c for c in wrapped)  # bind in every wrap
    # manifest/lock bytes landed in staging, not on the tree
    assert f"{staging_abs}/pixi.toml" in a.files
    assert f"{mount_abs}/pixi.toml" not in a.files
    # mksquashfs read STAGING and wrote the image to the TREE — outside
    # any namespace
    sq = next(c for c in a.commands if "mksquashfs" in c)
    assert f"mksquashfs {staging_abs} {REL}/image.sqfs" in sq
    assert "unshare" not in sq
    # scaffolding cleanup hits staging; the tree keeps its mountpoint
    tail = next(c for c in reversed(a.commands) if "rm -rf" in c)
    assert staging_abs in tail and f"mkdir -p {mount_abs}" in tail
    # outer activation sidecar still bakes the TREE path
    assert b'__weft_sq="' + REL.encode() in a.files[f"{REL}/activate.sh"]
    assert meta["staging"] == {"used": True, "dir": staging_abs}
    assert ("realize.staged", ) [0] in [e[0] for e in ev]


def test_staged_build_probe_failure_falls_back_honestly():
    a = FakeAdapter(answers={
        "unshare -rm": FakeShim(rc=97, err="unshare: not permitted"),
        "printf '%s %s'": _meta_answer()})
    ev, emit = _events()
    meta = _build_squashfs("env:v1:" + HASH, ENV_ROW, a, REL, [], "",
                           CAPS_USERNS, {}, emit,
                           staging_rel=f"stage/publish/{_sub()}")
    # after the failed probe nothing else attempted a namespace, and the
    # build happened at the destination — today's classic path
    assert sum("unshare -rm" in c for c in a.commands) == 1
    sq = next(c for c in a.commands if "mksquashfs" in c)
    assert f"mksquashfs {REL}/mnt {REL}/image.sqfs" in sq
    assert f"{REL}/mnt/pixi.toml" in a.files
    assert meta["staging"]["used"] is False
    assert "not permitted" in meta["staging"]["why"]
    assert "realize.staging_skipped" in [e[0] for e in ev]


def test_staged_resume_probes_staging_not_the_tree():
    sub = _sub()
    a = FakeAdapter(answers={"printf '%s %s'": _meta_answer()})
    # a previous staged attempt left the right lock in STAGING
    want = ENV_ROW["native_lock"].encode()
    a.files[f"/site/root/stage/publish/{sub}/pixi.lock"] = want
    digest = hashlib.sha256(want).hexdigest()
    a.answers["sha256sum"] = FakeShim(out=f"{digest}  x")
    ev, emit = _events()
    _build_squashfs("env:v1:" + HASH, ENV_ROW, a, REL, [], "",
                    CAPS_USERNS, {}, emit,
                    staging_rel=f"stage/publish/{sub}")
    names = [e[0] for e in ev]
    assert "realize.resumed" in names
    # resume means NO clean-slate wipe of either side before the stages
    resumed_at = names.index("realize.resumed")
    assert resumed_at >= 0
    wipes = [c for c in a.commands
             if c.startswith("rm -rf") and "stage/publish" in c
             and "mkdir -p /site/root/stage" in c]
    assert not wipes


# -- publish() wiring ------------------------------------------------------

def test_publish_passes_site_config_staging_and_reports_honestly(
        monkeypatch, tmp_path):
    """publish() derives the plan from site config and surfaces the
    outcome (used/why) in its result — no silent decisions."""
    from types import SimpleNamespace

    from weft import publish as pub
    from weft import realize as rl

    seen = {}

    def fake_build(env_id, env_row, adapter, rel, modules, modules_init,
                   caps, pack_tools, emit, staging_rel=None):
        seen["staging_rel"] = staging_rel
        return {"image_sha256": "d" * 64, "image_bytes": 7,
                **({"staging": {"used": True,
                                "dir": adapter.path(staging_rel)}}
                   if staging_rel else {})}

    monkeypatch.setattr(rl, "_build_squashfs", fake_build)
    monkeypatch.setattr(rl, "_spot_check_and_mark",
                        lambda *a, **k: None)

    class Store:
        def __init__(self, caps, config):
            self.caps, self.config = caps, config
            self.events = []

        def get_env(self, env_id):
            return {**ENV_ROW, "spec_hash": "s" * 64}

        def get_site(self, site):
            return {"capabilities": self.caps, "config": self.config}

        def get_spec(self, h):
            return {}

        def emit(self, event, **kw):
            self.events.append(event)

        def audit_log(self, *a, **kw):
            pass

    def run(caps, config, staging=None):
        adapter = FakeAdapter()
        store = Store(caps, config)
        weft = SimpleNamespace(
            adapters={"hpc": adapter}, store=store, pixi_pack=None,
            cas=None, transfers={}, envman=SimpleNamespace(solvers={}),
            dataman=None)
        return pub.publish(weft, "env:v1:" + HASH, "hpc", TREE,
                           "lab-py", "1", staging=staging), adapter

    # site config points staging at fast scratch; publish passes it down
    r, _ = run(CAPS_USERNS, {"publish_staging": "/fast/scratch"})
    assert seen["staging_rel"] == f"/fast/scratch/{_sub()}"
    assert r["staging"]["used"] is True

    # no userns: the plan says why, and the result carries it
    r, _ = run(CAPS_DIRECT, {"publish_staging": "/fast/scratch"})
    assert seen["staging_rel"] is None
    assert r["staging"]["used"] is False
    assert "user namespaces" in r["staging"]["why"]

    # explicit opt-out wins over site config
    r, _ = run(CAPS_USERNS, {"publish_staging": "/fast/scratch"},
               staging="none")
    assert seen["staging_rel"] is None
    assert "disabled" in r["staging"]["why"]
