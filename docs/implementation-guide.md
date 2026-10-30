# glasscast 实现指南

> **先读这里**：这是 2026-10-23～10-24 写的原始设计规格，按当时的任务分工（任务 A～G）组织，后面几轮变更（§9、§10）只追加、不回改前文，所以前面的接口、页面写法和 clip 模式很多已经被推翻；文中的文件名和路径（例如 `pages/cast_clip/`、`deploy/openresty/…`、`cast-cert-renew.*`）也是当时的，后来有的移走或改了名，以仓库里的实际文件为准。**glasscast 现在的行为和用法以 [README.md](../README.md) 和 [docs/platform-setup.md](platform-setup.md) 为准**；真机上的最终结论见 [docs/research.md](research.md) §1 开头。代码注释里的"实现指南 §x"指本文。

把电脑画面投到千问 AI 眼镜屏幕上的工具链。两套投屏方式共用一个进程：

- **image 模式（方案三）**：眼镜页面每秒拉一张 JPEG。
- **clip 模式（方案一）**：眼镜页面循环拉 `.fset` 帧合集，播完一段自动拉下一段。

调研结论见 `research.md`（必读，尤其 §2～§4）。官方文档见 <https://device.qianwen.com/docs>，需要查 Waft-DSL 细节时查第 31～36 节（UI页面开发），工具接入查第 37 节（章节编号按 2026-10 的文档目录顺序）。

本指南是实现者的唯一规格。有歧义时按"先跑通、可测试、少做假设"取舍，并把假设写进代码注释和 `docs/`。

## 1. 架构

```
电脑 (macOS)                                                 公网 HTTPS                  眼镜 / 平台
┌──────────────────────────────────────────┐   反向 SSH 隧道 / 临时隧道   ┌──────────────┐
│ glasscast serve  (一个进程，一个端口)       │ ─────────────────────────► │ OpenResty    │ ◄── 平台云端调用 MCP（start_cast）
│  ├ capture 线程：mss 截屏 → 缩放到 640×290  │                            │ cast.example…│ ◄── 眼镜 GET screen.jpg / clip.fset
│  ├ 编码：JPEG（image 模式）/ PNG 帧 → .fset │                            └──────────────┘
│  ├ HTTP：/s/<key>/screen.jpg, /s/<key>/clip.fset, /healthz
│  └ MCP：/mcp（streamable HTTP，Bearer）     │
└──────────────────────────────────────────┘
```

- MCP 工具只做控制面：`start_cast` 返回图片/帧文件的公网地址并打开采集，`stop_cast` 关闭。逐帧刷新由眼镜页面自己完成（`<tick>` 或 `on-completed` 链式切段）。
- 图片地址带一段随机 `key`（眼镜发不了请求头），MCP 用 Bearer Token。
- 所有响应 `Cache-Control: no-store`、`Access-Control-Allow-Origin: *`、`Cross-Origin-Resource-Policy: cross-origin`（后两者给本地模拟器用）。

## 2. 仓库布局与分工

```
glasscast/
  pyproject.toml              # 已建好：uv 项目，Python 3.12，依赖见其中
  README.md                   # 任务 B
  glasscast/
    __init__.py               # 已建好
    fset.py                   # 任务 A  .fset 读写
    frames.py                 # 任务 A  截屏源、缩放/裁剪、编码
    pipeline.py               # 任务 A  采集线程 + 最新帧/最新段
    config.py                 # 任务 B  配置（CLI + 环境变量）
    server.py                 # 任务 B  Starlette 应用 + 路由
    mcp_tools.py              # 任务 B  FastMCP 工具
    cli.py                    # 任务 B  `glasscast` 命令行入口
  tests/
    fixtures/genie_ball.fset  # 原稿：官方真实样本（32×30，48 帧 PNG）；公开版不分发，改用自制的 fixtures/sample.fset
    test_fset.py, test_frames.py, test_pipeline.py        # 任务 A
    test_server.py, test_mcp.py, test_cli.py              # 任务 B
  pages/
    cast-image/template.xml, data.json                    # 任务 C
    cast_clip/template.xml,  data.json                    # 任务 C
  skill/
    SKILL.md, ui_schema.json                              # 任务 C
  docs/
    research.md, implementation-guide.md                  # 已有
    platform-setup.md                                     # 任务 C：平台上逐步操作手册
    deploy.md                                             # 任务 D
  deploy/
    openresty/cast.example.com.http.conf / .https.conf # 任务 D
    server-setup.sh, cast-cert-renew.service/.timer, renew-cert.sh  # 任务 D
    tunnel.sh                                             # 任务 D：电脑侧反向 SSH 隧道
  tools/
    simulator/fetch_assets.sh, index.html, harness.js, serve.py, README.md  # 任务 E
  reference/                  # 已有：调研时的实验服务器、实验页面（说明见 reference/README.md）
```

