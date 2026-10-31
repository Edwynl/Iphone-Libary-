# 修复与本地验证报告

时间：2026-10-30 17:00 AEDT（Australia/Sydney）  
工作区：`C:\Antigravity\Claude Project\Windows AI Photos`  
分支：`feat/iphone-native-resumable-sync`；基线 HEAD：`c6408c1d2220241e555aa450839b553998ea1bd2`。修复保留为未提交更改。

用户已批准修复并要求继续。本轮处理原审查的全部 26 项代码/文档问题；其中 **15 项完成本地验证，11 项仍含外部验收门槛**。这不是“26 项已全部关闭”或商业发布批准。原 `FULL_CODE_REVIEW.md` 与历史缺陷证据保持为审查基线；旧的缺陷复现脚本不用于修复后的通过判定。

## 本地结果

- 全套 pytest：**60 passed / 1 skipped / 0 failed**。跳过项仍是 Windows 普通符号链接权限不足；真实 junction 用例已执行通过。新增回归组20项通过。
- Windows x64 Release Build：**0 warnings / 0 errors**，SDK 10.0.403；使用 docs/review 内独立 SDK，无全局 SDK 或 RG 项目修改。
- 实际 bin 输出 worker：ping、隔离空库、三个网页资源 HTTP 200 全部通过。
- 本机 WinUI：真实启动，打开同步管理对话框，点击暂停观察 Resume，点击恢复观察 Pause，确认 SQLite paused=false，正常关闭应用。没有启用公网/手机服务、添加真实照片或操作真实媒体库。
- 大文件：**4,294,967,313 字节 / 513 块**，2 GiB 处重启本地接收服务，最终 SHA-256 `e6c2056619e0dfe0fc5cb5ff42fd4ee077387e923c95d7bda88329cdf497704e`，37.24 秒。合成 MOV 字节、loopback HTTP，不是有效 MOV 编码或真实 iPhone/Tailscale 测试。
- Swift：10 个文件通过 tree-sitter 语法解析。**不是 Swift 编译、类型检查、SDK API 验证或 Xcode 测试**。
- Python 严重语法/名称类静态检查通过；依赖锁生成，许可证元数据记录了31个 Python包和17个 NuGet包，见 `docs/DEPENDENCIES.md`。

## 数据保护结果

