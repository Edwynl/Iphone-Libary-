# Windows AI Photos — Full Code Review

审查日期：2026-10-30 16:21 AEDT（Australia/Sydney）  
仓库：https://github.com/Edwynl/Iphone-Libary-  
目录：`C:\Antigravity\Claude Project\Windows AI Photos`  
分支：`feat/iphone-native-resumable-sync`  
审查 HEAD：`c6408c1d2220241e555aa450839b553998ea1bd2`

本报告按本轮完整 A–I 要求审查 PR #2 分支，覆盖前两阶段全部源码。优先级按用户定义：P0 数据丢失/严重安全/核心架构不可用；P1 编译、后台/续传/完整性阻断；P2 性能/兼容/稳定/资源；P3 UI/结构/文档/维护。

## 1. Executive Summary

**结论：当前不建议合并或发布 PR #1 / PR #2；先处理可覆盖原片的 P0、Windows 编译阻断和备份完整性问题。**

共 26 项：**P0=1，P1=11，P2=11，P3=3**。每项详见第 9 节，含文件/行号、原因、影响、建议与验证边界。

- 已直接 Clone 到原先为空的指定目录，没有嵌套文件夹；随后 Fetch，HEAD 与远端目标分支及预期提交一致。
- PR #1 的提交 `7098a6a8caf11ec4186b37851c05ca4c4af6c86f` 为 HEAD 祖先；PR #2 的 `2c3d3c8ac8a1e9beb947605e08fe5dffe7a1f0c3` 同样为祖先。两次 merge-base 检查退出码均为 0。[PR #1](https://github.com/Edwynl/Iphone-Libary-/pull/1)、[PR #2](https://github.com/Edwynl/Iphone-Libary-/pull/2) 只读查询均为 OPEN，不是已合并。
- 项目原有 pytest：**40 passed / 1 skipped**。补充的 12 项观察性检查全部达到预期；其中包括缺陷复现、反证及安全正常路径，并非“产品全部通过”。
- .NET 10 Restore 成功；真实 Windows Build 失败，首要明确根因是 FontWeights 命名空间；无法进行桌面 GUI 验收。
- **P0 已真实复现**：带有特定预置 junction 的备份目标可使 local backup 覆盖来源原片。使用的都是 docs/review 中生成的测试图片，未操作用户真实照片。
- **4 GiB + 17 bytes** 的合成资源通过 513 个 HTTP 分块传输，在 2 GiB 处重启接收服务后续传，最终摘要一致。这证明本机服务器路径的一项能力，不证明 iPhone/Tailscale/视频编解码通过。
- iOS 仅静态审核：Xcode 编译、签名、PhotoKit 真实资源、后台、iCloud、锁屏、强制关闭、Wi-Fi 切换全部 **Not Verified**。

本轮未修改既有跟踪文件、业务代码或 CI，未提交、推送、创建/合并 PR、触发 Actions；只生成审查文件、隔离工具链、测试数据与常规忽略的构建产物。未下载真实 AI 模型、未上传任何照片到云端，未改变 Tailscale Serve/Funnel/ACL。没有操作 VELVET 或 8765/8766 正式服务。

## 2. Architecture Assessment

| 组件 | 现状 | 审查结论 |
|---|---|---|
| WinUI 3 / C# | 持久 Python JSONL 子进程，GridView 分页，同步管理对话框 | 开发架构可用；当前编译失败；worker 的同步搜索和桥接无限等待削弱故障隔离 |
| Python AI / SQLite | Pillow、可选 pillow-heif、NumPy、可选 SigLIP2，WAL + 分线程连接 | 普通索引有测试；推理应移出写事务；真实模型/API/质量/性能未验证 |
| 原生 iPhone | SwiftUI、PhotoKit 原资源导出、本地 SQLite sent、Keychain token | 原片传输方向正确；资源覆盖、接收端核对、后台取消/恢复缺口明显 |
| 同步协议 | 8 MiB、块/整文件 SHA-256、fsync 后记录 offset、设备隔离 | 小文件与 >4 GiB 本机传输有证据；完成状态、跨卷和并发错误仍有缺陷 |
| 资源与索引 | photos 用于可索引照片，sync_transfers 保存资源关联 | 未统一所有原片，导致视频/RAW 二次备份及恢复缺失 |
| 本地 / S3 备份 | 显式 apply，本地校验临时副本后 replace；S3 upload_file | 本地 junction 有原片覆盖风险；无整库恢复流程或可验证 manifest |
| 分发与商业发布 | 未签名开发项目，需 Python；iOS XcodeGen 源码 | 尚不满足独立安装、升级、签名、SBOM/许可归档、灾难恢复门槛 |

50k/100k 合成索引结果支持当前精确向量查询的开发可行性，但不证明真实扫描/解码/AI 推理吞吐、中文召回、低配机、HDD/SMB 或长时间稳定性。当前不必仅凭规模数字改成微服务；优先分离资源清单、短事务、可恢复任务和明确状态模型。

发布缺口（不等同于新增缺陷）：无独立打包 Python/模型运行时；未验证干净 Windows 安装与升级；无签名 MSIX/EXE；未验证 iOS Development Team、设备签名、TestFlight；项目没有完整第三方 notices/SBOM/依赖锁定。模型卡标记 Apache-2.0，Windows SDK 包要求适用的再分发许可，不能把单一模型许可当作整个产品的许可结论。参见第 11 节资料。

## 3. Windows Build Results

环境：Windows x64，OS version 10.0.26300，Python 3.11.14。当前 PATH 原指向 `C:/Users/edwyn/.dotnet` 的 SDK 8.0.425；Program Files 检测到的是 .NET 6 runtime，不是 .NET 10 SDK。RG HVAC Copilot 的现有 global.json 同样指定 8.0.425，交接资料记录了该 SDK 安装。已搜索用户/Antigravity 常见项目与安装路径，没有找到可复用的现有 10 SDK。

为完成审核，在 **docs/review/.dotnet** 独立安装官方 .NET **10.0.403**，不改变全局 PATH/现有 RG 项目。仅构建进程设置 DOTNET_ROOT、DOTNET_CLI_HOME、NUGET_PACKAGES。工具首次运行自动输出开发证书创建提示；未执行 trust 命令或修改项目 HTTPS 配置。

