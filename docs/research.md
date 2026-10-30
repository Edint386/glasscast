# 电脑投屏到千问 AI 眼镜：调研记录（2026-10-23～10-28）

> **先读这里**：这是按日期记录的实验笔记，保留了当时的推测、弯路和后来被推翻的结论，后面的小节经常更正前面的说法，单独摘出来的某一句可能已经过时。**glasscast 现在的行为和用法以 [docs/usage.md](usage.md) 和 [docs/platform-setup.md](platform-setup.md) 为准**；真机上的最终结论汇总在 §1 开头。
>
> 文中"用户"指做真机测试的人；"官方文档第 N 节"指千问 AI 硬件开放平台文档（<https://device.qianwen.com/docs>）按 2026-10 目录顺序的章节编号；探针页面和实验文件在 `reference/`（说明见 [reference/README.md](../reference/README.md)），使用 glasscast 不需要它们。文中调速设置写成"N 帧""每秒 N 张""N fps""设 N"的，都是现在叫"档位"的设置，不是真实帧数；`.fset` 的"48 帧""10 帧"和实测的"每秒约 1.5 张"是真实的数。

本文记录为"把电脑画面投到眼镜屏幕"所做的调研，结论来自官方文档（<https://device.qianwen.com/docs>，2026-10-23 抓取）和在开放平台控制台、浏览器端模拟器、真机上的实测。标注【待验证】的条目当时尚未在模拟器或真机上跑过；标注"模拟器实测"的条目来自本仓库的本地模拟器（`tools/simulator/README.md` §4），真机行为可能不同（后面 §9.5 起的真机实测里有不少就不同）。

## 1. 结论摘要

### 最终结论（真机，2026-10-24～10-28，千问 AI 眼镜 S1）

这些结论覆盖本文其余部分里更早的说法。

- **页面由模型打开**：SKILL.md 让模型在 `start_cast` 成功后调用官方工具 `^^tQ8fCF.ui_page_render^^(templateName=..., dataSource=...)`。`ui_schema.json` 的 OUTPUT Hook 在真机上从不触发（平台交给 Hook 的 MCP 出参只有 `content[0].text`），所以 `skill/ui_schema.json` 是 `[]`；Hook 版本留在 `reference/ui-schema-hooks/`（§9.5）。
- **图片地址必须以 `.jpg` 结尾**：`…/screen.jpg/<会话>_<序号>.jpg`。`?ts=` 查询串写法能下载，但永远不显示（§9.6）。下文 §1～§9.5 里的 `?t=`、`?ts=` 写法都只在模拟器里成立。
- **图片链路**：手机上的千问AI穿戴 App（iOS CFNetwork）下载每张图，经蓝牙送到眼镜解码。每张图有约 0.65 s 的固定开销，在 2.5～4 KB 范围内与体积基本无关，所以最多约 1.5 张/秒。每次绑定刷新都会让还没加载完的 `<image>` 从头加载，页面在两次换图之间不能有任何刷新（§9.13、§9.19、§9.25）。
- **普通模式 = 页面 `cast-image`**：`<tick>` 每秒换一次地址。稳定，约 1 张/秒，延迟 1～2 s。
- **流畅模式 = 页面 `cast-emit`（事件倒数版）**：每换一张图做 `hops × 3` 步 emit:// 倒数（真机每步约 13 ms）。`start_cast(mode="fast", fps=N)` 返回 `hops = round(1000/N/20)`（1～25）。真机实际低于设置值：设 4（间隔 630～720 ms，约 1.5 张/秒）是实测的稳定上限；4.5 以上有时会掉进"卡住"状态——换图太快、每张都加载不完，画面停住，要重开页面或调低 fps（§9.19、§9.20）。fps 仍接受 1～10，代码和话术里"每秒约 N 张"的说法偏高，是已知待校准项。§9.8～§9.12 里流畅模式能到 4～10 张/秒的数字只在模拟器里成立。
- **画布与图片设置**：S1 的页面画布是 640×290，系统把它放在视野左上方，页面里没法居中（§9.20）。真机上用的设置：`GLASSCAST_CANVAS=480x218`、`GLASSCAST_COLOR_MODE=gray`、`GLASSCAST_FIT_MODE=crop`、`GLASSCAST_JPEG_QUALITY=90` + `GLASSCAST_JPEG_TARGET_KB=2.6`（二分质量压到约 2.6 KB）、`GLASSCAST_JPEG_ENCODER=auto`（装了 mozjpeg 就用）、`GLASSCAST_FAST_FPS=4`（见 `.env.example`）。代码默认的 640×290、质量 70 在真机上图太大、显示不出来（§9.7、§9.20～§9.22）。
- **内核与平台限制**：`data` 里超过 2147483647 的数在真机上被钳住，页面只在 `data` 里存小整数（§9.11）；根 view 下最多 64 个子元素（§9.8）；平台保存校验 URI-001 拒绝 `<frame-set action="{{...}}">`，所以 `pages/cast-fast`（帧合集定时器）只能在本地模拟器里跑，上不了平台（§9.4）。
- **`<gif>` / `<frame-set>` 的 `src` 只在节点创建时求值一次**，之后改了也不重新下载（§2.4、§9.2 E2、§9.24）。
- **停止**：`stop_cast` 停止截屏，之后图片地址返回"投屏已停止"占位图。停止时不要调用 UI 页面渲染工具（真机上会重新打开页面，§9.17）；用户用眼镜的常规返回手势离开页面。

### 调研初期的结论（2026-10-23，保留原文）

1. 眼镜端没有视频解码器，也收不到任何视频流（音视频推流只从眼镜往外推）。能做的是"连续换帧"：
   - **方案三（单图轮询）**：UI 页面里 `<tick>` 每秒变一次值，图片地址随之变化、重新下载。上限 1 fps，延迟最低（约 1～1.5 s）。已有第三方实测跑通（"UI 页面 + MCP，约 1 秒一刷"）。注意防缓存参数里必须带 `{{f:timestamp()}}`：只用 `?t={{tick:t}}` 时，同一内核里重开页面 tick 从头倒数，地址会命中上次的缓存（§9.2 E7）；当时的写法是 `?ts={{f:timestamp()}}_{{tick:t}}`（真机上查询串式地址不显示，后来改成路径式 `…/screen.jpg/<时间戳>_<tick>.jpg`，§9.6）。
   - **方案一（帧合集分段流）**：`<frame-set>` 播放 `.fset` 文件，`on-completed` 播完自动切下一段。段内可到 24 fps，但整体延迟更高（攒段 + 下载），带宽是硬约束。**模拟器实测链式切段不成立**（§2.4），该方案已下线，由 §9.3 的「帧合集定时器 + 图片轮询」（fast 模式）替代。
2. **`.fset` 文件格式已逆向确认**（见 §4），不再是障碍。
3. MCP 只做"控制面"：一次会话调用一次，返回图片/帧文件地址；逐帧刷新完全由页面自己完成。
4. 图片和帧文件必须放在**公网 HTTPS** 地址上：真机经手机网络下载；编辑器模拟器也不是直连，而是经平台的同源代理拉取。本机 `localhost` 对两者都不可见。

## 2. 平台机制（文档 + 控制台实测）

### 2.1 UI 页面（模板）的存储与引用

- 技能编辑器文件树中，自定义页面位于 `Pages/<页面名>/`，固定三个文件：`template.xml`（Waft-DSL）、`data.json`（Mock 数据，`dataSourceMock`）、`meta.json`（平台生成，不允许编辑）。
- 实测生成的 `meta.json`（编号类字段已换成占位：`<templateId>` 是平台分配的数字，`<page>` 是 6 位页面编码，`xxxxx-skill-xxxxxx` 是技能 slug）：

  ```json
  {
    "templateId": "<templateId>",
    "templateName": "cast",
    "displayName": "cast",
    "uri": "waft://lite/dsl/xxxxx-skill-xxxxxx/<page>",
    "pageType": "glass.xxxxx-skill-xxxxxx.<page>",
    "description": "…",
    "editBundleUrl": "https://ai-gop-public.oss-cn-beijing.aliyuncs.com/gop-ui/xxxxx-skill-xxxxxx/<page>/bundle.xml",
    "onlineBundleUrl": null,
    "latestBundleUrl": null,
    "flags": ["HOMEPAGE"]
  }
  ```

  `template.xml` 保存后会以 `bundle.xml` 形式放到公开 OSS（无需登录即可 GET），真机按 `uri` + bundle 地址下载渲染。
- 保存逻辑：按文件路径识别页面名和文件类型；`templateId` 为空时**新建**模板。因此手工创建也可行：新建文件夹 `Pages/<name>`，新建文件 `template.xml` 并保存，再保存 `data.json`。文件右键菜单有"设置为首页"。
- 在 SKILL.md 中引用页面：`{@UI页面渲染}(templateName="cast", dataSource={"url": "..."})`，官方工具编码 `tQ8fCF.ui_page_render`；或在技能根目录 `ui_schema.json` 里给某个工具配 Hook（`stage: "OUTPUT"`，条件根对象 = 工具出参，动作 `RENDER` + `templateName`）。两种都可，后者不依赖模型照抄地址，更稳。（真机上 Hook 从不触发，最后用的是前者，§9.5。）
- 编辑器 AI 可以生成页面（本次生成耗时约 12 分钟，产出见 §6），但它猜的属性值和文档有出入，以文档为准。

### 2.2 页面刷新机制（Waft-DSL）

- `<tick var="t" duration="秒" refresh="second|minute"/>`：所有 tick 共享一个 1000 ms 定时器；值为剩余秒数；全部归零后定时器销毁。要长时间刷新就把 `duration` 设大（如 3600）。
- 引用了 `{{tick:t}}` 的属性在值变化时重新计算（§7.6 绑定更新流程）。`<image src="{{url}}?t={{tick:t}}">` 即可每秒换地址。模拟器实测每秒重新下载、换图时保留旧图（`tools/simulator/README.md` §4 第 1 条）；真机【待验证】。**但只靠 `tick:t` 不够**：内核按完整 URL 缓存、跨页面有效，重开页面时 tick 从头倒数，`?t=86400、86399…` 会命中上次会话的缓存，必须再拼上 `{{f:timestamp()}}`（§9.2 E7）。
- 远程图片异步下载并缓存，所以地址必须变。URL 已含 `?` 时追加参数要用裸 `&`（DSL 不做实体解码，`&amp;` 会原样带入 URL，官方文档第 36 节 §9）；而裸 `&` 又让模板不是良构 XML（官方文档第 34 节 §5、§8 要求 XML 里写 `&amp;`）。所以本项目的页面让防缓存只占**一个**查询参数（`?ts={{f:timestamp()}}_{{d:i}}`），图片地址里根本不出现 `&`；`data://` 等事件路由里的 `&` 照文档写 `&amp;`（§9.2 E4）。
- `<page-meta screen-awake="keep"/>` 常亮（合法值 `short` / `keep`）；不设 `auto-exit-ms` 则不自动退出；`render-coalesce-ms` 用于云端高频推送页面的重绘折叠（方案一/二用不到）。
- `carousel` 的 `animation-interval` 是静态属性，不能驱动数据更新，不适合做刷新定时器。

