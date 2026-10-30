# glasscast 公网暴露与部署手册

千问平台（云端调用 MCP）和手机（下载图片）都只能访问**公网 HTTPS** 地址，电脑上的 `127.0.0.1:8766` 对它们不可见。本文说明怎样用你自己的域名和服务器，把电脑上的 `glasscast serve` 暴露出去。

| 方案 | 方式 | 适合 |
|---|---|---|
| **A** | 反向 SSH 隧道 + VPS 上的 nginx / OpenResty + Let's Encrypt，独立域名 `cast.example.com` | **主方案**，长期使用 |
| **B** | 同样的隧道，挂在已有 HTTPS 站点的路径下：`https://app.example.com/cast` | 已经有站点，不想加 DNS 记录和证书 |
| **C** | `cloudflared` 临时隧道（`*.trycloudflare.com`） | 只用于测试，地址每次都变 |
| **D** | 局域网直连：在 A 的基础上，手机连家里 Wi-Fi 时直接从电脑取图 | 可选优化，不能单独使用 |

不管用哪种方案，对外地址都要满足：

- **真证书**：由正规 CA 签发（例如 Let's Encrypt），自签证书不行。
- **HTTP/2 可以，不要 HTTP/3**：响应头里不能有 `alt-svc: h3`，否则平台的客户端会尝试升级到 HTTP/3 然后失败。
- **公网上的 `/admin/` 返回 404**（方案 A、B；方案 C、D 没有这条规则，只靠 Token，见各自的说明）：`glasscast set-fps`、`glasscast config` 用的管理接口只给电脑本机用。
- **快**：平台调用工具的总超时约 3 秒，见 §7。

文档里的占位符都要换成你自己的值。部署时 `server-setup.sh`、`tunnel.sh` 和 `sync_lan_cert.sh` 遇到占位符会拒绝运行：

| 文档里写的 | 换成 |
|---|---|
| `cast.example.com` | 给 glasscast 用的域名 |
| `app.example.com` | 方案 B：已有站点的域名 |
| `server.example.com`、`ubuntu` | VPS 的 SSH 地址和用户 |
| `203.0.113.10` | VPS 的公网 IP |
| `you@example.com` | 你的邮箱（Let's Encrypt 用它发到期提醒） |
| `/Users/you/glasscast` | 你克隆本仓库的目录 |
| `192.168.1.50`、`192.168.1.1` | 方案 D：电脑和路由器的局域网地址 |
| `1Panel-openresty-XXXX` | 1Panel 的 OpenResty 容器名 |

---

## 1. 需要准备什么

| 东西 | 方案 | 说明 |
|---|---|---|
| 一个域名，能改 DNS 记录 | A | 用子域名就行。本仓库的脚本不改 DNS |
| 一台有公网 IP 的 VPS | A、B | Debian / Ubuntu，有 sudo 权限。配置要求很低，投屏流量约每小时 10～20 MB |
| 电脑能免密 SSH 到 VPS | A、B | `tunnel.sh` 不会弹密码，也不会替你确认主机指纹 |
| 已有 HTTPS 站点的配置权限 | B | 要编辑它的 nginx 配置 |
| `cloudflared` | C | 不需要 VPS 和域名 |
| 能改 DNS 的路由器 | D | 见 §5 |
| 平台上的 MCP 插件和 Skill | 全部 | 见 [platform-setup.md](platform-setup.md) |

**服务器放在哪**：平台调用工具只等约 3 秒，服务器离国内越近越好。国内云服务器的线路最好，但域名通常要先备案，否则 80 / 443 会被拦截。海外 VPS 不用备案，跨境链路却慢且抖动，会偶发超时。选海外的话，优先选对国内线路好的机器，见 §7。

---

## 2. 方案 A：反向 SSH 隧道（主方案）

> **验证状态**：真机验证用的是 §2.8 的「方式二：1Panel 面板建站」，当时的 `root.conf` 比这里的早一版，upstream 设计相同。`deploy/nginx/` 的模板和现在的 `root.conf` 都还没在真实服务器上加载过，`server-setup.sh` 也还没有完整跑过，第一次用请按 §2.3 的顺序，先跑 `--check` 和 `--cert-dry-run`。电脑端只在 macOS 上测过，Linux / Windows 没测过。

### 2.1 链路

```
 手机（下载图片）/ 千问平台（调用 MCP）
      │  HTTPS（HTTP/2，不开 HTTP/3，不下发 Alt-Svc）
      ▼
 VPS 上的 nginx / OpenResty
   https://cast.example.com
     /healthz      ─┐
     /s/<key>/...  ─┼─► 127.0.0.1:18766（sshd 的反向转发端口，只绑回环）
     /mcp          ─┘            ▲
     /admin/...    → 404         │  SSH 连接（电脑主动连出去，电脑不需要公网 IP）
     其余          → 404         │
                                 │
 电脑               deploy/tunnel.sh ──► 127.0.0.1:8766  glasscast serve
```

- 电脑主动发起 SSH（`-R 127.0.0.1:18766:127.0.0.1:8766`），所以电脑在 NAT 后面、换网络都没关系，断线由 `tunnel.sh` 自动重连。
- 反代只转发 `/healthz`、`/s/`、`/mcp`，其余一律 404。`/admin/` 也显式写了 404，请求到不了电脑。
- `/s/` 只放行 GET / HEAD。读超时：`/healthz` 3 s，`/s/` 10 s，`/mcp` 30 s。
- 不记访问日志：图片地址里的 key 相当于看屏口令，见 §9。

### 2.2 准备服务器和 DNS

1. **加 DNS 记录**。在域名的 DNS 服务商控制台（DNSPod、阿里云解析、Cloudflare 等）加一条 A 记录：`cast.example.com` → `203.0.113.10`。

   - 不要加 AAAA 记录：配置只监听 IPv4，手机走 IPv6 时会连不上。
   - 用 Cloudflare 时把这条记录设成「仅 DNS」（灰色云），不要走它的代理。

   加好之后检查，期望输出 `203.0.113.10`：

   ```sh
   dig +short cast.example.com
   ```

2. **放行端口**。在云服务商的安全组 / 防火墙里放行 TCP 80 和 443。80 是给 Let's Encrypt 校验和续期用的，平台和手机只走 443。系统里开了 ufw 时：

   ```sh
   sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
   ```

   隧道端口 18766 只绑在服务器回环地址上，**不需要也不应该**放行。

3. **配好 SSH 免密登录**（在电脑上）。已有密钥就跳过第一条 `ssh-keygen`。最后一条不提示密码就对了；第一次要手工确认主机指纹：

   ```sh
   ssh-keygen -t ed25519
   ssh-copy-id ubuntu@server.example.com
   ssh ubuntu@server.example.com true
   ```

4. **安装 nginx 和 certbot**（Debian / Ubuntu）。用 1Panel 的话跳过这一步，看 §2.8。

   ```sh
   ssh -t ubuntu@server.example.com 'sudo apt update && sudo apt install -y nginx certbot dnsutils'
   ```

   nginx 需要 1.15.3 以上（Ubuntu 22.04 / 24.04 自带的版本都可以）；1.25.1 以上 `nginx -t` 会提示 `listen ... http2` 写法已弃用，只是警告，不影响使用。`dnsutils` 提供 `dig`，没有也能部署，只是少几项 DNS 检查。

