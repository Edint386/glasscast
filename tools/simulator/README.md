# glasscast 本地模拟器

在本机浏览器里渲染千问 AI 眼镜 S1 的 Waft-DSL 页面，不用登录平台，也不用戴眼镜。

- 渲染用的是平台技能编辑器「预览 & 调试」面板里同一套 **waft-lite WASM 内核**（官方公开文件）。
  内核**不随本仓库分发**：`fetch_assets.sh` 在运行时从官方公开 CDN 下载到 `assets/`（已被 `.gitignore` 排除）。
  使用时请遵守官方的条款。
- 本目录其余部分是我们自己写的测试台：`serve.py`（本地静态服务器 + 演示图源）和 `index.html` + `harness.js`（驱动内核的网页）。
- 它复现的是**编辑器预览**这一侧，**不复现手机下载 + 蓝牙传输 + 眼镜解码这条真机链路**（见 [§2](#2-它能验证什么不能验证什么)）。

只想看页面渲染效果，按下面的 [§1](#1-快速开始) 做就行，不需要千问账号，也不需要公网域名。
要把投屏装到自己的眼镜上，看平台操作手册 [docs/platform-setup.md](../../docs/platform-setup.md)，
手册的 [§9.2](../../docs/platform-setup.md#92-本地模拟器推荐) 讲在那个流程里怎么用模拟器。

| 文件 | 作用 |
|---|---|
| `fetch_assets.sh` | 把内核下载到 `assets/`，并校验大小和魔数 |
| `serve.py` | 只用标准库的服务器：加 COOP/COEP 头、映射 `/assets/` 和仓库的 `pages/`，提供演示资源 `/demo/*` 和可选代理 `/proxy` |
| `index.html` + `harness.js` | 测试台：左边贴 XML 和 data JSON，右边是屏幕、触控键、统计和拉取记录，下面是内核日志 |
| `test_serve.py` | `serve.py` 的 pytest（不需要浏览器、不访问公网、不截屏） |

## 1. 快速开始

需要：Python 3.12 + [uv](https://docs.astral.sh/uv/)、`bash`、`curl`、Chrome 或 Edge（Chromium 内核）。

本文所有命令都在仓库根目录（clone 下来的 `glasscast` 目录）运行。`fetch_assets.sh` 首次运行时下载内核，约 26 MB；`serve.py` 默认占用 8770 端口：

```sh
uv sync
tools/simulator/fetch_assets.sh
uv run python tools/simulator/serve.py
```

端口被占用时加 `--port <其他端口>`，下面地址里的 8770 跟着改。

用 Chrome / Edge 打开 <http://127.0.0.1:8770/>，等左上角状态变成「就绪」（首次加载 23 MB 资源包要几秒）。
默认载入「内核自检」预设，点**渲染**：屏幕上应出现「glasscast 模拟器自检」、每秒递减的 `tick 3599…` 和一个小动画球
（内核资源包自带的 `.fset`），状态显示「已渲染」。

然后试两个投屏页面（离线即可，图片由 `serve.py` 的 `/demo` 生成）：

- 预设 `cast_image_demo`：普通模式页面 `pages/cast-image`，大号序号每秒加 1；
- 预设 `cast_emit_demo`：流畅模式页面 `pages/cast-emit`，右下角的换图编号持续增加。

也可以直接打开 `http://127.0.0.1:8770/?preset=cast_emit_demo&autorender=1`。
要接本机的 glasscast 做端到端联调，见 [§3.3](#33-接本机-glasscast端到端)。

常用操作：

- 改了 XML 或数据后点**渲染**。同一个 uri 会先弹出旧页面再注入，和平台编辑器一样。
- **只更新数据**：同一份 XML 和 uri，换数据后重新注入，不弹栈（编辑器 Mock 面板的做法）。
- **重启内核**：刷新页面。编辑器里的内容存在浏览器 localStorage 里，刷新后还在。
- 触控键按 S1 手势映射：单击 = confirm，双击 = back，前滑 = prev，后滑 = next。
- 勾选「点选检查元素」后点屏幕，会显示命中元素的标签、模板行号、属性和包围盒。

## 2. 它能验证什么，不能验证什么

**能验证**（和平台编辑器预览一致）：

- 页面能不能渲染（日志里的 `render ok`），绑定、`a:if`、事件链（`emit://`、`data://update`）是否按预期工作；
- 拉图节奏：`cast-image` 每秒换一次地址；`cast-emit` 每倒数 `hops × 3` 步换一次地址；服务端看到的请求间隔；
- 数据形状：平铺的 `{"url": …}` 和 MCP 包装的 `{"structuredContent": {"url": …}}` 都能拼出正确地址；
- 远程图预算：解码后超过 742 400 B（640×290×4）的图会被拒绝并告警；
- 模板结构：harness 渲染前用 DOMParser 检查 XML（内核本身对坏 XML 几乎全部放行）；
- 和本机 glasscast 的端到端联调（`--source synthetic` 合成画面，不需要屏幕录制权限）。

**不能验证**（只能上真机，步骤见 [docs/platform-setup.md §11](../../docs/platform-setup.md#11-真机测试)）：

- **手机 + 蓝牙这条链路**。真机上每张图由手机 App 下载、经蓝牙传到眼镜再解码，每张约 0.65 s 的固定开销，
  所以真机最多每秒约 1.5 帧。模拟器里没有这段开销，流畅模式能跑得快得多，**模拟器里的帧率不代表真机**。
  真机实测：4 档（`start_cast(mode="fast", fps=4)`，hops = 12）约每秒 1.5 帧，是稳定上限。
- 真机的显示门槛：模拟器只按 742 400 B 预算判断；真机上 640×290 的图下载成功却不显示，要用 `.env.example` 里推荐的
  480×218 灰度小图。
- 真机上 `data` 里超过 2147483647 的整数会被卡在这个上限；模拟器是按 32 位绕回，发现不了这个问题。所以页面的 `data` 里只放小整数。
- 平台保存校验（例如 URI-001 拒绝 `<frame-set action="{{…}}">`）：`pages/cast-fast` 在模拟器里能跑，但平台保存不了。
- 平台云端那一侧：MCP 插件调用、Skill、模型调用「UI页面渲染」打开页面的过程，以及工具超时。
- 相机、语音、GMA（手机通道）、`svc://` 平台服务都没有模拟。

## 3. 验证投屏页面

`pages/` 下有三个页面：

| 页面 | 用途 | 换图的定时方式 | 平台能保存吗 | 见 |
|---|---|---|---|---|
| `cast-image` | 普通模式 | `<tick>` 每秒换一次图片地址 | 能 | [§3.1](#31-cast-image普通模式-演示图) |
| `cast-emit` | 流畅模式（现行） | `emit://` 事件倒数，每 `hops × 3` 步换一张 | 能 | [§3.2](#32-cast-emit流畅模式倒数换图-演示图) |
| `cast-fast` | 旧的流畅模式（只作对照） | `<frame-set>` 帧合集当定时器 | 不能，只能在模拟器里跑 | [§3.4](#34-cast-fast帧合集定时器只能在模拟器里跑) |

每一步都看三处：屏幕、「服务端看到的拉取」表（只记录 `/demo` 和 `/proxy`）、日志（可勾选「只看关键行」）。

### 3.1 cast-image（普通模式）+ 演示图

选预设 `cast_image_demo`，点渲染。期望：

- 大号序号每秒加 1、背景色轮换；
- 拉取表里 `/demo/screen.jpg` 间隔约 1.00 s，「demo 图拉取间隔（中位数）」约 1.00 s；
- 首次渲染会连发 3 个请求（`<tick>` 还没赋值），之后每秒一个；日志里没有 `[W]` 行。

页面的图片地址是 `{{url}}/<毫秒时间戳>_<tick>.jpg`（地址必须以 `.jpg` 结尾，真机才显示），`/demo` 会忽略尾段。

### 3.2 cast-emit（流畅模式，倒数换图）+ 演示图

`pages/cast-emit` 现在是**倒数换图**版本（真机验证过的写法，原理见模板顶部注释）：页面给自己发
`emit://event/dN?n=N` 事件，每收到一次把 `n` 减一再发出去；计数放在事件参数里、不写 `data`，所以倒数过程中没有绑定刷新，
不会打断正在加载的图片。只有 `n` 到 0 时才写一次换图编号 `g`（图片地址随之改变），再从 `m = hops × 3` 开始下一轮。
`hops` 由 `start_cast` 的出参下发：`hops = round(1000 / fps / 20)`，`fps=4` 时为 12，即每张图倒数 36 步。

选预设 `cast_emit_demo`（data 为 `{"url": "<本机>/demo/screen.jpg", "hops": 12}`），点渲染。期望：

- 演示图持续更换，右下角的换图编号 `g` 持续增加；
- 拉取表里每次换图一个请求，没有堆积；日志里没有 `[W]` 行；
- 间隔取决于模拟器里每一步的耗时（随机器负载在约 15～35 ms 间浮动），`hops=12` 时大约 0.5～1.3 s 一张。
  改 data 里的 `hops`（1～25）再点渲染，间隔随之变化。**这个速率不代表真机**，见 §2。

`pages/cast-emit` 没有内置副本，所以只有仓库里有这个目录时才会出现 `cast_emit_*` 预设。

### 3.3 接本机 glasscast（端到端）

终端 1 启动合成画面的 glasscast（不截屏，不需要屏幕录制权限），用 `--cast-key` 固定图片地址里的 key，方便复制：

```sh
uv run glasscast serve --source synthetic --public-url http://127.0.0.1:8766 \
    --cast-key localtest0001 --autostart fast
```

`--public-url` 写本机地址只能用来本地联调，眼镜和平台访问不到。8766 被占用时改成 `--port 8767 --public-url http://127.0.0.1:8767`，
下面的地址跟着改。

终端 2 运行 `uv run python tools/simulator/serve.py`，浏览器打开：

```
http://127.0.0.1:8770/?glasscast=http://127.0.0.1:8766/s/localtest0001&preset=cast_emit_glasscast&autorender=1
```

期望：画面是 glasscast 的合成画面（时间戳 + 计数器），右下角换图编号递增；终端 1 的访问日志是一行行
`GET /s/…/screen.jpg/<会话号>_<编号>.jpg 200`，编号逐个加一。

普通模式把 `preset` 换成 `cast_image_glasscast`：访问日志里是 `…/screen.jpg/<毫秒时间戳>_<tick>.jpg`，约每秒一个。

- 不带 `glasscast=` 参数时，预设里的地址是 `http://127.0.0.1:8766/s/REPLACE_KEY`，把 data 里的 `REPLACE_KEY` 换成真实 key 再渲染。
- glasscast 的响应带 `Access-Control-Allow-Origin: *`，跨端口加载不需要代理。拉取节奏看 glasscast 自己的访问日志（时间到毫秒）。
  这些请求不经过 `serve.py`，所以「服务端看到的拉取」表里没有它们。
- 真机上页面由模型调用「UI页面渲染」打开，`dataSource` 是平铺的 `{"url": …, "hops": …}`，这里的预设用的就是这个形状。
- 平台手册的 [§9.2](../../docs/platform-setup.md#92-本地模拟器推荐) 是同一件事的手动版：用 `page:cast-emit`、`page:cast-image` 预设，自己把 data 里的地址改成本机地址。

### 3.4 cast-fast（帧合集定时器，只能在模拟器里跑）

`pages/cast-fast` 是旧的流畅模式：播放 glasscast 的 `timer.fset`（2 帧 8×8 的帧合集），每播完一遍换一次图片地址。
平台保存校验 URI-001 不放行它用到的 `<frame-set action="{{…}}">`，所以它**上不了平台，也没有在真机上跑过**，只作对照保留。
平台和真机用的流畅模式是 `cast-emit`。

它要用 glasscast 的 `timer.fset`，没有 `/demo` 版本。先按 §3.3 启动终端 1 和终端 2，再打开：

```
http://127.0.0.1:8770/?preset=page:cast-fast
```

不要加 `autorender=1`：这个预设的 data 是 `pages/cast-fast/data.json` 原样，里面是占位地址（`cast.example.com`）。
先把 data 改成下面这样再点**渲染**：

```json
{
  "url": "http://127.0.0.1:8766/s/localtest0001/screen.jpg",
  "timer_url": "http://127.0.0.1:8766/s/localtest0001/timer.fset",
  "frame_duration_ms": 125
}
```

期望：画面持续更新，终端 1 的访问日志里能看到 `timer.fset`，之后是一行行 `screen.jpg`。
拉图间隔约 `2 × frame_duration_ms + 17 ms`，`frame_duration_ms` 为 125 时约 266 ms（约每秒 3.8 张）。
这是模拟器里的数字：真机上流畅模式的稳定上限约每秒 1.5 帧（§2）。

### 3.5 其他预设

| 预设 id | 页面 / 数据 | 用途 |
|---|---|---|
| `selftest` | 内置自检页 | 确认内核能启动、能渲染、能动（离线） |
| `cast_image_demo` / `cast_emit_demo` | `pages/cast-image` / `pages/cast-emit` + `/demo/screen.jpg` | §3.1、§3.2 |
| `cast_image_glasscast` / `cast_emit_glasscast` | 同上 + 本机 glasscast | §3.3 |
| `budget` | 内置页 + `/demo/big.png`（700×300 RGBA） | 预算自检：日志应出现 `over the 742400 device limit` 和 `image too large for the device display` 两条 `[W]`，图片不显示 |
| `cast_image_gray` | `pages/cast-image` + 灰度 JPEG | 实验：灰度图在单色屏上的效果和体积 |
| `cast_clip_demo`、`cast_clip_jpeg`、`cast_clip_gray`、`cast_clip_glasscast` | 已下线的第一轮页面 `cast_clip`（`harness.js` 内置副本）+ 演示帧合集 | 对照实验：`<frame-set>` 的 `src` 只在创建时求值一次，播完一段就停（见 §4 第 2 条）；JPEG 帧黑屏 |
| `page:<name>` | `pages/<name>/template.xml` + 同目录 `data.json` 原样 | `page:cast-image`、`page:cast-emit`、`page:cast-fast`。`data.json` 里是占位地址（`cast.example.com`），渲染前先把地址改成本机的 |
| `sample:<name>` | `serve.py --samples DIR` 目录里的 `<name>.xml`，data 为空 | 只有启动时给了 `--samples` 才出现，用来批量载入你自己的页面 |

### 3.6 URL 参数

| 参数 | 含义 |
|---|---|
| `preset=<id>` | 启动时载入预设（id 见 §3.5）。不存在的 id（例如没开 `--samples` 时的 `sample:<name>`）会在日志里提示一行，并改载 `selftest` |
| `autorender=1` | 内核就绪后自动渲染 |
| `glasscast=<base>` | `*_glasscast` 预设用的地址前缀，默认 `http://127.0.0.1:8766/s/REPLACE_KEY` |
| `entry=index.js` | 改用普通入口（默认用编辑器同款的 `index.blob-worker.js`；两者都能用） |
| `proxy=1` / `proxy=0` | 是否让内核的远程下载走 `/proxy?url=` |
| `scale=2` | 屏幕缩放 |
| `initTimeoutMs=…` | 内核启动超时（默认 60000） |

浏览器控制台里可以用 `glasscastSim.render()`、`glasscastSim.applyPreset(id)`、`glasscastSim.getLog()`、
`glasscastSim.getStats()`、`glasscastSim.snapshot()`（画布 PNG 的 dataURL），以及 `glasscastSim.harness`（§7 的驱动对象），方便自动化。

## 4. 模拟器里观察到的内核行为

以下在模拟器里测得（2026-10-23/24，waft-lite v0.2.11），只代表编辑器预览这一侧；真机结论以 `docs/research.md` 为准。

| # | 现象 | 对页面写法的影响 |
|---|---|---|
| 1 | `<image src>` 的地址每次变化都会重新下载，换图时保留旧图直到新图解码完（不闪）。首次渲染可能对同一页面连发 2～3 个请求 | 服务端要能接受地址里的计数为空或重复 |
| 2 | **`<frame-set>` 的 `src` 只在节点创建时求值一次**：之后无论 `d:`、`tick`、`dataSource://update`、`a:if` 怎么变都不重新下载（`<gif>` 同样） | 不能靠换 `src` 播放连续的帧合集；这是 `cast_clip` 下线的原因 |
| 3 | 内核**按完整 URL 缓存远程下载**，在内核生命周期内跨页面有效 | 地址每次都要不同：现行页面在地址里拼 `{{f:timestamp()}}` 或打开时的会话号 |
| 4 | `.fset` 里放 JPEG 帧：黑屏，不触发 `on-completed`，没有告警 | 帧合集只用 PNG 帧 |
| 5 | **屏幕是单色绿**：画布像素恒为 R=0、B=0，G = 亮度 | 颜色没有意义；8 位灰度图显示正常且体积更小（glasscast 默认 `--color gray`） |
| 6 | 解码后超过 742 400 B 的远程图被拒绝，日志格式见 §3.5 `budget` | 服务端先缩到 ≤ 640×290；真机还要更小（§2） |
| 7 | 内核对坏 XML 极其宽容：`<label text="x"</view>` 也打 `render ok … bindings=0` | 「已渲染」不代表模板正确，要配合 harness 的结构检查（仓库里由 `tests/test_pages.py` 用严格解析器把关） |
| 8 | 远程地址用 `http://` 可以；跨源要求对方带 CORS 头（内核用 XHR 下载） | 本地联调 glasscast 不需要代理；真机和平台要公网 HTTPS |
| 9 | `setCorsProxy(prefix)` 之后，**所有**远程下载（包括 localhost）都变成 `prefix + 百分号编码(url)`；`setCorsProxy('')` 不会恢复直连（XHR 报 `Invalid URL`） | harness 只在勾选时设置；取消勾选会自动刷新页面 |
| 10 | `wasm_get_content_offset_y()` 返回 142：640×290 的 DSL 画布落在 640×480 画布的 `(0,142)` | harness 按编辑器同样的方式取景 |
| 11 | `emit://` 是异步的（下一轮消息循环才发布），可以当节拍器；每一步约 15～35 ms，随负载浮动 | `cast-emit` 的倒数换图依赖这一点；真机每步约 13 ms |
| 12 | 三元表达式 `{{a ? b : c}}` 的结果被截断到 63 个字符；两段拼接 `{{url}}{{structuredContent.url}}` 不截断 | 页面绑定一律用两段拼接，不用三元 |

## 5. 内核资源

`fetch_assets.sh` 下载以下官方公开文件。相对布局必须和 CDN 一致，因为入口用 `import './dist/…'` 相对定位胶水。

| 本地路径（`assets/` 下） | 来源 | 大小（字节） | 必需 |
|---|---|---|---|
| `index.blob-worker.js` | `https://g.alicdn.com/ai-fe/glasses-open-platform/0.0.34/waft-lite/index.blob-worker.js` | 12 947 | 是 |
| `dist/waft-lite-wasm.blob-worker.js` | 同目录 `dist/` | 489 220 | 是 |
| `dist/waft-lite-wasm.wasm` | 同目录 `dist/` | 1 844 186 | 是 |
| `dist/waft-lite-wasm.data` | `https://ai-gop-public.oss-cn-beijing.aliyuncs.com/res/waft/0.0.25/waft-lite-wasm.data`（编辑器写死的地址；CDN 的 `dist/` 下没有它） | 23 438 662 | 是 |
| `dist/build-info.json` | 同目录 `dist/` | 489 | 否 |
| `index.js`、`dist/waft-lite-wasm.js` | 普通入口和普通胶水 | 12 935 / 488 241 | 否 |

脚本会校验大小区间、`.wasm` 的 `\0asm` 魔数、不是 OSS/CDN 的 XML 错误页，以及 `.data` 的大小等于胶水里写死的
`"remote_package_size"`。选项：`--check` 只校验，`--force` 全部重新下载，`--from DIR` 先从本地目录复制。
官方升级内核时改 `WAFT_VERSION=0.0.xx`（`WAFT_BASE`、`WAFT_DATA_URL` 也能覆盖）。
以上版本号和大小是 2026-10 的情况，内核为 waft-lite v0.2.11。

## 6. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 启动时报「无法监听 127.0.0.1:8770」 | 端口被占用。加 `--port <其他端口>`，浏览器地址跟着改 |
| 横幅「读不到 /api/info」 | 页面不是由 `serve.py` 提供的（例如直接双击打开了 `index.html`）。用 `serve.py` 打开 |
| 横幅「内核资源不完整」 | 还没运行 `fetch_assets.sh`，或下载不完整；运行 `tools/simulator/fetch_assets.sh --check` 看缺什么 |
| 横幅「页面不是 cross-origin isolated」 | 响应缺 COOP/COEP 头，WASM 多线程起不来。必须经 `serve.py` 访问；不要放在会改写响应头的代理后面 |
| 渲染成功但图片不出来 | 看日志里的 `[W] … remote image failed`：`err=-1 download failed` 多半是地址错了或对方没有 CORS 头；`err=-100` 是超预算 |
| glasscast 访问日志里一个请求都没有 | 地址里的 key 不对，或内核命中了缓存（内核按完整 URL 缓存，现行页面都在地址里拼了时间戳或会话号，一般不会遇到） |
| 状态长时间停在「启动内核…」或 `Running...` | 浏览器标签页在后台时内核启动会变慢（实测可到 20 多秒）；把标签页放到前台再等一会儿 |

## 7. 内核 API

下面是驱动内核时用到的接口和行为，供想自己驱动内核的人参考。

### 7.1 入口模块

`index.blob-worker.js`（以及 `index.js`）是 ES module：`export default async function createWaftLite(config)`。
两种入口的区别只在 pthread worker 的创建方式：blob 变体先建一个 `import "<胶水绝对地址>"` 的 blob: URL，再 `new Worker(blob, {type:'module'})`；
普通变体直接 `new Worker(new URL('waft-lite-wasm.js', import.meta.url))`。

`createWaftLite(config)` 的行为：

- `globalThis.crossOriginIsolated === false` 时**直接抛错**，提示加 COOP `same-origin` 和 COEP `require-corp`（或 `credentialless`）。
- `initTimeoutMs`（入口默认 30000，编辑器传 60000）限制整个启动；超时就 reject，晚到的模块会被 `destroy()`。`0` 表示不限。
- `locateFile(path, prefix)`：传入后完全接管路径。`.data` 用空前缀请求，必须显式映射；本 harness 映射到 `/assets/dist/`。
  不传时 `.data` 默认找 `./dist/waft-lite-wasm.data`（相对入口），`.wasm` 走 `import.meta.url`。
- `print`：不传就只放行含 `[ess:` 或 LVGL `[Warn]` 一类的行；传了就拿到 C 侧 stdout 的原始行。`printErr` 默认是 `console.error`。
- 其余字段原样挂到 Emscripten `Module` 上，内核会回调以下这些：

| 回调 | 何时调用 / 参数 |
|---|---|
| `onBootAnimComplete()` | 开机动画结束，桌面就绪；同时设置 `Module._bootAnimCompleted = true` |
| `onWaftStatus(category, data)` | C 侧状态上报。实测 `app → {"initStatus":"completed"}`；元素检查 `elementInspect → {hit, tag, line, attrs, x, y, w, h}`（画布坐标）。最新值可用 `getWaftStatus(cat)` 读取 |
| `onUiAction(detail)` | 页面发出的 `svc://ui-action`（JSON） |
| `onCameraRequest(action, detail)` | 页面请求相机（宿主页面要扮演相机服务，结果通过 `wasm_camera_on_*` 回报） |
| `onAbort(what)` | Emscripten abort |
| `setStatus(text)` | 资源包下载进度：`Downloading data... (loaded/total)` |
| `canvas` | 目标 `<canvas>`；没有时找 `#waft-canvas`。内核自己把它设成 640×480，用 2D context 的 `putImageData` 逐块绘制 |

resolve 后，模块上除了 Emscripten 的 `ccall/cwrap/FS` 之外，入口还加了 `setCorsProxy(prefix)`（行为见 §4 第 9 条）、
`getWaftStatus(category?)`、`_buildInfo`（`dist/build-info.json`，取不到就是 null）、`loadImageAssets(dir)`（依赖官方开发服务器，本工具不实现）
和 `destroy()`（终止所有线程并移除事件监听）。

### 7.2 `ccall` 一览

| 函数 | 签名 | 说明 |
|---|---|---|
| `wasm_test_open_dsl` | `(string xml, string dataJson, string uri) → void` | **注入页面**。uri 必须以 `waft://lite/dsl/` 开头。日志：`wasm_test_open_dsl <uri> -> queued` |
| `wasm_test_close_page` | `() → number` | 弹出当前页；返回 0 表示队列满。日志 `on-hide` → `on-unload` → `page destroy: <name>`；没有页面时是 `no page to pop` |
| `wasm_send_key` | `(number code, number state) → number` | 按键，state 默认 2。键码：confirm 10、back 27、prev 17、next 18、home 2、voice 200、left 20、right 19 |
| `wasm_inspect_click` / `wasm_inspect_clear` | `(x, y) → number` / `() → number` | 元素检查，坐标是 640×480 画布像素；结果走 `onWaftStatus('elementInspect', …)` |
| `waft_download_set_cors_proxy` | `(string prefix) → void` | 即 `setCorsProxy` |
| `wasm_get_content_offset_y` | `() → number` | 实测 142 |
| 其余导出 | `wasm_test_open_uri`、`wasm_test_gma_recv`、`wasm_camera_on_*` 等 | 本工具不用 |

### 7.3 uri 规则

页面 uri 的形式是 `"waft://lite/dsl/" + slug(app) + "/" + slug(page)`。`slug` 的规则：只含 `[A-Za-z0-9._-]` 的字符串原样保留；
否则转成小写 slug，后接 djb2 哈希的 36 进制（见 harness 的 `buildDslPageUri`）。注入的 uri 和当前页相同时先 `close_page` 再注入；
不同时直接压栈。真机上用的是平台分配的 `waft://lite/dsl/<技能>/<随机串>`。harness 默认给 dataSource 追加 `latitude`、`longitude`，可以用开关关闭。

### 7.4 日志格式和渲染判定

C 侧日志行的格式是 `[级别] [ess:<模块>] <内容>`，级别为 `I`、`W`、`E`。常见行：

```
[I] [ess:wasm-api] wasm_test_open_dsl waft://lite/dsl/glasscast/cast-image -> queued
[I] [ess:dsl-page] on-load
[I] [ess:dsl-page] render ok md5=_wasm_test_1 bindings=1        ← 渲染成功
[I] [ess:dsl-engine] bindings applied=0 skipped=0 aif_flip=0     ← 每次绑定刷新一行
[I] [ess:DSL_ROUTER] route data: data://update?i=1
[W] [ess:network_img] remote image 700x300 decodes to 840000 bytes, over the 742400 device limit, ignored: <url>
[W] [ess:waft_image] remote image failed: err=-100 image too large for the device display
[W] [ess:waft_image] remote image failed: err=-1 download failed
```

harness 的判定（与编辑器相同）：成功 `/rendered successfully/i` 或 `/render ok md5=/i`；失败 `/failed to render dsl|dsl render failed|render fail\w* md5=/i`；
注入后 5 s 没有结果就 pop 再补注入一次。这个内核构建**不打印**下载成功的明细，要观察拉取节奏只能看服务端：
`serve.py` 的 `/api/requests`，或 glasscast 自己的访问日志。

## 8. `serve.py`

```
uv run python tools/simulator/serve.py [--host 127.0.0.1] [--port 8770] [--assets DIR] [--pages DIR] [--samples DIR]
                                       [--allow-proxy] [--proxy-allow-private] [--allow-host NAME]...
                                       [--demo-frames 10] [--demo-size 640x290] [--quiet] [--open]
```

| 路径 | 内容 |
|---|---|
| `/`、`/harness.js`、`/README.md` | 本目录的白名单文件（`serve.py`、测试等不对外提供） |
| `/assets/…` | `--assets` 目录（默认 `tools/simulator/assets/`）；`max-age=3600` + ETag/304 |
| `/pages/…` | 仓库的 `pages/`（`--pages` 可改），只读 |
| `/samples/…` | 只有给了 `--samples DIR` 才有：该目录里的 `*.xml`，只读；harness 里出现为 `sample:<name>` 预设。不给时返回 404 |
| `/api/info` | 资源自检（缺失或损坏的文件、build-info）、代理开关、Pillow 是否可用、演示资源、可用页面和示例 |
| `/api/requests?since=<ts>` | 最近 300 条 `/demo`、`/proxy` 请求（时间、query、字节数、序号） |
| `/demo/screen.jpg`、`.png`（以及 `…/screen.jpg/<任意>.jpg`） | 640×290 演示图，每次请求序号加 1；没有 Pillow 时 `.jpg` 退回 PNG |
| `/demo/screen-gray.jpg`、`.png` | 8 位灰度版（需要 Pillow） |
| `/demo/clip.fset`、`/demo/clip/<任意>.fset` | 10 帧 RGBA PNG 的 `.fset`（格式见 `glasscast/fset.py`）；路径里的 `<任意>` 用来绕开内核缓存 |
| `/demo/clip-jpeg[/<任意>].fset`、`/demo/clip-gray[/<任意>].fset` | 实验帧格式：JPEG 帧、灰度 PNG 帧（需要 Pillow） |
| `/demo/big.png` | 700×300，故意超预算 |
| `/proxy?url=` | 同源代理（默认关闭） |

所有响应都带 `Cross-Origin-Opener-Policy: same-origin`、`Cross-Origin-Embedder-Policy: require-corp` 和 `X-Content-Type-Options: nosniff`。
`/demo/*` 和 glasscast 一样带 `Cache-Control: no-store`、`Access-Control-Allow-Origin: *`、`Cross-Origin-Resource-Policy: cross-origin`。
`serve.py` 只依赖标准库；装了 Pillow（`uv sync` 会装）才有 JPEG 和灰度演示图。

安全措施（防止它变成开放代理或被外部网页利用）：

- 默认只监听 127.0.0.1。所有请求都校验 `Host` 头，只认 `127.0.0.1`、`localhost`、`::1`、`--host` 和 `--allow-host`，用来挡 DNS 重绑定。
- `/proxy` 默认关闭。打开后：只接受本机发起的连接和同源请求；只允许 http/https；默认拒绝解析到本机或内网的目标，
  重定向的每一跳都重新校验（`--proxy-allow-private` 可放开），校验和建连用同一次 DNS 解析结果；不使用环境变量里的上游代理；
  上游内容上限 8 MiB、超时 10 s；只转发 `Content-Type`。
- 打开代理后，内核**所有**远程下载都会走它（§4 第 9 条），包括本机的 glasscast，这时要加 `--proxy-allow-private`。一般不需要开代理。

## 9. 测试

```sh
uv run pytest -q tools/simulator/test_serve.py
```

覆盖：隔离头、MIME、HEAD、ETag、路径穿越、白名单、Host 校验、`/api/*`、可选的示例目录、演示图尺寸和预算、`.fset` 结构
（独立解析，不依赖 `glasscast.fset`）、实验帧格式、代理的开关、编码形式、内网拦截、重定向再校验、DNS 重绑定、
忽略环境代理、只服务本机客户端、跨站拒绝、CLI 解析。浏览器侧按 §1、§3 人工验证。

## 10. 已知限制

- 模拟器不等于真机（§2）：真机速率、显示门槛、大整数截断、平台保存校验都要以真机和平台为准。
- 内核版本跟着平台编辑器走（入口 0.0.34，资源包 0.0.25）。官方升级后旧地址可能失效，用新的 `WAFT_VERSION` 重新运行 `fetch_assets.sh`。
- 只在 Chromium 内核浏览器上测过。Safari 和 Firefox 需要支持 SharedArrayBuffer 和 module worker，未验证。
- 代理前缀设置后无法撤销，只能重启内核（harness 会自动刷新页面）。
- 拉取间隔会随环境浮动（浏览器标签页在后台时明显变慢）。比较两种写法时要在同一条件下交替测。
- 官方 Waft-DSL 文档见 <https://device.qianwen.com/docs>（「页面开发指南」「能力矩阵」等章节）。
