# Windows AI Photos — local-first photo search for Windows

**Status: Development MVP, not a signed installer or a commercial release.**

A Windows 11/10 desktop photo browser with an offline-first indexing engine.
Photo originals stay in their existing folders. Search by filename works without
AI; when the optional local SigLIP 2 model is explicitly installed, indexing and
text-to-image semantic search run on the user's PC. **No Ollama required.**

## Implemented in this repository

- WinUI 3 native window: grid of thumbnails, folder picker, search box, load more,
  double-click/open-original semantics, scan progress and explicit Install AI button.
- SQLite catalog with incremental scans, timestamp/size change tracking, missing
  file cleanup, offline-drive protection, EXIF orientation, 384px cached previews.
- Remove an indexed folder without moving or deleting any source photos. The app
  excludes its own cache even when a chosen photo root contains app data.
- Local semantic image search using Google's Apache 2.0 SigLIP 2 base model.
  Normalized embeddings are stored in SQLite; top-N search uses NumPy dot product.
- Clear filename-only fallback and status text while AI is not installed.
- Persistent JSON-lines worker process; optional local iPhone upload server is **off by default**, loopback-only; no public endpoint or cloud image inference.
- Automated tests and Windows build CI definition.

## Requirements

For *development*:
- Windows 10 2004+ / Windows 11, x64. ARM64 port is planned, not verified.
- Visual Studio with WinUI / Windows App SDK workloads and .NET 10 SDK.
- Python 3.11+ available on the machine (developer bridge only).
- Windows App SDK NuGet restore for the GUI; pip packages for the engine.
- Optional GPU for faster AI indexing, CPU mode supported.

For *future distribution*: package the Python worker (or replace it with a .NET
Windows ML runtime), validate performance, sign binaries and produce an installer.
**The current app is NOT yet a one-click end-user install.**

## Quick start (Windows PowerShell)

```powershell
.\scripts\setup-dev.ps1
$env:AI_PHOTOS_PYTHON = (Resolve-Path '.\.venv\Scripts\python.exe').Path
.\scripts\build-windows.ps1
# From an installed WinUI development environment:
dotnet run --project .\src\WindowsAIPhotos\WindowsAIPhotos.csproj -p:Platform=x64
```

For AI content search (large downloads and local model only after explicit opt-in):

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes -r .\engine\requirements-ai.lock.txt
# Then click "Install AI model" in the app (~1.5GB weight download)
# Click Scan again so the AI creates vectors for existing photos.
```

### Test the engine without compiling WinUI

```powershell
$env:PYTHONPATH = (Resolve-Path .\engine).Path
.\.venv\Scripts\python.exe -m ai_photos.cli add-folder 'D:\Pictures'
.\.venv\Scripts\python.exe -m ai_photos.cli scan
.\.venv\Scripts\python.exe -m ai_photos.cli search 'snow mountains'
.\.venv\Scripts\python.exe -m pytest -q .\tests
```

Without an installed AI model, `search` matches filenames/paths only. It **does not**
pretend semantic image search is active.

## Photo formats

JPG/JPEG, PNG, WebP, TIFF, BMP and GIF via Pillow. HEIC/HEIF are discovered but
need an image decoder in the Python environment for thumbnails and semantic
indexing; otherwise entries report an error and retain the source path. RAW,
face clustering, GPS map views, video indexing and OCR are on the roadmap.
Do not expect these features in this MVP.

## Data and privacy

- Database and previews live in `%LOCALAPPDATA%\WindowsAIPhotos` (or
  `AI_PHOTOS_HOME` for tests and custom locations).
- Original photos are read-only. Explicit import/backup operations create copies;
  they never delete or modify source originals. Backup outputs reject linked and
  Windows reparse paths. Only catalog records are pruned when a source disappears.
- The **Install AI** action uses an HTTPS model download from Hugging Face.
  This downloads weights and processing files, **not the user's photos**.
- The app does not contain telemetry or advertisements. The optional iPhone companion enables a localhost-only token-protected upload service only when explicitly started.
- Model weights, personal photos, databases and cache are excluded from git.

## Architecture, release readiness and engineering handoff

- [Architecture](docs/ARCHITECTURE.md)
- [Engineering acceptance criteria](docs/ACCEPTANCE.md)
- [Release and licensing checklist](docs/RELEASE.md)

## GitHub repository and naming

This project was started in the user-created repository
`Edwynl/Iphone-Libary-` on a dedicated `feat/windows-ai-photos-mvp` branch.
The intended repository display name is `Windows-AI-Photos`.

**Repository rename is an owner-level GitHub Settings operation.** Visit
`Settings > General > Repository name` to rename to `Windows-AI-Photos`.
An optional `scripts/rename-existing-repository.ps1` uses the authenticated
GitHub CLI to perform the same operation. GitHub normally redirects old URLs.

**Important:** the repository was created as **public** by its owner. Set it to
private under Settings > General > Danger Zone if you do not intend to share
source code before a commercial release. No personal photos or tokens belong
in git; model weights and local caches are excluded by `.gitignore`.

## Development accuracy

The Python catalog/bridge has automated tests. The initial WinUI XAML and C# files
must be compiled and smoke-tested on Windows; a Linux test result does **not**
constitute a validated Windows executable. No pretrained model quality, Chinese
retrieval recall, index throughput, or 50k-photo benchmark has been measured yet.

## iPhone uploads, duplicate reviews, and opt-in backup (MVP)

### iPhone access: local-first + Tailscale, without port-forwarding

A companion web page now offers **manual** upload and read-only recent-photo
browsing from iPhone Safari. This is not yet automatic PhotoKit/background sync.
The server binds to `127.0.0.1` only; it will not listen on a public interface.

1. On Windows, choose **iPhone sync** in the application or run
   `python -m ai_photos.cli serve-iphone --port 8787` with `PYTHONPATH=engine`.
2. On Windows with Tailscale installed, run `tailscale serve --bg 8787`.
3. On iPhone, join the **same tailnet** and open the private HTTPS address shown
   by `tailscale serve status`; enter the one-session pairing token shown on the PC.
4. Select photos in Safari and upload. New files go to
   `~/Pictures/Windows AI Photos Imports` on the Windows PC, **not** to the index
   database or hidden cache; their iPhone originals stay untouched.

The Windows PC must be running; Tailscale Serve must be explicitly configured.
The local HTTP port is protected by a strong bearer token, but **do not expose
port 8787 publicly**. Tailscale Serve adds private HTTPS access. Background
camera-roll sync, Live Photo components, iCloud original retrieval, and video
transfer require further work on a native iOS companion app and Windows receiver.

### Duplicate review (never delete)

`python -m ai_photos.cli duplicates` produces SHA-256 exact duplicate groups and
**heuristic** perceptual-hash visually similar groups. Similar-looking files may
not be safe to delete. Neither the engine nor UI has an auto-delete action.
Fingerprints are cached and invalidated on file size or timestamp changes.

### Backup options: off by default

```powershell
# Dry run; target must be on independent non-overlapping storage:
python -m ai_photos.cli backup-local 'E:\Photo Backup'
# Explicit action, originals remain unchanged:
python -m ai_photos.cli backup-local 'E:\Photo Backup' --apply

