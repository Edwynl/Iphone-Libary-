# Changelog

## 0.1.0 — initial developer MVP

- Native WinUI 3 photo browser, file picker, paging and local JSONL worker.
- Incremental SQLite scan, thumbnail generation, offline-drive preservation,
  and file name/path search.
- Optional on-device SigLIP 2 image and text embedding pipeline; never auto-downloads
  models or uploads photographs.
- Folder removal (catalog only), with explicit Windows UI confirmation.
- Cache traversal protection when the user picks a parent directory.
- Python unit/integration tests (16 passing locally).

**Not done:** Windows compile verification, model smoke test, signed installer,
5k/50k file benchmark, RAW/HEIC decoding, face recognition and OCR.

## Local review fixes — 2026-10-30 (uncommitted)

- Guard backup output against Windows junction/reparse traversal and path swaps.
- Repair WinUI build and package mobile_web with the Python worker.
- Verify completed resources, recover corrupt imports without overwriting them,
  retain all native resource types and metadata in backup manifests, and add a
  verified local restore command into empty independent storage.
- Shorten catalog write transactions, repair similarity candidate recall and
  failed thumbnail retries, keep semantic inference off the worker control loop.
- Add receiver-bound iPhone reconciliation, alternatePhoto resources, cancellation
  propagation, per-resource failure records, endpoint binding and explicit network
  limits. These Swift changes still require Xcode and physical iPhone validation.
- Pin model revision/artifact hashes and Python dependency locks; add output smoke
  and unsigned iOS simulator build configuration. No CI dispatch or release.
