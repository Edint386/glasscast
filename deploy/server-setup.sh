#!/bin/sh
# glasscast 服务器端一键部署（docs/deploy.md 方案 A：反向 SSH 隧道 + nginx 反代 + Let's Encrypt）。
# 在你自己的 VPS 上以 root 运行。支持两种服务器环境：
#   --flavor nginx   （默认）系统自带的 nginx + certbot（Debian/Ubuntu：apt install nginx certbot）
#   --flavor 1panel  （可选变体）1Panel 面板装的 OpenResty 容器，certbot 用 docker 跑
#
# 用法（先把整个 deploy/ 目录传到服务器，见 docs/deploy.md §2.3）：
#   sudo sh server-setup.sh --check --domain cast.example.com --email you@example.com   # 只检查，不改任何东西
#   sudo sh server-setup.sh --domain cast.example.com --email you@example.com --cert-dry-run   # 演练签证书
#   sudo sh server-setup.sh --domain cast.example.com --email you@example.com --sshd-keepalive # 部署（幂等）
#   sudo sh server-setup.sh --uninstall                                  # 回滚：停用配置（证书保留）
#   sh server-setup.sh --render ./out --domain cast.example.com          # 只把模板渲染成配置文件看看/手工装，
#                                                                        # 不需要 root，不碰系统
#   部署后服务器上会留一份部署包 /opt/glasscast/deploy/ 和本次参数 /opt/glasscast/deploy.env，之后可以直接
#   sudo sh /opt/glasscast/deploy/server-setup.sh --check（或 --uninstall、重跑），不用再写参数。
#
# 做的事情（每一步都有输出，失败即停）：
#   0. 前置检查：root、部署文件齐全、nginx（或 OpenResty 容器）、certbot
#   1. DNS 校验：域名的 A 记录必须等于本机公网 IP，否则提示去 DNS 服务商加记录并退出（退出码 2）
#   2. 建目录，把 deploy/nginx/*.template 渲染到 /opt/glasscast/rendered/
#   3. 装 upstream + 80 端口 vhost（只放行 ACME 校验，其余 301 到 https），nginx -t && reload
#   4. 自测 ACME 校验路径可达（本机 + 公网）
#   5. certbot --webroot 签证书（已有且 30 天内不过期则跳过）
#   6. （仅 1panel）复制证书到站点目录
#   7. 装 443 vhost（/healthz、/s/、/mcp 反代到隧道端口，/admin/ 404），nginx -t && reload
#   8. 续期：nginx 变体靠 certbot 自带的定时任务 + 本脚本装的 deploy hook 自动 reload；
#      1panel 变体装 /opt/glasscast/renew-cert.sh + glasscast-cert-renew.timer
#   9. （可选 --sshd-keepalive）sshd 保活 drop-in
#  10. 验证：https 可用、不带 Alt-Svc、/admin/ 是 404、http→https 301
#
# 安全约定：
#   - 每次往 conf.d 放文件都立刻 nginx -t，失败就还原该文件再退出，不会把坏配置留在 conf.d 里
#     （坏配置会让同机所有站点之后都 reload 不了）。
#   - 覆盖前把旧文件备份到 /opt/glasscast/backup/<时间戳>/；--uninstall 只"移走"，不删除。
#   - 第 3、7 步每次运行都 reload（graceful，不断连接），不管文件这次有没有变化：上一次运行如果死在
#     reload 这一步，文件已经落盘、nginx 却没加载；只在"有变化"时 reload 的话，重跑会一直报"已是最新"。
set -eu

# ================================ 改这里 ================================
# 也可以不改文件，全部用命令行参数给（命令行优先）。留空 = 用上次部署保存的值（/opt/glasscast/deploy.env），
# 再没有就用括号里的默认值。
DOMAIN=""               # --domain          对外域名（必填），例如 cast.example.com；要有一条 A 记录指向本机公网 IP
EMAIL=""                # --email           Let's Encrypt 账号邮箱（部署时必填，用于证书到期提醒）
FLAVOR=""               # --flavor          nginx（默认）或 1panel
TUNNEL_PORT=""          # --tunnel-port     服务器回环地址上的反向转发端口（默认 18766），须与 tunnel.sh -r 一致
TUNNEL_ADDR=""          # --tunnel-addr     nginx 连隧道端口用的地址（默认 127.0.0.1；只有 1Panel 容器不是 host 网络时才改）
NGINX_CONF_DIR=""       # --conf-dir        放配置的 conf.d（默认 nginx：/etc/nginx/conf.d；1panel：<OPENRESTY_ROOT>/conf/conf.d）
OPENRESTY_ROOT=""       # --openresty-root  仅 1panel：1Panel OpenResty 的安装目录（默认 /opt/1panel/apps/openresty/openresty）
OPENRESTY_CONTAINER=""  # --container       仅 1panel：OpenResty 容器名（默认自动找名字以 1Panel-openresty 开头的容器）
PATH_PREFIX=""          # --path-prefix     只用于渲染方案 B 的 location 片段 glasscast-locations.inc（默认 /cast）
# ========================================================================

CERTBOT_IMAGE="certbot/certbot:latest"     # 仅 1panel：签证书 / 续期用的 certbot 容器镜像

# 仅供本地测试：把所有绝对路径挂到一个临时根目录下（配合 PATH 里假的 nginx / certbot / docker 等）。
# 在服务器上不要设置它。
PREFIX="${GLASSCAST_TEST_PREFIX:-}"

DEPLOY_ROOT="$PREFIX/opt/glasscast"
SAVED_ENV="$DEPLOY_ROOT/deploy.env"
RENDERED_DIR="$DEPLOY_ROOT/rendered"
RELOAD_MARKER="$DEPLOY_ROOT/.reload-pending"   # renew-cert.sh 装了新证书但还没 reload 成功时留下的标记
SYSTEMD_DIR="$PREFIX/etc/systemd/system"
SSHD_CONFIG="$PREFIX/etc/ssh/sshd_config"
SSHD_DROPIN_DIR="$PREFIX/etc/ssh/sshd_config.d"
SSHD_DROPIN="$SSHD_DROPIN_DIR/60-glasscast-keepalive.conf"
UNIT_SERVICE="glasscast-cert-renew.service"
UNIT_TIMER="glasscast-cert-renew.timer"
UPSTREAM_CONF_NAME="glasscast-upstream.conf"
LOCATIONS_INC_NAME="glasscast-locations.inc"
STAMP=$(date '+%Y%m%d-%H%M%S')-$$
BACKUP_DIR="$DEPLOY_ROOT/backup/$STAMP"

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
TEMPLATE_DIR="$SCRIPT_DIR/nginx"
# 部署包（相对 deploy/）：前置检查要求齐全，部署时整套复制到 $DEPLOY_ROOT/deploy/
PACKAGE_FILES="server-setup.sh renew-cert.sh $UNIT_SERVICE $UNIT_TIMER
nginx/glasscast-upstream.conf.template nginx/glasscast-http.conf.template
nginx/glasscast-https.conf.template nginx/glasscast-path-prefix.locations.template"

# ---------------------------------------------------------------------------
# 输出工具
# ---------------------------------------------------------------------------
step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    [ok] %s\n' "$*"; }
warn() { printf '    [!!] %s\n' "$*" >&2; WARNINGS=$((WARNINGS + 1)); }
die()  { printf '\n[错误] %s\n' "$*" >&2; exit 1; }
WARNINGS=0

