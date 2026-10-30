#!/bin/sh
# glasscast：证书续期（仅 1Panel 变体 server-setup.sh --flavor 1panel；服务器上以 root 运行）
#
# 系统自带 nginx（默认变体）不用本脚本：证书由 certbot 软件包自带的定时任务续期，
# server-setup.sh 装的 /etc/letsencrypt/renewal-hooks/deploy/ 钩子负责续期后 reload nginx。
#
# 安装位置：/opt/glasscast/renew-cert.sh（由 deploy/server-setup.sh 安装）
# 触发方式：systemd 的 glasscast-cert-renew.timer -> glasscast-cert-renew.service（每周一次）
# 手动运行：sudo /opt/glasscast/renew-cert.sh
#
# 域名、容器名等参数不写在这里：读 server-setup.sh 部署时保存的 /opt/glasscast/deploy.env。
#
# 行为：
#   1. 用 certbot/certbot 容器 renew（webroot；依赖 80 端口 vhost 一直放行 /.well-known/acme-challenge/）；
#   2. 证书内容没变就不重装、不 reload（没到期时 certbot renew 什么都不做，每周空跑一次很正常）；
#   3. "装好了新证书、reload 却失败"会留下标记 $DEPLOY_ROOT/.reload-pending：之后每次运行看到标记都会重试
#      -t 和 reload，成功才删标记。否则下一次比较时证书已经一致，会误判"不需要 reload"，
#      OpenResty 一直用旧证书直到过期。server-setup.sh 每次 reload 成功也会删掉这个标记；
#   4. 每一步打印一行带时间的日志，方便 journalctl -u glasscast-cert-renew.service 排查。
set -eu

# ============================ 改这里（一般不用改） ============================
DEPLOY_ROOT="${GLASSCAST_TEST_PREFIX:-}/opt/glasscast"   # server-setup.sh 的部署目录
CERTBOT_IMAGE="certbot/certbot:latest"
# ==============================================================================

PREFIX="${GLASSCAST_TEST_PREFIX:-}"   # 仅供本地测试，服务器上不要设置
CONFIG="$DEPLOY_ROOT/deploy.env"

log() {
    printf '%s [renew-cert] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

if [ ! -f "$CONFIG" ]; then
    log "错误：找不到 $CONFIG，先运行 deploy/server-setup.sh 部署" >&2
    exit 1
fi
FLAVOR="" DOMAIN="" OPENRESTY_CONTAINER="" OPENRESTY_ROOT=""
# deploy.env 由 server-setup.sh 以 root 写入，只含校验过的 KEY=值 行
. "$CONFIG"

if [ "$FLAVOR" != "1panel" ]; then
    log "部署变体是 '${FLAVOR:-?}'，不是 1panel：证书由 certbot 自带的定时任务续期，本脚本不需要运行"
    exit 0
fi
if [ -z "$DOMAIN" ] || [ -z "$OPENRESTY_CONTAINER" ] || [ -z "$OPENRESTY_ROOT" ]; then
    log "错误：$CONFIG 里缺少 DOMAIN / OPENRESTY_CONTAINER / OPENRESTY_ROOT，重新运行 server-setup.sh" >&2
    exit 1
fi

openresty_root="$PREFIX$OPENRESTY_ROOT"
site_ssl="$openresty_root/www/sites/$DOMAIN/ssl"
live_dir="$DEPLOY_ROOT/letsencrypt/live/$DOMAIN"
reload_marker="$DEPLOY_ROOT/.reload-pending"

log "certbot renew（webroot）开始：$DOMAIN"
docker run --rm -v "$DEPLOY_ROOT/letsencrypt:/etc/letsencrypt" -v "$openresty_root/root:/var/www/acme" \
  "$CERTBOT_IMAGE" renew --webroot --webroot-path /var/www/acme --quiet
log "certbot renew 结束"

if [ ! -s "$live_dir/fullchain.pem" ] || [ ! -s "$live_dir/privkey.pem" ]; then
    log "错误：找不到 $live_dir/{fullchain,privkey}.pem，先运行 server-setup.sh 签发证书" >&2
    exit 1
fi

if cmp -s "$live_dir/fullchain.pem" "$site_ssl/fullchain.pem" \
   && cmp -s "$live_dir/privkey.pem" "$site_ssl/privkey.pem"; then
    if [ ! -f "$reload_marker" ]; then
        log "证书未变化，不需要 reload"
        exit 0
    fi
    log "证书已是最新，但上次装完证书后 reload 没有成功（$reload_marker），重试 reload"
else
    log "证书已更新，安装到 $site_ssl"
    # 先落标记再动文件：下面任何一步失败（set -e 直接退出），下次运行都会重试 reload
    touch "$reload_marker"
    install -d -m 0755 "$site_ssl"
    install -m 0644 "$live_dir/fullchain.pem" "$site_ssl/fullchain.pem"
    install -m 0600 "$live_dir/privkey.pem" "$site_ssl/privkey.pem"
fi

log "openresty -t"
docker exec "$OPENRESTY_CONTAINER" openresty -t
log "openresty -s reload"
docker exec "$OPENRESTY_CONTAINER" openresty -s reload
rm -f "$reload_marker"
log "完成"