### 2.3 上传 deploy/ 并运行 server-setup.sh

以下命令都在**电脑**上执行。

1. **上传整个 `deploy/` 目录**：

   ```sh
   cd /Users/you/glasscast
   ssh ubuntu@server.example.com 'rm -rf ~/glasscast-deploy'
   scp -r deploy ubuntu@server.example.com:glasscast-deploy
   ```

   - 每次上传前先删旧副本。目标目录已存在时，`scp -r` 会嵌套成 `glasscast-deploy/deploy/`，脚本照样读外层的旧文件。
   - 放在家目录，不要放 `/tmp`：`/tmp` 人人可写，而接下来要用 root 执行这些脚本。

2. **先只检查，不改任何东西**：

   ```sh
   ssh -t ubuntu@server.example.com 'sudo sh ~/glasscast-deploy/server-setup.sh --check --domain cast.example.com'
   ```

   它会报告部署文件、nginx、certbot、DNS（包括 AAAA 和权威 DNS）、sshd 的转发设置、18766 端口占用、现有部署状态。DNS 没就绪时会打印操作指引。

3. **（推荐）演练签证书**，不消耗正式额度：

   ```sh
   ssh -t ubuntu@server.example.com 'sudo sh ~/glasscast-deploy/server-setup.sh --domain cast.example.com --email you@example.com --cert-dry-run'
   ```

   这一步会装好 upstream 和 80 端口配置（对其他站点无影响），然后用 Let's Encrypt staging 走一遍校验，不保存证书。

4. **正式部署**（幂等，可以重复运行）：

   ```sh
   ssh -t ubuntu@server.example.com 'sudo sh ~/glasscast-deploy/server-setup.sh --domain cast.example.com --email you@example.com --sshd-keepalive'
   ```

   `--sshd-keepalive` 可选但推荐：它安装 `/etc/ssh/sshd_config.d/60-glasscast-keepalive.conf`（`ClientAliveInterval 15`、`ClientAliveCountMax 3`）。电脑合盖或断网后，sshd 约 45 秒就回收旧会话占着的 18766，`tunnel.sh` 才能重新绑定。代价是服务器上所有无响应的 SSH 客户端约 45 秒后被断开，正常会话不受影响。

   | 步骤 | 做什么 |
   |---|---|
   | 0 | 前置检查：root、部署文件、nginx、certbot |
   | 1 | DNS 校验：域名的 A 记录必须等于本机公网 IP（经 `api.ipify.org` 探测，NAT 环境用 `--public-ip` 指定），否则打印指引并以退出码 2 结束 |
   | 2 | 建目录，把 `deploy/nginx/*.template` 渲染到 `/opt/glasscast/rendered/`，在 `/opt/glasscast/deploy/` 留一份部署包 |
   | 3 | 装 upstream 和 80 端口 vhost（只放行 ACME 校验，其余 301 到 https） |
   | 4 | 自测 ACME 校验路径（本机 + 公网） |
   | 5 | `certbot --webroot` 签证书；已有证书且 30 天内不过期就跳过 |
   | 6 | 仅 1Panel 变体：把证书复制到站点目录 |
   | 7 | 装 443 vhost |
   | 8 | 配置证书续期，见 §8 |
   | 9 | 仅 `--sshd-keepalive`：sshd 保活 |
   | 10 | 验证：`healthz` 有响应（隧道没连时 502 属正常）、不带 Alt-Svc、`/admin/config` 是 404、http 跳 https |

**常用参数**（完整列表：`sh deploy/server-setup.sh --help`，也可以改脚本顶部的「改这里」一段）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--domain` | 无，必填 | 对外域名 |
| `--email` | 无，部署时必填 | Let's Encrypt 账号邮箱 |
| `--flavor` | `nginx` | `nginx` = 系统 nginx + certbot；`1panel` = 1Panel 的 OpenResty 容器 |
| `--tunnel-port` | `18766` | 服务器回环上的反向转发端口，须与 `tunnel.sh -r` 一致 |
| `--tunnel-addr` | `127.0.0.1` | nginx 连隧道端口用的地址，只有 1Panel 容器不是 host 网络时才改 |
| `--conf-dir` | `/etc/nginx/conf.d` | 放配置的目录 |
| `--public-ip` | 自动探测 | 手动指定本机公网 IP |
| `--render 目录` | | 只把模板渲染到目录，不需要 root，不碰系统，见 §2.7 |

**安全约定**：

- 每次往 `conf.d` 放文件都立刻 `nginx -t`，失败就还原并退出，不会留下让同机所有站点都 reload 不了的坏配置。
- 动手前先确认现有配置能通过 `nginx -t`，不在坏配置上叠加。
- 被覆盖的旧文件备份到 `/opt/glasscast/backup/<时间戳>/`。

部署成功后，参数保存在 `/opt/glasscast/deploy.env`，之后可以不带参数运行 `sudo sh /opt/glasscast/deploy/server-setup.sh --check`。

### 2.4 电脑端：配置、启动 glasscast 和隧道

1. **配置 `.env`**（只需一次）。`.env` 已在 `.gitignore` 里。最后一条的输出填到 `.env` 的 `GLASSCAST_TOKEN`：

   ```sh
   cd /Users/you/glasscast
   cp .env.example .env
   openssl rand -hex 24
   ```

   编辑 `.env`：

   ```sh
   export GLASSCAST_PUBLIC_URL=https://cast.example.com
   export GLASSCAST_TOKEN=上一步生成的随机串
   export GLASSCAST_TUNNEL_HOST=server.example.com     # 这两行在文件末尾，取消注释
   export GLASSCAST_TUNNEL_USER=ubuntu
   ```

   Token 要固定下来：平台插件发布后就锁定了，改域名或 token 只能新建插件，所以**域名和 Token 都要长期不变**。

2. **启动 glasscast**（终端 1）：

   ```sh
   . ./.env && uv run glasscast serve
   ```

   - `GLASSCAST_PUBLIC_URL`（或 `--public-url`）决定工具返回的图片地址和 MCP 放行的 Host，必须与对外域名完全一致，否则平台会收到 421。
   - 监听地址保持 `127.0.0.1:8766`，不要改成 `0.0.0.0`。
   - macOS 要给启动它的终端程序开「屏幕录制」权限，否则只能截到黑屏或壁纸，见 [platform-setup.md](platform-setup.md) 第 1 步。

3. **启动隧道**（终端 2）：

   ```sh
   . ./.env && ./deploy/tunnel.sh --keep-awake
   ```

   `tunnel.sh` 的参数都可以用环境变量给，命令行优先：

   | 参数 | 环境变量 | 默认 | 说明 |
   |---|---|---|---|
   | `-H, --host` | `GLASSCAST_TUNNEL_HOST` | 无，必须指定 | SSH 服务器 |
   | `-u, --user` | `GLASSCAST_TUNNEL_USER` | `ubuntu` | SSH 用户 |
   | `-p, --ssh-port` | `GLASSCAST_TUNNEL_SSH_PORT` | `22` | SSH 端口 |
   | `-i, --identity` | | ssh-agent / `~/.ssh/config` | 私钥文件 |
   | `-l, --local-port` | `GLASSCAST_LOCAL_PORT` 或 `GLASSCAST_PORT` | `8766` | 电脑上 glasscast 的端口 |
   | `-r, --remote-port` | `GLASSCAST_REMOTE_PORT` | `18766` | 须与 `server-setup.sh --tunnel-port` 一致 |
   | `--public-url` | `GLASSCAST_PUBLIC_URL` | 无 | 保温请求和提示文字用的地址 |
   | `--warm-interval` | `GLASSCAST_WARM_INTERVAL` | `30` | 每隔多少秒经公网请求一次 `/healthz` 给连接池保温，0 关闭，见 §7 |
   | `--keep-awake` | | 关 | 用 `caffeinate -is` 阻止空闲睡眠，随脚本退出 |
   | `--dry-run` | | | 只打印 ssh 命令和等价的 autossh 命令 |
   | `-- 参数...` | | | 原样传给 ssh，例如 `-- -v` 看调试输出 |

   其余参数（`--local-host`、`--remote-bind`、`--max-backoff`、`--once`、`--no-local-check`）见 `./deploy/tunnel.sh --help`。

   - **合盖仍会睡眠**，睡眠期间眼镜拿不到画面，唤醒后隧道自动重连。锁屏时也只能截到黑屏；`--keep-awake` 不管显示器，想让屏幕一直亮着，再开一个终端运行 `caffeinate -d`。
   - 启动前会探测本机 `/healthz`。glasscast 没运行时只告警，照样建隧道。
   - 认证失败、主机指纹未确认这类重试无用的错误会直接退出并提示。端口被占用（旧会话未回收）会提示并继续重连。
   - 想用 autossh 代替（没有保温请求）：`brew install autossh`，等价命令用 `--dry-run` 打印。

### 2.5 验证

健康检查、协议和管理接口。三条 `curl` 的期望依次是：`ok`；`HTTP/2 200` 且没有 `alt-svc` 行；`404`。

```sh
. ./.env
curl -sS https://cast.example.com/healthz
curl -sSI https://cast.example.com/healthz | grep -i -E '^(HTTP|alt-svc)'
curl -sS -o /dev/null -w '%{http_code}\n' https://cast.example.com/admin/config
```

图片。`KEY` 换成 glasscast 启动输出「图片地址」里 `/s/` 后面那一段：

```sh
curl -sS -o /tmp/a.jpg -w '%{http_code} %{size_download}B %{time_total}s\n' https://cast.example.com/s/KEY/screen.jpg
```

MCP。带 Bearer 能列出工具，耗时应远小于 3 秒：

```sh
curl -sS https://cast.example.com/mcp \
  -H "Authorization: Bearer $GLASSCAST_TOKEN" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' -w '\n%{time_total}s\n'