P0 的实际 NTFS junction 条件已转成回归测试，验证源文件 SHA 与 mtime 不变。另验证目录不能在写入中被替换；测试先证明 FSCTL_SET_REPARSE_POINT 对普通空目录可成功，再证明写入租约使其返回 ERROR_DIR_NOT_EMPTY。该设计依据 [Microsoft 对非空目录重解析操作的约束](https://learn.microsoft.com/en-sg/windows-hardware/drivers/ifs/fsctl-set-reparse-point)。这是明确场景的验证，不是所有文件系统/驱动/恶意内核权限的安全证明。

完整性恢复不覆盖可疑旧导入：缺失/损坏记录失效，重传写到新的已验证路径。原片、可疑旧文件及完整导入不会被“取消 staging”删除。普通相同格式/摘要的正常导入共享一份物理文件，逻辑原名与资产关联仍在清单。

本地备份/恢复测试涵盖 HEIC、Live Photo paired MOV 和 alternatePhoto/DNG；本地副本附 manifest，恢复到独立空目录时验证摘要且不覆盖现有文件。恢复得到资源文件与关联清单，**不会自动重建 Apple Photos 相册/编辑历史**。S3 仅用 mock 验证清单与校验参数，没有向真实云服务上传照片，也没有实现已验证的云端灾难恢复。

## 26项逐项状态

### P0-01 — 本地验证通过

- 原问题：本地备份可经目标目录 junction 覆盖来源原片。
- 位置：[engine/ai_photos/file_safety.py:101](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/file_safety.py:101>)。
- 修复：拒绝输出重解析路径；固定 Windows 祖先目录，叶目录使用不可删除租约，POSIX 使用目录 fd；输出原子发布。
- 验证：实际 junction 拒绝、原片 SHA/mtime 不变；目录替换被拒；FSCTL_SET_REPARSE_POINT 在空目录可成功，持租约时返回 ERROR_DIR_NOT_EMPTY。

### P1-01 — 本地验证通过

- 原问题：WinUI C# 编译被错误 FontWeights 命名空间阻断。
- 位置：[src/WindowsAIPhotos/SyncManagement.cs:48](<C:/Antigravity/Claude Project/Windows AI Photos/src/WindowsAIPhotos/SyncManagement.cs:48>)。
- 修复：FontWeights 改用 Microsoft.UI.Text。
- 验证：Release Build：0 warnings / 0 errors；原 WMC 连带错误消失。

### P1-02 — 本地验证通过

- 原问题：Windows 输出遗漏 iPhone 网页所需静态资源。
- 位置：[src/WindowsAIPhotos/WindowsAIPhotos.csproj:20](<C:/Antigravity/Claude Project/Windows AI Photos/src/WindowsAIPhotos/WindowsAIPhotos.csproj:20>)。
- 修复：mobile_web 纳入实际编译输出。
- 验证：从 bin 输出启动 worker；ping/status 正常，首页/JS/CSS 均 HTTP 200。

### P1-03 — 实现已改，外部验收未完成

- 原问题：iPhone 完成检查点不与接收端及实际备份状态核对。
- 位置：[ios/PhotosCompanion/Sources/SyncState.swift:150](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/SyncState.swift:150>)。
- 修复：检查点绑定接收端 ID、逻辑资源和版本，跳过前调用服务端完整性 verify；老 sent 不再被直接信任。
- 验证：接收端 ID 持久/新库不同的服务器测试通过；Swift 语法通过；Xcode/设备对账补传 Not Verified。

### P1-04 — 本地验证通过

- 原问题：已完成传输会继续确认丢失或损坏的文件。
- 位置：[engine/ai_photos/sync.py:82](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/sync.py:82>)。
- 修复：begin/complete/verify 核验实际文件大小与 SHA；损坏/丢失返回未完成。补传恢复到新文件，保留可疑旧文件。
- 验证：损坏、缺失、再次补传、旧文件保留均有回归测试。

### P1-05 — 实现已改，外部验收未完成

- 原问题：PhotoKit alternatePhoto 被丢弃，可漏备 RAW/ProRAW。
- 位置：[ios/PhotosCompanion/Sources/PhotoSource.swift:105](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/PhotoSource.swift:105>)。
- 修复：PhotoKit 纳入 alternatePhoto，服务器协议接受该资源类型。
- 验证：DNG/alternatePhoto 服务器及备份回归通过；真实 RAW+JPEG/ProRAW 与 Xcode Not Verified。

### P1-06 — 实现已改，外部验收未完成

- 原问题：关闭自动备份后，已排队后台任务仍可上传。
- 位置：[ios/PhotosCompanion/Sources/BackgroundScheduler.swift:19](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/BackgroundScheduler.swift:19>)。
- 修复：关闭 automatic 时取消排队请求及活动自动任务；后台入口重新核验开关与配对。
- 验证：Swift 语法通过；真实关闭开关/后台唤醒行为 Not Verified。

### P1-07 — 实现已改，外部验收未完成

- 原问题：后台过期取消不能结束导出/上传等待，后续调度可能中断。
- 位置：[ios/PhotosCompanion/Sources/PhotoSource.swift:62](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/PhotoSource.swift:62>)。
- 修复：取消传播到 PhotoKit requestData ID、URLSession 等待者及网络探测；BG 完成回调只执行一次，提前排下一机会。
- 验证：Swift 语法通过；后台过期/锁屏/iCloud/挂起恢复 Not Verified。

### P1-08 — 实现已改，外部验收未完成

- 原问题：单个不可处理资源会持续阻塞后续全部备份。
- 位置：[ios/PhotosCompanion/Sources/SyncState.swift:177](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/SyncState.swift:177>)。
- 修复：按稳定资源键保存失败，单项失败继续其他资产；网络/鉴权/暂停等全局错误中断；失败项在后续遍历重试。
- 验证：代码与语法检查；真实失败 PhotoKit 资源、权限变更和恢复队列 Not Verified。

### P1-09 — 本地验证通过

- 原问题：二次备份遗漏 MOV/DNG，且不备份 Live Photo 关联清单。
- 位置：[engine/ai_photos/backup.py:14](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/backup.py:14>)。
- 修复：合并照片索引和全部已完成 native 资源，备份本地/S3 manifest；保留原名、Live Photo 关联、摘要和时间。
- 验证：HEIC+MOV+DNG 三资源本地备份/恢复哈希通过；mock S3 清单与 SHA256 参数通过；未调用真实云端。

### P1-10 — 本地验证通过

- 原问题：接收暂存与图片目录跨卷时无法完成传输。
- 位置：[engine/ai_photos/sync.py:374](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/sync.py:374>)。
- 修复：在目标卷创建并验证临时副本，再同目录原子发布；DB 提交后才清理 staging。
- 验证：禁止跨目录 rename 的 EXDEV 故障注入通过；>4GiB 服务重启续传通过。物理双卷/断电仍未验。

### P1-11 — 本地验证通过

- 原问题：备份静默忽略已修改、丢失或不可访问的索引文件。
- 位置：[engine/ai_photos/backup.py:65](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/backup.py:65>)。
- 修复：丢失、过期、越界、损坏条目显式 issues/complete=false；CLI 非零，GUI 不再显示无条件成功。
- 验证：修改原片但未重新扫描时备份无写入，返回未完成；源缺口不会静默省略。

### P2-01 — 本地验证通过

- 原问题：扫描与上传争用 SQLite 写锁，错误未按协议返回。
- 位置：[engine/ai_photos/catalog.py:241](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/catalog.py:241>)。
- 修复：缩短 SQLite 写事务，解码/推理前释放 writer；存储忙返回可重试 JSON 503。
- 验证：阻塞推理期间另一 writer 成功；HTTP 锁异常返回 JSON 503，无断连。

### P2-02 — 本地验证通过

- 原问题：Python 3.11 的扫描防链接逻辑漏掉 NTFS junction。
- 位置：[engine/ai_photos/catalog.py:141](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/catalog.py:141>)。
- 修复：扫描拒绝 NTFS reparse/junction，并核验子目录最终范围。
- 验证：实际 NTFS junction 外部图片不被扫描。

### P2-03 — 本地验证通过

- 原问题：相似照片预筛选漏掉阈值 4/5 内的哈希对。
- 位置：[engine/ai_photos/duplicates.py:120](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/duplicates.py:120>)。
- 修复：相似哈希用阈值+1 个不重叠 band，修复默认距离4的漏召回。
- 验证：四个旧 band 各差1 bit 的构造对可被找出，精确去重原测试仍通过。

### P2-04 — 本地验证通过

- 原问题：安装 HEIC 解码器后不重试既有失败缩略图。
- 位置：[engine/ai_photos/catalog.py:186](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/catalog.py:186>)。
- 修复：显式扫描会重试原先缩略图失败条目。
- 验证：解码器从不可用变可用后，未变化 HEIC 会重新处理并清除错误。

### P2-05 — 本地验证通过

- 原问题：原生导入对同内容不同文件名仍产生多份文件。
- 位置：[engine/ai_photos/sync.py:352](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/sync.py:352>)。
- 修复：相同格式字节以摘要寻址；多个逻辑资源保留独立关联/原名。
- 验证：同字节不同原名/资产只产生一份正常资源文件，仍保留两个完成逻辑任务。

### P2-06 — 实现已改，外部验收未完成

- 原问题：崩溃遗留导出/分块文件与未完成任务缺乏维护机制。
- 位置：[ios/PhotosCompanion/Sources/BackgroundChunkUploader.swift:137](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/BackgroundChunkUploader.swift:137>)。
- 修复：后台 taskDescription 持久关联 chunk 文件；旧任务结束后清理孤块/导出；检查点每逻辑资源保留当前版本；Windows 提供不完整 staging 显式取消及磁盘保留空间。
- 验证：Windows 取消 staging 和拒绝删除完整原片通过；iOS 崩溃重启/低存储清理及 SQLite 生命周期 Not Verified。

### P2-07 — 实现已改，外部验收未完成

- 原问题：配对凭证未绑定服务器地址，且没有实施文档中的 ts.net 限制。
- 位置：[ios/PhotosCompanion/Sources/SyncAPI.swift:84](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/SyncAPI.swift:84>)。
- 修复：只接受私有 HTTPS ts.net/443 origin，拒绝重定向；凭证绑定 origin/receiver，地址变化断开配对。
- 验证：代码与语法检查；真实 Tailscale/iPhone 配对、改地址、TLS 与重定向实测 Not Verified。

### P2-08 — 实现已改，外部验收未完成

- 原问题：Wi-Fi only 不是贯穿整个备份过程的明确蜂窝网络策略。
- 位置：[ios/PhotosCompanion/Sources/PhotoSource.swift:145](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/PhotoSource.swift:145>)。
- 修复：上传显式禁止蜂窝/昂贵/受限网络；严格 Wi-Fi 模式不自动取 iCloud-only 原片，UI 解释原因；策略变化取消当前任务。
- 验证：策略为保守失败，不声称 PhotoKit 具备蜂窝开关；真实切网与 iCloud 行为 Not Verified。

### P2-09 — 实现已改，外部验收未完成

- 原问题：照片无 EXIF 或视频的原始拍摄时间未被同步清单保留。
- 位置：[ios/PhotosCompanion/Sources/SyncAPI.swift:25](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/SyncAPI.swift:25>)。
- 修复：协议、DB、manifest 保存 PHAsset 时间；索引无 EXIF 时回填拍摄时间，WinUI 优先显示拍摄时间。
- 验证：PNG 接收时间/原名回归通过，C# 编译通过；真实 EXIF/QuickTime/时区/iPhone Not Verified。

### P2-10 — 本地验证通过

- 原问题：同步语义查询堵塞 worker 请求，桥接没有超时/取消。
- 位置：[engine/ai_photos/worker.py:89](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/worker.py:89>)。
- 修复：语义搜索使用有界独立槽；worker 管理请求保持可处理，C# 等待支持超时/取消并清理 pending。
- 验证：阻塞语义查询时 ping 可先返回；Windows 编译通过。超时取消的是等待，不承诺已经终止 Python 推理。

### P2-11 — 实现已改，外部验收未完成

- 原问题：AI 工件未固定不可变版本，installed 不等于可用。
- 位置：[engine/ai_photos/semantic.py:84](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/semantic.py:84>)。
- 修复：固定 HF revision、模型 SHA 清单和依赖 hashes；加载失败不再报告 ready，保留显式修复入口；提供依赖元数据清单。
- 验证：损坏/未验证/加载失败模型状态单测通过；依赖锁已解析，31 Python/17 NuGet 元数据已记录；真实模型/完整再分发验收未完成。

### P3-01 — 本地验证通过

- 原问题：同步管理界面主要依赖手动刷新。
- 位置：[src/WindowsAIPhotos/SyncManagement.cs:202](<C:/Antigravity/Claude Project/Windows AI Photos/src/WindowsAIPhotos/SyncManagement.cs:202>)。
- 修复：同步窗口每3秒刷新，禁止刷新请求重入，确认操作期间保留行；窗口关闭停止 timer。
- 验证：本机隔离 WinUI 启动、窗口打开、暂停/恢复往返 worker、正常退出已实测；真实设备海量任务 UI 未验。

### P3-02 — 本地验证通过

- 原问题：架构文档仍称没有 HTTP 服务。
- 位置：[docs/ARCHITECTURE.md:25](<C:/Antigravity/Claude Project/Windows AI Photos/docs/ARCHITECTURE.md:25>)。
- 修复：更新架构、双语 README、恢复/格式/网络能力边界及 release gates。
- 验证：原 No HTTP server 断言已修正，文档区分传输/索引/副本/验证状态。

### P3-03 — 实现已改，外部验收未完成

- 原问题：CI 未覆盖 iOS 和实际输出目录的跨端冒烟。
- 位置：[.github/workflows/ci.yml:39](<C:/Antigravity/Claude Project/Windows AI Photos/.github/workflows/ci.yml:39>)。
- 修复：CI 补真实输出 worker/web 冒烟和 macOS 无签名 iOS simulator build。
- 验证：仅修改配置，未推送、触发或重跑 GitHub Actions；Mac job 执行结果 Not Verified。

## 剩余验收与约束

1. **Mac/Xcode 与真实 iPhone 必须补验**：构建、签名、PhotoKit 资源组合、ProRAW、iCloud、权限收缩、锁屏/挂起/强关/过期、低存储、网络切换、后台 daemon 重连、接收端重建与补传。修复后的 Swift 源码已准备，但不能宣称其功能通过。
2. **严格 Wi-Fi 模式的保守边界**：PhotoKit 无逐请求蜂窝禁用开关，默认只读取本地原片。iCloud-only 原片需先在 Photos 下载，或用户明确关闭限制。没有把昂贵网络策略冒充绝对 Wi-Fi 保证。
3. **真实 AI 与规模**：没有下载真实模型权重，未验证 pinned 模型实际推理、CPU/GPU、中文/英文质量、50k–100k真实媒体。原合成性能数据仍只是 SQLite/NumPy 查询数据。模型 hash 清单/依赖锁不是质量或完整再分发许可批准。
4. **存储/云恢复/分发**：物理跨卷、断电、磁盘故障、真实 S3兼容商、不可变保留策略、云恢复、签名安装/升级、TestFlight 与完整 notices/SBOM 分发仍待验。PC finalize 可能暂时同时占用 staging 和目标副本空间；iPhone 也需容纳整份导出及块文件。
5. **配对根权限**：服务主配对码仍在会话内可复用，持码者可重新配对；文档已明确其根权限，短时/一次性配对窗口仍属发布前加固。设备 bearer 在每次变更前重新核验撤销状态；不要将该开发服务公开到互联网。
6. **取消语义**：C# 传输层超时/取消终止等待，不代表 Python 推理已被强制终止；iOS 单项错误会保留为未解决项并在后续遍历重试。用户删除/移出权限范围的失败资产可能仍需人工处理，不能静默宣称已经备份。

## 合并与发布建议

保留当前工作区供审查。本轮未 commit、push、创建/修改/合并 PR，未运行 GitHub Actions，未部署发布。未操作 VELVET/PWA 或 8765/8766 服务。审查报告、工具链和合成数据均留在 docs/review；大数据与工具目录已通过该目录 .gitignore 排除，勿将测试数据或 SDK 作为提交内容。

当前 Windows/Python 本地门槛已通过；iOS及其他外部门槛完成前，PR #2 仍不建议作为已验证的自动备份产品合并/发布。PR #1 的本地安全/构建修复可以作为独立审查范围，但本轮没有切换或重写 PR 分支。

证据：`fix-pytest.log/xml`、`fix-regressions.log`、`fix-windows-build.log`、`fix-output-smoke.log`、`fix-gui-smoke.json`、`fix-large-transfer-result.json`、`fix-swift-syntax.json`、`fix-static.log`、`dependency-inventory.json`。复现入口：`tests/test_review_regressions.py`、`scripts/smoke-output.py` 和 `docs/review/fix_large_transfer_probe.py`。
