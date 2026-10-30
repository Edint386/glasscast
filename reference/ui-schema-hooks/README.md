# ui_schema.json 的 Hook 版本（未使用，留作参考）

`ui_schema.hooks.json` 是原设计：`XXXXXX.start_cast` 返回后由 OUTPUT Hook 按 `mode` 自动打开 `cast-image` / `cast-fast`。
真机实测（2026-10-24，`docs/research.md` §9.5）Hook 从不命中：平台交给 Hook 的 MCP 出参只有 `response.content[0].text`，
没有 `structuredContent`，也没有平铺字段。所以平台和仓库的 `skill/ui_schema.json` 现在都是 `[]`，页面由模型在
`start_cast` 成功后调用官方工具「UI页面渲染」（`tQ8fCF.ui_page_render`）打开（见 `skill/SKILL.md`「打开页面（必读）」）。
两种机制同时开着可能叠出两个页面，所以只留一种。平台以后若能把结构化出参交给 Hook，可以换回这份文件。
