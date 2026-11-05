# 现有实现调查

调查日期：2026-11-05。依据用户提供的本地副本，只读检查源码与少量目录；没有运行 Bundler、修改资源或调用服务端。

## Arcaea-Bundler

入口为 `arcaea_bundler/main.py`，本地 README 给出的打包方式为：

```text
python main.py b -i <资源包目录> -o <输出文件前缀> -av <客户端版本> -bv <bundle版本>
```

这里的 `main.py` 指 Bundler 的入口文件，命令仅说明上游工具用法。第一次打包应显式指定版本；已有历史时可沿用历史中的客户端版本并自动递增 bundle 版本。

- 资源包采用 assets 目录布局，README 要求包含 `songs/songlist`、`songs/packlist`、`songs/unlocks`。
- 输出默认为同名 `.cb` 数据与 `.json` 元数据。两者必须一起准备和发布。
- 默认历史文件位于输入目录的 `metadata.oldjson`，记录既有文件哈希和版本；后续打包计算新增、修改、移除项。
- 打包成功会写回历史文件，因此调用 Bundler 会改变输入目录状态。需要保存打包前的历史以及打包产物，支持失败恢复。
- 本地入口捕获异常后只记录日志，没有显式返回非零退出码。因此接入时不能只看退出码，必须验证产物存在、元数据可解析、版本符合预期，以及数据范围和哈希匹配。
- 本地 Bundler 输出单个 `.cb`；本地服务端另有分片支持，不意味着该 Bundler 能生成分片。

源码定位：`Bundler.old_parse`、`Bundler.record_old_metadata`、`Bundler.parse`、`main`。

## Arcaea-server

项目根目录下的 `latest version` 为当前运行代码目录。默认路径定义于 `core/config_manager.py`：

| 配置 | 默认值（相对运行目录） |
| --- | --- |
| `CONTENT_BUNDLE_FOLDER_PATH` | `./database/bundle/` |
| `SONG_FILE_FOLDER_PATH` | `./database/songs/` |
| `SONGLIST_FILE_PATH` | `./database/songs/songlist` |

`core/bundle.py` 的 `BundleParser.parse` 遍历 bundle 目录中的 JSON，读取版本链并查找同名 `.cb`；本地副本还支持 `<stem>_0.cb` 等分片形式。Uploader 应在发布 JSON 前确保全部数据文件就位。

### 已确认的刷新 API

路由前缀来自 `api/__init__.py`、`api/system.py`，鉴权来自 `api/api_auth.py`，具体操作来自 `core/operation.py`。

| 请求 | 作用 |
| --- | --- |
| `GET /api/v1/system/operations` | 获取当前可用管理操作，要求 `system` 权限 |
| `POST /api/v1/system/operations/refresh_song_file_cache` | 清除并重建曲目目录、songlist 和文件哈希相关缓存；哈希预计算受服务端配置影响 |
| `POST /api/v1/system/operations/refresh_content_bundle_cache` | 重新解析 bundle 并清除相关查询缓存 |

POST 请求需要 `Token: <token>` 请求头、`Content-Type: application/json` 和 JSON 对象请求体（无参数时为 `{}`）。可以使用具有所需权限的用户 Token，或服务端配置的非空 `API_TOKEN`。不要将 Token 写入发布清单或日志。

成功响应的业务字段为 `code: 0`。部分错误也可能使用 HTTP 200，必须同时检查 HTTP 状态和业务码。API 部署前缀和是否启用需由实际环境确认。

独立进程调用相同 Python 刷新类不能更新已运行服务进程的内存缓存，因此上传后需要请求运行中的 API。多进程部署中各 worker 的缓存一致性也需核实，不能将单次请求成功直接等同于所有进程已刷新。

### 最初的曲目目录观察

本地存在 `active/songs/dl_2094/` 与 `database/songs/2094/`：

| 位置 | 本次看到的文件 |
| --- | --- |
| 资源包 `songs/dl_2094/` | `1080_base.jpg`、`1080_base_256.jpg`、`preview.ogg` |
| 服务端 `database/songs/2094/` | `0.aff`、`1.aff`、`2.aff`、`base.ogg` |

这说明当前资源包目录不能直接整体复制为服务端下载目录；该样本也没有提供服务端所需谱面和完整音频的来源。

本地 `core/download.py` 中下载候选文件包括 `0.aff` 至 `4.aff`、`base.ogg`、`3.ogg`、`video.mp4`、`video_audio.ogg`、`video_720.mp4`、`video_1080.mp4`。实际是否可下载还取决于 songlist 中的 `remote_dl`、难度、`audioOverride`、`additional_files` 等字段，不能将候选列表视为每首曲目的必需文件。

调查时服务端 `database/songs/` 顶层没有普通文件，因而未发现默认位置的 `songlist`。是否需要由 Uploader 同步 songlist、是否覆盖了默认配置，需要在曲目规则阶段确认。

用户随后明确原始曲目包是完整音频/谱面的输入来源；输入只含本次增量时保留既有资源。该工作流现已记录在[曲目资源规则](song-packages.md)，上面的目录观察仅保留为最初调查记录。

## resize_and_preview.py

实际文件是 `resize_and_preview.py`，不是名为 `resize_and_preview` 的目录。入口默认处理 `./active/songs`，也接受目标 songs 目录参数和 `--songlist` 文件名。

- 使用 Pillow 遍历图片；将 `base.jpg` 改为 `1080_base.jpg`，将 `1080_base_256.jpg`、`base_256.jpg`、`3_256.jpg` 调为 384×384。
- `base_256.jpg` 的改名位于 resize 分支内：尺寸已经符合要求时会先跳过，导致没有改名。独立曲绘只对少数字段有处理，不能直接保证 0–4 的全部情况。
- 使用 FFmpeg 生成预览，音频裁剪时间单位为毫秒，默认淡入 500ms、淡出 2000ms。脚本硬编码 FFmpeg 路径，接入时需要参数化。
- 成功生成并验证预览后，会删除工作目录里的 `base.ogg` 或 `3.ogg`。完整下载音频必须在此之前独立保存，不能处理原始输入包。
- 已有 `preview.ogg` 时直接跳过该曲目，连后续 `3.ogg` 都可能不处理；已有主预览不应阻止独立音频增量更新。
- 独立音频取 `difficulties[3]`，数组不足四项时会失败，且下标不一定对应 ratingClass 3；应按 `ratingClass` 查找并回退到曲目级预览字段。
- 仅特殊处理 `3.ogg`，没有覆盖其他数字音频；音频失败计数也不必然导致进程失败。

后续已将原脚本快照纳入项目，并在独立媒体模块中修复改名、预览重建、难度查找、FFmpeg 路径和失败清理等问题。当前只读预检仍不执行媒体转换，详见[内置组件说明](integrated-tools.md)。

## 占位谱面

已计算 `aletheia/0.aff`、`1.aff`、`2.aff` 的 SHA-256，三者一致：

```text
e1ea27ec9603a0bb9c5c273e2021b19ec969c141a33e9e91c14cd340d1f4aaf0
```

因此可用可配置的单个源文件补齐缺失的 0–2，不覆盖已有真实谱面。