| 动作 | 结果 | 证据 |
|---|---|---|
| 原有 SDK 8 Build | NETSDK1045，不支持 net10 | windows-build-sdk8.log |
| SDK 10 Restore / x64 | 成功；WindowsAppSDK 2.6.4 正常解析 | windows-restore-sdk10.log、dotnet-packages.txt |
| SDK 10 Release Build --no-restore | **失败**：2×CS0234，1×WMC9999，1 warning | windows-build-sdk10.log |
| MSBuild Content 项检查 | 12 个 Python 文件，0 个 mobile_web 资源 | packaging-result.json |
| WinUI 启动、FolderPicker、同步管理操作 | **Not Verified**，构建阻断 | 不以静态处理器存在认定 UI 可用 |

官方 NuGet 当前确有 2.6.4，不应误报版本不存在。WMC9999 可能是上游编译错误的连带故障，也可能仍需单独处理；本轮没有修改源码试修，故保留待复查。

可复用的本地构建命令：

```powershell
$env:DOTNET_ROOT = "$PWD\docs\review\.dotnet"
$env:DOTNET_CLI_HOME = "$PWD\docs\review\dotnet-home"
$env:NUGET_PACKAGES = "$PWD\docs\review\nuget-packages"
& docs/review/.dotnet/dotnet.exe restore src/WindowsAIPhotos/WindowsAIPhotos.csproj -p:Platform=x64
& docs/review/.dotnet/dotnet.exe build src/WindowsAIPhotos/WindowsAIPhotos.csproj -p:Platform=x64 -c Release --no-restore
```

## 4. iOS Code Review

**本节所有 iOS 编译和运行时结论均为 Not Verified；这里只记录静态/API 核对。** 本机没有 Swift/Xcode/xcodebuild，未伪造模拟器、签名或真实 iPhone 通过记录。

| 检查项 | 静态观察 | 验证边界 |
|---|---|---|
| SwiftUI / Photos 权限 | 使用 readWrite，接受 authorized/limited，拒绝 denied；Limited 文案存在 | 权限收缩、运行中撤销、重新选择有限图库未实测 |
| HEIC / HEIF 原片 | writeData 导出，不在客户端重新编码，增量 SHA | 真实解码、原图字节/EXIF/方向比对未实测 |
| ProRAW | 可传 DNG，但忽略 alternatePhoto | P1-05；synthetic DNG 不是 PhotoKit ProRAW 测试 |
| Live Photos | photo 与 pairedVideo 共享 localIdentifier，服务器关联在 DB | 实际成对资源、内容标识符与还原未实测；不保存编辑重建全语义 |
| iCloud 原片 | isNetworkAccessAllowed=true，等待 writeData 回调 | 下载进度、低存储、失联/取消与蜂窝策略未实测 |
| 后台 URLSession | 文件支持的单个分块，固定 session identifier，有 app-delegate reconnect | 不是整条上传流水线自动持久化；重启映射/清理和取消有缺口 |
| BGProcessingTask | 启动注册、网络要求、约 30min earliestBeginDate | 只是最早机会，不是周期保证；P1-06/07 |
| 断点续传 | begin offset，409 查询，循环读 8 MiB | 无跨进程客户端续传实测；需回归旧 daemon task 与新 begin 的并发 |
| 强制关闭 | 文档正确承认自动重启受系统限制 | force-quit/重新打开/锁屏后行为全部 Not Verified |
| 本地检查点 | SQLite 只在 complete 响应后写 sent | P1-03；不能证明 Windows 备份仍存在 |
| XcodeGen / 签名 | iOS 17、Swift 5.9、BG identifiers 与配置存在 | 不代表 Xcode 能编译、资源/Info 合成或签名分发通过 |

需在 Mac + 真实 iPhone 补：构建、签名；原始 HEIC/Live Photo/RAW+JPEG/ProRAW/大型 MOV；iCloud-only；运行中权限变化；锁屏/挂起/强关/过期；断网/切网；低存储；撤销/重新配对；更换接收端及备份丢失后补传。

## 5. Sync Protocol Reliability

正向证据：原有测试覆盖块摘要错误、offset 冲突、未完成不能 complete、跨设备拒绝、撤销、暂停、同一 begin 幂等、8 MiB 块、损坏暂存重置、服务重启、缺失暂存和缺失最终文件后 begin 恢复。R09 并发提交同一块得到 200/409，随后完整校验通过；未观察到同一 manager 内重复写入导致字节损坏。

大资源专项：4,294,967,313 字节，513 块；2,147,483,648 offset 关闭并重新创建本地服务，设备凭证和 offset 持续有效；最终 SHA-256 `e6c2056619e0dfe0fc5cb5ff42fd4ee077387e923c95d7bda88329cdf497704e`，总时长 31.45 秒。生成数据放在审查目录，扩展名 MOV 仅用于协议接收，**并非有效 MOV 编码样本**。

尚存问题：P1-03/04 的完成核对、P1-10 跨卷 finalize、P2-01 SQLite 并发、P2-06 临时文件。finalize 的文件 rename 与 SQLite commit 不是同一事务：进程在二者之间终止后，现有 begin 会重建传输，通常可在目标存在且摘要一致时避免覆盖，但可能要求整文件重新上传；未做操作系统掉电/物理硬盘故障验证。只有 fsync/SQLite WAL 和单次服务重启证据，不能宣称所有崩溃窗口已验证。

服务器 complete 在全文件 SHA 和磁盘操作期间持有全局 manager 锁；极大文件会阻塞其他写入/暂停操作，需后续真实负载测量。当前仅支持单进程内锁，未验证同一 DB 被多个接收进程共享运行；不得将其部署为多实例服务而不补一致性机制。

## 6. Security Findings

已核对的防护：默认服务关闭；MobileServer 明确拒绝非 loopback；强随机主码及设备 token；Windows 设备 token 存摘要，iPhone Keychain 使用 AfterFirstUnlockThisDeviceOnly；独立设备鉴权、跨设备 job 拒绝、撤销检查；字段参数化；文件名拒绝路径分量并限制扩展名；块大小/声明大小/摘要验证；网页 CSP、nosniff、no-store 等头。保留这些设计，不把开发服务描述成对公网安全。

