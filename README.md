# Arcaea-Uploader

[![CI](https://github.com/starskyer/Arcaea-Uploader/actions/workflows/ci.yml/badge.svg)](https://github.com/starskyer/Arcaea-Uploader/actions/workflows/ci.yml)

用于 **Lost-MSth** 的 [Arcaea-server](https://github.com/Lost-MSth/Arcaea-server) 的桌面资源更新工具。把待更新曲目放入一个输入目录，通过窗口整理格式、增量准备资源并审核 songlist，再完成打包、上传与可选的服务端操作。

**01 准备并审核、02 打包并上传、03 服务端操作与最终提交均已接入。** 选择曲目目录、填写 version 后可直接执行 01。工作区代表一批尚未提交的改动：01 可累积准备，02 上传成功后保留全部暂存内容，03 操作成功并人工确认提交后才清理。原始曲目包保持不变。

Bundler 核心、图片/预览处理与 SSH/SFTP 客户端现已内置，无需另行下载 Bundler 或配置脚本路径。待打包资源目录下文简称 active，沿用 616 下载解压资源的目录名。

## 当前进度

当前版本为 **v0.2.0（2026-11-10，预览版）**，完成首轮实际导入、上传与服务端操作验证。下表对应本次版本；主分支后续改动见 CHANGELOG 的“未发布”部分。

| 部分 | 已实现行为 |
| --- | --- |
| 桌面与配置 | 更新 / 配置页、JSON 配置持久化、ⓘ 说明与示例、无向背景配对、应用版本与包名预览 |
| 预处理与检查 | 嵌套曲目发现、格式归一、空谱排除、音频转换、曲绘补全、增量合并及 INFO / WARNING / ERROR 报告 |
| 01 准备审核 | 准备 bundle 与下载资源，打开审核总表；同一工作区可反复追加曲目或难度，保留人工修改与删除字段 |
| 02 打包上传 | 工作区 active 可自由修改；记录新增/修改/删除差异，打包并同步本地、SSH 上传；失败后继续原构建 |
| 03 可选操作 | 上传前可编辑定数、预览购买清单与确认跳过；上传后按序刷新、补购买项、写定数和评分，保存检查点；API 可独立选择直连 |
| 工作区生命周期 | 跨重启恢复、多 server 配置快照、旧批次兼容；03 验收并确认提交后清理，归档只读，后续修改另建任务 |

本版保留人工审核的资源、逐曲 version 和删除字段，支持非标准背景缩放与配对记忆；修复追加时的文件占用、API 超时后的继续执行以及提交后误改归档等问题。定数允许显式填写 `0.0` / `-1`，留空继续保留已有值。

回归测试覆盖桌面、购买项编辑与确认跳过、Unrank 定数、工作区自由修改及删除重试、背景缩放与 JPEG 整理、增量审核、构建恢复、归档及后续任务、测试 SSH/API 服务和真实 FFmpeg。CI 在 Windows / Linux、Python 3.10 / 3.13 上运行；发布时另行构建并检查 Windows 便携包。变更明细见 [CHANGELOG](CHANGELOG.md)。

## 当前限制与未覆盖行为

以下仅记录当前状态，**不是开发路线图，也不承诺后续一定实现**；部分行为可能长期保留或最终明确不支持，后续发现的问题会继续补充。

1. **不自动导入 packlist、unlocks。** 目前元数据合并仅覆盖 songlist。若新增曲包或解锁条件涉及 `active/songs/packlist`、`active/songs/unlocks` 等其他文档，需在执行 **02 打包上传之前**手动更新并保存。可编辑原 active 或本批审核副本；两边同时改成不同内容会报错，需人工统一。
2. **尚未实现本机部署的完整更新模式。** 02/03 目前围绕另一台主机上的 Arcaea-server 组织流程，文件上传默认使用 SSH。SSH 服务器 IP / 主机名留空不会自动切换为本机模式，完整上传任务仍无法进行。03 的独立入口通过配置的管理 API 调用，不建立 SSH 连接；这不代表已经支持本机资源就位、数据库处理等完整流程。
3. **连接方式有明确范围。** 当前 SSH 上传采用私钥认证，尚无密码登录输入框，也不支持其他传输协议。03 使用管理 API Token 环境变量，尚未提供 API 账号密码登录界面；旧 Web 管理页的登录状态不能直接用于这里。
4. **部分资源仍需人工处理。** 自定义 WAV 音效仅告警、不分发；非标准独立难度音频的下载声明仍需核对。图片会整理为真实 JPEG，非标准背景尺寸自动缩放至 1920×1440；音频后缀与实际编码不符时仍不专门识别或修复，具体边界见[曲目资源规则](docs/song-packages.md)。

![桌面界面示例，使用示例路径](docs/assets/desktop.png)

## 打开桌面窗口

Windows 用户可从 [Releases](https://github.com/starskyer/Arcaea-Uploader/releases) 下载便携版 ZIP，完整解压后运行 `Arcaea-Uploader.exe`，无需安装 Python；保留同目录的 `_internal` 文件夹。FFmpeg 仍需自行安装或在配置页指定。当前为预览版，具体限制见发布说明。

主分支可能包含尚未发布的功能，见[更新记录](CHANGELOG.md)的“未发布”部分；体验这些改动时请从源码运行，现有下载包以对应 Release 说明为准。

从源码运行时，需要 Python 3.10+（含 Tkinter），在项目目录安装依赖一次：

```powershell
python -m pip install -e .
```

Windows 已关联 Python `.pyw` 时，双击项目中的 `Arcaea-Uploader.pyw` 即可打开窗口，不显示控制台；也可使用安装后的 `arcaea-uploader-gui` 入口，或执行 `python -m arcaea_uploader`。

- **更新页**：选择收集曲目的来源目录，填写“本次导入 version”，点击 01；审核保存总表后点击 02。预处理和检查是辅助入口。滚动日志按 INFO / WARNING / ERROR 显示，结果可导出 JSON 报告。
- **临时工作区**：启动时提示未提交的工作区；新选目录时可选择继续累积或明确新建独立工作区。工作区管理可恢复各自的服务器配置和进度，同时处理不同 server。文件、占位谱面、定数草稿与购买候选都保留到 03 的最终提交，详见[工作区管理](docs/workspaces.md)。
- **配置页**：填写本地 Arcaea-server、active、曲目输入目录、发布工作区；填写 SSH 主机、端口、用户名、服务器项目路径、密钥及刷新 API 设置。路径可浏览选择，配置在同一页滚动查看。FFmpeg 仍为外部程序，可指定路径，或留空从 PATH 查找。
- 点击“保存配置”后，下次打开会恢复设置；也可载入已有 JSON 配置或另存。曲目目录和导入 version 在两页同步；“背景对应表…”可查看、编辑或重新读取 active 总表中的翻转关系。
- “沿用当前应用版本”默认勾选，输入框锁定并显示从 Bundler 历史识别的应用版本；取消后填写新应用版本，窗口预览 `.0` 承接包名称。该设置保存在 `bundle` 配置组，与曲目导入 version 分开。
- 管理 API 默认跟随系统代理；自有服务器可直连时，可取消配置页的“API 使用系统代理”，无需关闭整机代理。GET 连接失败最多再试一次，写请求不自动重发；定数同步复用当次预读和完整写入响应，重试跳过已完成曲目的查询。
- 提交清理后的 03 归档只读。进一步调整定数或购买安排时，点击“创建后续任务…”另存草稿；默认关闭资源刷新，成功后自动归档，原提交摘要与任务记录保留。

开发目录存在被忽略的 `config.local.json` 时优先使用；否则默认配置保存于用户本地应用数据目录的 `Arcaea-Uploader/config.json`。可用 `python -m arcaea_uploader gui --config <路径>` 明确指定。配置页允许先保存不完整的信息；资源预检时再校验本地路径，不要求提供 VPS 凭据。详见[桌面界面说明](docs/desktop.md)。

### 首次配置与使用

1. 在配置页指定本地 Arcaea-server 项目根目录、待打包的 active、曲目来源目录和临时工作区。`database_relative_path` 默认是 `latest version/database`；占位谱默认取该 database 下的 `songs/sayonarahatsukoi/0.aff`，也可另选一个仅含 timing 的空谱。
2. 配置 FFmpeg；为 02 填写 SSH 主机、端口（默认 22）、用户名、私钥、known_hosts 和 VPS 上的项目根目录，例如 `/srv/Arcaea-server`。首次连接会要求确认主机指纹。
3. 如需 03，配置管理 API 前缀，例如 `https://arc.example.com/api/v1`。`token_env` 填环境变量**名称**，实际值须与服务端认可的管理 Token 一致，设置方法见 [API 鉴权说明](docs/api-auth.md)。
4. 保存配置，在更新页填写本次导入 version，执行 01，检查日志并保存打开的工作区 songlist。如还需补资源，可换来源目录后继续 01；确认完整后再执行 02。
5. 在下方录入已知定数，选择 03 操作并执行。验收成功后点击“提交并清理…”。不需要 API 操作时，将其全部关闭并明确执行跳过，再确认提交。

只使用 01 时可以暂不填写 SSH/API。配置、背景对应表可直接编辑 JSON；工作区和任务进度保存在 `local.release_root`，无需依赖窗口一直开着。

## 收集曲目预处理

支持嵌套目录、目录名与 ID 不一致、slst 别名、可恢复的单曲 JSON、单张曲绘补缩略图、MP3/WAV 主音频转 OGG、忽略 Arcade 工程和排除默认空 AFF。标准 ID 采用 songlist.id，只有工作副本改名并报告 INFO；缺失、空或不合法的 ID 报 ERROR，不从目录名推断。空谱按内容判断，短谱不会仅因体积小而被排除；已声明但没有实际谱面的难度保留，rating 统一为 0。

已有总表可按精确 ID 补全缺失信息；新曲目描述缺失、同 ID 多来源或资源冲突会列为待处理，不凭空生成作者、定级和预览时间。详见[预处理规则](docs/preprocessing.md)。

目前版本导入 version 必填，01 生成草案时设置本轮曲目顶层的 version，包括原始未填写的情况；不为 `difficulties` 新增或强制覆盖 version，已存在的难度 version 保留。同时设置 `remote_dl: true`。审核时可修正逐曲 version，02 使用已保存值，允许同批多种版本；构建前也可调整导入默认值，无需因此重做 01，且不会批量覆盖审核总表。这个值用于 songlist；bundle 应用版本单独设置，命名规则见下文。背景对应表首次从 **所选bundle目录（active）** 的 `songs/songlist` 初始化，按无向关系一一配对；配置中只写一次 `"A": "B"`，即可从任意一边查到另一边。未明确填写 `bg_inverse` 时优先查表，再尝试 `_light` / `_conflict` 后缀规则，有歧义则 WARNING 提醒人工确认。

[config.example.json](config.example.json) 包含当前维护的背景配对，可直接查看、载入并按自己的资源调整；不包含背景图片。其本地路径、服务器地址均为示例。

各配置项旁的 **ⓘ** 提供用途和填写示例。SSH 用于传文件，API 用于刷新资源；旧 Web 管理页的 Cookie 与 API Token 分别鉴权。`token_env` 填本机环境变量名称，实际凭据放在变量值中；概念与只读验证步骤见 [API 鉴权说明](docs/api-auth.md)。

额外 WAV 特殊音效不进入 bundle 或服务端下载资源，报告 WARNING，谱面引用保留供人工处理。图片支持 JPG、JPEG、PNG 来源，在工作副本中转为真实 JPEG，透明区域以白色合成；只有主曲绘时自动补齐 384×384 缩略图。原图保持不变。音频后缀与实际编码不符时仍不做专门识别或修复。

INFO 表示可自动处理且无需人工检查；WARNING 表示可继续处理但需人工知悉或审核；ERROR 表示无法自动决定。扫描会收集其他曲目的问题，任一 ERROR 都会令本批次无效；WARNING 本身不阻断预检。

## 三阶段更新

1. **准备并审核（01）**：扫描所有输入曲目，增量准备服务端下载文件及 bundle 曲绘、背景和预览，自动合并总 songlist、输出冲突报告；准备结束后用记事本打开**本批工作区**的 songlist，交由人工检查和保存。此阶段不改原 active、本地 server 或 VPS。
2. **打包上传（02）**：校验人工保存的 songlist、输入和资源基线，按需重建预览、生成 bundle，先安装本地，再通过 SSH 上传核验。完成后标记“已上传，待 03”，完整工作区继续保留；失败时复用原构建继续。
3. **服务端与提交（03）**：按选项刷新资源、补购买、写定数、刷新评分。操作完成后验收，点击“提交并清理…”才销毁临时工作区。API 操作可以部分或全部明确跳过；最终提交仍需确认。定数暂缺时保留已有值，新增记录用 -1；修改已完成任务的定数或选项后，需重新执行 03。详见[03 操作](docs/server-operations.md)。

**未开始打包时，01 可以反复追加到同一个批次。** 换目录或在原目录放入新资源后再次执行 01，会保留先前已准备的曲目、已保存的人工 songlist 修改及 packlist / unlocks，合入新曲目或新难度；总表路径不变，idx 不重复分配。追加前先保存并关闭正在编辑的总表，完成后重新打开。各轮可调整导入 version，应用版本选项保持一致，直到确认完整后再执行一次 02。需要独立更新时点击“新建批次…”，原批次仍可从工作区管理中继续。

人工审核优先于再次扫描的原始单曲 songlist，既保留改过的值，也保留主动删除的字段，并在后续多轮 01 中持续生效。只保存原始曲目包中的 songlist 不等于审核工作区总表；01 打开和 02 使用的是 `<release_root>/<批次>/prepared/active/songs/songlist`。

01 完成后可在 `<批次>/prepared/active/` 自由新增、替换和删除其他资源，不设置资源文件白名单。02 记录相对原 active 的差异，使用最终副本打包，再按同一清单更新原 active；删除和覆盖前备份文件。人工替换的 `preview.ogg` 会保留，继续追加 01 也保留人工资源改动。构建完成后固定快照，重试不接受新的资源修改；版本历史仍由 Bundler 维护。

下方 03 页包含“曲目定数”和“购买项预览”。01 后即可离线分析：按最终 songlist 推断单曲 100、曲包 500，合并同包引用，显示价格、原价、关联物品、数量和可用状态。双击条目可自定义本批 ID、类别、价格，或选择跳过、恢复自动；修改保存在任务中，不改 songlist。无法推断（例如有意填写的字符串 `"null"`）和人工跳过的条目会在执行前弹窗确认；确认后记录 WARNING 并继续其他操作。支持重新分析和导出清单；实际 API 执行仍需完成 02，已有远端购买项保持原配置。

追加发生 ERROR 时保留原工作区并阻止 02，请修复该待追加目录后重试；不会静默只发布此前成功的部分。02 开始打包后固定本批构建，不能继续追加。详细规则见[工作区管理](docs/workspaces.md)。

关闭后通过“工作区管理…”继续原批次，可恢复配置、资源、定数草稿和操作进度。缺少 `workspace.json` 的旧批次只要报告有效也可恢复，继续保存时补建标记。首次 01 会复制并校验完整 active，因此耗时还取决于已有资源大小和磁盘速度；追加时复用工作区内未变化的资源，界面日志显示当前阶段及耗时。

“未提交”表示整批流程尚未验收完成。02 已经会安装与上传文件，03 也可能完成部分 API 操作；放弃工作区不会撤销这些已执行的变更。手动放弃需要单独确认。

songlist 冲突保留原值并报告，供审核时修改；非标准尺寸背景会直接缩放为 1920×1440，不裁剪。完整阶段边界见[三阶段更新流程](docs/update-workflow.md)。

bundle 采用两种命名规则：

| 模式 | JSON 版本 | 文件名前缀 |
| --- | --- | --- |
| 默认沿用当前应用版本 | 保留历史中的 `applicationVersionNumber`，递增 `versionNumber` | 本次 `versionNumber`，如 `7.0.256.5` |
| 手动切换应用版本 / 首次构建 | `applicationVersionNumber` 和 `versionNumber` 都使用新应用版本 | 新应用版本加 `.0`，如 `7.0.257.0` |

每包生成同前缀 `.cb` 与 `.json`。承接包保留上一包的 `previousVersionNumber`；之后沿用新应用版本继续生成 `.1`、`.2`。配置中的 `bundle.follow_app_version` 默认为 `true`，取消勾选后使用 `bundle.app_version`。首次构建没有历史，需手动填写应用版本；已与当前相同的手动版本提示改用沿用模式。校验成功后产物才就位，同名目标已存在则拒绝覆盖。

## 曲目如何分流

输入目录可直接包含曲目文件夹，例如 `incoming/example/`，也可有多层外包装目录；预处理按 songlist.id 识别曲目并生成标准工作副本。完整新曲目一般包含数字 `.aff`、`base.ogg`、`base.jpg`、`base_256.jpg` 和 songlist；已有曲目允许只提交新增文件及 songlist 补丁。

| 输入内容 | bundle 资源包侧 | 服务端 `latest version/database` 侧 |
| --- | --- | --- |
| 曲目 songlist | 合并至 `songs/songlist`，新曲目 `idx = max(idx) + 1` | 当前不自动写入 `songs/songlist` |
| 曲绘 JPG | `songs/dl_<id>/`，随后规范化名称与尺寸 | 不保留 |
| 自定义背景图片 | 1920×1440 → `img/bg/1080/`；1440×1080、1280×960 → `img/bg/`；其他尺寸缩放为 1920×1440 后进入 `1080/` | 不保留 |
| `base.ogg`、`0–4.ogg` | 生成 `preview.ogg` 或 `<难度>_preview.ogg`；非 `3.ogg` 的独立难度音频报告兼容性 WARNING | `songs/<id>/` 保留完整音频 |
| `0–4.aff` | 不保留 | `songs/<id>/`；只对实际缺失的 `0–2.aff` 补占位 |
| `video_1080.mp4` | 不保留 | `songs/<id>/` 原样保留 |

增量更新保留已有文件及未提交的字段、难度，不以原始目录替换整个目标目录。详细输入约定与例子见[曲目资源规则](docs/song-packages.md)。

背景导入前先检查 `img/bg` 与 `img/bg/1080` 两处全部文件，按原名及归一后的 JPG 名称避开已有背景；存在则跳过并报告 INFO，再对新背景按尺寸分流。其他尺寸自动缩放为 1920×1440，进入 `img/bg/1080/` 并报告 INFO。若同一曲目包同时提供两张背景，且原 songlist 明确声明其 `bg` / `bg_inverse` 关系，成功预处理或 01 后会自动记入本地配置并报告 INFO；保留已有配对，冲突时报 WARNING。只读扫描及失败批次不保存配对。songlist 引用的背景若既不在已有目录，也不由本批次引入，则报 WARNING。

## 可选命令行预检

需要 Python 3.10+。在本项目目录执行：

```powershell
python -m pip install -e .
Copy-Item config.example.json config.local.json
# 修改 config.local.json 中的 local 路径及 import.version
python -m arcaea_uploader plan --config config.local.json > plan.local.json
```

`plan` 使用 `local.input_root`、`assets_root`、`server_project_root`、`database_relative_path`、`placeholder_aff` 及 `import` 规则。相对路径以配置文件所在目录为基准。FFmpeg 用于音频转换与预览，SSH 用于 02 上传，API 用于 03；只读预检不连接 VPS，也不要求配置凭据。旧配置中的 `bundler_entry`、`bundler_root`、`media_script` 在载入时忽略，保存时移除，其余配置保留。

报告包含 songlist 合并草案、字段冲突、资源去向、下载文件哈希差异、占位谱面和预览任务。图片/音频条目标为 `prepare_media`，尚未转换，不能视为最终上传差异。缺失或冲突会报告，`publishable` 始终为 `false`，必须经过后续人工审核和执行阶段。

退出码：`0` 表示无阻断错误（仍可能有 WARNING），`1` 表示存在资源等 ERROR，`2` 表示配置或总 songlist 无效。songlist 字段冲突保留原值并报告 WARNING，由人工审核。预检不执行自动覆盖。

## 集成工具

- [Arcaea-Bundler](https://github.com/Lost-MSth/Arcaea-Bundler)：生成 `.cb` 与配套 `.json`，维护 `metadata.oldjson` 增量历史。MIT 许可，内置与现有本地副本完全相同的固定版本，保留作者 Lost-MSth 的版权声明和完整许可证。
- [Arcaea-server](https://github.com/Lost-MSth/Arcaea-server)：下载资源目录、bundle 版本索引及刷新管理 API。
- 内置媒体模块：由维护者提供的 `resize_and_preview.py` 整理而来，保留原始快照，修复缩略图漏改名、预览重建和难度查找问题；完整源音频由调用方保留。
- SSH / SFTP：通过 SSH 连接 VPS，端口默认 `22`；服务器项目根路径可指定，采用私钥认证并核对主机指纹。

行为依据本地副本核对，详见[工具调查](docs/existing-tools.md)。运行验证：

```powershell
python -m unittest discover -s tests -v
```

FFmpeg 在 PATH 中时会运行真实音频集成测试；也可设置 `ARCAEA_UPLOADER_TEST_FFMPEG` 指定测试用可执行文件。没有 FFmpeg 时仅跳过该集成测试。

CI 在 Windows / Linux 与 Python 3.10 / 3.13 检查代码，并验证 Python 分发包内容。Windows 便携版的构建和 Release 流程已配置；推送主分支不会自动生成新 Release，需另行更新版本并推送对应标签。

## 第三方授权

内置 Bundler 的原作者为 **Lost-MSth**，许可证为 **MIT**，原 `main.py` 未修改。来源提交、文件校验和、适配边界及分发时保留许可证的方式见[组件整合说明](docs/integrated-tools.md)和[第三方声明](THIRD_PARTY_NOTICES.md)。窗口右上角“关于与授权”可以查看完整 Bundler 许可证。

本项目自有代码采用 [MIT 许可证](LICENSE)，Copyright (c) 2026 StarSkyer。Bundler 保留 Lost-MSth 的原始 MIT 许可证与版权声明。Python 安装包包含这些许可文件；Windows 便携包同时带有 `licenses/` 和 `THIRD_PARTY_LICENSES.json`，保留实际随包 Python、Tcl/Tk、Pillow 等组件的许可证。

## 公开仓库范围

仓库保存 Uploader 代码、文档与脱敏配置示例。原始曲目、assets、生成的工作区、服务端数据库和凭据不进入仓库。第三方代码保持其原始许可与版权声明。

问题与建议通过 [Issues](https://github.com/starskyer/Arcaea-Uploader/issues) 提交；开发环境、贡献与版本发布分别见 [CONTRIBUTING](CONTRIBUTING.md)、[发布维护说明](docs/releasing.md)、[更新记录](CHANGELOG.md)。安全问题见 [SECURITY](SECURITY.md)。