任务 A～E 的文件互不重叠，每个任务只创建/修改自己名下的文件；依赖统一写在 `pyproject.toml` 里。

## 3. 任务 A：核心库

### 3.1 `glasscast/fset.py`

格式（已实测，见 research.md §4）：

```
uint32 LE N
N × (uint32 LE offset, uint32 LE size, uint32 LE reserved=0)
帧数据首尾相接，第一帧 offset = 4 + 12N
```

接口：

```python
@dataclass(frozen=True)
class FsetEntry: offset: int; size: int; reserved: int

def build_fset(frames: Sequence[bytes]) -> bytes
    # frames 非空；输出满足上面布局；reserved 写 0。

def parse_fset(data: bytes) -> list[bytes]
    # 校验：N>0；每项 offset/size 在文件内；首项 offset == 4+12N；各帧连续（offset_i+size_i == offset_{i+1}）；
    # 不连续或越界抛 FsetError（ValueError 子类）。

def inspect_fset(data: bytes) -> dict
    # {"frames": N, "header_bytes": 4+12N, "total_bytes": len, "frame_sizes": [...], "formats": ["png"|"jpeg"|"unknown", ...],
    #  "png_dims": [(w,h),...]}  —— PNG 从 IHDR 取宽高；JPEG 只标 "jpeg"。
```

测试：fixture 解析得 48 帧、全 PNG、全部 32×30、header 580、连续；`build_fset(parse_fset(x)) == x`（字节级回放）；构造 1 帧/3 帧回放；损坏头（N=0、offset 越界、不连续）抛 `FsetError`。

### 3.2 `glasscast/frames.py`

```python
CANVAS = (640, 290)   # 默认画布，等于 S1 一屏预算（见 research.md §2.3）

class FrameSource(Protocol):
    def grab(self) -> PIL.Image.Image   # RGB

class ScreenSource(FrameSource):
    def __init__(self, monitor: int = 1, region: tuple[int,int,int,int] | None = None)
    # 用 mss。region=(left, top, width, height) 屏幕坐标（逻辑像素；mss 在 Retina 上返回物理像素，自行换算或直接用 mss 的 monitor 字典）。
    # mss 实例不能跨线程共享：在调用线程内惰性创建。

class StaticSource(FrameSource):      # 测试用：固定图或序列
class SyntheticSource(FrameSource):   # 测试/演示用：画时间戳和计数器，便于肉眼看刷新

def fit_to_canvas(im, canvas=CANVAS, mode: Literal["fit","crop"]="fit", bg=(0,0,0)) -> Image
    # fit：等比缩放后居中贴到 canvas 黑底（letterbox）；crop：先按 canvas 比例居中裁剪再缩放。输出尺寸恒等于 canvas。
    # 缩放用 Image.Resampling.LANCZOS；输入先 convert("RGB")。

def encode_jpeg(im, quality=70) -> bytes          # 关闭 progressive；可选 subsampling=0 提升文字清晰度（默认 2）
def encode_png(im, compress_level=3) -> bytes       # 速度优先
def frame_digest(im) -> str                        # 缩到 64×32 灰度再 hash，用于"画面没变就不重编码"

def check_budget(width, height, nbytes) -> list[str]
    # 返回告警：w*h*4 > 742_400 或 nbytes > 1_048_576（research.md §2.3）
```

测试：fit/crop 输出恒为 640×290；极端宽高比不崩；JPEG/PNG 可被 Pillow 重新打开；`check_budget` 边界；`frame_digest` 对同图稳定、对不同图不同。截屏类用 `StaticSource` 代替，不要在测试里真的截屏（CI/沙箱无权限）。

### 3.3 `glasscast/pipeline.py`

```python
@dataclass
class Snapshot: seq: int; ts: float; data: bytes; width: int; height: int; kind: Literal["jpeg","png"]
@dataclass
class Clip:     seq: int; ts: float; data: bytes; frames: int; frame_duration_ms: int; width: int; height: int

class CastPipeline:
    def __init__(self, source: FrameSource, *, canvas=CANVAS, fit_mode="fit",
                 image_fps=1.0, jpeg_quality=70,
                 clip_fps=5.0, clip_frames=10, clip_format: Literal["png","jpeg"]="png",
                 skip_unchanged=True)
    mode: Literal["off","image","clip"]      # start(mode)/stop() 切换；线程常驻，off 时空转
    def start(self, mode) ; def stop(self) ; def close(self)
    def latest_snapshot(self) -> Snapshot | None
    def latest_clip(self) -> Clip | None
    def stats(self) -> dict   # mode, seq, frames_captured, encode_ms_avg, last_frame_age_s, clip 相关
```

