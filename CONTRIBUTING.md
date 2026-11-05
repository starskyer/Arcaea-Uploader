# 参与开发

当前是桌面预览版。资源准备和发布阶段的设计见 [更新流程](docs/update-workflow.md)，已实现范围见 [README](README.md)。建议先通过 Issue 描述预期行为和具体输入，再提交改动。

## 本地环境

需要 Python 3.10+、Tkinter，以及 Pillow。完整音频测试需要可执行的 FFmpeg。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m unittest discover -s tests -v
```

Linux 需要 Tk 与显示服务；无桌面时可用 `xvfb-run -a python -m unittest discover -s tests -v`。缺少 FFmpeg 时音频集成测试会跳过；设置 `ARCAEA_UPLOADER_TEST_FFMPEG` 可指定可执行文件。

测试使用临时生成的曲目和媒体，不依赖维护者的游戏资源。不要将真实资源包、服务器数据库、连接凭据或 `config.local.json` 放进提交。构造最小输入时使用合成图片、音频和谱面。

## 改动与验证

- 资源路由、增量合并和版本链改动应覆盖新增行为与有实际风险的失败场景。
- 界面改动应检查默认窗口、最小窗口和系统缩放；Tk 操作放在主线程。
- 第三方 Bundler 与历史媒体脚本保持原始字节。更新来源时同步来源记录、许可证与校验和。
- 提交 PR 时描述触发问题、修改后行为和实际运行的验证命令。

构建与发布流程见 [发布维护说明](docs/releasing.md)。
