# 眼镜页面

这里是投屏用的眼镜页面（千问 AI 硬件开放平台的 UI 页面，Waft-DSL 写的 `template.xml`）。页面由大模型在 `start_cast` 成功后调用平台内置的「UI页面渲染」工具打开（见 `skill/SKILL.md`），之后自己不停换图片地址、下载新的截图；MCP 不参与刷新。

| 目录 | 是什么 | 放到平台上？ |
|---|---|---|
| `cast-image/` | **普通模式**（默认）。`<tick>` 每秒改一次图片地址，真机约每秒 1 张、延迟 1～2 秒，最稳。 | 是，`Pages/cast-image` |
| `cast-emit/` | **流畅模式**。页面给自己发 `emit://` 事件倒数当节拍器，每换一张图倒数 `hops × 3` 步（真机每步约 13 毫秒），倒数过程中不刷新绑定。帧率设置 4（`hops` 12）时真机约每秒 1.5 张，这是实测稳定上限。 | 是，`Pages/cast-emit` |
| `cast-fast/` | 旧的流畅模式：把 2 帧 8×8 的 `.fset` 帧合集当定时器。平台保存校验（`URI-001`）不放行 `<frame-set action="{{…}}">`，**只能在本地模拟器里跑**。 | 否，只用于 `tools/simulator` |

在平台上怎么建这两个页面，见 [docs/platform-setup.md](../docs/platform-setup.md) 第 6 步。页面名就是目录名，必须原样使用（SKILL.md 按名字打开）。

## 每个目录里的文件

- `template.xml`：页面本身，整段粘贴到平台的 `Pages/<名字>/template.xml`。顶部注释说明了原理和写法约束，可以一起粘贴。
- `data.json`：编辑器预览用的 Mock 数据（平台上的 `Pages/<名字>/data.json`），只影响预览，真正投屏时的数据来自 `start_cast` 的返回。里面的 `cast.example.com` 和 `REPLACE_KEY` 是占位，预览时换成你的域名和 glasscast 启动时打印的 KEY。不要在这里写 token。
- 平台保存模板后会自动生成 `meta.json`，不要手工创建，本仓库里也没有。

## 页面要的数据（dataSource）

| 页面 | 字段 | 来源 |
|---|---|---|
| `cast-image` | `url` | `start_cast` 返回的 `url`，即 `https://cast.example.com/s/<KEY>/screen.jpg` |
| `cast-emit` | `url`、`hops` | `start_cast(mode="fast", fps=N)` 返回的 `url` 和 `hops = round(1000 / N / 20)`（1～25）。没有 `hops` 时页面按最慢档（25）跑 |
| `cast-fast` | `url`、`timer_url`、`frame_duration_ms` | `start_cast(mode="fast")` 的返回（仅模拟器） |

页面里的绑定都写成 `{{url}}{{structuredContent.url}}` 这样的两段拼接：不管数据是平铺的还是包在 MCP 的 `structuredContent` 里，都只有一段有值。页面在 `url` 后面拼上 `/<会话号>_<编号>.jpg` 作为每张图的地址，glasscast 把 `/s/<KEY>/screen.jpg/<任意>.jpg` 当作 `screen.jpg` 处理。

## 改页面前要知道的限制（真机 / 平台实测，2026-10）

- 图片地址必须以 `.jpg` 结尾。`screen.jpg?ts=…` 这类带查询参数的地址在真机上能下载成功，但永远不显示。
- 换图之间不要刷新绑定：每次绑定刷新都会让还没加载完的 `<image>` 从头加载，换得太勤一张都显示不出来。
- `data` 里只放小整数：超过 2147483647 的数在真机上会被卡在这个值（模拟器不会），所以时间戳不能存进 `data`。
- 根 `view` 下最多 64 个子元素，超过后整页解析失败。
- 所有 URI 属性都要以平台允许的 scheme 开头（`data://`、`emit://`、`event://` 等），否则保存时被 `URI-001` 拒绝。
- `<gif>`、`<frame-set>` 的 `src` 只在节点创建时取值一次，之后改它不会重新下载。
- S1 的页面画布是 640×290，系统把它放在视野左上角，页面里没法居中；图片框写 640×290 即可。
- 远程图片解码后最多 742 400 字节、文件最多 1 MiB；真机上还要更小，推荐用 `.env.example` 的设置（480×218 灰度，每张约 2.6 KB）。

`tests/test_pages.py` 会检查这些页面是良构 XML、图片地址写法正确、`cast-emit` 的 data.json 只有 `url` 和 `hops` 等约定。

## 本地预览

用 `tools/simulator`（官方编辑器预览同款的 WASM 内核，在本机渲染）：启动后打开 `http://127.0.0.1:8770/?preset=page:<名字>`，把 data 改成本机 glasscast 的地址再渲染。步骤见 [docs/platform-setup.md](../docs/platform-setup.md) 第 9.2 步和 [tools/simulator/README.md](../tools/simulator/README.md)。模拟器不模拟手机下载和蓝牙传输，速度比真机快，只用来确认页面逻辑。

做过的实验页面（探针、旧版本）在 `reference/experiments/`，只作参考，不要放到平台上。
