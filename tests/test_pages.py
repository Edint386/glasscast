"""眼镜页面（pages/*/template.xml）与 skill/ui_schema.json 的静态检查。

不渲染页面（渲染要用 tools/simulator 的 WASM 内核，见 tools/simulator/README.md），只守住几条
已经用模拟器验证过、又容易被改坏的约定：

- 模板是良构 XML：官方文档 34.5、34.8 要求 XML 属性里的 and 号写成实体；图片地址里写实体又会原样进 URL
  （文档 36.9），所以图片地址只用一个查询参数，事件路由里用实体（research.md §9.2 E4）。
- 绑定同时兼容"平铺出参"和"structuredContent 包装"两种 dataSource（research.md §9.2 E10），
  且不用三元表达式（模拟器实测结果被截断到 63 个字符）。
- skill/ui_schema.json 为空（平台 Hook 不命中，页面由模型打开）；参考用的 Hook 版本条件兼容两种形状，模板名与 pages/ 目录一致。
"""

from __future__ import annotations

import json
import re
import xml.dom.minidom
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "pages"
PAGE_NAMES = ("cast-image", "cast-fast")


def _template(name: str) -> str:
    return (PAGES / name / "template.xml").read_text(encoding="utf-8")


def _elements(text: str) -> dict[str, xml.dom.minidom.Element]:
    """标签名 → 第一个该标签的元素（属性值已按 XML 规则解码实体）。"""
    doc = xml.dom.minidom.parseString(text.encode("utf-8"))
    found: dict[str, xml.dom.minidom.Element] = {}
    for node in doc.getElementsByTagName("*"):
        found.setdefault(node.tagName, node)
    return found


@pytest.mark.parametrize("name", PAGE_NAMES)
def test_template_is_well_formed_xml(name: str) -> None:
    text = _template(name)
    _elements(text)  # 解析失败会抛 ExpatError
    # 去掉注释后不应再有裸 and 号（实体除外）
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    assert not re.search(r"&(?!amp;|lt;|gt;|quot;|apos;|#)", body)


@pytest.mark.parametrize("name", PAGE_NAMES)
def test_image_src_tolerates_both_output_shapes(name: str) -> None:
    src = _elements(_template(name))["image"].getAttribute("src")
    # 真机：地址必须以 .jpg 结尾，缓存破坏放在路径段里（research.md §9.6）
    assert src.startswith("{{url}}{{structuredContent.url}}/{{f:timestamp()}}_")
    assert src.endswith(".jpg")
    assert "?" not in src and "&" not in src
    assert "? " not in src and " : " not in src  # 不用三元表达式


def test_cast_fast_timer_bindings() -> None:
    els = _elements(_template("cast-fast"))
    timer = els["frame-set"]
    assert timer.getAttribute("src") == "{{timer_url}}{{structuredContent.timer_url}}"
    assert timer.getAttribute("frame-duration") == "{{frame_duration_ms}}{{structuredContent.frame_duration_ms}}"
    assert timer.getAttribute("action") == "{{d:act}}"  # 必须是绑定，静态 start 只播一遍（research.md §9.2 E4）
    assert timer.getAttribute("repeat-count") == "1"
    # minidom 已把实体解码；源码里是 &amp;（文档 36.10.3 约束 2）
    assert timer.getAttribute("on-completed") == "data://update?i={{d:i + 1}}&act=start"
    assert "data://update?i={{d:i + 1}}&amp;act=start" in _template("cast-fast")
    # 必须与 glasscast 的 timer.fset 一致（research.md §4：.fset 文件头里没有帧尺寸）
    from glasscast.frames import TIMER_FRAMES, TIMER_SIZE

    assert int(timer.getAttribute("total-frames")) == TIMER_FRAMES
    assert (int(timer.getAttribute("width")), int(timer.getAttribute("height"))) == TIMER_SIZE


def test_platform_ui_schema_is_empty() -> None:
    """平台上的 Hook 从不命中（research.md §9.5），页面由模型调用「UI页面渲染」打开；只留一种机制，免得叠两个页面。"""
    assert json.loads((ROOT / "skill" / "ui_schema.json").read_text(encoding="utf-8")) == []


