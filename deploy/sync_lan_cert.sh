#!/usr/bin/env bash
# 把服务器上 <你的域名> 的证书和私钥复制到本机，供 tools/lan_https_proxy.py 使用（docs/deploy.md 方案 D：局域网直连）。
#
# 用法（先改下面"改这里"那一段，或者用环境变量 / 命令行参数）：
#   . ./.env && ./deploy/sync_lan_cert.sh                                    # 参数都在 .env 里
#   ./deploy/sync_lan_cert.sh -H server.example.com -u ubuntu -d cast.example.com
#   ./deploy/sync_lan_cert.sh --flavor 1panel -d cast.example.com            # 证书在 1Panel 的站点目录里
#   ./deploy/sync_lan_cert.sh --remote-dir /path/on/server                   # 证书在别的目录
#
# 前提：能免密 SSH 到服务器，并且那个用户能免密 sudo（ssh user@server 'sudo -n true' 不报错），
#       因为证书私钥只有 root 能读。不想这样的话，就自己把 fullchain.pem、privkey.pem 复制到 --dest 目录。
#
# Let's Encrypt 证书约 90 天一张，服务器每 60 天左右自动续期一次。续期后本机这份不会自己更新：
# 重跑本脚本，再重启 lan_https_proxy.py。到期日本脚本运行时会打印。
#
# 私钥会落到本机磁盘（目录 0700、文件 0600）。它和公网服务器用的是同一把私钥，丢了等于域名证书泄露。
#
# 兼容 macOS 自带的 bash 3.2。
set -euo pipefail

# ================================ 改这里 ================================
# 每一项都可以不改文件：用环境变量（例如写进 .env 后 `. ./.env`）或命令行参数覆盖，命令行优先。
HOST="${GLASSCAST_TUNNEL_HOST:-server.example.com}"   # -H  你的服务器（与 tunnel.sh 共用环境变量）
SSH_USER="${GLASSCAST_TUNNEL_USER:-ubuntu}"           # -u  SSH 用户
SSH_PORT="${GLASSCAST_TUNNEL_SSH_PORT:-22}"           # -p  SSH 端口
DOMAIN="${GLASSCAST_DOMAIN:-}"                        # -d  证书的域名；留空就取 GLASSCAST_PUBLIC_URL 的主机名
FLAVOR="${GLASSCAST_SERVER_FLAVOR:-nginx}"            # --flavor  nginx（certbot 默认目录）或 1panel（1Panel 站点目录）
REMOTE_DIR="${GLASSCAST_REMOTE_CERT_DIR:-}"           # --remote-dir  服务器上放证书的目录；留空按 flavor 推算
# 本机目录，须与 tools/lan_https_proxy.py 的 --tls-dir 一致（同一个环境变量 GLASSCAST_LAN_TLS_DIR）
DEST="${GLASSCAST_LAN_TLS_DIR:-}"                     # --dest
# ========================================================================

usage() {
    cat <<EOF
用法：$0 [选项]

  -H, --host HOST        SSH 服务器（默认 ${HOST}，环境变量 GLASSCAST_TUNNEL_HOST）
  -u, --user USER        SSH 用户（默认 ${SSH_USER}，环境变量 GLASSCAST_TUNNEL_USER）
  -p, --ssh-port PORT    SSH 端口（默认 ${SSH_PORT}，环境变量 GLASSCAST_TUNNEL_SSH_PORT）
  -d, --domain DOMAIN    证书的域名（默认取 GLASSCAST_PUBLIC_URL 的主机名，环境变量 GLASSCAST_DOMAIN）
      --flavor F         nginx：/etc/letsencrypt/live/<域名>（默认）
                         1panel：/opt/1panel/apps/openresty/openresty/www/sites/<域名>/ssl
      --remote-dir DIR   服务器上放 fullchain.pem、privkey.pem 的目录（给了就不看 --flavor）
      --dest DIR         本机目录（默认和 lan_https_proxy.py 一样，环境变量 GLASSCAST_LAN_TLS_DIR）
  -h, --help             显示本帮助
EOF
}

die() { printf '错误：%s\n' "$*" >&2; exit 1; }
need_arg() { [ "$2" -ge 2 ] || die "$1 需要一个参数"; }

