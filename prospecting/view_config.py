"""prospecting/view_config.py — 段名 → HubSpot 私有 view URL 映射。

Ned 在 HubSpot 建好各段的私有 view(每个配一个 Account name / is-equal-to-any-of 过滤器并保存)后,
把 URL 回填到这里。键必须与 outreach_state.VIEW_NAME 的值 + "维护到点" 一致。
留空的段会被 run_view_cycle 跳过并提示。
"""

_BASE = "https://app.hubspot.com/contacts/9311334/objects/0-2/views"

VIEW_URL_MAP = {
    "未开发": f"{_BASE}/69132202/list",
    "待处理·换人或放弃": f"{_BASE}/69132217/list",
    "已回复·待跟进": f"{_BASE}/69132233/list",
    "维护到点": f"{_BASE}/69132236/list",
    "开发中": f"{_BASE}/69132267/list",     # 可选
}
