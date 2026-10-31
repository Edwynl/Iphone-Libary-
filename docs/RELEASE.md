# Release engineering, privacy and licenses

**Do not present this MVP as a packaged or validated consumer release.**

## First production targets

- Windows 11 and Windows 10 2004+, x64, en-US/zh-CN interface.
- Signed `.msix` or verified `.exe` with crash logging that excludes file contents.
- No local Python install requirement (bundle/replace worker).
- Optional model download with clear size display, progress, cancellation and
  pinning by immutable revision + SHA256 validation.
- All photo processing stays offline after initial model download.
- Core app should function without model or internet access.

## Third-party review needed

- `google/siglip2-base-patch16-224`: current HF model card lists Apache 2.0;
  include a license and notice for the exact published revision.
- `Microsoft.WindowsAppSDK` (WinUI): distribute according to applicable
  Microsoft redistribution terms and target Windows App Runtime requirements.
- Python, NumPy, Pillow, PyTorch, Transformers, Hugging Face Hub, safetensors,
  SentencePiece: inventory transitive licenses, runtime and redistribution size.
- Confirm all dependency versions, provenance and hashes in an SBOM before release.

## Privacy verification

- No telemetry by default. Opt-in iPhone companion starts a token-gated HTTP endpoint bound to 127.0.0.1 only; use Tailscale Serve HTTPS, never a public port.
- SQLite cache / thumbnails: user-visible size accounting, purge and rebuild UI.
- External drives and SMB shares: never delete indexing records if inaccessible.
- Deletion from the catalog must never delete source photo files.
- Use Windows DPAPI if sensitive settings or API tokens are introduced later.
- Ensure model downloader can be disabled for enterprise/offline installs.

## Performance and QA gates

- GPU (NVIDIA), CPU-only PC, 8GB RAM baseline, HDD, network share, laptop sleep.
- Windows 10 2004 and Windows 11 x64; ARM64 evaluated separately.
- Offline boot, missing interpreter, partial model download, corrupt HEIC/RAW,
  duplicate paths, large images, Chinese filenames, Unicode and EXIF rotation.
- Controlled index throughput on 1k / 10k / 50k samples with observed metrics.
- Signed installer installation, uninstall, upgrade and clean VM acceptance.

## Review remediation validation (2026-10-30)

- Local x64 .NET 10 Restore/Release Build and shipped worker/static-web smoke pass.
- Source safety and resource manifest/restore regressions run on Windows; see
  `docs/review/FIX_VALIDATION.md` for exact results and limitations.
- Dependency metadata inventory: `docs/DEPENDENCIES.md`; hashed Python locks target
  Python 3.11. AI model revision and artifact hashes are checked on explicit install.
- New simulator CI configuration is present, but no GitHub Actions were triggered.
- The next review delta adds immutable local backup snapshots, selected-history
  restore, failure-boundary regression tests and the missed Swift scene-activation
  scheduler handling. See `docs/review/PR3_BLOCKER_FIXES.md`; Swift/Xcode builds
  still have not run locally. S3 retention/recovery is a separate open gate.
- Still required: actual Xcode/API/type validation and physical iPhone matrix;
  real model inference and 50k–100k media workload; physical cross-volume/power-loss
  tests; real S3 provider recovery and immutable backup policy; exact license text
  redistribution/SBOM review; signed installers, upgrades and TestFlight acceptance.
