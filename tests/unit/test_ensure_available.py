"""ensure_available P3: the tagged-mode verb — envelope, pre-check
short-circuit + late-record, per-lane attempts, halting rules, and the
heartbeated one-ensure-per-session claim."""

import pytest

from helpers_verify import (ENV, cold_session, marker, no_toolchain,
                            script_log)
from weft.adapters.base import ShimResult
from weft.errors import WeftError


# ── intake ─────────────────────────────────────────────────────────────────

def test_target_and_request_shapes_refused(tmp_path, pixi_bin):
    w, sid = cold_session(tmp_path, pixi_bin)
    for target in ("s1", {}, {"nonsense": 1}):
        r = w.ensure_available(target, {"pypi": ["idna"]})
        assert r["error"] == "task.invalid", target
    r = w.ensure_available({"env": "env:v1:x"}, ["idna"],
                           lanes=["conda"])           # env + ranked: no
    assert r["error"] == "task.invalid" and "one solve" in r["detail"]
    for req in ("idna", {}, {"julia": ["X"]}):
        r = w.ensure_available({"session": sid}, req)
        assert r["error"] == "task.invalid", req
    r = w.ensure_available({"session": sid}, {"pypi": ["idna"]},
                           lanes=["conda"])          # dict + lanes: no
    assert r["error"] == "task.invalid"
    r = w.ensure_available({"session": sid}, ["idna"])   # list, no lanes
    assert r["error"] == "task.invalid" and "ranking" in r["detail"]
    r = w.ensure_available({"session": sid}, {"pypi": ["idna"]},
                           probe=True)                # probe wants a list
    assert r["error"] == "task.invalid" and "ranked list" in r["detail"]


# ── pre-check: satisfaction is checked, not assumed ────────────────────────

