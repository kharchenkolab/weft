# Fabric — Document 04 — Data Plane

## 1. Principles

Three principles govern data handling. *Content addressing everywhere*: every file or tree Fabric touches gets a DataRef (`dref:<sha256>`), so "is this already there?" is always answerable and nothing is transferred twice. *Move data to compute, results to the record*: inputs flow toward sites; what flows back by default is the result *manifest* plus small previews, with bulk outputs pulled lazily or on request. *The control plane is not the data plane*: bulk bytes travel over whatever channel is best for the pair of endpoints (rsync, rclone, object stores, Globus), independent of the SSH control channel.

## 2. DataRefs and the workspace

Files in the project workspace are hashed on first use (with an mtime+size fast path to avoid rehashing unchanged files) and registered in the local CAS by hardlink — the workspace stays a normal directory tree the user can browse, while the CAS provides stable identities. Directory trees hash as a canonical manifest of `(path, mode, file-hash)` entries, so a tree DataRef changes iff its contents change. Large instrument files are chunk-hashed (fixed 64 MiB chunks, Merkle root as the DataRef) — this enables resumable transfers and, later, delta transfer of files that grow append-only, a common pattern for run data.

DataRefs carry no semantics — no schema, no domain typing. A thin metadata sidecar (`origin`, free-form labels, source URL if fetched) supports UI display and provenance, nothing more. Fabric is not a data catalog (Document 00 §3); it is a courier with a perfect memory.

## 3. Location tracking and staging plans

The state store maintains a location table: `DataRef × Site → {present, verified_at, path}`. A task's staging plan is then a set difference — required refs minus refs present at the target — compiled into transfer operations grouped by method and batched (one rsync invocation for many small files, parallel streams for chunked large files). Verification is by hash at the destination (shim `hash-tree`), after which the location is registered. Locations are hints, not truths: a failed verification (scratch purged by site policy, for example — scratch filesystems on shared clusters are routinely auto-cleaned) silently demotes the entry and the plan recomputes, so site-side deletions cost a re-transfer, never a wrong result.

Results flow the same way in reverse: the epilogue hashes declared outputs into DataRefs *at the site*, registers them as present there, and only then does policy decide what to physically move. An output that will feed the next cluster job never needs to leave the cluster: task N+1's staging plan finds it already present. This site-side chaining is the single biggest bandwidth saver for iterative analysis and falls out of the model for free.

## 4. Transfer methods

`TransferMethod` is the third plugin seam. v1 ships: `local-link` (hardlink/copy within a machine), `rsync-ssh` (the workhorse for laptop↔workstation↔login-node; resumable, delta-capable, rides existing SSH config), and `rclone` (object stores for cloud sites; also the backing for an optional *relay bucket* when two sites can't see each other directly but both reach cloud storage). A `globus` method is planned for facility-scale transfers where endpoints exist — the right tool for multi-hundred-GB staging between institutional storage systems — and slots in without model changes since methods only implement `estimate(refs, src, dst)` and `transfer(refs, src, dst) → verified locations`.

Routing picks the cheapest method that connects the endpoints, preferring direct paths and falling back to relaying through the user's machine only as a last resort (correct but slow). Estimates (bytes to move, expected duration from measured per-pair throughput) are surfaced *before* execution for anything above a threshold, so the agent can tell the user "this will move 40 GB to the cluster, roughly 12 minutes" — an interaction pattern that builds trust and catches mistakes (wrong dataset, wrong site) before the bytes fly.

Transfers are resumable (rsync natively; chunked refs by chunk bookkeeping), rate-limitable per site (politeness on shared login nodes), and deduplicated in flight — one transfer per `(DataRef, site)` no matter how many tasks await it.

## 5. Result manifests and previews

Every job produces `manifest.json` in its sandbox, written by the epilogue at the site:

```json
{
  "job_id": "jb_01H…", "task_hash": "task:77ae…",
  "env_id": "env:9f3a…", "site": "hpc-univ",
  "exit_code": 0, "wall_s": 5170.2, "max_rss_gb": 41.3,
  "outputs": [
    {"path": "results/scan.h5", "ref": "dref:e07b…", "bytes": 1873741824,
      "preview": {"kind": "hdf5-tree", "detail": "…groups, dataset shapes/dtypes…"}},
    {"path": "results/best_fit.json", "ref": "dref:1c22…", "bytes": 2210,
      "preview": {"kind": "inline-json"}},
    {"path": "results/scan_corner.png", "ref": "dref:8d0f…",
      "preview": {"kind": "thumbnail", "png_b64": "…"}}
  ],
  "logs": {"tail": "…last 100 lines…", "full_ref": "dref:aa31…"}
}
```

Previews are generated site-side by a small preview library in the shim environment, by file kind: head/stats for tables (CSV/Parquet), group/dataset structure for HDF5, downscaled thumbnails for images and plots, first/last lines plus grep-able error extraction for logs, JSON inlined below a size cap. The point is token economy and latency: the agent reasons over kilobytes to decide what, if anything, to `fetch` in full. Manifests themselves are always pulled back immediately and stored in the workspace; they are the durable record of what happened.

## 6. Provenance

Provenance is a by-product, not a subsystem: the workspace accumulates an append-only chain of `(task spec, EnvID, input DataRefs) → (output DataRefs, manifest)` records. From any output file the user can walk back to the exact command, the exact locked environment, and the exact input hashes — sufficient to regenerate a figure months later or to attach a methods appendix to a paper. Export is a plain JSON/Markdown rendering of the chain for a selected set of outputs. Site-module dependencies appear as attested-but-not-hashed, per Document 03 §3, keeping the record honest about the one link Fabric cannot pin.

The `task_hash` in the manifest doubles as the memoization key (Document 01 §2): a resubmission with identical hash returns the recorded manifest unless the caller passes `force`, which both saves compute and nudges the overall system toward functional, cache-friendly task design.

## 7. Caches, quotas, and cleanup

Each site's CAS lives under `fabric_root/cas` on the storage the probe selected (scratch, not home). The data manager tracks per-site cache footprint against quota headroom from the (opportunistically refreshed) probe record and raises pressure events at thresholds; eviction is LRU over unpinned refs with user/agent confirmation, mirroring the environment GC policy — nothing on a shared system is deleted implicitly. Pinning is per-project: refs reachable from a project's provenance chain are pin-protected by default on the *local* CAS (the record must survive), but only advisory on remote caches (they are reconstructible by re-staging).

One deliberate simplification: Fabric does not attempt cross-user or cross-project sharing of remote caches in v1. The footprint is per-user; deduplication within one user's projects already captures most of the win, and shared caches on multi-user systems raise ownership and cleanup questions that are not worth solving before the core proves out.
