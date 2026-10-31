# Phase 2 — Native iPhone Backup & Windows Sync Management

Status: **development implementation / Draft PR**, not a signed TestFlight app or verified Windows installer.

## Architecture

1. iOS `PHAsset.fetchAssets` scans local Photos metadata. Users must approve Photos permissions; Limited Photos selection backs up only the selected items.
2. `PHAssetResource.assetResources` picks **original HEIC/JPEG/ProRAW DNG** still resources, **original videos**, and the **paired MOV** for Live Photos. No recompression. The two Live Photo resources share the same `PHAsset.localIdentifier` on Windows.
3. Cancellable `PHAssetResourceManager.requestData` exports each resource to the iPhone app's private Application Support directory. Strict Wi-Fi mode reads local originals only; PhotoKit has no per-request cellular prohibition. For iCloud-only originals, download in Photos first or explicitly allow system network retrieval by disabling Wi-Fi-only mode.
4. The iPhone calculates whole-file SHA-256 and sends transfer metadata to the paired Windows server over Tailscale Serve **HTTPS**.
5. Windows returns an idempotent transfer ID and durable byte offset. Files are sent in at most **8 MiB chunks** using iOS file-backed background `URLSessionUploadTask`. Each chunk has `X-Upload-Offset` and `X-Chunk-SHA256`; Windows fsyncs the chunk **before acknowledging it**.
6. On reconnection/relaunch, the iPhone re-initiates the resource and resumes from Windows' acknowledged offset. Windows copies into a verified destination-volume temporary file, then atomically publishes content-addressed bytes under `Pictures/Windows AI Photos Imports/<sha-prefix>/`. Staging remains until the completed DB commit, so cross-volume storage and interrupted finalization can recover. A corrupt old import is retained and recovery uses a new path.
7. The iPhone checkpoints SHA-256/size under a receiver ID and checks Windows' actual file integrity before skipping a resource. The management dialog refreshes every three seconds, with pause/resume, stop, device revoke and explicit incomplete-staging discard. Confirmation actions suspend automatic row replacement.
8. A receiver-scoped SQLite queue uses stable PhotoKit IDs and metadata versions. Full discovery reads metadata in batches of 500 off the main actor. An observer is retained across passes in the same process, allowing unchanged automatic batches to reuse a successfully completed discovery. Changes, receiver/permission changes, foreground activation, manual full sync, discovery failure/cancellation, cold process start and cache expiry require a full refresh. Cache eligibility expires after six hours; this is checked on the next opportunity, not a scheduled six-hour guarantee. Newly discovered/changed assets precede the old backlog even when their EXIF dates are old. Of each 20-job batch, up to one slot retries a due failure and one audits a previously completed asset. Audits are eligible after 24 hours, **not guaranteed to run every 24 hours**; actual latency depends on backlog, foreground use and iOS scheduling. Selected-library removal only changes queue eligibility, never deletes originals. This process-local reuse is not persistent incremental PhotoKit history; real iPhone discovery/energy costs remain Not Verified.
9. iPhone progress separates the last successful batch, pending/failed assets and the last fully backed-up observed snapshot. Completion history is scoped to the Windows receiver. Unknown/stale discovery shows an unknown backlog rather than zero. Snapshot completion requires a current discovery, a committed generation, zero pending/failed assets and no pass failure; it does not imply a fresh integrity audit of every Windows file in that batch.

## Trust model & security

- No public inbound port. Windows binds `127.0.0.1:8787`; use `tailscale serve --bg 8787`. iPhone requires a `https://...ts.net` address and Tailscale installed/signed in.
- Windows exposes a **master pairing code** via "Enable iPhone pairing". This is reusable root pairing authority for the service session, not a one-time code; keep it private. Devices receive separate credentials stored in iPhone Keychain; Windows stores their SHA-256 digests. A holder of the master code can pair again after a device revoke. Short-lived pairing windows remain a hardening gate.
- Sync is opt-in, **off until explicitly enabled** from Windows; background syncing on iPhone is off by default until the user toggles it. Once enabled, an independent receiver survives WinUI close. Reopening WinUI attaches to the same receiver. Stop it in Manage sync. It does not automatically start after sign-out or reboot; re-enable it to resume durable transfers.
- Windows can pause syncing while allowing metadata/status endpoints; revoked tokens cannot resume existing jobs. Staging remains local and no original photos on the iPhone or Windows are deleted.
- Filenames are sanitized; server refuses traversal, oversized JSON/chunks, unknown extensions, unauthenticated uploads, wrong offsets, cross-device job access, SHA-256 mismatches.
- The HTTP server is designed for **personal Tailnet use**, not internet hosting. A production release requires independent security review, quotas, recoverability, traffic testing and audit logging.

## iPhone development setup (macOS required)

Install Xcode 16+ and XcodeGen (`brew install xcodegen`), then:

```bash
bash scripts/build-ios.sh
# OR:
cd ios/PhotosCompanion
xcodegen generate
open PhotosCompanion.xcodeproj
```

Select an Apple Development Team in Xcode signing; build to a **real iPhone** for Photos and background transfer tests. The iOS bundle ID is `com.edwyn.windowsaiphotos.companion` (customize if already used by another developer).

## Windows setup

Run Windows AI Photos in dev mode; click "Enable iPhone pairing". The independent receiver continues after the window closes; the dialog explains how to stop it. From an administrator/user PowerShell with Tailscale installed:

```powershell
tailscale serve --bg 8787
tailscale serve status
```

Open `https://<your-device>.<your-tailnet>.ts.net` on your iPhone (with Tailscale active), then enter that URL and the master pairing code in Photos Companion; pair once. Enable Automatic Backup and choose Wi-Fi only (default).

**Do not** use `tailscale funnel`; `funnel` makes the service public. Do not open port 8787 on your router.

CLI management from `engine/`, using the same Python environment and `AI_PHOTOS_HOME` as the app:

```powershell
python -m ai_photos.cli receiver-start --port 8787
python -m ai_photos.cli receiver-status
python -m ai_photos.cli receiver-stop
```

`receiver-status` does not start a receiver. The existing `serve-iphone` command remains a foreground server. Ports 8765 and 8766 are refused. No service installation, scheduled task, registry startup entry, firewall rule or Tailscale configuration is applied by these commands. Stop drains admitted requests; long disk work may report stop pending before it finishes.

Management uses authenticated JSON over a remote-rejecting Windows named pipe; its control secret is DPAPI-encrypted for the current Windows user. It is separate from the loopback media HTTP listener and is never forwarded through Tailscale. A per-data-directory OS lease prevents two managed receivers. A new receiver process rotates the root pairing code; existing device credentials and acknowledged offsets remain in SQLite. The HTTP server admits at most 16 connections, uses a 15-second inactivity timeout, and returns retryable HTTP 503 when saturated. Existing per-device pending-transfer and disk-reserve checks remain; this is not a full hostile-client rate limiter.

## API v1 contract

- `POST /api/sync/v1/pair` with master bearer code: `{device_id, name}` -> `{device_id,device_token,protocol}`
- `POST /api/sync/v1/transfers` with device bearer: `{asset_id,resource_type,filename,size,sha256}` -> `{transfer_id,offset,size,status,chunk_bytes}`
- `GET /api/sync/v1/transfers/{id}` -> durable offset and status
- `PUT /api/sync/v1/transfers/{id}/chunk` with `X-Upload-Offset`, `X-Chunk-SHA256`, binary body <= 8 MiB -> new durable offset
- `POST /api/sync/v1/transfers/{id}/complete` -> status after whole-file SHA-256 and move into imports
- `GET /api/sync/v1/status` -> device connectivity/paused

HTTP 409 with `expected_offset` means iPhone must re-query server before retry. HTTP 423 means owner paused uploads. HTTP 401 means the device is unpaired or revoked. All error bodies are JSON.

## Important limits

- Apple decides **when** BGProcessingTask runs; not real-time or guaranteed daily execution. Force quitting the app prevents automatic background relaunch. File-backed background URLSession allows an in-flight chunk to continue under normal suspension; new chunks may wait until an iOS scheduling opportunity.
- The first library backup should be run with Photos Companion in the foreground, plugged in and on Wi-Fi for large videos. Every individual asset export can temporarily consume storage equal to that asset's size (plus an 8 MiB chunk).
- **No iPhone build or physical Tailscale test was possible in this non-macOS environment.** Current tests validate the Windows protocol, not actual PhotoKit runtime behavior.
- The Photo Library catalog currently indexes HEIC as a photo (optional `pip install -r engine/requirements-heic.txt` to enable thumbnails) but does not provide video thumbnails/playback in the main photo grid. Videos are stored losslessly and appear in the transfer dashboard.
- Cloud backup and Windows installer remain Phase 3; no Android companion or multi-user internet hosting.
- Server's pairing code rotates when the Windows sync service process restarts, but already-paired per-device tokens remain valid until revoked.

## Review gates (before merge or TestFlight)

1. XcodeGen generation + Xcode iOS Simulator build, then real iPhone Code Signing / PhotoKit resource export tests.
2. Windows x64 WinUI 3 build, manual UI verification of Sync Management actions.
3. HEIC original bitwise hash match; Live Photo HEIC + MOV share asset ID; >4 GB MOV partial upload/interruption/restart tests.
4. Test iPhone locked, backgrounded, force-quit, low-storage, iCloud-only assets, Wi-Fi->cellular handover, lost/revoked token, interrupted Tailnet, Windows reboot.
5. Test 50,000 photos with bounded checkpoint DB growth and no duplicate imports; backup and restore Windows catalog before any production deployment.
6. External security review and packaging/licensing compliance.