本机 Tailscale 已安装、BackendState=Running。只读取状态并记录脱敏摘要，未改 Serve/Funnel/ACL，也未连接 iPhone。官方 Tailscale Serve 文档支持 tailnet 内 HTTPS 代理方案；本轮 **没有完成真实 tailnet 手机链路验证**。8787 开发服务也没有被持久开启；测试全部 port=0。

需处理：P0-01 本地备份写路径安全、P2-02 扫描 junction、P1-06 关闭自动后的意图执行、P2-07 凭证与接收端绑定、P2-08 网络策略。另有发布前加固：主配对码在服务会话内可重复使用且无过期/限流；知道主码可重新配对被撤销设备。应将其视为长期根配对权限，而不是文档中暗示的一次性码，提供短时/一次性配对窗口。缺少磁盘字节配额、慢请求超时、完整审计日志；当前 personal-tailnet 范围降低暴露面，但不等于无需加固。

照片外传核对：普通扫描/检索本地处理；真实模型只在显式 install 下载；S3 apply=False 不建客户端，apply=True 是用户明确上传。未发现自动默认向第三方推理上传照片的路径。iCloud 原片下载是 PhotoKit 授权读取的一部分，仍需按网络策略验证。云端 S3 **只有 TLS 与提供商侧能力，不是端到端客户端加密**，这一限制在文档已有披露。

## 7. Data Integrity Risks

| 范围 | 结论 |
|---|---|
| 原片只读承诺 | 普通 scan/duplicates 有正向测试；本地备份的预置 junction 条件下承诺被打破（P0-01） |
| 字节完整性 | 协议 SHA/fsync/offset 有测试和大文件证据；完成后损坏/丢失审计不足（P1-03/04） |
| 元数据完整性 | 未重编码支持资源内字节保留；PHAsset 外部时间/相册/编辑语义未完整保存（P2-09） |
| 去重 | SHA 精确检测非删除；相似分组有漏召回（P2-03）；导入重复存储（P2-05） |
| 离线硬盘 | 原有扫描离线根测试通过，不删其索引；备份却静默省略离线/过期项（P1-11） |
| Live Photo | 配对关联只在 Windows DB；二次副本未保存 MOV/manifest（P1-09） |
| 本地副本 | 临时复制后源/副本摘要校验；不具备不可变版本、完整性清单或整库恢复 |
| S3 副本 | upload_file 失败可传播；自定义 sha256 Metadata 不是独立服务端 SHA-256 验证证据，无 HEAD/回读/恢复验证 |
| 恢复 | **没有实现完整 restore 命令/GUI/恢复演练**；现有备份不能称为可验证灾难恢复 |

S3 SDK/服务可能执行默认传输校验，不能笼统断言“完全无校验”；但不同 S3 兼容商、分段上传和校验语义未实测。应显式约定服务端 checksum 与不可变 manifest，并做下载后摘要/关联恢复演练。覆盖同 key/同路径并无内建保留历史版本；独立存储、版本保留、服务器端加密策略及可选客户端 E2EE 都是发布设计待办。本轮没有创建 bucket、调用上传或进行真实云恢复。

## 8. Test Results

| 本地检查 | 结果 | 证据文件 |
|---|---|---|
| 全部原有 pytest | 40 passed / 1 skipped，17.45s | pytest.log、pytest.xml |
| skip 原因核对 | Windows 无符号链接创建权限；非产品功能通过 | pytest-skip-detail.log |
| 12 项观察性补充检查 | 12/12 预期观察成立，含多个缺陷及反证 | reproduction-results.json、reproduce_findings.py |
| NTFS junction 扫描 | 实际遍历到来源外生成图片 | junction-result.json |
| NTFS junction 本地备份 | 实际覆盖生成的来源 B 原片 | backup-junction-result.json |
| >4GiB HTTP 续传 | 4,294,967,313 bytes，513 chunks，SHA一致 | large-transfer-result.json、large_transfer_probe.py |
| AI scan / 上传并发 | 复现锁异常和非 JSON 断连 | concurrency-result.json、concurrency_probe.py |
| AST / XAML 名称 | 21 Python 文件解析、16 事件处理器名称存在 | R11；不等于 C#/Swift 编译 |
| Ruff 严重语法/名称类规则 | E9/F63/F7/F82 全通过 | ruff-critical.log |
| Ruff isolated 全规则默认检查 | 37 项，主要导入/未用变量/风格及宽 catch 等，未修复 | ruff-isolated.log；非仓库已配置发布门槛 |
| Windows Restore / Build | Restore成功，Build失败 | windows-restore-sdk10.log、windows-build-sdk10.log |
| iOS / 真实 AI / 真实云恢复 | **Not Verified** | 环境/数据与显式模型安装限制 |

补充检查 R01 反证：新配对但无任务的 SUM CASE ELSE 0 正确返回 0，不是 null，因此没有把它作为 UI 缺陷。R10 反证：CON 文件干最终被添加 hash，不形成保留设备名，且 >100GiB 声明被拒绝。审查保留反证，避免将未经验证的猜测写成问题。

合成扩展性测量（相同 768维归一化向量，真实 SQLite/NumPy 查询；不是照片/模型质量测试）：

| 条目数 | Filename 查询 | Semantic 查询 | 进程峰值工作集 | DB大小 |
|---:|---:|---:|---:|---:|
| 50,000 | 0.031s | 0.531s | 379.62 MiB | 228.08 MiB |
| 100,000 | 0.078s | 1.063s | 724.28 MiB | 456.54 MiB |

这些数值受本机缓存/CPU/SSD及合成数据分布影响。未测量真实 50k–100k 原片扫描、HEIC 解码、GPU/CPU 推理、中英文召回、UI 全量滚动或长时内存泄漏。bench 不应被用作商业性能承诺。

测试数据、SDK 与 NuGet 缓存均保留在 docs/review 的忽略目录内，以便后续批准修复后重用；包含约 4GiB 合成大资源。它们不是提交候选。没有清理用户文件，测试中的改写/删除只针对本轮自己生成的隔离 fixture。