行为：

- 一个守护线程按当前模式的节拍截帧：image 模式按 `image_fps`；clip 模式按 `clip_fps`。`skip_unchanged` 为真且 digest 未变时，image 模式复用上一张 JPEG 但仍更新 `seq/ts`（眼镜每秒都要有东西可取）；clip 模式照常把帧加入段（段长固定）。
- clip 模式：攒满 `clip_frames` 帧后 `build_fset` 发布为 `latest_clip`，然后开始攒下一段；**只保留最新一段**（眼镜拉"最新"，不排队，避免延迟累积）。`frame_duration_ms = round(1000/clip_fps)`，上限 1000（Waft-DSL 限制）。
- 所有异常在线程内捕获、计数并退避 1 s，不能让线程死掉。
- 模式切换时清空攒帧缓冲。

测试：用 `SyntheticSource` + 很高的 fps 跑几百毫秒，断言 snapshot 更新、clip 帧数正确、`parse_fset` 能解析、stop 后不再更新、stats 字段齐全。测试要确定性地等待（轮询带超时），不要 `sleep` 猜时间。

## 4. 任务 B：服务、MCP、CLI

### 4.1 `glasscast/config.py`

`Settings` dataclass，来源优先级：CLI 参数 > 环境变量 `GLASSCAST_*` > 默认值。字段：`host="127.0.0.1"`, `port=8766`, `public_url`（必填：对外可达的 HTTPS 根地址，例如 `https://cast.example.com`，末尾无斜杠）, `token`（MCP Bearer；默认从 `GLASSCAST_TOKEN` 读；为空则生成并打印一次）, `cast_key`（图片路径里的随机段，默认启动时生成 16 位 urlsafe）, `monitor=1`, `region=None`, `fit_mode="fit"`, `canvas=(640,290)`, `image_fps=1`, `jpeg_quality=70`, `clip_fps=5`, `clip_frames=10`, `clip_format="png"`, `source="screen"|"synthetic"`（后者演示/联调用，不需要屏幕权限）。

### 4.2 `glasscast/server.py`

`create_app(settings, pipeline) -> Starlette`。路由：

| 方法/路径 | 行为 |
|---|---|
| `GET /healthz` | `200 ok` |
| `GET /s/{key}/screen.jpg` | key 不对 → 404。无帧时返回"等待画面"占位 JPEG（用 `SyntheticSource` 画一张，同尺寸）。头：`Content-Type: image/jpeg`, `Cache-Control: no-store`, `ETag: "<seq>"`, `X-Glasscast-Seq`, `X-Glasscast-Age`（秒） |
| `GET /s/{key}/clip.fset` 及 `GET /s/{key}/clip/{n}.fset` | 最新完整段；`n` 仅用于眼镜端防缓存，服务端忽略。无段时返回 1 帧占位 .fset（帧数仍补齐到 `clip_frames`，重复占位帧），保证页面 `total-frames` 恒成立。头：`Content-Type: application/octet-stream`, `X-Glasscast-Frames`, `X-Glasscast-Frame-Duration-Ms`, `Cache-Control: no-store` |
| `GET /s/{key}/status.json` | `pipeline.stats()` + 当前 URL |
| `POST /mcp` 等 | FastMCP streamable HTTP（见 4.3），挂载在 `/mcp`；Bearer 校验中间件：`Authorization: Bearer <token>` 不匹配 → 401 |

通用响应头：`Access-Control-Allow-Origin: *`, `Cross-Origin-Resource-Policy: cross-origin`。忽略 query string。错误不泄露 key。

### 4.3 `glasscast/mcp_tools.py`

已装的是官方 `mcp` SDK **2.3.0**（`FastMCP` 已改名）：`from mcp.server.mcpserver import MCPServer`；`server = MCPServer("glasscast", instructions=...)`；工具用 `@server.tool()` 注册；`server.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True, transport_security=TransportSecuritySettings(...), host=...)` 返回一个 Starlette 子应用，挂到主应用上；`server.session_manager` 只有在调用过 `streamable_http_app()` 之后才能访问，要把 `session_manager.run()` 接进主应用的 lifespan，否则第一次请求 500。**DNS 重绑定保护**：平台请求的 Host 是 `cast.example.com`（本地测试是 `127.0.0.1:8766`），必须用 `transport_security` 显式允许这些 Host（或关闭该保护），否则平台会收到 421。迁移说明：<https://py.sdk.modelcontextprotocol.io/v2/migration/>。工具：

