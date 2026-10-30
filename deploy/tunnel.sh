#!/usr/bin/env bash
# glasscast 反向 SSH 隧道（docs/deploy.md 方案 A / B，在电脑上运行），断线自动重连。
#
#   服务器 127.0.0.1:18766  <==SSH==  电脑 127.0.0.1:8766（glasscast serve）
#   服务器上的 nginx 把 https://<你的域名> 反代到它自己的 127.0.0.1:18766（deploy/server-setup.sh 装的配置）。
#   电脑主动连出去，所以电脑在 NAT 后面、换网络都没关系，也不需要公网 IP。
#
# 用法（先改下面"改这里"那一段，或者用环境变量 / 命令行参数）：
#   ./deploy/tunnel.sh -H server.example.com -u ubuntu   # 服务器 18766 -> 本机 8766
#   . ./.env && ./deploy/tunnel.sh --keep-awake          # 参数写在 .env 里（GLASSCAST_TUNNEL_HOST 等），运行期间阻止 macOS 空闲睡眠
#   ./deploy/tunnel.sh -l 8766 -r 18766 -H server.example.com -u ubuntu -i ~/.ssh/id_ed25519
#   ./deploy/tunnel.sh --dry-run                         # 只打印将要执行的 ssh 命令（以及等价的 autossh 命令）
#   ./deploy/tunnel.sh -- -v                             # `--` 之后的参数原样传给 ssh（这里是打开调试输出）
#
# 前提：
#   - 已能免密登录：ssh ubuntu@server.example.com true（BatchMode=yes，不会弹密码/主机指纹确认；
#     第一次连接请先手工 ssh 一次接受主机指纹）。
#   - 服务器 sshd 允许远程转发（Ubuntu 默认 AllowTcpForwarding yes）。server-setup.sh --check 会显示。
#
# 行为：
#   - ExitOnForwardFailure=yes：远端端口绑不上（被占用）就立即退出重连，而不是"连上了但没转发"。
#   - ServerAliveInterval=15 / ServerAliveCountMax=3：约 45 秒内发现网络已断（合盖、换 Wi-Fi），退出重连。
#   - 重连退避 1s、2s、4s… 最长 --max-backoff 秒；一次连接稳定超过 60 秒后退避清零。
#   - 远端只绑 127.0.0.1：隧道端口不对公网开放，只有服务器本机的 nginx 能访问。
#   - 隧道建立后每 30 s 经公网地址请求一次 /healthz，给 nginx 到隧道的长连接保温（--warm-interval）。
#   - Ctrl-C / SIGTERM：结束 ssh 并退出。
#
# 可以用 autossh 代替本脚本（效果相同，但没有保温请求）：
#   AUTOSSH_GATETIME=0 autossh -M 0 -N -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
#     -o ExitOnForwardFailure=yes -R 127.0.0.1:18766:127.0.0.1:8766 ubuntu@server.example.com
#   （brew install autossh；-M 0 表示不用 autossh 自带的监控端口，靠 ServerAlive 探活。）
#
# 兼容 macOS 自带的 bash 3.2（不用关联数组、不用 ${var,,} 等新语法）。
set -eu
set -o pipefail

# ================================ 改这里 ================================
# 每一项都可以不改文件：用环境变量（例如写进 .env 后 `. ./.env`）或命令行参数覆盖，命令行优先。
HOST="${GLASSCAST_TUNNEL_HOST:-server.example.com}"   # -H  你的服务器（SSH 地址或 IP）
SSH_USER="${GLASSCAST_TUNNEL_USER:-ubuntu}"           # -u  SSH 用户
SSH_PORT="${GLASSCAST_TUNNEL_SSH_PORT:-22}"           # -p  SSH 端口
REMOTE_PORT="${GLASSCAST_REMOTE_PORT:-18766}"         # -r  服务器上的反向转发端口，须与 server-setup.sh --tunnel-port 一致
LOCAL_PORT="${GLASSCAST_LOCAL_PORT:-${GLASSCAST_PORT:-8766}}"   # -l  电脑上 glasscast serve 的端口
# 对外地址：保温请求和提示文字用；与 glasscast serve 共用 GLASSCAST_PUBLIC_URL
PUBLIC_URL="${GLASSCAST_PUBLIC_URL:-https://cast.example.com}"
# ========================================================================