## 9. P0/P1/P2/P3 Issues

以下路径行号以审查 HEAD 为准；“静态确认”不等于实机功能验证。相互关联的问题保留独立入口与不同修复验证目标。

### P0-01 — 本地备份可经目标目录 junction 覆盖来源原片

- **位置**：[engine/ai_photos/backup.py:56](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/backup.py:56>)。
- **原因**：只校验顶层 target 与来源的关系；拼接 root_id/relative 后没有检查目标链的 junction/reparse point，最终 os.replace(tmp, out) 跟随父目录重定向。
- **影响/前提**：前提：备份目录内已有指向来源目录的 junction。同名目标可落到另一来源原片上，造成内容被覆盖；结果仍宣称原片未修改。不是默认干净目标必然发生，也不是已证明的远程任意写入。
- **建议修复**：在计划和实际写入时拒绝目标子路径上的 junction/symlink/reparse point；校验最终解析位置不落入任一来源；采用能抵御路径替换竞态的 Windows 文件句柄方案。先加保护和回归测试，再恢复本地备份。
- **验证结果**：本机真实 NTFS junction + 两张生成 PNG 已复现：来源 B 的 SHA 从 69c0968d… 变为 ef504a6a…，等于来源 A；返回 copied=1。证据 backup-junction-result.json。全部在 docs/review 内，未接触真实照片。

### P1-01 — WinUI C# 编译被错误 FontWeights 命名空间阻断

- **位置**：[src/WindowsAIPhotos/SyncManagement.cs:48](<C:/Antigravity/Claude Project/Windows AI Photos/src/WindowsAIPhotos/SyncManagement.cs:48>)。
- **原因**：使用 Windows.UI.Text.FontWeights；本项目 Windows App SDK 2.6.4 下该类位于 Microsoft.UI.Text。
- **影响/前提**：目标提交无法生成可运行桌面程序，同步管理 UI 和其他桌面交互均不能验收。
- **建议修复**：修正为对应 WinUI API，随后重新完整构建。该轮同时出现 WMC1509/WMC9999，须在修正首个 C# 错误后确认是否仍存在；目前不将 XAML 错误认定为独立根因。
- **验证结果**：真实 Windows x64、.NET 10.0.403、Restore 成功后 Build 失败：2 个 CS0234，另有 1 个 WMC9999，1 个警告。windows-build-sdk10.log。未修源码。

### P1-02 — Windows 输出遗漏 iPhone 网页所需静态资源

- **位置**：[src/WindowsAIPhotos/WindowsAIPhotos.csproj:20](<C:/Antigravity/Claude Project/Windows AI Photos/src/WindowsAIPhotos/WindowsAIPhotos.csproj:20>)。
- **原因**：Content 仅复制 engine/ai_photos/**/*.py；mobile.py:234 读取 mobile_web/index.html、app.js、app.css，但三者未随输出复制。
- **影响/前提**：从编译输出启动的 Python worker 即使服务启动成功，Safari 首页与脚本也会缺失；在仓库源码目录执行的 pytest 无法发现该部署问题。原生同步 JSON API不依赖这些网页资源。
- **建议修复**：把 mobile_web 内容纳入输出及未来发布产物；从实际输出目录启动服务并请求三个资源做冒烟测试。
- **验证结果**：MSBuild 求值 Content 共 12 项，mobile_web 项数为 0：msbuild-content-items.json、packaging-result.json。资源缺失已确认；因编译阻断，GUI 端到端 Not Verified。

### P1-03 — iPhone 完成检查点不与接收端及实际备份状态核对

- **位置**：[ios/PhotosCompanion/Sources/SyncState.swift:113](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/SyncState.swift:113>)。
- **原因**：checkpoint.contains 为真就跳过网络验证；CheckpointStore.swift:17 仅以 resource_key 为主键，没有 Windows 接收端身份、传输 ID、摘要或备份世代。断开/重新配对也不清除或分区这些记录。
- **影响/前提**：Windows 文件丢失、接收端更换或 Windows 数据库重建后，同一 iPhone 可继续显示成功却永不补传已完成资源。服务器 begin 的缺失文件修复逻辑不会被调用。
- **建议修复**：将检查点绑定已验证的接收端身份/备份世代，周期性与服务端清单及实际文件核对；提供完整性审计和重新验证功能。不能把本地 sent 记录当作永久备份证明。
- **验证结果**：静态控制流与 SQLite schema 已确认；服务器缺失文件恢复的原有 pytest 只直接重试 begin，未覆盖 Swift 的跳过分支。iPhone 实机行为 Not Verified。

### P1-04 — 已完成传输会继续确认丢失或损坏的文件

- **位置**：[engine/ai_photos/sync.py:193](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/sync.py:193>)。
- **原因**：begin 对 complete 记录只做 is_file；complete 在第 268 行直接返回成功，连存在性都不查。已有摘要没有用于后续完整性验证。
- **影响/前提**：损坏备份仍被当成安全副本；complete 响应丢失后若文件被外部删除，重试 complete 仍返回成功。
- **建议修复**：统一 complete/begin 的完成验证，校验文件存在、大小和按策略重算摘要；失败进入可补传状态，避免仅凭 DB 状态成功。与 P1-03 的接收端清单核对一起处理。
- **验证结果**：R02：改写已完成生成文件后 begin 仍 complete；R03：删除生成文件后 complete 仍 complete，而 begin 能转 pending。reproduction-results.json。原始来源未改动。

### P1-05 — PhotoKit alternatePhoto 被丢弃，可漏备 RAW/ProRAW

- **位置**：[ios/PhotosCompanion/Sources/PhotoSource.swift:42](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/PhotoSource.swift:42>)。
- **原因**：switch 仅接收 photo/video/pairedVideo 和条件 fullSize 资源；alternatePhoto 直接走 nil。Apple 的 ProRAW 示例明确要求同时检查 photo 和 alternatePhoto 中的 RAW 类型。
- **影响/前提**：RAW 作为 alternatePhoto 的资产会只备主图或完全漏掉 RAW；当前 synthetic DNG 服务端测试不能证明真实 ProRAW 导出完整。并非断言所有 ProRAW 都缺失。
- **建议修复**：按 PHAssetResource 的类型及 UTType 识别全部所需原始资源，给替代原片稳定资源身份并扩展协议枚举；区分原片、调整后版本与编辑元数据。
- **验证结果**：静态及 Apple 官方 API/WWDC 示例核对成立。真实 PhotoKit、RAW+JPEG、ProRAW 资源组合与 Xcode 编译均 Not Verified。

