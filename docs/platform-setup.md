# 千问平台操作手册：把「电脑投屏」装到眼镜上

本手册面向已经有**千问 AI 眼镜 S1** 和**千问 AI 硬件开放平台**（<https://device.qianwen.com>）账号的开发者，带你从零完成：

电脑上跑起 glasscast 并确认公网可达 → 注册 MCP 插件 → 建 Skill（两个眼镜页面 + SKILL.md）→ 预览与沙箱调试 → 真机测试 → 以「个人私有」发布到自己的眼镜。

> 控制台菜单和按钮名称按 2026 年 10 月的控制台写，平台改版后文字可能略有出入，按意思找即可。官方文档：<https://device.qianwen.com/docs>（MCP 插件、UI 页面、技能调试与发布等章节）。

## 工作方式

- 电脑上的 `glasscast serve` 截屏，压成很小的灰度 JPEG，放在 `https://cast.example.com/s/<KEY>/screen.jpg/<任意>.jpg`；同一个进程在 `/mcp` 提供 MCP 服务，工具是 `start_cast` / `stop_cast` / `cast_status`。
- 你对眼镜说「开始投屏」→ 平台大模型按 SKILL.md 调用 `start_cast` → 再调用平台内置的「UI页面渲染」工具打开眼镜页面，把返回的图片地址交给页面。
- 页面自己不停换地址下载新图：手机上的「千问AI穿戴」App 下载图片，经蓝牙传给眼镜解码显示。MCP 只负责开始、停止和查询，不参与刷新。

