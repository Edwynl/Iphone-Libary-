# Engineering handoff to Claude Code / Codex

## Milestone 0 (delivered source)

- [x] Dedicated WinUI project and persistent JSONL local engine sources
- [x] SQLite catalog, scan and thumbnails without photo modification
- [x] Content search implementation behind explicit SigLIP 2 installation
- [x] Filename fallback when AI is absent or unavailable
- [x] Incremental index + offline root preservation
- [x] Remove folder from catalog without touching originals, and exclude own cache
- [x] Python tests passing in Linux environment
- [ ] Windows x64 `dotnet restore` and `dotnet build` pass on real runner
- [ ] Real WinUI browse and folder picker validated on Windows 10/11
- [ ] Real SigLIP 2 downloaded and text/image embedding shape validated
- [ ] 50,000 images ingestion benchmark and GPU/CPU memory profiling

## Reviewer checklist / PR preparation

1. Check API/build compatibility of `MainWindow.xaml`, `App.xaml` and code-behind
   with Windows App SDK 2.6.4, .NET 10 and Windows SDK 10.0.19041.
2. Validate `FolderPicker` HWND initialization in an unpackaged WinUI application.
3. Validate both `Siglip2Model.get_text_features` and `get_image_features` return
   the expected tensor or `pooler_output` in pinned transformers version.
4. Confirm Chinese and English search retrieval quality on representative images.
5. Confirm an empty, absent, read-only, disconnected and network-share photo root.
6. Confirm no model download happens before explicit consent.
7. Confirm the app never writes to source folders, including orientation correction.
8. Measure 50,000-image latency: first scan, warm scan, semantic search, thumbnails.
9. Review package size and licensing for Windows SDK, Python, Pillow, torch,
   transformers, SigLIP 2; ship notices where required.
10. Add cancellation, failure recovery and long-running scan resume before beta.

## Acceptance criteria for beta v0.2

- Clean Windows machine installs a signed installer with **no Python or Ollama**
  prerequisite; model optional and activated only after user confirmation.
- Able to index 50,000 standard image files, restart and continue without
  unnecessary re-indexing, search in Chinese+English, search while adding files.
- No source mutation; invalid/corrupt photos do not crash the scan.
- A loss of access to external drive does not erase its catalog.
- Automated Windows CI plus clean VM smoke test are both documented.

## Suggested next PRs (avoid one giant merge)

1. `fix/windows-build-and-launch`: Windows CI validation; fix build issues.
2. `feat/semantic-model-validation`: complete real model smoke tests and fixture.
3. `feat/file-watcher-and-cancel`: live updates, pause/resume, progress.
4. `feat/standalone-onnx-runtime`: verified self-contained Windows inference.
5. `feat/installer-signing`: MSIX/EXE, code signing, upgrade tests.