```python
start_cast(mode: Literal["image","clip"] = "image") -> dict
  # 打开采集，返回：
  # {"success": true, "mode": "image",
  #  "url": "<public_url>/s/<key>/screen.jpg",          # image 模式页面用
  #  "clip_url": "<public_url>/s/<key>/clip.fset",      # clip 模式页面用
  #  "width": 640, "height": 290,
  #  "clip_frames": 10, "frame_duration_ms": 200,
  #  "message": "投屏已开始，每秒刷新一次"}
stop_cast() -> {"success": true, "message": "投屏已停止"}
cast_status() -> {"success": true, "active": bool, "mode": ..., "last_frame_age_s": ..., "seq": ...}
```

要求：工具描述（docstring）用中文写清触发场景，因为平台把描述给大模型做工具选择；返回体必须是 JSON 对象（平台 `ui_schema.json` 的条件 `success == true` 读的是它）；任何工具都要在 1 s 内返回（平台 3 s 超时）。

### 4.4 `glasscast/cli.py`

`glasscast serve [--public-url URL] [--port N] [--host H] [--token T] [--source screen|synthetic] [--monitor N] [--region L,T,W,H] [--fit fit|crop] [--image-fps F] [--jpeg-quality Q] [--clip-fps F] [--clip-frames N] [--clip-format png|jpeg] [--autostart image|clip]`
启动时打印：公网地址、`/s/<key>/screen.jpg` 完整地址、MCP 地址、Bearer（仅当是自动生成时），以及 macOS 屏幕录制权限提示（首次截屏会弹系统授权，拒绝后 mss 抓到黑屏或报错）。

`glasscast fset build OUT.fset IMG...`、`glasscast fset inspect FILE.fset`、`glasscast fset extract FILE.fset OUTDIR`。

`glasscast snapshot OUT.jpg [--region ...]`：截一张并按画布处理，用来调分辨率/裁剪区域。

`pyproject.toml` 已声明 `[project.scripts] glasscast = "glasscast.cli:main"`。

测试：Starlette `TestClient` 覆盖全部路由与错误分支（用 `SyntheticSource`）；MCP 用 `mcp` 的 streamable HTTP 客户端走 ASGI（若 SDK 版本不便，退化为直接调用工具函数并单独测 Bearer 中间件）；CLI 用 `subprocess` 跑 `--help` 和 `fset inspect fixture`。

## 5. 任务 C：眼镜页面、Skill 文件、平台操作手册

### 5.1 `pages/cast-image/template.xml`

```xml
<view xmlns:a="waft://dsl" width="100%" height="100%" flex-flow="column" bg-color="#000000">
  <page-meta screen-awake="keep"/>
  <tick var="t" duration="3600" refresh="second"/>
  <image src="{{url}}?t={{tick:t}}" width="100%" height="100%" image-fit="stretch"/>
</view>
```

`data.json`：`{"url": "https://cast.example.com/s/REPLACE_KEY/screen.jpg"}`（Mock 用，真实地址运行时由工具出参提供）。

### 5.2 `pages/cast_clip/template.xml`

```xml
<view xmlns:a="waft://dsl" width="100%" height="100%" flex-flow="column" bg-color="#000000">
  <page-meta screen-awake="keep"/>
  <data i="0"/>
  <frame-set src="{{clip_url}}?i={{d:i}}"
             width="640" height="290"
             total-frames="10" frame-duration="200"
             repeat-count="1" color-format="0" load-async="true"
             on-completed="data://update?i={{d:i + 1}}"/>
</view>
```

`total-frames` / `frame-duration` 必须与服务端 `clip_frames` / `frame_duration_ms` 一致（默认 10 / 200）。在文件顶部加 XML 注释说明这一点。`data.json`：`{"clip_url": "https://cast.example.com/s/REPLACE_KEY/clip.fset"}`。

两份 XML 都要按文档 §34（页面开发指南）自检：根 view 100%×100% 且显式 `flex-flow`；属性引号、标签闭合；URL 里不要写 `&amp;`（第一个参数用 `?`，所以这里不涉及）。

### 5.3 `skill/SKILL.md`

