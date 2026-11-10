"""Contextual desktop help, shared by home and configuration controls."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldHelp:
    title: str
    description: str
    example: str


FIELD_HELP = {
    ("workflow", "stages"): FieldHelp(
        "三阶段更新与辅助操作",
        "目标流程：选择曲目目录、填写本次导入 version → 01 准备并审核 → "
        "人工检查并保存 songlist → 02 打包上传 → 03 操作与确认提交。\n\n"
        "01 会自动包含预处理和资源检查，无需提前点击两个辅助按钮。"
        "未开始打包时，再次 01 追加到当前批次，可换目录补充曲目并调整导入 version。"
        "追加前先保存并关闭 songlist，已保存的人工修改和此前资源会保留；需要独立更新时点击“新建批次”。"
        "预处理曲目用于单独查看标准化结果；检查资源用于单独查看增量计划和诊断。\n\n"
        "标准化结果保存在发布工作区，原始曲目包保持不变。"
        "02 将使用审核后保存的总 songlist，不会重新合并覆盖你的编辑；"
        "完成后先安装到本地服务端，再同步 VPS。\n\n"
        "03 按顺序刷新 bundle、songs、world map，补缺少的购买项，写入人工指定的定数，"
        "购买项预览可双击编辑本批 ID、类别和价格；特殊或跳过条目执行前弹窗确认，确认后仅跳过购买项。"
        "最后刷新成绩评分。定数可填 0 / -1 表示不计评分，或留空保留已有值；"
        "界面和 API 使用真实定数，数据库存十倍整数（-1 对应 -10）。API 项目可以逐项或全部明确跳过。"
        "提交清理后的归档只读；再次调整可点击“创建后续任务…”，新任务独立保存，成功后自动归档。"
        "02 或 03 操作完成都先保留工作区，最终验收并确认提交后才清理。\n\n"
        "01 自动预处理并准备审核副本；保存后执行 02，先安装本地再 SSH 上传核验。03 的 API 操作可选，最终提交需确认；也支持手动上传的独立任务。",
        "incoming → 工作区与审核 → 本地 database / VPS → 03 → 确认提交\n"
        "下方“03 · 定数与服务端”页可双击填写定数，保存后可跨会话继续。",
    ),
    ("import", "version"): FieldHelp(
        "本次导入 version",
        "必填。01 生成草案时，用此值设置本轮导入曲目顶层的 version，包括原本缺少 version 的情况。"
        "不会新增或覆盖难度中的 version。未导入曲目保持原值，已保存的人工审核优先。\n\n"
        "02 开始构建前可修正此默认值，无需重做 01。02 使用工作区 songlist 中逐曲保存的 version，"
        "允许各曲目不同；修改此输入框不会批量覆盖已审核总表。\n\n"
        "这是 songlist 中的曲目版本标记；bundle 的版本号和文件名由打包历史另行计算。",
        '7.0 → 导入条目写入 "version": "7.0"',
    ),
    ("bundle", "follow_app_version"): FieldHelp(
        "沿用当前应用版本",
        "默认勾选：读取 active/metadata.oldjson 中最新 bundle 对应的应用版本，"
        "锁定应用版本输入框，下一包按历史递增。\n\n"
        "客户端应用版本升级时取消勾选，手动填写新应用版本。"
        "此时生成 <新应用版本>.0 承接包。重新勾选会保留手动填写的草稿。",
        "沿用：7.0.256.4 → 7.0.256.5.cb / .json\n"
        "改为应用版本 7.0.257：7.0.257.0.cb / .json",
    ),
    ("bundle", "app_version"): FieldHelp(
        "应用版本",
        "勾选沿用时显示从打包历史读取的值。取消勾选后可填写新的应用版本，"
        "使用以点分隔的非负整数。首次打包没有历史时，也需要手动填写。\n\n"
        "切换应用版本时，bundle JSON 的 applicationVersionNumber 和 versionNumber "
        "都设为新版本，文件名额外加 .0。",
        "输入 7.0.257 → 7.0.257.0.cb 与 7.0.257.0.json",
    ),
    ("local", "server_project_root"): FieldHelp(
        "本地 Arcaea-server 项目",
        "选择本机 Arcaea-server 项目的根目录。程序会在此目录下拼接"
        "“本地 database 相对路径”，找到 bundle 和 songs。\n\n"
        "预检只读取本地资源；01 在工作区准备，02 先更新本地副本，再上传 VPS。",
        "D:\\Arcaea\\Arcaea-server\n"
        "对应数据库：D:\\Arcaea\\Arcaea-server\\latest version\\database",
    ),
    ("local", "assets_root"): FieldHelp(
        "待打包资源（active）",
        "选择完整资源包目录，其中通常有 char、img、songs、tl 和 metadata.oldjson。"
        "此目录提供已有资源、总 songlist 和打包历史。\n\n"
        "它与只存放本批原始曲目的输入目录分开设置。Bundler 已内置，"
        "无需选择 Bundler 项目路径。",
        "D:\\Arcaea\\active\n总表：D:\\Arcaea\\active\\songs\\songlist",
    ),
    ("local", "input_root"): FieldHelp(
        "待更新曲目目录",
        "选择本次收集曲目的根目录，下面可以放多首曲目，支持继续扫描嵌套文件夹。"
        "标准 ID 优先使用每首曲目 songlist 中的 id。\n\n"
        "预处理只在工作副本中整理名称、曲绘和音频，保留此处原始文件。"
        "目标流程中，01 会自动执行预处理和资源检查。",
        "D:\\Arcaea\\incoming\n"
        "  ├─ song_a\\2.aff、base.ogg、base.jpg、songlist\n"
        "  └─ song_b\\…",
    ),
    ("local", "release_root"): FieldHelp(
        "发布工作区",
        "存放每次处理的独立工作副本和报告，避免修改原始曲目包。"
        "预处理后无需把这里的文件搬回输入目录；同一会话中，"
        "配置匹配的工作副本会供后续检查使用。\n\n"
        "“工作区管理”可查看历史批次、修复原始包后继续处理，或手动清理。"
        "跨次继续时会核验源文件、配置与副本哈希，复用未改变的成功曲目。"
        "失败或中断的批次保留，不会因关闭窗口自动删除。\n\n"
        "01 在此准备资源并打开待审核总 songlist；"
        "未开始打包时可反复 01 追加，总表路径不变；追加失败需修复后重试。"
        "审核阶段也可自由修改 prepared/active 的其他资源，02 记录差异并同步新增、替换和删除；"
        "人工替换的预览音频保留。"
        "02 使用已保存的审核结果打包，先安装到本地 database，再上传 VPS。"
        "02 上传后继续保留所有文件。server-task.json 保存定数、购买候选和 03 进度；"
        "03 完成并最终确认提交后才清理，完成摘要与任务归档保留在 .history / .server-tasks。",
        "D:\\Arcaea\\releases\n每次处理在其中建立独立批次目录。",
    ),
    ("ssh", "host"): FieldHelp(
        "SSH 服务器地址",
        "填写 VPS 的 IP 或域名。SSH 用于连接服务器、传输 bundle 和下载资源，"
        "与你此前用 SCP 上传文件的连接信息对应。端口单独填写。\n\n"
        "02 使用这些设置上传；辅助预处理、检查与 01 准备不会连接 VPS。",
        "arc.example.com 或 192.0.2.42\n此处直接填写主机名，无需 https://。",
    ),
    ("ssh", "port"): FieldHelp(
        "SSH 端口",
        "填写服务器 SSH 服务监听端口，默认 22，允许 1–65535。"
        "与 SCP 连接使用的 SSH 端口相同。",
        "22；服务器改过端口时，例如 2222",
    ),
    ("ssh", "username"): FieldHelp(
        "SSH 用户名",
        "登录 VPS 的系统账户名。该账户需要能读取并写入远端项目的 database 目录。",
        "ubuntu\n相当于 scp 中 ubuntu@arc.example.com 的 ubuntu。",
    ),
    ("ssh", "server_project_root"): FieldHelp(
        "服务器上的项目路径",
        "填写 VPS 上 Arcaea-server 项目的完整路径，使用服务器的 Linux 路径格式。"
        "程序会在其后拼接“远端 database 相对路径”。",
        "/home/ubuntu/Arcaea-server\n"
        "对应数据库：/home/ubuntu/Arcaea-server/latest version/database",
    ),
    ("ssh", "private_key_path"): FieldHelp(
        "SSH 私钥",
        "选择本机用于登录 VPS 的私钥文件，服务器需已授权对应公钥。"
        "常见为 .pem 或 id_ed25519；与 SCP 的 -i 参数用途相同。\n\n"
        "该文件只用于 SSH 登录。资源刷新 API 的 Token 需要另行设置。",
        "D:\\Keys\\server.pem 或 ~/.ssh/id_ed25519",
    ),
    ("ssh", "known_hosts_path"): FieldHelp(
        "已知主机文件（known_hosts）",
        "本机保存 SSH 服务器身份公钥的文件，用于判断连接的是否为已确认的 VPS。"
        "它与用于登录的私钥是两个文件。\n\n"
        "可选择已有的 known_hosts；服务器重装导致身份变化时，需要核对新指纹。",
        "~/.ssh/known_hosts\nWindows 常见位置：C:\\Users\\你的用户名\\.ssh\\known_hosts",
    ),
    ("api", "base_url"): FieldHelp(
        "服务端管理 API 地址",
        "SSH / SCP 负责把文件传上去；API 是服务端提供的 HTTP 接口，"
        "用于让运行中的服务端重新读取曲目和 bundle 资源索引。\n\n"
        "填写对外可访问的管理 API 前缀，一般到 /api/v1，"
        "带上实际使用的 http:// 或 https:// 以及必要端口。"
        "具体刷新接口会在此前缀下调用。\n\n"
        "03 执行时使用该配置调用资源、购买项和定数接口；预处理和检查无需填写。",
        "https://arc.example.com/api/v1\n"
        "自定义端口示例：http://192.0.2.42:8080/api/v1",
    ),
    ("api", "use_system_proxy"): FieldHelp(
        "API 使用系统代理",
        "默认勾选，兼容已有行为：API 请求跟随系统或环境变量中的 HTTP(S) 代理。"
        "取消勾选后，仅本工具的管理 API 使用直连，无需关闭整机代理。\n\n"
        "如果自有服务器可直接访问，而系统代理导致连接慢或偶发断开，可取消勾选。"
        "此设置不改变 API 目标、Token、SSH 连接或其他程序的代理。"
        "已有工作区会恢复自己的配置，请在继续工作区后核对这里的开关。",
        "自有 VPS 可直连：取消勾选 → 保存配置 → 继续原任务的 03",
    ),
    ("api", "token_env"): FieldHelp(
        "Token 环境变量名",
        "这里填写运行 Uploader 的电脑上的环境变量名称。"
        "实际 Token 保存在该环境变量的值中，避免把凭据写进 config.json。\n\n"
        "执行 03 时读取这个变量的值，并通过 HTTP 的 Token 请求头"
        "发送给服务端。静态 Token 在服务端 config.py 的 Config 类内填写 API_TOKEN，"
        "自行设置随机值并重启服务后生效；客户端环境变量使用同一个值。"
        "也可以使用具有 system 权限的 API 用户登录取得的 Token。"
        "SSH 密码和私钥分别管理。\n\n"
        "旧 Web 管理页使用 Cookie/session 登录，网页能刷新并不代表已配置 API Token。"
        "API 登录验证数据库里的用户与权限，和 Web 管理账号分别维护；"
        "即使都叫 admin，密码也不保证相同。"
        "HTTPS 负责加密传输，Token 仍需独立取得或配置。\n\n"
        "环境变量要对 Uploader 进程可见。在 PowerShell 临时设置时，"
        "需从同一窗口启动 Uploader；通过系统环境变量设置后，重新启动程序。"
        "当前预处理和检查不需要 Token。",
        "本栏填写：ARCAEA_UPLOADER_API_TOKEN\n"
        "同名环境变量的值：你的服务端管理 Token\n\n"
        "PowerShell 临时设置示例：\n"
        '$env:ARCAEA_UPLOADER_API_TOKEN = "你的服务端管理 Token"\n'
        "python -m arcaea_uploader",
    ),
    ("local", "database_relative_path"): FieldHelp(
        "本地 database 相对路径",
        "本机项目根目录到 database 的相对路径。"
        "保持默认值即可匹配常见 Arcaea-server 目录结构。",
        "latest version/database\n"
        "与本地项目 D:\\Arcaea\\Arcaea-server 拼接后：\n"
        "D:\\Arcaea\\Arcaea-server\\latest version\\database",
    ),
    ("local", "placeholder_aff"): FieldHelp(
        "占位谱面",
        "服务端曲目下载目录需要至少具备 0.aff、1.aff、2.aff。"
        "缺失难度会用此占位内容补齐，已有谱面保持原样。\n\n"
        "留空时从本地项目的 database/songs/sayonarahatsukoi/0.aff 推导；"
        "也可选择自备占位文件。",
        "D:\\Arcaea\\Arcaea-server\\latest version\\database\\songs\\sayonarahatsukoi\\0.aff",
    ),
    ("local", "ffmpeg_path"): FieldHelp(
        "FFmpeg",
        "外部音频处理程序，用于将主音频 MP3 / WAV 转为 OGG，并生成 preview.ogg。"
        "Uploader 内置媒体处理逻辑，但没有捆绑 FFmpeg。\n\n"
        "留空时从 PATH 查找 ffmpeg；也可选择 ffmpeg.exe 文件。"
        "谱面引用的额外 WAV 音效需要人工处理，不会自动作为主音频导入。",
        "D:\\Tools\\ffmpeg\\bin\\ffmpeg.exe\n若 ffmpeg 已在 PATH 中，本栏可留空。",
    ),
    ("ssh", "database_relative_path"): FieldHelp(
        "远端 database 相对路径",
        "VPS 项目根目录到 database 的相对路径。与本地路径独立设置，"
        "按服务器上的实际目录填写。",
        "latest version/database\n"
        "与 /home/ubuntu/Arcaea-server 拼接后：\n"
        "/home/ubuntu/Arcaea-server/latest version/database",
    ),
    ("import", "background_pairs"): FieldHelp(
        "背景翻转对应表",
        "保存背景 ID 的无向一一配对关系，确认一组后两边都能互推。"
        "首次配置 active 后会从 songs/songlist 的现有明确配对初始化。\n\n"
        "01 或预处理成功后，原曲目包明确声明且同时提供两张图片的新配对会自动记入配置并提示 INFO；"
        "冲突保留已有记录，不会自动覆盖。\n\n"
        "每组只需写一次，同一个 ID 只能对应一个背景。"
        "编辑后先应用对应表，再保存配置，配对会写入配置文件。",
        '{\n  "example_light": "example_conflict"\n}\n'
        "同时支持 light → conflict 和 conflict → light。",
    ),
}
