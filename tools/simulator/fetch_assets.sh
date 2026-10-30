#!/usr/bin/env bash
# 下载 waft-lite 浏览器渲染内核（千问 AI 眼镜 S1 模拟器）到 tools/simulator/assets/。
#
# 资源来源见 docs/research.md §3：控制台"千问 AI 眼镜 S1"预览用的就是这套公开文件。
# 目录布局必须保持 CDN 上的相对关系，因为内核入口用 import.meta.url 相对定位胶水：
#
#   assets/index.blob-worker.js              内核入口（控制台用的变体），import './dist/waft-lite-wasm.blob-worker.js'
#   assets/index.js                          内核入口（普通变体），import './dist/waft-lite-wasm.js'
#   assets/dist/waft-lite-wasm.blob-worker.js Emscripten 胶水（pthread worker 走 blob: URL 再 import 自身）
#   assets/dist/waft-lite-wasm.js            Emscripten 胶水（pthread worker 直接 new Worker(本文件)）
#   assets/dist/waft-lite-wasm.wasm          WASM 本体（以 "\0asm" 开头）
#   assets/dist/waft-lite-wasm.data          --preload-file 资源包（字体、内置图片、示例 DSL、3 个 .fset）
#   assets/dist/build-info.json              构建戳（可选；缺了内核只是打印 "build stamp unavailable"）
#
# 用法：
#   tools/simulator/fetch_assets.sh            # 缺什么下什么，已有且校验通过的跳过
#   tools/simulator/fetch_assets.sh --force    # 全部重新下载
#   tools/simulator/fetch_assets.sh --check    # 只校验，不下载（缺文件或校验失败时退出码 1）
#   tools/simulator/fetch_assets.sh --from DIR # 先尝试从本地目录按文件名复制（DIR 下平铺或同样的 dist/ 布局都行），再下载缺的
#
# 可用环境变量覆盖：
#   WAFT_ASSETS_DIR  目标目录（默认脚本旁的 assets/）
#   WAFT_VERSION     控制台打包版本（默认 0.0.34）
#   WAFT_BASE        内核 JS/WASM 所在目录（默认 g.alicdn.com 上该版本的 waft-lite/）
#   WAFT_DATA_URL    资源包地址（控制台写死的 OSS 地址；CDN 的 dist/ 下没有 .data，会 404）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${WAFT_ASSETS_DIR:-$SCRIPT_DIR/assets}"
WAFT_VERSION="${WAFT_VERSION:-0.0.34}"
WAFT_BASE="${WAFT_BASE:-https://g.alicdn.com/ai-fe/glasses-open-platform/$WAFT_VERSION/waft-lite}"
WAFT_DATA_URL="${WAFT_DATA_URL:-https://ai-gop-public.oss-cn-beijing.aliyuncs.com/res/waft/0.0.25/waft-lite-wasm.data}"

MODE="fetch"     # fetch | check
FORCE=0
FROM_DIR=""

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    --check) MODE="check" ;;
    --from) shift; FROM_DIR="${1:-}"; [ -n "$FROM_DIR" ] || { echo "--from 需要目录参数" >&2; exit 2; } ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# 清单：相对路径|下载地址|最小字节|最大字节|类型|是否必需
# 大小区间按 0.0.34 实测值放宽（入口 ~13 KB，胶水 ~490 KB，wasm 1 844 186 B，data 23 438 662 B），
# 只用来拦住"下到了错误页/截断文件"，不是精确校验；.data 另外按胶水里的 remote_package_size 精确比对。
MANIFEST="$(cat <<EOF
index.blob-worker.js|$WAFT_BASE/index.blob-worker.js|4096|262144|js|1
dist/waft-lite-wasm.blob-worker.js|$WAFT_BASE/dist/waft-lite-wasm.blob-worker.js|200000|8000000|js|1
dist/waft-lite-wasm.wasm|$WAFT_BASE/dist/waft-lite-wasm.wasm|500000|33554432|wasm|1
dist/waft-lite-wasm.data|$WAFT_DATA_URL|5000000|200000000|data|1
dist/build-info.json|$WAFT_BASE/dist/build-info.json|16|65536|json|0
index.js|$WAFT_BASE/index.js|4096|262144|js|0
dist/waft-lite-wasm.js|$WAFT_BASE/dist/waft-lite-wasm.js|200000|8000000|js|0
EOF
)"

file_size() { wc -c <"$1" | tr -d ' '; }

