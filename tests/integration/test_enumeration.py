"""Enumeration round (aba items 3/4/6/7 + weft-ui round 24): consumers
rendering live projections need enumeration verbs with stable cursors,
not N point reads — and the small congruent extensions that fell out of
the same ask note (storage role lists, tree member names, a digested
compute block)."""

import time

import pytest

from weft.api import PUBLIC_TOOLS, Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


# -- data_list ----------------------------------------------------------------

def test_data_list_pages_stably_with_locations(w, tmp_path):
    refs = []
    for i in range(5):
        p = tmp_path / f"in{i}.bin"
        p.write_bytes(b"x" * (100 + i))
        refs.append(w.data_register(str(p))["ref"])
    page1 = w.data_list(limit=3)
    assert len(page1["refs"]) == 3 and page1["next_cursor"]
    page2 = w.data_list(limit=3, cursor=page1["next_cursor"])
    assert len(page2["refs"]) == 2 and page2.get("next_cursor") is None
    seen = [r["ref"] for r in page1["refs"] + page2["refs"]]
    assert set(seen) == set(refs) and len(seen) == len(set(seen)), \
        "pages must partition the refs exactly — no dupes, no gaps"
    row = page1["refs"][0]
    assert set(row) >= {"ref", "kind", "bytes", "meta", "locations"}
    assert all("external" in loc for loc in row["locations"]), \
        "locations carry the typed external flag (round 26 contract)"


def test_data_list_filters_by_kind_and_site(w, tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"file-bytes")
    w.data_register(str(f))
    d = tmp_path / "tree"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "m.txt").write_text("member")
    tref = w.data_register(str(d))["ref"]
    trees = w.data_list(kind="tree")
    assert [r["ref"] for r in trees["refs"]] == [tref]
    nowhere = w.data_list(at="local")
    assert nowhere["refs"] == [], "nothing was staged to the site yet"


def test_data_list_cursor_stable_under_inserts(w, tmp_path):
    """Keyset cursor: rows registered AFTER a page was cut must not
    shift or duplicate the next page (the offset-pagination failure
    weft-ui hit on jobs_where)."""
    for i in range(4):
        p = tmp_path / f"s{i}.bin"
        p.write_bytes(bytes([i]) * 50)
        w.data_register(str(p))
    page1 = w.data_list(limit=2)
    got1 = {r["ref"] for r in page1["refs"]}
    late = tmp_path / "late.bin"
    late.write_bytes(b"late" * 25)
    w.data_register(str(late))
    rest_refs = set()
    cursor = page1["next_cursor"]
    while cursor:
        page = w.data_list(limit=2, cursor=cursor)
        rest_refs |= {r["ref"] for r in page["refs"]}
        cursor = page.get("next_cursor")
    assert not (got1 & rest_refs), "cursor re-served a row from page 1"
    assert len(got1 | rest_refs) == 5


# -- audit_tail filters -------------------------------------------------------

def test_audit_tail_filters_and_cursor(w):
    w.site_note("local", "one")
    with w.as_actor("agent:c-9"):
        w.site_note("local", "two")
    w.site_note("local", "three")
    out = w.audit_tail(50, actor="agent:c-9")
    assert [r["actor"] for r in out["audit"]] == ["agent:c-9"]
    acts = w.audit_tail(50, action="site.note")
    assert len(acts["audit"]) == 3
    ts_mid = acts["audit"][1]["ts"]
    since = w.audit_tail(50, action="site.note", since=ts_mid)
    assert len(since["audit"]) == 2, "since= is inclusive of the boundary row"
    # seq cursor pages BACKWARD from the tail
    first_page = w.audit_tail(2, action="site.note")
    assert len(first_page["audit"]) == 2 and first_page["next_before_seq"]
    older = w.audit_tail(2, action="site.note",
                         before_seq=first_page["next_before_seq"])
    assert len(older["audit"]) == 1
    seqs = [r["seq"] for r in older["audit"] + first_page["audit"]]
    assert seqs == sorted(seqs), "rows stay in seq order within and across pages"


# -- jobs_where keyset cursor -------------------------------------------------

