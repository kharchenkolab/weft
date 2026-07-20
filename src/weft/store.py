"""Durable state: one SQLite database + an in-process event bus (doc 01 §6).

Single-writer by design. WAL mode; a process-wide RLock serializes *all*
access (reads included — CPython's sqlite3 does not tolerate concurrent
cursor use on one connection across threads). Remote state is the source
of truth for running jobs — rows here are reconciled snapshots.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS specs(
  spec_hash TEXT PRIMARY KEY, name TEXT, body TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS envs(
  env_id TEXT PRIMARY KEY, spec_hash TEXT, canonical TEXT, native_lock TEXT,
  manifest TEXT, platforms TEXT, weakly_reproducible INTEGER DEFAULT 0,
  created_at REAL);
CREATE TABLE IF NOT EXISTS realizations(
  env_id TEXT, site TEXT, strategy TEXT, location TEXT,
  state TEXT, log TEXT, created_at REAL, updated_at REAL,
  PRIMARY KEY(env_id, site));
CREATE TABLE IF NOT EXISTS datarefs(
  ref TEXT PRIMARY KEY, kind TEXT, bytes INTEGER, chunks TEXT, meta TEXT);
CREATE TABLE IF NOT EXISTS locations(
  ref TEXT, site TEXT, path TEXT, present INTEGER, verified_at REAL,
  PRIMARY KEY(ref, site));
CREATE TABLE IF NOT EXISTS sites(
  name TEXT PRIMARY KEY, kind TEXT, config TEXT, capabilities TEXT,
  health TEXT DEFAULT 'unknown', probed_at REAL);
CREATE TABLE IF NOT EXISTS jobs(
  job_id TEXT PRIMARY KEY, task_hash TEXT, task TEXT, site TEXT,
  state TEXT, sched_handle TEXT, error TEXT, manifest TEXT,
  created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS jobs_by_state ON jobs(state);
CREATE INDEX IF NOT EXISTS jobs_by_task ON jobs(task_hash);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT,
  job_id TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS audit(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, action TEXT,
  site TEXT, command TEXT, why TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, base_env_id TEXT, site TEXT, location TEXT,
  added_conda TEXT, added_pypi TEXT, state TEXT, created_at REAL);
"""

_SESSION_MIGRATIONS = [
    ("installers", "ALTER TABLE sessions ADD COLUMN installers TEXT"),
    ("last_used", "ALTER TABLE sessions ADD COLUMN last_used REAL"),
    # lazy clone (parallel-FS round): NULL reads as materialized — every
    # pre-migration session was cloned eagerly at start
    ("materialized", "ALTER TABLE sessions ADD COLUMN materialized INTEGER"),
]


