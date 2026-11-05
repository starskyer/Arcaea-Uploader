# 内置组件与授权处理

## Bundler 的整合方式

采用直接随项目保存源码的方式，无需用户额外下载 Arcaea-Bundler，也不依赖 Git submodule 或外部 main.py 路径。

| 文件 | 职责 |
| --- | --- |
| `arcaea_uploader/_vendor/arcaea_bundler/main.py` | 原作者源码，逐字节保留 |
| `arcaea_uploader/_vendor/arcaea_bundler/LICENSE` | 原 MIT 许可证完整副本 |
| `arcaea_uploader/_vendor/arcaea_bundler/UPSTREAM.json` | 作者、来源、固定提交、SHA-256 与选择原因 |
| `arcaea_uploader/bundler.py` | 本项目维护的调用适配层 |
| `THIRD_PARTY_NOTICES.md` | 仓库与分发包的第三方说明 |

本地 `main.py` 与上游提交 [`2b659e230f9d03520e4df0642a0d3cf6a7e78395`](https://github.com/Lost-MSth/Arcaea-Bundler/tree/2b659e230f9d03520e4df0642a0d3cf6a7e78395) 完全一致，许可证也逐字节一致。这次保持现有更新流程的兼容性，没有自动升级为上游最新分片版本。

适配层直接调用 `Bundler` 类，让异常传播给调用者；显式关闭文件句柄，拒绝覆盖已有输出、拒绝把输出放进 assets，并验证元数据版本、数据范围和 SHA-256。它是供执行流程调用的内部 API，当前尚未连接 GUI 的发布按钮。成功打包仍会更新工作区内的 `metadata.oldjson`，不应直接在生产资源目录试运行。

`build_bundle(assets_root, output_directory, ...)` 的第二个参数为输出目录。普通更新使用生成 JSON 的 `versionNumber` 作为文件名前缀；应用版本切换且本包 `versionNumber` 等于新应用版本时，使用 `<新应用版本>.0`，与维护者现有承接包约定一致。适配层先检查正式目标是否已存在，再在临时目录构建和校验，最后 CB、JSON 依次就位；发现同名目标时不推进版本历史。第三方源码没有修改。

`build_configured_bundle(assets_root, output_directory, **config["bundle"])` 使用与 UI 相同的版本选择规则。默认沿用历史应用版本并递增 bundle 版本；手动模式将新应用版本同时传给原核心的 `app_version`、`bundle_version`，上一包版本仍从历史衔接。只改变应用版本参数却沿用旧 bundle 分支的情况已在适配层处理。只读预览位于 `bundle_versions.py`，不会创建产物或推进历史。

与原 Bundler README 的完整参数对应，例如从 `7.0.256.4` 切换到应用版本 `7.0.257`：

```powershell
python main.py b -i active -o 7.0.257.0.cb -av 7.0.257 -bv 7.0.257
```

这是参数对应说明；Uploader 实际直接调用内置核心，不需要外部 main.py。该包 JSON 中两个版本字段为 `7.0.257`，`previousVersionNumber` 为 `7.0.256.4`。之后沿用模式生成 `7.0.257.1.cb/.json`。首次构建也可手动指定应用版本，`previousVersionNumber` 为 null。songlist 的导入 version 独立配置，不参与这里的版本推导。

未来更新第三方核心时，应替换源码和对应 LICENSE，更新固定提交及校验和，再验证初次打包、增量版本、产物校验和服务端兼容性。本项目修复优先放在适配层；若确需修改原作者文件，应记录修改内容并保留原声明。`.gitattributes` 防止 Windows Git 换行转换破坏已核对的原始快照。

## MIT 许可证如何适用

[上游 MIT 许可证](https://github.com/Lost-MSth/Arcaea-Bundler/blob/2b659e230f9d03520e4df0642a0d3cf6a7e78395/LICENSE) 允许使用、复制、修改、合并和分发该代码，要求副本或主要部分保留原版权声明及许可声明。因此本项目保留 `Copyright (c) 2024 Lost-MSth` 和完整 LICENSE，并在第三方说明及界面中列明来源。

不需要仅因为整合 MIT 组件就将整个项目标为同一作者或同一许可证。本项目自有代码的许可证仍可另选；该选择不会替代 Bundler 的原许可。当前未擅自为维护者决定全项目许可证。

`pyproject.toml` 将 LICENSE、来源 JSON 放进 Python 包，并将第三方说明带入 wheel；`MANIFEST.in` 也包含源码分发所需文件。未来独立 EXE 仍需带上这些文本，且应检查实际捆绑的其他依赖。本次没有捆绑 FFmpeg 二进制。

## 图片与音频脚本

维护者提供的原临时脚本保留为 `arcaea_uploader/_legacy/resize_and_preview.py`，不作为运行入口；其 SHA-256 与改造记录在 `ORIGIN.json`。项目实际使用的内部接口位于 `arcaea_uploader/media.py`：

- `normalize_jacket`：基本曲绘改名；缩略图统一为 384×384，已经达标时也会完成改名。保留源文件，只修改明确指定的工作输出。
- `preview_interval`：按 `ratingClass` 查找独立难度预览字段，缺少字段则继承曲目级时间。
- `create_preview`：沿用 PCM 中间文件、Vorbis 编码及淡入淡出流程。显式请求替换时可重建已有预览；转换、验证失败则保留旧预览和源音频，清理临时文件。
- FFmpeg 可指定路径或从 PATH 查找，不再硬编码原机器位置；Windows 子进程不弹控制台。

完整音频不会由该模块自动删除；后续资源准备流程只在对应发布工作副本中决定哪些文件进入 bundle。独立曲绘和少数独立音频的最终客户端命名仍按后续决策处理，通用裁剪函数要求调用者明确指定目标文件名。

## 验证范围

自动化测试覆盖原始源码与许可证校验、初次/增量打包、版本历史、损坏产物检测、缩略图、已有预览重建、转换失败保留旧文件、旧配置迁移以及 GUI 授权展示。真实 FFmpeg 测试使用临时生成的音频，不读取或修改游戏资源。
