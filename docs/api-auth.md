# API、HTTPS 与 Token 环境变量

Uploader 的网络配置分为两组：SSH 负责传文件，API 负责让运行中的服务端重新读取资源索引。HTTPS 提供加密传输和服务器证书验证；API Token 提供管理操作的身份凭据。知道 HTTPS 地址或能用 SCP 登录 VPS，并不会自动取得 API 管理权限。

## 服务端在哪里配置 API_TOKEN

在正在运行的 Arcaea-server 项目中，编辑 `latest version/config.py`，找到 **`class Config` 内部**的设置：

```python
class Config:
    # 其他既有配置……
    API_TOKEN = "替换为自己生成的随机Token"
```

该字段的原注释为“API接口完全控制权限Token，留空则不使用”。它是自己设置的一段静态凭据，不由 Web 管理账号的密码推导，也无需先登录领取。可在终端生成一个随机值，再将输出填入上述字段：

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

服务端启动时，`main.py` 的 `_load_user_config()` 读取 `Config` 类，再由 `ConfigManager.load()` 将类属性覆盖到默认配置中。若进程环境设置了 `ARCAEA_SERVER_CONFIG`，优先读取该路径或模块；否则使用项目中的 `config.py` / `config` 包。需要修改服务实际加载的那一份；只修改 Windows 本机副本不会改变 VPS 的配置。

修改后重启应用进程才会重新加载。以使用 systemd 的部署为例：

```bash
sudo systemctl restart arcaea-server.service
```

资源刷新操作只重建资源缓存，不会重新读取这个配置字段。`API_TOKEN` 留空只表示未启用静态全权限 Token，API 路由和数据库账号登录机制仍然存在。

`api/api_auth.py` 的 `role_required()` 将请求头 `Token` 与非空 `Config.API_TOKEN` 直接比较；匹配后调用 `APIUser.set_role_system()`，具有完整管理权限。因此同一静态 Token 可用于资源刷新、歌曲信息和购买信息等接口，无需逐项配置凭据。这个流程不需要创建 API 用户或修改其角色。

客户端配置与服务端的对应关系如下：

| 位置 | 内容 |
| --- | --- |
| VPS 的 `Config.API_TOKEN` | 自己生成的实际 Token |
| Uploader 的 `api.token_env` | 本机环境变量名称，例如 `ARCAEA_UPLOADER_API_TOKEN` |
| 本机同名环境变量的值 | 与 VPS 设置完全相同的实际 Token |
| HTTP 请求头 | `Token: <实际 Token>` |

设置完成后使用后文的只读 GET 验证。HTTP 200 且 `code=0` 才确认成功；仅改完文件、尚未重启或客户端的值不匹配时不能视为已就绪。

## Web 管理页与 API 的区别

Arcaea-server 自带的旧 Web 管理页通过 `/web/login` 登录，浏览器保存 Cookie/session。刷新曲目和 bundle 的网页按钮使用该登录状态调用 Web 路由。

`/api/v1` 使用另一套鉴权：调用方在 HTTP 请求头中携带 `Token`。非空 `Config.API_TOKEN` 可作为管理 Token；也可使用具有 `system` 权限的 API 用户登录 Token。旧 Web 页的 Cookie 不会自动变成 API Token，给 Uploader 填上 Web 页地址也不会复制浏览器登录状态。

两套入口最终调用相同资源刷新操作，但登录凭据不同。API 用户重新登录或退出可能替换、撤销其登录 Token，因此不能假定浏览器曾经登录过就有永久可用的 API Token。

## 能否用账号密码取得 Token

可以，服务端提供 `POST /api/v1/token`。请求 JSON 为 `{"auth": "<账号:密码的 Base64 文本>"}`，成功响应的 `data.token` 是后续使用的 Token。但这里验证的是游戏数据库 `user` 表里的用户及其权限；旧 Web 页验证 `Config.USERNAME` / `Config.PASSWORD`，两份凭据分别维护，即使用户名都叫 `admin`，密码也不保证相同。

账号登录返回 401、`code=-201` 表示这组凭据未被 API 接受；不能据此断言旧 Web 页的密码错误，也不能从网页登录 Cookie 换取 API Token。成功领取 Token 后，还应请求 `GET /api/v1/system/operations`，确认具有资源刷新需要的 `system` 权限；仅取得 Token 不代表具备管理权限。

