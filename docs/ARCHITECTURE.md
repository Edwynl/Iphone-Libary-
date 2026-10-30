# Windows AI Photos — Architecture v0.1

## Goals

- Developer-ready photo browser and content-based semantic search on Windows.
- Index at least 50,000 photo paths without copying originals.
- First-run experience before any AI installation: local folder scan, preview,
  file name and path search, progress, controlled model download button.
- Later: native signed installer without Python installed separately.

## Components

```
WinUI 3 / C# (src/WindowsAIPhotos)
  | JSONL messages (persistent local child process via stdin/stdout)
  v
Python Worker (engine/ai_photos/worker.py)
  +-- PhotoCatalog: SQLite WAL, incremental scanning + thumbnail cache
  +-- Siglip2Encoder: optional local HuggingFace model
        | image and text embeddings (normalized float32)
        v
     SQLite embeddings table -> NumPy dot-product top-K
```

Indexing reads photo bytes locally using Pillow. The HTTP receiver is off by
default; explicit `start_mobile` binds loopback only. Remote access requires
private Tailscale Serve HTTPS, never public port forwarding or Funnel.
`start_mobile` now launches an independent opt-in receiver. Closing WinUI stops
the stdio worker; the receiver remains active until explicitly stopped or the
login session ends. Reopening the app attaches to it. Local management uses a
DPAPI-authenticated, remote-rejecting Windows named pipe and an OS singleton
lease per data directory. No automatic startup service is installed. See
[native sync lifecycle](PHASE2_NATIVE_SYNC.md) for controls and limits.
An image might be decoded twice on its first scan (thumbnail and embedding).
Semantic vectors are ~768-1024 floats depending on the actual model config;
size is validated on read. Exact dimensionality must be verified at runtime.

## JSONL protocol

Client sends one JSON object per line to worker stdin:

```json
{"id":1,"action":"add_folder","params":{"path":"D:\\Pictures"}}
{"id":2,"action":"scan","params":{}}
{"id":3,"action":"search","params":{"query":"snowy mountain","limit":120}}
```

Worker replies:

```json
{"id":1,"ok":true,"data":{"path":"D:\\Pictures","added":true}}
{"event":"progress","data":{"stage":"indexing","seen":25,"updated":25,"embedded":0}}
{"id":2,"ok":true,"data":{"seen":25,"updated":25,"embedded":0,"removed":0,"errors":0,"elapsed_sec":1.23}}
```

Actions: `ping`, `status`, `folders`, `add_folder`, `browse`, `search`, `scan`,
`install_model`. Long-running scan and install are worker-thread jobs; UI
receives async progress events and final responses. One mutating long-running
operation is allowed at a time; a separate bounded semantic-search slot leaves
status and management responsive. Bridge waits are bounded and accept cancellation.

## Design invariants

- Source photos are never written or renamed.
- Model is never downloaded unless user clicks Install AI or explicitly uses CLI.
- Offline/unavailable source root does not trigger catalog deletion.
- Nested folder roots are refused to avoid duplicate ownership.
- Embeddings of changed images are invalidated before recomputation.
- Image vector and text vector must use the same model ID and dimension.
- OCR/EXIF location/face grouping are NOT accidentally inferred or exposed.

## Constraints / next steps

- Search currently loads all embeddings for the chosen model into memory and
  computes exact cosine ranks. At 50k images this is reasonable for development,
  but cache+ANN index should be benchmarked before release.
- Python + PyTorch must be packaged or replaced with verified ONNX/Windows ML
  image and text encoders + tokenizer to produce an independent Windows installer.
- Model revision is pinned in config.py. Installation writes a SHA-256 artifact
  manifest and verifies it before use. Python 3.11 dependency locks include hashes;
  real model runtime/quality and complete redistribution notices remain release gates.
- Heic/raw decoding, large-file corruption, image cancellation, zero-copy
  thumbnails and multicore batching are follow-up tasks.
- GUI is not yet integrated with Windows-native folder watcher, taskbar progress,
  per-folder permission prompts, rename detection or update distribution.

## Resource backup and recovery after review fixes

- `photos` remains the browse/index table. Completed native resources are also
  included in both backup plans, including MOV/MP4/DNG. JSON manifests
  retains original names, per-device asset/resource associations, capture dates,
  SHA-256, sizes and backup paths. One physical file may have several logical refs.
- Missing, stale, unsafe or corrupt entries make `complete=false`; CLI returns 2.
  The desktop UI requires reconciliation instead of showing unconditional success.
- Local output uses validated, pinned non-reparse directories; Windows leases
  prevent directory replacement and keep the leaf nonempty during writes.
- Local v2 backups publish immutable `objects/<sha-prefix>/<sha><extension>`
  files with `replace=False`, then verify all references and publish an immutable
  `snapshots/<id>.json`. Only a verified full run replaces the root
  `backup-manifest.json` last-successful selection. The root is a complete manifest
  copy, so the default restore interface stays usable. If publication reports an
  error after atomic rename, `latest_published` reflects visibility of the new
  complete manifest; the result still reports incomplete/uncertain durability.
- A manifest separates `backup_path` (content object) from `restore_path`
  (original root/name). Repeated names and native asset associations survive
  content sharing. Old v1 manifests are archived before successful migration;
  their flat files are never overwritten. No history retention deletion or orphan
  object GC is automatic. This is not a globally atomic filesystem snapshot of
  changing sources or a guarantee against physical power-loss/media failure.
- `restore-local BACKUP EMPTY_TARGET` verifies hashes and paths; `--apply` copies
  into an independent empty target without overwriting existing files, and emits
  a restored resource manifest. It does not recreate an Apple Photos library.
- `backup-history BACKUP` lists complete snapshot metadata; `restore-local ...
  --snapshot ID` selects history. Restore preflights every referenced checksum,
  size and safe output path before copying. Existing v1 snapshots are supported.
- S3 uses explicit SHA256 checksum negotiation and a resource manifest. No live
  provider upload/download or full cloud restore has been validated; bucket
  versioning, recovery drills and optional client-side encryption remain gates.

## Native iPhone verification boundary

The repaired Swift sources have not been compiled by Xcode or run on an iPhone.
They include alternatePhoto resources, receiver-scoped verified checkpoints,
per-resource failure records, cancellation-aware PhotoKit streaming and URLSession
waiters, and cancellation of queued automatic work when disabled. Strict Wi-Fi
mode reads local originals only: PhotoKit offers no per-resource cellular switch,
so iCloud-only originals must first be downloaded in Photos or the restriction
explicitly disabled. This limit is shown in the app. A new simulator CI job is
configured but has not been dispatched during local review/fixing.
