桌面预览版：已接入 **01 准备审核 → 保存总表 → 02 打包上传 → 03 操作与确认提交**。01 可累积准备；02 上传后保留全部工作区内容，03 按所选 API 操作成功并人工确认提交后才清理。支持启动恢复、新目录提醒、多 server 配置快照与检查点重试。

Windows 用户下载 `Arcaea-Uploader-<版本>-Windows-x64.zip`，完整解压后运行 `Arcaea-Uploader.exe`，无需安装 Python。FFmpeg 仍需另行安装或在配置页指定；不要单独移动 EXE，需保留 `_internal` 目录。

`config.example.json` 包含已确认的背景配对示例，可通过配置页载入并修改本地路径。本机配置默认保存在 `%LOCALAPPDATA%\Arcaea-Uploader\config.json`。发布包不含游戏资源、数据库或连接凭据。

packlist / unlocks 仍需人工更新；完整本机部署模式尚不支持，02 需要 SSH。SSH 客户端采用内置 Paramiko，保留许可证、可替换源码与对应完整源代码归档。实际使用前请阅读 README 的现有限制及更新流程。

Python 用户也可下载 wheel 或源码包。完整变更见 CHANGELOG，文件校验和见 `SHA256SUMS.txt`。此便携版尚未进行代码签名。