usage() {
    cat <<EOF
用法：sudo sh $0 --domain 域名 --email 邮箱 [选项]        部署（幂等，可重复运行）
      sudo sh $0 --check [--domain 域名]                  只做检查，不改任何东西
      sudo sh $0 --uninstall [--domain 域名]              回滚：停用配置（证书保留）
      sh $0 --render 目录 --domain 域名 [--flavor ...]    只渲染配置文件到目录（不需要 root）

部署参数（也可以写在脚本顶部"改这里"那一段；不给时用上次部署保存在 $SAVED_ENV 的值）：
  --domain DOMAIN        对外域名，例如 cast.example.com（要有一条 A 记录指向本机公网 IP）
  --email EMAIL          Let's Encrypt 账号邮箱（部署时必填）
  --flavor nginx|1panel  nginx = 系统自带 nginx + certbot（默认）；1panel = 1Panel 的 OpenResty 容器
  --tunnel-port PORT     服务器回环地址上的反向转发端口（默认 18766），须与电脑上 tunnel.sh -r 一致
  --tunnel-addr ADDR     nginx 连隧道端口用的地址（默认 127.0.0.1）
  --conf-dir DIR         conf.d 目录（默认 nginx：/etc/nginx/conf.d；1panel：<openresty-root>/conf/conf.d）
  --openresty-root DIR   仅 1panel：1Panel OpenResty 安装目录（默认 /opt/1panel/apps/openresty/openresty）
  --container NAME       仅 1panel：OpenResty 容器名（默认自动找 1Panel-openresty*）
  --path-prefix /PATH    渲染方案 B 片段用的路径前缀（默认 /cast）

其他选项：
  --check                只做检查（DNS、nginx、certbot、端口、sshd 转发设置、现有部署状态），不改任何东西
  --cert-dry-run         只演练签证书（certbot --dry-run，走 staging，不保存证书），之后退出
  --public-ip IP         手动指定本机公网 IP（不给时用 https://api.ipify.org 探测；NAT 环境探测可能不准）
  --sshd-keepalive       安装 /etc/ssh/sshd_config.d/60-glasscast-keepalive.conf（ClientAliveInterval 15），
                         让 sshd 约 45 秒内回收断线隧道占着的端口，tunnel.sh 才能尽快重连
  --uninstall            回滚：把本域名的配置、续期任务、sshd drop-in 移到 $DEPLOY_ROOT/disabled/，
                         reload nginx；证书保留
  --render DIR           把 deploy/nginx/ 的模板按上面的参数渲染到 DIR（手工部署、方案 B、1Panel 面板建站用）
  -h, --help             显示本帮助
EOF
}

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
ORIG_ARGS="$*"
MODE="install"
RENDER_OUT=""
PUBLIC_IP_OVERRIDE=""
CERT_DRY_RUN=0
SSHD_KEEPALIVE=0

need_arg() { [ "$2" -ge 2 ] || die "$1 需要一个参数"; }

while [ $# -gt 0 ]; do
    case $1 in
        --*=*)  # --opt=value 拆成 --opt value
            opt=${1%%=*}; val=${1#*=}; shift; set -- "$opt" "$val" "$@"; continue ;;
    esac
    case $1 in
        --domain) need_arg "$1" $#; DOMAIN=$2; shift 2 ;;
        --email) need_arg "$1" $#; EMAIL=$2; shift 2 ;;
        --flavor) need_arg "$1" $#; FLAVOR=$2; shift 2 ;;
        --tunnel-port) need_arg "$1" $#; TUNNEL_PORT=$2; shift 2 ;;
        --tunnel-addr) need_arg "$1" $#; TUNNEL_ADDR=$2; shift 2 ;;
        --conf-dir) need_arg "$1" $#; NGINX_CONF_DIR=${2%/}; shift 2 ;;
        --openresty-root) need_arg "$1" $#; OPENRESTY_ROOT=${2%/}; shift 2 ;;
        --container) need_arg "$1" $#; OPENRESTY_CONTAINER=$2; shift 2 ;;
        --path-prefix) need_arg "$1" $#; PATH_PREFIX=${2%/}; shift 2 ;;
        --public-ip) need_arg "$1" $#; PUBLIC_IP_OVERRIDE=$2; shift 2 ;;
        --render) need_arg "$1" $#; MODE="render"; RENDER_OUT=$2; shift 2 ;;
        --check) MODE="check"; shift ;;
        --uninstall) MODE="uninstall"; shift ;;
        --cert-dry-run) CERT_DRY_RUN=1; shift ;;
        --sshd-keepalive) SSHD_KEEPALIVE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "未知参数：$1" ;;
    esac
done

# ---------------------------------------------------------------------------
# 通用函数
# ---------------------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }
matches() { printf '%s' "$2" | grep -Eq "$1"; }   # $1 = 扩展正则，$2 = 值

is_ipv4() {
    matches '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' "$1"
}

is_port() {
    case $1 in ''|*[!0-9]*) return 1 ;; esac
    [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

# 上次部署保存的参数：只填"命令行和改这里都没给"的项。命令行给了不同的域名时整份不用（那是另一个部署）。
load_saved() {
    [ -f "$SAVED_ENV" ] || return 0
    saved_domain=$(sed -n 's/^DOMAIN=//p' "$SAVED_ENV" | head -n 1)
    if [ -n "$DOMAIN" ] && [ "$DOMAIN" != "$saved_domain" ]; then
        info "（$SAVED_ENV 记录的是 ${saved_domain:-别的部署}，与 --domain $DOMAIN 不同，不使用其中的参数）"
        return 0
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        case $line in ''|'#'*) continue ;; esac
        k=${line%%=*}
        v=${line#*=}
        case $k in
            DOMAIN|EMAIL|FLAVOR|TUNNEL_PORT|TUNNEL_ADDR|NGINX_CONF_DIR|OPENRESTY_ROOT|OPENRESTY_CONTAINER)
                eval "cur=\${$k}"
                if [ -z "$cur" ]; then
                    eval "$k=\$v"
                fi
                ;;
        esac
    done < "$SAVED_ENV"
    info "（未给出的参数沿用 $SAVED_ENV）"
}

apply_defaults() {
    FLAVOR=${FLAVOR:-nginx}
    TUNNEL_PORT=${TUNNEL_PORT:-18766}
    TUNNEL_ADDR=${TUNNEL_ADDR:-127.0.0.1}
    PATH_PREFIX=${PATH_PREFIX:-/cast}
    OPENRESTY_ROOT=${OPENRESTY_ROOT:-/opt/1panel/apps/openresty/openresty}
    DOMAIN=$(printf '%s' "$DOMAIN" | tr 'A-Z' 'a-z')
}

is_placeholder_domain() {
    case $1 in
        example.com|*.example.com|example.net|*.example.net|example.org|*.example.org) return 0 ;;
    esac
    return 1
}

# 这些值会被 sed 写进 nginx 配置、写进 deploy.env，所以只接受安全字符。
validate() {
    case $FLAVOR in
        nginx|1panel) ;;
        *) die "--flavor 只能是 nginx 或 1panel：$FLAVOR" ;;
    esac
    [ -n "$DOMAIN" ] || { usage >&2; die "缺少 --domain（对外域名，例如 cast.example.com）"; }
    matches '^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*[a-z0-9]$' "$DOMAIN" \
        || die "--domain 不像一个域名：$DOMAIN"
    is_port "$TUNNEL_PORT" || die "--tunnel-port 不合法：$TUNNEL_PORT"
    [ "$TUNNEL_PORT" -ge 1024 ] || die "--tunnel-port 请用 1024 以上的端口（普通用户的 ssh -R 绑不了特权端口）：$TUNNEL_PORT"
    is_ipv4 "$TUNNEL_ADDR" || die "--tunnel-addr 必须是 IPv4 地址：$TUNNEL_ADDR"
    [ "$TUNNEL_ADDR" != "0.0.0.0" ] || die "--tunnel-addr 不能是 0.0.0.0（隧道端口是明文，只能在回环或 docker 网桥地址上）"
    matches '^(/[A-Za-z0-9._-]+)+$' "$PATH_PREFIX" || die "--path-prefix 应形如 /cast：$PATH_PREFIX"
    for p in "$NGINX_CONF_DIR" "$OPENRESTY_ROOT"; do
        [ -z "$p" ] || matches '^/[A-Za-z0-9._/-]*$' "$p" || die "目录只能用字母、数字和 . _ / -：$p"
    done
    [ -z "$OPENRESTY_CONTAINER" ] || matches '^[A-Za-z0-9][A-Za-z0-9._-]*$' "$OPENRESTY_CONTAINER" \
        || die "--container 不合法：$OPENRESTY_CONTAINER"
    if [ -n "$EMAIL" ]; then
        matches '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$' "$EMAIL" || die "--email 看起来不是邮箱：$EMAIL"
    fi
}