```

不带 Bearer，期望 401：

```sh
curl -sS -o /dev/null -w '%{http_code}\n' https://cast.example.com/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

全部通过后到平台注册 MCP 插件：服务地址 `https://cast.example.com/mcp`，认证方式 Bearer Token（即 `GLASSCAST_TOKEN`），传输协议选「无状态协议」。详见 [platform-setup.md](platform-setup.md) 第 2、3 步。

### 2.6 （可选）隧道开机自启：launchd

把下面内容存为 `~/Library/LaunchAgents/com.example.glasscast-tunnel.plist`，改好路径、服务器和域名，然后运行 `launchctl load ~/Library/LaunchAgents/com.example.glasscast-tunnel.plist`。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.glasscast-tunnel</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/you/glasscast/deploy/tunnel.sh</string>
    <string>-H</string><string>server.example.com</string>
    <string>-u</string><string>ubuntu</string>
    <string>-i</string><string>/Users/you/.ssh/id_ed25519</string>
    <string>--public-url</string><string>https://cast.example.com</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/you/Library/Logs/glasscast-tunnel.log</string>
  <key>StandardErrorPath</key><string>/Users/you/Library/Logs/glasscast-tunnel.log</string>
</dict>
</plist>
```

- launchd 不会读 `.env`，所以参数要写在 plist 里。
- launchd 进程拿不到终端里的 ssh-agent：私钥要么无口令，要么把口令存进钥匙串（`ssh-add --apple-use-keychain ~/.ssh/id_ed25519`，并在 `~/.ssh/config` 写 `UseKeychain yes`、`AddKeysToAgent yes`）。
- 项目放在云盘同步目录时，文件可能被设成「仅在线」，launchd 启动时读不到脚本。把 `deploy/` 设成始终保留在本机，或把项目放到云盘目录以外。
- 停用：`launchctl unload ~/Library/LaunchAgents/com.example.glasscast-tunnel.plist`。
- `glasscast serve` 建议仍在终端里手动启动：屏幕录制权限是授给启动它的终端程序的，launchd 启动的进程要另外授权。

### 2.7 手工部署

不想让脚本动服务器时，可以只用它渲染配置，自己安装。`--render` 不需要 root，不碰系统。开始前按 §2.3 第 1 步把 `deploy/` 上传到 `~/glasscast-deploy`。以下命令在**服务器**上执行：

```sh
sh ~/glasscast-deploy/server-setup.sh --render ~/glasscast-out --domain cast.example.com
```

输出四个文件：

| 文件 | 放到哪 | 作用 |
|---|---|---|
| `glasscast-upstream.conf` | `conf.d`，每台服务器一份 | `upstream glasscast_tunnel`，http 级 |
| `cast.example.com.http.conf` | `conf.d` | 80 端口：ACME 校验 + 301 到 https |
| `cast.example.com.https.conf` | `conf.d` | 443 端口：反代 `/healthz`、`/s/`、`/mcp` |
| `glasscast-locations.inc` | 方案 B 用，见 §3；方案 A 不用 | location 片段 |

以下几段按顺序在同一个终端里执行。先进入输出目录，建好 ACME 校验目录：

```sh
cd ~/glasscast-out
sudo install -d /var/www/glasscast-acme
```

错误日志里可能有图片地址的 key，先建好文件、收紧权限（nginx 自己建的是 0644）：

```sh
sudo install -m 0640 -g adm /dev/null /var/log/nginx/glasscast-error.log
```

先装 upstream 和 80 端口：

```sh
sudo install -m 0644 glasscast-upstream.conf cast.example.com.http.conf /etc/nginx/conf.d/
sudo nginx -t && sudo systemctl reload nginx
```

签证书：

```sh
sudo certbot certonly --webroot --webroot-path /var/www/glasscast-acme \
  -d cast.example.com --cert-name cast.example.com \
  --agree-tos -m you@example.com --non-interactive --keep-until-expiring