按文档第 13 节"Skill规范"写：frontmatter `name: 电脑投屏`，`description` 写清触发语句（"开始投屏""把电脑投到眼镜上""停止投屏""投屏状态"）和 Do NOT use。正文流程：

1. 用户要求投屏 → 调用 `^^PLUGIN_CODE.start_cast^^`（`mode` 默认 `image`；用户说"流畅/视频模式"则 `clip`）。
2. 成功（`success == true`）→ 页面由 `ui_schema.json` 的 Hook 自动渲染（见 5.4）；语音只回一句"投屏已开始"。失败 → 把 `message` 念给用户。
3. "停止投屏" → `^^PLUGIN_CODE.stop_cast^^`。
4. "投屏状态" → `^^PLUGIN_CODE.cast_status^^`，把 `last_frame_age_s` 等说成人话。

`PLUGIN_CODE` 是平台注册 MCP 插件后分配的编码，手册里写明替换方法（编辑器里输入 `@` 选工具会自动插入正确编码）。

### 5.4 `skill/ui_schema.json`

按文档第 31 节 §3.4 格式：顶层数组；一个元素 `name: "PLUGIN_CODE.start_cast"`；`hooks`：

- `hook-image`：`stage: "OUTPUT"`，`condition: "success == true && mode == 'image'"`，`intercepted: false`，`actions: [{"type":"RENDER","templateName":"cast-image"}]`
- `hook-clip`：同上，`mode == 'clip'` → `cast_clip`

页面里的 `{{url}}` / `{{clip_url}}` 对应工具出参字段名（OUTPUT 阶段根对象 = 工具出参）。

### 5.5 `docs/platform-setup.md`

逐步操作手册（给人看，带每一步在控制台的位置）：

1. 本地启动 `glasscast serve --public-url …`，确认 `https://…/healthz`。
2. 控制台 → 资源库 → MCP 插件 → 创建 MCP 插件：名称 `电脑投屏`、基础 URL `https://cast.example.com`、认证 Bearer Token（填 glasscast 打印的 token）、传输 streamable http、endpoint `/mcp` → 创建后"刷新 MCP 工具"，应看到 3 个工具 → 逐个"调试"通过 → 发布插件。
3. 控制台 → Skill 管理 → 打开「电脑投屏实验」（或新建空白 Skill）→ 文件树新建 `Pages/cast-image/template.xml` 粘贴保存，`data.json` 粘贴保存；同样建 `cast_clip`。注意 `meta.json` 由平台生成，不要手工建。
4. 把 `skill/SKILL.md` 内容贴进 SKILL.md（用 `@` 把 `PLUGIN_CODE.start_cast` 等替换成真实工具引用）；新建根目录 `ui_schema.json` 粘贴（同样替换 `PLUGIN_CODE`），保存时平台会校验模板名存在。
5. 页面预览：打开 `template.xml` → 右上"预览" → 设备选 S1 → Mock 里把 `url` 改成真实地址 → 观察每秒刷新与控制台告警（图片超预算会告警）。
6. 调试（沙箱）→ 真机测试（先在「设备管理」里绑定自己的眼镜、开启开发者模式）→ 发布（个人私有，绑定目标设备）。
7. 常见问题：MCP 刷新不到工具（公网 HTTPS、HTTP/3 关闭、3 s 超时）、图片不显示（预算、地址必须 https、`?t=` 必须变化）、页面不刷新（tick duration 归零）。

## 6. 任务 D：公网暴露（服务器 `server.example.com`）

服务器环境见 research.md §7。主方案 **反向 SSH 隧道**：电脑运行 `deploy/tunnel.sh`（`ssh -N -o ServerAliveInterval=15 -o ExitOnForwardFailure=yes -R 127.0.0.1:18766:127.0.0.1:8766 server.example.com`，循环重连），服务器 OpenResty 把 `cast.example.com` 反代到 `127.0.0.1:18766`。

产出：