PLACEHOLDER_HOST="server.example.com"
PLACEHOLDER_URL="https://cast.example.com"
LOCAL_HOST="127.0.0.1"
REMOTE_BIND="127.0.0.1"
IDENTITY=""
MAX_BACKOFF=30
STABLE_SECONDS=60
CONNECT_TIMEOUT=10   # ssh -o ConnectTimeout：TCP 连接加 SSH 握手/密钥交换的上限
# ssh 活过这么久才报"隧道已建立"：连不上的服务器（SYN 被丢弃）要到 ConnectTimeout 才失败，
# 认证和远程端口绑定失败紧随其后，所以要比 ConnectTimeout 多留几秒，不能只等 5 秒
ESTABLISH_WAIT=$((CONNECT_TIMEOUT + 5))
ONCE=0
DRY_RUN=0
KEEP_AWAKE=0
LOCAL_CHECK=1
# 隧道建立后每隔这么多秒经公网地址拉一次 /healthz，让 nginx 到隧道的 keepalive 连接保持热态
# （冷连接要先在 SSH 隧道里打开新通道，多一次往返；平台调用工具的总超时只有约 3 s）。0 = 关闭
WARM_INTERVAL="${GLASSCAST_WARM_INTERVAL:-30}"
# 保温请求强制连到域名的公网地址（curl --resolve），不受本地 DNS 影响：用了方案 D（家里路由器把域名解析到
# 这台电脑的局域网地址）时，不钉住的话保温请求会落到本机，服务器那条隧道连接就冷了。
# auto = 经 DoH（https://1.1.1.1/dns-query）查域名的公网 A 记录；也可以直接写 IP；空串 = 不钉住
WARM_RESOLVE="${GLASSCAST_WARM_RESOLVE-auto}"
EXTRA_SSH=()

usage() {
    cat <<EOF
用法：$0 [选项] [-- 额外的 ssh 参数...]

  -H, --host HOST          SSH 服务器（默认 ${HOST}，环境变量 GLASSCAST_TUNNEL_HOST）
  -u, --user USER          SSH 用户（默认 ${SSH_USER}，环境变量 GLASSCAST_TUNNEL_USER）
  -p, --ssh-port PORT      SSH 端口（默认 ${SSH_PORT}，环境变量 GLASSCAST_TUNNEL_SSH_PORT）
  -i, --identity FILE      私钥文件（默认用 ssh-agent / ~/.ssh/config）
  -l, --local-port PORT    电脑上 glasscast 的端口（默认 ${LOCAL_PORT}，环境变量 GLASSCAST_LOCAL_PORT 或 GLASSCAST_PORT）
      --local-host ADDR    电脑上 glasscast 的地址（默认 ${LOCAL_HOST}）
  -r, --remote-port PORT   服务器上的反向转发端口（默认 ${REMOTE_PORT}，环境变量 GLASSCAST_REMOTE_PORT；
                           须与服务器 nginx 配置一致，即 server-setup.sh --tunnel-port）
      --remote-bind ADDR   服务器上绑定的地址（默认 127.0.0.1；只在 1Panel 的 OpenResty 容器不是 host 网络时
                           改成 docker 网桥网关地址，如 172.17.0.1，并给 sshd 开 GatewayPorts clientspecified；
                           0.0.0.0 / * / :: 会把明文端口开到所有网卡，直接拒绝）
      --public-url URL     对外地址，用于保温请求和提示文字（默认 ${PUBLIC_URL}，环境变量 GLASSCAST_PUBLIC_URL；
                           方案 B 写 https://app.example.com/cast 这样带路径的地址）
      --max-backoff SEC    重连最长等待秒数（默认 ${MAX_BACKOFF}）
      --once               只连一次，断开就退出（调试用）
      --keep-awake         运行期间用 caffeinate 阻止 macOS 空闲睡眠（合盖仍会睡眠）
      --no-local-check     不检查本机 glasscast 是否在运行
      --warm-interval SEC  每 SEC 秒经公网地址请求一次 /healthz，给 nginx 的隧道长连接保温
                           （默认 ${WARM_INTERVAL}，环境变量 GLASSCAST_WARM_INTERVAL；0 关闭）
      --dry-run            只打印 ssh 命令和等价的 autossh 命令，不执行
  -h, --help               显示本帮助

环境变量 GLASSCAST_WARM_RESOLVE：保温请求连到哪个 IP。auto（默认）= 经 DoH（1.1.1.1）查公网 A 记录；
也可以直接写服务器 IP；设成空串就按系统 DNS 解析。
EOF
}