### 2.3 图片预算

- 远程图解码后像素字节 ≤ 742 400 B（一屏 ARGB8888，640×290），文件 ≤ 1 MiB。PNG/GIF 最大 640×290，JPEG 约 640×386。超限：模拟器拒绝显示并告警；真机"显示异常或极慢"。
- 结论：**服务端先缩到 ≤ 640×290 再给**，`image-fit="stretch"` 救不了。

### 2.4 帧合集 `<frame-set>`

- 必填 `src`、`total-frames`；`frame-duration` 默认 41 ms，最大 1000 ms；`repeat-count="1"` + `on-completed` 才会触发完成回调（`0` 或手动帧模式不注册）。
- `on-completed` 是 URI 模板，触发时按页面当前上下文解析；可写 `data://update?i={{d:i + 1}}` 更新局部状态。原设想 `src="https://…/clip_{{d:i}}.fset"` 随之变化并重新下载——**模拟器实测不成立**：`src` 只在创建时求值一次，`d:i` 变了也不重新下载，页面只播第一段就停住（`tools/simulator/README.md` §4 第 2 条）。官方属性表里 `src` 的类型也只写"路径字符串"，明确可绑定的只有 `current-frame`、`action`。内核还按 URL 缓存下载结果、跨页面有效（同上第 3 条）。因此 clip 模式已下线（`skill/ui_schema.json` 不再绑定 `cast_clip`，页面移到 `reference/experiments/`）；高帧率需求已由 §9.3 的帧合集定时器方案（`pages/cast-fast`）满足，不再追切段机制。
- `color-format` 0=RGB565 / 1=ARGB8888 是解码目标格式；`cached` 是否整段读进内存；`placeholder` 对第三方实际不可用。
- 远程资源经 batched downloader 异步下载，下载期间显示占位图（如有）。

### 2.5 MCP 插件与工具调用

- 注册表单字段：插件名称、描述、基础 URL、认证方式（无认证 / Bearer Token / OAuth 2 Bearer Token / 自定义）、传输协议（streamable http）、endpoint（如 `/mcp`）。平台自动拉取工具清单。
- 工具必须 3000 ms 内返回；平台从云端访问 MCP，必须是公网 HTTPS。平台支持按这种方式接入（Bearer，STREAMABLE）。
- 平台执行客户端是 okhttp，会按 `Alt-Svc` 升级到 HTTP/3；服务端必须关闭 HTTP/3（本仓库 `deploy/` 下的 OpenResty 配置已处理）。
- 每次工具调用都经过大模型（秒级），所以 MCP 不能参与逐帧刷新。

### 2.6 设备与账号

- 真机测试的前提：控制台「设备管理」里绑定一副眼镜并开启开发者模式。之后在 Skill 编辑页 →「真机测试」→ 选设备绑定。
- 调研用的是新建的实验 Skill「电脑投屏实验」（slug `xxxxx-skill-xxxxxx`，下文称"实验技能"），内含编辑器 AI 生成的页面 `Pages/cast`。

## 3. 浏览器端模拟器（可本地运行）

编辑器的"千问 AI 眼镜 S1"预览就是 waft-lite 的 WASM 构建，资源全部公开：

| 文件 | 地址 | 大小 |
|---|---|---|
| 内核入口（ES module，导出 `createWaftLite`） | `https://g.alicdn.com/ai-fe/glasses-open-platform/0.0.34/waft-lite/index.blob-worker.js` | 13 KB |
| Emscripten 胶水（blob-worker 变体，`index.blob-worker.js` 引用） | `https://g.alicdn.com/ai-fe/glasses-open-platform/0.0.34/waft-lite/dist/waft-lite-wasm.blob-worker.js` | 489 KB |
| 普通胶水（普通入口 `index.js` 引用，可选） | `https://g.alicdn.com/ai-fe/glasses-open-platform/0.0.34/waft-lite/dist/waft-lite-wasm.js` | 488 KB |
| WASM | `https://g.alicdn.com/ai-fe/glasses-open-platform/0.0.34/waft-lite/dist/waft-lite-wasm.wasm` | 1.8 MB |
| 资源包（Emscripten `--lz4` 预载，内含字体、内置图片、示例 DSL、3 个 .fset） | `https://ai-gop-public.oss-cn-beijing.aliyuncs.com/res/waft/0.0.25/waft-lite-wasm.data` | 23 MB |

- 编辑器预览的调用方式：`createWaftLite({canvas, locateFile, initTimeoutMs, print, printErr, …})` → 可选 `setCorsProxy(prefix)` → `inject(xml, dataSourceJson, uri)` → 等渲染结果 → `refresh()` / `popCurrentPage()`。`uri` 形如 `waft://lite/dsl/<app>/<page>`。
- 页面必须 cross-origin isolated（COOP/COEP 头），否则 WASM pthreads 起不来。
- 编辑器预览里远程图片走平台的同源代理；本地自建时可不设代理，直接同源提供图片。

## 4. `.fset` 文件格式（实测确认）

对资源包里 3 个样本（`genie_ball.fset` 48 帧、`setup.fset` 122 帧、`transition.fset` 86 帧）逐字节验证：

```
offset 0      uint32 LE  frame_count (N)
offset 4      N × { uint32 LE offset, uint32 LE size, uint32 LE reserved(=0) }   // 每项 12 字节
offset 4+12N  帧数据首尾相接；第 i 帧 = file[offset_i : offset_i + size_i]
```

- 三个样本里每一帧都是完整的 **PNG**（8-bit RGBA，`89 50 4E 47 …`，以 `IEND` 结尾）；第一帧 offset = 4 + 12N，各帧连续，reserved 全 0，文件末尾正好等于最后一帧末尾。
- 帧尺寸不在文件头里，由 XML 的 `width`/`height` 与 `total-frames` 指定；`color-format` 决定解码后的像素格式。
- 样本引用方式：`<frame-set src="ext://tutorial/setup.fset" width="110" height="110" total-frames="122" frame-duration="33" color-format="1" placeholder="res://tutorial/setup_000.png"/>`（来自资源包里的官方示例 DSL）。
- 帧 blob 用 JPEG：模拟器实测黑屏，也不触发 `on-completed`，日志没有告警（`tools/simulator/README.md` §4 第 4 条）。只用 PNG 帧，JPEG 仅作实验选项。
- 调研时用 `genie_ball.fset`（32×30，48 帧，22 911 B）做格式测试夹具。官方样本属于平台资源包，不随本仓库分发；仓库的测试改用形状相同的自制夹具 `tests/fixtures/sample.fset`（由 `tests/fixtures/make_sample_fset.py` 生成）。

## 5. 带宽与延迟估算（未实测，按链路 100 KB/s 量级假设）

- 眼镜经手机网络 + 蓝牙后台通道取资源，实际吞吐未知；文档未给数字。
- 方案三：一张 640×290 屏幕内容 JPEG（质量 70）约 10～60 KB，1 秒内可下完，节奏稳定在 1 fps；延迟 ≈ 0.5 s（等刻度）+ 下载 ≈ 1～1.5 s。
- 方案一：PNG 帧对屏幕内容压缩尚可（纯色/文字），一帧 640×290 约 10～40 KB；10 帧一段约 100～400 KB，下载 1～4 s。延迟 ≈ 攒段 + 下载 + 段内偏移 ≈ 3～6 s，段间有停顿。要缩尺寸/降帧率换流畅。
- 可做双缓冲（两个 `<frame-set>` 交替预载，`auto-play="false"` + `action` 绑定）隐藏段间停顿【待验证】。

## 6. 编辑器 AI 生成的页面（Pages/cast，仅作参考）

```xml
<view width="100%" height="100%" flex-flow="column" bg-color="#000000">
  <!-- saved-as-template cast -->
  <page-meta screen-awake="always"/>
  <tick var="t" duration="1000" refresh="true"/>
  <image src="{{url}}?t={{tick:t}}" width="100%" height="100%" image-fit="stretch"/>
</view>
```

`data.json` 为 `{"url": ""}`。其中 `screen-awake="always"`、`refresh="true"` 不是文档列出的合法值（应为 `keep`、`second`），`duration` 单位是秒不是毫秒。实现时以文档为准。

## 7. 调研用的电脑与服务器环境

- 电脑：macOS；`uv`（Python 3.12）、Pillow 11、ffmpeg、node、cloudflared（只用于临时 quick tunnel）。macOS 自带的系统 Python 太旧，统一用 `uv` 管理的 Python。
- 服务器 `server.example.com`（SSH 用户 ubuntu）：Ubuntu，1Panel 管理的 OpenResty（vhost 在 `/opt/1panel/apps/openresty/openresty/conf/conf.d/*.conf`，证书在 `www/sites/<域名>/ssl/`），docker。证书用 `certbot/certbot` 容器 webroot 签发，HTTP/3 关闭；`deploy/` 下的文件按这套环境写。
- DNS：在域名服务商处给 `cast.example.com` 加一条指向服务器的 A 记录。

## 8. 待验证清单

（2026-10-23 列的清单。多数条目后来在 §9 的模拟器和真机实测里有了答案，汇总见 §1 开头的最终结论。）

