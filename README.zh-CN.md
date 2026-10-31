# Windows AI Photos：Windows 本地 AI 智能相册

> 当前版本为 **开发用 MVP 源代码**，尚未经过真实 Windows 编译与安装包验收；不是可直接安装的商业版本。

## 目标

用中文或英文描述画面内容，在本地几万张照片中搜索匹配的图片。照片保存在原目录，不上传云端、不移动、不复制。**不需要 Ollama。**

## 已写入的功能

| 功能 | 现状 |
|---|---|
| Windows 原生窗口（WinUI 3） | 源码已实现，等待 Windows 编译验收 |
| 从多个文件夹导入本地照片 | 已实现 |
| 缩略图网格，打开原始照片 | 已实现 |
| JPG/PNG/WebP/TIFF 等常用照片 | 已支持（依赖 Pillow 解码） |
| 首次索引与文件变更增量更新 | 已实现 |
| 文件名/路径检索 | 已实现，不装 AI 也可用 |
| 图片内容语义搜索 | 已实现 SigLIP 2 接口；需要先安装 AI 模型并实机验证 |
| 搜索中文/英文自然语言 | 模型设计支持；实际准确率待样本评估 |
| 不可访问硬盘的照片记录保护 | 已实现 |
| HEIC / RAW / OCR / 人物识别 | 尚未完成 |
| 无 Python 依赖的独立安装包 | 尚未完成 |

## 本地硬盘占用

- 软件源码和索引组件较小；实际发布程序体积以后续安装包为准。
- SigLIP 2 Base 权重大约 1.5 GB，只有主动点击“Install AI model”后才下载。
- 5 万张照片的缩略图预计 1–4 GB，向量与数据库另外占空间。
- 原始图片不会复制到数据库。

## Windows 开发步骤（PowerShell）

1. 安装 Python 3.11+、.NET 10 SDK、Visual Studio 的 WinUI/Windows App SDK 开发组件。
2. 解压后在项目根目录执行 `./scripts/setup-dev.ps1`。
3. 将 Python 解释器设置到环境变量 `AI_PHOTOS_PYTHON`（脚本会输出准确命令）。
4. 执行 `./scripts/build-windows.ps1`，编译后在 Visual Studio 或 `dotnet run` 启动。
5. 使用按钮添加图片文件夹，并扫描。
6. 如需图像语义搜索，先安装 `engine/requirements-ai.txt`，再在界面点击 Install AI model，完成后重新扫描。

只想测试照片引擎，无需 WinUI：

```powershell
$env:PYTHONPATH = (Resolve-Path .\engine).Path
.\.venv\Scripts\python.exe -m ai_photos.cli add-folder 'D:\Pictures'
.\.venv\Scripts\python.exe -m ai_photos.cli scan
.\.venv\Scripts\python.exe -m ai_photos.cli search '雪山'
```

## GitHub 仓库

当前仓库由你创建：`Edwynl/Iphone-Libary-`，MVP 使用 `feat/windows-ai-photos-mvp`
分支提交，合并之前请检查 PR 的 Windows 构建结果。

建议更名为 **Windows-AI-Photos**：仓库右上角 **Settings → General → Repository name**。
也可以在已安装并登录 `gh` 的电脑上运行 `scripts/rename-existing-repository.ps1`。
已确认当前仓库是 **Public（公开）**：如计划商业发布且代码暂不公开，
请在 Settings → General → Danger Zone 将其改为 Private。

初版另增加了安全的「从相册中移除目录」功能，只会删除目录索引，**不删除源照片**；
即使所选目录覆盖了本软件缓存目录，也不会把缓存缩略图再次索引成照片。

## 给 Claude Code 的开发任务

请先审核 `docs/ACCEPTANCE.md`：优先完成 Windows x64 构建、真实 SigLIP 2 编码输出验证、文件夹选择和显示测试；不要跳过测试就发布 EXE。然后再处理 5 万张照片的性能测试、安装器、Windows ML/ONNX 模型打包、中文搜索准确率及 HEIC/RAW 支持。


## 第二阶段：iPhone 原生自动备份（开发预览）

- `ios/PhotosCompanion/`：SwiftUI + PhotoKit 原生 iPhone 项目（需要 Mac、Xcode 和 XcodeGen 编译）。
- 原画质导出：HEIC / Live Photos 的静态照片及配对 MOV / DNG / 视频，不主动转码。
- 8 MiB 分块、SHA-256 校验和 Windows 端持久化偏移量，掉线后按确认偏移恢复。
- Windows Sync Management：配对、每台设备撤销授权、暂停/恢复、停止服务、查看任务进度。
- 默认不启用远程同步；iPhone 自动同步需用户明确开启。建议 Tailscale Serve 私有 HTTPS。
- iOS 后台任务由系统调度，不能保证实时或固定时间；手动强制退出 App 后不能期待它自动启动。

⚠️ 目前通过的是 Windows Python 协议测试及 Swift 语法检查，**尚未在 Xcode 和真实 iPhone 编译及验证**。
请先阅读 [`docs/PHASE2_NATIVE_SYNC.md`](docs/PHASE2_NATIVE_SYNC.md)，不要把它当成已发布软件。

## 审查修复后的能力边界

- 原生 MOV、MP4、DNG 与 Live Photo 双资源都纳入二次备份清单，保存原名、资产关联、拍摄时间和 SHA-256。
- 本地备份拒绝 junction、符号链接和重解析输出路径；过期、离线、缺失或损坏条目会明确标记未完成。
- 本地备份保存不可变 v2 快照和按 SHA-256 寻址的内容文件；照片变化不覆盖旧版本，失败批次不发布不完整清单，旧 v1 备份保留并可恢复。不会自动清理历史或中断遗留内容，需预留空间。此策略尚未应用到 S3。
- `restore-local 备份目录 独立空目录` 默认只验证；加 `--apply` 才复制。不会覆盖已有文件，也不重建 Apple Photos 相册或编辑历史。
- `backup-history 备份目录` 列出历史 ID；`restore-local 备份目录 独立空目录 --snapshot 快照ID --apply` 恢复指定版本，省略 `--snapshot` 则使用最后成功清单。
- iPhone 完成记录绑定 Windows 接收端身份，跳过前向服务器核验；更换电脑须重新配对。
- 严格“仅 Wi-Fi”只导出手机已下载的原片，因为 PhotoKit 没有逐请求禁止蜂窝下载的接口。iCloud 原片需先在 Photos 下载，或明确关闭此限制。
- AI 安装改用固定模型 revision 和 SHA-256 清单，依赖锁在 `engine/requirements-ai.lock.txt`；该锁针对 Windows Python 3.11，真实模型与 iPhone 仍需实测。
- 当前未签名发布、未合并 PR、未运行云端 CI。完整修复验证与剩余门槛见 `docs/review/FIX_VALIDATION.md`。
