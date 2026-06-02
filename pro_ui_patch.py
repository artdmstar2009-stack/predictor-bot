from __future__ import annotations

from typing import Any

VERSION = "PRO_UI_PATCH_V2"

TARGET_ESCAPED = r'''      <div class=\"ai-strip\"><span>AI-\u043b\u0438\u043d\u0438\u044f</span><b>${beautyPickSummary(m.probabilities, picks)}</b></div>'''
REASON_ESCAPED = r'''
      <div class=\"reason-list\">${(m.insights?.reasons || []).slice(0,3).map(r => `<span class=\"reason-chip\">${beautyEsc(r)}</span>`).join('')}</div>'''

TARGET_PLAIN = '''      <div class="ai-strip"><span>AI-линия</span><b>${beautyPickSummary(m.probabilities, picks)}</b></div>'''
REASON_PLAIN = '''
      <div class="reason-list">${(m.insights?.reasons || []).slice(0,3).map(r => `<span class="reason-chip">${beautyEsc(r)}</span>`).join('')}</div>'''

HIDE_REASON_CSS = """
    .reason-list { display:none !important; }
"""


def _patch_html(html: str) -> str:
    html = html.replace(REASON_ESCAPED, "")
    html = html.replace(REASON_PLAIN, "")

    # Remove the @ symbol only when it is used as an odds prefix, e.g. @1.89.
    html = html.replace(">@${fmtOdd", ">${fmtOdd")
    html = html.replace(" @${fmtOdd", " ${fmtOdd")

    if ".reason-list { display:none !important; }" not in html:
        html = html.replace("</style>", HIDE_REASON_CSS + "\n  </style>", 1)
    return html


def apply(app: Any) -> None:
    if getattr(app, "_PRO_UI_PATCH_APPLIED", False):
        return
    try:
        import mini_app
    except Exception as exc:
        logger = getattr(app, "logger", None)
        if logger:
            logger.exception("pro ui patch import failed: %s", exc)
        return

    original = getattr(mini_app, "_index_html", None)
    if not callable(original) or getattr(original, "_pro_ui_patch_wrapped", False):
        return

    def patched_index_html() -> str:
        return _patch_html(original())

    patched_index_html._pro_ui_patch_wrapped = True
    mini_app._index_html = patched_index_html
    app._PRO_UI_PATCH_APPLIED = True
    print(f"{VERSION}_APPLIED")
