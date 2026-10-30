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

All photo bytes are read locally using Pillow. No HTTP server is started.
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
receives async progress events and final responses. Only one long-running
operation is allowed at a time. Progress and errors should be presented clearly.

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
- Model revision is not pinned to an immutable HF commit yet; production must
  pin, hash-check and ship corresponding attribution/licensing notices.
- Heic/raw decoding, large-file corruption, image cancellation, zero-copy
  thumbnails and multicore batching are follow-up tasks.
- GUI is not yet integrated with Windows-native folder watcher, taskbar progress,
  per-folder permission prompts, rename detection or update distribution.
