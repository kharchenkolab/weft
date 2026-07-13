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
