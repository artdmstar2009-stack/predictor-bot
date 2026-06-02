from __future__ import annotations

from typing import Any

VERSION = "MINIAPP_ACCESS_PATCH_V1"

ACCESS_JS = r"""
function miniAccessCleanProfile(p){
  const root = document.getElementById('profileStats');
  if(!root) return;
  root.querySelectorAll('.profile-wide').forEach(block => {
    const label = (block.querySelector('span')?.textContent || '').trim();
    if(label.includes('\u0420\u0435\u0444\u0435\u0440\u0430\u043b\u044c\u043d\u0430\u044f \u0441\u0441\u044b\u043b\u043a\u0430')) block.remove();
  });
  if(!(p && p.is_admin === true)){
    root.querySelectorAll('.admin-promo').forEach(block => block.remove());
  }
}
if(typeof renderProfile === 'function' && !window.__miniAccessRenderProfileWrapped){
  const miniAccessPreviousRenderProfile = renderProfile;
  renderProfile = function(p){ miniAccessPreviousRenderProfile(p); miniAccessCleanProfile(p); };
  window.__miniAccessRenderProfileWrapped = true;
}
"""


def _patch_index_html(mini_app: Any) -> None:
    original = getattr(mini_app, "_index_html", None)
    if not callable(original) or getattr(original, "_miniapp_access_wrapped", False):
        return

    def patched_index_html() -> str:
        html = original()
        html = html.replace(
            "if(!p?.is_admin) return;",
            "if(!(p && p.is_admin === true)){ document.querySelectorAll('.admin-promo').forEach(x => x.remove()); return; }",
        )
        if "miniAccessCleanProfile" not in html:
            html = html.replace(
                "document.getElementById('refresh').onclick = load;",
                ACCESS_JS + "\ndocument.getElementById('refresh').onclick = load;",
                1,
            )
        return html

    patched_index_html._miniapp_access_wrapped = True
    mini_app._index_html = patched_index_html


def apply(app: Any) -> None:
    if getattr(app, "_MINIAPP_ACCESS_PATCH_APPLIED", False):
        return
    try:
        import mini_app
    except Exception as exc:
        logger = getattr(app, "logger", None)
        if logger:
            logger.exception("miniapp access patch import failed: %s", exc)
        return

    _patch_index_html(mini_app)
    app._MINIAPP_ACCESS_PATCH_APPLIED = True
    print(f"{VERSION}_APPLIED")