### P1-06 — 关闭自动备份后，已排队后台任务仍可上传

- **位置**：[ios/PhotosCompanion/Sources/BackgroundScheduler.swift:17](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/BackgroundScheduler.swift:17>)。
- **原因**：任务 handler 无 automatic 检查就调用 syncNow；automatic=false 只存设置，没有取消 BGTaskScheduler 请求。syncNow 本身也不知道此调用是否为自动任务。
- **影响/前提**：保持配对时关闭自动备份，先前排队任务仍可能运行并上传照片；当前隐私控制与用户选择不一致。
- **建议修复**：关闭开关时取消对应待执行请求；后台 handler 执行前重新核验 automatic/配对/权限，并定义正在执行任务的停止语义。手动 Sync 仍应允许独立运行。
- **验证结果**：静态路径确认；iOS 调度触发与开关关闭后的设备测试 Not Verified。

### P1-07 — 后台过期取消不能结束导出/上传等待，后续调度可能中断

- **位置**：[ios/PhotosCompanion/Sources/BackgroundScheduler.swift:21](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/BackgroundScheduler.swift:21>)。
- **原因**：expirationHandler 仅 job.cancel；PhotoSource.writeData 与 BackgroundChunkUploader.upload 使用 checked continuation，没有 cancellation handler。上传 waitsForConnectivity；下一次 schedule 只有 syncNow 返回后执行。
- **影响/前提**：过期时正等 iCloud 导出或离线后台上传，Task 取消不能保证 continuation 返回、setTaskCompleted 执行或再次排队。文件后台任务可继续，但扫描链与完成回调可能停滞。
- **建议修复**：任务开始时安排下一次机会；实现有界等待和取消感知，将可继续的 URLSession 任务与扫描 Task 的生命周期解耦，持久化恢复映射并保证 BGTask 完成回调。
- **验证结果**：静态确认缺少取消传播；不声称已复现 iOS 永久挂起。锁屏、过期、挂起、iCloud 弱网和强制关闭测试全部 Not Verified。

### P1-08 — 单个不可处理资源会持续阻塞后续全部备份

- **位置**：[ios/PhotosCompanion/Sources/SyncState.swift:115](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/SyncState.swift:115>)。
- **原因**：每个 export/upload 错误直接退出整个 syncNow；cursor 仅在整项资产成功后增加，没有逐资源失败队列或跳过/重试策略。
- **影响/前提**：最前面的资源损坏、长期无法从 iCloud 下载或格式被服务器拒绝时，后续手动/自动同步反复命中同一项，其他照片一直未备份。
- **建议修复**：持久化逐资源 pending/retry/failed 状态，区分鉴权等全局错误与单项错误；允许其他资产继续，明确展示失败项并提供重试。不能静默跳过并宣称整库完成。
- **验证结果**：静态循环、外层 catch、cursor 写入位置已核对；PhotoKit 故障与真实队列恢复 Not Verified。

### P1-09 — 二次备份遗漏 MOV/DNG，且不备份 Live Photo 关联清单

- **位置**：[engine/ai_photos/backup.py:22](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/backup.py:22>)。
- **原因**：本地/S3 的 _sources 只枚举 photos；sync.py:309 仅将 SUPPORTED_EXTENSIONS 入库，集合不含 MOV/MP4/DNG。关联资产、设备、资源类型与摘要只在 sync_transfers，而备份不包含此表或恢复清单。
- **影响/前提**：iPhone 导入资源显示完成后，再做整库备份，只复制照片部分；Live Photo 配对 MOV、视频、ProRAW 以及关联关系不随独立副本保存。
- **建议修复**：建立统一原始资源清单供索引和备份使用；备份全部已确认资源及可恢复关联 manifest，验证还原后的 Live Photo 双资源和摘要。
- **验证结果**：R04 实际接收 PNG+paired MOV+DNG 共 3 个完成资源；本地实际复制 1 个，S3 dry-run 仅计划 1 个，无关联 DB/manifest。未访问云端。

### P1-10 — 接收暂存与图片目录跨卷时无法完成传输

- **位置**：[engine/ai_photos/sync.py:302](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/sync.py:302>)。
- **原因**：sync-staging 位于 catalog.home，最终目录位于 Pictures/指定 inbox；直接 os.replace，无跨文件系统回退。
- **影响/前提**：当 Pictures 重定向到其他盘、网络位置或 AI_PHOTOS_HOME 位于另一卷，整文件上传和校验后仍失败，重复尝试不能完成。
- **建议修复**：在目标卷暂存，或采用目标卷临时文件复制、fsync、重新校验摘要再原子替换；DB 提交应能在崩溃后协调恢复。
- **验证结果**：R05 注入标准 EXDEV，异常传播且传输保持 pending/full offset。此项是故障注入验证，不是物理双卷实测；Python os.replace 文档说明跨文件系统可能失败。

### P1-11 — 备份静默忽略已修改、丢失或不可访问的索引文件

- **位置**：[engine/ai_photos/backup.py:35](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/backup.py:35>)。
- **原因**：_sources 对 size/mtime 不匹配、文件消失或 OSError 使用 continue；返回结构不含 stale/missing/error 数。
- **影响/前提**：源文件尚未重新扫描或硬盘离线时，可出现索引有照片但备份 files=0、UI 显示 Backup finished，无法知道备份缺口。
- **建议修复**：以完整清单生成计划，对每个条目记录 copied/verified/stale/missing/offline/failed；过期项应重新核对或要求刷新，整库完整性有缺口时不能给出无条件成功。
- **验证结果**：R12 修改一张生成照片但不扫描，catalog=1、backup files=0/copied=0，结果没有任何 stale/error 字段。

### P2-01 — 扫描与上传争用 SQLite 写锁，错误未按协议返回

