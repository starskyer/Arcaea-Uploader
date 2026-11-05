桌面预览版：当前支持曲目预处理、只读资源预检、配置保存与报告导出。**完整“准备并审核 / 发布更新”、SSH 上传和刷新 API 尚未接入，按钮保持禁用。**

Windows 用户下载 `Arcaea-Uploader-<版本>-Windows-x64.zip`，完整解压后运行 `Arcaea-Uploader.exe`，无需安装 Python。FFmpeg 仍需另行安装或在配置页指定；不要单独移动 EXE，需保留 `_internal` 目录。

`config.example.json` 包含 36 组背景配对示例，可通过配置页载入并修改本地路径。本机配置默认保存在 `%LOCALAPPDATA%\Arcaea-Uploader\config.json`。发布包不含游戏资源、数据库或连接凭据。

Python 用户也可下载 wheel 或源码包。完整变更见 CHANGELOG，文件校验和见 `SHA256SUMS.txt`。此便携版尚未进行代码签名。