1. 真机：`?t={{tick:t}}` 是否每秒重新下载并换图；换图是否闪烁（模拟器实测每秒重新下载）。
2. `frame-set` 的 `src` 绑定变化后是否重新下载并播放：**模拟器不成立**（`src` 只在创建时求值一次，`on-completed` → `data://update` 能触发、`d:i` 会变，但不重新下载）。真机未测，但已不影响方案：已由 §9.3 的帧合集定时器方案替代，clip 不再追。
3. `.fset` 内放 JPEG 帧能否解码：模拟器不能（黑屏），真机未测。
4. 真机实际下载吞吐（决定分辨率/帧率上限）。
5. 真机上 `screen-awake="keep"` 是否真的不息屏。
6. **（2026-10-24 被平台校验卡住，见 §9.4）** 真机上 `cast-fast` 的定时器循环是否成立：`action="{{d:act}}"` + `on-completed="data://update?…"` 每轮重新应用 `action` 让帧合集从头再播（模拟器成立，§9.2 E4）；记录拉图间隔中位数，与 `2 × frame_duration_ms` 对比。
7. 真机上 `frame-duration` 绑定数据是否生效（模拟器成立，§9.2 E9）：用 `--fast-fps 2` 启动，间隔应约翻倍。
8. 真机上 `frame-set` 加 `floating="true"` 后图片是否铺满 640×290、上下不被裁（模拟器成立，`pages/cast-fast/template.xml` 注释）。
9. 真机上 `{{f:timestamp()}}` 是否每次数据更新 / tick 刷新都重新取值（图片地址的 `ts=` 是否每轮都变，看 glasscast 访问日志）；`on-completed` 里 `&amp;` 的解析结果（文档说路由层会去掉残留的 `amp;`，模拟器没去掉但不影响循环，§9.2 E4）；平台保存模板时对 XML 的校验是否严格。
10. 平台实际交给 Hook 条件和 RENDER 页面的是平铺出参、`CallToolResult` 包装还是只有文本：`ui_schema.json` 和页面已同时兼容前两种（§9.2 E10）；记录调试面板里 `start_cast` 返回的样子。
11. 运行中调帧率（`start_cast(mode=fast, fps=N)`，2026-10-24 新增）：`cast-fast` 已经打开时 Hook 再次 RENDER 同一页面，按官方文档第 33 节 §4.2 是 `on-new-intent`（不重建页面，数据先注入），`frame-duration` 的绑定会不会随新注入的 `frame_duration_ms` 更新、正在循环的帧合集会不会按新时长播放，模拟器和真机都没测。若不更新，候选做法是在 `cast-fast` 里加 `<lifecycle-event on-new-intent="data://update?…">` 强制重新应用绑定（E4：`data://update` 会重新应用全部绑定），需先在模拟器验证；在此之前的兜底是先说"退出"再调帧率。

## 9. 模拟器实测（2026-10-23，补充）

### 9.1 平台编辑器的"预览 & 调试"面板

- 面板用同一套 WASM 内核，远程图片一律经平台同源代理拉取。实测对 trycloudflare 隧道地址代理返回 **503**，页面日志 `remote image failed: err=-1 download failed`；换成阿里云文档站的 PNG 也没有渲染出来（是否同样 503 未能确认，抓不到 worker 里发出的请求）。
- 面板只渲染**已保存**的模板；编辑器右上角的「预览」只是 XML 只读预览，不是模拟器。
- 这条链路只影响编辑器预览，**真机不经过这个代理**（眼镜经手机网络直连图片地址）。编辑器预览里远程图是否可用，待有自有域名后再测一次。
- 测试过程中平台登录态失效过一次（所有接口报 Network Error，刷新后要求重新登录）。

### 9.2 本地模拟器实验（`tools/simulator`，内核 waft-lite v0.2.11）

| # | 实验 | 结果 |
|---|---|---|
| E1 | `<image src="{{url}}?t={{tick:t}}">` + `<tick refresh="second">` | 每秒重新下载一次（间隔 1.00～1.02 s），换图不闪（内核保留旧图直到新图解码完）；首帧会多发 2～3 个请求（`?t=` 为空）|
| E2 | `<frame-set>` 的 `src` 用 `d:i`、`tick`、`dataSource://update`、`a:if` 任意方式改变 | **不重新下载**：`src` 只在节点创建时求值一次；`a:if` 是翻转显示（日志 `aif_flip`），不重建节点 |
| E3 | `on-completed="{{ctx:uri}}"` 让页面重开自己 | 无效，没有第二次下载 |
| E4 | **帧合集定时器**：`<data i="0" act="start"/>`，`<frame-set total-frames="2" frame-duration="100" repeat-count="1" action="{{d:act}}" on-completed="data://update?i={{d:i + 1}}&act=start"/>`，`<image src="{{url}}?i={{d:i}}">` | **循环成立**：每次 `data://update` 会重新应用全部绑定，`action` 的绑定被重新应用 → 帧合集从头再播 → 又触发 `on-completed`。图片每轮重新下载，周期中位 **217 ms**（2 帧×100 ms + 开销），即约 4.6 fps；4 帧×60 ms 时约 270 ms。`action="start"` 写成静态值则只播一次。`&` 两种写法都能循环：裸 `&` 两个参数都正确；写 `&amp;` 时内核不解码实体、路由层也没去掉残留前缀（日志 `data://update?i=N&amp;act=start`，`act` 没被写入），但 `d:i` 照常加 1、全部绑定照常刷新，`action` 仍是 `start`，循环成立。2026-10-24 复测 `&amp;` 写法（接真实 glasscast，`frame_duration_ms` 125）：中位 266 ms，与裸 `&` 相同。页面现用 `&amp;`（官方文档第 36 节 §10.3 约束 2，模板是良构 XML） |
| E5 | `.fset` 帧用 JPEG | 黑屏、无告警、不触发 `on-completed`；**只能 PNG** |
| E6 | 屏幕色彩 | 画布 R=B=0、G=亮度：**单色绿屏**。灰度图显示正常且体积小得多（灰度 JPEG ≈ 彩色的 70%，灰度 PNG 帧 ≈ RGBA 的 1/3） |
| E7 | 内核缓存 | 远程下载按完整 URL 缓存，内核生命周期内有效；地址每次必须不同。实测坑：重开页面后 `d:i` 从 0 重数，`?i=0…27` 全部命中旧缓存，服务端一个请求都收不到。用 `{{f:timestamp()}}` 拼进地址即可（`?ts={{f:timestamp()}}&i={{d:i}}`），它在每次数据更新时重新取值 |
| E9 | `frame-duration="{{frame_duration_ms}}"` 绑定数据 | 生效：绑定 100 → 图片拉取周期中位 0.219 s；绑定 400 与写死 400 都是 0.82 s。页面帧率可由工具出参控制 |
| E11 | 预声明多段：3 个 `<frame-set a:if="{{d:i == n}}" src="…seg{n}.fset">`，`on-completed` 把 `d:i` 加 1（2026-10-24） | 切段链本身成立（`route data://update?i=1/2/3`，`aif_flip=2`），但三段在页面打开后 0.07 / 0.10 / 0.14 s **一次性全部下载**，隐藏节点也不延迟加载。所以这只是"把一个大段拆成几个文件预取"，段里的内容是打开页面那一刻的，不是直播；节点数上限（64 个子节点）也限制了总长度 |
| E8 | 跨源 | 内核用 XHR（CORS 模式）下载，图片服务必须返回 `Access-Control-Allow-Origin`；设置 CORS 代理后所有下载都走代理 |
| E10 | 数据形状兼容（2026-10-24）：dataSource 用平铺 `{"url": …}` 或 MCP 包装 `{"content": […], "structuredContent": {"url": …}, "isError": false}` | **三元表达式不可用**：`{{url ? url : structuredContent.url}}` 能选对分支，但结果被截断到 **63 个字符**（64 字节缓冲，两个分支都一样；普通路径绑定不截断，90 字符照常），长一点的地址就坏了。**两段拼接可用**：`{{url}}{{structuredContent.url}}`，不存在的路径解析为空串，两种形状都得到完整地址（含 90 字符）；`frame-duration="{{frame_duration_ms}}{{structuredContent.frame_duration_ms}}"` 两种形状都生效。`cast-fast` 两种形状各 30 s，拉图间隔中位 282 / 283 ms，画面铺满；`cast-image` 1002 / 1006 ms。`{{x != '' ? …}}` 这类比较式的三元结果也不对（实测输出了字面量） |

### 9.3 对方案的结论

- **方案一（.fset 分段流）按原设计不可行**：帧合集无法换段。帧合集在本项目里的正确用法是当**定时器**（E4），不是当播放器。
- **高帧率投屏 = 帧合集定时器 + 图片轮询**（新页面 `cast-fast`）：帧率由 `frame-duration` 决定，模拟器里可到 4～5 fps；真机受下载带宽限制，`frame-duration` 要按实测带宽放大（例如 500 ms→2 fps）。请求与下载是异步的，如果下载慢于定时器会堆积，服务端应保证单张图小（灰度、≤ 640×290、JPEG 质量 60～70，目标 ≤ 15 KB）。
- **1 fps 模式（`cast-image`）**保留为最稳妥的兜底：只有一个 `<tick>`，没有帧合集下载。
- 两种页面都要走灰度 JPEG；`.fset` 定时器用一个最小的 2 帧灰度 PNG（约 100 字节/帧），随 glasscast 一起提供（`/s/<key>/timer.fset`）。

### 9.4 平台保存校验 URI-001 与内核探针（2026-10-24）

**现象**：把 `pages/cast-fast/template.xml` 贴进编辑器点「保存」，服务端保存接口返回
`success:false`，错误 `{"category":"SECURITY","ruleId":"URI-001","message":"\"{{d:act}}\" 没有 scheme——路由 URI 必须使用允许的 scheme（位于 <frame-set> 的 \"action\" 属性）"}`。
校验在服务端，编辑器 AI 的 XML 检查没有这条规则（它对 `action="{{d:act}}"` 和 `action="start"` 都报 0 ERROR），
但编辑器 AI 也拒绝把带 `action` 的模板写进模板库（它的写入路径不跑这条校验，但它把平台校验当作权威）。
官方完整参考（第 36 节 §3.3）明确写着 `action` 取值 `start / stop / pause / resume`、支持绑定，所以这是校验表把所有 `action` 属性当作 `<event action>` 路由 URI 的误判。

**服务端校验探针**（每次一个真实保存，间隔 ≥ 30 s，未触发风控）：

| 写法 | 结果 |
|---|---|
| `action="{{d:act}}"` | URI-001 拒绝 |
| `action="{{d:i >= 0 ? 'start' : 'data://x'}}"`（表达式里含 scheme 字面量） | URI-001 拒绝（"路由 URI 必须使用允许的 scheme"） |
| `action="start{{d:nil}}"`（字面量开头 + 空绑定） | URI-001 拒绝 |
| 不写 `action`（tick 页面，同 cast-image） | 保存成功 |

结论：经编辑器保存，`<frame-set>` 的 `action` 只要不以允许的 scheme 开头就进不了库；而以 scheme 开头的值内核又不认。

**内核探针**（本地模拟器 waft-lite v0.2.11，`tools/simulator`，接真实 glasscast，看内核日志里 `route data: data://update?i=N` 的次数，6 s 窗口）：

