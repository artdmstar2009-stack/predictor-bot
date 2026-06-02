from __future__ import annotations

from typing import Any

VERSION = "PRO_UI_PATCH_V1"

REASON_CSS = r"""
    .reason-list { display:flex; flex-wrap:wrap; gap:6px; margin-top:-2px; }
    .reason-chip { display:inline-flex; align-items:center; min-height:24px; padding:4px 7px; border:1px solid rgba(255,255,255,.09); border-radius:8px; background:rgba(255,255,255,.04); color:var(--muted); font-size:11px; line-height:1.25; }
"""

TARGET_ESCAPED = r'''      <div class=\"ai-strip\"><span>AI-\u043b\u0438\u043d\u0438\u044f</span><b>${beautyPickSummary(m.probabilities, picks)}</b></div>'''
REASON_ESCAPED = r'''
      <div class=\"reason-list\">${(m.insights?.reasons || []).slice(0,3).map(r => `<span class=\"reason-chip\">${beautyEsc(r)}</span>`).join('')}</div>'''

TARGET_PLAIN = '''      <div class="ai-strip"><span>AI-линия</span><b>${beautyPickSummary(m.probabilities, picks)}</b></div>'''
REASON_PLAIN = '''
      <div class="reason-list">${(m.insights?.reasons || []).slice(0,3).map(r => `<span class="reason-chip">${beautyEsc(r)}</span>`).join('')}</div>'''


def _patch_html(html: str) -> str:
    if ".reason-chip" not in html:
        html = html.replace("</style>", REASON_CSS + "\n  </style>", 1)
    if "m.insights?.reasons" in html:
        return html
    if TARGET_ESCAPED in html:
        return html.replace(TARGET_ESCAPED, TARGET_ESCAPED + REASON_ESCAPED, 1)
    if TARGET_PLAIN in html:
        return html.replace(TARGET_PLAIN, TARGET_PLAIN + REASON_PLAIN, 1)
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