因此有两条可用路径：配置非空静态 `API_TOKEN`，或者使用已有/另行配置的、具有 `system` 权限的 API 用户。后者可以由 Uploader 在后台登录取得 Token，无须用户手动维护环境变量。当前界面仍使用 Token 环境变量方式，尚未提供 API 账号登录输入框；旧 Web 页账号不能直接替代这项配置。

API 账号的权限保存在数据库的 `user_role`、`role_power` 等表中，通过 `APIUser.select_role_and_powers()` 读取，不在 Web 的 `USERNAME` / `PASSWORD` 配置里。这份源码的默认初始化数据中，`system` 角色具有全部权限，而 `admin` 角色不包含 `system` 或 `change`；实际部署可调整这些数据，不能仅凭账号或角色名判断能否刷新和修改资源。

## base_url 与 token_env

| 配置项 | 填写内容 | 示例 |
| --- | --- | --- |
| `api.base_url` | 管理 API 的 URL 前缀 | `https://arc.example.com/api/v1` |
| `api.token_env` | 运行 Uploader 的电脑上的环境变量名称 | `ARCAEA_UPLOADER_API_TOKEN` |
| 同名环境变量的值 | 服务端认可的实际管理 Token | 自己配置或取得的真实值 |

环境变量可理解为操作系统为进程提供的一组“名称 → 值”。`token_env` 让程序按指定名称查找值，避免把实际凭据存入 `config.json`。例如配置里保存 `"token_env": "ARCAEA_UPLOADER_API_TOKEN"`，程序读取 `os.environ["ARCAEA_UPLOADER_API_TOKEN"]`，将取出的值放入 HTTP `Token` 请求头。

此处采用的是名为 `Token` 的自定义请求头，值直接为 Token；不额外拼接 `Bearer `。环境变量不是 HTTPS 协议的一部分，命名也不会在服务器上自动创建 Token。

## 在本机验证

先从自己的服务端配置取得已有非空 `API_TOKEN`，或通过具备相应权限的 API 账户取得登录 Token。若服务端没有配置、也没有现存有效 Token，就没有可直接读取的值；需要先单独完成服务端鉴权配置。不要把 SSH 密码填入这里。

以下命令在 PowerShell 内临时设置变量，示例中的值需换成真实管理 Token：

```powershell
$env:ARCAEA_UPLOADER_API_TOKEN = '替换为真实管理Token'
$headers = @{ Token = $env:ARCAEA_UPLOADER_API_TOKEN }
Invoke-RestMethod -Method Get -Uri 'https://arc.example.com/api/v1/system/operations' -Headers $headers
python -m arcaea_uploader
```

GET 仅列出当前账户允许访问的系统操作，不会刷新资源。HTTP 200 且响应 `code=0` 才表示这一接口的鉴权成功。401 表示缺少或无效 Token，403 表示权限不足。用户明确执行 03 时才会使用对应 POST 操作，并分别核验曲目、bundle 的刷新结果。

`$env:...` 只影响当前 PowerShell 及从它启动的子进程。从另一个已打开的窗口或资源管理器双击程序，不会获得这个临时值。运行便携 EXE 时也需在同一窗口用其实际路径启动。关闭此 PowerShell 会结束这次临时设置；也可执行 `Remove-Item Env:ARCAEA_UPLOADER_API_TOKEN` 清除。

03 已使用管理 API 执行用户确认的操作；预处理、资源检查和工作区恢复仍不要求 Token，也不会自行调用 API。Windows 进程没有同名变量时，会读取已保存的当前用户环境变量；进程内显式设置的值优先。

## 03 使用的管理接口

03 按用户选择调用下列入口；鉴权配置完成后，具体输入、顺序、确认与重试规则见[服务端操作](server-operations.md)：

| 操作 | `/api/v1` 下的入口 | API 账号所需权限 |
| --- | --- | --- |
| 刷新曲目与 bundle 缓存 | `POST /system/operations/<operation_name>` | `system` |
| 新增 / 修改歌曲信息及定数 | `POST /songs`、`PUT /songs/<song_id>` | `change` |
| 新增 / 修改购买信息 | `POST /purchases`、`PUT /purchases/<purchase_name>` | `change` |

静态全权限 `API_TOKEN` 覆盖上述操作。SSH 文件上传属于 02；上述写入操作由用户明确执行 03 时触发。
