# 构建与发布

仓库主分支用于开发，GitHub Releases 提供带版本号的预览版。普通推送不发布版本；推送与项目版本一致的 `v*` 标签会触发 Windows 构建、验证与预览发布。

## 自动检查

CI 在 Windows / Linux、Python 3.10 / 3.13 运行测试。Linux 使用 Xvfb 运行 Tk 测试，FFmpeg 用于真实音频集成测试；另有 wheel、源码包构建、元数据检查和安装验证。

Actions 使用固定提交，Dependabot 按月提供依赖更新。PR 工作流只有读取仓库内容的权限；发布工作流仅在维护者推送版本标签时拥有 Release 写入权限。

## 本地构建

建议在独立虚拟环境安装依赖，不将 FFmpeg 或游戏资源放入打包目录：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,windows]"
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\python -m build
.venv\Scripts\python scripts/build_windows.py
.venv\Scripts\python scripts/check_distribution.py dist --write-checksums
```

Windows 构建须使用 64 位 Windows Python。脚本在独立临时工作区运行 PyInstaller，复制使用文档、示例配置和运行时许可证，启动 EXE 做 Tk、Pillow、Bundler 来源及版本逻辑的冒烟检查，再生成 ZIP。部分 Python 安装器未附 Tcl 独立许可证时，会从 Tcl 官方仓库取得相同运行时版本的许可证。

`dist/` 中的主要产物：

- `Arcaea-Uploader-<版本>-Windows-x64.zip`：含 EXE、`_internal` 依赖、文档、完整示例配置和许可证目录。
- `arcaea_uploader-<版本>-py3-none-any.whl`：Python 安装包。
- `arcaea_uploader-<版本>.tar.gz`：源码分发包。
- `SHA256SUMS.txt`：上述文件校验和。

便携版不捆绑 FFmpeg，不包含本机配置、SSH 凭据或游戏内容。`--smoke-test <JSON报告>` 是构建验证入口，不启动资源任务或网络连接。

## 发布版本

1. 更新 `pyproject.toml` 与 `arcaea_uploader/__init__.py` 的版本及 CHANGELOG。
2. 在 main 上确认 CI 通过，检查分发内容与当前功能说明。
3. 创建并推送对应标签，例如 `git tag v0.1.0`、`git push origin v0.1.0`。
4. 检查 Preview release 工作流及 Releases 附件，在干净环境解压运行验证。

当前流程始终标为预发布。未来完整更新流程实现后，再调整稳定版策略；GitHub 自动生成的 Source code ZIP 不包含可运行的 EXE。