| # | 写法 | 6 s 内 update 次数 | 说明 |
|---|---|---|---|
| K0 | `action="{{d:act}}"`（对照） | 19 | 循环成立，约 3.2 轮/s（浏览器面板在后台） |
| K1 | `action="start"` 字面量 | 1 | 字面量属性在 `data://update` 后不重新应用，只播一遍 |
| K2 | `a:action="{{d:act}}"` / `ACTION="{{d:act}}"` | 1 | 内核只认小写无前缀的 `action` |
| K3 | `auto-play` / `delay-start` / `repeat-count` / `animation-name`（配 `<animation>` 子标签）绑定并每轮重新赋值 | 各 1 | 这些属性重新应用都不会让帧合集从头播 |
| K4 | 两个同源 frame-set 按 `a:if="{{d:s == 'a'}}"` / `'b'` 交替，`on-completed` 翻转 `s` | 2（i=1、i=2 后停） | `a:if` 只是隐藏/显示，不销毁重建；第二次显示的节点不再自动播放。E11 的"切段链"成立只因三个节点各只显示一次 |
| K5 | `action="start"` + `on-completed="dataSource://update?i=…"` | 1 | 主数据更新同样不重建节点 |
| K6 | `<data act="startx">` | 1 | 内核对 `action` 值精确匹配，不是前缀匹配 |
| K7 | `action="start{{d:nil}}"`（`<data nil=""/>`） | 22 | 内核侧可用（有绑定就会重新应用），但被服务端校验拒绝 |
| K8 | `action="{{d:i >= 0 ? 'start' : 'data://x'}}"` | 19 | 内核侧可用，服务端校验拒绝 |
| K9 | `a:for` 重建节点 | 未测 | 文档说数组长度变化会增删子节点，但 `data://` / `dataSource://update` 只能传字符串，页面内拼不出新数组 |

`gif` 组件没有 `action`，`repeat-count="0"` 时不注册 `on-completed`，也不能当循环定时器；内核 wasm 字符串里没有任何控制动画的 `waft://` 意图。

**对方案的影响**：帧合集定时器只能靠 `action` 绑定重启，而编辑器保存校验不放行 `action`，所以 **cast-fast（约 4 fps）目前进不了平台**。
平台上的 `cast-fast` 暂时存的是和 cast-image 相同的 tick 页面（1 fps 的过渡版，未随仓库提供），语音"流畅模式"仍可用、只是不更快。
可能的出路：① 走「创建 Skill → 上传技能」zip 导入（文档只给了截图，导入是否跑同一条校验未知）；
② 向平台反馈 URI-001 对 `<frame-set action>` 的误判（官方文档与校验矛盾）；③ 接受 1 fps。

### 9.5 真机首测（2026-10-24）：Hook 看不到 MCP 的结构化出参，页面要由模型打开

- 眼镜绑定编辑态技能后说"开始投屏"：平台调用了 `start_cast`（glasscast 日志 `start_cast: off → image`），播报"投屏已开始"，问"投屏状态"也正确；但眼镜上**没有任何页面**，glasscast 和 OpenResty 的访问日志里都没有来自眼镜的 `screen.jpg` 请求。
- 插件调试页「工具返回（toolOutput）」给出了平台实际拿到的出参形状：`{"summary": {}, "response": {"isError": false, "content": [{"type": "text", "text": "{...JSON 文本...}"}]}}`。**没有 `structuredContent`，也没有平铺字段**（glasscast 两种都返回了，平台丢弃了 structuredContent）。所以 `ui_schema.json` 里 `success == true` 和 `structuredContent.success == true` 两条条件永远不命中，Hook 不会 RENDER（§8 第 10 条有了答案：形状是"只有 content[0].text 文本"）。
- 解决：走官方文档第 31 节 §1.1 的另一条路——在 SKILL.md 里引用官方工具「UI页面渲染」（`tQ8fCF.ui_page_render`，编辑器「工具」按钮 → 官方镜端插件 → UI页面渲染 插成卡片），语法 `{@UI页面渲染}(templateName="cast-image", dataSource={"url": "<返回的 url>"})`，由模型在 `start_cast` 成功后自己调用，`dataSource` 的字段从工具返回的 JSON 文本里照抄。`skill/SKILL.md` 已按此改写（新增「打开页面（必读）」一节，规则 1 改为"页面由你打开"）。`ui_schema.json` 保留但永远不命中，待验证模型路线后再决定删不删。

### 9.6 真机首测（续）：图片地址必须以 .jpg 结尾；截屏权限的假象

- 页面打开后（§9.5 的改法生效），眼镜上只有标签文字和 tick 倒数，没有图。日志里**每秒一次** `GET /s/<key>/screen.jpg?ts=…` 都是 200、约 26 KB，请求方 User-Agent 是 `千问AI穿戴/<版本号> CFNetwork/… Darwin/…`——即 iPhone 上的千问穿戴 App 替眼镜下载，再经蓝牙送到眼镜。
- 逐项排除：改成彩色三通道 JPEG、把画布缩到 600×272（避开 742 400 B 的预算边界）、再缩到 320×145 / 质量 40（约 5 KB），眼镜都不显示；`floating` 标签改回普通布局流、图片外套深绿色容器后，文字和绿色容器都能看到——说明页面在最上面，是 `<image>` 本身没渲染。
- **命中**：把地址从 `…/screen.jpg?ts=<时间戳>_<序号>` 改成 `…/screen.jpg/<时间戳>_<序号>.jpg`（nginx 把尾段改写回 `screen.jpg`）后，画面立刻出现。结论：真机/穿戴 App 的原生下载器按 URL **后缀**判断图片类型，带查询串的地址"下载成功但不渲染"；模拟器的 wasm 内核不挑。glasscast 已原生接受 `/s/<key>/screen.jpg/<stamp>`（`server.py`），两个页面模板和 `tests/test_pages.py` 已改成路径式缓存破坏。
- ~~之后换回默认 640×290 灰度 JPEG（约 26 KB）仍然每秒显示~~ **更正（2026-10-24 复核）**：这句有误。换回 640×290 灰度后用户报告"又没有画面了"，另一次复现（640×290 灰度，约 20 KB）同样不显示。见 §9.7。
- **截屏权限的假象**：从一个没有「屏幕录制」权限的 App（例如某个桌面 App 内置的终端）启动的 glasscast 截到的始终是桌面壁纸（没有菜单栏、程序坞和窗口），这正是 macOS 未授予「屏幕录制」权限时的返回结果；之前把它当成"真实桌面"是误判。权限按**启动进程的 App** 授予：在哪个终端 / App 里启动 `glasscast serve`，就要给哪个授权。

### 9.7 复核一轮"强制流畅模式"的改动（2026-10-24）

复核此前为"强制流畅模式"做的改动，结论：

**真机能显示什么（以日志 + 用户反馈为准）**

| 画布 / 色彩 / 质量 | 单张 | 真机 |
|---|---|---|
| 640×290 灰度 70 | 19～26 KB | 下载 200，不显示（两次复现） |
| 600×272 彩色 70 | 9～21 KB | 显示（当时的记录） |
| 480×218 彩色 40 | 约 10 KB | 显示（真机拉图，用户确认） |
| 320×145 灰度 40 | 约 5 KB | 显示（用户看到桌面壁纸） |

所以"灰度不显示、彩色才显示"的结论不成立：320×145 灰度能显示。共同点是 **640×290 不显示**。640×290×4 = 742 400 B 正好等于文档的远端图解码预算上限，真机多半是"达到上限即拒绝"，也可能是单张体积；两者都随画布缩小而消失。`.env` 固定 `GLASSCAST_COLOR_MODE=rgb`、`GLASSCAST_CANVAS=480x218`、`GLASSCAST_JPEG_QUALITY=40`（已验证组合）；代码默认值 640×290 不变（模拟器、测试都用它），但 `glasscast serve` 现在会在画布 ≥ 640×290 时打印提醒。

**"fast/6 在跑，画面还是 1 fps"不是平台打开错了页面**：平台上的 `cast-fast` 早已是 1 fps 的 tick 过渡版（帧合集定时器版被 URI-001 拒绝，§9.4）；平台上三个页面 `cast`、`cast-image`、`cast-fast` 现在内容完全相同。所以无论打开哪一个都是每秒一张；服务端 `--autostart fast --fast-fps 6` 只是在电脑上空转 6 fps 截屏。之后日志里的 `start_cast(fast)` 都来自编辑器沙箱（同时段只有 Mac 浏览器在拉图），上表 480×218 那次真机拉图之后真机没有再测过。

**工具卡片的存储格式**：平台把卡片存成 `^^插件编码.工具编码^^` 文本，编辑器载入或粘贴时转成卡片。直接保存含 `^^XXXXXX.start_cast^^` 的文本，`tool_schema.json` 会正确生成。

**平台工具超时仍会偶发**：同一时段插件调试页 `cast_status` 6/6 成功（平台两次请求间隔 0.5～0.7 s），沙箱里 `start_cast` 先后失败两次再成功一次。失败的两次一次是平台两次请求间隔 1.5 s（服务端总处理 < 0.2 s），一次是 nginx 记 499（平台先断开）且重试请求根本没到服务器——都在平台到境外服务器这一段。服务器到电脑这一段热连接约 0.25 s/请求，48 个并发请求没有卡住。对策：SKILL.md 全局规则 6 改成"超时立刻用同样参数重试一次，仍失败才播报"（三个工具都幂等），实测重试会被执行。

### 9.8 流畅模式的新路：emit:// 事件总线自环（2026-10-24，仅模拟器）

帧合集定时器被 URI-001 挡住后，又把文档和内核里所有可能的亚秒级触发源过了一遍（查文档 + 本地模拟器）：

- **排除**：`<gif>`（src 只在创建时取值一次，`repeat-count="1"` 只播一遍，换地址不重载，模拟器实测）；`carousel`（没有事件）；`markdown-view` 自动滚动（有毫秒间隔但没有事件）；`recycle-view`（只有按键或云端推送会推动它）；ui_schema `renderMode: update`、MCP 流式返回（都要经过模型，秒级）；`a:if`/`a:for` 重建节点。
- **可行（模拟器）**：页面给自己发事件。`<lifecycle-event on-load="emit://event/h0?"/>` 起头，`<event match="event://hK?" action="emit://event/hK+1?"/>` 串成 N 跳，最后一跳 `action="data://update?f={{f:timestamp()}}"` 并回到 h0；`<image src="…/{{d:f}}.jpg">` 每 N 跳换一次地址。`emit://` 是异步的（下一轮消息循环才发布），所以是一个节拍器；纯 emit 不刷新绑定，只有最后一跳的 data:// 刷新。所有 URI 属性都以 scheme 开头，结构上不会撞 URI-001。

| 跳数 | 模拟器图片请求速率 | 间隔中位 / 最大 |
|---|---|---|
| 2 | 15.0 次/秒 | 67 / 89 ms |
| 4 | 10.1 次/秒 | 100 / 138 ms |
| 8 | 3.6 次/秒 | 283 / 303 ms |