- **位置**：[engine/ai_photos/catalog.py:235](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/catalog.py:235>)。
- **原因**：扫描先 _upsert 取得写事务，再在同一事务内执行可能很慢的 AI 推理；上传在 sync.py:259 UPDATE 等待锁。mobile.py:218 未捕获 sqlite3.OperationalError。
- **影响/前提**：AI 冷启动或慢推理可使上传等待超过 15 秒后断开 HTTP，Swift 本次同步退出。文件字节已落盘而 offset 未提交；重试仍可安全恢复，但体验与 JSON 错误契约失效。
- **建议修复**：把解码/推理移到写事务外，缩短事务；为存储忙返回明确可重试 JSON/状态，客户端有退避恢复。
- **验证结果**：真实并发 HTTP + 阻塞 fake encoder：16.66 秒后 RemoteDisconnected，日志 database is locked，stage=28 字节、DB offset=0。concurrency-result.json/log。

### P2-02 — Python 3.11 的扫描防链接逻辑漏掉 NTFS junction

- **位置**：[engine/ai_photos/catalog.py:138](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/catalog.py:138>)。
- **原因**：followlinks=False 加 Path.is_symlink 无法拒绝 Windows junction；项目允许 Python 3.11。
- **影响/前提**：可扫描选定来源以外的目录，进入大型目录或循环 junction 时性能和范围控制失效。与 P0-01 是不同入口；此处没有证明扫描修改原片。
- **建议修复**：用 Windows reparse-point/junction 检查与最终路径范围校验，记录已访问目录身份以避免循环；补 Windows junction 测试。
- **验证结果**：本机真实 junction 指向另一生成目录，is_symlink=false，scan seen=1 并索引外部 private.png。junction-result.json；现有 symlink pytest 在 Windows 跳过。

### P2-03 — 相似照片预筛选漏掉阈值 4/5 内的哈希对

- **位置**：[engine/ai_photos/duplicates.py:117](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/duplicates.py:117>)。
- **原因**：注释称四个 16-bit band 可保证 Hamming distance<=5 共享一 band；差异分散到每个 band 时并不成立。
- **影响/前提**：默认 distance=4 下已有相似图候选被永久漏掉；SHA-256 精确重复分组不受此问题影响。
- **建议修复**：采用对最大阈值有数学保证的分区数量/半径检索，例如阈值+1 个不重叠分区，或正确的多索引哈希；以构造对验证无漏召回。
- **验证结果**：R07 注入两个距离恰为 4、四个 band 各一 bit 不同的指纹，similar_groups=0。验证筛选算法，非真实图片主观相似质量测试。

### P2-04 — 安装 HEIC 解码器后不重试既有失败缩略图

- **位置**：[engine/ai_photos/catalog.py:180](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/catalog.py:180>)。
- **原因**：是否更新仅看 size/mtime；之前 thumbnail/error 失败不触发重试。
- **影响/前提**：先导入 HEIC、后安装 pillow-heif 的常见路径中，重新 Scan 仍保留旧错误，也不生成该照片 AI 向量。
- **建议修复**：允许失败条目按解码器能力版本或显式重试重新处理，避免对永久损坏文件无界重试。
- **验证结果**：R08 切换 _thumb 为可用解码器后再次 scan，_thumb 调用 0 次，错误保留。

### P2-05 — 原生导入对同内容不同文件名仍产生多份文件

- **位置**：[engine/ai_photos/sync.py:291](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/sync.py:291>)。
- **原因**：最终路径包含原始 basename；幂等键含 device/asset/resource_type/hash。相同摘要不同 basename 会生成不同路径。
- **影响/前提**：跨资产、跨设备或文件名变化会浪费磁盘；SHA 去重分析可事后发现，但导入本身没有统一内容存储语义。
- **建议修复**：用摘要寻址的内容存储保留单份字节，资产/资源清单保存多个逻辑引用及原始文件名；不自动删除用户原片。
- **验证结果**：R06 相同字节、不同资产和文件名实际生成两份 HEIC。同一 begin 请求的现有幂等测试仍通过。

### P2-06 — 崩溃遗留导出/分块文件与未完成任务缺乏维护机制

- **位置**：[ios/PhotosCompanion/Sources/BackgroundChunkUploader.swift:66](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/BackgroundChunkUploader.swift:66>)。
- **原因**：重启后任务不在 pending 字典时直接 return，无法清理对应 chunk 文件；PhotoSource:56 每次导出新 UUID，进程崩溃不会执行 defer。Windows pending 只有数量限制，无取消、过期或磁盘预算。
- **影响/前提**：反复中断大型视频可残留整份导出和块文件，耗尽 iPhone/PC 空间；100 个未完成资源可阻止同设备新任务。
- **建议修复**：持久化 task/resource/临时路径关系，在确认后台任务状态后安全回收；设存储预算、可见的取消/清理操作与失败恢复。不可删除仍被 URLSession 使用的文件。
- **验证结果**：静态确认缺少任务路径恢复/启动清理；真实 iPhone 空间占用和崩溃清理 Not Verified。Windows 配额为数量而非字节已核对。

### P2-07 — 配对凭证未绑定服务器地址，且没有实施文档中的 ts.net 限制

- **位置**：[ios/PhotosCompanion/Sources/SyncAPI.swift:62](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/SyncAPI.swift:62>)。
- **原因**：URL 仅要求 HTTPS 任意 host；ContentView:19 配对后仍可编辑 serverURL，Keychain 只有一个全局 token，没有接收端身份绑定。
- **影响/前提**：修改地址时旧设备 bearer 可被发到另一 HTTPS 主机；配对码也可被提交到误填站点。不是 TLS 验证失效，也未证明无交互外传。
- **建议修复**：明确允许的接收端策略，切换主机要求重新配对并隔离凭证；绑定稳定接收端身份。若产品要求仅 Tailscale，应验证约定域名及服务身份。
- **验证结果**：静态 URL/Keychain/UI 核对；未向任意外站发送 token，真实 Tailscale 端到端 Not Verified。

### P2-08 — Wi-Fi only 不是贯穿整个备份过程的明确蜂窝网络策略

