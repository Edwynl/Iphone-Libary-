# PR #3 三项阻断问题修复

任务日期：2026-10-31（客户端 Australia/Sydney；工具日志时间保持原样）。
工作目录：`C:\Antigravity\Claude Project\Windows AI Photos`。
分支：`fix/review-backup-sync-hardening`，此次基于 `3886da6e26e6da7a2b8806bd378828998ccbd560` 修复，继续更新 Draft PR #3。
审核来源：[PR #3 review 5710996402](https://github.com/Edwynl/Iphone-Libary-/pull/3#pullrequestreview-5710996402)。

## Executive Summary

三项源码问题均已修改：遗漏的 Swift throwing 调用改走统一错误处理；本地备份保存不可变内容与历史快照；失败备份不再发布不完整清单替换最后成功状态。用户已批准修复。**本地 98 passed / 1 skipped / 0 failed；Windows x64/.NET 10 Release 构建 0 警告、0 错误。**

新增 23 项快照回归测试，加 2 项实际存储目录 junction 测试。真实构建输出 worker/CLI 已完成两次备份、历史列表、旧版及最新版恢复，全部逐字节验证。只使用生成的测试图片与隔离目录，未使用真实用户照片、云端、Tailscale 配置或受保护的 8765/8766 服务。

**Swift 的源码调用修复不等于 iOS 构建通过。** 当前 Windows 没有 Swift/Xcode 工具；13 个 Swift 文件通过 grammar 解析，所有显式 `BackgroundScheduler.schedule()` 调用点完成静态核对，但 Swift 类型/SDK、Simulator、签名 iPhone 和真实后台同步仍 **Not Verified**。未来完整 Simulator 构建入口已存在于 `scripts/build-ios.sh` 与 CI 配置，本轮没有执行 GitHub Actions。

原完整审查、前两次修复报告和日志保留为历史记录，不覆盖原有验证结论，也不将其计数作为当前结果。

## P1-1：遗漏的 Swift 错误处理调用

- 原因：`BackgroundScheduler.schedule()` 改为 throws 后，场景激活回调仍直接调用，没有 try/catch。语法解析无法发现这个类型检查错误。
- 文件/行：`ios/PhotosCompanion/Sources/PhotosCompanionApp.swift:15`；已有统一处理在 `SyncState.swift:78`。
- 修复：场景激活改为 `sync.scheduleBackground()`，与其他调用点共用显示 `schedulingError` 的处理路径。
- 验证：核对整个 Sources，唯一剩余的显式 `BackgroundScheduler.schedule()` 调用位于 SyncState 的 do/try/catch 中；场景文件改走 wrapper。13 文件 grammar 解析无错误。
- 状态：**源码修复；Xcode 类型/SDK 编译和错误显示真机验证 Not Verified。** 本地 Python 测试数不作为 iOS 编译证明。实际 Simulator 编译和签名 iPhone 验收仍是合并门槛。

## P1-2：本地备份覆盖历史版本

- 原因：原来按源文件相对路径写入，源内容改变后调用默认允许替换的 copy，覆盖此前已备份字节。
- 文件/行：`engine/ai_photos/backup.py:155`、`:199`、`:214`；`engine/ai_photos/file_safety.py:238`。
- 修复：本地 v2 格式使用不可变内容文件与不可变快照。内容只允许 `replace=False`，相同摘要的内容可以被不同快照/文件名引用；已存在但校验不匹配的内容明确报错并保留，不静默覆盖。快照 JSON 同样只允许排他发布。

```text
backup/
  objects/<sha-prefix>/<sha256><extension>  # immutable resource bytes
  snapshots/<snapshot-id>.json             # immutable complete manifest
  snapshots/legacy-<manifest-sha>.json      # retained complete v1 manifest
  backup-manifest.json                    # complete last-successful selection
```

- 恢复路径：v2 清单区分 `backup_path`（内容文件）和 `restore_path`（原始目录/文件名）。Live Photo、MOV/DNG 的关联、原名、拍摄时间等原有 metadata 继续保留。恢复前验证全部清单路径、SHA 和大小，实际复制仍禁止覆盖已有恢复文件。
- 历史操作：新增 `backup-history BACKUP`，以及 `restore-local BACKUP EMPTY_TARGET --snapshot ID --apply`。省略 snapshot 使用最后成功清单。历史列表只验证清单，不声称已检查全部媒体字节；restore 才逐文件验证。
- v1 兼容：首次迁移先排他保存旧完整清单，旧平铺文件保持原样；旧失败清单仅留在 `.previous-manifests` 供检查，不作为可恢复快照展示。新备份不覆盖旧 v1 媒体路径。
- 验证：照片变化前后两个快照均恢复正确；旧内容 hash/mtime 和旧快照字节不变；相同内容保存一份内容、恢复两个原始名字；完整 v1 迁移及失败迁移后恢复通过。真实构建输出的 worker 和 CLI 也验证两个版本的保存与恢复。
- 状态：**本机生成数据验证通过。** 不承诺文件系统全局时间点一致性；仍需真实介质、跨卷、离线与物理断电验收。

## P1-3：失败增量备份使最后成功清单不可恢复

- 原因：原流程即使文件复制/校验失败，仍把 `complete=false` 的清单写到唯一受支持的恢复入口。
- 文件/行：`engine/ai_photos/backup.py:204`、`:210`、`:220`、`:223`；历史选择与恢复在 `:305`、`:335`。
- 修复顺序：校验来源 → 排他保存/复用内容 → 再次验证全部引用 → 排他发布完整快照 → 原子更新完整的根清单。文件或新快照发布失败时不写入失败清单替换根入口。未引用内容保留，可在下次重试校验后复用，不冒充已完成快照。
- 保留旧状态：根清单的完整副本继续兼容默认恢复入口，历史快照不依赖根清单。根清单损坏或丢失时，history 可找到独立快照，并按显式 ID 恢复。
- 发布错误边界：若文件系统在原子 rename **之后**才报告错误，结果仍 `complete=false`；同时检查新完整清单是否可见，如实返回 `latest_published`。这时旧快照仍在，新快照也可恢复，不做有破坏风险的回滚。不能把“报错”简单等同于“没有任何发布”。物理持久化仍需要真实断电验证。
- 验证：第二个资源复制失败（第一个已成功）、新快照清单发布失败、根清单发布前失败、rename 后模拟 durability 错误、最终摘要错误、模拟进程中断均覆盖。默认恢复保持旧完整备份，或在 rename 后错误场景恢复新完整备份；显式旧快照始终保留并可恢复。失败后重试可复用已验证的遗留内容。
- 状态：**本机故障注入验证通过。** KeyboardInterrupt 是中断模拟，不是真实断电/内核崩溃证明。

## Test Results

| 验证 | 当前结果 | 证据 |
| --- | --- | --- |
| 全套 Python pytest | 98 passed / 1 skipped / 0 failed，44.25 秒 | [log](snapshot-pytest.log)、[JUnit](snapshot-pytest.xml) |
| 新增快照测试 | 23 通过 | `tests/test_backup_snapshots.py` |
| 新增 objects/snapshots junction 测试 | 2 通过，源 SHA/mtime 保持不变 | `tests/test_review_regressions.py` |
| Windows x64 .NET 10 Release | 0 warnings / 0 errors | [build](snapshot-dotnet-build.log) |
| 实际构建输出 worker/CLI | 两次备份、2 个历史快照、旧版/最新版精确恢复、源文件未变 | [interface smoke](snapshot-interface-smoke.log) |
| 实际输出接收端与 web assets | worker ping、空隔离库、HTTP 200 ×3 | [output smoke](snapshot-output-smoke.log) |
| 输出与源码 | 17 个 Python/web 文件逐字节一致 | [validation](snapshot-validation.json) |
| Python 关键语法/名称检查 | E9/F63/F7/F82 通过 | [static check](snapshot-static-check.log) |
| Swift | 13 文件 grammar 无错误；目标调用点核对完成 | [syntax](snapshot-swift-syntax.json)、validation JSON |
| Xcode/Simulator/iPhone/Tailscale | **Not Verified** | 无 Mac/设备实测证据 |

跳过项仍为普通 Windows 符号链接权限测试；真实 NTFS junction 与非空目录租约回归均运行。生成图片不含用户照片。没有安装新的 runtime 依赖或改动 VELVET/PWA/主端口。

复现（本地 review Python 环境）：

```powershell
docs/review/.venv/Scripts/python.exe -m pytest -q tests/ --basetemp=docs/review/next-snapshot-rerun-temp -o cache_dir=docs/review/pytest-cache
docs/review/.venv/Scripts/python.exe docs/review/check_snapshot_output.py src/WindowsAIPhotos/bin/x64/Release/net10.0-windows10.0.19041.0/engine
docs/review/.venv/Scripts/python.exe docs/review/check_swift_syntax.py --output docs/review/snapshot-swift-syntax.json
```

## 保留限制与合并建议

- **P2 未关闭**：每个 iOS pass 仍创建 tracker 并做全量 PhotoKit/SQLite metadata 发现。必须在真实 50k/100k 图库测量耗电、发现成本及新照片等待时间；本轮不以纯内存 SQLite 数字代替真机数据，也不进行缺乏 SDK 验证的增量发现改造。
- 本次不可变快照策略适用于**本地备份**。S3 的对象 key、清单发布和版本保留策略没有在本次改造；仍需启用/验证 provider 版本化并实现完整云恢复，不能把本地结果用于宣称云端历史已安全。没有进行真实云上传。
- 不自动删除历史、失败批次遗留内容或用户文件。没有 GC/历史清理界面，存储会随保留的版本增长，磁盘不足会明确失败。恢复后得到资源文件和关联清单，不自动重建 Apple Photos 相册或编辑历史。
- 默认历史恢复仍通过 CLI，WinUI 未新增快照浏览/选择界面；没有运行本轮原生窗口交互。当前后台接收端生命周期 UI 的实测记录保留在前次报告，不冒充本轮重测。
- 当前仍为开发版；E2EE、实际 AI 与大图库性能、真实云灾备、签名安装包、跨卷/断电和 iPhone/Tailscale 验收继续保留。无新的 P0 结论，也不把原完整审查所有 P1/P2/P3 标为已关闭。

保持 PR #3 为 Draft，仍按 PR #1 → PR #2 → PR #3 的顺序复审。请 Claude 优先复核：内容/快照排他写入、旧版本保留、v1 迁移、失败边界、根清单原子选择、恢复路径/摘要验证，以及 Swift 场景激活错误处理。**本轮不合并、不发布、不触发 GitHub Actions。**
