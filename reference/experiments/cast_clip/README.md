# cast_clip（已废弃的实验页面，不要上传到平台）

这是第一轮设计里 clip 模式（方案一：`.fset` 帧合集分段流）的眼镜页面，原来放在 `pages/cast_clip/`。
第二轮把它移到这里，**只留作本地模拟器对照实验**，`skill/ui_schema.json` 和 `skill/SKILL.md` 都不再引用它，
`docs/platform-setup.md` 也不再让你把它粘贴到平台上。

## 原设计

```xml
<data i="0"/>
<frame-set src="{{clip_url}}?i={{d:i}}" width="640" height="290"
           total-frames="10" frame-duration="200" repeat-count="1" color-format="0" load-async="true"
           on-completed="data://update?i={{d:i + 1}}"/>
```

设想：每段 10 帧播完 → `on-completed` 执行 `data://update` 让 `d:i` 加 1 → `src` 的地址随之变化 →
内核重新下载服务端最新的一段 → 如此循环，得到段内 5 fps 的"视频"。

## 为什么不可用

本地模拟器（`tools/simulator`，内核 waft-lite v0.2.11）实测，见 `docs/research.md` §2.4、§9.2：

| 实验 | 结果 |
|---|---|
| E2：`src` 里用 `d:i`、`tick`、`dataSource://update`、`a:if` 任意方式改变 | **不重新下载**。`<frame-set>` 的 `src` 只在节点创建时求值一次；`on-completed` → `data://update?i=1` 确实触发、`d:i` 确实变成 1（同页 `<label>` 能看到），但帧合集不会去拉新地址。`a:if` 只是翻转显示（日志 `aif_flip`），不重建节点 |
| E3：`on-completed="{{ctx:uri}}"` 让页面重开自己 | 无效，没有第二次下载 |
| E7：内核缓存 | 远程下载按完整 URL 缓存、内核生命周期内跨页面有效，上次会话的 `clip.fset?i=0` 会直接命中缓存 |
| E5：`.fset` 帧用 JPEG | 黑屏、无告警、不触发 `on-completed`，只能用 PNG 帧 |

所以这个页面只能播第一段（约 2 秒）就停在最后一帧。官方属性表（官方文档第 36 节 §3.3）里 `src` 的类型也只写
"路径字符串"，明确支持绑定的只有 `current-frame` 和 `action`。

## 替代方案

帧合集在本项目里的正确用法是当**定时器**，不是当播放器：`pages/cast-fast/` 用一个 2 帧 8×8 的固定 `.fset`
（glasscast 的 `/s/<key>/timer.fset`）配合 `action="{{d:act}}"` 循环重播，每轮通过 `data://update` 让
`<image>` 的地址变化、重新下载一张截图（research.md §9.2 E4、E9，§9.3）。

## 还能拿它做什么

- 在本地模拟器里复现 E2：`uv run python tools/simulator/serve.py`，选预设 `cast_clip … + 本地演示帧合集`
  （`pages/` 下已经没有 `cast_clip`，harness 会改用它内置的同款副本；要测本目录这份，把 `template.xml`
  内容贴进左侧 XML 框再点"渲染"）。
- glasscast 的 `/s/<key>/clip.fset` 路由保留为实验接口（实现指南 §9.1），可以配合本页面看段内播放效果。

如果将来找到了能换段的机制，要先在 `tools/simulator` 里验证，再把页面移回 `pages/`、在 `ui_schema.json`
里加回对应的 Hook（`templateName` 要和目录名一致）。