- **位置**：[ios/PhotosCompanion/Sources/SyncAPI.swift:81](<C:/Antigravity/Claude Project/Windows AI Photos/ios/PhotosCompanion/Sources/SyncAPI.swift:81>)。
- **原因**：仅初始 NWPath 检查和 allowsExpensiveNetworkAccess，未设置 allowsCellularAccess；iCloud 导出独立启用 isNetworkAccessAllowed，缺少持续网络切换协调。昂贵网络与蜂窝网络不完全是同一语义。
- **影响/前提**：无法仅凭现代码保证 Wi-Fi→蜂窝切换或 iCloud 下载遵守 UI 所示策略，可能产生流量成本。也不应忽略 Tailscale VPN 下路径识别兼容性。
- **建议修复**：对应用上传明确设置蜂窝/昂贵/受限网络策略，处理路径变化；对 PhotoKit/iCloud 说明系统约束并验证实际行为，必要时暂停导出。
- **验证结果**：静态与 Apple allowsExpensiveNetworkAccess/蜂窝访问 API 区别核对；未证明某运营商一定绕过。切网和 iCloud 流量实机测试 Not Verified。

### P2-09 — 照片无 EXIF 或视频的原始拍摄时间未被同步清单保留

- **位置**：[src/WindowsAIPhotos/Models/PhotoCard.cs:33](<C:/Antigravity/Claude Project/Windows AI Photos/src/WindowsAIPhotos/Models/PhotoCard.cs:33>)。
- **原因**：协议只传 asset_id/type/name/size/hash，无 PHAsset.creationDate 等外部元数据；接收文件 mtime 为写入时间，主网格和默认排序使用 mtime。
- **影响/前提**：字节内 EXIF 可完整保留，但没有嵌入拍摄时间的照片、视频和相册关系无法从备份恢复；UI 日期可能变成上传日。不能将字节完整等同于 Photo Library 语义完整。
- **建议修复**：在资源 manifest 保存原始创建/修改时间及必要元数据，区分接收时间与拍摄时间；展示优先使用可靠拍摄时间，明确时区来源。
- **验证结果**：静态协议字段与 PhotoCard 日期来源已核对。真实 EXIF、QuickTime 元数据、时区及还原效果 Not Verified。

### P2-10 — 同步语义查询堵塞 worker 请求，桥接没有超时/取消

- **位置**：[engine/ai_photos/worker.py:120](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/worker.py:120>)。
- **原因**：search 在 JSONL 读取线程直接做模型加载/推理和全量向量查询；PythonEngineBridge.cs:75 无限等 source.Task。SQLite 忙或推理卡住时 status/暂停/停止等请求也需排队。
- **影响/前提**：工作进程仍存活但 UI 操作可能长时间无响应，用户缺少可控恢复；每次查询全量读向量在大库反复分配内存。
- **建议修复**：将查询任务移到可控队列，控制模型并发；提供请求超时/取消和明确 busy 状态，保留管理控制通道，按测量决定缓存/ANN。
- **验证结果**：同步控制流静态确认。合成 100k/768维查询 1.063 秒、峰值 724.28 MiB；真实模型首次加载、100k 真实照片与 WinUI 响应 Not Verified。

### P2-11 — AI 工件未固定不可变版本，installed 不等于可用

- **位置**：[engine/ai_photos/semantic.py:38](<C:/Antigravity/Claude Project/Windows AI Photos/engine/ai_photos/semantic.py:38>)。
- **原因**：installed 仅检查 config 与权重文件存在；snapshot_download 无 revision；Python/AI 依赖使用宽版本范围，无锁定工件清单。模型加载失败后 UI 仍可判定已安装。
- **影响/前提**：新环境难以重现相同模型/依赖；不完整或损坏工件可能长期触发降级，缺乏可靠重装/修复入口，商用分发的来源与许可清单也不可复现。
- **建议修复**：区分 downloaded/verified/loadable 状态，校验固定 revision、摘要与处理器兼容性；锁依赖并生成 SBOM/第三方 notices，提供显式修复/重装。
- **验证结果**：静态确认。未下载约 1.5GB 真实模型，真实 SigLIP2 文本/图像维度、中英文检索、GPU/CPU 性能 Not Verified。模型卡标记 Apache-2.0，不代替完整依赖许可审计。

### P3-01 — 同步管理界面主要依赖手动刷新

- **位置**：[src/WindowsAIPhotos/SyncManagement.cs:146](<C:/Antigravity/Claude Project/Windows AI Photos/src/WindowsAIPhotos/SyncManagement.cs:146>)。
- **原因**：LoadAsync 只在打开窗口及 Refresh/控制动作后执行；没有定时器或同步事件订阅，sidebar 也不随接收进度自动刷新。
- **影响/前提**：传输进行时展示的百分比、完成数量和设备最近活动会陈旧；不能将这个界面称为已验证实时进度管理。
- **建议修复**：增加生命周期受控的刷新/事件订阅，窗口关闭时释放；展示错误、已取消/失败任务、原片位置与明确运行状态。
- **验证结果**：静态确认；实际按钮、窗口尺寸、长文件名、中文和高 DPI 行为因构建失败 Not Verified。

### P3-02 — 架构文档仍称没有 HTTP 服务

- **位置**：[docs/ARCHITECTURE.md:25](<C:/Antigravity/Claude Project/Windows AI Photos/docs/ARCHITECTURE.md:25>)。
- **原因**：文档绝对断言 No HTTP server is started，与 worker start_mobile 和原生同步服务实现不一致。README 的格式/阶段描述也需要按能力矩阵统一。
- **影响/前提**：交接人员可能错误理解网络面、启用条件和已实现/未验证边界。默认不启动 HTTP 本身仍成立。
- **建议修复**：更新为默认关闭、显式启动、loopback+Tailscale 的完整架构，分别标明传输支持、索引支持、备份支持、实机验证状态。
- **验证结果**：文档与调用路径静态比较确认；本轮按限制未改现有文档，只生成审查报告。

### P3-03 — CI 未覆盖 iOS 和实际输出目录的跨端冒烟