每次数据更新恰好一次图片请求，没有堆积。模板在 `reference/experiments/cast_emit/`。

**平台保存探针（2026-10-24，已通过）**：在实验技能里新建测试页 `Pages/cast-emit`（未被技能引用），保存 `reference/experiments/cast_emit/cast-emit.4hops.xml` 的内容，保存直接成功，没有 URI-001。平台为设备编译的 bundle（`…/gop-ui/xxxxx-skill-xxxxxx/<page>/bundle.xml`）内容未被改写，放进本地模拟器（数据用 `structuredContent.url` 形状）照样跑：10 秒 106 次拉图，约 10.6 次/秒，间隔中位 99 ms。`cast-image`、`cast-fast` 未改动，什么都没发布。

**仍未验证**：① ~~平台保存校验是否放行~~（已放行）；② 真机每跳耗时和下载速度（图片由 iPhone 千问穿戴 App 下载再经蓝牙送到眼镜，换地址快于下载完成时可能重复请求，需要按真机实测调跳数）；③ 一个约 30 Hz 的事件循环对眼镜 CPU 和电量的影响。

**其他路**：Agent 自定义接入可以由服务端主动推 `SHOW_UI` 的 `UPDATE` 换图片地址（官方文档「Agent 自定义接入」中 `SHOW_UI` 一节），但要内测白名单、公网 wss 端点，且插件和关联技能要由用户本人发布；zip「技能上传」导入大概率走同一套校验；可在官方钉钉答疑群反馈 URI-001 对 `<frame-set action>` 的误判（文档写明 action 支持绑定）。

**接入流畅模式（2026-10-24）**：
- 正式页面 `pages/cast-emit/template.xml`（当时的版本，现已换成 §9.19 的倒数版）：第一版是 16 个 topic h00～h15、每跳 3 条 event；审查后改成下面「审查后修订」里的 25 跳版本。跳数 n 由 dataSource 的 `hops` 写入 `d:n`（on-load 与 on-new-intent 都写，缺省 8）。属性里没有小于号和 and 号（内核不解码实体）。
- glasscast：`start_cast` 出参新增 `hops = round(1000 / fast_fps / emit_hop_ms)`（第一版 1～16，现为 1～25），`emit_hop_ms` 默认 20（`GLASSCAST_EMIT_HOP_MS` / `--emit-hop-ms`）。默认 4 fps → 12 跳；模拟器 12 跳约 4.2 张/秒、8 跳约 6.5、4 跳约 10.6、2 跳约 19（每跳 15～35 ms 随负载变化）。运行中 2→12 跳经 on-new-intent 切换，链条不断、不重复。
- SKILL.md：流畅模式（含调帧率）改为 `UI页面渲染(templateName="cast-emit", dataSource={"url", "hops"})`；`cast-fast` 不再被引用（平台上仍是 1 fps 的 tick 页）。编辑器沙箱实测：「用流畅模式投屏」→ `start_cast(mode=fast)` → `UI页面渲染(cast-emit, url, hops=12)` → 页面渲染成功 → 播报"已用流畅模式投屏"。编辑器预览的内核在后台标签页里超时，拉图不可作为测速依据。
- **真机标定方法**：看 glasscast 访问日志里 `千问AI穿戴` 的 `screen.jpg/<时间戳>_<tick>.jpg` 请求。现在每次绑定刷新时间戳都会变，所以看两点：① 每秒请求数（约等于 page_fps 说明事件链在跑，约 1 次说明只剩 tick 兜底）；② 同一个 `_<tick>` 后缀下有几次请求（事件链在跑时一秒内多次请求共用同一个 tick 值）。每跳耗时 ≈ 请求间隔中位数 ÷ hops，按它改 `GLASSCAST_EMIT_HOP_MS` 重启即可，不用改页面。

**审查后修订（2026-10-24）**：

- **内核限制：根 view 下最多 64 个子元素**。第 65 个子元素起整页 `parse FAILED`，页面打开但什么都不跑。模拟器对照：平铺 64 个子元素能解析、65 个失败；20 跳旧结构（67 个子元素）失败、19 跳（64 个）通过。嵌套页面另有一个更宽的上限（两个子 view 各 31 个 label 共 68 个元素能解析，各 40 个失败），未细测。
- **更正**：之前记过「整页 `{{…}}` 表达式总字节数上限约 1191」，是测量方法的问题：渲染后固定等 1.8～2.5 秒就读日志，后台标签页里内核被节流，还没跑到解析结果，「能解析」的读数其实是「还没出结果」。改成轮询日志等 `parse FAILED` 或第一批 `route emit` 后，结论变成上面的子元素上限，和表达式长度无关。
- **页面结构改为每跳 2 条 event**（脚本生成，`--check` 校验；脚本现在是 `reference/experiments/cast_emit/gen_cast_emit.py`，仓库里留的输出 `cast-emit.loop.xml` 是 §9.13 定因实验的版本）：`w` 是第 0 跳，可见时写 `d:f`（换图），再按 n 发 h01 或直接回到 w；第 k 跳 `d:n > k+1` 发下一跳、`k+1 >= d:n` 回到 w；最后一跳 h24 无条件回到 w。子元素 7 + 2×25 = 57 个，topic 25 个（每页最多 32 个 match 条目），都留了余量。默认 20 ms 一跳时页面最慢约 2 张/秒；`fast_fps` 低于 2 时 glasscast 仍按 fast_fps 截帧，页面多拉的是重复帧，`start_cast` 出参 `page_fps` 报告页面实际拉图速率。
- **其他修订**：被遮挡时（on-hide）`v` 置 0，链条照转但不换图，on-show 恢复；图片地址改成 `{{url}}{{structuredContent.url}}/{{f:timestamp()}}_{{tick:t}}.jpg`，每次刷新都是新地址，首帧不会命中内核按 URL 的跨会话缓存；hops 判断改成 `{{hops != 0}}`，模型把 hops 当字符串传也能写入。status.json 的 `page.fast`、`glasscast config`、`glasscast set-fps` 都报告 hops。
- **模拟器验证（4 个标签页并行跑正确性，单标签页测速）**：

| 场景 | 结果 |
|---|---|
| hops=4 可见 → 遮挡 → 恢复 | 每圈 4.0 跳；遮挡期间链条照转、换图 0 次；恢复后照常换图 |
| hops 传字符串 "4"，再经 on-new-intent 改成 "12" | 每圈 4.1 跳 → 12.6 跳 |
| 不传 hops、hops=0 | 保持默认 8 跳 |
| hops=40（超上限） | 在第 25 跳回到 w，链条不断 |
| 运行中 25 → 2 → 12 → 1 | 每次都在下一跳按新值收尾，链条不断 |
| 测速：12 跳（4 fps 默认） | 约 3.75 张/秒，每跳约 22 ms |
| 测速：25 跳 | 约 1.9 张/秒，每跳约 20 ms |

- 平台：实验技能的 `Pages/cast-emit` 已保存为 25 跳版本（注释缩成一行，去掉注释后与仓库逐字节相同，sha256 前 16 位 `4ca5034869af5623`），SKILL.md 的调帧率播报改成「已调到每秒 N 张」（去掉「画面会重新打开」）。cast-image、cast-fast 未改动，什么都没发布。

### 9.9 真机首测流畅模式 + 「电脑上的投屏程序没有响应」的原因（2026-10-24）

**页面没问题**：cast-emit（25 跳版本）在真机上能打开并跑事件链。眼镜（`千问AI穿戴` App）每秒拉 6～9 张（hops=12），比模拟器估算的约 4.2 张加 1 张兜底还多，之后再按 §9.8 的标定方法调 `GLASSCAST_EMIT_HOP_MS`。

**「没有响应」是平台调用电脑超时**：平台每次工具调用都要在 3000 ms 内完成（文档「执行链路工具调用限制」），当前插件传输协议是 Streamable HTTP，每次调用是两个串行请求：先 `initialize`，再 `tools/call`。实测：

| 证据 | 结果 |
|---|---|
| 沙箱复现 | 平台返回 `{"maPattern":"工具调用失败", … "tts":"工具执行失败。MCP 工具执行超时"}`（`is_error` 却是 false）|
| 失败率 | 约 20 分钟内平台约 18 次工具调用，11 次超时（成功的 7 次在电脑端都只用了几毫秒）|
| glasscast 处理耗时 | 每个请求 2～130 ms，不是瓶颈 |
| 境外服务器 → Mac 隧道 | 热连接每个请求约 0.20～0.25 s（就是一次网络往返），25 次无一卡住 |
| 境外服务器 → device.qianwen.com | TCP 建连 0.40～1.44 s（1.44 s 是丢包重传），TLS 完成 1.4～1.9 s |
| 失败形态 | 有时只到了 `initialize`，`tools/call` 根本没到；有时 `tools/call` 在代理上记为 499（平台先挂断），却没到 glasscast（代理要先收完请求体才转发，请求体在跨境链路上丢包重传） |

结论：瓶颈是平台（中国大陆）到境外服务器上的代理这一段跨境链路，往返约 0.4 s 且丢包；每次调用两次串行请求，再加隧道两次约 0.5 s，经常超过 3 s。电脑端程序一直在正常运行。

**已做**：SKILL.md 规则 6 和 E 类话术点名平台的超时提示（"工具调用失败""工具执行失败""MCP 工具执行超时"，即使 `is_error` 为 false），要求先不说话、同参数再调一次。沙箱验证：改之前模型超时后直接播报失败；改之后每次超时都会重试一次。

**已改传输协议（2026-10-24）**：插件 XXXXXX 的传输协议从 Streamable HTTP 改成「无状态协议」（只改这一项，仍是草稿，未发布）。之后每次工具调用只有一个 `POST /mcp`，不再先发 `initialize`。

改完后的沙箱结果（样本少，仅供参考）：

| 阶段 | 结果 |
|---|---|
| 改协议前、改话术前 | 超时后直接播报"没有响应"，3 次全部失败 |
| 改话术后、仍是 Streamable | 会重试，但 2 次都是两次都超时 |
| 改协议后 | 4 次需要调用工具的试验全部打开了页面；其中 3 次第一次超时、重试成功 |

第一次仍常超时：有的请求根本没到境外服务器上的代理；有一次请求到了 glasscast、4 ms 就回了，平台仍判超时。剩下的延迟全在大陆到境外服务器这一段，重试能兜住大部分。

