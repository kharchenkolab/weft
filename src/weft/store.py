"""Durable state: one SQLite database + an in-process event bus (doc 01 §6).

Single-writer by design. WAL mode; a process-wide lock serializes writes so
background pollers and the API surface can share the store. Remote state is
the source of truth for running jobs — rows here are reconciled snapshots.
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
"""


def _j(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True)


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
        self._subscribers: list[Callable[[dict], None]] = []

    # -- events ---------------------------------------------------------

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._subscribers.append(fn)

    def emit(self, kind: str, job_id: str | None = None, **payload) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO events(ts, kind, job_id, payload) VALUES(?,?,?,?)",
                (time.time(), kind, job_id, _j(payload)),
            )
            seq = cur.lastrowid
        event = {"seq": seq, "kind": kind, "job_id": job_id, **payload}
        for fn in list(self._subscribers):
            try:
                fn(event)
            except Exception:
                pass  # subscribers must not break the emitter
        return seq

    def events_since(self, cursor: int, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (cursor, limit)
        ).fetchall()
        return [
            {
                "seq": r["seq"], "ts": r["ts"], "kind": r["kind"],
                "job_id": r["job_id"], **json.loads(r["payload"]),
            }
            for r in rows
        ]

    def audit_log(
        self, actor: str, action: str, *, site: str = "", command: str = "",
        why: str = "", result: str = "",
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit(ts, actor, action, site, command, why, result)"
                " VALUES(?,?,?,?,?,?,?)",
                (time.time(), actor, action, site, command, why, result[:4000]),
            )

    def audit_tail(self, n: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # -- specs / envs -----------------------------------------------------

    def put_spec(self, spec_hash: str, name: str, body: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO specs VALUES(?,?,?,?)",
                (spec_hash, name, _j(body), time.time()),
            )

    def get_spec(self, spec_hash: str) -> dict | None:
        r = self._conn.execute(
            "SELECT body FROM specs WHERE spec_hash=?", (spec_hash,)
        ).fetchone()
        return json.loads(r["body"]) if r else None

    def put_env(
        self, env_id: str, spec_hash: str, canonical: dict, native_lock: str,
        manifest: str, platforms: list[str], weakly_reproducible: bool = False,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO envs VALUES(?,?,?,?,?,?,?,?)",
                (
                    env_id, spec_hash, _j(canonical), native_lock, manifest,
                    _j(platforms), int(weakly_reproducible), time.time(),
                ),
            )

    def get_env(self, env_id: str) -> dict | None:
        r = self._conn.execute("SELECT * FROM envs WHERE env_id=?", (env_id,)).fetchone()
        if not r:
            return None
        return {
            "env_id": r["env_id"], "spec_hash": r["spec_hash"],
            "canonical": json.loads(r["canonical"]), "native_lock": r["native_lock"],
            "manifest": r["manifest"], "platforms": json.loads(r["platforms"]),
            "weakly_reproducible": bool(r["weakly_reproducible"]),
        }

    def env_for_spec(self, spec_hash: str) -> str | None:
        r = self._conn.execute(
            "SELECT env_id FROM envs WHERE spec_hash=? ORDER BY created_at DESC LIMIT 1",
            (spec_hash,),
        ).fetchone()
        return r["env_id"] if r else None

    # -- realizations ------------------------------------------------------

    def set_realization(
        self, env_id: str, site: str, strategy: str, location: str,
        state: str, log: str = "",
    ) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO realizations VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(env_id, site) DO UPDATE SET strategy=?, location=?,"
                " state=?, log=?, updated_at=?",
                (env_id, site, strategy, location, state, log, now, now,
                 strategy, location, state, log, now),
            )

    def get_realization(self, env_id: str, site: str) -> dict | None:
        r = self._conn.execute(
            "SELECT * FROM realizations WHERE env_id=? AND site=?", (env_id, site)
        ).fetchone()
        return dict(r) if r else None

    def realizations_for(self, env_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM realizations WHERE env_id=?", (env_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- data locations ------------------------------------------------------

    def put_dataref(
        self, ref: str, kind: str, nbytes: int,
        chunks: list[str] | None = None, meta: dict | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO datarefs VALUES(?,?,?,?,?)",
                (ref, kind, nbytes, _j(chunks) if chunks else None, _j(meta or {})),
            )

    def get_dataref(self, ref: str) -> dict | None:
        r = self._conn.execute("SELECT * FROM datarefs WHERE ref=?", (ref,)).fetchone()
        if not r:
            return None
        return {
            "ref": r["ref"], "kind": r["kind"], "bytes": r["bytes"],
            "chunks": json.loads(r["chunks"]) if r["chunks"] else None,
            "meta": json.loads(r["meta"]),
        }

    def set_location(self, ref: str, site: str, path: str, present: bool = True) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO locations VALUES(?,?,?,?,?) "
                "ON CONFLICT(ref, site) DO UPDATE SET path=?, present=?, verified_at=?",
                (ref, site, path, int(present), time.time(),
                 path, int(present), time.time()),
            )

    def refs_present_at(self, site: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT ref FROM locations WHERE site=? AND present=1", (site,)
        ).fetchall()
        return {r["ref"] for r in rows}

    def locations_of(self, ref: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM locations WHERE ref=? AND present=1", (ref,)
        ).fetchall()
        return [dict(r) for r in rows]

    def demote_location(self, ref: str, site: str) -> None:
        """Location proved wrong (purged scratch etc.) — plan recomputes."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE locations SET present=0 WHERE ref=? AND site=?", (ref, site)
            )

    # -- sites ------------------------------------------------------------

    def put_site(self, name: str, kind: str, config: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sites(name, kind, config) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET kind=?, config=?",
                (name, kind, _j(config), kind, _j(config)),
            )

    def set_capabilities(self, name: str, caps: dict, health: str = "ok") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sites SET capabilities=?, health=?, probed_at=? WHERE name=?",
                (_j(caps), health, time.time(), name),
            )

    def set_health(self, name: str, health: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE sites SET health=? WHERE name=?", (health, name))

    def get_site(self, name: str) -> dict | None:
        r = self._conn.execute("SELECT * FROM sites WHERE name=?", (name,)).fetchone()
        if not r:
            return None
        return {
            "name": r["name"], "kind": r["kind"], "config": json.loads(r["config"]),
            "capabilities": json.loads(r["capabilities"]) if r["capabilities"] else None,
            "health": r["health"], "probed_at": r["probed_at"],
        }

    def list_sites(self) -> list[dict]:
        rows = self._conn.execute("SELECT name FROM sites ORDER BY name").fetchall()
        return [self.get_site(r["name"]) for r in rows]

    # -- jobs -------------------------------------------------------------

    def put_job(self, job_id: str, task_hash: str, task: dict, site: str, state: str) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs(job_id, task_hash, task, site, state, created_at,"
                " updated_at) VALUES(?,?,?,?,?,?,?)",
                (job_id, task_hash, _j(task), site, state, now, now),
            )

    def update_job(
        self, job_id: str, *, state: str | None = None,
        sched_handle: str | None = None, error: dict | None = None,
        manifest: dict | None = None,
    ) -> None:
        sets, vals = ["updated_at=?"], [time.time()]
        if state is not None:
            sets.append("state=?"); vals.append(state)
        if sched_handle is not None:
            sets.append("sched_handle=?"); vals.append(sched_handle)
        if error is not None:
            sets.append("error=?"); vals.append(_j(error))
        if manifest is not None:
            sets.append("manifest=?"); vals.append(_j(manifest))
        vals.append(job_id)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", vals)

    def get_job(self, job_id: str) -> dict | None:
        r = self._conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._job_row(r) if r else None

    def jobs_where(self, state: str | None = None, site: str | None = None) -> list[dict]:
        q, vals = "SELECT * FROM jobs", []
        conds = []
        if state:
            conds.append("state=?"); vals.append(state)
        if site:
            conds.append("site=?"); vals.append(site)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at"
        return [self._job_row(r) for r in self._conn.execute(q, vals).fetchall()]

    def nonterminal_jobs(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE state NOT IN ('DONE','FAILED','CANCELLED')"
        ).fetchall()
        return [self._job_row(r) for r in rows]

    def latest_manifest_for_task(self, task_hash: str) -> dict | None:
        r = self._conn.execute(
            "SELECT manifest FROM jobs WHERE task_hash=? AND state='DONE' "
            "AND manifest IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
            (task_hash,),
        ).fetchone()
        return json.loads(r["manifest"]) if r else None

    @staticmethod
    def _job_row(r: sqlite3.Row) -> dict:
        return {
            "job_id": r["job_id"], "task_hash": r["task_hash"],
            "task": json.loads(r["task"]), "site": r["site"], "state": r["state"],
            "sched_handle": r["sched_handle"],
            "error": json.loads(r["error"]) if r["error"] else None,
            "manifest": json.loads(r["manifest"]) if r["manifest"] else None,
            "created_at": r["created_at"], "updated_at": r["updated_at"],
        }

    def close(self) -> None:
        self._conn.close()