- **位置**：[.github/workflows/ci.yml:21](<C:/Antigravity/Claude Project/Windows AI Photos/.github/workflows/ci.yml:21>)。
- **原因**：只有 Ubuntu Python pytest 和 Windows Restore/Build；没有 XcodeGen/Xcode 模拟器构建，也没有编译产物资源/worker 冒烟。
- **影响/前提**：Swift API/构建问题和静态资源遗漏可能长期未被自动检测；当前 Python 通过不能替代这些检查。
- **建议修复**：获得修复批准后补 macOS 构建和输出目录冒烟门槛，设备测试保持人工验收；不要把 CI 绿灯当成实机备份通过。
- **验证结果**：只读取 ci.yml，未启动、重跑或修改任何 GitHub Actions。此项是覆盖缺口，不声称已有 Swift 编译失败。

## 10. Top 10 Prioritized Fixes

这是待批准的修复顺序，**本轮未实施**。

| 顺序 | 工作包 | 对应问题 | 修复后必须通过 |
|---:|---|---|---|
| 1 | 阻断备份目标重解析路径写入来源 | P0-01 | junction/symlink/reparse及竞态用例；来源字节和时间戳完全不变 |
| 2 | 修复 Windows 编译并验证实际输出 | P1-01/02 | SDK10完整Build、GUI启动、三个网页资源200、输出worker冒烟 |
| 3 | 统一完整资源清单与可验证副本 | P1-09/11 | MOV/DNG/Live Photo双资源/manifest均覆盖；缺失、离线、过期明确失败/待办 |
| 4 | 服务端完成核对和客户端接收端绑定 | P1-03/04，P2-07 | 文件丢失/损坏、换电脑/重建DB、重新配对后正确补传且凭证不串用 |
| 5 | 修正 PhotoKit 原始资源覆盖 | P1-05 | Mac构建+真实RAW+JPEG、ProRAW、HEIC/HEIF、Live Photo哈希与资源清单 |
| 6 | 关闭自动、取消、过期与恢复协调 | P1-06/07，P2-06/08 | 自动开关/锁屏/过期/离线/切网/强关/低存储实机矩阵 |
| 7 | 单资源错误队列和安全重试 | P1-08 | 故障项有记录且不阻塞其他资产，恢复后可补传，无虚报整库成功 |
| 8 | 跨卷 finalize 与短 SQLite 事务 | P1-10，P2-01 | 物理双卷、长推理并发、进程崩溃窗口、JSON错误与有界退避 |
| 9 | Windows junction 扫描隔离和索引修复 | P2-02/03/04/05 | 来源范围、循环保护、dhash阈值边界、解码器升级、重复导入存储语义 |
| 10 | 发布验收、AI工件、元数据和可恢复性 | P2-09/10/11，P3项 | 真实AI与中英文数据、50k–100k媒体、短事务/超时、恢复演练、签名与SBOM/许可、Mac及输出目录测试 |

## 11. PR #1 / PR #2 Merge Recommendation

**PR #1：暂缓合并。** 其 MVP 范围已经包含 backup.py 的来源覆盖风险和不完整备份成功语义；即使将 PR #2 拆开，也不能绕过 P0。Windows 构建/输出目录、真实 AI 和桌面功能应建立独立证据。本轮实际构建的是指定 PR #2 HEAD，**没有 checkout 并单独编译 PR #1 HEAD**；不将 PR #2 特有 FontWeights 错误自动归因于 PR #1。

**PR #2：维持 Draft/Request Changes。** PR #2 基于 PR #1，当前 WinUI 编译失败且 iPhone 完成记录、后台生命周期、RAW 覆盖和整资源二次副本存在阻断。协议本机正向测试和 >4GiB 续传是进展，但不足以批准原生自动备份发布。

批准修复后建议先小范围数据安全/构建修复，再资源清单和同步状态模型，再实机与发布验收；保留每次修复的独立证据。没有任何业务修复、提交、推送、PR 改动或合并在本轮发生。报告完成后停止，等待 Edwyn 批准修复计划。

资料与证据边界：

- [Microsoft FontWeights API](https://learn.microsoft.com/en-us/windows/windows-app-sdk/api/winrt/microsoft.ui.text.fontweights?view=windows-app-sdk-2.0)：对应 WinUI 命名空间；实际编译日志是本项目错误的直接证据。
- [Microsoft.WindowsAppSDK 2.6.4](https://www.nuget.org/packages/Microsoft.WindowsAppSDK/2.6.4)：版本存在和包信息；实际 restore 已解析该版本。
- [Apple：Capture and process ProRAW images](https://developer.apple.com/videos/play/wwdc2021/10160/)：读取 RAW 资源应检查 photo 和 alternatePhoto 的类型；本报告对具体设备组合的影响为静态推断。
- [Apple：BGTaskScheduler](https://developer.apple.com/documentation/BackgroundTasks/BGTaskScheduler)：任务提交与取消接口；调度结果须实机验证。
- [Apple：allowsExpensiveNetworkAccess](https://developer.apple.com/documentation/foundation/urlrequest/allowsexpensivenetworkaccess) 和 [蜂窝网络控制](https://developer-mdn.apple.com/library/archive/documentation/NetworkingInternetWeb/Conceptual/NetworkingOverview/Platform-SpecificNetworkingTechnologies/Platform-SpecificNetworkingTechnologies.html)：昂贵网络与蜂窝访问是不同配置维度。
- [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)：tailnet 内服务代理方案；未以该文档替代本项目端到端实测。
- [Python os.replace](https://docs.python.org/3/library/os.html#os.replace)：跨文件系统可能失败；本报告采用 EXDEV 故障注入。
- [Amazon S3 上传完整性](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html)：服务端/SDK checksum 与 metadata 声明应区分；没有验证所有兼容提供商。
- [Google SigLIP 2 模型卡](https://huggingface.co/google/siglip2-base-patch16-224)：标记 Apache-2.0；未完成全部传递依赖法律审查或真实权重验证。

附加文件：pytest.xml、reproduction-results.json、large-transfer-result.json、scalability-result.json、concurrency-result.json、junction-result.json、backup-junction-result.json、packaging-result.json、source-anchors.json、git/pr 查询与构建日志。原始路径/行号均来自审查提交。
