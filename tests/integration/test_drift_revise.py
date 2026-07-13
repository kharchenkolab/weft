"""Step 5: reproduce-else-revise, and near-match as a query (never a
silent substitution)."""

import pytest

from weft.api import Weft
from weft.envman import diff_envs
from weft.spec import current_platform

pytestmark = pytest.mark.solver

# live locks resolve for the controller's platform; the lock-corruption
# helpers must poke at that subdir's URLs, not a hardcoded linux-64
PLAT = current_platform()


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {
        "root": str(tmp_path / "site"), "pixi_source": pixi_bin,
        "policy": {"on_drift": "revise"}})
    return w


def test_diff_is_package_level():
    old = {"platforms": {"linux-64": [
        {"kind": "conda", "name": "numpy", "version": "2.0.1"},
        {"kind": "conda", "name": "gone", "version": "1.0"}]}}
    new = {"platforms": {"linux-64": [
        {"kind": "conda", "name": "numpy", "version": "2.1.0"},
        {"kind": "conda", "name": "fresh", "version": "0.1"}]}}
    d = diff_envs(old, new)
    # keys carry platform and kind: multi-platform envs can move a package
    # on one platform only
    assert d["changed"] == [{"name": "linux-64/conda:numpy",
                             "from": "2.0.1", "to": "2.1.0"}]
    assert d["added"] == ["linux-64/conda:fresh"]
    assert d["removed"] == ["linux-64/conda:gone"]


def _break_lock(w, env_id):
    """The lock references artifacts the index no longer serves."""
    row = w.store.get_env(env_id)
    with w.store._lock, w.store._conn:
        w.store._conn.execute(
            "UPDATE envs SET native_lock=? WHERE env_id=?",
            (row["native_lock"].replace(f"/{PLAT}/xz-", f"/{PLAT}/xz-GONE-"),
             env_id))


def test_reproduce_first_stale_lock_is_re_derived(w):
    """A fresh solve yields the SAME identity → the recorded lock was stale,
    not the world. Re-derive it, keep the EnvID, get the work done. Nothing
    to report to the record: identity is unchanged."""
    env = w.env_ensure({"name": "stale", "deps": {"conda": ["xz >=5"]}})
    env_id = env["env_id"]
    _break_lock(w, env_id)

    r = w.task_submit({"command": "xz --version > results/v.txt",
                       "env": env_id, "outputs": ["results/"],
                       "site": "local"})
    job = w.runner.wait(r["job_id"], 1800)
    assert job["state"] == "DONE", job["error"]
    assert job["manifest"]["env_id"] == env_id          # identity untouched
    kinds = [e["kind"] for e in w.events_poll(0, 500, compact=False)["events"]]
    assert "env.restored" in kinds
    assert "job.env_revised" not in kinds


def test_drift_revises_instead_of_dead_ending(w):
    """The world genuinely moved: the recorded env can't be rebuilt AND a
    fresh solve of its spec yields a different package set. Under
    on_drift=revise the work proceeds under the nearest working env, with the
    delta reported — never a silent redefinition of the old EnvID."""
    real = w.env_ensure({"name": "drifty", "deps": {"conda": ["xz >=5"]}})
    row = w.store.get_env(real["env_id"])

    # An env recorded LAST YEAR: its resolution pinned versions the index no
    # longer serves, so it cannot be rebuilt — and a fresh solve of the same
    # spec now yields a different package set (hence a different EnvID).
    old_canonical = {**row["canonical"]}
    old_canonical["platforms"] = {
        PLAT: [{**p, "version": "0.0.0-withdrawn"}
               for p in row["canonical"]["platforms"][PLAT]]}
    env_id = "env:v1:" + "d" * 64          # the historical EnvID
    w.store.put_env(env_id, row["spec_hash"], old_canonical,
                    row["native_lock"].replace(f"/{PLAT}/xz-",
                                               f"/{PLAT}/xz-GONE-"),
                    row["manifest"], row["platforms"])

    r = w.task_submit({"command": "xz --version > results/v.txt",
                       "env": env_id, "outputs": ["results/"],
                       "site": "local"})
    job = w.runner.wait(r["job_id"], 1800)
    assert job["state"] == "DONE", job["error"]        # work got done

    effective = job["manifest"]["env_id"]
    assert effective != env_id                        # under a NEW identity
    ev = next(e for e in w.events_poll(0, 500, compact=False)["events"]
              if e["kind"] == "job.env_revised")
    assert ev["requested"] == env_id and ev["effective"] == effective
    assert ev["diff"]["changed"] or ev["diff"]["added"]
    # provenance records what actually ran (no silent redefinition)
    p = w.provenance(r["job_id"])
    assert p["environment"]["env_id"] == effective


def test_strict_default_still_dead_ends(tmp_path, pixi_bin):
    """Without on_drift=revise, the strict path is unchanged: a failure is a
    failure, with a cause."""
    w = Weft(tmp_path / "ws2", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site2"),
                                       "pixi_source": pixi_bin})
    env = w.env_ensure({"name": "strict", "deps": {"conda": ["xz >=5"]}})
    row = w.store.get_env(env["env_id"])
    with w.store._lock, w.store._conn:
        w.store._conn.execute(
            "UPDATE envs SET native_lock=? WHERE env_id=?",
            (row["native_lock"].replace(f"/{PLAT}/xz-", f"/{PLAT}/xz-GONE-"),
             env["env_id"]))
    r = w.task_submit({"command": "true", "env": env["env_id"],
                       "site": "local"})
    job = w.runner.wait(r["job_id"], 900)
    assert job["state"] == "FAILED"
    assert job["error"]["error"] == "env.realize_failed"


def test_find_near_is_a_query(w):
    """Near-matches are offered with their diffs; weft never substitutes."""
    base = w.env_ensure({"name": "base", "deps": {"conda": ["xz >=5"]}})
    w.runner.wait(w.task_submit({"command": "true", "env": base["env_id"],
                                 "site": "local"})["job_id"], 900)

    near = w.env_find_near({"name": "wanted",
                            "deps": {"conda": ["xz >=5", "zlib"]}},
                           site="local")
    assert near, "an already-realized close env should be offered"
    top = near[0]
    assert top["env_id"] == base["env_id"]
    assert top["realized_at"] == ["local"]
    assert top["missing_packages"] == ["zlib"]     # exactly what you'd give up
    assert top["grade"] == "fully-pinned"
    # ...and nothing was substituted behind our back: ensuring the real spec
    # still produces its own distinct env
    exact = w.env_ensure({"name": "wanted",
                          "deps": {"conda": ["xz >=5", "zlib"]}})
    assert exact["env_id"] != base["env_id"]