while [ $# -gt 0 ]; do
    case $1 in
        -H|--host) need_arg "$1" $#; HOST=$2; shift 2 ;;
        -u|--user) need_arg "$1" $#; SSH_USER=$2; shift 2 ;;
        -p|--ssh-port) need_arg "$1" $#; SSH_PORT=$2; shift 2 ;;
        -d|--domain) need_arg "$1" $#; DOMAIN=$2; shift 2 ;;
        --flavor) need_arg "$1" $#; FLAVOR=$2; shift 2 ;;
        --remote-dir) need_arg "$1" $#; REMOTE_DIR=${2%/}; shift 2 ;;
        --dest) need_arg "$1" $#; DEST=${2%/}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "未知参数：$1" ;;
    esac
done

# 域名：-d > GLASSCAST_DOMAIN > GLASSCAST_PUBLIC_URL 的主机名（去掉 https:// 和路径）
if [ -z "$DOMAIN" ] && [ -n "${GLASSCAST_PUBLIC_URL:-}" ]; then
    DOMAIN=${GLASSCAST_PUBLIC_URL#*://}
    DOMAIN=${DOMAIN%%/*}
    DOMAIN=${DOMAIN%%:*}
fi
[ -n "$DOMAIN" ] || die "不知道证书的域名：用 -d <域名>，或设置 GLASSCAST_PUBLIC_URL"
[ "$HOST" != "server.example.com" ] || die "服务器还是文档里的占位符 server.example.com：用 -H <你的服务器> 指定，或设置 GLASSCAST_TUNNEL_HOST"
[ "$DOMAIN" != "cast.example.com" ] || die "域名还是文档里的占位符 cast.example.com：用 -d <你的域名> 指定"

if [ -z "$REMOTE_DIR" ]; then
    case $FLAVOR in
        nginx) REMOTE_DIR="/etc/letsencrypt/live/$DOMAIN" ;;
        1panel) REMOTE_DIR="/opt/1panel/apps/openresty/openresty/www/sites/$DOMAIN/ssl" ;;
        *) die "--flavor 只能是 nginx 或 1panel：$FLAVOR" ;;
    esac
fi
case $REMOTE_DIR in
    /*) ;;
    *) die "--remote-dir 要写绝对路径：$REMOTE_DIR" ;;
esac
case $REMOTE_DIR in
    *[!A-Za-z0-9._/-]*) die "--remote-dir 只能用字母、数字和 . _ / -：$REMOTE_DIR" ;;
esac

if [ -z "$DEST" ]; then
    # 与 tools/lan_https_proxy.py 的默认目录一致
    if [ "$(uname -s)" = "Darwin" ]; then
        DEST="$HOME/Library/Application Support/glasscast/tls"
    else
        DEST="${XDG_CONFIG_HOME:-$HOME/.config}/glasscast/tls"
    fi
fi

SSH=(ssh -o BatchMode=yes -p "$SSH_PORT" "$SSH_USER@$HOST")
umask 077
mkdir -p "$DEST"
chmod 700 "$DEST"
cleanup() { rm -f "$DEST/fullchain.pem.new" "$DEST/privkey.pem.new"; }
trap cleanup EXIT

printf '从 %s@%s:%s 复制证书到 %s\n' "$SSH_USER" "$HOST" "$REMOTE_DIR" "$DEST"
"${SSH[@]}" "sudo -n cat '$REMOTE_DIR/fullchain.pem'" > "$DEST/fullchain.pem.new"
"${SSH[@]}" "sudo -n cat '$REMOTE_DIR/privkey.pem'" > "$DEST/privkey.pem.new"

[ -s "$DEST/fullchain.pem.new" ] && [ -s "$DEST/privkey.pem.new" ] || die "从服务器读到的证书或私钥是空的，未替换"
if [ "$(openssl x509 -in "$DEST/fullchain.pem.new" -noout -pubkey | openssl sha256)" != \
     "$(openssl pkey -in "$DEST/privkey.pem.new" -pubout | openssl sha256)" ]; then
    die "证书和私钥对不上，未替换（本机原有的文件没动）"
fi
mv "$DEST/fullchain.pem.new" "$DEST/fullchain.pem"
mv "$DEST/privkey.pem.new" "$DEST/privkey.pem"
openssl x509 -in "$DEST/fullchain.pem" -noout -subject -enddate
echo "已更新。重启 tools/lan_https_proxy.py 后生效。"