# 各变体的路径。*_HOST_* 是宿主机上的路径，其余（SSL_CERT、ERROR_LOG、ACME_NGINX_DIR）是 nginx 进程看到的路径
# ——1Panel 的 OpenResty 在容器里，两者不同。
setup_paths() {
    HTTP_CONF_NAME="$DOMAIN.http.conf"
    HTTPS_CONF_NAME="$DOMAIN.https.conf"
    if [ "$FLAVOR" = "1panel" ]; then
        OR_HOST_ROOT="$PREFIX$OPENRESTY_ROOT"
        if [ -n "$NGINX_CONF_DIR" ]; then CONF_D="$PREFIX$NGINX_CONF_DIR"; else CONF_D="$OR_HOST_ROOT/conf/conf.d"; fi
        SITE_DIR="$OR_HOST_ROOT/www/sites/$DOMAIN"
        ACME_HOST_DIR="$OR_HOST_ROOT/root"          # 1Panel 把它挂成容器里的 /usr/share/nginx/html
        ACME_NGINX_DIR="/usr/share/nginx/html"
        CERTBOT_WEBROOT="/var/www/acme"             # certbot 容器里看到的 webroot
        LE_DIR="$DEPLOY_ROOT/letsencrypt"           # certbot 容器的 /etc/letsencrypt
        SSL_CERT="/www/sites/$DOMAIN/ssl/fullchain.pem"
        SSL_KEY="/www/sites/$DOMAIN/ssl/privkey.pem"
        HOST_SSL_CERT="$SITE_DIR/ssl/fullchain.pem"
        HOST_SSL_KEY="$SITE_DIR/ssl/privkey.pem"
        LOG_HOST_DIR="$SITE_DIR/log"
        ERROR_LOG="/www/sites/$DOMAIN/log/error.log"
        HOOK_FILE=""
        NGINX_LABEL="OpenResty"
    else
        if [ -n "$NGINX_CONF_DIR" ]; then CONF_D="$PREFIX$NGINX_CONF_DIR"; else CONF_D="$PREFIX/etc/nginx/conf.d"; fi
        SITE_DIR=""
        ACME_HOST_DIR="$PREFIX/var/www/glasscast-acme"
        ACME_NGINX_DIR="/var/www/glasscast-acme"
        CERTBOT_WEBROOT="$ACME_HOST_DIR"
        LE_DIR="$PREFIX/etc/letsencrypt"
        SSL_CERT="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
        SSL_KEY="/etc/letsencrypt/live/$DOMAIN/privkey.pem"
        HOST_SSL_CERT="$LE_DIR/live/$DOMAIN/fullchain.pem"
        HOST_SSL_KEY="$LE_DIR/live/$DOMAIN/privkey.pem"
        LOG_HOST_DIR="$PREFIX/var/log/nginx"
        ERROR_LOG="/var/log/nginx/glasscast-error.log"
        HOOK_FILE="$LE_DIR/renewal-hooks/deploy/glasscast-reload-nginx.sh"
        NGINX_LABEL="nginx"
    fi
    LIVE_DIR="$LE_DIR/live/$DOMAIN"
}

# 在 nginx 所在的地方跑 nginx 命令（1panel：容器里的 openresty）。
ngx() {
    if [ "$FLAVOR" = "1panel" ]; then
        docker exec "$OPENRESTY_CONTAINER" openresty "$@"
    else
        nginx "$@"
    fi
}

# nginx -t；失败时打印 nginx 的报错原文。
ngx_test() {
    if out=$(ngx -t 2>&1); then
        info "$NGINX_LABEL -t 通过"
        return 0
    fi
    printf '%s\n' "$out" | sed 's/^/      | /' >&2
    return 1
}

ngx_reload() {
    if [ "$FLAVOR" = "nginx" ] && have systemctl && systemctl is-active --quiet nginx 2>/dev/null; then
        systemctl reload nginx
    else
        ngx -s reload
    fi
    # reload 成功就加载了磁盘上的全部配置和证书，renew-cert.sh 留下的待 reload 标记随之作废
    rm -f "$RELOAD_MARKER"
    ok "$NGINX_LABEL reload 完成"
}

backup_file() {  # $1 = 要备份的文件
    mkdir -p "$BACKUP_DIR"
    cp -p "$1" "$BACKUP_DIR/$(basename "$1")"
    info "已备份旧版本 -> $BACKUP_DIR/$(basename "$1")"
}

# 把模板渲染成配置文件。值在 validate() 里校验过，不含 sed 的特殊字符 | & \。
render_one() {  # $1 = 模板文件名（deploy/nginx/ 下），$2 = 输出文件
    sed -e "s|__DOMAIN__|$DOMAIN|g" \
        -e "s|__TUNNEL_ADDR__|$TUNNEL_ADDR|g" \
        -e "s|__TUNNEL_PORT__|$TUNNEL_PORT|g" \
        -e "s|__ACME_ROOT__|$ACME_NGINX_DIR|g" \
        -e "s|__SSL_CERT__|$SSL_CERT|g" \
        -e "s|__SSL_KEY__|$SSL_KEY|g" \
        -e "s|__ERROR_LOG__|$ERROR_LOG|g" \
        -e "s|__PATH_PREFIX__|$PATH_PREFIX|g" \
        "$TEMPLATE_DIR/$1" > "$2"
    if grep -n '__[A-Z][A-Z_]*__' "$2" >&2; then
        die "$2 里还有没替换的占位符（见上）"
    fi
}

render_all() {  # $1 = 输出目录
    mkdir -p "$1"
    render_one glasscast-upstream.conf.template "$1/$UPSTREAM_CONF_NAME"
    render_one glasscast-http.conf.template "$1/$HTTP_CONF_NAME"
    render_one glasscast-https.conf.template "$1/$HTTPS_CONF_NAME"
    render_one glasscast-path-prefix.locations.template "$1/$LOCATIONS_INC_NAME"
}

# 安装一个配置文件到 conf.d 并立即 nginx -t；失败就还原（或移除新文件）后退出。
# 结果写在全局 VHOST_CHANGED（1 = 这次换了文件且已通过 -t；0 = 文件本来就一致，没有跑 -t）。
# reload 由调用方无条件执行（见文件头"安全约定"）。
# 注意：不要把本函数放进 if/&&/|| 条件里调用——POSIX sh 在条件上下文里会忽略函数体内的 set -e。
install_vhost() {  # $1 = 源文件, $2 = 目标路径
    src=$1
    dst=$2
    VHOST_CHANGED=0
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        ok "已是最新：$dst"
        return 0
    fi
    had_old=0
    if [ -f "$dst" ]; then
        backup_file "$dst"
        had_old=1
    fi
    install -m 0644 "$src" "$dst"
    info "已安装 $dst"
    if ! ngx_test; then
        if [ "$had_old" -eq 1 ]; then
            cp -p "$BACKUP_DIR/$(basename "$dst")" "$dst"
            info "已还原旧版本 $dst"
        else
            rm -f "$dst"
            info "已移除刚装的 $dst"
        fi
        die "$NGINX_LABEL -t 未通过（见上方输出），conf.d 已恢复原状"
    fi
    VHOST_CHANGED=1
}

resolve_a() {  # $1 = 域名，[$2 = DNS 服务器]；每行一个 IPv4
    if have dig; then
        if [ $# -ge 2 ]; then
            dig +short +time=3 +tries=2 A "$1" "@$2" 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+){3}$' || true
        else
            dig +short +time=3 +tries=2 A "$1" 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+){3}$' || true
        fi
    elif [ $# -lt 2 ] && have getent; then
        getent ahostsv4 "$1" 2>/dev/null | awk '{print $1}' | sort -u || true
    fi
}

