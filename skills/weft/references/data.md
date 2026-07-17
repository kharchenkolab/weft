# Data

Everything is content-addressed: `data_register(path)` → `dref:<sha256>`
(files, whole directory trees, or Merkle-chunked files ≥64 MiB — chunked
files also carry a plain content hash for wire verification). The location
table knows which sites hold what; staging is the set difference.

- **Plans are honest:** `task_submit(...)["plan"]["staging"]` gives
  `bytes_to_move`, `transfer_method`, `estimate_s` *before* bytes move —
  relay big ones to the user.
- **Progress:** watch `transfer.start` / `transfer.progress` (bytes, rate,
  ETA; ≥1s apart) / `transfer.done` events.
- **Chaining:** outputs are ingested into the *site* CAS at collection; a
  downstream task mounting an output ref stages **0 bytes**. Chains of
  tasks on one site never round-trip data through the workspace.
- **Memoization:** identical task (env+inputs+code+command+outputs) →
  recorded manifest returned instantly; `force=True` to actually re-run.
- **Fetch selectively:** manifests carry previews (JSON inlined, table
  heads, image thumbs, log tails). `data_fetch(ref, "local/path")` only
  when the full artifact is needed; it verifies content on arrival.
- **Purged scratch:** a site deleting cached data costs a re-transfer,
  never a wrong result — a task hitting it fails `data.verify_failed`
  (retryable, locations demoted); resubmit and staging re-transfers.
- **Transfer methods** are chosen per site: `rsync-ssh` normally,
  `ssh-pipe` (tar over the control channel) on boxes without rsync,
  `local-link` on the same machine. You don't pick; the endpoint does.
- **Inputs are read-only by contract — zero-copy makes it matter.** On
  one filesystem, registration and local staging are hardlinks (instant,
  no duplication, any file size): the sandbox input, the CAS blob, and
  possibly the user's original file are the SAME inode. A task that
  mutates an input in place doesn't just damage one file — it falsifies
  the content-addressed record (the blob no longer hashes to its name;
  memoization/provenance/staging then carry wrong bytes under a
  true-looking name). Write results to declared outputs only; if a tool
  insists on mutating its input, copy it inside the job sandbox first
  (`cp data/in.h5 work.h5 && tool work.h5`).

## Retention: retain marks; storage moves only when storage demands it

Runs leave files; most are never wanted, some become precious LATE.
A KEEP is a pinned selection at a durable address (misc/retention2.md)
— ordinary browsable files, run-level provenance in a `.weft-run.json`
sidecar. WHERE keeps live follows from the site's declared storage:

- `durable: true` on the site → **mark in place**: zero bytes move,
  the sandbox paths stay THE paths forever, the sandbox becomes
  sweep-exempt (`moved: false` in the result).
- `durable: "/abs/path"` → one site-side hop to
  `<path>/runs/<label>/<target>/` — never crosses the wire.
- neither → the site can keep nothing; say where per call
  (`dest="@workspace"` ships home, background + transfer events) or
  the call refuses with `retain.no_durable` naming all three levers.

```python
w.run_inventory(job_or_kernel_id)      # what the run left (terminal
                                       # receipt; survives EVERYTHING;
                                       # scaffold: true flags weft's files)
w.run_inventory(target, live=True)     # the sandbox NOW; never persisted
w.run_retain(target, include=["figs/**"], label="proj-9")
                                       # NO include = all MY files
                                       # (scaffold stays out). LIVE run →
                                       # PIN: decision now, settled at
                                       # stop/death/completion.
w.retained_runs(label="proj-9")        # the catalog: what's kept, WHERE
w.run_file_stat(t, rel); w.run_file_read(t, rel)
                                       # the (run, relpath) KEY — resolves
                                       # sandbox → keep, answers at=...
w.run_forget(target=...)               # the INVERSE of retain: removes
                                       # what retain created — the pin
                                       # always; copies only if a move
                                       # made them. Unmarking deletes
                                       # NOTHING.
w.run_discard(target)                  # byte destruction (sandboxes
                                       # only). On a still-marked target:
                                       # SELECTIVE — junk goes, keeps stay.
                                       # Full deletion = forget + discard.
```

- TTL (`policy: {run_remains_days: N}`) is OPT-IN, default off — no
  silent loss. Retained targets are exempt regardless.
- Chaining on a kept result: task inputs take the key directly —
  `{"run": jb_1, "rel": "results/out.b", "mount_as": "out.b"}` —
  resolved to the output's ref (declared outputs: no rehash;
  undeclared: lazy identity + `run:` lineage). Same key on
  `data_register(run=, rel=)`.
- LINK: keeps of declared outputs anchor their refs (`meta.keep`) —
  after cache eviction, `data_fetch` and task staging RE-OBTAIN the
  bytes from the keep, hash-verified (`data.reobtained_from_keep`);
  a modified keep is refused (`data.verify_failed`, source "keep").

