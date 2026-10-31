# PR #2 审核意见后续修复与验证

日期：2026-10-30 AEDT。工作目录：`C:\Antigravity\Claude Project\Windows AI Photos`。
修复分支：`fix/review-backup-sync-hardening`；继续更新 Draft PR #3，基于上一修复提交 `f09708be00dc91eed9f822a543e97363c8b106c1`。

审核来源：[PR #2 独立审核](https://github.com/Edwynl/Iphone-Libary-/pull/2#pullrequestreview-5709850895)，其审查基线为 `c6408c1d`。用户已批准继续修复、推送并交给 Claude 审核。本次不合并分支，不发布应用，不执行 GitHub Actions。

## Executive Summary

此次补上前次修复尚未覆盖的两个 P1：稳定资产队列，以及独立的 Windows 后台接收进程。另处理两个 P2：HTTP 连接/读取边界，以及被静默忽略的 BGTask 调度失败。

**本地验证：73 passed / 1 skipped / 0 failed；WinUI x64 Release 构建 0 警告、0 错误。** 实际 WinUI 开启接收端、关闭窗口、重开连接、点击停止均通过；worker 退出/被终止、接收进程重启和并发启动也有真实子进程测试。

**iOS 代码已实现但仍为 Not Verified。** 13 个 Swift 文件仅通过语法解析，Python 执行了 Swift 文件中的实际 SQL；这不等于 Swift 编译或 PhotoKit 正常工作。新增 Swift Package 单元测试已提供，但 Windows 上没有运行 Xcode、`swift test` 或真机测试。仍不建议直接合并或商业发布。

原始完整审查和 26 项状态保留在 [FULL_CODE_REVIEW.md](FULL_CODE_REVIEW.md) 与 [FIX_VALIDATION.md](FIX_VALIDATION.md)。这些是历史快照；不能把本次测试结果反填成当时已通过，也不能把全部问题标为关闭。

## P1 — 稳定 ID 队列替代数值游标

- 文件/行号：`ios/PhotosCompanion/Sources/AssetSyncQueue.swift:13`、`:28`、`:136`；`PhotoSource.swift:24`、`:51`、`:139`；`SyncState.swift:134`。
- 原因：旧代码按最新照片倒序取数组，再持久化数组下标。拍摄、删除或改变照片权限会改变数组位置，新照片可能一直排在已保存游标之前，必须等待旧库循环完成。
- 影响：大型图库的新增照片延迟备份，数组重排造成重复检查或跳过，后台每次仅处理少量资产时尤其明显。
- 修复：持久化 `(receiver, PHAsset.localIdentifier)`、元数据版本和发现时间。每次机会先在主 actor 外分批扫描元数据，每批 500 条，完整扫描后才激活快照；未完成发现不会宣称备份完成。新增/变化资产按发现时间优先，不依赖 EXIF 日期。PhotoKit 变化通知使下一资产前重新协调并重选任务。实际处理通过稳定 ID 重新获取资产。
- 公平性与恢复：20 个任务最多预留一个到期重试、一个到期完整性复核；失败使用退避，单次机会使用固定时间界限，避免前台循环无限重试同一个坏资源。旧版本结果不能把新版本标为完成。资源逐个执行已有的 Windows SHA 校验/断点续传，全部成功才完成资产。失去访问权限不删除原片。
- 验证：四个本地 SQL 合约测试通过，涵盖旧 EXIF 的新导入优先、重排/移除、版本失效、旧结果拒绝、接收库隔离、重试退避、复核资格和中断发现。两个 100,000 条元数据快照及一次优先查询耗时 **0.748 秒**（内存 SQLite、Python 执行实际 Swift SQL）。**不是 iPhone/磁盘数据库/PhotoKit 的性能基准。**
- 后续验收：在 Mac 运行 `swift test --package-path ios/PhotosCompanion`，再运行 Xcode 构建。真机验证 50k/100k 库处理中持续拍摄、旧照片新导入、编辑、删除、Limited Photos 变化、BG 过期及进程重启；记录新照片实际等待时间和主线程响应。以上均 **Not Verified**。

复核任务在上次验证 24 小时后具备资格，不保证每张照片每 24 小时都被复核。延迟取决于积压和 iOS 调度。成功完成一次后台预算不被误报为 BGTask 失败；只有当前快照没有 pending/failed 时才更新完整完成时间。

## P1 — 独立 Windows 接收端

- 文件/行号：`engine/ai_photos/receiver_agent.py:75`、`:126`、`:170`、`:212`、`:234`、`:252`；`worker.py:102`；`src/WindowsAIPhotos/Services/PythonEngineBridge.cs:152`。
- 原因：旧接收端在 stdio worker 内，关闭 WinUI 会终止它，后台 URLSession 无法继续连接。
- 影响：用户以为同步仍在后台运行，但窗口关闭后上传停止。
- 修复：用户点击 Enable iPhone pairing 才启动 detached 接收进程；默认不启动。WinUI 关闭仅停止 stdio worker。重新打开应用连接同一接收端；Manage sync 的 Stop receiver 显式停止。每个数据目录由 OS 独占租约保护，两个并发启动者只能得到一个实际实例。Windows 无法脱离宿主 Job 时明确报启动失败，不偷偷退回依赖窗口生命周期的模式。
- 生命周期边界：这是**当前登录会话的独立进程**，不是安装完成的 Windows Service。不自动设置开机启动/登录任务/注册表/防火墙/Tailscale。注销或重启后要重新启用；SQLite 保存设备凭据和已确认 offset。显式 Stop 等待已接纳 HTTP 请求完成；长磁盘任务可能先报告 stop pending，不能据此删除 staging 或原片。
- 验证：真实 worker 子进程退出后继续上传；新 worker 重新连接同一 PID/端口；窗口关闭时 worker 和 launcher 退出而接收端保留；实际 WinUI 重开显示 Listening；Stop 后界面显示 Server off，Stop 禁用，接收 PID 退出。真实接收进程 stop/start 后，旧设备凭据有效、offset=8 恢复、原始 MOV 合成字节及 SHA 正确。并发启动、重复启动、不同 inbox 拒绝、暂停返回 423、撤销返回 401 均通过。
- 后续验收：实际 Windows 注销/重启、安装后的运行环境、长期磁盘/网络中断、物理断电和真实手机链路仍 **Not Verified**。后台进程长期运行期间不应覆盖其部署目录；更新程序应先要求停止接收端。

### 本机管理安全

`receiver_agent.py:49` 的 Windows named pipe 拒绝远程客户端；使用认证 JSON，不使用 pickle。随机控制密钥通过当前用户 DPAPI 加密后保存（`:59`），不放在命令行。控制通道不经 Tailscale，媒体 HTTP 仍只绑定 loopback。控制状态包含实例随机标识；停止通过受认证通道完成，不按未可信 PID 杀进程。

错误密钥、数组格式请求、本机空连接、重复/并发启动测试通过。只记录非敏感状态，报告和 PR 不包含测试配对密钥。本轮并未完成独立 IPC 渗透测试；同一 Windows 用户或管理员能控制自身进程，不在隔离保证范围内。Windows 实现使用 CPython 的 `_winapi`/named-pipe 包装，当前以 Python 3.11.14 验证，其他解释器版本和最终打包环境需要复测。

## P2 — HTTP 连接与空闲读取限制

- 文件/行号：`engine/ai_photos/mobile.py:32`、`:117`。
- 原因/影响：ThreadingHTTPServer 原先能创建无上限线程，慢连接长期占用资源，影响同步稳定性。
- 修复：默认最多 16 个并发连接、15 秒 socket 空闲超时，饱和时返回 retryable HTTP 503；请求完成/出错归还名额。显式停止先停止接纳，再等待已接纳请求。未启动过的 server 关闭不会卡住。保留已有 8 MiB chunk、JSON 大小、单设备 pending 数及磁盘预留检查。
- 验证：真实 socket 占满一个名额，第二个连接返回 503；不完整 header 空闲超时释放名额，随后正常 200。停止等待不完整配对请求完成后才返回。三项专项测试通过。
- 边界：这是个人 Tailnet 接收端的资源上限，不是完整的用户级流量计费、所有设备总配额、持续慢速恶意流量防护或互联网服务安全证明。不能使用 Funnel/公网端口。

## P2 — BGTask 调度失败可见

- 文件/行号：`ios/PhotosCompanion/Sources/BackgroundScheduler.swift:35`、`:40`；`SyncState.swift:78`；`ContentView.swift:49`。
- 原因/影响：原 `try? BGTaskScheduler.submit` 静默吞掉错误，自动同步开关开启但任务未成功提交时没有反馈。
- 修复：调度函数抛出错误，由主 actor 上的 SyncState 保存独立 `schedulingError`，Automatic backup 区显示错误与重试提示。成功重新提交/关闭开关清除此状态，普通前台同步不覆盖该错误。
- 验证：静态调用一致性和 Swift 语法解析通过。SDK 类型检查及真机注入 unavailable/notPermitted/tooManyPendingTaskRequests，确认界面可见并重试成功：**Not Verified**。Apple 调度和强制关闭行为仍受系统限制。

## 审核意见覆盖情况

| 原审核项 | 当前状态 | 证据/剩余条件 |
| --- | --- | --- |
| HEIC + MOV + DNG 二次备份遗漏 | 前次已修复，本轮回归通过 | 完整原始资源 inventory、manifest、本地恢复；真实云灾备未验证 |
| 完成文件损坏仍被接受 | 前次已修复，本轮回归通过 | SHA 验证/失效/保留坏副本并恢复；物理断电仍待测 |
| checkpoint 缺接收库维度 | 前次已改，iOS 仍 Not Verified | receiver/version/Windows verify；需真机切库/库恢复 |
| URLSession/export 临时文件遗留 | 前次已改，iOS 仍 Not Verified | taskDescription、取消、启动清理；需真实进程死亡验证 |
| 数字 PhotoKit 游标 | 本次替换实现，iOS 仍 Not Verified | 实际 SQL 合约通过；Swift/native tests 未执行 |
| 关闭 WinUI 导致接收停止 | 本次本地验证通过 | 实际 GUI + 子进程 + 重启续传；不承诺开机自启 |
| 输出包缺 web assets | 前次已修复，本轮验证通过 | 17 个输出文件逐字节一致，三项 web 资源 HTTP 200 |
| 连接/超时/资源上限 | 本次部分完成并验证 | 有界并发/空闲超时；完整跨设备总配额与滥用防护待做 |
| 静默 BGTask 调度错误 | 本次实现，iOS 仍 Not Verified | 错误持久展示，需 SDK/真机验证 |
| 客户端 E2EE、云端完整恢复 | **仍未实现/验收** | 不把 TLS/S3 checksum 当端到端加密或云端灾备证明 |
| 大视频、50k+ 性能 | 证据范围有限 | 前次 4 GiB+17 合成字节测试保留；本次 100k 仅 SQL 元数据；真实图库仍待测 |

## Test Results 与复现入口

| 项目 | 结果 | 记录 |
| --- | --- | --- |
| Python 3.11.14 全套 pytest | 73 passed，1 skipped，40.39 秒 | [next-pytest.log](next-pytest.log)、[JUnit](next-pytest.xml) |
| Windows .NET 10.0.403 / x64 Release | 0 warnings / 0 errors | [next-dotnet-build.log](next-dotnet-build.log) |
| 实际构建输出 worker + web | pass / HTTP 200 ×3 | [next-output-smoke.log](next-output-smoke.log) |
| 输出与源码一致性 | 17 个文件逐字节一致 | [next-validation.json](next-validation.json) |
| 实际 WinUI 生命周期 | 开启、关窗保留、重开、停止通过 | [next-gui-lifecycle.json](next-gui-lifecycle.json) |
| Python 关键语法/名称检查 | E9/F63/F7/F82 通过 | [next-static-check.log](next-static-check.log) |
| Swift 语法 | 13 文件无 grammar error | [next-swift-syntax.json](next-swift-syntax.json) |
| Swift Package / Xcode / iPhone | **Not Verified** | Mac 与真实设备验收门槛 |

跳过项为普通 Windows symlink 权限测试；实际 NTFS junction/原片 hash 与 mtime 保护用例均执行通过。完整性/原片保护回归仍包含在这 73 项中。所有数据为测试生成内容。测试、SDK、缓存和媒体 fixture 留在忽略目录，没有上传照片、安装自启动服务或修改 VELVET/PWA、8765、8766。

从项目根目录使用本地 review Python 环境：

```powershell
docs/review/.venv/Scripts/python.exe -m pytest -q tests/ --basetemp=docs/review/next-rerun-temp -o cache_dir=docs/review/pytest-cache
docs/review/.venv/Scripts/python.exe scripts/smoke-output.py src/WindowsAIPhotos/bin/x64/Release/net10.0-windows10.0.19041.0/engine --state-dir docs/review
docs/review/.venv/Scripts/python.exe docs/review/check_swift_syntax.py
```

新增 queue XCTest 位于 `ios/PhotosCompanion/QueueTests/AssetSyncQueueTests.swift`，覆盖优先级、重试/复核预留、接收库隔离和持久化重开。CI 已添加未来执行入口，**本轮不运行云端 CI**。

## PR #1 / PR #2 / PR #3 建议与下一步

继续保持 PR #3 为 Draft，堆叠顺序 PR #1 → PR #2 → PR #3，不自动合并。请 Claude 重点复核稳定队列的状态/取消/公平性、PhotoKit 通知与权限变化、独立接收端的启动停止竞态、DPAPI 与 pipe 边界、停止时写入/SQLite 一致性。

验收优先级：

1. **P1**：Mac `swift test` + Xcode Simulator 构建，再用签名 iPhone 完成队列变化、PhotoKit/iCloud、BG 过期/挂起/强关验证。
2. **P1**：真实 Tailscale 上传 HEIC + Live Photo MOV + ProRAW + 大视频，网络切换、Windows 重启后重新启用并续传；完整本地恢复逐文件校验。
3. **P2**：50k/100k 真实照片、实际 AI 推理、长期运行/磁盘耗尽、跨设备总配额、E2EE/云恢复、分发签名与安装包验证。

本轮没有新确认的 P0，也不把剩余 P1/P2 的外部验收标成通过。原始 P3 与发布门槛继续参考历史完整审查。完成本次推送与交接后等待进一步审核，不自动合并或发布。
