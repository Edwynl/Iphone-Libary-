# 审核遗留 P2：图库发现复用与同步进度

任务日期：2026-10-31，客户端 Australia/Sydney。目录：`C:\Antigravity\Claude Project\Windows AI Photos`。
分支：`fix/review-backup-sync-hardening`，本轮基线 `c62f97c098afe89fd9bd30579eef83ee8323c93d`，继续更新 Draft PR #3。

## 本轮审核读取结果

GitHub 插件与 `gh api .../pulls/3/reviews` 交叉核对：开始本轮时，PR #3 尚无针对 `c62f97c` 的新独立审核，最新仍是针对 `3886da6` 的 [review 5710996402](https://github.com/Edwynl/Iphone-Libary-/pull/3#pullrequestreview-5710996402)。其中三个 P1 已在 `c62f97c` 完成源码修改，参见 [PR3_BLOCKER_FIXES.md](PR3_BLOCKER_FIXES.md)，不重复声称新发现或重新关闭。

用户要求查看并修复，因此本轮继续处理剩余两项 P2：该 review 指出的每轮全库发现成本，以及 [PR #2 review 5710874807](https://github.com/Edwynl/Iphone-Libary-/pull/2#pullrequestreview-5710874807) 对“批次成功看似全库完成”的进度建议。

**结果边界：源码已改；100 passed / 1 skipped 的 Python 测试和 15 文件 Swift grammar 检查通过。9 个原生 Swift XCTest 已准备但未执行，Xcode、PhotoKit、设备耗电/性能仍 Not Verified。不能把 P2 的真机验收标成关闭。**

## P2-1：自动小批次重复扫描整个图库

- 原因/影响：此前每个 `performSync` 新建 `PhotoLibraryTracker`，即使同一进程上次发现成功、图库没变化，每次 20 个资产预算也要重新读取全库 metadata 并 UPSERT SQLite，增加启动、CPU 和数据库成本。
- 位置：`SyncState.swift:49`、`:167`；`PhotoSource.swift:24`、`:63`、`:89`；新 `DiscoveryCacheState.swift:7`。
- 修改：SyncState 在获得合法 Photos 访问后才创建 tracker，并在同一进程的后续 pass 中保留它。相同 receiver/权限上下文、已成功完成、未收到相关变化且未过期时，直接跳过重新 fetch、SQLite 发现写入及空的 detached task。
- 安全失效：冷启动、手动全量同步、进入前台、receiver/权限模式变化、观察到 fetch result 变化、发现阶段失败/取消及缓存资格超过六小时都会要求全量刷新。六小时不是唤醒保证，只在下一次机会检查。系统时间倒退也不复用缓存。退出配对时显式取消观察并释放 tracker。
- 竞争保护：每次真正扫描取得唯一 revision ticket，成功发布 queue generation 后才能标记完成；扫描途中收到变化时，旧 ticket 不能清除新变化。失败/取消不能让半次发现变成可复用结果，过期任务的失败也不能撤销后续成功 ticket。PhotoKit fetch 和 UI 回调均不在 notification lock 内执行。
- API 依据：Apple 的 [PHPhotoLibraryChangeObserver](https://developer.apple.com/documentation/photos/phphotolibrarychangeobserver) 用于观察已获取资源/结果的变化；[PHFetchResultChangeDetails](https://developer.apple.com/documentation/photos/phfetchresultchangedetails/hasincrementalchanges) 区分变化信息和更新后的结果。本轮仍采用需要时的完整发现，没有实现 persistent change token 或逐条增量发现。
- 验证：新增 5 个纯 Foundation XCTest 场景，覆盖无变化重复小批次、扫描中变化、取消重试与过期回调、receiver/授权隔离、冷启动/手动失效/到期/时间倒退。它们包含 5,000 个无变化逻辑批次的复用断言，但 **没有在 Windows 执行**，更不是 100k 真机照片基准。
- 状态：**无条件“每个 pass 都重扫”的实现已调整，实际节能/耗时仍 Not Verified。** 进程被 iOS 回收、每次冷启动、频繁变化或频繁切回前台时仍会全量发现；此变化不等于解决所有大型图库发现成本。

## P2-2：区分批次、当前积压与快照完成

- 原因/影响：旧审核中的有限预算完成提示会让用户误以为全库已备份。上一轮已增加 `remaining == 0` 限制，但 UI 仍写 Last successful pass，历史时间未按 receiver 隔离，也没有明确的未知积压显示。
- 位置：`SyncState.swift:40`、`:85`、`:263`；`ContentView.swift:59`、`:69`；`AssetSyncQueue.swift:67`、`:161`；`PhotosCompanionApp.swift:14`。
- 修改：界面分别显示 Last successful batch、Pending or failed、Last snapshot fully backed up。发现未知/过期/变化时积压显示 Not checked yet，不用零代替未知。Photos 变化通知异步使界面进度失效，回到前台也要求重新核对。
- 完成规则：仅当当前 receiver/权限上下文仍有效、发现已完整提交 generation、pending/failed 为零且本次没有失败，才能记录完整快照日期。批次时间需要实际处理资产成功；同一 generation 的空 pass 不刷新完整快照日期。日期和 generation 使用 receiver 隔离的 UserDefaults key，旧全局 `lastCompleted` 不迁移为可信的当前接收库状态。断开配对后清空当前显示，正在同步时不允许重新配对混入另一上下文。
- 语义：快照完成是当前观察到的、已允许访问的资源版本已全部具有完成记录；它**不是**此批次对所有 Windows 文件重新做过 SHA 审计的承诺。界面明确说明这一点，并保留新变化可能尚未处理的提示。
- 验证：2 个新增 Python 测试执行 Swift 文件里的实际 SQL：100 个资产完成 20 个仍有 80 个 pending/failed、没有 generation 的半次发现不能成为完成快照；接收端 generation 查询隔离。追加 1 个原生 queue XCTest 验证预算语义，**未执行**。SwiftUI 的日期、切库、权限和取消场景仍需 Xcode/真机验证。

## 本机测试与证据

| 项目 | 结果 | 记录 |
| --- | --- | --- |
| 全套 Python pytest | 100 passed / 1 skipped / 0 failed，45.00 秒 | [log](discovery-pytest.log)、[JUnit](discovery-pytest.xml) |
| Swift SQL 合约测试 | 共 6 项通过，其中 2 项本轮新增 | `tests/test_swift_queue_contract.py` |
| Python E9/F63/F7/F82 | 通过 | [static](discovery-static-check.log) |
| Swift grammar | 15 文件通过 | [syntax](discovery-swift-syntax.json) |
| Swift XCTest | 共 9 个已准备，本轮新增 6 个，**未运行** | `ios/PhotosCompanion/QueueTests/` |
| Xcode/Simulator/PhotoKit/iPhone/耗电 | **Not Verified** | 本机无 macOS/Xcode |
| Windows/.NET 本轮重建 | **未重跑**，没有更改 engine/WinUI 源码 | `c62f97c` 的历史构建/输出 smoke 记录保留 |

普通 Windows symlink 权限测试跳过，实际 NTFS junction 回归仍运行。新增/完整测试没有使用私人媒体或真实云端；缓存和临时数据在忽略目录。原 98 项本地 Python 通过的测试本轮仍在套件中。所有证据校验与 Swift 文件摘要见 [discovery-validation.json](discovery-validation.json)。

## 原生验收要求与仍开放项目

在 Mac 先运行：

```bash
swift test --package-path ios/PhotosCompanion
bash scripts/build-ios.sh
```

再以真实 50k/100k 图库记录：首次发现、同进程下一自动批次、发生变化、权限/选择变化、手动同步、进入前台、六小时资格到期、进程重启和发现中 BG 过期。用 Instruments 比较 PhotoKit fetch 次数、发现 UPSERT、CPU、内存、墙钟耗时和电量；确认无变化复用时只处理预算队列，变化/中断后必须完整刷新。不得用旧的内存 SQLite benchmark 宣称节能成功。

UI 验收需要覆盖：100 资产只完成 20；失败资源退避未到期；无可处理资源但仍有失败；选择缩小/扩大；receiver 切换；取消；相同 generation 的重复空 pass；原先全局历史日期。确认未知积压不会显示为零、批次成功不会作为完整快照成功。

三个旧 P1 的源码修复和本地备份验证继续保留，iOS 编译门槛仍未满足。S3 历史/恢复、E2EE、真实手机/Tailscale、实际 AI、大型图库、安装签名、跨卷/断电等门槛没有在本轮宣称完成。没有新 P0 结论，也没有把全部原始 P1/P2/P3 标为关闭。

保持 PR #3 Draft，按 PR #1 → PR #2 → PR #3 复审，不合并、不发布、不运行 GitHub Actions，不改动 VELVET/PWA 或 8765/8766。请 Claude 复核 revision/context 的并发规则、取消与 observer 生命周期、receiver 隔离、generation/进度和 Swift API/actor 正确性。
