# iPhone sync and cloud backup design decisions

## Recommended deployment

- One Windows WinUI 3 photo library plus a **localhost-only** upload/browse service.
- Pair from iPhone Safari over a **Tailscale Serve HTTPS** URL. No WAN port forwarding, no exposed reverse proxy, no public Funnel.
- On trusted home LAN, Tailscale can still route peer-to-peer; do not expose raw HTTP to arbitrary home devices.
- Windows machine must remain powered on. Tailscale does not turn it into always-on cloud storage.
- Store new iPhone files in a user-visible import directory; never delete or write Photos assets on iPhone.

## Current MVP

- Local Windows client and AI search (optional SigLIP 2).
- Manual iPhone image uploads through mobile Safari/PWA-style page.
- Bearer token authorization, 512 MiB upload limit, localhost-only listener.
- Import deduplication by full SHA-256 for identical transfers.
- Recent-photo read-only mobile browsing through authenticated thumbnail fetches.
- Non-destructive exact/near-duplicate suggestions for PC library.
- Offline/external-drive backup with verified file copies; optional S3-compatible backup via explicit CLI action.

## Future native iOS app for true automatic background sync

- SwiftUI + PhotoKit with user-granted Photos access (support full and limited access).
- Change tracking via PhotoKit; persistent upload queue and content SHA-256.
- Background URLSession file uploads, with iOS scheduling / power constraints.
- On supported iOS versions investigate PhotoKit background resource upload extensions.
- Pair once with QR/token and mutual identity verification, rotate/revoke tokens.
- Wi-Fi/Tailscale preference, resumable chunked uploads for 1-10 GB videos.
- Preserve original HEIC/HEIF, EXIF, Live Photo still/video pairing and identifiers.
- Detect iCloud-optimized off-device originals; permission and download UX.
- Sync checkpoint per device, conflict handling and verified checksum receipts.
- No automatic deletion on Windows or iPhone; device-to-device mirror is separate from backup.

## Cloud backup hardening before commercial release

- Provider profiles: external USB/NAS, S3/R2/B2, then optional OneDrive/iCloud interoperability.
- Client-side encrypted uploads with OS-managed or recovery-key-managed keys.
- Credential storage via Windows Credential Manager/DPAPI, not app configuration or GitHub.
- Versioning, retention, snapshots, dedup/chunking, throttling, schedules, restore wizard.
- Cloud backup is optional, OFF by default, and costs depend on provider/storage/egress.

## Important acceptance gates

- End-to-end test of actual iPhone -> Tailscale Serve -> Windows upload.
- Windows reboot, PC sleep/wake, iPhone power loss, upload retries, Wi-Fi switching.
- iOS native app QA on physical devices; Safari web UI cannot guarantee automatic background sync.
- 50k image duplicate scan benchmarks; never assume perceptual hashes prove identity.
- S3 restore and integrity drills; test least-privilege bucket credentials.


## Phase 2 implementation (2026-10-30)

The native SwiftUI app, PhotoKit export, best-effort background scheduling,
per-device pairing, resumable chunk protocol, and Windows transfer-management
surface are on `feat/iphone-native-resumable-sync`. See `PHASE2_NATIVE_SYNC.md`.
**Still to validate:** iOS physical device / Xcode, Windows WinUI build,
Tailscale remote transfer and >4 GiB videos.