# 校验单个文件；通过返回 0，失败打印原因返回 1。
verify() {
  local path="$1" min="$2" max="$3" kind="$4"
  [ -f "$path" ] || { echo "缺失"; return 1; }
  local size; size="$(file_size "$path")"
  if [ "$size" -lt "$min" ] || [ "$size" -gt "$max" ]; then
    echo "大小 $size 不在 [$min, $max] 内（可能下到了 OSS/CDN 的 XML 错误页）"; return 1
  fi
  local head4; head4="$(head -c 5 "$path" | od -An -c | tr -d ' \n')"
  case "$kind" in
    wasm)
      [ "$(head -c 4 "$path" | od -An -tx1 | tr -d ' \n')" = "0061736d" ] || { echo "不是 WASM（缺 \\0asm 魔数）"; return 1; } ;;
    js)
      case "$head4" in "<?xml"|"<!DOC"|"<html"|"<HTML") echo "内容是 HTML/XML 错误页"; return 1 ;; esac ;;
    json)
      case "$head4" in "{"*) : ;; *) echo "不是 JSON 对象"; return 1 ;; esac ;;
    data)
      case "$head4" in "<?xml"|"<!DOC"|"<html") echo "内容是 XML/HTML 错误页"; return 1 ;; esac
      # 胶水里写死了资源包大小（"remote_package_size": N），能取到就精确比对。
      local glue="$DEST/dist/waft-lite-wasm.blob-worker.js" expect=""
      [ -f "$glue" ] || glue="$DEST/dist/waft-lite-wasm.js"
      if [ -f "$glue" ]; then
        expect="$(grep -oE '"remote_package_size": *[0-9]+' "$glue" | head -n1 | grep -oE '[0-9]+$' || true)"
      fi
      if [ -n "$expect" ] && [ "$expect" != "$size" ]; then
        echo "大小 $size 与胶水声明的 remote_package_size=$expect 不一致（胶水与资源包版本不配套）"; return 1
      fi ;;
  esac
  echo "ok ($size B)"
  return 0
}

try_copy_from() {
  local rel="$1" target="$2" base cand
  [ -n "$FROM_DIR" ] || return 1
  base="$(basename "$rel")"
  for cand in "$FROM_DIR/$rel" "$FROM_DIR/$base"; do
    if [ -f "$cand" ]; then
      cp "$cand" "$target.part" && mv "$target.part" "$target"
      echo "    从本地复制：$cand"
      return 0
    fi
  done
  return 1
}

download() {
  local url="$1" target="$2"
  echo "    下载：$url"
  # --fail：HTTP 4xx/5xx 不落盘；先写 .part 再改名，避免留下半截文件。
  if curl --fail --location --silent --show-error --retry 3 --retry-delay 2 \
       --connect-timeout 15 --max-time 900 -o "$target.part" "$url"; then
    mv "$target.part" "$target"
    return 0
  fi
  rm -f "$target.part"
  return 1
}

mkdir -p "$DEST/dist"
echo "waft-lite 资源目录：$DEST"
echo "内核：$WAFT_BASE"
echo "资源包：$WAFT_DATA_URL"

failed=0
# .data 的精确校验依赖胶水，所以按清单顺序（胶水在 .data 之前）处理。
while IFS='|' read -r rel url min max kind required; do
  [ -n "$rel" ] || continue
  msg=""
  target="$DEST/$rel"
  mkdir -p "$(dirname "$target")"
  echo "- $rel"
  if [ "$MODE" = "check" ]; then
    if msg="$(verify "$target" "$min" "$max" "$kind")"; then
      echo "    $msg"
    else
      echo "    校验失败：$msg"
      [ "$required" = "1" ] && failed=1
    fi
    continue
  fi
  if [ "$FORCE" = "0" ] && msg="$(verify "$target" "$min" "$max" "$kind")"; then
    echo "    已存在，跳过：$msg"
    continue
  fi
  if try_copy_from "$rel" "$target"; then
    if msg="$(verify "$target" "$min" "$max" "$kind")"; then
      echo "    $msg"
      continue
    fi
    echo "    本地副本校验失败（$msg），改为下载"
    rm -f "$target"
  fi
  if download "$url" "$target" && msg="$(verify "$target" "$min" "$max" "$kind")"; then
    echo "    $msg"
  else
    echo "    失败：${msg:-下载出错}"
    if [ "$required" = "1" ]; then
      failed=1
    else
      echo "    （可选文件，忽略）"
      rm -f "$target"
    fi
  fi
done <<<"$MANIFEST"

if [ "$failed" != "0" ]; then
  echo "有必需文件缺失或校验失败，模拟器无法启动。" >&2
  exit 1
fi
echo "完成。启动：uv run python tools/simulator/serve.py   然后浏览器打开 http://127.0.0.1:8770/"