log() { printf '%s [tunnel] %s\n' "$(date '+%H:%M:%S')" "$*"; }
err() { printf '%s [tunnel] %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
die() { err "错误：$*"; exit 1; }

need_arg() { [ "$2" -ge 2 ] || die "$1 需要一个参数"; }

while [ $# -gt 0 ]; do
    case $1 in
        -H|--host) need_arg "$1" $#; HOST=$2; shift 2 ;;
        -u|--user) need_arg "$1" $#; SSH_USER=$2; shift 2 ;;
        -p|--ssh-port) need_arg "$1" $#; SSH_PORT=$2; shift 2 ;;
        -i|--identity) need_arg "$1" $#; IDENTITY=$2; shift 2 ;;
        -l|--local-port) need_arg "$1" $#; LOCAL_PORT=$2; shift 2 ;;
        --local-host) need_arg "$1" $#; LOCAL_HOST=$2; shift 2 ;;
        -r|--remote-port) need_arg "$1" $#; REMOTE_PORT=$2; shift 2 ;;
        --remote-bind) need_arg "$1" $#; REMOTE_BIND=$2; shift 2 ;;
        --public-url) need_arg "$1" $#; PUBLIC_URL=${2%/}; shift 2 ;;
        --max-backoff) need_arg "$1" $#; MAX_BACKOFF=$2; shift 2 ;;
        --once) ONCE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --keep-awake) KEEP_AWAKE=1; shift ;;
        --no-local-check) LOCAL_CHECK=0; shift ;;
        --warm-interval) need_arg "$1" $#; WARM_INTERVAL=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        --) shift; EXTRA_SSH=("$@"); break ;;
        *) usage >&2; die "未知参数：$1" ;;
    esac
done

is_port() {
    case $1 in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}
is_port "$SSH_PORT" || die "SSH 端口不合法：$SSH_PORT"
is_port "$LOCAL_PORT" || die "本地端口不合法：$LOCAL_PORT"
is_port "$REMOTE_PORT" || die "远端端口不合法：$REMOTE_PORT"
case $MAX_BACKOFF in ''|*[!0-9]*) die "--max-backoff 必须是正整数：$MAX_BACKOFF" ;; esac
[ "$MAX_BACKOFF" -ge 1 ] || die "--max-backoff 必须 ≥ 1"
case $WARM_INTERVAL in ''|*[!0-9]*) die "--warm-interval 必须是非负整数：$WARM_INTERVAL" ;; esac
if [ -n "$IDENTITY" ] && [ ! -r "$IDENTITY" ]; then
    die "私钥文件不可读：$IDENTITY"
fi
case $REMOTE_BIND in
    127.0.0.1|localhost) ;;
    ''|'*'|0.0.0.0|::|'[::]')
        # 隧道端口是明文 HTTP，直通 glasscast：/s/<key>/、/mcp、/admin 都会绕过 TLS 和 nginx 的限制，
        # 安全组或防火墙一旦放行就是公网可达（docs/deploy.md「安全须知」：这个端口不应该对外）
        die "--remote-bind $REMOTE_BIND 会把服务器上的 ${REMOTE_PORT}（明文、绕过 TLS 和 nginx）开到所有网卡，不允许。OpenResty 容器不是 host 网络时，绑 docker 网桥网关地址（例如 172.17.0.1）" ;;
    *) err "注意：--remote-bind $REMOTE_BIND 会让服务器上的 $REMOTE_PORT 在这个地址上可见（需要 sshd GatewayPorts clientspecified）；只应该是 docker 网桥网关这类内部地址" ;;