def test_ui_schema_hooks() -> None:
    """参考用的 Hook 版本（reference/ui-schema-hooks/）仍保持与页面一致，平台修好后可直接换回。"""
    schema = json.loads((ROOT / "reference" / "ui-schema-hooks" / "ui_schema.hooks.json").read_text(encoding="utf-8"))
    assert isinstance(schema, list) and len(schema) == 1
    hooks = schema[0]["config"]["hooks"]
    by_template = {h["actions"][0]["templateName"]: h for h in hooks}
    assert set(by_template) == set(PAGE_NAMES)
    for name, mode in (("cast-image", "image"), ("cast-fast", "fast")):
        assert (PAGES / name / "template.xml").is_file()
        hook = by_template[name]
        assert hook["stage"] == "OUTPUT" and hook["intercepted"] is False and hook["enable"] is True
        assert hook["actions"][0]["type"] == "RENDER" and hook["actions"][0]["renderMode"] == "open"
        # 平铺出参与 MCP CallToolResult 包装（官方文档第 37 节 §2.1）两种形状都能命中
        assert hook["condition"] == (
            f"(success == true && mode == '{mode}') || "
            f"(structuredContent.success == true && structuredContent.mode == '{mode}')"
        )


# --------------------------------------------------------------------------- cast-emit（事件倒数节拍器，research.md §9.19）


def test_cast_emit_page() -> None:
    """流畅模式页面 cast-emit：事件倒数到 0 才换图，倒数中不写 data（真机验证过的结构，research.md §9.19、§9.20）。"""
    from glasscast.pipeline import EMIT_MAX_HOPS

    text = _template("cast-emit")
    doc = xml.dom.minidom.parseString(text.encode("utf-8"))  # 良构
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    assert "&" not in body  # 内核不解码实体，data:// 里也不写 and 号
    comment = re.search(r"<!--(.*?)-->", text, flags=re.S).group(1)
    assert not set(comment) & set("<>{}&")  # 注释里不放这些字符，平台粘贴和内核解析都安全
    children = [n for n in doc.documentElement.childNodes if n.nodeType == n.ELEMENT_NODE]
    assert len(children) <= 64 - 4  # 内核限制根 view 下最多 64 个子元素（research.md §9.8）
    # 不用帧合集（平台保存校验 URI-001 拒绝绑定的 action），不放 tick（每次绑定刷新都会打断图片加载）
    assert not doc.getElementsByTagName("frame-set") and not doc.getElementsByTagName("tick")

    events = doc.getElementsByTagName("event")
    for e in events:
        assert e.getAttribute("action").split("://")[0] in ("emit", "data")  # 所有 URI 属性都带 scheme
        assert not e.getAttribute("a:if")

    def acts(topic: str) -> list[str]:
        return [e.getAttribute("action") for e in events if e.getAttribute("match") == topic]

    # d1～d9 按 n 的首位数字分流（match 是前缀匹配），每步只把事件参数 n 减一，不写 data
    for k in range(1, 10):
        assert acts(f"event://d{k}") == ["emit://event/d{{e:n - 1}}?n={{e:n - 1}}"]
    # 只有 n 到 0 时 d0 匹配：先换图（g 加一，唯一一次刷新），再从 m 开始下一轮（同一 match 按书写顺序执行）
    assert acts("event://d0?") == ["data://update?g={{d:g + 1}}", "emit://event/d{{d:m}}?n={{d:m}}"]
    assert len(events) == 11

    lifecycle = doc.getElementsByTagName("lifecycle-event")
    on_load = [le.getAttribute("on-load") for le in lifecycle]
    # 会话号用「月日时分秒」：每次打开都是新地址，又不超过真机的 int32 上限
    assert "data://update?s={{f:formatDate('now','MMddHHmmss')}}" in on_load
    assert on_load.count("emit://event/d10?n=10") == 1  # 打开时起跑一次
    guard = [le for le in lifecycle if le.getAttribute("on-new-intent")]
    assert len(guard) == 1 and guard[0].getAttribute("a:if") == "{{hops != 0}}"  # 字符串 hops 也能写入；缺省保持默认
    assert guard[0].getAttribute("on-load") == guard[0].getAttribute("on-new-intent") == "data://update?m={{hops * 3}}"
    assert not [le for le in lifecycle if le.getAttribute("on-hide") or le.getAttribute("on-show")]
    defaults = doc.getElementsByTagName("data")[0]
    assert defaults.getAttribute("m") == str(3 * EMIT_MAX_HOPS)  # 缺 hops 时按最慢档（25 × 3 步）

    image = doc.getElementsByTagName("image")[0]
    # .jpg 结尾，地址只随会话号 s 和换图编号 g 变化；两段拼接兼容平铺与 structuredContent 两种出参
    assert image.getAttribute("src") == "{{url}}{{structuredContent.url}}/{{d:s}}_{{d:g}}.jpg"
    assert image.getAttribute("a:if") == "{{d:s != 0}}"
    assert (image.getAttribute("width"), image.getAttribute("height")) == ("640", "290")  # S1 页面画布
    assert "f:timestamp" not in text
    data = json.loads((PAGES / "cast-emit" / "data.json").read_text(encoding="utf-8"))
    assert set(data) == {"url", "hops"}