```

再装 443 端口：

```sh
sudo install -m 0644 cast.example.com.https.conf /etc/nginx/conf.d/
sudo nginx -t && sudo systemctl reload nginx
```

配置续期后自动 reload：

```sh
sudo install -d /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/glasscast-reload-nginx.sh >/dev/null <<'EOF'
#!/bin/sh
nginx -t -q && systemctl reload nginx
EOF
sudo chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/glasscast-reload-nginx.sh
```

**用 Caddy、Traefik 等其他反代**时，照着 `deploy/nginx/glasscast-https.conf.template` 改写，要点：

- 只转发 `/healthz`、`/s/`、`/mcp`，`/admin/` 返回 404。
- Host 原样透传，否则 MCP 返回 421。
- 关访问日志。
- 关 HTTP/3。这一点要特别留意，有些反代默认就开（例如 Caddy，要在全局配置里写 `servers { protocols h1 h2 }`）。
- 到隧道端口的连接要复用，见 §7。

### 2.8 1Panel 用户

用 1Panel 面板管理服务器时，有两种方式，选一种。不需要再 `apt install nginx`。

**方式一：脚本部署（`--flavor 1panel`）**

```sh
ssh -t ubuntu@server.example.com 'sudo sh ~/glasscast-deploy/server-setup.sh --flavor 1panel --domain cast.example.com --email you@example.com --sshd-keepalive'
```

- 配置放进 `/opt/1panel/apps/openresty/openresty/conf/conf.d/`，每次改动都在容器里跑 `openresty -t`。
- 容器名自动找 `1Panel-openresty*`，找不到或不止一个时用 `--container` 指定。
- certbot 用 docker 跑（`certbot/certbot:latest`），数据在 `/opt/glasscast/letsencrypt/`，证书复制到站点目录的 `ssl/`。续期见 §8。
- 手写的 `conf.d/*.conf` **不会**出现在 1Panel 的网站列表里，这是正常的。不要再在面板里创建同名网站，否则 `server_name` 重复（`--check` 会提示）。
- 脚本假设容器是 host 网络，并且宿主的 `.../openresty/root` 挂成容器里的 `/usr/share/nginx/html`，`.../openresty/www` 挂成 `/www`（1Panel 默认如此）。`--check` 和部署时的 ACME 自测会发现不一致。
- 容器不是 host 网络时，容器里的 `127.0.0.1` 连不到 sshd 的转发端口。首选把容器改回 host 网络；实在不行再按 `--check` 打印的提示，用 `--tunnel-addr` 和 `tunnel.sh --remote-bind` 绑 docker 网桥网关。不要绑 `0.0.0.0`，`tunnel.sh` 也会拒绝。

**方式二：1Panel 面板建站**（真机验证时用的方式，当时的配置比这里的早一版）

1. 面板「网站」→ 创建反向代理网站，域名填 `cast.example.com`，代理地址随便填（下面会替换）。
2. 在面板里申请 Let's Encrypt 证书（HTTP 校验或 DNS 校验都可以），开启 HTTPS。**不要开 HTTP3。**
3. 在服务器上渲染 upstream 并放进 `conf.d`（`deploy/` 已按 §2.3 上传到 `~/glasscast-deploy`）：

   ```sh
   sh ~/glasscast-deploy/server-setup.sh --render ~/glasscast-out --flavor 1panel --domain cast.example.com
   sudo cp ~/glasscast-out/glasscast-upstream.conf /opt/1panel/apps/openresty/openresty/conf/conf.d/
   ```

4. 用仓库里的 `root.conf` 替换面板生成的反代配置：

   ```sh
   sudo cp ~/glasscast-deploy/1panel/root.conf \
     /opt/1panel/apps/openresty/openresty/www/sites/cast.example.com/proxy/root.conf
   ```

5. 测试并生效。`1Panel-openresty-XXXX` 换成实际的容器名，用 `sudo docker ps --format '{{.Names}}' | grep -i openresty` 查：

   ```sh
   sudo docker exec 1Panel-openresty-XXXX openresty -t \
     && sudo docker exec 1Panel-openresty-XXXX openresty -s reload
   ```

注意：

- 这个 `root.conf` 把除 `/admin/` 之外的路径都转给 glasscast（不像 §2.7 的 vhost 只放行三类路径），`/admin/` 显式 404。
- 之后在面板里再修改这个反向代理时，面板会重写 `root.conf`，改完要重新复制。
- 面板默认给网站开访问日志。`root.conf` 里写了 `access_log off`，但替换之前的 `www/sites/cast.example.com/log/access.log` 可能已经记下了带 key 的地址，清掉它。
- 证书由面板续期。方案 D 同步证书时用 `--flavor 1panel`。

### 2.9 日常操作

在电脑上，终端 1 启动投屏服务：

```sh
. ./.env && uv run glasscast serve
```

终端 2 开隧道：

```sh
. ./.env && ./deploy/tunnel.sh --keep-awake
```

验证：

```sh
curl -sS https://cast.example.com/healthz
```

在服务器上，三条依次是体检、看隧道是否在线（应看到 sshd）、看错误日志（可能含 key）：

```sh
sudo sh /opt/glasscast/deploy/server-setup.sh --check
sudo ss -ltnp 'sport = :18766'
sudo tail -n 50 /var/log/nginx/glasscast-error.log
```

1Panel 的错误日志在 `/opt/1panel/apps/openresty/openresty/www/sites/cast.example.com/log/error.log`。

### 2.10 回滚

按影响从小到大：

1. **只停用暴露**：在电脑上 Ctrl-C 结束 `tunnel.sh`（用了自启的话 `launchctl unload`）。服务器配置还在，外部访问得到 502，不影响同机其他站点。
2. **停用服务器配置**：

   ```sh
   ssh -t ubuntu@server.example.com 'sudo sh /opt/glasscast/deploy/server-setup.sh --uninstall'
   ```

   把本域名的配置、续期任务、sshd drop-in **移到** `/opt/glasscast/disabled/<时间戳>/`（不删除），`nginx -t` 通过后 reload。证书保留。如果移走后 `nginx -t` 反而失败（比如方案 B 的 include 还在用这个 upstream），会把文件放回原处并报错。

3. **恢复**：`ssh -t ubuntu@server.example.com 'sudo sh /opt/glasscast/deploy/server-setup.sh'`，沿用保存的参数。装过 sshd 保活的，恢复时要再加 `--sshd-keepalive`（这个开关不保存）。
4. **彻底清理**（确认不再需要后手工做）：

   - 系统 nginx：`sudo certbot delete --cert-name cast.example.com`，删除 `/opt/glasscast/` 和 `/var/www/glasscast-acme/`。
   - 1Panel：删除 `/opt/glasscast/` 和 `/opt/1panel/apps/openresty/openresty/www/sites/cast.example.com/`。
   - 到 DNS 服务商删除 A 记录，在平台下线 MCP 插件。

---

## 3. 方案 B：挂在已有站点的路径下

**适用**：服务器上已经有一个 HTTPS 站点（下文叫 `app.example.com`），不想加 DNS 记录和证书。隧道和方案 A 完全相同，区别只在服务器入口：对外地址是 `https://app.example.com/cast`。

**代价**：

- 和已有站点共用一个 `server` 块，配置写错会影响它，reload 也是一起生效。
- 已有站点如果由部署脚本重新生成配置，这段 include 会丢，要加回去。
- 证书、HTTP/3 设置、安全头都跟着已有站点。**已有站点开了 HTTP/3 或下发 Alt-Svc 时，这个方案不能用**，先用 §2.5 的命令查一下。

开始前：按 §2.2 第 3 步配好 SSH 免密，按 §2.3 第 1 步把 `deploy/` 上传到 `~/glasscast-deploy`，按 §2.4 第 1 步建好 `.env`。

**步骤**（第 1～4 步在服务器上）：

1. 渲染（`--domain` 填已有站点的域名，`--path-prefix` 默认 `/cast`）：

   ```sh
   sh ~/glasscast-deploy/server-setup.sh --render ~/glasscast-out --domain app.example.com --path-prefix /cast
   ```

   这里只用得到 `glasscast-upstream.conf` 和 `glasscast-locations.inc`。**不要**安装另外两个 `app.example.com.*.conf`，它们会和已有站点冲突。

2. 备份已有站点的配置，然后安装这两个文件。`glasscast-locations.inc` 不要放进 `conf.d`：`conf.d/*.conf` 在 http 级，location 放在那里 `nginx -t` 会失败。第一条里站点配置的路径以实际为准。

   ```sh
   sudo cp -p /etc/nginx/sites-enabled/app.example.com ~/app.example.com.bak
   sudo cp ~/glasscast-out/glasscast-upstream.conf /etc/nginx/conf.d/
   sudo install -d /etc/nginx/snippets
   sudo cp ~/glasscast-out/glasscast-locations.inc /etc/nginx/snippets/
   ```

3. 编辑已有站点的配置，在 `server { listen 443 ssl ...; }` 里、`location /` **之前**加一行：

   ```nginx
   include /etc/nginx/snippets/glasscast-locations.inc;
   ```

4. 测试并生效。`-t` 不通过就**不要** reload，把备份拷回去：

   ```sh
   sudo nginx -t && sudo systemctl reload nginx
   ```

   已有站点在 1Panel 上时：渲染加 `--flavor 1panel`，upstream 放进 `/opt/1panel/apps/openresty/openresty/conf/conf.d/`，片段放到 `www/` 下，include 写容器里看到的路径（`/www/...`），用 §2.8 的 `docker exec` 命令测试和 reload。

5. 电脑上把 `.env` 里的 `GLASSCAST_PUBLIC_URL` 改成带路径的地址，然后启动：

   ```sh
   export GLASSCAST_PUBLIC_URL=https://app.example.com/cast
   ```

   终端 1 启动投屏服务：

   ```sh
   . ./.env && uv run glasscast serve
   ```

   终端 2 开隧道：

   ```sh
   . ./.env && ./deploy/tunnel.sh --keep-awake
   ```

   验证。第一条期望 `ok`，第二条确认已有站点仍然正常：

   ```sh
   curl -sS https://app.example.com/cast/healthz
   curl -sS https://app.example.com/
   ```

6. 平台插件的服务地址填 `https://app.example.com/cast/mcp`。表单分成两栏时，基础 URL 填 `https://app.example.com/cast`，endpoint 填 `/mcp`；平台不接受带路径的基础 URL 时，基础 URL 填 `https://app.example.com`，endpoint 填 `/cast/mcp`。

片段的工作方式：

- `/cast/s/<key>/...` 转成 `/s/<key>/...`，`/cast/mcp` 转成 `/mcp`，glasscast 不需要知道前缀。
- 只转发 `healthz`、`s`、`mcp` 三类路径，`/cast/admin/` 显式 404。不要为 `/admin` 加转发。
- 用 `^~`，不让站点里已有的正则 location（例如给 `.jpg` 加缓存头的规则）抢走图片请求。
- `/s/` 上写了 `access_log off`，图片地址不进已有站点的访问日志。错误日志关不掉，见 §9。
- 不写 `add_header`，免得覆盖站点 server 级的安全头。

**回滚**：删掉 include 那一行，`sudo nginx -t && sudo systemctl reload nginx`。

---

## 4. 方案 C：cloudflared 临时隧道（仅测试）

**适用**：还没有服务器，只想快速验证能不能跑通。这会把本机服务暴露到一个公开的随机域名上。

先按 [使用手册 §5](usage.md#5-完整部署清单) 第 3 步建好 `.env`（`cp .env.example .env` 并填 `GLASSCAST_TOKEN`；`GLASSCAST_PUBLIC_URL` 会被下面的 `--public-url` 覆盖）。

装好 cloudflared，在终端 1 启动隧道，输出里找 `https://<随机>.trycloudflare.com`：

```sh
brew install cloudflared
cloudflared tunnel --url http://127.0.0.1:8766
```

在终端 2 启动 glasscast，`<随机>` 换成上一步的地址：

```sh
. ./.env && uv run glasscast serve --public-url https://<随机>.trycloudflare.com
```

再开一个终端验证。第一条应输出 `ok`；第二条有输出说明它在声明 HTTP/3：

```sh
curl -sS https://<随机>.trycloudflare.com/healthz
curl -sSI https://<随机>.trycloudflare.com/healthz | grep -i alt-svc
```

**限制与风险**：

- 地址**每次启动都变**，要同步改 `--public-url` 和平台插件。已发布的插件锁定后不能改，所以只能用于草稿插件的调试。
- Cloudflare 边缘可能下发 `Alt-Svc: h3=...`，临时隧道关不掉，MCP 注册和调用可能失败。
- 平台编辑器的预览面板经平台代理取图，取 trycloudflare 地址时实测返回过 503。真机不经过这个代理。
- 转发的是**整个端口**，没有反代那条 `/admin/` 的 404 规则：管理接口 `/admin/fps`、`/admin/config` 也能从公网访问到，只靠 Bearer Token 保护。测试结束就 Ctrl-C。
- 没有 SLA，有并发和带宽限制，延迟取决于节点，不适合长期使用。

**回滚**：Ctrl-C 结束 `cloudflared`，地址立即失效，本机和服务器都没有残留。

---

## 5. 方案 D：局域网直连（可选）

手机和电脑在同一个 Wi-Fi 时，图片本来要绕「手机 → VPS → SSH 隧道 → 电脑」一圈，跨境时每张要 0.4～0.7 s。方案 D 让手机直接从电脑取图，局域网里一张图约 15 ms。

真机上每张图还有约 0.65 s 的固定开销（手机经蓝牙传给眼镜、眼镜解码），所以这个方案改善的是延迟和稳定性，**不能**突破每秒 1.5～2 张的上限。

```
 千问平台（MCP）                 ─► 公网 DNS ─► VPS ─► SSH 隧道 ─► 电脑 glasscast     不变
 手机，在外面                    ─► 公网 DNS ─► VPS ─► SSH 隧道 ─► 电脑 glasscast     不变
 手机，家里 Wi-Fi ─► 路由器 DNS：cast.example.com = 192.168.1.50
                                 ─► 电脑 :443 lan_https_proxy.py ─► 127.0.0.1:8766
```

做法是让路由器把同一个域名解析到电脑的局域网地址，电脑用**同一张证书**在 443 上做 TLS。手机校验的是证书，所以地址不用改，平台那边也不用动。

**前提**：方案 A 已经跑通；路由器能自定义 DNS 记录（OpenWrt / dnsmasq，或者叫「自定义 hosts」「本地解析」「DNS 重写」的功能）。

**步骤**：

1. **固定电脑的局域网地址**。在路由器上给电脑加一条静态 DHCP 租约 `192.168.1.50`。macOS 的 Wi-Fi「私有地址」要设成「固定」或关闭，否则硬件地址会变，租约失效。

2. **把证书复制到电脑**。系统 nginx 部署运行：

   ```sh
   . ./.env && ./deploy/sync_lan_cert.sh
   ```

   1Panel 部署运行：

   ```sh
   . ./.env && ./deploy/sync_lan_cert.sh --flavor 1panel
   ```

   私钥只有 root 能读，所以 SSH 用户要能免密 sudo（`ssh ubuntu@server.example.com 'sudo -n true'` 不报错）。不想这样的话，自己把 `fullchain.pem`、`privkey.pem` 放到本机目录。

   | 参数 | 环境变量 | 默认 | 说明 |
   |---|---|---|---|
   | `-H, --host` | `GLASSCAST_TUNNEL_HOST` | 无，必须指定 | SSH 服务器 |
   | `-u, --user` | `GLASSCAST_TUNNEL_USER` | `ubuntu` | SSH 用户 |
   | `-p, --ssh-port` | `GLASSCAST_TUNNEL_SSH_PORT` | `22` | SSH 端口 |
   | `-d, --domain` | `GLASSCAST_DOMAIN` | 取 `GLASSCAST_PUBLIC_URL` 的主机名 | 证书的域名 |
   | `--flavor` | `GLASSCAST_SERVER_FLAVOR` | `nginx` | `nginx` 读 `/etc/letsencrypt/live/<域名>`；`1panel` 读 1Panel 站点的 `ssl` 目录 |
   | `--remote-dir` | `GLASSCAST_REMOTE_CERT_DIR` | 按 `--flavor` 推算 | 服务器上放证书的目录，给了就不看 `--flavor` |
   | `--dest` | `GLASSCAST_LAN_TLS_DIR` | 见下 | 本机目录 |

   本机默认目录：macOS 是 `~/Library/Application Support/glasscast/tls/`，其他系统是 `~/.config/glasscast/tls/`。目录 0700，文件 0600。

3. **启动转发**（再开一个终端）：

   ```sh
   . ./.env && uv run python tools/lan_https_proxy.py
   ```

   默认监听 `0.0.0.0:443`，解密后原样转给 `127.0.0.1:8766`。macOS 上普通用户可以监听所有网卡的 443，不用 sudo；只绑某个地址（`--host 192.168.1.50`）或在 Linux 上要用 sudo，或给 python 加 `cap_net_bind_service`。macOS 防火墙弹窗时选允许。

   | 参数 | 环境变量 | 默认 | 说明 |
   |---|---|---|---|
   | `--host` | `GLASSCAST_LAN_HOST` | `0.0.0.0` | 监听地址 |
   | `--port` | `GLASSCAST_LAN_PORT` | `443` | 监听端口，手机只会连 443 |
   | `--upstream-host` | `GLASSCAST_LAN_UPSTREAM_HOST` | `127.0.0.1` | 默认上游地址 |
   | `--upstream-port` | `GLASSCAST_LAN_UPSTREAM_PORT` 或 `GLASSCAST_PORT` | `8766` | 默认上游端口 |
   | `--tls-dir` | `GLASSCAST_LAN_TLS_DIR` | 同第 2 步 | 含 `fullchain.pem`、`privkey.pem` 的目录 |
   | `--cert`、`--key` | `GLASSCAST_LAN_CERT`、`GLASSCAST_LAN_KEY` | | 直接指定文件，优先于 `--tls-dir` |
   | `--route /前缀/=主机:端口` | | | 同一域名下还有别的服务时按路径分流并剥掉前缀，可重复，例如 `--route /other/=127.0.0.1:9000` |
   | `--idle-timeout` | | `300` | 分流模式下空闲连接多久关掉（秒） |
   | `-v, --verbose` | | | 分流模式下逐个请求打日志，key 已打码 |

4. **先在电脑上验证**，这时还不用动路由器。期望输出 `ok`：

   ```sh
   curl -sS --resolve cast.example.com:443:192.168.1.50 https://cast.example.com/healthz
   ```

5. **在路由器上加 DNS 覆盖**，把 `cast.example.com` 解析到 `192.168.1.50`。dnsmasq 的写法：

   ```
   address=/cast.example.com/192.168.1.50
   ```

   然后检查，期望输出 `192.168.1.50`：

   ```sh
   dig +short cast.example.com @192.168.1.1
   ```

6. **在手机上验证**：断开再连上 Wi-Fi 清掉 DNS 缓存，然后开始投屏。转发程序每条连接结束时打一行日志，开头的地址是手机的局域网地址，就说明走的是局域网。

**注意**：

- **`tunnel.sh` 要继续开着**。平台调用 MCP 走的是公网解析，不经过家里的路由器；手机离开家里 Wi-Fi 时也走公网。
- `tunnel.sh` 的保温请求不受路由器影响：`GLASSCAST_WARM_RESOLVE` 默认 `auto`，经 DoH（`https://1.1.1.1/dns-query`）查公网 A 记录并钉住。也可以直接写 VPS 的 IP，设成空串则按系统 DNS 解析。
- **开了覆盖之后，在家里的网络上 `curl` 测的是局域网路径**。想测公网路径，用手机流量，或者加 `--resolve cast.example.com:443:203.0.113.10`。
- **转发程序要一直开着**。它没运行、或电脑换了地址时，家里 Wi-Fi 下的手机取不到图。不用了就把路由器的 DNS 覆盖去掉。
- 手机开了加密 DNS 或代理工具时，可能绕过路由器的 DNS，走回公网。这不影响功能，只是没有加速。
- **证书续期后这份副本不会自己更新**。Let's Encrypt 证书约 90 天一张，约 60 天续期一次。到期前要重跑 `sync_lan_cert.sh`，再重启 `lan_https_proxy.py`。脚本运行时会打印到期日。过期后手机在家取图会失败。
- **私钥落到了本机磁盘**，和公网服务器用的是同一把，丢了等于域名证书泄露。
- 转发是原样字节流，没有 nginx 那条 `/admin/` 的 404 规则。局域网里的设备能访问到 `/mcp` 和 `/admin/`，它们都要 Bearer Token。

**回滚**：去掉路由器的 DNS 覆盖，Ctrl-C 结束 `lan_https_proxy.py`，删除本机的证书目录。

---

## 6. 方案对比

| 维度 | A 反向隧道 + 独立域名 | B 已有站点的路径下 | C cloudflared 临时隧道 | D 局域网直连（叠加在 A 上） |
|---|---|---|---|---|
| 需要什么 | 域名、VPS、sudo | 已有站点的配置权限 | 只要 cloudflared | 能改 DNS 的路由器 |
| 证书 | Let's Encrypt，自动续期 | 复用已有站点的 | Cloudflare 提供 | 复用 A 的，续期后手动同步 |
| 地址 | 稳定 | 稳定 | 每次变 | 同 A |
| 能用于已发布插件 | 能 | 能 | 不能 | 同 A |
| HTTP/3 | 不开 | 取决于已有站点，要自己查 | 可能下发 Alt-Svc | 不涉及（只有 HTTP/1.1） |
| `/admin/` | 公网 404 | 公网 404 | 公网可达，只靠 Token | 局域网可达，只靠 Token |
| 对其他服务的影响 | 无（独立 vhost，失败自动还原） | 与已有站点共用 `server` 块 | 无 | 占用电脑的 443 端口 |
| 回滚 | `--uninstall` | 删掉 include 那一行 | Ctrl-C | 去掉路由器的覆盖 |

---

## 7. 延迟与连接复用

平台调用工具的总超时约 3 秒，这 3 秒要覆盖「平台 → 服务器 → SSH 隧道 → 电脑」的全部往返。跨境链路上这个预算很紧，所以配置里有这些写法：

| 做法 | 在哪里 | 为什么 |
|---|---|---|
| 插件选「无状态协议」 | 平台 | 每次调用只发一个 `POST /mcp`，少一次 `initialize` 往返 |
| upstream `keepalive 16`、`keepalive_timeout 3600s` | `glasscast-upstream.conf` | 每条新连接都要在 SSH 隧道里新开一个 channel。一条跨境链路上实测，冷连接一次 `/healthz` 约 0.75 s，热连接约 0.25 s |
| uvicorn keep-alive 7200 s | `glasscast serve` | uvicorn 默认 5 s 就关空闲连接，连接池永远是冷的。比 nginx 的 3600 s 长，总是 nginx 先关，不会拿到对端刚关掉的连接 |
| 同一地址写两个 peer，`max_fails=0` | `glasscast-upstream.conf` | 隧道重连后池里的旧连接全部失效，而 nginx 对只有一个 server 的 upstream 不重试 |
| `proxy_next_upstream error timeout non_idempotent` | `/mcp` 的 location | 旧连接失败时立刻换新连接重发，POST 也重发。三个工具重复执行都安全 |
| 每 30 s 经公网请求一次 `/healthz` | `tunnel.sh --warm-interval` | 给连接池保温，走的就是平台请求的同一条路 |
| 超时后自动重试一次 | `skill/SKILL.md` 规则 6 | 平台提示超时时，模型同参数再调一次 |

配好之后，平台调试页调用 `cast_status` 在跨境链路上实测总耗时约 1.2 s。跨境链路仍可能第一次超时，重试能兜住大部分，详见 [platform-setup.md](platform-setup.md#q2对话里提示mcp-工具执行超时或眼镜说电脑上的投屏程序没有响应)。想根治，就把公网入口换到对国内线路好的机器上（例如 CN2、BGP 线路），跨境往返可以从约 0.4 s 降到几十毫秒。

图片下载没有 3 秒的限制，但同样受益于连接复用。

---

## 8. 证书续期

| 部署方式 | 谁来续期 | 续期后 | 检查 |
|---|---|---|---|
| 系统 nginx（默认） | certbot 软件包自带的定时任务 | `/etc/letsencrypt/renewal-hooks/deploy/glasscast-reload-nginx.sh` 检查配置并 reload | `systemctl list-timers \| grep certbot`；`sudo certbot renew --dry-run --cert-name cast.example.com` |
| `--flavor 1panel` | `glasscast-cert-renew.timer`（每周日 03:17，随机延迟 6 h，错过了开机补跑）运行 `/opt/glasscast/renew-cert.sh` | 证书变了才重装并 reload | `systemctl list-timers glasscast-cert-renew.timer`；`journalctl -u glasscast-cert-renew.service -n 50`；手动 `sudo /opt/glasscast/renew-cert.sh` |
| 1Panel 面板建站 | 面板的证书自动续期 | 面板负责 | 面板的证书页面 |
| 方案 D 的本机副本 | 不会自动更新 | 重跑 `sync_lan_cert.sh`，重启 `lan_https_proxy.py` | 脚本运行时打印到期日 |

- 除 DNS 校验外，续期都依赖 80 端口：不要关掉它，也不要去掉 80 端口 vhost 里放行 `/.well-known/acme-challenge/` 的规则。
- `--flavor 1panel` 下，如果装好了新证书但 reload 失败，会留下 `/opt/glasscast/.reload-pending`，下次运行（或任何一次 `server-setup.sh` reload 成功）会补上。
- 查线上证书：

  ```sh
  openssl s_client -connect cast.example.com:443 -servername cast.example.com </dev/null 2>/dev/null \
    | openssl x509 -noout -dates -issuer
  ```

---

## 9. 安全须知

- **图片地址里的 key 等于看屏口令**：拿到完整地址的人就能看到你的屏幕，眼镜发不了请求头，图片只靠它保护。不要外传地址和带地址的日志。怀疑泄露就重启 glasscast；固定了 `GLASSCAST_CAST_KEY` 的话换一个新的。
- **日志**：`deploy/` 里的配置都关了访问日志（方案 A 的 vhost、方案 B 的 `/s/`、1Panel 的 `root.conf`）。但隧道断开导致 502 / 504 时，`error_log` 会记下请求行，里面有 key，这份日志要当密码对待：系统 nginx 的日志文件是 0640（root 和 adm 组），`--flavor 1panel` 的日志目录是 0700。
- **隧道端口不应该对外**：18766 是明文 HTTP，直通 glasscast，绕过 TLS 和 nginx 的所有限制。它只绑回环；`tunnel.sh --remote-bind` 拒绝 `0.0.0.0`；不要在安全组里放行它。
- **`/admin/` 只在本机可用**：反代返回 404，glasscast 自己还要求与 `/mcp` 相同的 Bearer Token。眼镜上调帧率走 MCP 的 `start_cast(mode="fast", fps=N)`。方案 C 和方案 D 没有这条 404 规则，只靠 Token。
- **Token** 只放在 `.env` 和平台插件配置里。不要写进 SKILL.md、页面、data.json，不要提交到 git，不要把含密钥的技能发布到公开市场。技能用「个人私有」范围发布。
- **glasscast 只监听 `127.0.0.1`**，不要改成 `0.0.0.0`。
- **MCP 的 Host 校验**（DNS 重绑定保护）只放行 `--public-url` 里的主机和本机回环。不要为了省事关掉它（`GLASSCAST_DNS_REBINDING_PROTECTION=0`），需要多放行一个 Host 时用 `GLASSCAST_ALLOWED_HOSTS` 或 `--allowed-host`。
- **投屏的是屏幕上的一切**，包括弹出的通知和密码框。用 `GLASSCAST_REGION` 只投需要的区域，不用时说「停止投屏」。

---

## 10. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `healthz` 返回 **502** | 隧道没连上，或电脑上 glasscast 没运行 | 看 `tunnel.sh` 输出；电脑上 `curl http://127.0.0.1:8766/healthz`；服务器上 `sudo ss -ltnp 'sport = :18766'` 应看到 sshd |
| `healthz` 返回 **504** | glasscast 卡住，超过 3 s 没响应 | 看 glasscast 日志；`/s/` 超过 10 s 也会 504 |
| `tunnel.sh` 报 `remote port forwarding failed` | 上次断线的旧会话还占着 18766 | 等 sshd 回收（装了 `--sshd-keepalive` 约 45 s，否则可能很久）；或 `ssh ubuntu@server.example.com "sudo ss -ltnp 'sport = :18766'"` 找到旧 sshd 进程后 `sudo kill <pid>` |
| `tunnel.sh` 报 `Permission denied` 或 `Host key verification failed` 并退出 | 免密登录没配好，或没确认主机指纹 | `ssh-add -l` 检查；手工 `ssh ubuntu@server.example.com true` 一次 |
| `server-setup.sh` 以退出码 2 结束 | DNS 还没指向这台服务器 | 按打印的指引改 DNS，等生效后重跑；公网 IP 探测不准时加 `--public-ip` |
| certbot 签发失败 | DNS 未生效、80 端口被安全组拦截，或失败次数太多被限流 | 先用 `--cert-dry-run` 排查；限流时等 1 小时 |
| `/mcp` 返回 **421** | `--public-url` 的主机和实际 Host 不一致，或反代没有透传 Host | 检查 `GLASSCAST_PUBLIC_URL`；反代配置里要有 `proxy_set_header Host $host` |
| `/mcp` 返回 **401** | Token 不一致 | 对比平台插件里的 token 和 `GLASSCAST_TOKEN`；最常见的原因是没固定 token，重启后换了新的 |
| `/mcp` 返回 **406** | 手工 curl 时 `Accept` 头不对 | 写成 `Accept: application/json, text/event-stream` |
| `/s/...` 返回 **404** | key 不对（没固定 key 时每次重启都变） | 重新调用 `start_cast` 拿新地址；页面预览的 Mock 数据要换成新 key |
| `/s/...` 返回 **403** | 用了 GET / HEAD 以外的方法，包括浏览器跨域预检 OPTIONS | 预期行为。本地模拟器跨域取图用 `tools/simulator/serve.py --allow-proxy` 的 `/proxy?url=` |
| 平台「刷新 MCP 工具」失败或超时 | HTTP/3 升级、超过 3 s、隧道断开 | `curl -sSI https://cast.example.com/healthz` 确认没有 `alt-svc`；确认隧道在线；测 `/mcp` 耗时 |
| 平台提示「MCP 工具执行超时」 | 跨境链路慢，见 §7 | 确认插件选了「无状态协议」；确认 `tunnel.sh` 的保温没有关 |
| 响应里有 `alt-svc` | 反代或面板开了 HTTP/3 | 关掉；Cloudflare 代理要改成「仅 DNS」 |
| 证书错误 | 证书没装或过期 | 用 §8 的 `openssl` 命令查；手机的系统时间也要对 |
| reload 失败 | 配置语法错误，或 docker / 容器一时异常 | 系统 nginx：`sudo nginx -t`；1Panel：`sudo docker exec 1Panel-openresty-XXXX openresty -t`。修好后重跑 `server-setup.sh`，它每次都会 reload |
| `--check` 提示域名被别的配置引用 | 在面板里建过同名网站，或方式一和方式二混用了 | 只保留一种，见 §2.8 |
| 1Panel 面板里看不到这个网站 | 手写的 `conf.d/*.conf` 不进网站列表 | 正常，不要再建同名网站 |
| 方案 D：手机在家取不到图 | 转发程序没开、电脑换了地址、证书过期 | 看 `lan_https_proxy.py` 的日志；`curl --resolve` 复测，见 §5 第 4 步 |

---

## 11. 文件清单

| 仓库文件 | 在哪里用 | 作用 |
|---|---|---|
| `deploy/server-setup.sh` | 服务器，root | 幂等部署；`--check`、`--cert-dry-run`、`--sshd-keepalive`、`--uninstall`、`--render` |
| `deploy/nginx/glasscast-upstream.conf.template` | 渲染后进 `conf.d` | `upstream glasscast_tunnel`：同一地址两个 peer + keepalive |
| `deploy/nginx/glasscast-http.conf.template` | 渲染后进 `conf.d` | 80 端口：放行 ACME 校验，其余 301 到 https |
| `deploy/nginx/glasscast-https.conf.template` | 渲染后进 `conf.d` | 443 端口：`ssl http2`，反代 `/healthz`、`/s/`、`/mcp`，`/admin/` 404，其余 404 |
| `deploy/nginx/glasscast-path-prefix.locations.template` | 方案 B，渲染为 `glasscast-locations.inc` | location 片段，不要放进 `conf.d` |
| `deploy/1panel/root.conf` | 1Panel 面板建站 | 替换面板生成的 `proxy/root.conf` |
| `deploy/renew-cert.sh` | 仅 `--flavor 1panel`，装到 `/opt/glasscast/` | certbot 容器续期，证书变了才重装并 reload |
| `deploy/glasscast-cert-renew.service`、`.timer` | 仅 `--flavor 1panel`，装到 `/etc/systemd/system/` | 每周运行一次 `renew-cert.sh` |
| `deploy/tunnel.sh` | 电脑 | 反向 SSH 隧道，断线重连，连接池保温 |
| `deploy/sync_lan_cert.sh` | 电脑，方案 D | 把服务器上的证书复制到本机 |
| `tools/lan_https_proxy.py` | 电脑，方案 D | 局域网 HTTPS 转发，只用标准库 |

服务器上的布局（系统 nginx）：

```
/opt/glasscast/
  deploy.env                  # 上次成功部署的参数
  deploy/                     # 部署包副本，可就地 --check / 重跑 / --uninstall
  rendered/                   # 渲染好的配置，方案 B 的 glasscast-locations.inc 也在这里
  backup/<时间戳>/            # 被覆盖前的旧文件
  disabled/<时间戳>/          # --uninstall 移走的文件
/etc/nginx/conf.d/
  glasscast-upstream.conf
  cast.example.com.http.conf
  cast.example.com.https.conf
/etc/letsencrypt/live/cast.example.com/                         # 证书
/etc/letsencrypt/renewal-hooks/deploy/glasscast-reload-nginx.sh # 续期后 reload
/var/www/glasscast-acme/                                        # ACME 校验目录
/var/log/nginx/glasscast-error.log
/etc/ssh/sshd_config.d/60-glasscast-keepalive.conf              # 仅 --sshd-keepalive
```

`--flavor 1panel` 的不同之处：

```
/opt/glasscast/
  renew-cert.sh
  letsencrypt/                # certbot 容器的 /etc/letsencrypt
  .reload-pending             # 只在装了新证书但 reload 失败时存在
/opt/1panel/apps/openresty/openresty/
  conf/conf.d/                # 三个配置文件
  www/sites/cast.example.com/{ssl,log}/     # 容器里是 /www/sites/cast.example.com/
  root/.well-known/acme-challenge/          # 容器里在 /usr/share/nginx/html 下
/etc/systemd/system/glasscast-cert-renew.{service,timer}
```
