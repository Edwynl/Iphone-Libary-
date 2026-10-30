# Phase 2 — Native iPhone Backup & Windows Sync Management

Status: **development implementation / Draft PR**, not a signed TestFlight app or verified Windows installer.

## Architecture

1. iOS `PHAsset.fetchAssets` scans local Photos metadata. Users must approve Photos permissions; Limited Photos selection backs up only the selected items.
2. `PHAssetResource.assetResources` picks **original HEIC/JPEG/ProRAW DNG** still resources, **original videos**, and the **paired MOV** for Live Photos. No recompression. The two Live Photo resources share the same `PHAsset.localIdentifier` on Windows.
3. `PHAssetResourceManager.writeData` exports each resource to the iPhone app's private Application Support directory. iCloud-only resources may need iCloud download.
4. The iPhone calculates whole-file SHA-256 and sends transfer metadata to the paired Windows server over Tailscale Serve **HTTPS**.
5. Windows returns an idempotent transfer ID and durable byte offset. Files are sent in at most **8 MiB chunks** using iOS file-backed background `URLSessionUploadTask`. Each chunk has `X-Upload-Offset` and `X-Chunk-SHA256`; Windows fsyncs the chunk **before acknowledging it**.
6. On reconnection/relaunch, the iPhone re-initiates the resource and resumes from Windows' acknowledged offset. After complete whole-file verification, Windows moves the staged resource to the user-visible `Pictures/Windows AI Photos Imports/<sha-prefix>/` folder and marks the transfer complete.
7. The iPhone records completion in its local SQLite checkpoint. The Windows Sync Management dialog shows device identities, recent jobs, and byte progress; pause/resume, stop service, and revoke individual device credentials are available.

## Trust model & security

- No public inbound port. Windows binds `127.0.0.1:8787`; use `tailscale serve --bg 8787`. iPhone requires a `https://...ts.net` address and Tailscale installed/signed in.
- Windows exposes a **master pairing code** via the "Enable iPhone pairing" UI. Use it once on the iPhone; server exchanges it for a unique per-device credential. iPhone stores that credential in Keychain. Windows stores only its SHA-256 digest. Revoke device from Windows without rotating all devices.
- Sync is opt-in, **disabled at startup** until explicitly enabled from Windows; background syncing on iPhone is off by default until the user toggles it.
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

Run Windows AI Photos in dev mode; click "Enable iPhone pairing" and keep the local worker running. From an administrator/user PowerShell with Tailscale installed:

```powershell
tailscale serve --bg 8787
tailscale serve status
```

Open `https://<your-device>.<your-tailnet>.ts.net` on your iPhone (with Tailscale active), then enter that URL and the master pairing code in Photos Companion; pair once. Enable Automatic Backup and choose Wi-Fi only (default).

**Do not** use `tailscale funnel`; `funnel` makes the service public. Do not open port 8787 on your router.

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