- `deploy/openresty/cast.example.com.http.conf`：80 端口，只放行 `/.well-known/acme-challenge/`（root `/usr/share/nginx/html`），其余 301 到 https。
- `deploy/openresty/cast.example.com.https.conf`：443 `ssl http2`（不要 http3），证书路径 `/www/sites/cast.example.com/ssl/{fullchain,privkey}.pem`，`server_tokens off`，安全头用 HSTS、`X-Content-Type-Options`、`Referrer-Policy`；`location = /healthz`、`location ^~ /s/`（`proxy_read_timeout 10s`，`proxy_buffering off`）、`location ^~ /mcp`（`proxy_http_version 1.1; proxy_set_header Connection ""; proxy_read_timeout 30s; proxy_set_header Authorization $http_authorization`），其余 404；`client_max_body_size 64k`。
- `deploy/server-setup.sh`：在服务器上以 root 跑的幂等脚本：建 `/opt/glasscast/{letsencrypt}` 与 `/opt/1panel/apps/openresty/openresty/www/sites/cast.example.com/{log,ssl}`；先装 http conf 并 `openresty -t && -s reload`；用 `docker run --rm -v /opt/glasscast/letsencrypt:/etc/letsencrypt -v /opt/1panel/apps/openresty/openresty/root:/var/www/acme certbot/certbot:latest certonly --webroot --webroot-path /var/www/acme -d cast.example.com --agree-tos -m <email> --non-interactive` 签证书；复制证书到 ssl 目录；装 https conf；reload；装 renew 脚本和 systemd timer（见 `deploy/renew-cert.sh`）。脚本开头检查 DNS 已解析到本机公网 IP，否则退出并提示先在域名服务商处加 A 记录 `cast.example.com → 203.0.113.10`。
- `deploy/tunnel.sh` 与 `deploy/README`（写在 `docs/deploy.md`）：三种暴露方式的取舍——(T) 反向 SSH 隧道（主）；(P) 不加域名，挂在 `app.example.com/cast/` 路径下（给出要追加的 location 片段，但说明会和另一个服务共用 vhost）；(Q) `cloudflared tunnel --url http://127.0.0.1:8766` 临时隧道（仅测试，地址每次变）。

本任务只产出文件和文档；登录服务器跑脚本、改 DNS 由部署的人按 `docs/deploy.md` 手动完成。

## 7. 任务 E：本地模拟器工具（尽力而为）

目标：不登录平台也能用真实渲染内核验证页面。

- `tools/simulator/fetch_assets.sh`：把 research.md §3 表里 4 个文件下载到 `tools/simulator/assets/`（`.gitignore` 排除），校验大小大致合理。
- `tools/simulator/serve.py`：静态服务器，给所有响应加 `Cross-Origin-Opener-Policy: same-origin` 和 `Cross-Origin-Embedder-Policy: require-corp`（pthreads 需要），并把 `/assets/` 映射到下载目录；`/proxy?url=` 可选的图片代理（取远程图并加 CORP 头），用于加载 glasscast 之外的地址。
- `tools/simulator/index.html` + `harness.js`：一个 `<canvas>`；左边文本框贴 XML 与 data JSON，按钮"渲染"；日志区。按编辑器预览的调用方式驱动内核：`import('./assets/index.blob-worker.js')` → `createWaftLite({canvas, locateFile: f => f === 'waft-lite-wasm.data' ? '/assets/waft-lite-wasm.data' : '/assets/' + f, initTimeoutMs, print, printErr})`，再 `inject(xml, JSON.stringify(data), uri)`，`uri` 用 `waft://lite/dsl/glasscast/<page>`。内核入口 `index.blob-worker.js` 必读（12 KB，导出名和 `ccall` 用法都在里面）。
- `tools/simulator/README.md`：怎么起、怎么验证两个页面、已知限制。把摸清的内核 API（函数名、事件名、`ccall` 签名）记进去，哪怕页面最终没跑通也要记录卡在哪一步。

## 8. 验收

- `uv run pytest` 全绿；`uv run glasscast serve --source synthetic --public-url http://127.0.0.1:8766 --autostart image` 后 `curl /healthz`、`curl -o a.jpg /s/<key>/screen.jpg`、`curl -o a.fset /s/<key>/clip.fset && uv run glasscast fset inspect a.fset`（10 帧 PNG 640×290）。
- MCP：用 `mcp` 客户端（或 `curl` 发 `initialize` + `tools/list`）带 Bearer 能列出 3 个工具；无 Bearer 得 401。
- 两份页面 XML 通过本地模拟器渲染（任务 E 跑通时）或至少通过"页面开发指南"的自检清单。
- 代码风格：类型标注、无全局可变状态（pipeline 对象显式传入）、中文注释说明平台约束来源（引用 research.md 的小节号）。

## 9. 第二轮变更（v2，依据 research.md §9 的模拟器实测）

实测推翻了 clip 模式的前提：`<frame-set>` 的 `src` 只在创建时求值，换段不可能。替代做法是把帧合集当**定时器**，驱动 `<image>` 亚秒级轮询（research.md §9.2 E4）。同时屏幕是单色绿，图片改用灰度。本轮改动按任务划分：

