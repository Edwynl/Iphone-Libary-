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