def _j(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True)


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
        self._subscribers: list[Callable[[dict], None]] = []

    def _migrate(self) -> None:
        """Additive migrations for stores created by older versions."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        if "array_group" not in cols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN array_group TEXT")
            self._conn.execute("ALTER TABLE jobs ADD COLUMN array_index INTEGER")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS jobs_by_group ON jobs(array_group)"
            )
        if "queue_reason" not in cols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN queue_reason TEXT")
        if "superseded_by" not in cols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN superseded_by TEXT")
        kcols = {r[1] for r in self._conn.execute("PRAGMA table_info(kernels)")}
        if kcols and "label" not in kcols:
            self._conn.execute("ALTER TABLE kernels ADD COLUMN label TEXT")
        scols = {r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        for col, ddl in _SESSION_MIGRATIONS:
            if col not in scols:
                self._conn.execute(ddl)
        ecols = {r[1] for r in self._conn.execute("PRAGMA table_info(envs)")}
        if "parent_env_id" not in ecols:
            self._conn.execute("ALTER TABLE envs ADD COLUMN parent_env_id TEXT")
            self._conn.execute("ALTER TABLE envs ADD COLUMN layerable INTEGER")
        rcols = {r[1] for r in
                 self._conn.execute("PRAGMA table_info(realizations)")}
        if "bytes" not in rcols:
            self._conn.execute("ALTER TABLE realizations ADD COLUMN bytes INTEGER")
            self._conn.execute("ALTER TABLE realizations ADD COLUMN last_used REAL")
        if "read_only" not in rcols:
            self._conn.execute(
                "ALTER TABLE realizations ADD COLUMN read_only INTEGER DEFAULT 0")
        kcols = {r[1] for r in
                 self._conn.execute("PRAGMA table_info(kernels)")}
        # empty = fresh DB, the CREATE below carries the column already
        if kcols and "session_id" not in kcols:
            self._conn.execute(
                "ALTER TABLE kernels ADD COLUMN session_id TEXT")
        if kcols and "capture" not in kcols:
            self._conn.execute(
                "ALTER TABLE kernels ADD COLUMN capture TEXT")
        # retention.md R1: the terminal receipt of what a run left behind.
        # KNOWLEDGE, not holdings — outlives sweeps, retain and forget.
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS run_inventories("
            "target TEXT PRIMARY KEY, site TEXT, recorded_at REAL,"
            "entries TEXT, truncated INTEGER, total INTEGER)"
        )
        # retention.md R6: durable kernel transcripts — block code/rc/
        # capped text mirrored as OBSERVED, so text saves and
        # restart-replay survive sandbox sweeps and scratch purges
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kernel_blocks("
            "kernel_id TEXT, block INTEGER, code TEXT, rc INTEGER,"
            "out TEXT, err TEXT, recorded_at REAL,"
            "PRIMARY KEY(kernel_id, block))"
        )
        # retention.md R2: HOLDINGS — where each retained run's files
        # live (workspace runs dir or a site retain.dir). Dropped by
        # run_forget on confirmed deletion; never by TTL.
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS retained_runs("
            "target TEXT PRIMARY KEY, site TEXT, label TEXT,"
            "location TEXT, in_place INTEGER, files INTEGER, bytes INTEGER,"
            "method TEXT, state TEXT, error TEXT, retained_at REAL,"
            "selection TEXT, moved INTEGER)"
        )
        rrcols = {r[1] for r in
                  self._conn.execute("PRAGMA table_info(retained_runs)")}
        if rrcols and "moved" not in rrcols:
            # v1 rows all moved (mark-in-place is a v2 concept); NULL
            # reads as moved for forget semantics
            self._conn.execute(
                "ALTER TABLE retained_runs ADD COLUMN moved INTEGER")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS spec_aliases("
            "spec_hash TEXT PRIMARY KEY, env_id TEXT, created_at REAL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS site_notes("
            "site TEXT, ts REAL, author TEXT, note TEXT)"
        )
        # submit-time plans, queryable post-restart: scope is a job_id, or
        # a group id for arrays (one plan for the fan-out, not N copies)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS plans("
            "scope TEXT PRIMARY KEY, plan TEXT, created_at REAL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS site_routes("
            "src TEXT, dst TEXT, shared_fs_path TEXT, direct_ssh INTEGER,"
            " src_addr TEXT, probed_at REAL, PRIMARY KEY(src, dst))"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS metrics("
            "ts REAL, site TEXT, key TEXT, value REAL)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS metrics_by_key ON metrics(site, key)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS services("
            "service_id TEXT PRIMARY KEY, site TEXT, jobdir TEXT,"
            "handle TEXT, ports TEXT, state TEXT, task TEXT,"
            "created_at REAL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kernels("
            "kernel_id TEXT PRIMARY KEY, site TEXT, lang TEXT, env_id TEXT,"
            "jobdir TEXT, handle TEXT, state TEXT, blocks_run INTEGER,"
            "created_at REAL, last_used REAL, label TEXT, session_id TEXT,"
            "capture TEXT)"
        )

    # -- serialized access helpers ------------------------------------------

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self._lock, self._conn:
            return self._conn.execute(sql, params).lastrowid

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _row(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    # -- events ---------------------------------------------------------

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._subscribers.append(fn)

    def emit(self, kind: str, job_id: str | None = None, **payload) -> int:
        seq = self._write(
            "INSERT INTO events(ts, kind, job_id, payload) VALUES(?,?,?,?)",
            (time.time(), kind, job_id, _j(payload)),
        )
        event = {"seq": seq, "kind": kind, "job_id": job_id, **payload}
        for fn in list(self._subscribers):
            try:
                fn(event)
            except Exception:
                pass  # subscribers must not break the emitter
        return seq

    def events_since(self, cursor: int, limit: int = 200) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (cursor, limit)
        )
        return [
            {
                "seq": r["seq"], "ts": r["ts"], "kind": r["kind"],
                "job_id": r["job_id"], **json.loads(r["payload"]),
            }
            for r in rows
        ]

    # who acts through this workspace: "agent" unless the EMBEDDER (a UI
    # serving a human, a notebook) says otherwise at construction. Never
    # settable per call from tool arguments — an agent could then write
    # someone else's name into the trail.
    audit_actor = "agent"

    def audit_log(
        self, actor: str | None, action: str, *, site: str = "",
        command: str = "", why: str = "", result: str = "",
    ) -> None:
        self._write(
            "INSERT INTO audit(ts, actor, action, site, command, why, result)"
            " VALUES(?,?,?,?,?,?,?)",
            (time.time(), actor or self.audit_actor, action, site, command,
             why, result[:4000]),
        )

    def audit_tail(self, n: int = 50) -> list[dict]:
        rows = self._rows("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (n,))
        return [dict(r) for r in reversed(rows)]

    # -- specs / envs -----------------------------------------------------

    def put_spec(self, spec_hash: str, name: str, body: dict) -> None:
        # upsert: same hash = same semantic spec (the hash excludes only
        # identity-neutral notes), so last-write-wins is exactly how notes
        # get attached to an existing spec without forking its identity
        self._write(
            "INSERT INTO specs(spec_hash, name, body, created_at) "
            "VALUES(?,?,?,?) ON CONFLICT(spec_hash) DO UPDATE SET "
            "name=excluded.name, body=excluded.body",
            (spec_hash, name, _j(body), time.time()),
        )

    def set_route(self, src: str, dst: str, shared_fs_path: str | None,
                  direct_ssh: bool, src_addr: str = "") -> None:
        self._write(
            "INSERT INTO site_routes(src, dst, shared_fs_path, direct_ssh,"
            " src_addr, probed_at) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(src, dst) DO UPDATE"
            " SET shared_fs_path=?, direct_ssh=?, src_addr=?, probed_at=?",
            (src, dst, shared_fs_path, int(direct_ssh), src_addr,
             time.time(),
             shared_fs_path, int(direct_ssh), src_addr, time.time()))

    def get_route(self, src: str, dst: str) -> dict | None:
        r = self._row("SELECT * FROM site_routes WHERE src=? AND dst=?",
                      (src, dst))
        return dict(r) if r else None

    def forget_site(self, name: str) -> None:
        """Drop everything the registration created: the site row, its
        routes (both directions), its realization records, and its data
        locations. Nothing site-side is touched, and datarefs stay —
        copies elsewhere (or the workspace CAS) remain fetchable."""
        self._write("DELETE FROM sites WHERE name=?", (name,))
        self._write("DELETE FROM site_routes WHERE src=? OR dst=?",
                    (name, name))
        self._write("DELETE FROM realizations WHERE site=?", (name,))
        self._write("DELETE FROM locations WHERE site=?", (name,))

    def routes_for(self, site: str) -> list[dict]:
        return [dict(r) for r in self._rows(
            "SELECT * FROM site_routes WHERE src=? OR dst=?", (site, site))]

    def add_site_note(self, site: str, note: str,
                      author: str = "agent") -> None:
        self._write(
            "INSERT INTO site_notes(site, ts, author, note) VALUES(?,?,?,?)",
            (site, time.time(), author, note))

    def site_notes(self, site: str, limit: int = 50) -> list[dict]:
        rows = self._rows(
            "SELECT ts, author, note FROM site_notes WHERE site=? "
            "ORDER BY ts DESC LIMIT ?", (site, limit))
        return [dict(r) for r in reversed(rows)]

    def put_spec_alias(self, spec_hash: str, env_id: str) -> None:
        """A spec that reached an existing env through an adaptive path
        (soft-constraint relaxation): re-ensuring it must be a cache hit."""
        self._write(
            "INSERT OR IGNORE INTO spec_aliases(spec_hash, env_id, "
            "created_at) VALUES(?,?,?)",
            (spec_hash, env_id, time.time()),
        )

    def get_spec(self, spec_hash: str) -> dict | None:
        r = self._row("SELECT body FROM specs WHERE spec_hash=?", (spec_hash,))
        return json.loads(r["body"]) if r else None

    def put_env(
        self, env_id: str, spec_hash: str, canonical: dict, native_lock: str,
        manifest: str, platforms: list[str], weakly_reproducible: bool = False,
    ) -> None:
        self._write(
            "INSERT OR IGNORE INTO envs(env_id, spec_hash, canonical,"
            " native_lock, manifest, platforms, weakly_reproducible,"
            " created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                env_id, spec_hash, _j(canonical), native_lock, manifest,
                _j(platforms), int(weakly_reproducible), time.time(),
            ),
        )

    def get_env(self, env_id: str) -> dict | None:
        r = self._row("SELECT * FROM envs WHERE env_id=?", (env_id,))
        if not r:
            return None
        return {
            "env_id": r["env_id"], "spec_hash": r["spec_hash"],
            "canonical": json.loads(r["canonical"]), "native_lock": r["native_lock"],
            "manifest": r["manifest"], "platforms": json.loads(r["platforms"]),
            "weakly_reproducible": bool(r["weakly_reproducible"]),
            "parent_env_id": r["parent_env_id"] if "parent_env_id" in r.keys()
            else None,
            "layerable": bool(r["layerable"]) if "layerable" in r.keys()
            and r["layerable"] is not None else False,
        }

    def set_env_parent(self, env_id: str, parent_env_id: str,
                       layerable: bool) -> None:
        self._write(
            "UPDATE envs SET parent_env_id=?, layerable=? WHERE env_id=?",
            (parent_env_id, int(layerable), env_id))

    def children_of_env(self, parent_env_id: str) -> list[str]:
        return [r["env_id"] for r in self._rows(
            "SELECT env_id FROM envs WHERE parent_env_id=?", (parent_env_id,))]

    def replace_env_lock(self, env_id: str, native_lock: str,
                         manifest: str) -> None:
        """Re-derive a stale/corrupt lock for an unchanged identity. Safe by
        construction: only called when a fresh solve produced the SAME
        EnvID, so the canonical form (and thus the identity) is identical."""
        self._write("UPDATE envs SET native_lock=?, manifest=? WHERE env_id=?",
                    (native_lock, manifest, env_id))

    def list_envs(self) -> list[dict]:
        return [{"env_id": r["env_id"], "spec_hash": r["spec_hash"],
                 "name": r["name"], "platforms": json.loads(r["platforms"])
                 if r["platforms"] else [], "created_at": r["created_at"]}
                for r in self._rows(
                    "SELECT e.env_id, e.spec_hash, e.platforms, e.created_at,"
                    " s.name FROM envs e LEFT JOIN specs s"
                    " ON e.spec_hash = s.spec_hash ORDER BY e.created_at DESC")]

    def env_for_spec(self, spec_hash: str) -> str | None:
        r = self._row(
            "SELECT env_id FROM envs WHERE spec_hash=? ORDER BY created_at DESC LIMIT 1",
            (spec_hash,),
        )
        if r:
            return r["env_id"]
        r = self._row("SELECT env_id FROM spec_aliases WHERE spec_hash=?",
                      (spec_hash,))
        return r["env_id"] if r else None

    # -- realizations ------------------------------------------------------

    def set_realization(
        self, env_id: str, site: str, strategy: str, location: str,
        state: str, log: str = "", read_only: bool = False,
    ) -> None:
        now = time.time()
        self._write(
            "INSERT INTO realizations(env_id, site, strategy, location, state,"
            " log, created_at, updated_at, read_only)"
            " VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(env_id, site) DO UPDATE SET strategy=?, location=?,"
            " state=?, log=?, updated_at=?, read_only=?",
            (env_id, site, strategy, location, state, log, now, now,
             int(read_only),
             strategy, location, state, log, now, int(read_only)),
        )

    def touch_realization(self, env_id: str, site: str,
                          nbytes: int | None = None) -> None:
        """LRU/quota metadata a host GC policy needs (footprint + recency)."""
        if nbytes is None:
            self._write(
                "UPDATE realizations SET last_used=? WHERE env_id=? AND site=?",
                (time.time(), env_id, site))
        else:
            self._write(
                "UPDATE realizations SET last_used=?, bytes=? "
                "WHERE env_id=? AND site=?",
                (time.time(), nbytes, env_id, site))

    def get_realization(self, env_id: str, site: str) -> dict | None:
        r = self._row(
            "SELECT * FROM realizations WHERE env_id=? AND site=?", (env_id, site)
        )
        return dict(r) if r else None

    def realizations_for(self, env_id: str) -> list[dict]:
        return [dict(r) for r in self._rows(
            "SELECT * FROM realizations WHERE env_id=?", (env_id,)
        )]

    def realizations_for_site(self, site: str) -> list[dict]:
        return [dict(r) for r in self._rows(
            "SELECT * FROM realizations WHERE site=?", (site,)
        )]

    # -- data locations ------------------------------------------------------

    def put_dataref(
        self, ref: str, kind: str, nbytes: int,
        chunks: list[str] | None = None, meta: dict | None = None,
    ) -> None:
        self._write(
            "INSERT OR IGNORE INTO datarefs(ref, kind, bytes, chunks, meta) "
            "VALUES(?,?,?,?,?)",
            (ref, kind, nbytes, _j(chunks) if chunks else None, _j(meta or {})),
        )

    def update_dataref_meta(self, ref: str, patch: dict) -> None:
        row = self.get_dataref(ref)
        if not row:
            return
        meta = {**(row.get("meta") or {}), **patch}
        self._write("UPDATE datarefs SET meta=? WHERE ref=?",
                    (_j(meta), ref))

    def get_dataref(self, ref: str) -> dict | None:
        r = self._row("SELECT * FROM datarefs WHERE ref=?", (ref,))
        if not r:
            return None
        return {
            "ref": r["ref"], "kind": r["kind"], "bytes": r["bytes"],
            "chunks": json.loads(r["chunks"]) if r["chunks"] else None,
            "meta": json.loads(r["meta"]),
        }

    def all_datarefs(self) -> list[dict]:
        return [{"ref": r["ref"], "meta": json.loads(r["meta"] or "{}")}
                for r in self._rows("SELECT ref, meta FROM datarefs")]

    def datarefs_with_meta(self, key: str, value: str) -> list[dict]:
        rows = self._rows("SELECT ref, meta FROM datarefs")
        out = []
        for r in rows:
            meta = json.loads(r["meta"] or "{}")
            if meta.get(key) == value:
                out.append({"ref": r["ref"], "meta": meta})
        return out

    def set_location(self, ref: str, site: str, path: str, present: bool = True) -> None:
        self._write(
            "INSERT INTO locations(ref, site, path, present, verified_at)"
            " VALUES(?,?,?,?,?) "
            "ON CONFLICT(ref, site) DO UPDATE SET path=?, present=?, verified_at=?",
            (ref, site, path, int(present), time.time(),
             path, int(present), time.time()),
        )

    def refs_present_at(self, site: str) -> set[str]:
        return {r["ref"] for r in self._rows(
            "SELECT ref FROM locations WHERE site=? AND present=1", (site,)
        )}

    def locations_of(self, ref: str) -> list[dict]:
        return [dict(r) for r in self._rows(
            "SELECT * FROM locations WHERE ref=? AND present=1", (ref,)
        )]

    def demote_location(self, ref: str, site: str) -> None:
        """Location proved wrong (purged scratch etc.) — plan recomputes."""
        self._write(
            "UPDATE locations SET present=0 WHERE ref=? AND site=?", (ref, site)
        )

    # -- sites ------------------------------------------------------------

    def put_site(self, name: str, kind: str, config: dict) -> None:
        self._write(
            "INSERT INTO sites(name, kind, config) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET kind=?, config=?",
            (name, kind, _j(config), kind, _j(config)),
        )

    def set_capabilities(self, name: str, caps: dict, health: str = "ok") -> None:
        self._write(
            "UPDATE sites SET capabilities=?, health=?, probed_at=? WHERE name=?",
            (_j(caps), health, time.time(), name),
        )

    def set_health(self, name: str, health: str) -> None:
        self._write("UPDATE sites SET health=? WHERE name=?", (health, name))

    def get_site(self, name: str) -> dict | None:
        r = self._row("SELECT * FROM sites WHERE name=?", (name,))
        if not r:
            return None
        return {
            "name": r["name"], "kind": r["kind"], "config": json.loads(r["config"]),
            "capabilities": json.loads(r["capabilities"]) if r["capabilities"] else None,
            "health": r["health"], "probed_at": r["probed_at"],
        }

    def list_sites(self) -> list[dict]:
        rows = self._rows("SELECT name FROM sites ORDER BY name")
        return [self.get_site(r["name"]) for r in rows]

    # -- jobs -------------------------------------------------------------

    def put_job(self, job_id: str, task_hash: str, task: dict, site: str,
                state: str, array_group: str | None = None,
                array_index: int | None = None) -> None:
        now = time.time()
        self._write(
            "INSERT INTO jobs(job_id, task_hash, task, site, state, created_at,"
            " updated_at, array_group, array_index) VALUES(?,?,?,?,?,?,?,?,?)",
            (job_id, task_hash, _j(task), site, state, now, now,
             array_group, array_index),
        )

    def update_job(
        self, job_id: str, *, state: str | None = None,
        sched_handle: str | None = None, error: dict | None = None,
        manifest: dict | None = None, queue_reason: str | None = None,
        task: dict | None = None, task_hash: str | None = None,
    ) -> None:
        sets, vals = ["updated_at=?"], [time.time()]
        if state is not None:
            sets.append("state=?"); vals.append(state)
        if task is not None:
            sets.append("task=?"); vals.append(_j(task))
        if task_hash is not None:
            sets.append("task_hash=?"); vals.append(task_hash)
        if queue_reason is not None:
            sets.append("queue_reason=?"); vals.append(queue_reason)
        if sched_handle is not None:
            sets.append("sched_handle=?"); vals.append(sched_handle)
        if error is not None:
            sets.append("error=?"); vals.append(_j(error))
        if manifest is not None:
            sets.append("manifest=?"); vals.append(_j(manifest))
        vals.append(job_id)
        self._write(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", tuple(vals))

    def get_job(self, job_id: str) -> dict | None:
        r = self._row("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        return self._job_row(r) if r else None

    def jobs_where(self, state: str | None = None, site: str | None = None,
                   limit: int | None = None, offset: int = 0) -> list[dict]:
        q, vals, conds = "SELECT * FROM jobs", [], []
        if state:
            conds.append("state=?"); vals.append(state)
        if site:
            conds.append("site=?"); vals.append(site)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at"
        if limit is not None:
            q += " LIMIT ? OFFSET ?"
            vals += [limit, offset]
        return [self._job_row(r) for r in self._rows(q, tuple(vals))]

    def nonterminal_jobs(self) -> list[dict]:
        return [self._job_row(r) for r in self._rows(
            "SELECT * FROM jobs WHERE state NOT IN ('DONE','FAILED','CANCELLED')"
        )]

    def latest_manifest_for_task(self, task_hash: str) -> dict | None:
        r = self._row(
            "SELECT manifest FROM jobs WHERE task_hash=? AND state='DONE' "
            "AND manifest IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
            (task_hash,),
        )
        return json.loads(r["manifest"]) if r else None

    @staticmethod
    def _job_row(r: sqlite3.Row) -> dict:
        keys = r.keys()
        task = json.loads(r["task"])
        return {
            "job_id": r["job_id"], "task_hash": r["task_hash"],
            "task": task, "label": task.get("label") or None,
            "site": r["site"], "state": r["state"],
            "sched_handle": r["sched_handle"],
            "error": json.loads(r["error"]) if r["error"] else None,
            "manifest": json.loads(r["manifest"]) if r["manifest"] else None,
            "created_at": r["created_at"], "updated_at": r["updated_at"],
            "array_group": r["array_group"] if "array_group" in keys else None,
            "array_index": r["array_index"] if "array_index" in keys else None,
            "queue_reason": r["queue_reason"] if "queue_reason" in keys else None,
            "superseded_by": r["superseded_by"]
            if "superseded_by" in keys else None,
        }

    def detach_from_group(self, job_id: str) -> None:
        """Superseded array element (retried): leaves the group's counts."""
        self._write("UPDATE jobs SET array_group=NULL WHERE job_id=?", (job_id,))

    def put_plan(self, scope: str, plan: dict) -> None:
        self._write(
            "INSERT INTO plans(scope, plan, created_at) VALUES(?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET plan=excluded.plan",
            (scope, _j(plan), time.time()))

    def get_plan(self, scope: str) -> dict | None:
        r = self._row("SELECT plan FROM plans WHERE scope=?", (scope,))
        return json.loads(r["plan"]) if r else None

    def mark_superseded(self, job_id: str, by_job_id: str) -> None:
        """Record which retry replaced this row, so consumers can fold it
        under the group's history instead of showing a mystery duplicate."""
        self._write("UPDATE jobs SET superseded_by=? WHERE job_id=?",
                    (by_job_id, job_id))

    # -- array groups ---------------------------------------------------------

    def jobs_in_group(self, group: str, state: str | None = None,
                      offset: int = 0, limit: int | None = None) -> list[dict]:
        q, vals = "SELECT * FROM jobs WHERE array_group=?", [group]
        if state:
            q += " AND state=?"
            vals.append(state)
        q += " ORDER BY array_index"
        if limit is not None:
            q += " LIMIT ? OFFSET ?"
            vals += [limit, offset]
        return [self._job_row(r) for r in self._rows(q, tuple(vals))]

    def group_counts(self, group: str) -> dict:
        rows = self._rows(
            "SELECT state, COUNT(*) AS n FROM jobs WHERE array_group=? "
            "GROUP BY state", (group,)
        )
        counts = {r["state"]: r["n"] for r in rows}
        return {
            "total": sum(counts.values()),
            "done": counts.get("DONE", 0),
            "failed": counts.get("FAILED", 0),
            "cancelled": counts.get("CANCELLED", 0),
            "running": counts.get("RUNNING", 0) + counts.get("COLLECTING", 0),
            "queued": counts.get("QUEUED", 0),
            "preparing": counts.get("PENDING", 0) + counts.get("RESOLVING_ENV", 0)
                         + counts.get("STAGING", 0),
        }

    def failed_in_group(self, group: str, limit: int = 3) -> list[dict]:
        rows = self._rows(
            "SELECT job_id, array_index, error FROM jobs "
            "WHERE array_group=? AND state='FAILED' ORDER BY array_index LIMIT ?",
            (group, limit),
        )
        out = []
        for r in rows:
            err = json.loads(r["error"]) if r["error"] else {}
            out.append({
                "job_id": r["job_id"], "index": r["array_index"],
                "code": err.get("error"),
                "signature": (err.get("hints", {}).get("log_signature") or {}
                              ).get("signature"),
                "detail": (err.get("detail") or "")[:200],
            })
        return out

    # -- sessions -----------------------------------------------------------

    def put_run_inventory(self, target: str, site: str, entries: list[dict],
                          truncated: bool, total: int) -> None:
        self._write(
            "INSERT OR REPLACE INTO run_inventories(target, site,"
            " recorded_at, entries, truncated, total) VALUES(?,?,?,?,?,?)",
            (target, site, time.time(), json.dumps(entries),
             int(truncated), total),
        )

    def get_run_inventory(self, target: str) -> dict | None:
        r = self._row("SELECT * FROM run_inventories WHERE target=?",
                      (target,))
        if not r:
            return None
        out = dict(r)
        out["entries"] = json.loads(out["entries"] or "[]")
        out["truncated"] = bool(out["truncated"])
        return out

    def put_kernel_block(self, kernel_id: str, block: int,
                         code: str | None = None, rc: int | None = None,
                         out: str | None = None,
                         err: str | None = None) -> None:
        self._write(
            "INSERT INTO kernel_blocks(kernel_id, block, code, rc, out,"
            " err, recorded_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(kernel_id, block) DO UPDATE SET"
            " code=COALESCE(excluded.code, code),"
            " rc=COALESCE(excluded.rc, rc),"
            " out=COALESCE(excluded.out, out),"
            " err=COALESCE(excluded.err, err), recorded_at=excluded.recorded_at",
            (kernel_id, block, code, rc, out, err, time.time()),
        )

    def kernel_blocks(self, kernel_id: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM kernel_blocks WHERE kernel_id=? ORDER BY block",
            (kernel_id,))]

    def put_retained(self, target: str, site: str, label: str | None,
                     location: str, in_place: bool, files: int,
                     nbytes: int, state: str,
                     selection: dict | None = None,
                     moved: bool = True) -> None:
        self._write(
            "INSERT OR REPLACE INTO retained_runs(target, site, label,"
            " location, in_place, files, bytes, method, state, error,"
            " retained_at, selection, moved)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (target, site, label, location, int(in_place), files, nbytes,
             None, state, None, time.time(),
             json.dumps(selection) if selection else None, int(moved)),
        )

    def update_retained(self, target: str, *, state: str | None = None,
                        method: str | None = None,
                        error: str | None = None,
                        files: int | None = None,
                        nbytes: int | None = None) -> None:
        sets, vals = [], []
        for col, v in (("state", state), ("method", method),
                       ("error", error), ("files", files),
                       ("bytes", nbytes)):
            if v is not None:
                sets.append(f"{col}=?"); vals.append(v)
        if sets:
            vals.append(target)
            self._write(f"UPDATE retained_runs SET {', '.join(sets)} "
                        "WHERE target=?", tuple(vals))

    def get_retained(self, target: str) -> dict | None:
        r = self._row("SELECT * FROM retained_runs WHERE target=?",
                      (target,))
        return dict(r) if r else None

    def retained_where(self, label: str | None = None,
                       site: str | None = None,
                       state: str | None = None) -> list[dict]:
        q, vals = "SELECT * FROM retained_runs", []
        conds = []
        for col, v in (("label", label), ("site", site), ("state", state)):
            if v is not None:
                conds.append(f"{col}=?"); vals.append(v)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        return [dict(r) for r in self._conn.execute(
            q + " ORDER BY retained_at", vals)]

    def delete_retained(self, target: str) -> None:
        self._write("DELETE FROM retained_runs WHERE target=?", (target,))

    def put_session(self, session_id: str, base_env_id: str, site: str,
                    location: str, materialized: bool = True) -> None:
        now = time.time()
        self._write(
            # column-explicit: migrations add columns, and a positional
            # INSERT would break the moment one lands
            "INSERT INTO sessions(session_id, base_env_id, site, location,"
            " added_conda, added_pypi, state, created_at, last_used,"
            " materialized)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (session_id, base_env_id, site, location, "[]", "[]",
             "active", now, now, 1 if materialized else 0),
        )

    def set_session_materialized(self, session_id: str) -> None:
        self._write("UPDATE sessions SET materialized=1 WHERE session_id=?",
                    (session_id,))

    def get_session(self, session_id: str) -> dict | None:
        r = self._row("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        if not r:
            return None
        keys = r.keys()
        return {
            "session_id": r["session_id"], "base_env_id": r["base_env_id"],
            "site": r["site"], "location": r["location"],
            "added_conda": json.loads(r["added_conda"]),
            "added_pypi": json.loads(r["added_pypi"]),
            "installers": json.loads(r["installers"]) if "installers" in keys
            and r["installers"] else [],
            "state": r["state"],
            "created_at": r["created_at"],
            # pre-migration rows have no last_used: creation is the only
            # activity fact on record — never invent a fresher one
            "last_used": (r["last_used"] if "last_used" in keys
                          and r["last_used"] else r["created_at"]),
            # NULL = pre-lazy row = was cloned eagerly at start
            "materialized": bool(r["materialized"]
                                 if "materialized" in keys
                                 and r["materialized"] is not None else 1),
        }

    def touch_session(self, session_id: str) -> None:
        self._write("UPDATE sessions SET last_used=? WHERE session_id=?",
                    (time.time(), session_id))

    def list_sessions(self, site: str | None = None) -> list[dict]:
        q, vals = "SELECT session_id FROM sessions", ()
        if site:
            q += " WHERE site=?"; vals = (site,)
        return [self.get_session(r["session_id"]) for r in self._rows(q, vals)]

    def session_add_installer(self, session_id: str, cmd: str,
                              note: str = "", input: dict | None = None) -> None:
        s = self.get_session(session_id)
        entry = {"cmd": cmd, "note": note}
        if input:
            entry["input"] = input   # content-addressed source: portability
        self._write(
            "UPDATE sessions SET installers=? WHERE session_id=?",
            (_j(s["installers"] + [entry]), session_id))

    def session_add_deps(self, session_id: str, conda: list[str], pypi: list[str]) -> None:
        s = self.get_session(session_id)
        self._write(
            "UPDATE sessions SET added_conda=?, added_pypi=? WHERE session_id=?",
            (_j(s["added_conda"] + conda), _j(s["added_pypi"] + pypi), session_id),
        )

    def set_session_state(self, session_id: str, state: str) -> None:
        self._write(
            "UPDATE sessions SET state=? WHERE session_id=?", (state, session_id)
        )

    # -- metrics (measured reality feeding placement/estimates over time) ------

    def add_metric(self, site: str, key: str, value: float) -> None:
        self._write("INSERT INTO metrics(ts, site, key, value) "
                    "VALUES(?,?,?,?)",
                    (time.time(), site, key, float(value)))

    def metric_summary(self, site: str, key: str, days: float = 30) -> dict:
        rows = self._rows(
            "SELECT value FROM metrics WHERE site=? AND key=? AND ts>? "
            "ORDER BY value", (site, key, time.time() - days * 86400))
        vals = [r["value"] for r in rows]
        if not vals:
            return {"n": 0}
        return {"n": len(vals), "median": vals[len(vals) // 2],
                "p90": vals[int(len(vals) * 0.9)], "min": vals[0],
                "max": vals[-1]}

    # -- events retention -------------------------------------------------------

    def events_count(self) -> int:
        return self._row("SELECT COUNT(*) AS n FROM events")["n"]

    def prune_events(self, older_than_days: float = 30,
                     keep_kinds: tuple = ("array.done", "job.failed",
                                          "budget.watchdog")) -> int:
        cutoff = time.time() - older_than_days * 86400
        placeholders = ",".join("?" * len(keep_kinds))
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"DELETE FROM events WHERE ts < ? AND kind NOT IN ({placeholders})",
                (cutoff, *keep_kinds))
            return cur.rowcount

    # -- services --------------------------------------------------------------

    def put_service(self, service_id: str, site: str, jobdir: str,
                    handle: str, ports: list[int], task: dict) -> None:
        self._write(
            "INSERT INTO services(service_id, site, jobdir, handle, ports,"
            " state, task, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (service_id, site, jobdir, handle, _j(ports), "starting",
             _j(task), time.time()))

    def get_service(self, service_id: str) -> dict | None:
        r = self._row("SELECT * FROM services WHERE service_id=?",
                      (service_id,))
        if not r:
            return None
        d = dict(r)
        d["ports"] = json.loads(d["ports"])
        d["task"] = json.loads(d["task"])
        return d

    def update_service(self, service_id: str, *, state: str) -> None:
        self._write("UPDATE services SET state=? WHERE service_id=?",
                    (state, service_id))

    def list_services(self, state: str | None = None) -> list[dict]:
        q, vals = "SELECT service_id FROM services", ()
        if state:
            q += " WHERE state=?"; vals = (state,)
        return [self.get_service(r["service_id"])
                for r in self._rows(q + " ORDER BY created_at", vals)]

    # -- kernels ---------------------------------------------------------------

    def put_kernel(self, kernel_id: str, site: str, lang: str,
                   env_id: str | None, jobdir: str, handle: str,
                   label: str = "", session_id: str | None = None,
                   capture: str = "transcript") -> None:
        now = time.time()
        self._write(
            "INSERT INTO kernels(kernel_id, site, lang, env_id, jobdir,"
            " handle, state, blocks_run, created_at, last_used, label,"
            " session_id, capture)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kernel_id, site, lang, env_id, jobdir, handle, "running",
             0, now, now, label or None, session_id, capture),
        )

    def get_kernel(self, kernel_id: str) -> dict | None:
        r = self._row("SELECT * FROM kernels WHERE kernel_id=?", (kernel_id,))
        return dict(r) if r else None

    def update_kernel(self, kernel_id: str, *, state: str | None = None,
                      handle: str | None = None,
                      blocks_run: int | None = None) -> None:
        sets, vals = ["last_used=?"], [time.time()]
        if state is not None:
            sets.append("state=?"); vals.append(state)
        if handle is not None:
            sets.append("handle=?"); vals.append(handle)
        if blocks_run is not None:
            sets.append("blocks_run=?"); vals.append(blocks_run)
        vals.append(kernel_id)
        self._write(f"UPDATE kernels SET {', '.join(sets)} WHERE kernel_id=?",
                    tuple(vals))

    def list_kernels(self, state: str | None = None) -> list[dict]:
        q, vals = "SELECT * FROM kernels", ()
        if state:
            q += " WHERE state=?"; vals = (state,)
        return [dict(r) for r in self._rows(q + " ORDER BY created_at", vals)]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