detect_public_ip() {
    addr=""
    if have curl; then
        addr=$(curl -s -4 --max-time 5 https://api.ipify.org 2>/dev/null || true)
    fi
    if ! is_ipv4 "$addr" && have ip; then
        # 云主机常见 NAT：这里拿到的可能是内网地址，此时请用 --public-ip
        addr=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}' || true)
    fi
    printf '%s' "$addr"
}

# 域名所在 zone 的一个权威 NS（从域名本身往上找第一个有 NS 记录的名字）；找不到就不输出。
find_zone_ns() {
    name=$DOMAIN
    while :; do
        ns=$(dig +short +time=3 +tries=1 NS "$name" 2>/dev/null | head -n 1 | sed 's/\.$//' || true)
        if [ -n "$ns" ]; then
            printf '%s' "$ns"
            return 0
        fi
        case $name in
            *.*.*) name=${name#*.} ;;
            *) return 0 ;;
        esac
    done
}

cert_valid_30d() {  # $1 = pem；30 天内不过期且包含本域名 -> 0
    [ -s "$1" ] || return 1
    have openssl || return 1
    openssl x509 -in "$1" -noout -checkend 2592000 >/dev/null 2>&1 || return 1
    openssl x509 -in "$1" -noout -text 2>/dev/null | grep -Fq "DNS:$DOMAIN"
}

cert_summary() {  # $1 = pem
    if have openssl && [ -s "$1" ]; then
        openssl x509 -in "$1" -noout -subject -issuer -enddate 2>/dev/null | sed 's/^/      /'
    fi
}

# ---------------------------------------------------------------------------
# 检查（--check 和部署共用）
# ---------------------------------------------------------------------------
resolve_container() {
    if [ -n "$OPENRESTY_CONTAINER" ]; then
        # 不用 docker ps --filter name=^...$：老版本 docker 的容器名带前导 /，正则精确匹配会落空
        docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$OPENRESTY_CONTAINER" \
            || die "OpenResty 容器 $OPENRESTY_CONTAINER 没有在运行（docker ps 里找不到）。用 --container 指定正确的名字。"
    else
        found=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^1Panel-openresty' || true)
        count=$(printf '%s\n' "$found" | grep -c . || true)
        [ "$count" -eq 1 ] || die "按名字 1Panel-openresty* 找到 $count 个运行中的容器，请用 --container 指定（docker ps 查看）"
        OPENRESTY_CONTAINER=$found
    fi
    ok "OpenResty 容器 $OPENRESTY_CONTAINER 运行中"
}