### 9.1 任务 F：核心库与服务端

- `frames.py`：新增 `color_mode: Literal["gray","rgb"]="gray"`（`fit_to_canvas` 后 `convert("L")`；JPEG/PNG 编码按灰度输出）；新增 `build_timer_fset(frames=2, size=(8,8)) -> bytes`：2 帧 8×8 灰度 PNG 的 `.fset`，内容固定（可缓存），供页面当定时器用。
- `pipeline.py`：模式改为 `off | image | fast`；`fast` 模式只是 `image_fps` 更高（默认 4）且同样产出 JPEG；删除或保留 clip 帧合集逻辑都可以，但不再对外承诺（保留 `build_fset`/`parse_fset`，它们有单测且 `fset` 子命令在用）。
- `config.py` / `cli.py`：`--fast-fps`（默认 4）、`--color gray|rgb`（默认 gray）；`--autostart image|fast`。
- `server.py`：新增 `GET /s/{key}/timer.fset`（`Cache-Control: no-store` 可以省，但保持 CORS/CORP 头）；`clip.fset` 路由可保留为实验接口。
- `mcp_tools.py`：`start_cast(mode: "image"|"fast"="image")`，返回增加 `"timer_url": "<public_url>/s/<key>/timer.fset"` 与 `"frame_duration_ms"`（fast 模式下 `round(1000/fast_fps/2)`，即定时器每帧时长，2 帧一轮）；`mode` 字段回传实际模式；描述里说明 fast 模式更流畅但更吃带宽。
- 测试相应更新；`uv run pytest -q` 全绿。

### 9.2 任务 G：页面与平台文件

- 新增 `pages/cast-fast/template.xml`：

  ```xml
  <view xmlns:a="waft://dsl" width="100%" height="100%" flex-flow="column" bg-color="#000000">
    <page-meta screen-awake="keep"/>
    <data i="0" act="start"/>
    <frame-set src="{{timer_url}}" width="8" height="8" total-frames="2" frame-duration="{{frame_duration_ms}}"
               repeat-count="1" color-format="0" action="{{d:act}}"
               on-completed="data://update?i={{d:i + 1}}&act=start"/>
    <image src="{{url}}?ts={{f:timestamp()}}&i={{d:i}}" width="100%" height="100%" image-fit="stretch"/>
  </view>
  ```

  已在模拟器验证（research.md §9.2 E4/E9）：`frame-duration` 可以绑定数据（绑定 100 → 周期 0.219 s，绑定 400 → 0.82 s）；`{{f:timestamp()}}` 每次数据更新都重新取值，用它做防缓存（内核按完整 URL 缓存，同一内核里重开页面时 `i` 从 0 重数会命中旧缓存，所以不能只靠 `i`）。`cast-image` 页面同样把 `?t={{tick:t}}` 改成 `?ts={{f:timestamp()}}&t={{tick:t}}`。`data.json` 给出 `url`、`timer_url`、`frame_duration_ms` 三个 Mock 值。
- `pages/cast_clip/` 删除（或移到 `reference/experiments/` 并注明不可用）。
- `skill/ui_schema.json`：`mode == 'fast'` → `cast-fast`；`mode == 'image'` → `cast-image`。`skill/SKILL.md`：触发词区分"流畅模式/快速模式"→ `fast`。
- `docs/platform-setup.md` 同步。
- 用本地模拟器验证（`uv run python tools/simulator/serve.py` + glasscast `--source synthetic --autostart fast`，预设 `cast-image + 本机 glasscast` 的做法，`tools/simulator/README.md` §3.3）：`cast-fast` 的图片拉取间隔中位数应接近 `2 × frame_duration_ms`。把实测数字写进 `pages/cast-fast/template.xml` 顶部注释。

**审查后的修订（2026-10-24，以仓库里的文件为准，上面的原稿片段不再逐字有效）：**

