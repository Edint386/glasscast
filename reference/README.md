# reference/：调研时的实验文件

这里是做 glasscast 时留下的实验服务器、探针页面和被淘汰的设计，配合 [docs/research.md](../docs/research.md) 看。
**使用 glasscast 不需要这个目录**：要上传到平台的页面在 `pages/`，技能在 `skill/`，步骤见
[README.md](../README.md) 和 [docs/platform-setup.md](../docs/platform-setup.md)。

这些文件都是当时的原样（注释里的结论是写文件那一刻的判断，可能已经被 research.md 后面的小节更正），不要直接拿来替换
`pages/` 下的页面。注释里"临时放进平台 `Pages/cast-emit`"是指当时在实验技能里借用同一个页面名做真机测试。

## 一览

| 文件 | 是什么 | research.md |
|---|---|---|
| `labserver.py` | 调研第一天的极简实验服务器（标准库 + Pillow）：`/screen.jpg`、`/screen.png` 每次请求画一张带时间的图，`/clip.fset`、`/clipjpg.fset` 现做 10 帧 `.fset`，`/log` 看最近谁来拉了什么 | §2.2、§2.4、§4、§9.2 E1/E2/E5 |
| `ui-schema-hooks/` | `ui_schema.json` 的 OUTPUT Hook 版本（按 `mode` 自动打开页面）。真机上 Hook 从不触发，所以 `skill/ui_schema.json` 现在是 `[]`，页面改由模型调用「UI页面渲染」打开。详见目录内 README | §9.5 |
| `experiments/cast_clip/` | 第一轮的 clip 模式页面（`.fset` 分段流）。`<frame-set>` 的 `src` 只在创建时求值，换不了段，已废弃。详见目录内 README | §2.4、§9.2 E2/E3/E5/E7 |
| `experiments/cast_emit/cast-emit.4hops.xml` | 第一个 emit:// 事件总线自环页面（固定 4 跳换一次图），也是平台保存校验（URI-001）的探针：保存成功 | §9.8 |
| `experiments/cast_emit/cast-emit.draft.xml` | emit 自环的早期草稿：`hops` 由 dataSource 写入，最多 8 跳、每跳 3 条 event；把时间戳存进 `data`（真机上会被钳在 int32 上限） | §9.8、§9.11 |
| `experiments/cast_emit/cast-emit.loop.xml` | 退役的流畅模式页面：每跳 2 条 event、最多 25 跳；仓库里这份是"每 3 圈才换一张图"的定因实验版。真机上每圈刷新都会打断图片加载，不出画面 | §9.8 审查后修订、§9.10～§9.13 |
| `experiments/cast_emit/gen_cast_emit.py` | 生成上面的 `cast-emit.loop.xml`。`uv run python reference/experiments/cast_emit/gen_cast_emit.py` 覆盖写入，加 `--check` 只比较 | §9.8、§9.13 |
| `experiments/cast_emit/text_probe.xml` | 探针一：32 步一圈的 emit 自环，只显示圈数、不放图片，看事件循环触发的刷新能不能在眼镜上显示（能） | §9.16、§9.17 |
| `experiments/cast_emit/static_image_probe.xml` | 探针二：圈数 + 一张地址不变的图（都能显示） | §9.17 |
| `experiments/cast_emit/per_lap_image_probe.xml` | 探针三：每圈（约 0.4 s）换一张新图（一张都不显示） | §9.17、§9.18 |
| `experiments/cast_emit/every_n_laps_probe.xml` | 探针四：每 2 圈换一张新图（换图之间的刷新打断加载，只显示一张） | §9.18、§9.19 |
| `experiments/cast_emit/countdown_probe.xml` | 探针五：计数放进事件参数、换图之间零刷新，每张图隔 `hops × 3` 步。真机上能流畅换图，是现在 `pages/cast-emit` 的直接前身 | §9.19、§9.20 |
| `experiments/cast_emit/ruler_probe.xml`、`ruler2_probe.xml` | 屏幕标尺：量出页面画布就是约 640×290、由系统摆在视野左上方，页面里改不了 | §9.20 |
| `experiments/cast_emit/gif_probe.xml` | 探针六：`<gif repeat-count="1" on-completed>` 自定节奏。模拟器里 `src` 变了不重新下载，只换一次 | §9.24 |
| `experiments/cast_emit/probe7_delivery.xml` | 探针七：3×3 格子同时请求不同体积的 `.fset` / `.gif` / `.jpg`，看真机能送哪些格式、下载是否串行、耗时怎么随体积变 | §9.25 |

## 怎么跑

- 页面类文件可以在本地模拟器里直接贴进去渲染：`uv run python tools/simulator/serve.py`，把 XML 贴进左侧 XML 框、
  data JSON 写 `{"url": "<glasscast 的 …/screen.jpg 地址>"}`，再点"渲染"（见 `tools/simulator/README.md`）。
  模拟器复现的是平台编辑器的预览内核，不是手机下载 + 蓝牙那条真机链路，真机上的结果可能完全不同（research.md §9.10～§9.13）。
- `gif_probe.xml` 需要 glasscast 出 GIF：`--gif-levels 4`（或 `GLASSCAST_GIF_LEVELS=4`）。`url` 照常填 `…/screen.jpg` 地址，
  页面自己在后面拼 `/<会话>_<序号>.gif`；没开 GIF 时这个地址是 404。
- `probe7_delivery.xml` 用 glasscast 内置的探针路由 `/s/<key>/screen.jpg/probe/<会话>_<名字>.<后缀>`（`glasscast/probe.py`），
  `url` 照常填 `…/screen.jpg` 地址即可。
- `labserver.py`：`uv run python reference/labserver.py 8765`，只监听 127.0.0.1；字体用 macOS 自带的 Arial，找不到时退回 Pillow 默认字体。
