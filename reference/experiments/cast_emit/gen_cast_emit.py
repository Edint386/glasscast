"""生成 reference/experiments/cast_emit/cast-emit.loop.xml（已退役的流畅模式页面：emit 事件总线自环，research.md §9.8）。

真机上这一版不出画面（每圈都刷新绑定，打断图片加载，research.md §9.13），现行页面是
pages/cast-emit/template.xml 的事件倒数版（research.md §9.19）。本脚本只为复现实验保留。

用法：``uv run python reference/experiments/cast_emit/gen_cast_emit.py``（覆盖写入）；``--check`` 只比较不写入。

页面结构（写法约束见下方 COMMENT）：

- ``w``、h01～h{N-1} 共 N 个 topic 串成一圈，一圈 n 跳（n 由 dataSource 的 hops 下发）。
- ``w`` 是第 0 跳：写 ``d:f``，触发绑定刷新、图片换地址；同时按 n 发下一跳（n 为 1 时直接发回 ``w``）。
  不按 on-hide/on-show 停换图：真机上播报时页面会被短暂遮挡，之后收不到 on-show，链条就再也不换图（research.md §9.11）。
- 第 k 跳在 ``d:n > k+1`` 时发下一跳，否则发回 ``w``；最后一跳无条件发回 ``w``，所以运行中 hops 变小
  或超过上限都不会断链。
- 图片地址 ``{{url}}{{structuredContent.url}}/{{d:f}}.jpg``：只有 ``d:f`` 变化时才换地址。``d:f`` 在页面加载时
  写一次时间戳（首帧不会命中内核按 URL 的跨会话缓存），之后只由 ``w`` 写入。
- 不放 ``<tick>``、地址里也不放 ``f:`` 函数：内核每次绑定刷新都会让图片重新加载（模拟器实测同一地址被重复请求），
  真机上图片要经手机下载、蓝牙传到眼镜，加载中被打断就永远显示不出来（research.md §9.10）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: 内核限制：根 view 下最多 64 个子元素，65 个整页 "parse FAILED"（模拟器实测，research.md §9.8）
KERNEL_MAX_CHILDREN = 64
#: 官方文档 36.3.14：每页最多 32 个 match 条目
MAX_MATCH_TOPICS = 32
#: 跳数上限。子元素 = 9 个固定元素 + 2N 个 event，25 跳时 59 个（离 64 留 5 个余量）；
#: topic 25 个（离 32 留 7 个余量）。默认 20 ms 一跳时，页面最慢约 1000/(25×20) = 2 张/秒
MAX_HOPS = 25
#: 缺 hops 时的默认跳数
DEFAULT_HOPS = 8
#: 实验（research.md §9.13）：事件链每跑几圈才换一张图。真机上每秒 3 张以上一张都显示不出来，
#: 用它把换图间隔拉到约 1 s（hops=25 时 3 圈约 75 跳），确认眼镜在慢速下能否显示事件链换上去的图
LAPS_PER_IMAGE = 3

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = Path(__file__).resolve().parent / "cast-emit.loop.xml"

COMMENT = """  <!--
    glasscast 流畅模式页面（emit 事件总线自环当节拍器，research.md 9.8）。页面名 cast-emit = 本目录名。
    本文件由 reference/experiments/cast_emit/gen_cast_emit.py 生成，不要手改。

    原理：页面给自己发事件 w、h01、h02……，emit 是异步的（下一轮消息循环才发布），每一跳几十毫秒。
    一圈 n 跳（n 由 start_cast 出参 hops 经 dataSource 下发，1～{max_hops}）：w 是第 0 跳，写一次 d:f，
    绑定刷新、图片换地址、重新下载一张；跳到第 n 跳时回到 w。所以每秒约 1000 除以（n 乘每跳毫秒数）张。
    hops 由 glasscast 按 fast_fps 和每跳毫秒数估算（GLASSCAST_EMIT_HOP_MS），改帧率不用改本页。

    写法约束：
      所有 URI 属性都以 scheme 开头（emit、data、event），平台保存校验 URI-001 放行（2026-10-24 探针通过）。
      topic 用两位数：event 的 match 是前缀匹配，两位数保证互不为前缀；每页最多 32 个 match 条目。
      内核限制根 view 下最多 64 个子元素（多一个整页解析失败），所以每跳只用两条 event，最多 {max_hops} 跳。
      收尾条件写成「常量 大于等于 d:n」（常量写在左边，属性里不能写小于号）；最后一跳无条件回到 w，
      运行中 hops 变小（on-new-intent）时链条在下一跳收尾，不会断。
      hops 判断用 不等于 0：模型把 hops 当字符串传进来也能写入；缺 hops 时保持默认 {default_hops}。
      data 地址里不写 and 号，各个字段分开写入；a:if 里不用 f 和 e 命名空间。
      不按 on-hide 停换图：真机上语音播报会短暂遮挡页面，之后不一定收到 on-show，换图会永远停住。
      d:f 是每圈加一的小计数器：真机把超过 2147483647 的数卡在这个上限（模拟器则是绕回），
      所以不能把时间戳存进 data，否则地址永远不变、只出一帧（research.md 9.11）。
      地址里的时间戳只在绑定刷新时现算，保证每次打开页面都是新地址，不命中缓存。
      不放 tick：每次绑定刷新都会让图片重新加载，多一次刷新就多打断一次加载。
  -->"""


def render(max_hops: int = MAX_HOPS, default_hops: int = DEFAULT_HOPS, laps: int = LAPS_PER_IMAGE) -> str:
    if not 2 <= max_hops <= MAX_MATCH_TOPICS:
        raise ValueError(f"max_hops 必须在 2～{MAX_MATCH_TOPICS}（每跳一个 topic，每页最多 {MAX_MATCH_TOPICS} 个 match 条目）")
    if 9 + 2 * max_hops > KERNEL_MAX_CHILDREN:
        raise ValueError(f"{max_hops} 跳需要 {9 + 2 * max_hops} 个子元素，超过内核上限 {KERNEL_MAX_CHILDREN}")

    def topic(k: int) -> str:
        return "w" if k == 0 else f"h{k:02d}"

    lines = [
        '<view xmlns:a="waft://dsl" width="100%" height="100%" flex-flow="column" bg-color="#000000">',
        COMMENT.format(max_hops=max_hops, default_hops=default_hops),
        '  <page-meta screen-awake="keep"/>',
        # 停止投屏：dataSource 带 stop（非 0）时页面关闭自己（技能在 stop_cast 之后用 stop=1 再打开一次本页面）
        '  <lifecycle-event a:if="{{stop != 0}}" on-load="waft://app/exit-page?appid={{ctx:appId}}" on-new-intent="waft://app/exit-page?appid={{ctx:appId}}"/>',
        # s：本次打开页面的会话号（月日时分秒，小于 int32 上限）；f：换图计数；k：圈数计数；m：每几圈换一张
        f'  <data s="0" f="0" k="0" m="{laps}" n="{default_hops}"/>',
        # 会话号用「月日时分秒」（如 1024183815）：不超过真机的 int32 上限，每次打开都不同；表达式里的除法会被忽略，不能用 timestamp/1000
        "  <lifecycle-event on-load=\"data://update?s={{f:formatDate('now','MMddHHmmss')}}\"/>",
        '  <lifecycle-event a:if="{{hops != 0}}" on-load="data://update?n={{hops}}" on-new-intent="data://update?n={{hops}}"/>',
        '  <lifecycle-event on-load="emit://event/w?"/>',
        # 同一 match 的动作按书写顺序执行、a:if 在执行前才判断（模拟器实测）：满 m 圈先换图、再清零，最后圈数加一
        '  <event a:if="{{d:k >= d:m}}" match="event://w?" action="data://update?f={{d:f + 1}}"/>',
        '  <event a:if="{{d:k >= d:m}}" match="event://w?" action="data://update?k=0"/>',
        '  <event match="event://w?" action="data://update?k={{d:k + 1}}"/>',
    ]
    for k in range(max_hops):
        here = f"event://{topic(k)}?"
        if k < max_hops - 1:
            lines.append(f'  <event a:if="{{{{d:n > {k + 1}}}}}" match="{here}" action="emit://event/{topic(k + 1)}?"/>')
            lines.append(f'  <event a:if="{{{{{k + 1} >= d:n}}}}" match="{here}" action="emit://event/w?"/>')
        else:
            lines.append(f'  <event match="{here}" action="emit://event/w?"/>')  # 最后一跳无条件回到 w
    # 地址只随 s、f 变化（圈数 k 变化时地址不变）；s 写入前不显示，避免命中旧会话缓存
    lines.append(
        '  <image a:if="{{d:s != 0}}" src="{{url}}{{structuredContent.url}}/{{d:s}}_{{d:f}}.jpg"'
        ' width="100%" height="100%" image-fit="stretch"/>'
    )
    lines.append("</view>")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="只检查仓库模板是否与生成结果一致")
    args = ap.parse_args(argv)
    text = render()
    if args.check:
        same = TEMPLATE.read_text(encoding="utf-8") == text
        print("一致" if same else "不一致：运行 uv run python reference/experiments/cast_emit/gen_cast_emit.py 重新生成")
        return 0 if same else 1
    TEMPLATE.write_text(text, encoding="utf-8")
    print(f"已写入 {TEMPLATE.relative_to(ROOT)}（{MAX_HOPS} 跳，{len(text.encode())} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