**可选改进（按收益排序）**：
1. ~~插件传输协议改成「无状态协议」~~（已改）（文档 5.2：无需预先建立连接，直接发 Function Call）：每次调用少一个跨境往返和一次隧道往返。glasscast 本来就是 stateless + JSON 响应，直接收 `tools/call`（不带 `initialize`、Accept 为空、`*/*` 或 `application/json`）都正常返回；只有 Accept 只写 `text/event-stream` 时 406。
2. 若保留 Streamable HTTP：在服务器的 OpenResty 上直接应答 `initialize`（Lua），省一次隧道往返（约 0.25 s）。
3. 根治：把公网入口放到对大陆线路好的机器上，跨境往返从约 0.4 s 降到几十毫秒。

**附带发现**：1Panel 给本站开了默认访问日志（`log/access.log`，main 格式），里面有带 cast-key 的图片地址；deploy.md 原来写的是 `access_log off`。日志目录是 0700，但仍应关掉或换 key 时一并处理。

### 9.10 流畅模式在真机上不出画面：图片加载被不停打断（2026-10-24）

**现象**：第一次在眼镜上开 cast-emit（hops=12）时，手机端 `千问AI穿戴` 每秒下载 6～9 张图（全部 200），但用户说眼镜上没有画面，只看得出光机亮着。之后再开流畅模式，页面根本没跑（一张图都没请求），眼镜直接暗掉，语音却说"已用流畅模式投屏"。

**原因（推断，待真机确认）**：真机上每张图要手机下载、再经蓝牙传到眼镜，耗时远超 0.2 s；旧页面每圈（约 0.17 s）都换一次地址，再加 tick 每秒一次，加载还没完成就被新地址打断，所以一张都显示不出来。普通模式每秒换一次能显示，说明真机单张加载在 1 s 以内。

**模拟器补充实测**：内核每次绑定刷新都会让图片重新加载，地址没变也会再请求（用 `<data>` 做计数器时同一地址被请求约 3 次）；所以不能靠 data 里的计数器把换图间隔拉长。帧合集计时器也不行：用 `a:if` 重建的 `<frame-set>` 只会触发一次 `on-completed`，而它的 `action` 绑定又过不了平台保存校验（URI-001）。`<tick>` 只有秒和分两档。能用的亚秒节拍只剩 emit 自环，最多 25 跳（真机约 0.35 s，模拟器约 0.5 s）。

**改动**：
- 页面去掉 `<tick>`，图片地址改成 `{{url}}{{structuredContent.url}}/{{d:f}}.jpg`；`d:f` 在加载时写一次时间戳，之后只由 `w` 每圈写一次。模拟器：25 跳时 5 秒 10 次请求、10 个不同地址（每张只请求一次）；遮挡期间 0 次请求，恢复后继续。
- `.env` 加 `GLASSCAST_FAST_FPS=2`：流畅模式默认 2 张/秒，即 hops=25，换图最慢。
- 平台 `Pages/cast-emit` 已保存（去注释后与仓库一致，sha256 前 16 位 `0d1a0b529e55c7f5`）；沙箱「用流畅模式投屏」→ `start_cast` 一次成功 → `ui_page_render(cast-emit, hops=25)` 渲染成功，预览每秒拉 2～3 张。

**待真机确认**：① 重启眼镜后普通模式是否正常（排除后来那次眼镜状态异常）；② 流畅模式 2 张/秒时是否出画面。出画面就说明真机单张加载在约 0.35 s 内；仍不出画面就说明这条下载链路做不到比每秒 1 张更快，流畅模式应该退回普通模式的速度。

### 9.11 真机只出一帧旧图：时间戳存进 data 被卡在 int32 上限（2026-10-24）

代理访问日志里眼镜请求的地址是 `…/screen.jpg/0.jpg`，之后全是 `…/screen.jpg/2147483647.jpg`：真机把 `data://update?f={{f:timestamp()}}` 写入的 13 位时间戳**卡在 int32 上限 2147483647**（模拟器是按 32 位绕回，每次都不同，所以模拟器发现不了）。地址不再变化，眼镜只下载一次；之后再开页面直接用缓存里的这张旧图，一张都不请求，看到的是很早以前的电脑画面。

同时去掉了 on-hide/on-show 门控：真机上语音播报会遮挡页面，之后不一定收到 on-show，换图会永远停住。

**改法**：`d:f` 改成每圈加一的小计数器 `data://update?f={{d:f + 1}}`；时间戳只在地址里现算：`{{url}}{{structuredContent.url}}/{{f:timestamp()}}_{{d:f}}.jpg`（没有 tick，绑定刷新只发生在每圈一次的 f 更新上，所以每圈正好一个新地址）。平台已保存（去注释 sha256 前 16 位 `887a9d2d9daacbc8`）。教训：data 里只存小整数。

### 9.12 流畅模式真机结论：这条图片链路显示不了每秒 1 张以上（2026-10-24）

9.11 修好后，眼镜确实按每秒 3～4 张请求新地址（计数从 0 涨到 102，每张都下载成功），但眼镜一直黑屏，只看得出光机亮着。把图缩到 320×145 灰度质量 30（每张 2.4～4.2 KB，原来约 9.7 KB）再测，眼镜每秒请求 5～7 张，仍然黑屏。

结论：每张图要经手机下载、蓝牙传到眼镜再解码，眼镜在下一张地址到来前完成不了，换图快于约每秒 1 张就一张都显示不出来；缩小图片也不够。页面上能做的最慢节拍是 emit 自环 25 跳（真机约 0.3 s），更慢只能靠 data 计数器，而每次 data 更新都会让图片重新加载。普通模式（tick 每秒一张）已经接近这条链路的上限。

已把图片设置恢复为真机验证过的 480×218 彩色质量 40。要真正更流畅，只能换不经过"手机下载图片"的推送通道（例如 Agent 自定义接入推 SHOW_UI UPDATE，需要用户本人发布插件和技能）。

### 9.13 定因实验：事件链慢速换图（2026-10-24，待真机结果）

目的：区分「换图太快、眼镜来不及显示」和「事件链触发的换图在眼镜上本身不显示」。页面改成每 3 圈才换一张（`LAPS_PER_IMAGE=3`），地址 `{{d:s}}_{{d:f}}.jpg` 只用两个小数字：会话号 `s` 在加载时写入 `f:formatDate('now','MMddHHmmss')`（10 位数字，小于 int32 上限；模拟器实测表达式里的除法会被忽略，`f:timestamp() / 1000` 仍是 13 位），`f` 是换图计数；圈数 `k` 变化时地址不变。模拟器：每约 1.3 s 换一张，两次换图之间没有多余请求。平台已保存（去注释 sha256 前 16 位 `3e6d7ed0bcc4a88c`）。

真机预计间隔（每跳约 13 ms）：「用流畅模式投屏」（hops 25）约 1 s；「调到 3 帧」（17）约 0.66 s；「调到 4 帧」（12）约 0.47 s；「调到 6 帧」（8）约 0.31 s。

**真机结果：只显示一帧，原因确定。** 地址确实按计划在变（`<会话号>_1` … `_12`，约每 0.9 s 一张），但每张图都被请求两次：换图那一圈一次，约 0.3 s 后的下一圈（只是圈数 k 加一、地址没变）又一次。说明**真机每次绑定刷新都会让图片从头重新加载，地址没变也一样**（模拟器不会）。手机很快下载完，但图要经蓝牙传到眼镜再显示，需要的时间超过一圈（约 0.3 s），所以每张都被下一圈的刷新打断。唯一显示出来的那一帧，是页面加载后事件链停了约 10 s（语音交互期间）时的第 0 张。

**结论**：眼镜要求两次绑定刷新之间至少隔零点几秒到 1 秒；emit 自环每圈都得刷新一次（真机最多约 0.3 s 一圈），无论每几圈换图、图多大都显示不出来。普通模式（tick 每秒一次）正好满足这个间隔。流畅模式在这块眼镜上走不通，除非换成不经过页面图片加载的推送通道。

### 9.14 局域网直连：手机在家直接从 Mac 取图（2026-10-25）

手机和 Mac 在同一个局域网时，原来眼镜每张图都要「手机 → 境外服务器上的代理 → SSH 隧道 → Mac」绕一圈，热连接也要约 0.4 s，新建连接约 0.7 s。改成：

- 路由器（192.168.1.1）：给 Mac 一个静态 DHCP 租约 192.168.1.50（macOS 的 Wi-Fi 私有地址要设成「固定」，否则硬件地址会变）；路由器的 DNS 里加一条覆盖，让 `cast.example.com` 解析到 192.168.1.50（dnsmasq 写法 `address=/cast.example.com/192.168.1.50`）。如果路由器开着 DNS 重定向，手机手动设的 DNS 也会被接管，连 `dig @1.1.1.1` 都会拿到局域网地址。
- Mac：证书从服务器复制到 `~/Library/Application Support/glasscast/tls/`（0600，`deploy/sync_lan_cert.sh` 重新同步）；`tools/lan_https_proxy.py` 在 443 上做 TLS，转给 127.0.0.1:8766。局域网取一张图约 15 ms（经境外服务器约 0.7 s）。
- 隧道保温：`deploy/tunnel.sh` 的保温请求用 `--resolve` 钉到经 DoH（https://1.1.1.1/dns-query）查到的公网地址（`GLASSCAST_WARM_RESOLVE`，默认 auto），否则会落到本机、境外服务器的隧道连接变冷。顺带修了脚本在中文环境下 bash 3.2 把「变量名后紧跟中文」当成变量名的一部分（`LOCAL_PORT（`）的老问题。
- 平台调用电脑（MCP）仍走公网解析 → 境外服务器 → 隧道，不受影响。离开家里 Wi-Fi 时手机走公网解析，也照常经境外服务器。

### 9.15 停止投屏会关掉眼镜上的页面（真机上不生效，见 §9.17）；流畅模式结论（2026-10-25）

- 不再让用户说「退出」（系统级指令，容易冲突）。两个页面都加了 `<lifecycle-event a:if="{{stop != 0}}" on-load/on-new-intent="waft://app/exit-page?appid={{ctx:appId}}"/>`；技能在「停止投屏 / 退出投屏 / 关闭投屏…」时先调 `stop_cast`（超时照常重试一次），不管结果如何再分别用 `templateName=cast-image` 和 `cast-emit`、`dataSource={"stop": 1}` 打开一次，页面收到就关闭自己。平台已保存（cast-image 去注释 sha 前 16 位 `591f3791c8dfb5ea`，cast-emit `8ed517a2db9f8896`，SKILL.md 6 处替换、4 张卡片完好），未做验证。本地 cast-image 之前被套了两层根 view，已按平台版本重建。
- 用户实测（局域网直连 + 320×145 灰度质量 30 小图）：**普通模式有画面**（灰度小图确认能显示），**流畅模式仍无画面**。日志里每张图在每圈刷新时都被重新请求，说明即使网络只要几毫秒、图只有 2.4 KB，蓝牙传到眼镜再显示仍超过一圈（约 0.3 s）。结论同 9.13：页面加载图片这条路做不出比每秒一张更快的投屏。