- Retain-vs-surface: retention is DURABILITY; rendering an output in a
  UI right now is a foreground fetch — different operation. The
  surface verbs: `run_file_stat(target, rel)` (on-disk vs swept —
  inventory says what EXISTED, stat says what remains) and
  `run_file_read(target, rel, max_bytes=)` (capped base64 preview from
  the sandbox, live or dead; hard cap 8 MB — a preview channel, not a
  transport). Fetching a big or in-place-retained file home is the
  blessed COMPOSITION: `data_register(path, site=...) →
  data_fetch(ref, dest)` — the register mints the lineage edge, so
  fetch-on-open and re-entry are one mechanism.
- Re-entry: a retained file feeding a NEW calculation gets identity
  lazily — `data_register(path)` carries `origin run:<target>/<path>`
  (provenance walks THROUGH it into the producing run);
  `data_register(path, site=...)` registers site-side (hardlink into
  the site CAS, original stays, same-site reuse stages 0 bytes).
  DIRECTORIES work as units: a retained `.zarr` registered site-side
  mints a tree ref (same convention as output collection) — usable as
  a task input, or `data_fetch(ref, dest)` rebuilds the directory at
  the workspace pulling only blobs it doesn't already hold (an evolved
  store re-registered moves just its new chunks).
- Kernels default to `capture="transcript"`: block code/rc/text mirror
  into the store as observed — text saves and `kernel_restart(replay)`
  survive scratch purges; `capture="none"` opts out.

## Ingesting remote sources

```python
w.data_register("https://example.org/run2189.h5")            # → workspace CAS
w.data_register("https://…/big.h5", site="hpc")              # straight into
                                                             # the site's CAS
w.data_register(url, expected_sha256="ab12…")                # verify a
                                                             # published checksum
```
`s3://`, `gs://`, `azure://` work when rclone sits next to pixi. Without an
expected hash, hash-on-arrival is the identity (`meta.trust =
"first-fetch"`). Ingest only — discovery and cataloging stay above weft.

## Reference-in-place (big data on stable storage)

When the data lives on a cluster's durable share and the site CAS sits
on scratch, ingesting would COPY it across filesystems. Instead:

```python
w.data_fingerprint(path, site)         # cheap stat manifest {path, bytes,
                                       # mtime}; hash_under=N samples small
                                       # files — drift detection, no identity
w.data_register(path, site=..., ingest=False)
                                       # hash site-side (one read, NO write):
                                       # the path itself is the ref's durable
                                       # home. Same-site tasks mount it as a
                                       # SYMLINK — zero bytes move
```

- Every staging re-checks a STAT-FENCE (file: size+mtime; tree: file
  count+bytes): a drifted home fails `data.verify_failed` with
  `hints.source = "external-home"` + recorded-vs-observed — identity is
  content, so a changed home means a DEAD ref; re-register for the new
  content. Content drift subtler than a stat change is caught by hash
  verification whenever bytes move.
- Bytes ingest LAZILY only when they must move (cross-site staging,
  `data_fetch`): fence, then ingest into the source site's CAS
  (`data.ingested_for_transfer` event), then the normal verified
  transfer. Forced ingest is never needed — it happens when physics
  demands it.
- Sharper teeth on read-only inputs: through a symlink, a task that
  writes its input damages the DURABLE HOME, not a re-obtainable cache
  copy.
- GC never lists external locations (weft holds no bytes there); the
  home's lifecycle belongs to its owner.

## Reproducibility bundles (the record that travels)

`bundle_export(job_id, "result.weft.tgz")` — one file holding a finished
result's full provenance closure: the task, every env identity (spec +
canonical lock + native lockfile, incl. captured installer sources),
every input blob (recursing through the jobs that produced them), and
the recorded output refs. `bundle_import(path)` loads it into ANY
workspace; re-running the returned task (force=True) must reproduce the
recorded output refs — that comparison is the proof of re-derivation.
Honest limits ride in `reproducibility`: an `escape-hatch` env re-executes
its captured installers; `attested` modules must exist at the destination;
`state-dependent` results replay transcripts, not derivations.

Hosts building their own record on top can ride a sealed envelope:
`bundle_export(..., metadata=<bytes or JSON>)` stores it verbatim and
`bundle_import` returns it under `metadata` — weft never parses it and
it never enters the bundle's identity or the re-derivation proof.
Context only (64 MB cap); data belongs in blobs. Re-exporting an
imported bundle does not carry an old envelope forward.

## Site-to-site routing (the controller carries bytes only as a last resort)

At registration weft probes byte routes between sites: a **shared
filesystem** (a nonce under src's root visible from dst — group NFS,
cluster scratch) or **direct ssh** (dst already reaches src with the
user's own keys; weft brokers no identity, it only discovers what your
config permits). `sites_describe` lists routes; `site_route_probe(src,
dst)` re-probes. Staging then moves bytes src→dst as a **link/copy**
(shared FS) or a **dst-side rsync pull** (direct), hash-verified at the
destination like every transfer — the controller stays out of the data
path. No route → the honest fallback: fetch home, ship out (two hops),
visible in the plan (`staging.site_to_site`) and events (`transfer.done
via=fs-link|direct-pull|controller-detour`). Sites behind NAT/port-maps
can set `peer_host`/`peer_port` — the address PEERS use, when it differs
from the controller's.
