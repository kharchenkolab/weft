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

## Retention: keeping run outputs as plain files

Runs leave files; most are never wanted, some become precious LATE.
The retention tier (misc/retention.md) keeps chosen files as ORDINARY
BROWSABLE FILES — no refs, no hashes — with run-level provenance in a
`.weft-run.json` sidecar:

```python
w.run_inventory(job_or_kernel_id)      # what the run left (recorded at
                                       # terminal state; survives EVERYTHING)
w.run_inventory(target, live=True)     # a RUNNING run's sandbox as it is
                                       # NOW — same shape, {"live": true},
                                       # never persisted; the receipt is
                                       # still written at terminal state
w.run_retain(target, include=["figs/**"], exclude=["tmp/**"],
             label="proj-9")           # keep: free locally (reflink/link),
                                       # in place under a site's declared
                                       # retain.dir, else background
                                       # transfer home. On a LIVE run this
                                       # becomes a PIN (pinned-pending):
                                       # the decision is recorded now, the
                                       # EVENTUAL files are captured when
                                       # the run settles (stop/death/
                                       # completion). Completed blocks'
                                       # $WEFT_BLOCK_DIR dirs capture
                                       # immediately (protocol-immutable).
                                       # layout="label" nests the tree as
                                       # runs/<label>/<target>/ so keeps
                                       # mirror the host's run structure.
w.retained_runs(label="proj-9")        # what's kept, where — one query
w.run_discard(target)                  # sandbox GC now (policy
                                       # run_remains_days sweeps the rest)
w.run_forget(label="proj-9")           # reclaim retained bytes; the
                                       # inventory (knowledge) survives
```

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