- `cast-fast` 的 `on-completed` 改为 `data://update?i={{d:i + 1}}&amp;act=start`（官方文档第 36 节 §10.3 约束 2；模拟器复测循环照常，research.md §9.2 E4）。
- 两个页面的图片地址只用一个查询参数：`cast-fast` 为 `?ts={{f:timestamp()}}_{{d:i}}`，`cast-image` 为 `?ts={{f:timestamp()}}_{{tick:t}}`。图片地址里写 `&amp;` 会原样进 URL、写裸 `&` 又不是良构 XML，合成一个参数两头都不沾；服务端本来就忽略查询串（§4.2）。两份模板现在都是良构 XML（`tests/test_pages.py`）。
- 页面里的数据绑定都写成两段拼接：`{{url}}{{structuredContent.url}}`、`{{timer_url}}{{structuredContent.timer_url}}`、`{{frame_duration_ms}}{{structuredContent.frame_duration_ms}}`，同时兼容平铺出参和 MCP `CallToolResult` 包装两种 dataSource（官方文档第 37 节 §2.1；research.md §9.2 E10，三元表达式会把结果截断到 63 个字符，不能用）。`skill/ui_schema.json` 的条件同理改为 `(success == true && mode == 'fast') || (structuredContent.success == true && structuredContent.mode == 'fast')`（`image` 同理），取代 §5.4 的写法。
- `cast-image` 的 tick `duration` 改为 86400（约 24 h 后停刷，取代 §5.1 的 3600，见 docs/platform-setup.md Q6 第 1 条）；`cast-fast` 的 `<frame-set>` 加了 `floating="true"`（模拟器实测不加会把图片挤下去 24 像素，见模板注释）。
- §5.5 第 5 步作废：编辑器右上角的「预览」只是 XML 只读预览，不是模拟器。预览改用编辑器的「预览 & 调试」面板或本地模拟器（research.md §9.1；docs/platform-setup.md §7）。
- §2 布局里的 `pages/cast_clip/` 已移到 `reference/experiments/cast_clip/`（不可用，只留作模拟器对照）；§5.2 只作历史记录。

## 10. 第三轮变更（v3，2026-10-24）：帧率可调、公网链路、平台实况

### 10.1 帧率可调（已实现，469 项测试全绿）

- `glasscast/pipeline.py`：`CastPipeline.fast_fps` 可在运行中改（setter 立即重排截帧节拍；`parse_fast_fps` 校验 1～10、可带小数，超出一律拒绝不钳制；`fast_fps_warnings` 对过高帧率给提示；`FAST_FPS_MIN/MAX`、`FAST_FPS_RANGE_TEXT` 共用）。
- `glasscast/mcp_tools.py`：`start_cast(mode, fps=None)`；`mode=fast` 且给了 `fps` 时先 `set_fast_fps` 再开始，出参 `frame_duration_ms` 按新帧率计算，另带 `fps`、`fast_fps`；非法 `fps` 返回 `success=false` + 中文 `message`（含当前值），什么都不改；`mode=image` 时忽略 `fps` 并在 `message` 里说明。`cast_status` 多了 `fast_fps`、`fast_frame_duration_ms`。工具描述里写明语音用法（"投屏调到 N 帧""快一点""慢一点"）。
- `glasscast/server.py`：`POST /admin/fps`（`{"fps": 6}`）、`GET /admin/config`，Bearer 与 `/mcp` 相同；公网 vhost 对 `/admin/` 一律 404，只给本机用。uvicorn `timeout_keep_alive=7200`（见 10.2）。
- `glasscast/cli.py`：`glasscast set-fps N`、`glasscast config`（只连本机，读 `GLASSCAST_HOST/PORT/TOKEN`）；serve 启动横幅列出帧率与调节方式。
- `skill/SKILL.md` 模式四、`docs/platform-setup.md`、`README.md` 同步。

### 10.2 公网链路实测与修正

见 `docs/deploy.md` §2.8、§7：1Panel 网站 + Let's Encrypt；OpenResty upstream keepalive（同地址两个 peer、`non_idempotent` 重试）；uvicorn 空闲连接 2 小时；`deploy/tunnel.sh --warm-interval 30` 保温。`cast_status` 调试一次约 1.2 s，跨境链路上超时仍偶发（research.md §9.9）。

### 10.3 平台实况与阻塞

- 当时的插件还是草稿、未发布；`skill/SKILL.md`、`skill/ui_schema.json` 里的 `PLUGIN_CODE` 换成了平台分配的插件编码（公开版统一写成占位 `XXXXXX`，按 docs/platform-setup.md 换成你自己的）。SKILL.md 的三处工具引用在编辑器里用「工具」按钮插成卡片。
- **阻塞**：编辑器保存校验 `URI-001` 拒绝 `<frame-set>` 的 `action` 属性（`research.md` §9.4），帧合集定时器版 `cast-fast` 进不了库；平台上先存了过渡版 tick 页面（未随仓库提供）。`pages/cast-fast/template.xml` 保持定时器版不变，等校验问题解决（zip 导入 / 平台修复）后换回。之后流畅模式改走 emit:// 事件页面 `cast-emit`（research.md §9.8、§9.19），`cast-fast` 只在本地模拟器里用。