esac
if [ "$HOST" = "$PLACEHOLDER_HOST" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        err "注意：服务器还是文档里的占位符 $PLACEHOLDER_HOST"
    else
        die "服务器还是文档里的占位符 ${PLACEHOLDER_HOST}：用 -H <你的服务器> 指定，或设置环境变量 GLASSCAST_TUNNEL_HOST（也可以改脚本顶部「改这里」）"
    fi
fi
if [ "$PUBLIC_URL" = "$PLACEHOLDER_URL" ] && [ "$WARM_INTERVAL" -gt 0 ]; then
    err "注意：对外地址还是占位符 ${PLACEHOLDER_URL}，不做保温请求（用 --public-url 或环境变量 GLASSCAST_PUBLIC_URL 指定你的域名）"
    WARM_INTERVAL=0
fi

FORWARD="$REMOTE_BIND:$REMOTE_PORT:$LOCAL_HOST:$LOCAL_PORT"

# 公共 ssh 选项（ssh 与 autossh 共用）
SSH_OPTS=(
    -N -T
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=3
    -o ExitOnForwardFailure=yes
    -o ConnectTimeout="$CONNECT_TIMEOUT"
    -o TCPKeepAlive=yes
    -o BatchMode=yes
    # 不复用 ~/.ssh/config 里可能配置的连接复用（ControlMaster），否则转发会挂在别的连接上，重连语义不可控
    -o ControlMaster=no
    -o ControlPath=none
    -p "$SSH_PORT"
)
if [ -n "$IDENTITY" ]; then
    SSH_OPTS+=(-i "$IDENTITY" -o IdentitiesOnly=yes)
fi
SSH_OPTS+=(-R "$FORWARD")
# bash 3.2 在 set -u 下展开空数组会报 unbound variable，所以用 ${arr[@]+...} 写法
SSH_CMD=(ssh -n "${SSH_OPTS[@]}" ${EXTRA_SSH[@]+"${EXTRA_SSH[@]}"} "$SSH_USER@$HOST")

if [ "$DRY_RUN" -eq 1 ]; then
    printf 'ssh 命令：\n  '
    printf '%q ' "${SSH_CMD[@]}"
    printf '\n等价的 autossh 命令（autossh 负责重启，-M 0 关闭其监控端口）：\n  AUTOSSH_GATETIME=0 autossh -M 0 '
    printf '%q ' "${SSH_OPTS[@]}" ${EXTRA_SSH[@]+"${EXTRA_SSH[@]}"} "$SSH_USER@$HOST"
    printf '\n'
    exit 0
fi

command -v ssh >/dev/null 2>&1 || die "找不到 ssh"

SSH_PID=""
WARM_PID=""
ERR_FILE=$(mktemp "${TMPDIR:-/tmp}/glasscast-tunnel.XXXXXX")

stop_warm() {
    if [ -n "$WARM_PID" ] && kill -0 "$WARM_PID" 2>/dev/null; then
        kill "$WARM_PID" 2>/dev/null || true
        wait "$WARM_PID" 2>/dev/null || true
    fi
    WARM_PID=""
}
cleanup() {
    trap - INT TERM EXIT
    stop_warm
    if [ -n "$SSH_PID" ] && kill -0 "$SSH_PID" 2>/dev/null; then
        kill "$SSH_PID" 2>/dev/null || true
        wait "$SSH_PID" 2>/dev/null || true
    fi
    rm -f "$ERR_FILE"
}
on_signal() {
    log "收到退出信号，关闭隧道"
    cleanup
    exit 0
}
trap on_signal INT TERM
trap cleanup EXIT

if [ "$KEEP_AWAKE" -eq 1 ]; then
    if command -v caffeinate >/dev/null 2>&1; then
        # -i 阻止空闲睡眠，-s 接电源时阻止系统睡眠；-w 跟随本脚本进程，脚本退出后自动结束
        caffeinate -is -w $$ &
        log "已启用 caffeinate（随本脚本退出）"
    else
        err "找不到 caffeinate（非 macOS？），忽略 --keep-awake"
    fi
fi

check_local() {
    [ "$LOCAL_CHECK" -eq 1 ] || return 0
    command -v curl >/dev/null 2>&1 || return 0
    if curl -fsS -m 2 -o /dev/null "http://$LOCAL_HOST:$LOCAL_PORT/healthz" 2>/dev/null; then
        log "本机 glasscast 在线（http://$LOCAL_HOST:$LOCAL_PORT/healthz）"
    else
        err "本机 http://$LOCAL_HOST:$LOCAL_PORT/healthz 不通：glasscast 还没启动？隧道照样建立，但外部请求会得到 502。"
        err "  启动：uv run glasscast serve --public-url $PUBLIC_URL --port $LOCAL_PORT"
    fi
}

# 保温：nginx 的 upstream keepalive 池（deploy/nginx/ 的 upstream glasscast_tunnel）只有被用到才保持热态。
# 经公网地址请求，走的就是平台请求的同一条路（TLS → nginx → 池里的连接 → sshd → 本机）。
warm_resolve_args() {
    # 输出 curl 的 --resolve 参数（每行一个）；查不到就不输出，退回按系统解析
    [ -n "$WARM_RESOLVE" ] || return 0
    local hostport host ip
    hostport=${PUBLIC_URL#*://}; hostport=${hostport%%/*}; host=${hostport%%:*}
    ip=$WARM_RESOLVE
    if [ "$ip" = auto ]; then
        # 用 DoH（https://1.1.1.1/dns-query）查：家里路由器开了「DNS 重定向」时，发往 53 端口的查询（包括 dig @1.1.1.1）
        # 都会被路由器接管，拿到的是本地解析的局域网地址
        ip=$(curl -fsS -m 4 -H 'accept: application/dns-json' "https://1.1.1.1/dns-query?name=$host&type=A" 2>/dev/null \
            | grep -oE '"data": *"[0-9]+(\.[0-9]+){3}"' | grep -oE '[0-9]+(\.[0-9]+){3}' | tail -1 || true)
    fi
    [ -n "$ip" ] || return 0
    printf '%s\n' --resolve "$host:443:$ip"
}
warm_loop() {
    local args=()
    while :; do
        sleep "$WARM_INTERVAL"
        args=()
        while IFS= read -r line; do args+=("$line"); done < <(warm_resolve_args)
        curl -fsS -m 5 -o /dev/null ${args[@]+"${args[@]}"} "$PUBLIC_URL/healthz" 2>/dev/null || true
    done
}
start_warm() {
    [ "$WARM_INTERVAL" -gt 0 ] || return 0
    command -v curl >/dev/null 2>&1 || return 0
    warm_loop &
    WARM_PID=$!
    log "保温：每 ${WARM_INTERVAL}s 请求一次 $PUBLIC_URL/healthz（--warm-interval 0 关闭；钉住地址：$(warm_resolve_args | tail -1)）"
}

# 根据 ssh 的报错给出提示。返回 1 表示是重试也无法自愈的错误（应退出）。
explain_failure() {
    if grep -q 'remote port forwarding failed' "$ERR_FILE"; then
        err "提示：服务器上 $REMOTE_BIND:$REMOTE_PORT 已被占用，多半是上一次断线的旧会话还没被 sshd 回收。"
        err "  等它超时即可（服务器跑过 server-setup.sh --sshd-keepalive 时约 45 秒）；"
        err "  或手动查看：ssh $SSH_USER@$HOST \"sudo ss -ltnp 'sport = :$REMOTE_PORT'\"，确认是旧的 sshd 会话后 kill 掉它。"
        return 0
    fi
    if grep -q 'Host key verification failed' "$ERR_FILE"; then
        err "提示：主机指纹未确认。先手工执行一次：ssh -p $SSH_PORT $SSH_USER@$HOST true ，确认指纹后再运行本脚本。"
        return 1
    fi
    if grep -q 'Permission denied' "$ERR_FILE"; then
        err "提示：免密登录失败。确认 ssh-agent 已加载私钥（ssh-add -l），或用 -i 指定私钥。"
        return 1
    fi
    if grep -Eq 'Could not resolve hostname' "$ERR_FILE"; then
        err "提示：解析不了 ${HOST}，检查网络或主机名。"
    fi
    return 0
}

log "隧道：服务器 $HOST 的 $REMOTE_BIND:$REMOTE_PORT  ->  本机 $LOCAL_HOST:${LOCAL_PORT}（Ctrl-C 退出）"

backoff=1
attempt=0
while :; do
    attempt=$((attempt + 1))
    check_local
    log "第 $attempt 次连接 $SSH_USER@$HOST:$SSH_PORT ..."
    : > "$ERR_FILE"
    started=$(date +%s)
    "${SSH_CMD[@]}" 2>"$ERR_FILE" &
    SSH_PID=$!

    # ESTABLISH_WAIT 秒（ConnectTimeout + 5）内没退出才认为隧道已建立：连接超时、认证失败、
    # 远程端口绑定失败（ExitOnForwardFailure）都会在这之前让 ssh 退出
    waited=0
    while [ "$waited" -lt "$ESTABLISH_WAIT" ] && kill -0 "$SSH_PID" 2>/dev/null; do
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$SSH_PID" 2>/dev/null; then
        log "隧道已建立（ssh 已稳定运行 ${ESTABLISH_WAIT}s）。验证：curl $PUBLIC_URL/healthz"
        start_warm
    fi

    rc=0
    wait "$SSH_PID" || rc=$?
    SSH_PID=""
    stop_warm
    lasted=$(( $(date +%s) - started ))
    if [ -s "$ERR_FILE" ]; then
        sed 's/^/    ssh: /' "$ERR_FILE" >&2
    fi
    err "ssh 退出（退出码 ${rc}，本次持续 ${lasted}s）"

    fatal=0
    explain_failure || fatal=1
    if [ "$fatal" -eq 1 ]; then
        die "以上错误重试也不会自愈，退出"
    fi
    if [ "$ONCE" -eq 1 ]; then
        exit "$rc"
    fi

    if [ "$lasted" -ge "$STABLE_SECONDS" ]; then
        backoff=1
    fi
    log "${backoff}s 后重连"
    sleep "$backoff"
    backoff=$((backoff * 2))
    if [ "$backoff" -gt "$MAX_BACKOFF" ]; then
        backoff=$MAX_BACKOFF
    fi
done