### 9.16 文字探针：事件循环触发的刷新能不能在眼镜上显示（2026-10-25，待真机）

思路：把事件循环拉到最长，先只显示文字，看眼镜上能不能刷新，排除图片加载这一环。页面 `reference/experiments/cast_emit/text_probe.xml`：32 步一圈（每页最多 32 个 match 条目，真机约 0.4 s），每圈只在最后写一次 `d:f`，屏幕显示「流畅测试：每圈加一」和大号圈数；保留 stop 关闭规则。模拟器：能渲染，4 s 加了 7 次，stop 后页面执行 exit-page 并销毁。平台 `Pages/cast-emit` 暂时换成这个探针（去注释 sha 前 16 位 `d5fe469956bb9563`）；仓库 `pages/cast-emit/template.xml` 仍是图片版，测完要换回去。

### 9.17 探针二、三：地址不变的图能显示；每圈换图待测（2026-10-25）

- 文字探针（9.16）真机结果：数字一直在跳，事件循环触发的刷新能在眼镜上显示。
- 探针二（`static_image_probe.xml`，圈数 + 一张地址不变的图）真机结果：圈数和图都显示。日志里这张图只在页面打开时请求了两次（相隔约 0.6 s），之后每圈刷新都不再请求。说明「每次刷新都重新加载同一张图」只发生在图还没加载完时；加载完就不会了。对比之前失败的测试：那时一圈 25 步（约 0.3 s），现在 32 步（约 0.4 s），且已走局域网、图约 3.7 KB。
- 「停止投屏」时用 stop=1 打开页面让它自己关闭，在眼镜上不生效（页面照常打开、圈数照常跳），反而像开始了投屏。已从技能里撤掉这一步（停止只停电脑端截屏，不调 UI 页面渲染）；页面里的 stop 规则留着无害。关闭指令暂不再测。
- 探针三（`per_lap_image_probe.xml`）：32 步一圈，每圈换一张新图（地址 会话号_圈数），屏幕显示圈数。平台 `Pages/cast-emit` 现为此页（去注释 sha 前 16 位 `bf87e2061efe8ae7`）。

### 9.18 每圈换图不显示；改测每 2 圈换图（2026-10-25）

探针三真机结果：圈数正常，图一张都没显示。日志：83 张图里 82 张只请求了一次（局域网几毫秒下载完），新图间隔 0.4～0.5 s。结合探针二（地址不变的图能显示）和普通模式（每 1 s 换一次能显示）：打断显示的是图片地址变化，不是页面刷新，也不是网络；手机下载后经蓝牙传到眼镜再解码需要超过约 0.5 s、不到 1 s。

探针四（`every_n_laps_probe.xml`）：32 步一圈，每圈写圈数 f；每 2 圈（约 0.85 s）换一张新图（地址 会话号_换图次数 g），屏幕显示「圈 f · 图 g」。平台 `Pages/cast-emit` 现为此页（去注释 sha 前 16 位 `1b0c367fece6c0fe`）。能显示 → 眼镜单张处理 < 0.85 s，流畅模式约 1.2 张/秒；不能 → 接近 1 s，普通模式即上限。

### 9.19 每 2 圈换图也不显示：换图前的刷新会打断加载；改用事件参数计数（2026-10-25）

探针四真机结果：圈数在跳，只显示了一张图。日志：每张图被请求 3～6 次——每圈末尾连写圈数、计数器（到点再写换图编号），一圈里刷新两三次（相隔约 40 ms），图还没在眼镜上显示出来时，每次刷新都让它重新加载。对照：地址不变的图（每圈一次刷新）请求两次后显示；每圈换图（换图之间没有额外刷新）每张只请求一次但 0.45 s 不够。所以要同时满足：换图间隔够长（约 0.6～1 s），且换图之间不刷新。

探针五（`countdown_probe.xml`）：计数放进事件参数，不写 data。`emit://event/dN?n=N`，`<event match="event://d1">`～`d9`（前缀匹配，覆盖所有正整数）动作 `emit://event/d{{e:n - 1}}?n={{e:n - 1}}`；n 到 0 时只有 `event://d0?` 匹配，写一次 `g` 并以 `d:m` 重新开始。每张图隔 m 步，m = hops × 3（默认 75 步约 1 s；调到 3/4/6 帧时 51/36/24 步）。模拟器：只在换图时写 g，中间零写入。平台 `Pages/cast-emit` 现为此页（去注释 sha 前 16 位 `171a3cb2ddfc2bde`）。

### 9.20 探针五真机门槛；图片大小的影响；"卡住"是自锁状态（2026-10-25）

探针五能在真机上流畅换图。用户逐档测试，换图间隔取自 glasscast 日志（同一会话相邻编号首次请求的时间差）：

表中的"N 帧"和"设 N"指现在叫"档位"的设置，不是真实帧数。

| 图片 | 设置（步数） | 间隔中位数 | 用户观察 |
|---|---|---|---|
| 320×145 灰度 q30，约 4.1 KB | 3 帧（51） | 720 ms | 稳定 |
| 同上 | 3.3 帧（45） | 634 ms | 稳定 |
| 同上 | 3.5 帧（42） | 620 ms | 偶尔卡一下 |
| 同上 | 3.8 帧（39） | 498 ms | 时好时坏 |
| 同上 | 4 帧（36） | 约 460～500 ms | 不行 |
| 240×109 灰度 q25，约 2.5 KB | 4 帧（36） | 正常时 630～720 ms | 应该稳了 |
| 同上 | 4.5 帧（33） | 正常时 540～720 ms，后段 405 ms | 不行 |

同样的步数会落在两种速度上：眼镜在处理图片时计数循环被拖慢，间隔 600～750 ms；图片一旦没显示出来，循环全速（约 12.5 ms/步），下一张来得更快、更显示不出来，日志里几十秒都停在这种状态（先是 4.5 帧稳定在 405 ms；之后 4 帧在一次 2.7 s 停顿后稳定在 450 ms）。"一会可以一会卡住"就是在这两个状态之间掉下去。正常显示时的实际速度在两种图片大小下都是每秒约 1.5 张；小图的作用是让更高的档位不那么容易掉进卡住状态。固定步数的根本缺陷是它不知道图片显示完没有；`<gif>` 有 `on-completed`（`repeat-count ≥ 1`），可以改成"显示完一张再换下一张"，待测。

探针五的图片框原来是 640×170（约 3.8:1），画布 2.2:1，`stretch` 把画面横向拉到 1.7 倍；先改成 374×170 居中（比例对了但太小）；再改成 `width="100%" height="100%"`、计数文字 `floating`，真机上图片没有铺满，反而缩在左上角、和文字重叠；现在写死 `width="640" height="290"`（整屏），左上角只浮一个换图编号，落在画面左侧黑边里（仓库文件同步，待真机确认）。

电脑端压缩（Mac 截屏，同一张屏幕内容，单位 KB，"/"后为开 `optimize`）：

| 画布 | 灰度 q30 | 灰度 q25 | 灰度 q20 | 灰度 q15 |
|---|---|---|---|---|
| 480×218 | 6.5 / 6.0 | 5.9 / 5.3 | 5.3 / 4.6 | 4.6 / 3.8 |
| 320×145 | 3.4 / 3.1 | 3.1 / 2.8 | 2.8 / 2.4 | 2.5 / 2.1 |
| 240×109 | 2.3 / 2.0 | 2.1 / 1.8 | 1.9 / 1.6 | 1.7 / 1.3 |

`encode_jpeg` 现在默认开 `optimize`（重算霍夫曼表，画质不变，仍是基线 JPEG），体积小 10～15%。减少灰度级数（posterize 16/8/4 级）只再小 1～10%，不采用。同一画面存成 GIF：240×109 8 级灰度约 3.4 KB，320×145 约 5.2 KB。

标尺页（`reference/experiments/cast_emit/ruler_probe.xml`，临时放进平台 `Pages/cast-emit`）真机读数：上排最右能看到 |600（之后还有一点），左列最下能看到 —250（之后还有一点），根 view（100%×100%）的框和固定 640×290 的框几乎重合——页面可用区域就是约 640×290，固定 640×290 的图片框是对的。看到的"偏在中间、上下没用满"是画面内容本身：笔记本屏幕比画布高，`fit` 缩进 2.2:1 画布后左右有黑边（眼镜上黑 = 不发光）；浅色界面在眼镜上是一大块亮绿。改用 `GLASSCAST_FIT_MODE=crop`（裁掉屏幕上下各约 15%，内容横向铺满）。探针五已换回平台，换图编号移到右下角（`left="596" top="268"`）。整屏缩到 320×145 时文字缩小约 4.6 倍，要看清字只能只投屏幕的一部分（`GLASSCAST_REGION`）或做反色（亮字黑底更适合绿色光机），待定。

用户随后说图片仍"偏左偏上、没铺满眼镜"，跟比例无关。平台上的标尺页后来被手动改成加了 `theme="no-status-bar|disable-chat-bar"`。标尺二（`ruler2_probe.xml`）：同样的 theme，根 view 写死 1000×600，刻度画到 x=1000、y=600，`+` 在 (320,145)，看页面能不能超出 640×290。真机读数和第一版一样（上排到 600 多、左列到 250 多），`+` 在刻度约 (300 多, 150) 处——页面画布就是 640×290，内容超出也被裁掉，去掉状态栏/精灵球也不变。偏左偏上是眼镜系统摆放这块 640×290 区域的位置，页面里改不了；官方介绍说 S1「显示位置远近高低可调节」，要去千问 App 的眼镜设置里调。之后平台 `Pages/cast-emit` 换回探针五（640×290 图片框）。

### 9.21 更强的 JPEG 编码：mozjpeg（2026-10-25）

目标："画质更好、体积更小，电脑多花点算力没关系"。用两张 Mac 截屏（crop 到 320×145、480×218 灰度）比较 Pillow（libjpeg-turbo + optimize）和 mozjpeg 4.1.5（`brew install mozjpeg`，keg-only，`/opt/homebrew/opt/mozjpeg/bin/cjpeg`，`-baseline`，trellis 量化默认开）。指标：SSIM 和 ssimulacra2（感知指标，`brew install jpeg-xl` 带）。以 Pillow q25 为基准，同等分数下的体积：

| 编码 | SSIM 同分体积 | ssimulacra2 同分体积 |
|---|---|---|
| mozjpeg 默认量化表（3） | −6～−11% | −15～−21% |
| mozjpeg `-tune-ssim` | −9～−11% | +4～+6%（感知更差，不用） |
| mozjpeg `-quant-table 2` | −5～−14% | −16～−25% |
| mozjpeg `-quant-table 5` | −4～−9% | −20～−24% |