| 模式 | 对眼镜说 | 眼镜页面 | 真机速度（2026-10 实测） |
|---|---|---|---|
| 普通模式（默认） | 「开始投屏」 | `cast-image` | 约每秒 1 张，延迟 1～2 秒，最稳 |
| 流畅模式 | 「用流畅模式投屏」 | `cast-emit` | 帧率设置为 4 时约每秒 1.5 张；设置再高可能卡住（见 [Q5](#q5流畅模式画面卡住不动或者没有说的每秒-n-张那么快)） |

要粘贴到平台上的文件：

| 本仓库文件 | 平台上的位置 | 说明 |
|---|---|---|
| `pages/cast-image/template.xml` | `Pages/cast-image/template.xml` | 普通模式页面 |
| `pages/cast-image/data.json` | `Pages/cast-image/data.json` | 编辑器预览用的 Mock 数据 |
| `pages/cast-emit/template.xml` | `Pages/cast-emit/template.xml` | 流畅模式页面 |
| `pages/cast-emit/data.json` | `Pages/cast-emit/data.json` | 编辑器预览用的 Mock 数据 |
| `skill/SKILL.md` | 技能根目录 `SKILL.md` | 粘贴前把 `XXXXXX` 换成你的插件编码（第 7 步） |
| `skill/ui_schema.json` | 技能根目录 `ui_schema.json` | 内容就是 `[]`，保持为空（第 8 步） |

- **不要上传** `pages/cast-fast`（平台保存校验不放行，只能在本地模拟器里跑，见 [pages/README.md](../pages/README.md)）和 `reference/` 下的任何文件。
- 平台自动生成、**不要手工创建或编辑**的文件：`tool_schema.json`（保存 SKILL.md 时生成）、每个页面目录下的 `meta.json`（保存 `template.xml` 时生成）。

## 0. 准备

- 一副千问 AI 眼镜 S1，已在手机「千问AI穿戴」App 里绑定。真机测试用的是 iPhone，Android 没测过。
- 千问 AI 硬件开放平台账号（已完成开发者认证），眼镜出现在控制台「设备管理」里。
- 电脑：macOS（Linux / Windows 没测过），装好 [uv](https://docs.astral.sh/uv/)，已克隆本仓库并执行过 `uv sync`（见 [README](../README.md)）。
- 一个**公网 HTTPS** 域名指向电脑上的 glasscast，证书由正规 CA 签发（例如 Let's Encrypt，自签证书不行）。平台云端（调 MCP）和手机（下载图片）都要访问它，localhost、局域网地址它们都访问不到。搭法见 [docs/deploy.md](deploy.md)：主方案是反向 SSH 隧道 + 服务器上的 OpenResty / nginx；cloudflared 临时隧道只适合测试。
  - HTTP/2 没问题，但**不要对外宣告 HTTP/3**（响应头里不能有 `alt-svc: h3`），否则平台的客户端会升级到 HTTP/3 后连接失败。
  - 平台的工具调用超时约 **3 秒**，包括平台到你的服务器、再经隧道到电脑的全部往返。服务器离国内越近越好，跨境线路慢时会偶发超时（SKILL.md 会让模型自动重试一次，见 [Q2](#q2对话里提示mcp-工具执行超时或眼镜说电脑上的投屏程序没有响应)）。

下文的占位符换成你自己的值：

| 占位符 | 含义 | 从哪里来 |
|---|---|---|
| `cast.example.com` | 你的公网域名 | 你自己的域名 |
| `TOKEN` | MCP 的 Bearer Token | `.env` 里的 `GLASSCAST_TOKEN` |
| `KEY` | 图片地址里的随机段 | glasscast 启动时打印的 `…/s/<KEY>/screen.jpg` |
| `XXXXXX` | 你的 MCP 插件编码（6 个字符） | 第 7.2 步 |
| `/Users/you/glasscast` | 你克隆本仓库的目录 | 你自己的路径 |
| `REPLACE_KEY` | `pages/*/data.json` 里的 Mock 地址占位 | 只影响编辑器预览，换成 KEY 或不改都行 |

## 1. 电脑端：启动 glasscast

在项目目录里复制配置模板，最后一条生成一个 token：

```bash
cd /Users/you/glasscast
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

编辑 `.env`：`GLASSCAST_PUBLIC_URL` 改成 `https://cast.example.com`，`GLASSCAST_TOKEN=` 后面填上一步生成的 token。然后启动：

```bash
. ./.env && uv run glasscast serve
```

- `.env.example` 里的画面参数就是真机验证过的推荐值（`GLASSCAST_CANVAS=480x218`、灰度、`crop`、每张约 2.6 KB、流畅模式帧率 4），保持不动即可。代码默认的 640×290、质量 70 的图在真机上下载成功却不显示。
- **Token 必须固定**。不设 `GLASSCAST_TOKEN` 时每次启动都随机生成一个，平台里填的旧 token 立刻失效（表现为 401、工具数为 0）。Token 只放在 `.env` 和平台插件配置里，**不要**写进 SKILL.md、页面或 data.json。
- 启动输出里有图片地址 `https://cast.example.com/s/<KEY>/screen.jpg` 和 MCP 地址 `https://cast.example.com/mcp`。记下 KEY，第 2 步要用。KEY 每次启动都会变，这没关系：页面用的地址由 `start_cast` 在运行时返回。
- **macOS 屏幕录制权限**：系统设置 → 隐私与安全性 → 屏幕录制，给**启动 glasscast 的那个 App**（终端 / iTerm / VS Code……）打开开关，然后重启 glasscast。没有权限时只能截到黑屏或桌面壁纸。只想先联调平台，可以用合成画面：`. ./.env && uv run glasscast serve --source synthetic`。
- 另开一个终端，按 [docs/deploy.md](deploy.md) 打开公网通道（例如 `deploy/tunnel.sh`）。不想让电脑睡眠：再开一个终端运行 `caffeinate -d`。

## 2. 从公网验证（全部通过再去平台）

在项目目录下执行。最好在另一张网络上跑（例如手机热点）；服务器在海外时，用国内网络测出的耗时更接近平台的情况。先设好下面几项检查共用的变量，`KEY` 换成第 1 步记下的值：

```bash
. ./.env
B=https://cast.example.com
KEY=你的KEY
TOKEN="$GLASSCAST_TOKEN"
```

① 健康检查，输出 `ok`：

```bash
curl -sS "$B/healthz"; echo
```

② 协议，应看到 `HTTP/2 200`，且不应出现 `alt-svc` 行（尤其是 `h3`）：

```bash
curl -sSI --http2 "$B/healthz" | grep -i -E '^(HTTP|alt-svc)'
```

③ 图片，和眼镜页面一样用以 `.jpg` 结尾的路径，应返回 200、`content-type: image/jpeg`、`cache-control: no-store`。文件大小在推荐设置下约 2～3 KB；还没开始投屏时返回的是占位图，同样是 JPEG：

```bash
curl -sS -D - -o /tmp/glasscast-test.jpg "$B/s/$KEY/screen.jpg/test_1.jpg" | grep -i -E '^(HTTP|content-type|cache-control)'
ls -l /tmp/glasscast-test.jpg
```

④ MCP，带 Bearer 列工具，应看到 `start_cast`、`stop_cast`、`cast_status`；最后一行的耗时应远小于 3 秒：

```bash
curl -sS -X POST "$B/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  -w '\n耗时 %{time_total}s\n'
```

⑤ MCP，不带 Bearer，应返回 401：

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$B/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

| 检查 | 不通过时 |
|---|---|
| ① 不是 `ok` | 隧道 / 反代没通，或 glasscast 没在运行 |
| ② 出现 `alt-svc: h3` | 关掉反代 / CDN 的 HTTP/3（本仓库 `deploy/` 里的配置已关闭）。本仓库的配置对外是 HTTP/2；显示 HTTP/1.1 理论上也能用，但没在平台上测过。curl 报不支持 `--http2` 时去掉这个参数或换新版 curl |
| ③ 404 | KEY 不对（重启后会变），或反代没把 `/s/` 转发过来 |
| ④ 401 / 421 / 超时 | 401：token 不一致；421：`GLASSCAST_PUBLIC_URL` 的域名和你访问的域名不一致；耗时接近或超过 3 秒：见 Q2 |

glasscast 的 MCP 是**无状态**的（不需要先 `initialize`），所以上面可以直接 `tools/list`。想先看一眼 `start_cast` 的返回，可以把 ④ 的 `-d` 换成 `'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"start_cast","arguments":{"mode":"fast"}}}'`，看完再用同样方式调一次 `stop_cast`。

## 3. 注册 MCP 插件

控制台 → 左侧「资源库」→「MCP 插件」→「创建 MCP 插件」：

| 表单字段 | 填写 | 说明 |
|---|---|---|
| 插件名称 | `电脑投屏` | 建议不超过 16 个字 |
| 描述 | `把电脑屏幕投到眼镜：开始/停止投屏、切换普通或流畅模式、查询投屏状态` | |
| 服务地址 | `https://cast.example.com/mcp` | 表单分成「基础 URL」和「endpoint」两栏时：基础 URL 填 `https://cast.example.com`（末尾不带 `/`），endpoint 填 `/mcp`。挂在路径前缀下时（例如 `https://app.example.com/cast`），完整地址是 `https://app.example.com/cast/mcp` |
| 认证方式 | `Bearer Token`，值填 `TOKEN` | 只填 token 本体，平台自己加 `Bearer ` 前缀。**token 只填在这里** |
| 传输协议 | 「无状态协议」（stateless） | 每次调用只发一个请求，不先 `initialize`。选 Streamable HTTP 也能用，但每次调用多一次往返，更容易撞上 3 秒超时 |

保存时平台会连接服务、拉取工具列表。打开插件详情，点「刷新 MCP 工具」，应看到 **3 个工具**：`start_cast`、`stop_cast`、`cast_status`，状态为「草稿」。工具描述来自 glasscast 的代码，平台上改不了。工具数为 0 见 [Q1](#q1插件创建成功但工具数为-0--刷新不到工具)。

> **先别发布插件。** 草稿状态的插件就能被 Skill 引用、在沙箱和真机上测试；插件发布后会锁定、不能再改，改域名或 token 只能新建插件（插件编码也会变）。等第 11 步真机测通后再发布（第 12 步）。

## 4. 逐个调试工具

在插件详情里对每个工具点「调试」（期望值按 `.env.example` 的推荐设置）：

| 工具 | 入参 | 期望返回 |
|---|---|---|
| `start_cast` | `{"mode": "image"}` | `success: true`、`mode: "image"`、`url` 为 `https://cast.example.com/s/<KEY>/screen.jpg`、`width: 480`、`height: 218`、`message` |
| `start_cast` | `{"mode": "fast"}` | `success: true`、`mode: "fast"`、`url`（同上）、`hops: 12`、`fps: 4` |
| `start_cast` | `{"mode": "fast", "fps": 20}` | `success: false`、`message: "帧率只能调到每秒 1 到 10 张，可以带小数，现在是每秒 4 张"`；投屏状态和帧率都不变 |
| `cast_status` | `{}` | `success: true`、`active: true`、`mode`、`last_frame_age_s`、`fast_fps` |
| `stop_cast` | `{}` | `success: true`、`message: "投屏已停止"` |

- 每个工具调试成功一次，状态会变成「调试通过」。
- 调试面板的「工具返回」形如 `{"summary": {}, "response": {"isError": false, "content": [{"type": "text", "text": "{…}"}]}}`，字段都在 `content[0].text` 这段 JSON 文本里。这是正常的：平台只把这段文本交给模型。也正因为如此，页面不能靠 `ui_schema.json` 的 Hook 自动打开，而由模型按 SKILL.md 调用「UI页面渲染」打开（第 8 步）。
- 偶尔超时就再点一次调试；经常超时见 Q2。

## 5. 创建 Skill

控制台 →「Skill 管理」→「创建 Skill」→「空白文档」：

| 字段 | 填写 | 说明 |
|---|---|---|
| Skill 名称 | `电脑投屏` | 与 SKILL.md 开头的 `name:` 保持一致 |
| Skill 标识 | 例如 `glasscast-cast` | 字母、数字、中划线；**创建后不能改** |
| Skill 描述 | 复制 `skill/SKILL.md` 开头 `description:` 后面的整段 | 影响什么时候召回本技能 |

创建后进入技能编辑器：左边是文件树（`SKILL.md`、`Pages` 等），右上角有「调试」「真机测试」「发布」「版本」。

## 6. 创建两个眼镜页面

页面名就是 `Pages/` 下的文件夹名，必须分别叫 **`cast-image`** 和 **`cast-emit`**（SKILL.md 按这两个名字打开页面）。

1. 文件树里如果没有 `Pages` 文件夹，先新建文件夹 `Pages`。
2. 在 `Pages` 下新建文件夹 `cast-image`，**马上**在里面新建文件 `template.xml`（空文件夹刷新后会消失）。
3. 打开 `template.xml`，粘贴本仓库 `pages/cast-image/template.xml` 的全部内容 →「保存」。平台随后生成只读的 `meta.json`，其中 `templateName` 应为 `cast-image`。
4. 打开同目录的 `data.json`（没有就新建），粘贴 `pages/cast-image/data.json` 的内容 →「保存」。这是编辑器预览用的 Mock 数据，可以把里面的域名和 `REPLACE_KEY` 换成你的值；真正投屏时，页面数据来自 `start_cast` 的返回，与它无关。
5. 对 `cast-emit` 重复 1～4，内容来自 `pages/cast-emit/`。Mock 里的 `hops: 12` 对应帧率设置 4。

- 模板顶部的 `<!-- … -->` 注释可以一起粘贴，它不影响运行。
- 保存报错见 [Q8](#q8保存-templatexml-时报错)。不要建 `cast-fast`；编辑器 AI 生成的页面也不要用。

## 7. 粘贴 SKILL.md，把 XXXXXX 换成你的插件编码

### 7.1 工具卡片是怎么存的

SKILL.md 里引用工具用的是「工具卡片」。平台保存时把每张卡片存成 `^^插件编码.工具编码^^` 这样的纯文本，编辑器载入或粘贴时再把这段文本显示成卡片。仓库里的 `skill/SKILL.md` 就是这种文本，里面有两类引用：

- `^^XXXXXX.start_cast^^`、`^^XXXXXX.stop_cast^^`、`^^XXXXXX.cast_status^^`：你的「电脑投屏」插件的 3 个工具。`XXXXXX` 是占位，**必须换成你自己的插件编码**。插件编码是平台给每个插件分配的 6 个字符，每个人都不一样。
- `^^tQ8fCF.ui_page_render^^`：平台内置的「UI页面渲染」工具（编辑器「工具」按钮 → 官方镜端插件 → UI页面渲染），官方示例里用的也是这个编码，**保持不动**。

### 7.2 找到你的插件编码

1. 打开技能根目录的 `SKILL.md`，在末尾输入 `@`（或点编辑区的「工具」按钮）→ 展开「电脑投屏」→ 单击 `start_cast`，插入一张卡片 →「保存」。
2. 打开文件树里的 `tool_schema.json`（平台自动生成，只读），找到 `start_cast`，它的编码形如 `AbC123.start_cast`（`插件编码.工具编码`）。点号前面的 6 个字符 `AbC123` 就是你的插件编码。插入的卡片上也显示这个编码。

### 7.3 替换并粘贴

在电脑上把占位换掉，结果直接复制到剪贴板（仓库里的文件不变）。下面的 `AbC123` 换成你的编码；`grep -c` 那一行应输出 `0`，`pbcopy` 那一行是 macOS 的剪贴板命令：

```bash
cd /Users/you/glasscast
sed 's/XXXXXX/AbC123/g' skill/SKILL.md | grep -c XXXXXX
sed 's/XXXXXX/AbC123/g' skill/SKILL.md | pbcopy
```

1. 编辑器里打开 `SKILL.md` → Cmd+A 全选 → 删除 → Cmd+V 粘贴 →「保存」。第 7.2 步临时插的那张卡片也一起被替换掉了。
2. **刷新浏览器页面**，重新打开 `SKILL.md`：应看到 4 张卡片（`start_cast`、`stop_cast`、`cast_status` 和「UI页面渲染」），文中不应再有 `^^…^^` 文本。
3. 打开 `tool_schema.json`：应列出这 4 个工具。

- 整篇粘贴时编辑器偶尔报 `Illegal value for lineNumber`，卡片暂时没显示出来：文本已经正确保存，刷新页面即可。
- 「UI页面渲染」没有变成卡片：删掉那段 `^^…^^` 文本，用「工具」→ 官方镜端插件 → UI页面渲染重新插一张，后面的 `(templateName=…)` 保持不动。
- SKILL.md 里不要写 token、域名或 KEY，页面地址由 `start_cast` 在运行时返回。

SKILL.md 的要点（开始、打开页面、超时重试、停止的流程已在真机上验证；帧率预设是发布前改的，还没上真机跑过）：

- `start_cast` 成功后，模型必须立刻调用「UI页面渲染」：普通模式 `templateName="cast-image"`、`dataSource={"url": …}`；流畅模式 `templateName="cast-emit"`、`dataSource={"url": …, "hops": …}`，值原样照抄工具返回。
- 工具超时或平台提示「工具调用失败 / 工具执行失败 / MCP 工具执行超时」时，先不说话，用同样的参数重试一次；两次都失败才播报。
- 停止投屏只调 `stop_cast`，**不调**「UI页面渲染」（在真机上调用它会把投屏页面重新打开）。
- 说到帧率的（「投屏调到 N 帧」「快一点」「慢一点」「最流畅」「最省流量」）一律 `start_cast(mode=fast, fps=N)`，成功后带新的 `hops` 再打开一次 `cast-emit`。「最流畅」「最快」设成 4，「快一点」在当前值上加 1、最多到 4，「慢一点」减 1、最低到 1，「最省流量」设成 1；用户说出的具体数字照传。

## 8. ui_schema.json 保持为空

技能根目录的 `ui_schema.json` 内容就是 `[]`（本仓库 `skill/ui_schema.json`；平台上没有这个文件也可以）。

原因：`ui_schema.json` 的 OUTPUT Hook 在真机上从不命中（平台交给 Hook 的 MCP 出参只有 `content[0].text`），所以页面改由模型打开；两种机制同时开着还可能叠出两个页面。原来的 Hook 版本放在 `reference/ui-schema-hooks/`，只作参考，不要粘贴。

## 9. 预览（不用眼镜）

### 9.1 编辑器预览

打开技能编辑器的「预览 & 调试」面板，设备选「千问 AI 眼镜 S1」，选中要看的页面。

- 面板只渲染**已保存**的模板；编辑器右上角的「预览」只是 XML 的只读视图，不是模拟器。
- **编辑器预览显示不了远程图片**（2026-10 实测：图片经平台代理拉取失败，日志里是 `download failed`）。所以这里只能确认页面能打开、没有报错，`cast-emit` 右下角的换图编号会持续增加。这不代表真机不行，看真实画面请用下面的本地模拟器或真机。

### 9.2 本地模拟器（推荐）

`tools/simulator` 用编辑器预览同款的官方 WASM 渲染内核在本机渲染页面（内核由脚本从官方公开 CDN 下载，本仓库不分发）。它复现的是编辑器预览，不模拟手机下载和蓝牙传输，所以速度比真机快，只用来确认页面逻辑和画面。详见 [tools/simulator/README.md](../tools/simulator/README.md)。

首次运行先下载内核（约 26 MB）：

```bash
tools/simulator/fetch_assets.sh
```

终端 1 启动 glasscast，记下输出里的 KEY：

```bash
uv run glasscast serve --source synthetic --public-url http://127.0.0.1:8766 --autostart fast
```

终端 2 启动模拟器：

```bash
uv run python tools/simulator/serve.py
```

正式的 glasscast 也占着 8766 端口时，先停掉它，或者把终端 1 的命令改成 `--port 8767 --public-url http://127.0.0.1:8767`（下面 data 里的地址跟着改）。

1. 浏览器（Chrome / Edge）打开 `http://127.0.0.1:8770/?preset=page:cast-emit`（不要加 `autorender=1`，否则会先按 data.json 原样去拉占位地址）。
2. 把右侧 data 改成 `{"url": "http://127.0.0.1:8766/s/<KEY>/screen.jpg", "hops": 12}` → 点「渲染」。
3. 期望：画面持续更新，右下角换图编号递增；终端 1 的访问日志里是一行行 `GET /s/…/screen.jpg/<会话号>_<编号>.jpg 200`，编号逐个加一。
4. `cast-image` 同理：打开 `?preset=page:cast-image`，data 改成 `{"url": "http://127.0.0.1:8766/s/<KEY>/screen.jpg"}`，期望约每秒一个请求。

## 10. 沙箱调试

技能编辑页 → 右上角「调试」，在输入框里依次输入，并展开会话里的工具调用看入参和返回：

| 输入 | 期望 |
|---|---|
| `开始投屏` | `start_cast(mode=image)` → `UI页面渲染(templateName="cast-image", dataSource={"url": …})` → 回复「投屏已开始」 |
| `用流畅模式投屏` | `start_cast(mode=fast)` → `UI页面渲染(templateName="cast-emit", dataSource={"url": …, "hops": 12})` → 「已用流畅模式投屏」 |
| `投屏调到 3 帧` | `start_cast(mode=fast, fps=3)` → 返回 `hops: 17` → 再打开 `cast-emit` → 「已调到每秒 3 张」 |
| `慢一点`（当前帧率 3） | 先 `cast_status` 拿到 `fast_fps: 3` → `start_cast(mode=fast, fps=2)` → 「已调到每秒 2 张」 |
| `最流畅` | `start_cast(mode=fast, fps=4)` → 再打开 `cast-emit` → 「已调到每秒 4 张」 |
| `投屏调到 20 帧` | `start_cast(mode=fast, fps=20)` → `success: false` → 原样播报「帧率只能调到每秒 1 到 10 张……」 |
| `换成普通模式` | `start_cast(mode=image)` → 打开 `cast-image` → 「已换成普通模式」 |
| `投屏状态` | `cast_status` → 一句话，例如「正在投屏，普通模式，画面刚刚更新」 |
| `停止投屏` | 只调 `stop_cast`，**不调**「UI页面渲染」→ 「投屏已停止」 |
| `帮我用手机投屏到电视` | **不应**召回本技能 |

- 回复里不应出现任何 URL、KEY、token、`hops`。
- 遇到「MCP 工具执行超时」，模型应先不说话、自动重试一次（Q2）。
- 帧率改动会一直保留到 glasscast 重启。测完说一句「投屏调到 4 帧」改回推荐值。
- 沙箱里主要看工具调用对不对；眼镜页面的实际效果以真机为准。

## 11. 真机测试

1. 控制台 →「设备管理」：确认你的眼镜在列表里，打开「开发者模式」。
2. 技能编辑页 → 右上角「真机测试」→ 选你的眼镜 → 完成绑定。此后这副眼镜的语音指令会走技能的**编辑态**版本。
3. 电脑上 glasscast 和公网通道保持运行，戴上眼镜依次说：
   - 「开始投屏」→ 眼镜上出现电脑画面（单色绿），约每秒刷新一次，延迟 1～2 秒。画面位于视野左上角，是系统摆放 640×290 页面的位置，页面里改不了。
   - 「用流畅模式投屏」→ 换成 `cast-emit`，约每秒 1.5 张，右下角的换图编号持续增加（不动了就是卡住，见 Q5）。
   - 「投屏状态」→ 播报模式和画面是否刚刚更新。
   - 「停止投屏」→ 播报「投屏已停止」，画面换成「投屏已停止」占位图。页面不会自己关闭，用眼镜的返回手势退出即可。
4. 同时看 glasscast 终端的访问日志：眼镜的请求来自 `千问AI穿戴` App（User-Agent 里能看到），地址形如 `GET /s/…/screen.jpg/<…>.jpg 200`。没有这类请求、或有请求但眼镜上没画面，见 Q3、Q4。
5. 测完想让眼镜回到正式链路：在「设备管理」里解绑，或关闭开发者模式。

## 12. 发布到自己的眼镜

1. **先发布插件**：「资源库」→「MCP 插件」→「电脑投屏」→ 详情页「发布」。前提是 3 个工具都已「调试通过」。发布后插件锁定，不能再改（见 Q10）。
2. 技能编辑页 → 右上角「发布」→ 按页面提示完成格式校验和安全扫描 →「创建版本」：发布范围选「**个人私有**」，按提示绑定目标设备（你自己的眼镜），填写版本说明 → 提交。如果进入审核，等待结果。
3. 发布后关闭开发者模式（或解绑编辑态），再对眼镜说「开始投屏」，确认已发布版本也正常。

> **不要发布到「公开市场」，也不要把技能分享给别人。** 插件里带着你的私人 token，投屏地址能看到你的屏幕。别人想用，请让他们按本手册用自己的电脑、域名、token 和插件重新搭一套。

## 13. 验证清单

电脑与公网：

- [ ] `.env` 由 `.env.example` 复制而来，填好了 `GLASSCAST_PUBLIC_URL`、`GLASSCAST_TOKEN`，画面参数是推荐值；重启 glasscast 后 token 不变。
- [ ] 屏幕录制权限已授予启动 glasscast 的 App，`screen.jpg` 不是黑屏或纯壁纸。
- [ ] `curl $B/healthz` 返回 `ok`，协议 HTTP/2，响应头没有 `alt-svc: h3`。
- [ ] `$B/s/$KEY/screen.jpg/test_1.jpg` 返回 200、`image/jpeg`，约 2～3 KB。
- [ ] `POST /mcp` 带 Bearer 列出 3 个工具、耗时远小于 3 秒；不带 Bearer 返回 401。
- [ ] （用本仓库 `deploy/` 的反代配置时）`curl -sS -o /dev/null -w '%{http_code}\n' $B/admin/config` 返回 404，管理接口只在电脑本机可用。

平台：

- [ ] MCP 插件「电脑投屏」：服务地址 `https://cast.example.com/mcp`、Bearer Token、无状态协议；3 个工具都「调试通过」。
- [ ] `Pages/cast-image`、`Pages/cast-emit` 各有 `template.xml`、`data.json` 和平台生成的 `meta.json`，`templateName` 分别是 `cast-image`、`cast-emit`。没有 `cast-fast`。
- [ ] `SKILL.md` 刷新后显示 4 张卡片，全文没有 `XXXXXX`，`tool_schema.json` 列出 4 个工具（含 `ui_page_render`）。
- [ ] `ui_schema.json` 是 `[]`（或不存在）。

运行：

- [ ] 本地模拟器里两个页面都能持续换图。
- [ ] 沙箱：「开始投屏」「用流畅模式投屏」都先调 `start_cast` 再调「UI页面渲染」；「停止投屏」只调 `stop_cast`；回复里没有 URL。
- [ ] 真机：两种模式都能自动弹出页面、持续刷新；「投屏状态」「停止投屏」正确。
- [ ] 发布（个人私有）并关闭开发者模式后，真机仍能正常投屏。

## 14. 常见问题

### Q1：插件创建成功，但工具数为 0 / 刷新不到工具

逐项排查，改完在插件详情里点「刷新 MCP 工具」：

1. 公网不通：按第 2 步 ① 从外网 `curl $B/healthz`。电脑睡眠、隧道断开、glasscast 没运行都会这样。
2. 服务地址填错：完整地址是 `https://cast.example.com/mcp`。写成 `/mcp/` 或别的路径会得到 307 / 404。
3. 401：平台里填的 token 与 `GLASSCAST_TOKEN` 不一致，glasscast 日志会提示「Bearer Token 不匹配」。最常见的原因是没固定 token，重启后换了新的。
4. 421：`GLASSCAST_PUBLIC_URL` 的域名与插件里的域名不一致（MCP 只放行这个 Host）。改成一致后重启 glasscast。
5. HTTP/3：响应头里有 `alt-svc: h3`，平台客户端升级到 HTTP/3 后失败。关掉反代 / CDN 的 HTTP/3。
6. 公网 curl 正常，但 glasscast 日志里完全没有平台的请求：看官方文档里关于平台访问外部服务（受信任地址 / 白名单）的说明，确认你的域名能被平台访问。

### Q2：对话里提示「MCP 工具执行超时」，或眼镜说「电脑上的投屏程序没有响应」

平台每次工具调用要在约 3 秒内完成，平台到服务器、服务器经隧道到电脑的往返都算在内。glasscast 本身几毫秒就返回，超时几乎都是链路问题。

- 插件传输协议选「无状态协议」（第 3 步），每次调用少一次往返。
- 服务器尽量放在到国内线路好的地方；跨境线路往返慢、丢包时最容易超时。
- 保持隧道热连接、电脑不睡眠（`deploy/tunnel.sh` 默认定时请求 `/healthz` 保温；`caffeinate -d`）。
- SKILL.md 已让模型在超时后先不说话、原样重试一次，第二次仍失败才播报「电脑上的投屏程序没有响应」。
- 判断卡在哪一段：glasscast 日志里没有那次请求，说明请求没到电脑；有请求而且很快就回了，说明是返回途中或平台那一侧超时。

### Q3：工具成功了，但眼镜上没有弹出页面

1. 在沙箱里展开工具调用：`start_cast` 之后有没有调用「UI页面渲染」？没有调用，多半是「UI页面渲染」没有变成卡片（`tool_schema.json` 里没有 `ui_page_render`），按第 7.3 步重插。
2. `templateName` 必须是 `cast-image` / `cast-emit`，与 `Pages/` 下的文件夹名一字不差，并且页面已保存（有 `meta.json`）。
3. 真机测试时眼镜要绑定编辑态技能、开着开发者模式（第 11 步）；发布后要确认发布范围绑定了这副眼镜。

### Q4：页面弹出了，但没有画面 / 一直黑屏

先看 glasscast 访问日志里有没有来自 `千问AI穿戴` 的 `GET /s/…/screen.jpg/<…>.jpg`：

1. **没有请求**：页面拿到的 `url` 不对。沙箱里看「UI页面渲染」的 `dataSource` 是否原样带了 `start_cast` 返回的 `url`（`https://cast.example.com/s/<KEY>/screen.jpg`）。再确认手机能用 HTTPS 访问这个域名：证书必须有效，用手机浏览器打开第 2 步 ③ 的地址试试。
2. **有请求、200，但眼镜上不显示**：图片太大。用 `.env.example` 的推荐设置（480×218 灰度，每张约 2.6 KB）；代码默认的 640×290、质量 70 在真机上不显示。如果你改过页面模板：图片地址必须以 `.jpg` 结尾（`…/screen.jpg/<会话号>_<编号>.jpg`），带 `?ts=` 这类查询参数的地址能下载成功但永远不显示。
3. **显示出来的是黑屏或只有桌面壁纸**：macOS 屏幕录制权限没给启动 glasscast 的 App，或给了之后没重启 glasscast。用 `--source synthetic` 对照；电脑锁屏时也会截到黑屏。

### Q5：流畅模式画面卡住不动，或者没有说的「每秒 N 张」那么快

- 原理：`cast-emit` 每换一张图要倒数 `hops × 3` 步（真机每步约 13 毫秒），`hops = round(1000 / 帧率设置 / 20)`，由 `start_cast` 返回。而手机下载、蓝牙传输加眼镜解码，每张图有约 0.65 秒的固定开销，所以**实际速度低于帧率设置**：设置 4 约每秒 1.5 张，这是实测的稳定上限。设置 ≤ 2 时 `hops` 都封顶 25，页面节奏相同，都约每秒 1 张，见 [使用手册 §12](usage.md#12-已知问题与-todo)。
- 设置到 4.5 以上时，换图可能快到一张都显示不完，画面冻住，而且会一直停在这个状态。SKILL.md 里「最流畅」「最快」「快一点」最多只调到 4，不会进入这个区间；只有明确说出更大的数字（例如「投屏调到 6 帧」）才会。处理：说「投屏调到 4 帧」或更低；仍然不动就用返回手势离开页面，重新说「用流畅模式投屏」；或者换回普通模式。
- 播报的「已调到每秒 N 张」说的是帧率设置，不是真机实际张数。这是已知的不准确，换算校准还没做（TODO：fps 换算校准）。
- 电脑上也能改帧率：`. ./.env && uv run glasscast set-fps 3`。已经打开的页面不会跟着变，再对眼镜说一次「用流畅模式投屏」，页面会带新的 `hops` 重新打开。

### Q6：停止投屏后页面还在

设计如此：`stop_cast` 只停止电脑端截屏，图片地址随后返回「投屏已停止」占位图；页面仍开着、仍会下载这张占位图。用眼镜的返回手势退出页面即可。不要让技能在停止时调用「UI页面渲染」，在真机上那会把投屏页面重新打开。

### Q7：编辑器预览里图片不显示

正常现象，见第 9.1 步。用本地模拟器（第 9.2 步）或真机看画面。

### Q8：保存 template.xml 时报错

- `URI-001`，提示 `"{{d:act}}" 没有 scheme`：你粘贴的是 `cast-fast`。平台保存校验不放行 `<frame-set action="{{…}}">`，流畅模式请用 `cast-emit`。
- XML 格式错误：确认整段复制完整（注释的 `<!--` 和 `-->` 成对，最后一行是 `</view>`），引号没有被改成全角。两个页面都是良构 XML，`tests/test_pages.py` 会检查。
- 自己改过页面后，页面能打开但什么都不运行：根 `view` 下最多 64 个子元素，超过后整页解析失败。其他写页面时的限制见 [pages/README.md](../pages/README.md)。

### Q9：画面偏在视野左上角 / 字太小看不清

- S1 的页面画布是 640×290，系统把它放在视野左上角，页面里没法居中。
- 整个屏幕缩到 480×218，字会很小。用 `GLASSCAST_REGION=左,上,宽,高` 只投屏幕的一块区域（坐标用 `uv run glasscast monitors` 和 `uv run glasscast snapshot --raw out.png` 量）。眼镜是单色绿屏，深色背景、浅色文字的界面看起来更清楚。

### Q10：插件已发布后，要改域名、token 或工具

已发布的插件锁定，只能新建插件 → 调试 → 得到**新的插件编码** → 按第 7.2、7.3 步用新编码重新替换并粘贴 SKILL.md → 重新发布技能。所以发布插件前就把域名和 token 定下来。没发布之前，直接在插件详情里改，然后「刷新 MCP 工具」。

### Q11：安全与流量

- 图片地址里的 KEY 是随机串，眼镜发不了请求头，图片只靠它保护：拿到完整地址的人就能看到你的屏幕。不要外传地址或带地址的日志截图；怀疑泄露就重启 glasscast（没固定 `GLASSCAST_CAST_KEY` 时会换新 KEY）。
- MCP 的 token 只填在平台插件里，不要出现在 SKILL.md、页面、data.json 或提交到 git 的任何文件里。
- 流量（推荐设置，每张约 2.6 KB）：普通模式约 9 MB/小时，流畅模式（约每秒 1.5 张）约 14 MB/小时。停止投屏后页面开着时仍在下载占位图，退出页面才会停。
- 在家时可以让手机直接从电脑的局域网地址取图，少走一段公网，见 [docs/deploy.md 的方案 D：局域网直连](deploy.md#5-方案-d局域网直连可选)（`tools/lan_https_proxy.py`，可选）。

## 附录 A：start_cast 返回字段

| 字段 | 含义 | 用在哪里 |
|---|---|---|
| `success` | 是否成功 | SKILL.md 判断成功或播报失败 |
| `mode` | 实际的投屏模式：`image` / `fast` | 选择打开哪个页面 |
| `url` | 图片地址 `https://cast.example.com/s/<KEY>/screen.jpg`；页面在后面拼上 `/<会话号>_<编号>.jpg` | 两个页面的 `dataSource.url` |
| `hops` | 流畅模式页面每换一张图的跳数，`round(1000 / 帧率设置 / 20)`，1～25 | `cast-emit` 的 `dataSource.hops` |
| `fps`、`fast_fps` | 调用之后生效的流畅模式帧率设置（1～10） | 播报「已调到每秒 N 张」 |
| `width`、`height` | 图片尺寸 | 参考 |
| `message` | 一句中文说明，失败时原样播报 | SKILL.md |
| `timer_url`、`frame_duration_ms` | 只给 `cast-fast`（本地模拟器）用 | 平台上用不到 |
| `page_fps` | 按模拟器速度估算的页面换图速率，不代表真机 | 参考 |

## 附录 B：平台上最终的文件结构

```text
电脑投屏（技能根目录）
├── SKILL.md              ← skill/SKILL.md（XXXXXX 已换成你的插件编码），4 张工具卡片
├── tool_schema.json      ← 平台自动生成，只读
├── ui_schema.json        ← []（可以没有这个文件）
└── Pages/
    ├── cast-image/
    │   ├── template.xml  ← pages/cast-image/template.xml
    │   ├── data.json     ← pages/cast-image/data.json（Mock，只影响编辑器预览）
    │   └── meta.json     ← 平台生成
    └── cast-emit/
        ├── template.xml  ← pages/cast-emit/template.xml
        ├── data.json     ← pages/cast-emit/data.json（Mock，只影响编辑器预览）
        └── meta.json     ← 平台生成
```