def test_jobs_where_keyset_cursor_partitions(w):
    jids = [w.task_submit({"command": f"echo {i}", "site": "local"})["job_id"]
            for i in range(4)]
    for j in jids:
        w.runner.wait(j, 120)
    page1 = w.jobs_where(limit=3)
    assert len(page1["jobs"]) == 3 and page1["next_cursor"]
    page2 = w.jobs_where(limit=3, cursor=page1["next_cursor"])
    got = [j["job_id"] for j in page1["jobs"] + page2["jobs"]]
    assert set(got) == set(jids) and len(got) == 4
    assert page2.get("next_cursor") is None


# -- storage roles as lists ---------------------------------------------------

def test_storage_role_list_first_is_env_var(w, tmp_path):
    from weft.policy import storage_env_vars
    policy = {"storage": {"large": ["/groups/a", "/groups/b"],
                          "scratch": "/scratch/me"}}
    env = storage_env_vars(policy)
    assert env["WEFT_STORAGE_LARGE"] == "/groups/a", \
        "first entry stays THE env var — one path per role by contract"
    assert env["WEFT_STORAGE_SCRATCH"] == "/scratch/me"


def test_storage_role_list_surfaces_in_describe(w, tmp_path):
    w.register_site("shelf", "local", {
        "root": str(tmp_path / "shelf"),
        "policy": {"storage": {"large": ["/groups/a", "/groups/b"]}}})
    facts = w.sites_describe("shelf")["storage"]
    assert facts["roles"]["large"] == ["/groups/a", "/groups/b"]


def test_storage_role_hostile_intake_refuses(w, tmp_path):
    """Public verbs are a returns-never-raises boundary: the refusal is
    the PAYLOAD (typed, with the offending role named), and no site row
    may be created by a refused registration."""
    for i, bad in enumerate(([], [42], ["/ok", ""], "  ")):
        out = w.register_site(f"bad{i}", "local", {
            "root": str(tmp_path / "b"),
            "policy": {"storage": {"large": bad}}})
        assert out.get("error") == "task.invalid", (bad, out)
        assert "storage.large" in out["detail"], out
        assert f"bad{i}" not in {s["name"] for s in w.store.list_sites()}


# -- data_members -------------------------------------------------------------

def test_data_members_manifest_order_and_pagination(w, tmp_path):
    d = tmp_path / "tree"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("alpha")
    (d / "b.txt").write_text("beta-beta")
    (d / "sub" / "c.txt").write_text("gamma")
    ref = w.data_register(str(d))["ref"]
    out = w.data_members(ref)
    paths = [m["path"] for m in out["members"]]
    manifest = [e["path"] for e in w.cas.tree_manifest(ref)]
    assert paths == manifest, "members come in MANIFEST order (prefetch case)"
    assert all(set(m) >= {"path", "bytes", "sha256"} for m in out["members"])
    assert out["total"] == 3
    p1 = w.data_members(ref, limit=2)
    assert len(p1["members"]) == 2 and p1["next_cursor"]
    p2 = w.data_members(ref, limit=2, cursor=p1["next_cursor"])
    assert [m["path"] for m in p1["members"] + p2["members"]] == manifest


def test_data_members_refusals(w, tmp_path):
    f = tmp_path / "flat.bin"
    f.write_bytes(b"not-a-tree")
    fref = w.data_register(str(f))["ref"]
    out = w.data_members(fref)
    assert out["error"] == "task.invalid", out   # files have no members
    out = w.data_members("dref:" + "0" * 64)
    assert out["error"] == "data.missing", out


# -- sites_describe compute digest --------------------------------------------

def test_sites_describe_compute_digest(w):
    row = w.sites_describe("local")
    comp = row["compute"]
    assert set(comp) >= {"gpus", "os", "arch"}, comp
    from weft.capability import compute_view
    assert comp == compute_view(row.get("capabilities") or {}), \
        "ONE owner: the digest is compute_view verbatim, not a re-derivation"


def test_new_verbs_are_public_tools(w):
    for verb in ("data_list", "data_members"):
        assert verb in PUBLIC_TOOLS, verb