输出都是基线（SOF0）单通道灰度 JPEG。320×145 实测：Pillow q25 2.17～2.50 KB、ssimulacra2 61～63；mozjpeg 表 2 q35 1.93～2.28 KB（小 9～11%）、64～65（更好）。mozjpeg 的质量数值和 Pillow 不对应（表 2 在 q30→q35 之间分数跳一档），换编码器后按体积重选质量。编码一张 480×218 约 5 ms（子进程，stdin/stdout）。Homebrew 的 jpeg-xl 0.12.0 不带 cjpegli，jpegli 没测。

`encode_jpeg(encoder="auto")` 现在装了 mozjpeg 就用它（量化表 2），没装或运行失败退回 Pillow；配置 `GLASSCAST_JPEG_ENCODER=auto|mozjpeg|pillow`、`--jpeg-encoder`，`GLASSCAST_MOZJPEG` 可指定 cjpeg 路径；启动信息"输出"一行写出实际用的编码器。glasscast 已按 320×145 灰度、mozjpeg q35、crop 重启。

### 9.22 定体积编码：分辨率换质量（2026-10-25）

目标："分辨率高一点、压得更狠、尽量保持画质"。比较方法：各画布用 mozjpeg（表 2）扫质量，解码后按眼镜的做法双线性拉到 640×290，与 640×290 灰度原图算 ssimulacra2；固定每张字节数看谁分高（三张 Mac 截屏）：

| 每张 | 320×145 | 400×181 | 480×218 | 560×254 |
|---|---|---|---|---|
| 2.5 KB | 29.7～34.9 | 28.2～35.4 | 20.3～33.3 | 20.8～38.0 |
| 3.0 KB | 34.4～41.5 | 35.8～42.7 | 32.0～41.3 | 32.6～43.3 |
| 4.0 KB | 43.5～47.0 | 43.0～49.1 | 40.7～49.2 | 43.8～48.8 |

同样字节数下各分辨率差不多，400×181 在 2.5～3.5 KB 略好，更高分辨率要到 4 KB 左右才占优；图越复杂，高分辨率被迫用的质量越低（480×218 定 2.5 KB 时质量 14～22）。做一张模拟绿色单色显示的对比图（未随仓库提供）就看得很清楚：整屏缩下来字都太小，浅色界面在眼镜上是一大块亮绿。

新增定体积编码：`encode_jpeg_target`（在质量范围里二分，取体积 ≤ 目标的最高质量，约 7 次编码，mozjpeg 480×218 一张约 35 ms）。配置 `GLASSCAST_JPEG_TARGET_KB` / `--jpeg-target-kb`（0 = 固定质量，> 0 时 `jpeg_quality` 是上限），status.json 有 `jpeg_target_bytes`、`last_jpeg_quality`。glasscast 已按 480×218 灰度、mozjpeg、每张 ≤ 2.6 KB（质量上限 90）、crop 重启（2.5 KB 左右在 4 帧下稳定，§9.20）。

### 9.23 更费算力的压缩：逐帧搜索、jpegli、guetzli（2026-10-25）

都在 480×218 灰度、每张 ≤ 2.6 KB、ssimulacra2（对同分辨率原图）下比，三张 Mac 截屏：

- **逐帧搜索 mozjpeg 参数**（量化表 0～8、`-tune-psnr/ssim/ms-ssim`、`-notrellis` 共 13 种，每种二分到 2.6 KB，选分最高的）：比固定表 2 高 1.7～5.3 分（48.1→49.8、52.5→55.6、52.4→57.7），赢家随画面变（表 6、表 6、表 4）。单线程约 1 s/帧（编码约 90 次 × 5 ms + ssimulacra2 13 次 × 40 ms），多核并行可到约 0.15 s。反色+拉对比后的图赢家多是表 2 `-notrellis`。
- **jpegli**（Google，已从 libjxl 拆到 github.com/google/jpegli；Homebrew 的 jpeg-xl 不带，源码编译需用 Command Line Tools 的 clang 21，Xcode 15 的 clang 配新 SDK 编不过）：要 q1～q7 才压进 2.6 KB，分数 47.9～52.5，与 mozjpeg 表 2 持平、不如表 4。它的优势在高画质区间，我们这个码率用不上。
- **guetzli**：只支持质量 ≥ 84，体积是预算的几倍，不适用。

结论：再往上挤画质，可行的是逐帧搜索 mozjpeg 参数（+2～5 分）；它和"反色+拉对比"（§9.22 之后的对比图：同 2.6 KB 质量 22→29、黑底亮字更适合绿色光机）互不冲突，可以叠加。

### 9.24 GIF 自定节奏测试（探针六，2026-10-27，待真机）

想法：用 `<gif repeat-count="1" on-completed="data://update?g={{d:g + 1}}">`，眼镜显示完一张才换下一张，不再按固定步数（固定步数一旦掉进"计数全速、图片显示不出来"的状态就出不来，§9.20）。官方文档（Waft-DSL 完整参考里的 `<gif>` 组件一节）：`<gif>` 只有 src（支持 http(s)）、repeat-count（1 = 播一遍）、auto-play、placeholder（第三方实际不可用）、image-fit、on-completed / on-completed-if；on-completed 在"播放达到 repeat-count 次数后"触发、回调时按当前上下文解析模板；**没说 src 绑定变化会不会重新下载**，也没有 on-error；帧时长取自 GIF 文件。§9.8 记过模拟器里 gif 的 src 只在创建时取值一次、换地址不重载——真机没测过，这次一并验证。

电脑端：`encode_gif`（frames.py：转灰度、均匀分 2/4/8/16 级、不抖动、非隔行、帧时长写进 GCE，10 ms 为单位）；`Snapshot.gif`，`gif_levels > 0` 时每帧和 JPEG 一起编码（480×218 约 2 ms），复用同一个像素摘要缓存；新路由 `/s/<key>/screen.jpg/<stamp>.gif`（排在通用 `{stamp}` 前面；穿戴 App 按后缀认图片类型），没开 GIF 时 404。配置 `GLASSCAST_GIF_LEVELS`（默认 0 = 不出）、`GLASSCAST_GIF_DELAY_MS`（默认 100），`--gif-levels` / `--gif-delay-ms`；status.json 有 `last_gif_bytes`、`gif_levels`、`gif_delay_ms`。实测 480×218 4 级灰度 2.8～3.4 KB（同屏 JPEG 定 2.6 KB）；gifsicle -O3 只再小约 3%，不用。

探针六（`reference/experiments/cast_emit/gif_probe.xml`）已临时放进平台 `Pages/cast-emit`：一个 640×290 拉伸的 `<gif>`，src = `{{url}}/{{f:formatDate('now','MMddHHmmss')}}_{{d:g}}.gif`（会话号直接写进 src，不用 a:if，避免隐藏节点先拉 `0_0`），右下角显示 g。判读：g 停在 0 = on-completed 没触发（或 GIF 没显示）；停在 1 = 触发了但 src 变化不重新下载；一直涨 = 自定节奏成立，换图间隔看日志里 `.gif` 请求。glasscast 已按 480×218、mozjpeg 定 2.6 KB、GIF 4 级、帧时长 100 ms 重启。

**本地模拟器结果（2026-10-27，waft-lite v0.2.11，探针六 + 第二个 glasscast 实例：合成画面、测试 key、:8767）**：页面渲染成功；GIF 下载一次（`…/<会话号>_0.gif` 3.2 KB），4 级灰度画面铺满 640×290 正常显示；`on-completed` 触发一次（日志 `route data: data://update?g=1`，右下角显示 1）；之后 src 变成 `…_1.gif`，**没有再下载**，画面停在第一张。与 §9.8 一致：`<gif>` 的 src 只在创建时取值。平台"预览 & 调试"测不了：它的远程图一律 `remote image failed: err=-1 download failed`，请求根本没到 glasscast。真机内核是否不同未知——同一个 waft-lite 内核，大概率也停在 1。

### 9.25 更流畅的办法：调研结论与真机探针七（2026-10-28，待真机）

系统调研的结论：

- 瓶颈是每个文件约 0.65 s 的固定开销（手机下载 + 蓝牙 + 解码，2.5～4 KB 内与体积基本无关），一张图一个文件最多约 1.5 张/秒。只有"一次下载装很多帧、在眼镜本地播放"能突破。
- 平台没有视频/推流显示组件（音视频能力都是眼镜往外传）。不靠数据写入就能自己变化的只有 carousel（只有淡入淡出、≥100 ms）、frame-set / gif（src 只在创建时取值）、animate-* 补间、markdown 自动滚动。纯技能页面运行中不能新建节点（data:// 只能写标量）。
- 设计时在本地模拟器里发现：**内核下载是一个串行队列，按文档顺序一个接一个**（上一个响应到了才发下一个请求），图片、gif、frame-set 共用。由此排第一的方案是**预先声明一串片段节点（.fset / .gif），服务端把每个请求压住到那一段录完再回**——相当于用 HTTP 响应做推送；每段 1～2 s、10～20 帧在眼镜本地播。真机未知：手机能不能把远程 .fset / .gif 送到眼镜、真机下载是不是也串行、每个文件耗时随体积怎么涨（蓝牙吞吐）。
- 备选：一张 600×272 JPEG 拼 2×2 四帧，用 left/top 在 640×290 裁剪框里依次显示（只走已验证的 .jpg 通路，约 2.5～3.7 张/秒，节奏不均）。并行可做：终端/代码文字重排成 1 位 PNG（只提升清晰度）。需要白名单和用户自己发布的 Agent 推送方案暂不考虑。

**探针七**（`reference/experiments/cast_emit/probe7_delivery.xml`，已临时放进平台 `Pages/cast-emit`，保存成功）：3×3 格子，按文档顺序请求 `…/screen.jpg/probe/<时间>_<名字>`：f16/f40/f80/f160（20 帧 200×91 灰度 PNG 的 .fset，约 16/40/80/160 KB）、fj20（.fset 字节放在 .jpg 地址后）、g8（20 帧 GIF）、i4/i12（单张 JPEG）。每帧烧进名字和帧号、循环播放。新路由 `GET /s/<key>/screen.jpg/probe/<会话>_<名字>.<后缀>`（glasscast/probe.py，启动时预生成）。模拟器验证：8 个格子全部出现并在播放，请求按文档顺序各一次（图片按惯例各 3 次）。真机要看：哪些格子出现、哪些在动；日志里请求是一个接一个还是同时到；相邻请求间隔随体积的变化（拟合 每文件耗时 = 固定开销 + 体积/吞吐）。
