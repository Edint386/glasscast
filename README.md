# glasscast

把电脑屏幕投到千问 AI 眼镜 S1，用语音开始和停止。适合瞄一眼终端、构建进度或文档，不适合看视频、玩游戏。

> 非官方个人项目，与阿里巴巴、千问团队和千问 AI 硬件开放平台没有任何关联。

## 它怎么工作

眼镜没有给第三方开放视频流，所以 glasscast 用图片代替：

1. 电脑上的 `glasscast serve` 不停截屏，把每一帧压成约 2.6 KB 的灰度 JPEG，经你自己的公网 HTTPS 域名提供。
2. 眼镜上的页面不停换图片地址，手机 App 下载最新一张，再经蓝牙传给眼镜显示。
3. 你对眼镜说「开始投屏」，千问平台的大模型调用 glasscast 的 MCP 工具，再打开页面。MCP 只管开始、停止和查询，不参与逐帧刷新。

## 实际效果

下面是在 S1 真机上测到的结果：

| | 普通模式（默认） | 流畅模式 |
|---|---|---|
| 速度 | 约每秒 1 张 | 最高约每秒 1.5 张 |
| 眼镜页面 | `cast-image` | `cast-emit` |
| 特点 | 最稳 | 帧率设到 4.5 以上有时会卡住 |

- 延迟约 1～2 秒。
- 单色绿屏，没有声音，也不能在眼镜上操作电脑。
- 画面固定在视野左上角。整屏缩小后小字看不清，看代码时建议只投一块区域（`GLASSCAST_REGION`）。
- 只在 macOS 上测过。

## 流畅模式的实现思路

普通模式靠 `<tick>` 换图，但页面里的 tick 共用一个 1 秒定时器，最快每秒 1 张。想更快，要解决两个问题：

**1. 页面没有更短的定时器。** 所以让页面自己当节拍器：给自己发 `emit://` 事件，收到后把计数减一再发出去。emit 是异步的，真机上每一步约 13 毫秒，步数决定换图间隔。

**2. 页面一刷新，没加载完的图片就要从头加载。** 一张图从下载到显示约 0.65 秒，换图前多刷新一次都可能显示不出来。所以计数放在事件参数里（`emit://event/dN?n=N`），不写 `data`，倒数期间页面完全不刷新。

一轮的过程：

1. 页面从 `m` 开始倒数，`m = hops × 3`。`hops` 由 `start_cast` 按帧率算出，模型打开页面时传进来，改帧率不用改页面。
2. 倒到 0 时写一次换图编号，这是一轮里唯一的刷新。图片地址变成 `…/<会话>_<编号>.jpg`，手机下载最新一张。
3. 重新从 `m` 开始倒数。

页面的事件规则数量有限，这里用了一个小技巧：事件匹配是前缀匹配，`d1`～`d9` 九条规则按 `n` 的首位数字分流，就能倒任意步数；`d0?` 只在 `n` 等于 0 时命中。完整页面见 [`pages/cast-emit/template.xml`](pages/cast-emit/template.xml)。

传的始终是一张张普通 JPEG，不是视频流，也不是 GIF。GIF 只是默认关闭的实验开关，现有页面都没用它。

上限来自图片本身：每张图有约 0.65 秒的固定开销。帧率设到 4 时，真机每张图间隔约 630～720 毫秒，约每秒 1.5 张，这是实测的稳定上限。设到 4.5 以上，有时会换得比加载还快，画面就冻住了。

## 需要准备

- 千问 AI 眼镜 S1 和手机 App「千问AI穿戴」
- [千问 AI 硬件开放平台](https://device.qianwen.com)账号
- 一台装好 [uv](https://docs.astral.sh/uv/) 的 Mac，投屏时不能锁屏或睡眠
- 一个指向这台电脑的公网 HTTPS 域名，必须是真证书

## 快速开始

先在本机试一下，不需要眼镜：

```bash
git clone https://github.com/Edint386/glasscast.git
cd glasscast
uv sync
GLASSCAST_TOKEN=local-test-token-123456 GLASSCAST_CAST_KEY=localtest123 \
  uv run glasscast serve --source synthetic --public-url http://127.0.0.1:8766 --autostart image
```

用浏览器打开 <http://127.0.0.1:8766/s/localtest123/screen.jpg>，每刷新一次就是新的一帧合成画面。

接到眼镜上：

1. 运行 `cp .env.example .env`，填好域名和 token（用 `openssl rand -hex 24` 生成）。
2. 给启动 glasscast 的终端打开「屏幕录制」权限。
3. 把服务暴露到公网 HTTPS，见 [公网部署](docs/deploy.md)。
4. 启动服务：`. ./.env && uv run glasscast serve`。
5. 在平台上建 MCP 插件、技能和两个页面，见 [平台配置](docs/platform-setup.md)。
6. 对眼镜说「开始投屏」。

## 语音命令

| 你说 | 效果 |
|---|---|
| 开始投屏 | 用普通模式投屏 |
| 用流畅模式投屏 | 用流畅模式投屏 |
| 投屏调到 N 帧 | 调整帧率设置，N 取 1～10，建议不超过 4 |
| 投屏状态 | 查询是否在投、画面是否在更新 |
| 停止投屏 | 电脑停止截屏，再用眼镜的返回手势离开页面 |

## 安全

- 图片地址里的 KEY 等于看屏口令，拿到完整地址的人就能看到你的屏幕。
- 投屏会投出屏幕上的一切，包括通知和密码框。
- Token 只填在平台的插件配置和本机的 `.env` 里，不要写进技能或页面。

详见 [使用手册的安全一节](docs/usage.md#8-安全)。

## 文档

- [使用手册](docs/usage.md)：完整部署步骤、命令行和环境变量、HTTP 和 MCP 接口、排错、已知问题
- [公网部署](docs/deploy.md)：反向 SSH 隧道、nginx、1Panel、cloudflared
- [平台配置](docs/platform-setup.md)：在千问平台上建插件、技能和页面
- [眼镜页面](pages/README.md)：每个页面是什么、改页面前要知道的限制
- [本地模拟器](tools/simulator/README.md)：不用眼镜预览页面
- [实验记录](docs/research.md)：带日期的调研和真机测试笔记

## 许可

代码以 [MIT 许可证](LICENSE) 发布。「千问」「Qwen」等名称和商标归其各自所有者。模拟器用到的官方渲染内核不包含在本仓库里，由脚本从官方 CDN 下载。使用时请遵守平台的开发者协议，投屏内容和公网暴露的风险由使用者自行承担。

**English:** glasscast mirrors a Mac screen to Qwen AI glasses S1 by serving small grayscale JPEGs over your own HTTPS domain, at about 1 to 1.5 images per second. Setup docs are in Simplified Chinese. This is an unofficial personal project.