# Optional AWS S3 / Cloudflare R2 / Backblaze B2 S3 API (requires boto3):
python -m pip install -r engine/requirements-cloud.txt
python -m ai_photos.cli backup-s3 --bucket YOUR_BUCKET --endpoint-url https://YOUR-S3-ENDPOINT
# Review the plan, credentials and bucket configuration first. Then add --apply.
```

Credentials come from normal AWS SDK credentials/environment; the application
does not request or write your cloud secrets. S3 uploads use HTTPS transport,
**not end-to-end client-side encryption**; configure bucket-level encryption,
versioning, recovery tests, and independent backup copies before relying on it.
There is **no restore UI or verified disaster-recovery procedure** yet.


## Phase 2: native iPhone automatic backup (development)

The **iOS Photos Companion** SwiftUI source is in `ios/PhotosCompanion/` (XcodeGen
project). It exports HEIC, Live Photo HEIC + paired MOV, ProRAW DNG and full
video resources through PhotoKit, then submits resumable SHA-256-verified 8 MiB
chunks over private HTTPS (Tailscale Serve). Windows now offers a Sync Management
dialog: device pairing, progress, pause/resume, stop server and per-device revoke.
The first large backup should run in the foreground; background processing
is opportunistic, **not an always-on real-time sync**. No source photos deleted.

Read [`docs/PHASE2_NATIVE_SYNC.md`](docs/PHASE2_NATIVE_SYNC.md) for architecture,
protocol, Mac/iOS build steps, security requirements and acceptance gates.

Run on a **Mac with Xcode + XcodeGen**: `bash scripts/build-ios.sh`. There is
not yet a verified iPhone build, Apple TestFlight distribution, or a signed
Windows executable. Tests currently exercise the Windows sync protocol only.
Install `pip install -r engine/requirements-heic.txt` for optional HEIC previews.

## Verified resource backup manifests

Local and S3 backup plans include completed native video/DNG/Live Photo resources,
with a JSON manifest retaining original names, asset associations, capture times
and hashes. Stale, offline or corrupt entries are reported as incomplete instead
of silently skipped. Local output rejects junction/symlink/reparse paths.

Local backups now retain immutable v2 snapshots and content-addressed objects.
Changing a photo creates a new version; previous snapshots remain restorable.
A failed run keeps the last successful manifest usable. Existing v1 backups are
preserved during migration and remain selectable. Nothing automatically prunes
history or leftover objects from interrupted runs; plan storage accordingly.
This policy applies to **local** backups; S3 still requires provider versioning
and a separately verified recovery policy.

```powershell
# Verify a local backup and plan restoration into an EMPTY independent directory:
python -m ai_photos.cli restore-local 'E:\Photo Backup' 'F:\Restored Photos'
# Explicit copy; existing destination files are never overwritten:
python -m ai_photos.cli restore-local 'E:\Photo Backup' 'F:\Restored Photos' --apply
# List retained snapshots, then restore a selected historical version:
python -m ai_photos.cli backup-history 'E:\Photo Backup'
python -m ai_photos.cli restore-local 'E:\Photo Backup' 'F:\Older Photos' --snapshot SNAPSHOT_ID --apply
```

This restores original resource files plus their manifest; it does not recreate
Apple Photos albums/edits or provide a cloud restore GUI. No E2EE is implemented.
The native app now requires a private `https://...ts.net` origin. Changing the
computer requires re-pairing. Strict Wi-Fi mode exports only local originals;
PhotoKit cannot prohibit cellular access per iCloud request, so download iCloud
originals in Photos first or explicitly disable this restriction. iOS scheduling
and these repairs still require Mac/Xcode and real iPhone acceptance testing.

See `docs/review/FIX_VALIDATION.md` for the local repair evidence and open gates.
