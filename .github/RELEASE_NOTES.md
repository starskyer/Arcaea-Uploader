完成首轮实际使用与反馈的桌面预览版，接通 **01 准备审核 → 保存总表 → 02 打包上传 → 03 操作与确认提交**。

## 本版主要变化

- 同一工作区可反复追加曲目或难度，保留人工审核；工作区 active 支持自由修改资源，02 记录差异并备份、同步本地改动。
- 图片整理为真实 JPEG，缺少曲绘缩略图时自动补齐；非标准背景缩放为 1920×1440，明确提供的背景组合可自动记忆。
- 01 后即可预览、编辑和导出购买项安排；无法推断的项经确认后跳过，其余操作继续。
- 允许修正逐曲 version；定数可显式填写 `0.0` / `-1` 表示 Unrank，留空保留已有值。界面/API 使用真实定数，数据库使用十倍整数。
- API 可独立选择直连或系统代理；改进长时间刷新、连接错误提示和断点恢复，写请求不会自动重放。
- 03 验收后确认提交才清理工作区；完成归档只读，之后调整定数或购买安排需创建独立后续任务。
- 修复小屏幕下购买项预览被上方操作区挤到不可见的问题。

## 下载与使用

Windows 用户下载 `Arcaea-Uploader-0.2.0-Windows-x64.zip`，完整解压后运行 `Arcaea-Uploader.exe`，无需安装 Python。请保留 `_internal` 目录；FFmpeg 仍需另行安装或在配置页指定。

`config.example.json` 包含背景配对示例。便携版默认将本机配置保存在 `%LOCALAPPDATA%\Arcaea-Uploader\config.json`；源码用户可继续使用既有配置及工作区。本版不包含游戏资源、数据库或连接凭据。

packlist / unlocks 仍需人工更新，02 使用 SSH 私钥上传；API 代理开关不影响 SSH 连接。若旧服务端把数据库零定数回显为 `-1`，需按 [03 操作说明](https://github.com/starskyer/Arcaea-Uploader/blob/v0.2.0/docs/server-operations.md) 修复零值序列化后重试。

Python 用户可下载 wheel 或源码分发包。完整变更见 [CHANGELOG](https://github.com/starskyer/Arcaea-Uploader/blob/v0.2.0/CHANGELOG.md)，文件校验和见 `SHA256SUMS.txt`。便携版尚未进行代码签名；第三方许可证和 Paramiko 对应源码随包提供。