def test_satisfied_short_circuits_and_late_records(tmp_path, pixi_bin,
                                                   monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    log = script_log(monkeypatch, w, {
        "WEFT-VERIFY": marker("idna", got="3.7"),
        "pip install": ShimResult(0, "must not install", "")})
    out = w.ensure_available({"session": sid}, {"pypi": ["idna"]})
    assert out["satisfied"] is True and out["changed"] is False
    assert out["attempts"] == []
    assert out["verified"]["idna"]["status"] == "passed"
    assert out["runtime"]
    # LATE-RECORD: on disk but unrecorded -> recorded at the pre-check
    assert "idna" in w.store.get_session(sid)["added_pypi"]
    assert not [k for k, _ in log if k == "pip install"]   # zero installs


def test_wrong_version_present_is_not_satisfied(tmp_path, pixi_bin,
                                                monkeypatch):
    """Pins give the pre-check meaning: present-but-wrong-version
    enters the install lane."""
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    calls = {"n": 0}

    def oracle():
        calls["n"] += 1
        # pre-check sees 1.0; post-install verify sees 2.0
        return marker("idna", got="1.0" if calls["n"] == 1 else "2.0")

    script_log(monkeypatch, w, {
        "WEFT-VERIFY": oracle,
        "#method": ShimResult(0, "#method uv\nok", "")})   # fast pypi lane
    out = w.ensure_available({"session": sid}, {"pypi": ["idna ==2.0"]})
    assert out["satisfied"] is True and out["changed"] is True
    assert [a["lane"] for a in out["attempts"]] == ["pypi"]
    assert out["attempts"][0]["outcome"] == "installed"
    assert out["verified"]["idna"]["status"] == "passed"


# ── attempts: typed, halting, verbatim ─────────────────────────────────────

def test_lane_failure_is_typed_with_the_envelope(tmp_path, pixi_bin,
                                                 monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    script_log(monkeypatch, w, {
        "WEFT-VERIFY": marker("absentpkg", ok=False),
        "#method": ShimResult(
            1, "", "ERROR: No matching distribution found for absentpkg"),
        "pip install": ShimResult(
            1, "", "ERROR: No matching distribution found for absentpkg")})
    out = w.ensure_available({"session": sid}, {"pypi": ["absentpkg"]})
    assert out["error"] == "env.solve_conflict"      # classifier verbatim
    assert out["hints"]["attempts"][0]["outcome"] == "failed"
    assert out["hints"]["attempts"][0]["error"]["error"] == \
        "env.solve_conflict"
    assert out["hints"]["runtime"]                   # flip moment kept


def test_site_outage_halts_remaining_lanes(tmp_path, pixi_bin,
                                           monkeypatch):
    """An outage is not an unavailability verdict: later lanes are
    SKIPPED (halted), and the top-level code is the outage."""
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)

    def dead():
        raise WeftError("site.unreachable", "ssh transport failed",
                        stage="infra", retryable=True)

    script_log(monkeypatch, w, {
        "WEFT-VERIFY": marker("x", ok=False),
        "#method": dead, "pip install": dead,
        "install.packages": ShimResult(0, "must not run", "")})
    out = w.ensure_available({"session": sid},
                             {"pypi": ["idna"], "cran": ["praise"]})
    assert out["error"] == "site.unreachable" and out["retryable"]
    lanes = {a["lane"]: a for a in out["hints"]["attempts"]}
    assert lanes["pypi"]["outcome"] == "failed"
    assert lanes["cran"]["outcome"] == "skipped"
    assert lanes["cran"]["skip_reason"] == "halted"


def test_verify_false_is_installed_unverified(tmp_path, pixi_bin,
                                              monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    log = script_log(monkeypatch, w, {
        "WEFT-VERIFY": ShimResult(1, "must not run", ""),
        "#method": ShimResult(0, "#method uv\nok", "")})
    out = w.ensure_available({"session": sid}, {"pypi": ["idna"]},
                             verify=False)
    assert out["satisfied"] is True
    assert out["attempts"][0]["outcome"] == "installed_unverified"
    assert not [k for k, _ in log if k == "WEFT-VERIFY"]   # zero oracles


# ── the claim: one ensure per session, heartbeat semantics ─────────────────

def test_concurrent_ensure_refused_retryable(tmp_path, pixi_bin,
                                             monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    assert w.store.claim_session_ensure(sid, "other-holder")
    out = w.ensure_available({"session": sid}, {"pypi": ["idna"]})
    assert out["error"] == "state.conflict" and out["retryable"]
    assert "holder_beat_age_s" in out["hints"]
    w.store.release_session_ensure(sid, "other-holder")


def test_stale_claim_is_taken_over(tmp_path, pixi_bin, monkeypatch):
    """Beat-age staleness, never chain duration (the lease lesson)."""
    import time
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    assert w.store.claim_session_ensure(sid, "dead-holder")
    w.store._write("UPDATE sessions SET ensure_hb=? WHERE session_id=?",
                   (time.time() - 3600, sid))
    script_log(monkeypatch, w, {"WEFT-VERIFY": marker("idna", got="3.7")})
    out = w.ensure_available({"session": sid}, {"pypi": ["idna"]})
    assert out["satisfied"] is True                  # takeover worked
    assert w.store.session_ensure_claim(sid) is None   # and released


def test_ensure_events_emitted(tmp_path, pixi_bin, monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    script_log(monkeypatch, w, {
        "WEFT-VERIFY": marker("idna", got="1.0"),
        "#method": ShimResult(0, "#method uv\nok", "")})
    w.ensure_available({"session": sid}, {"pypi": ["idna"]}, verify=False)
    kinds = [e["kind"] for e in w.store.events_since(0, 300)]
    assert "session.ensure_attempt" in kinds
    assert "session.ensure_done" in kinds


def test_re_ensure_short_circuit_is_cheap(tmp_path, pixi_bin, monkeypatch):
    """Machine cadence + cost budget: a satisfied re-ensure never
    re-solves — pre-check only (<1s each, LOCAL number; per-site
    budgets are the reality run's job)."""
    import time
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    script_log(monkeypatch, w, {"WEFT-VERIFY": marker("idna", got="3.7")})
    w.ensure_available({"session": sid}, {"pypi": ["idna"]})   # warm
    t0 = time.monotonic()
    for _ in range(3):
        out = w.ensure_available({"session": sid}, {"pypi": ["idna"]})
        assert out["changed"] is False and out["attempts"] == []
    assert time.monotonic() - t0 < 3.0


# ── ranked mode (P4) ───────────────────────────────────────────────────────

def _ranked_rig(monkeypatch, script):
    """script: {(pkg, lane): 'installed'|WeftError}"""
    from weft.session import SessionManager
    calls = []

    def fake_install(self, session_id, adapter, conda=None, pypi=None,
                     cran=None, verify=None, **kw):
        lane = "conda" if conda else ("pypi" if pypi else "cran")
        pkg = (conda or pypi or cran)[0]
        calls.append((pkg, lane))
        r = script[(pkg, lane)]
        if r == "installed":
            return {"installed": {lane: [pkg]},
                    "verified": {pkg: {"status": "passed",
                                       "check": "metadata"}},
                    "session_id": session_id}
        raise r

    monkeypatch.setattr(SessionManager, "install", fake_install)

    def fake_exec_fn(self, s, adapter):
        def run(script_text, timeout):
            return ShimResult(0, "", "")     # no markers: pre-check unknown
        return run

    from weft.session import SessionManager as SM
    monkeypatch.setattr(SM, "_verify_exec_fn", fake_exec_fn)
    return calls


def test_ranked_packages_chain_independently(tmp_path, pixi_bin,
                                             monkeypatch):
    """Package A succeeds in lane 1; package B falls through to lane 2;
    package C exhausts — one call, per-package outcomes, top-level
    exhaustion with satisfied/unsatisfied discriminated."""
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    fail = WeftError("env.solve_conflict", "not here", stage="solve")
    calls = _ranked_rig(monkeypatch, {
        ("aa", "conda"): "installed",
        ("bb", "conda"): fail, ("bb", "pypi"): "installed",
        ("cc", "conda"): fail, ("cc", "pypi"): fail})
    out = w.ensure_available({"session": sid}, ["aa", "bb", "cc"],
                             lanes=["conda", "pypi"])
    assert out["error"] == "env.unavailable_in_lanes"
    assert out["hints"]["satisfied"] == ["aa", "bb"]
    assert out["hints"]["unsatisfied"] == ["cc"]
    assert ("bb", "pypi") in calls and ("aa", "pypi") not in calls
    assert "suggestion" not in out["hints"]      # attempts ARE the advice


def test_ranked_grammar_skip(tmp_path, pixi_bin, monkeypatch):
    """A github ref in a conda/pypi lane is a SKIPPED lane, not a
    burned typed failure."""
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    calls = _ranked_rig(monkeypatch, {("org/widget@v1", "cran"):
                                      "installed"})
    out = w.ensure_available({"session": sid}, ["org/widget@v1"],
                             lanes=["conda", "pypi", "cran"])
    assert out["satisfied"] is True
    outcomes = {a["lane"]: a["outcome"] for a in out["attempts"]}
    assert outcomes["conda"] == "skipped" == outcomes["pypi"]
    assert out["attempts"][0]["skip_reason"] == "grammar"
    assert outcomes["cran"] == "installed"
    assert calls == [("org/widget@v1", "cran")]


def test_crash_mid_chain_releases_claim_and_converges(tmp_path, pixi_bin,
                                                      monkeypatch):
    """Crash injection: the claim never outlives the ensure; re-ensure
    converges."""
    from weft.session import SessionManager
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)

    def boom(self, *a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(SessionManager, "install", boom)
    monkeypatch.setattr(
        SessionManager, "_verify_exec_fn",
        lambda self, s, ad: lambda sc, t: ShimResult(0, "", ""))
    with pytest.raises(KeyboardInterrupt):
        w.sessions.ensure_available(sid, w.adapters["local"], ["aa"],
                                    lanes=["conda"])
    assert w.store.session_ensure_claim(sid) is None    # released
    calls = _ranked_rig(monkeypatch, {("aa", "conda"): "installed"})
    out = w.ensure_available({"session": sid}, ["aa"], lanes=["conda"])
    assert out["satisfied"] is True                     # converges


# ── P5: dialect, probe, env target ─────────────────────────────────────────

def test_dialect_spelling_rides_the_chain(tmp_path, pixi_bin,
                                          monkeypatch):
    """An R-namespace bare name derives conda's r-<lowercase> spelling;
    the attempt records the spelling ACTUALLY used."""
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    fail = WeftError("env.solve_conflict", "no conda build", stage="solve")
    calls = _ranked_rig(monkeypatch, {
        ("RNetCDF", "conda"): fail,          # keyed by DISPLAY name
        ("RNetCDF", "cran"): "installed"})
    from weft.session import SessionManager
    spellings = []

    def keyed(self, session_id, adapter, conda=None, pypi=None,
              cran=None, verify=None, **kw):
        lane = "conda" if conda else ("pypi" if pypi else "cran")
        spelling = (conda or pypi or cran)[0]
        spellings.append((lane, spelling))
        if lane == "conda":
            raise fail
        return {"installed": {lane: [spelling]},
                "verified": {"RNetCDF": {"status": "passed",
                                         "check": "loads"}},
                "session_id": session_id}

    monkeypatch.setattr(SessionManager, "install", keyed)
    out = w.ensure_available({"session": sid}, ["RNetCDF"],
                             lanes=["conda", "cran"])
    assert out["satisfied"] is True
    assert ("conda", "r-rnetcdf") in spellings       # dialect derived
    assert ("cran", "RNetCDF") in spellings          # registry name kept
    atts = {a["lane"]: a for a in out["attempts"]}
    assert atts["conda"]["spelling"] == "r-rnetcdf"
    assert atts["cran"]["spelling"] == "RNetCDF"


def test_dialect_requires_effective_verify(tmp_path, pixi_bin,
                                           monkeypatch):
    """The NOT-X guard: derivation without a postcondition is the
    unguarded-translation back door — refused with both levers."""
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    out = w.ensure_available({"session": sid}, ["RNetCDF"],
                             lanes=["conda", "cran"], verify=False)
    assert out["error"] == "task.invalid"
    assert "levers" in out["hints"]


def test_bare_name_across_cran_and_pypi_is_ambiguous(tmp_path, pixi_bin,
                                                     monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    out = w.ensure_available({"session": sid}, ["thing"],
                             lanes=["pypi", "cran"])
    assert out["error"] == "task.invalid"
    assert "ambiguous" in out["detail"]
    # per-lane spellings are the escape
    calls = _ranked_rig(monkeypatch, {("thing", "pypi"): "installed"})
    out2 = w.ensure_available(
        {"session": sid},
        [{"name": "thing", "pypi": "thing", "cran": "Thing"}],
        lanes=["pypi", "cran"])
    assert out2["satisfied"] is True


def test_env_target_mints_and_reports(tmp_path, pixi_bin, monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    monkeypatch.setattr(w, "env_ensure",
                        lambda spec: {"env_id": "env:v1:childchild"})
    out = w.ensure_available({"env": "env:v1:deadbeefcafe"},
                             {"pypi": ["emcee"]})
    assert out["satisfied"] is True and out["changed"] is True
    assert out["env_id"] == "env:v1:childchild"
    assert out["attempts"][0]["lane"] == "extends_env"
    assert out["attempts"][0]["outcome"] == "solved"
    monkeypatch.setattr(w, "env_ensure", lambda spec: {
        "error": "env.layer_conflict", "stage": "solve",
        "detail": "contradicts frozen pin", "retryable": False,
        "hints": {}})
    out2 = w.ensure_available({"env": "env:v1:deadbeefcafe"},
                              {"pypi": ["emcee ==99"]})
    assert out2["error"] == "env.layer_conflict"
    assert out2["hints"]["attempts"][0]["outcome"] == "failed"


def test_probe_honesty(tmp_path, pixi_bin, monkeypatch):
    """404 is FALSE (the index's answer); transport trouble is UNKNOWN
    — never false (an agent ranking on a false fact)."""
    import io
    import urllib.error

    def fake_urlopen(req, timeout=15.0):
        url = req.full_url
        if "pypi.org" in url:
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        if "anaconda.org" in url:
            return io.BytesIO(b'{"latest_version": "5.4.6"}')
        raise urllib.error.URLError("proxy down")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    w, sid = cold_session(tmp_path, pixi_bin)
    out = w.ensure_available(
        {"session": sid},
        [{"name": "xz", "conda": "xz", "pypi": "xz", "cran": "xz"}],
        lanes=["conda", "pypi", "cran"], probe=True)
    facts = out["candidates"]["xz"]
    assert facts["conda"]["available"] is True
    assert facts["conda"]["version_latest"] == "5.4.6"
    assert facts["pypi"]["available"] is False
    assert facts["cran"]["available"] == "unknown"
    assert "proxy" in facts["cran"]["reason"]


def test_probe_uses_the_one_dialect_function(tmp_path, pixi_bin,
                                             monkeypatch):
    from weft import probe as probe_mod
    asked = []
    monkeypatch.setattr(probe_mod, "_BACKENDS", {
        "conda": lambda n: (asked.append(("conda", n)) or
                            {"available": True, "spelling": n}),
        "cran": lambda n: (asked.append(("cran", n)) or
                           {"available": True, "spelling": n}),
        "pypi": lambda n: {"available": True, "spelling": n}})
    w, sid = cold_session(tmp_path, pixi_bin)
    w.ensure_available({"session": sid}, ["RNetCDF"],
                       lanes=["conda", "cran"], probe=True)
    assert ("conda", "r-rnetcdf") in asked      # the dialect, in probe
    assert ("cran", "RNetCDF") in asked


# ── aba check-in: ranked cran_repos + env-target site= verify-now ──────────

def test_cran_repos_ride_the_cran_lane_only(tmp_path, pixi_bin,
                                            monkeypatch):
    """Repositories reach exactly the cran lane's install and ride the
    attempt as provenance (like spelling); other lanes never see them."""
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    from weft.session import SessionManager
    seen = []

    def keyed(self, session_id, adapter, conda=None, pypi=None,
              cran=None, verify=None, cran_repos=None, **kw):
        lane = "conda" if conda else ("pypi" if pypi else "cran")
        seen.append((lane, cran_repos))
        if lane == "conda":
            raise WeftError("env.solve_conflict", "no build",
                            stage="solve")
        return {"installed": {lane: cran},
                "verified": {"RNetCDF": {"status": "passed",
                                         "check": "loads"}},
                "session_id": session_id}

    monkeypatch.setattr(SessionManager, "install", keyed)
    out = w.ensure_available({"session": sid}, ["RNetCDF"],
                             lanes=["conda", "cran"],
                             cran_repos=["https://r.example.org/repo"])
    assert out["satisfied"] is True
    assert ("conda", None) in seen                    # never leaked
    assert ("cran", ["https://r.example.org/repo"]) in seen
    cran_att = next(a for a in out["attempts"] if a["lane"] == "cran")
    assert cran_att["repositories"] == ["https://r.example.org/repo"]
    conda_att = next(a for a in out["attempts"] if a["lane"] == "conda")
    assert "repositories" not in conda_att


def test_cran_repos_need_a_cran_lane(tmp_path, pixi_bin, monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    out = w.ensure_available({"session": sid}, ["numpy"],
                             lanes=["conda"],
                             cran_repos=["https://r.example.org"])
    assert out["error"] == "task.invalid"
    out2 = w.ensure_available({"session": sid}, {"pypi": ["numpy"]},
                              cran_repos=["https://r.example.org"])
    assert out2["error"] == "task.invalid"


def test_cran_repos_hostile_urls_refused_at_intake(tmp_path, pixi_bin):
    """Malformed-input lane: the URLs later render into R code — refuse
    at intake, never accept-and-mangle."""
    w, sid = cold_session(tmp_path, pixi_bin)
    for bad in (["ftp://mirror.example"],           # not http(s)
                ["https://r.example.org/\nevil"],   # container-breaking
                "https://r.example.org",            # not a list
                [42]):
        out = w.ensure_available({"session": sid}, {"cran": ["pkgA"]},
                                 cran_repos=bad)
        assert out["error"] == "task.invalid", bad
        out2 = w.session_install(sid, cran=["pkgA"], cran_repos=bad)
        assert out2["error"] == "task.invalid", bad


def test_probe_with_secondary_repos_is_unknown_never_false(tmp_path,
                                                           pixi_bin):
    """crandb indexes CRAN only: with extra repositories the cran probe
    must answer unknown — a package living in the secondary registry
    would otherwise probe FALSE, a lie an agent would rank on."""
    w, sid = cold_session(tmp_path, pixi_bin)
    out = w.ensure_available({"session": sid}, ["secretpkg"],
                             lanes=["cran"], probe=True,
                             cran_repos=["https://r.example.org"])
    fact = out["candidates"]["secretpkg"]["cran"]
    assert fact["available"] == "unknown"
    assert "not probeable" in fact["reason"]


def test_env_target_cran_repos_ride_the_spec(tmp_path, pixi_bin,
                                             monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    captured = {}
    monkeypatch.setattr(
        w, "env_ensure",
        lambda spec: captured.update(spec) or {"env_id": "env:v1:kid"})
    out = w.ensure_available({"env": ENV}, {"cran": ["pkgA"]},
                             cran_repos=["https://r.example.org"])
    assert out["satisfied"] is True
    assert captured["r_repositories"] == ["https://r.example.org"]
    out2 = w.ensure_available({"env": ENV}, {"pypi": ["numpy"]},
                              cran_repos=["https://r.example.org"])
    assert out2["error"] == "task.invalid"    # repos with no cran delta


def test_env_target_site_verifies_now_against_ready_realization(
        tmp_path, pixi_bin, monkeypatch):
    """The re-extend shape (aba item 1): the result env already realized
    on the site -> the claim is proven NOW, no realize forced, and the
    envelope SAYS so (verified populated + verified_site)."""
    from helpers_verify import marker, script_log
    w, sid = cold_session(tmp_path, pixi_bin)
    monkeypatch.setattr(w, "env_ensure", lambda spec: {"env_id": ENV})
    script_log(monkeypatch, w, {"WEFT-VERIFY": marker("plotpkg")})
    out = w.ensure_available({"env": ENV}, {"pypi": ["plotpkg"]},
                             site="local")
    assert out["satisfied"] is True and out["changed"] is False
    assert out["verified"]["plotpkg"]["status"] == "passed"
    assert out["verified_site"] == "local"
    assert "verified now" in out["note"]


def test_env_target_site_failing_claim_is_a_degraded_realization(
        tmp_path, pixi_bin, monkeypatch):
    from helpers_verify import marker, script_log
    w, sid = cold_session(tmp_path, pixi_bin)
    monkeypatch.setattr(w, "env_ensure", lambda spec: {"env_id": ENV})
    script_log(monkeypatch, w,
               {"WEFT-VERIFY": marker("plotpkg", ok=False)})
    out = w.ensure_available({"env": ENV}, {"pypi": ["plotpkg"]},
                             site="local")
    assert out["error"] == "env.realize_failed"
    assert out["hints"]["postcondition"] is True
    assert "env_repair" in out["hints"]["levers"]
    assert out["hints"]["env_id"] == ENV
    assert out["hints"]["attempts"][0]["outcome"] == "solved"


def test_env_target_site_unrealized_defers_and_says_so(tmp_path,
                                                       pixi_bin,
                                                       monkeypatch):
    """A fresh child env has no realization anywhere: never realized on
    the caller's clock — the note names the deferral, verified stays
    empty (the machine discriminator aba relays to the agent)."""
    w, sid = cold_session(tmp_path, pixi_bin)
    monkeypatch.setattr(w, "env_ensure",
                        lambda spec: {"env_id": "env:v1:neverbuilt"})
    out = w.ensure_available({"env": ENV}, {"pypi": ["plotpkg"]},
                             site="local")
    assert out["satisfied"] is True
    assert out["verified"] == {} and "verified_site" not in out
    assert "not realized on 'local'" in out["note"]


def test_env_target_site_unknown_oracle_keeps_enforcement_deferred(
        tmp_path, pixi_bin, monkeypatch):
    """Fail-closed: an oracle that cannot run is unknown, never a
    verdict — the note keeps enforcement at realize."""
    from helpers_verify import script_log
    from weft.adapters.base import ShimResult
    w, sid = cold_session(tmp_path, pixi_bin)
    monkeypatch.setattr(w, "env_ensure", lambda spec: {"env_id": ENV})
    script_log(monkeypatch, w,
               {"WEFT-VERIFY": ShimResult(0, "garbage no marker", "")})
    out = w.ensure_available({"env": ENV}, {"pypi": ["plotpkg"]},
                             site="local")
    assert out["satisfied"] is True
    assert out["verified"]["plotpkg"]["status"] == "unknown"
    assert "enforcement" in out["note"] and "realize" in out["note"]


def test_site_on_session_target_refused_loudly(tmp_path, pixi_bin):
    """site= is an env-target lever; silently ignoring it on a session
    target would let a caller believe verify-now ran somewhere."""
    w, sid = cold_session(tmp_path, pixi_bin)
    out = w.ensure_available({"session": sid}, {"pypi": ["numpy"]},
                             site="local")
    assert out["error"] == "task.invalid"
    assert "session" in out["detail"]


def test_probe_cran_repos_without_cran_lane_refused(tmp_path, pixi_bin):
    w, sid = cold_session(tmp_path, pixi_bin)
    out = w.ensure_available({"session": sid}, ["numpy"],
                             lanes=["pypi"], probe=True,
                             cran_repos=["https://r.example.org"])
    assert out["error"] == "task.invalid"
