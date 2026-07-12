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