check_prereq() {
    step "0. 前置检查（变体：$FLAVOR，域名：$DOMAIN）"
    if [ -z "$PREFIX" ] && [ "$(id -u)" -ne 0 ]; then
        die "请用 root 运行：sudo sh $0 $ORIG_ARGS"
    fi
    for f in $PACKAGE_FILES; do
        [ -f "$SCRIPT_DIR/$f" ] || die "缺少文件 $SCRIPT_DIR/$f（请把整个 deploy/ 目录一起上传）"
    done
    ok "部署文件齐全（$SCRIPT_DIR）"
    for c in curl install cmp sed grep; do
        have "$c" || die "缺少命令 $c"
    done
    if [ "$FLAVOR" = "1panel" ]; then
        have docker || die "缺少 docker（1panel 变体需要）"
        resolve_container
        netmode=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$OPENRESTY_CONTAINER" 2>/dev/null || true)
        if [ "$netmode" = "host" ]; then
            ok "容器使用 host 网络（配置里的 $TUNNEL_ADDR:$TUNNEL_PORT 就是宿主机上的隧道端口）"
        elif [ "$TUNNEL_ADDR" = "127.0.0.1" ]; then
            # 不要建议 0.0.0.0：那会把明文、绕过 TLS 和 nginx 限制的隧道端口开到所有网卡上
            warn "容器网络模式是 '$netmode' 而不是 host：容器里的 127.0.0.1:$TUNNEL_PORT 指向容器自身，连不到 sshd 的反向转发端口。首选把 OpenResty 容器改回 host 网络；实在不行，只把隧道绑到 docker 网桥网关（不要绑 0.0.0.0）：网关地址用 docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 查（通常 172.17.0.1），本脚本加 --tunnel-addr <网关IP>，tunnel.sh 加 --remote-bind <网关IP>，sshd 开 GatewayPorts clientspecified。"
        else
            info "容器网络模式 '$netmode'，隧道地址 $TUNNEL_ADDR（tunnel.sh 要用 --remote-bind $TUNNEL_ADDR）"
        fi
    else
        have nginx || die "找不到 nginx。Debian/Ubuntu：apt install nginx；1Panel 用户加 --flavor 1panel"
        ok "$(nginx -v 2>&1 | head -n 1)"
        if have systemctl && systemctl is-active --quiet nginx 2>/dev/null; then
            ok "nginx 正在运行（systemd）"
        elif have pgrep && pgrep -x nginx >/dev/null 2>&1; then
            ok "nginx 正在运行"
        else
            die "nginx 没在运行。先启动：systemctl enable --now nginx"
        fi
        if have certbot; then
            ok "$(certbot --version 2>&1 | head -n 1)"
        elif [ "$MODE" = "install" ]; then
            die "找不到 certbot。Debian/Ubuntu：apt install certbot（或 snap install --classic certbot）"
        else
            warn "找不到 certbot，部署前先安装：apt install certbot"
        fi
    fi
    [ -d "$CONF_D" ] || die "找不到配置目录 $CONF_D（nginx 装在别处？用 --conf-dir 指定）"
    ok "配置目录 $CONF_D"
    if [ "$FLAVOR" = "nginx" ]; then
        conf_glob="${CONF_D#"$PREFIX"}/*.conf"
        if ngx -T 2>/dev/null | grep -Fq "$conf_glob"; then
            ok "nginx 主配置会加载 $conf_glob"
        else
            warn "nginx -T 的输出里没看到 include $conf_glob：放进去的文件可能不生效（检查 /etc/nginx/nginx.conf 的 http 块）"
        fi
    fi
    others=""
    for f in "$CONF_D"/*.conf "$PREFIX/etc/nginx/sites-enabled"/*; do
        [ -f "$f" ] || continue
        case $(basename "$f") in
            "$HTTP_CONF_NAME"|"$HTTPS_CONF_NAME") continue ;;
        esac
        if grep -q -F "$DOMAIN" "$f" 2>/dev/null; then
            others="$others $f"
        fi
    done
    if [ -n "$others" ]; then
        warn "以下配置也引用了 $DOMAIN，可能有重复的 server_name（例如在 1Panel 面板里建过同名网站）：$others"
    fi
}

check_dns() {
    step "1. DNS 校验：$DOMAIN 必须解析到本机公网 IP"
    if [ -n "$PUBLIC_IP_OVERRIDE" ]; then
        is_ipv4 "$PUBLIC_IP_OVERRIDE" || die "--public-ip 不是合法的 IPv4：$PUBLIC_IP_OVERRIDE"
        public_ip=$PUBLIC_IP_OVERRIDE
        info "本机公网 IP（--public-ip 指定）：$public_ip"
    else
        public_ip=$(detect_public_ip)
        if is_ipv4 "$public_ip"; then
            info "本机公网 IP（api.ipify.org / ip route 探测）：$public_ip"
        else
            die "探测不到本机公网 IP，请加 --public-ip <本机公网IP> 重试"
        fi
    fi
    resolved=$(resolve_a "$DOMAIN" | sort -u | tr '\n' ' ' | sed 's/ *$//')
    info "$DOMAIN 当前解析：${resolved:-（无 A 记录）}"
    dns_ok=1
    [ -n "$resolved" ] || dns_ok=0
    for r in $resolved; do
        [ "$r" = "$public_ip" ] || dns_ok=0
    done
    if [ "$dns_ok" -ne 1 ]; then
        cat >&2 <<EOF

[DNS 未就绪] $DOMAIN 解析为 "${resolved:-无}"，本机公网 IP 为 $public_ip。
这一步需要人工操作（本脚本不会改 DNS）：
  1. 打开域名的 DNS 服务商控制台（DNSPod、阿里云解析、Cloudflare 等），给 $DOMAIN 添加一条 A 记录
     （已有同名记录就修改它）：
       主机记录：域名去掉主域名的部分（例如 cast.example.com 在 example.com 下就填 cast）
       记录类型：A    记录值：$public_ip    TTL：600 或默认
     用 Cloudflare 时把这条记录设成"仅 DNS"（灰色云），不要走 Cloudflare 代理。
  2. 等待生效：在任意机器执行  dig +short $DOMAIN  返回 $public_ip
  3. 重新运行本脚本。
若自动探测的公网 IP 不准（NAT 环境），加 --public-ip <真实公网IP>。
EOF
        return 2
    fi
    ok "DNS 已指向本机（$public_ip）"
    if have dig; then
        aaaa=$(dig +short +time=3 +tries=1 AAAA "$DOMAIN" 2>/dev/null | grep ':' || true)
        if [ -n "$aaaa" ]; then
            warn "$DOMAIN 还有 AAAA（IPv6）记录 $aaaa：本配置只监听 IPv4，手机走 IPv6 时会连不上。删掉 AAAA 记录，或自己给两个 vhost 加 listen [::]:80 / listen [::]:443 ssl http2"
        fi
        # 额外看一眼权威 DNS（Let's Encrypt 从权威服务器取记录），不一致只告警。
        ns=$(find_zone_ns)
        if [ -n "$ns" ]; then
            auth=$(resolve_a "$DOMAIN" "$ns" | sort -u | tr '\n' ' ' | sed 's/ *$//')
            if [ "$auth" = "$public_ip" ]; then
                ok "权威 DNS $ns 也返回 $auth"
            else
                warn "权威 DNS $ns 返回 '${auth:-无}'，与 $public_ip 不一致；签证书可能失败，等记录完全生效再试"
            fi
        fi
    fi
}

check_sshd() {
    step "sshd 反向转发设置（电脑上的 tunnel.sh 依赖）"
    if have sshd || [ -x /usr/sbin/sshd ]; then
        sshd_bin=$(command -v sshd 2>/dev/null || echo /usr/sbin/sshd)
        settings=$("$sshd_bin" -T 2>/dev/null | grep -E '^(allowtcpforwarding|gatewayports|clientaliveinterval|clientalivecountmax) ' || true)
        if [ -n "$settings" ]; then
            printf '%s\n' "$settings" | sed 's/^/      /'
            if printf '%s\n' "$settings" | grep -Eq '^allowtcpforwarding (no|local)$'; then
                warn "sshd 禁止了远程端口转发（AllowTcpForwarding），反向隧道建不起来"
            fi
            if printf '%s\n' "$settings" | grep -Eq '^clientaliveinterval 0$'; then
                info "提示：ClientAliveInterval=0，电脑异常断线后旧会话可能长时间占着 $TUNNEL_PORT；可加 --sshd-keepalive"
            fi
        else
            info "（sshd -T 无输出，跳过）"
        fi
    else
        info "（找不到 sshd，跳过）"
    fi
    if have ss; then
        listener=$(ss -ltnpH "sport = :$TUNNEL_PORT" 2>/dev/null || true)
        if [ -n "$listener" ]; then
            info "端口 $TUNNEL_PORT 正在监听："
            printf '%s\n' "$listener" | sed 's/^/      /'
            if ! printf '%s\n' "$listener" | grep -q 'sshd'; then
                warn "$TUNNEL_PORT 被非 sshd 进程占用，反向隧道会失败（换一个 --tunnel-port，tunnel.sh -r 同步改）"
            fi
        else
            info "端口 $TUNNEL_PORT 当前没有监听（电脑上的 tunnel.sh 尚未连接）"
        fi
    fi
}

show_state() {
    step "当前部署状态"
    tmp_render=$(mktemp -d "${TMPDIR:-/tmp}/glasscast-render.XXXXXX")
    render_all "$tmp_render"
    for n in "$UPSTREAM_CONF_NAME" "$HTTP_CONF_NAME" "$HTTPS_CONF_NAME"; do
        if [ -f "$CONF_D/$n" ]; then
            if cmp -s "$tmp_render/$n" "$CONF_D/$n"; then
                info "$n：已安装（与本次参数渲染的版本一致）"
            else
                info "$n：已安装（与本次参数渲染的版本不同，部署时会备份后覆盖）"
            fi
        else
            info "$n：未安装"
        fi
    done
    rm -rf "$tmp_render"
    if [ -s "$HOST_SSL_CERT" ]; then
        info "站点证书："
        cert_summary "$HOST_SSL_CERT"
    else
        info "站点证书：无"
    fi
    if [ "$FLAVOR" = "1panel" ]; then
        if have systemctl; then
            state=$(systemctl is-enabled "$UNIT_TIMER" 2>/dev/null || true)
            info "续期 timer：${state:-未安装}"
        fi
    else
        if [ -f "$HOOK_FILE" ]; then info "续期后 reload 的 deploy hook：已安装"; else info "续期后 reload 的 deploy hook：未安装"; fi
    fi
}

# ---------------------------------------------------------------------------
# 部署步骤
# ---------------------------------------------------------------------------
save_env() {
    tmp="$SAVED_ENV.tmp"
    {
        printf '# 由 deploy/server-setup.sh 生成（%s）：上一次成功部署的参数。\n' "$(date '+%Y-%m-%d %H:%M:%S')"
        printf '# 重跑 server-setup.sh（--check / --uninstall / 部署）和 renew-cert.sh 会读它。只能是 KEY=值，不要加引号。\n'
        printf 'FLAVOR=%s\n' "$FLAVOR"
        printf 'DOMAIN=%s\n' "$DOMAIN"
        printf 'EMAIL=%s\n' "$EMAIL"
        printf 'TUNNEL_PORT=%s\n' "$TUNNEL_PORT"
        printf 'TUNNEL_ADDR=%s\n' "$TUNNEL_ADDR"
        printf 'NGINX_CONF_DIR=%s\n' "$NGINX_CONF_DIR"
        if [ "$FLAVOR" = "1panel" ]; then
            printf 'OPENRESTY_ROOT=%s\n' "$OPENRESTY_ROOT"
            printf 'OPENRESTY_CONTAINER=%s\n' "$OPENRESTY_CONTAINER"
        fi
    } > "$tmp"
    chmod 0600 "$tmp"
    mv "$tmp" "$SAVED_ENV"
    info "参数已保存到 $SAVED_ENV"
}

make_dirs() {
    step "2. 创建目录、渲染配置"
    for d in "$DEPLOY_ROOT" "$ACME_HOST_DIR/.well-known/acme-challenge"; do
        if [ -d "$d" ]; then ok "已存在 $d"; else mkdir -p "$d"; ok "已创建 $d"; fi
    done
    if [ "$FLAVOR" = "1panel" ]; then
        for d in "$LE_DIR" "$SITE_DIR/log" "$SITE_DIR/ssl"; do
            if [ -d "$d" ]; then ok "已存在 $d"; else mkdir -p "$d"; ok "已创建 $d"; fi
        done
        chmod 0700 "$LE_DIR"
        # error_log 在隧道断开（502/504）时会记下请求行，里面有图片地址的 key；nginx 建日志文件默认 0644，
        # 所以收紧目录。OpenResty master 以 root 打开日志、worker 继承文件描述符，写日志不受影响。
        chmod 0700 "$SITE_DIR/log"
    else
        mkdir -p "$LOG_HOST_DIR"
        log_file="$PREFIX$ERROR_LOG"
        if [ ! -e "$log_file" ]; then
            # 预先建好、只给 root 和 adm 组读：error_log 里可能有图片地址的 key（nginx 自己建的文件是 0644）
            : > "$log_file"
            chmod 0640 "$log_file"
            if have getent && getent group adm >/dev/null 2>&1; then chgrp adm "$log_file" 2>/dev/null || true; fi
            ok "已创建 $log_file（0640）"
        fi
    fi
    render_all "$RENDERED_DIR"
    ok "配置已渲染到 $RENDERED_DIR/（方案 B 的片段 $LOCATIONS_INC_NAME 也在这里）"
    # 留一份完整部署包：上传目录被清掉之后也能就地 --check / 重跑 / 回滚
    pkg="$DEPLOY_ROOT/deploy"
    if [ "$SCRIPT_DIR" != "$pkg" ]; then
        mkdir -p "$pkg/nginx"
        for f in $PACKAGE_FILES; do
            install -m 0644 "$SCRIPT_DIR/$f" "$pkg/$f"
        done
        chmod 0755 "$pkg/server-setup.sh" "$pkg/renew-cert.sh"
        info "部署包副本已放到 $pkg/（之后可以 sudo sh $pkg/server-setup.sh --check）"
    fi
}

# 若上次中途失败留下了 https vhost 却没有证书，先把它移走，否则 nginx -t 会因为找不到证书文件而失败。
guard_orphan_https() {
    if [ -f "$CONF_D/$HTTPS_CONF_NAME" ] && { [ ! -s "$HOST_SSL_CERT" ] || [ ! -s "$HOST_SSL_KEY" ]; }; then
        warn "发现已安装的 $HTTPS_CONF_NAME 但证书缺失，先移到 $BACKUP_DIR/（证书签好后会重新安装）"
        mkdir -p "$BACKUP_DIR"
        mv "$CONF_D/$HTTPS_CONF_NAME" "$BACKUP_DIR/$HTTPS_CONF_NAME"
    fi
}

baseline_test() {
    step "3. 安装 upstream 和 80 端口 vhost（只放行 ACME 校验，其余 301 到 https）"
    info "先确认现有配置本身是好的（不在坏配置上叠加改动）"
    ngx_test || die "当前 $NGINX_LABEL 配置本身就没通过 -t，先修好再运行本脚本（本脚本未做任何配置改动）"
}

install_http() {
    install_vhost "$RENDERED_DIR/$UPSTREAM_CONF_NAME" "$CONF_D/$UPSTREAM_CONF_NAME"
    install_vhost "$RENDERED_DIR/$HTTP_CONF_NAME" "$CONF_D/$HTTP_CONF_NAME"
    # 无条件 reload：文件没变时配置就是 baseline_test 刚验过的那份，直接 reload 即可（见文件头"安全约定"）
    ngx_reload
}

check_acme_path() {
    step "4. 自测 ACME 校验路径"
    probe_name="glasscast-probe"
    probe_file="$ACME_HOST_DIR/.well-known/acme-challenge/$probe_name"
    token="glasscast-$STAMP"
    printf '%s' "$token" > "$probe_file"
    chmod 0644 "$probe_file"
    url="http://$DOMAIN/.well-known/acme-challenge/$probe_name"
    got=$(curl -s --max-time 5 --resolve "$DOMAIN:80:127.0.0.1" "$url" 2>/dev/null || true)
    if [ "$got" != "$token" ]; then
        rm -f "$probe_file"
        if [ "$FLAVOR" = "1panel" ]; then
            die "本机 http://127.0.0.1（Host: $DOMAIN）取不到探针文件。检查 OpenResty 容器是否把 $ACME_HOST_DIR 挂成 $ACME_NGINX_DIR：docker inspect -f '{{json .Mounts}}' $OPENRESTY_CONTAINER"
        fi
        die "本机 http://127.0.0.1（Host: $DOMAIN）取不到探针文件 $probe_file。检查 80 端口是不是这台 nginx 在监听、有没有别的 server 抢了 $DOMAIN（nginx -T | grep -n server_name）"
    fi
    ok "本机经 $NGINX_LABEL 能取到探针文件"
    got=$(curl -s --max-time 8 "$url" 2>/dev/null || true)
    if [ "$got" = "$token" ]; then
        ok "经公网地址 $url 能取到探针文件"
    else
        warn "经公网地址取不到探针文件（可能是云防火墙/安全组没放行 80，或本机不支持回环访问自己的公网 IP）。Let's Encrypt 需要从公网访问 80 端口。"
    fi
    rm -f "$probe_file"
}

issue_cert() {
    step "5. 签发证书（Let's Encrypt，webroot）"
    if [ "$CERT_DRY_RUN" -eq 0 ] && cert_valid_30d "$LIVE_DIR/fullchain.pem"; then
        ok "已有有效证书（30 天内不过期），跳过签发："
        cert_summary "$LIVE_DIR/fullchain.pem"
        return 0
    fi
    set -- certonly --webroot --webroot-path "$CERTBOT_WEBROOT" \
        -d "$DOMAIN" --cert-name "$DOMAIN" \
        --agree-tos -m "$EMAIL" --non-interactive --keep-until-expiring
    if [ "$CERT_DRY_RUN" -eq 1 ]; then
        set -- "$@" --dry-run
        info "演练模式：certbot --dry-run（Let's Encrypt staging，不保存证书）"
    fi
    fail_hint="certbot 签发失败。常见原因：DNS 未生效、80 端口被安全组拦截、短时间内失败太多次（Let's Encrypt 限流，等 1 小时）"
    if [ "$FLAVOR" = "1panel" ]; then
        info "docker run --rm $CERTBOT_IMAGE $*"
        docker run --rm \
            -v "$LE_DIR:/etc/letsencrypt" \
            -v "$ACME_HOST_DIR:/var/www/acme" \
            "$CERTBOT_IMAGE" "$@" \
            || die "$fail_hint"
    else
        if [ -n "$PREFIX" ]; then
            set -- "$@" --config-dir "$LE_DIR"
        fi
        info "certbot $*"
        certbot "$@" || die "$fail_hint"
    fi
    if [ "$CERT_DRY_RUN" -eq 1 ]; then
        ok "演练通过。去掉 --cert-dry-run 重新运行即可签正式证书。"
        exit 0
    fi
    [ -s "$LIVE_DIR/fullchain.pem" ] || die "certbot 结束了但找不到 $LIVE_DIR/fullchain.pem"
    ok "证书就绪："
    cert_summary "$LIVE_DIR/fullchain.pem"
}

copy_cert() {
    [ "$FLAVOR" = "1panel" ] || return 0
    step "6. 复制证书到 $SITE_DIR/ssl"
    if cmp -s "$LIVE_DIR/fullchain.pem" "$HOST_SSL_CERT" 2>/dev/null \
       && cmp -s "$LIVE_DIR/privkey.pem" "$HOST_SSL_KEY" 2>/dev/null; then
        ok "站点证书已是最新"
        return 0
    fi
    install -m 0644 "$LIVE_DIR/fullchain.pem" "$HOST_SSL_CERT"
    install -m 0600 "$LIVE_DIR/privkey.pem" "$HOST_SSL_KEY"
    ok "已复制 fullchain.pem（0644）和 privkey.pem（0600）"
}

install_https() {
    step "7. 安装 443 vhost（ssl + http2，/healthz、/s/、/mcp 反代到 $TUNNEL_ADDR:$TUNNEL_PORT，/admin/ 404）"
    install_vhost "$RENDERED_DIR/$HTTPS_CONF_NAME" "$CONF_D/$HTTPS_CONF_NAME"
    # 无条件 reload（见文件头"安全约定"）。vhost 没换时 install_vhost 没跑 -t，而上一步可能刚换了证书，
    # 所以先 -t 再 reload；-t 不过时 nginx 仍在用 reload 之前的配置，按报错修好后重跑即可。
    if [ "$VHOST_CHANGED" -eq 0 ]; then
        ngx_test || die "$NGINX_LABEL -t 未通过（见上方输出），$NGINX_LABEL 仍在使用 reload 之前的配置"
    fi
    ngx_reload
}

install_renewal_1panel() {
    step "8. 安装证书续期（$DEPLOY_ROOT/renew-cert.sh + $UNIT_TIMER）"
    install -m 0755 "$SCRIPT_DIR/renew-cert.sh" "$DEPLOY_ROOT/renew-cert.sh"
    ok "已安装 $DEPLOY_ROOT/renew-cert.sh（参数读 $SAVED_ENV）"
    mkdir -p "$SYSTEMD_DIR"
    units_changed=0
    for u in "$UNIT_SERVICE" "$UNIT_TIMER"; do
        if [ -f "$SYSTEMD_DIR/$u" ] && cmp -s "$SCRIPT_DIR/$u" "$SYSTEMD_DIR/$u"; then
            ok "已是最新：$SYSTEMD_DIR/$u"
        else
            install -m 0644 "$SCRIPT_DIR/$u" "$SYSTEMD_DIR/$u"
            ok "已安装 $SYSTEMD_DIR/$u"
            units_changed=1
        fi
    done
    if have systemctl; then
        if [ "$units_changed" -eq 1 ]; then
            systemctl daemon-reload
            info "systemctl daemon-reload"
        fi
        systemctl enable --now "$UNIT_TIMER"
        ok "$UNIT_TIMER 已启用"
        systemctl list-timers "$UNIT_TIMER" --no-pager 2>/dev/null | sed 's/^/      /' || true
    else
        warn "没有 systemctl，续期 timer 未启用；请自行用 cron 每周运行 $DEPLOY_ROOT/renew-cert.sh"
    fi
}

install_renewal_nginx() {
    step "8. 证书续期（certbot 自带的定时任务 + 续期后 reload nginx 的 deploy hook）"
    mkdir -p "$(dirname "$HOOK_FILE")"
    tmp=$(mktemp "${TMPDIR:-/tmp}/glasscast-hook.XXXXXX")
    cat > "$tmp" <<EOF
#!/bin/sh
# 由 glasscast deploy/server-setup.sh 安装：$DOMAIN 的证书续期后检查配置并 reload nginx。
# certbot renew 每续期成功一张证书就运行一次 renewal-hooks/deploy/ 下的脚本；不是本域名就什么都不做。
# 回滚：sudo sh /opt/glasscast/deploy/server-setup.sh --uninstall（会移走本文件）。
case " \${RENEWED_DOMAINS:-} " in
    *" $DOMAIN "*) ;;
    *) exit 0 ;;
esac
nginx -t -q && { systemctl reload nginx 2>/dev/null || nginx -s reload; }
EOF
    if [ -f "$HOOK_FILE" ] && cmp -s "$tmp" "$HOOK_FILE"; then
        rm -f "$tmp"
        ok "已是最新：$HOOK_FILE"
    else
        [ -f "$HOOK_FILE" ] && backup_file "$HOOK_FILE"
        install -m 0755 "$tmp" "$HOOK_FILE"
        rm -f "$tmp"
        ok "已安装 $HOOK_FILE"
    fi
    if have systemctl && { systemctl is-enabled certbot.timer >/dev/null 2>&1 \
                           || systemctl is-enabled snap.certbot.renew.timer >/dev/null 2>&1; }; then
        ok "certbot 的自动续期定时任务已启用（systemctl list-timers | grep certbot 查看）"
    elif [ -f "$PREFIX/etc/cron.d/certbot" ]; then
        ok "certbot 的自动续期由 /etc/cron.d/certbot 负责"
    else
        warn "没找到 certbot 的自动续期定时任务（certbot.timer / snap.certbot.renew.timer / /etc/cron.d/certbot）。请自己加一条每天运行的 cron：certbot renew -q"
    fi
    info "演练续期：certbot renew --dry-run --cert-name $DOMAIN"
}

install_sshd_keepalive() {
    step "9. sshd 保活 drop-in（--sshd-keepalive）"
    if ! grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' "$SSHD_CONFIG" 2>/dev/null; then
        warn "$SSHD_CONFIG 没有 Include /etc/ssh/sshd_config.d/*.conf，跳过（请手工在 sshd_config 里加 ClientAliveInterval 15 / ClientAliveCountMax 3）"
        return 0
    fi
    tmp=$(mktemp "${TMPDIR:-/tmp}/glasscast-sshd.XXXXXX")
    cat > "$tmp" <<'EOF'
# glasscast：电脑异常断线（合盖、换网）后，让 sshd 在约 45 秒内判定旧会话已死并释放
# 反向转发端口，deploy/tunnel.sh 才能重新绑定（否则 ExitOnForwardFailure 会一直失败）。
# 对其他 SSH 会话的影响：无响应的客户端约 45 秒后被断开。回滚：删除本文件后 systemctl reload ssh。
ClientAliveInterval 15
ClientAliveCountMax 3
EOF
    if [ -f "$SSHD_DROPIN" ] && cmp -s "$tmp" "$SSHD_DROPIN"; then
        rm -f "$tmp"
        ok "已是最新：$SSHD_DROPIN"
        return 0
    fi
    mkdir -p "$SSHD_DROPIN_DIR"
    [ -f "$SSHD_DROPIN" ] && backup_file "$SSHD_DROPIN"
    install -m 0644 "$tmp" "$SSHD_DROPIN"
    rm -f "$tmp"
    sshd_bin=$(command -v sshd 2>/dev/null || echo /usr/sbin/sshd)
    if ! "$sshd_bin" -t; then
        rm -f "$SSHD_DROPIN"
        die "sshd -t 未通过，已移除 $SSHD_DROPIN"
    fi
    systemctl reload ssh 2>/dev/null || systemctl reload sshd
    ok "已安装 $SSHD_DROPIN 并 reload sshd（现有会话不受影响）"
    "$sshd_bin" -T 2>/dev/null | grep -E '^clientalive' | sed 's/^/      /' || true
}

verify() {
    step "10. 验证"
    hdr=$(mktemp "${TMPDIR:-/tmp}/glasscast-hdr.XXXXXX")
    code=$(curl -s -o /dev/null -D "$hdr" -w '%{http_code} %{http_version}' --max-time 8 \
        --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/healthz" 2>/dev/null || true)
    status=${code%% *}
    httpver=${code#* }
    case $status in
        200) ok "https://$DOMAIN/healthz -> 200（隧道和电脑上的 glasscast 都在线）" ;;
        502|504) ok "https://$DOMAIN/healthz -> $status（vhost 正常；电脑上的 tunnel.sh / glasscast 还没连上，属预期）" ;;
        ''|000) warn "本机 https 请求失败（TLS 或连接错误），请看 $PREFIX$ERROR_LOG" ;;
        *) warn "https://$DOMAIN/healthz -> $status（非预期）" ;;
    esac
    [ -n "$httpver" ] && [ "$httpver" != "$code" ] && info "协商的 HTTP 版本：$httpver"
    if grep -qi '^alt-svc:' "$hdr" 2>/dev/null; then
        warn "响应里带了 Alt-Svc 头，平台可能尝试 HTTP/3 而失败：$(grep -i '^alt-svc:' "$hdr" | tr -d '\r')"
    else
        ok "响应不带 Alt-Svc（不会被引导到 HTTP/3）"
    fi
    if grep -qi '^strict-transport-security:' "$hdr" 2>/dev/null; then
        ok "安全头已生效（HSTS 等）"
    fi
    rm -f "$hdr"
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 --resolve "$DOMAIN:443:127.0.0.1" \
        "https://$DOMAIN/admin/config" 2>/dev/null || true)
    if [ "$code" = "404" ]; then
        ok "https://$DOMAIN/admin/config -> 404（管理接口不经公网转发）"
    else
        warn "https://$DOMAIN/admin/config 返回 '$code'，预期 404：检查 $HTTPS_CONF_NAME 里的 location ^~ /admin/"
    fi
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 --resolve "$DOMAIN:80:127.0.0.1" "http://$DOMAIN/healthz" 2>/dev/null || true)
    if [ "$code" = "301" ]; then
        ok "http -> https 301 跳转正常"
    else
        warn "http://$DOMAIN/healthz 返回 '$code'，预期 301"
    fi
}

summary() {
    step "完成"
    [ "$WARNINGS" -gt 0 ] && info "有 $WARNINGS 条告警，请往上翻看 [!!] 行。"
    cat <<EOF
    服务器侧就绪。接下来在电脑上（项目根目录，见 docs/deploy.md §2.4）：
      1. . ./.env && uv run glasscast serve --public-url https://$DOMAIN
      2. ./deploy/tunnel.sh -H <这台服务器的 SSH 地址> -u <SSH 用户> -r $TUNNEL_PORT --public-url https://$DOMAIN
      3. curl https://$DOMAIN/healthz   # 期望 ok
    方案 B（挂在已有站点路径下）用的片段：$RENDERED_DIR/$LOCATIONS_INC_NAME
    体检：sudo sh $DEPLOY_ROOT/deploy/server-setup.sh --check
    回滚：sudo sh $DEPLOY_ROOT/deploy/server-setup.sh --uninstall
EOF
}

# ---------------------------------------------------------------------------
# 回滚
# ---------------------------------------------------------------------------
uninstall() {
    step "回滚：停用 $DOMAIN（变体 $FLAVOR；文件只移走，不删除）"
    if [ -z "$PREFIX" ] && [ "$(id -u)" -ne 0 ]; then
        die "请用 root 运行：sudo sh $0 --uninstall"
    fi
    if [ "$FLAVOR" = "1panel" ]; then
        have docker || die "缺少 docker"
        resolve_container
    fi
    dis="$DEPLOY_ROOT/disabled/$STAMP"
    moved=""
    for n in "$HTTPS_CONF_NAME" "$HTTP_CONF_NAME" "$UPSTREAM_CONF_NAME"; do
        if [ -f "$CONF_D/$n" ]; then
            mkdir -p "$dis"
            mv "$CONF_D/$n" "$dis/$n"
            ok "已移走 $CONF_D/$n -> $dis/"
            moved="$moved $n"
        else
            info "$n 未安装，跳过"
        fi
    done
    if [ -n "$moved" ]; then
        if ngx_test; then
            ngx_reload
        else
            for n in $moved; do
                cp -p "$dis/$n" "$CONF_D/$n"
            done
            die "移走后 $NGINX_LABEL -t 反而失败（别的配置还在用 upstream glasscast_tunnel？例如方案 B 的 include），已放回原处"
        fi
    fi
    if [ "$FLAVOR" = "1panel" ]; then
        if have systemctl; then
            if systemctl disable --now "$UNIT_TIMER" 2>/dev/null; then ok "已停用 $UNIT_TIMER"; else info "$UNIT_TIMER 未启用"; fi
        fi
        for u in "$UNIT_SERVICE" "$UNIT_TIMER"; do
            if [ -f "$SYSTEMD_DIR/$u" ]; then
                mkdir -p "$dis"
                mv "$SYSTEMD_DIR/$u" "$dis/$u"
                ok "已移走 $SYSTEMD_DIR/$u"
            fi
        done
        if have systemctl; then systemctl daemon-reload || true; fi
    elif [ -f "$HOOK_FILE" ]; then
        mkdir -p "$dis"
        mv "$HOOK_FILE" "$dis/"
        ok "已移走 $HOOK_FILE"
    fi
    if [ -f "$SSHD_DROPIN" ]; then
        mkdir -p "$dis"
        mv "$SSHD_DROPIN" "$dis/"
        sshd_bin=$(command -v sshd 2>/dev/null || echo /usr/sbin/sshd)
        if "$sshd_bin" -t; then
            systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
            ok "已移走 sshd drop-in 并 reload sshd"
        else
            warn "移走 sshd drop-in 后 sshd -t 失败，请人工检查 /etc/ssh/sshd_config.d/"
        fi
    fi
    if [ "$FLAVOR" = "1panel" ]; then
        kept="证书与 certbot 账号：$LE_DIR/；站点目录：$SITE_DIR/"
        purge="删除 $DEPLOY_ROOT/ 和 $SITE_DIR/"
    else
        kept="证书：$LIVE_DIR/（certbot 仍会续期它）；ACME 目录：$ACME_HOST_DIR/"
        purge="certbot delete --cert-name $DOMAIN；删除 $DEPLOY_ROOT/ 和 $ACME_HOST_DIR/"
    fi
    cat <<EOF

    已停用。保留未动的内容（确认不再需要后可人工清理）：
      - $kept
      - 部署参数：$SAVED_ENV
      - 被移走的文件：${moved:+$dis/}
    彻底清理（人工）：$purge；到 DNS 服务商删除 $DOMAIN 的 A 记录；在千问平台下线 MCP 插件。
    恢复：sudo sh $DEPLOY_ROOT/deploy/server-setup.sh（沿用保存的参数）
EOF
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
DOMAIN=$(printf '%s' "$DOMAIN" | tr 'A-Z' 'a-z')
if [ "$MODE" != "render" ]; then
    load_saved
fi
apply_defaults
validate
setup_paths

case $MODE in
    render)
        [ -n "$RENDER_OUT" ] || die "--render 需要一个输出目录"
        if is_placeholder_domain "$DOMAIN"; then
            printf '提示：%s 是文档里的占位符，正式部署时换成你自己的域名。\n' "$DOMAIN" >&2
        fi
        render_all "$RENDER_OUT"
        printf '已按以下参数渲染（变体 %s）：域名 %s，隧道 %s:%s，方案 B 前缀 %s\n' \
            "$FLAVOR" "$DOMAIN" "$TUNNEL_ADDR" "$TUNNEL_PORT" "$PATH_PREFIX"
        for n in "$UPSTREAM_CONF_NAME" "$HTTP_CONF_NAME" "$HTTPS_CONF_NAME" "$LOCATIONS_INC_NAME"; do
            printf '  %s/%s\n' "${RENDER_OUT%/}" "$n"
        done
        printf '证书路径（%s 看到的）：%s\n手工安装步骤见 docs/deploy.md §2.7。\n' "$NGINX_LABEL" "$SSL_CERT"
        ;;
    check)
        if is_placeholder_domain "$DOMAIN"; then
            warn "$DOMAIN 是文档里的占位符，正式部署时用 --domain 换成你自己的域名"
        fi
        check_prereq
        check_dns || warn "DNS 未就绪（见上方指引）"
        check_sshd
        show_state
        step "检查当前 $NGINX_LABEL 配置"
        ngx_test || warn "当前配置未通过 -t"
        step "检查结束（$WARNINGS 条告警），未做任何改动"
        ;;
    install)
        is_placeholder_domain "$DOMAIN" && die "$DOMAIN 是文档里的占位符：用 --domain 换成你自己的域名"
        [ -n "$EMAIL" ] || { usage >&2; die "部署需要 --email（Let's Encrypt 账号邮箱）"; }
        case $EMAIL in
            *@example.com|*@example.net|*@example.org) die "$EMAIL 是文档里的占位符：用 --email 换成你自己的邮箱" ;;
        esac
        check_prereq
        check_dns
        make_dirs
        guard_orphan_https
        baseline_test
        install_http
        check_acme_path
        issue_cert
        copy_cert
        install_https
        save_env   # 证书和 443 vhost 都就绪才记下参数（renew-cert.sh、之后不带参数的重跑和回滚都读它）
        if [ "$FLAVOR" = "1panel" ]; then install_renewal_1panel; else install_renewal_nginx; fi
        if [ "$SSHD_KEEPALIVE" -eq 1 ]; then
            install_sshd_keepalive
        fi
        check_sshd
        verify
        summary
        ;;
    uninstall)
        uninstall
        ;;
esac
