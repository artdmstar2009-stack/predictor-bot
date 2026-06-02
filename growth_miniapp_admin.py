from __future__ import annotations

from typing import Any

VERSION = "GROWTH_MINIAPP_ADMIN_V1"

ADMIN_CSS = r"""
    .admin-promo { grid-column:1/-1; min-height:auto; border-color:rgba(244,196,48,.26); background:rgba(244,196,48,.055); }
    .admin-promo-grid { display:grid; grid-template-columns:1.15fr .7fr .7fr auto; gap:8px; margin-top:10px; }
    .admin-promo-grid input { height:40px; min-width:0; }
    .admin-promo-grid button { min-height:40px; border-color:rgba(244,196,48,.38); background:rgba(244,196,48,.1); }
    .admin-promo-list { display:grid; gap:6px; margin-top:10px; color:var(--muted); font-size:11px; }
    @media (max-width: 620px) { .admin-promo-grid { grid-template-columns:1fr 1fr; } .admin-promo-grid input:first-child, .admin-promo-grid button { grid-column:1/-1; } }
"""

ADMIN_JS = r"""
async function miniAdminCreatePromo(){
  const code = (document.getElementById('adminPromoCode')?.value || '').trim();
  const amount = parseInt(document.getElementById('adminPromoAmount')?.value || '0', 10) || 0;
  const maxUses = parseInt(document.getElementById('adminPromoUses')?.value || '0', 10) || 0;
  if(!code || amount <= 0){ toast('Введи код и сумму'); return; }
  try{
    const data = await api('/api/admin/promos', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({code, amount, max_uses:maxUses, initData})});
    toast(data.message || 'Промокод создан');
    await loadProfile();
  }catch(e){ const msg=e.message || 'Не удалось создать промокод'; tg?.showAlert?.(msg); toast(msg); }
}
function miniAdminPromoList(items){
  if(!items || !items.length) return '<span>Промокодов пока нет</span>';
  return items.slice(0,5).map(x => {
    const limit = Number(x.max_uses || 0) <= 0 ? '∞' : Number(x.max_uses || 0);
    return `<span><code>${growthEsc(x.code)}</code> +${growthMoney(x.amount)} · ${Number(x.used_count || 0)}/${limit}</span>`;
  }).join('');
}
function miniAdminAttachProfile(p){
  if(!p?.is_admin) return;
  const el = document.getElementById('profileStats');
  if(!el || document.getElementById('adminPromoApply')) return;
  const promos = p.admin_promos || [];
  el.insertAdjacentHTML('beforeend', `
    <div class="metric admin-promo">
      <span>Админ · промокоды</span>
      <div class="admin-promo-grid">
        <input id="adminPromoCode" autocomplete="off" placeholder="Код, например START500" />
        <input id="adminPromoAmount" inputmode="numeric" placeholder="Сумма" />
        <input id="adminPromoUses" inputmode="numeric" placeholder="Лимит" />
        <button id="adminPromoApply" type="button">Создать</button>
      </div>
      <div class="admin-promo-list">${miniAdminPromoList(promos)}</div>
    </div>`);
  const button = document.getElementById('adminPromoApply');
  if(button) button.onclick = miniAdminCreatePromo;
}
if(typeof renderProfile === 'function' && !window.__growthAdminRenderProfileWrapped){
  const previousRenderProfile = renderProfile;
  renderProfile = function(p){ previousRenderProfile(p); miniAdminAttachProfile(p); };
  window.__growthAdminRenderProfileWrapped = true;
}
"""


def _log(app: Any):
    return getattr(app, "logger", None)


def _patch_profile(app: Any, mini_app: Any, growth_features: Any) -> None:
    original = getattr(mini_app, "_profile", None)
    if not callable(original) or getattr(original, "_growth_admin_wrapped", False):
        return

    def patched_profile(bot: Any, user_id: int) -> dict[str, Any]:
        profile = dict(original(bot, user_id) or {})
        is_admin = bool(bot.is_admin(int(user_id))) if hasattr(bot, "is_admin") else False
        profile["is_admin"] = is_admin
        if is_admin:
            try:
                profile["admin_promos"] = growth_features.list_promo_codes(bot, 10)
            except Exception:
                profile["admin_promos"] = []
        return profile

    patched_profile._growth_admin_wrapped = True
    mini_app._profile = patched_profile


def _patch_index_html(app: Any, mini_app: Any) -> None:
    original = getattr(mini_app, "_index_html", None)
    if not callable(original) or getattr(original, "_growth_admin_wrapped", False):
        return

    def patched_index_html() -> str:
        html = original()
        if ".admin-promo" not in html:
            html = html.replace("</style>", ADMIN_CSS + "\n  </style>", 1)
        if "miniAdminCreatePromo" not in html:
            html = html.replace("document.getElementById('refresh').onclick = load;", ADMIN_JS + "\ndocument.getElementById('refresh').onclick = load;", 1)
        return html

    patched_index_html._growth_admin_wrapped = True
    mini_app._index_html = patched_index_html


def _patch_start_web_server(app: Any, mini_app: Any, growth_features: Any) -> None:
    if getattr(app, "_GROWTH_ADMIN_WEB_PATCHED", False):
        return

    async def index(_request):
        return mini_app._text_response(app, "ok", "text/plain")

    async def app_page(_request):
        return mini_app._text_response(app, mini_app._index_html(), "text/html")

    async def api_summary(_request):
        return mini_app._json_response(app, mini_app._summary(app))

    async def api_matches(request):
        user_id, _, _ = mini_app._auth_user(app, request)
        sport = request.query.get("sport", "all")
        query = request.query.get("q", "")
        limit = mini_app._int_query(request.query.get("limit"), 80, 1, 200)
        return mini_app._json_response(app, {"items": mini_app._active_matches(app, sport, limit, query, user_id)})

    async def api_match(request):
        user_id, _, _ = mini_app._auth_user(app, request)
        match_id = int(request.match_info.get("match_id", "0") or "0")
        row = app.get_match(match_id)
        if not row:
            return mini_app._json_response(app, {"error": "not_found"}, status=404)
        return mini_app._json_response(app, mini_app._row_to_match(app, row, user_id))

    async def api_predict(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        user_id, _, error = mini_app._auth_user(app, request, body)
        if not user_id:
            return mini_app._json_response(app, {"error": error}, status=401)
        match_id = mini_app._int_query(str(body.get("match_id", "0")), 0, 0, 10**12)
        pick = str(body.get("pick", "")).upper()
        ok, message, match = mini_app._place_prediction(app, user_id, match_id, pick)
        if not ok:
            return mini_app._json_response(app, {"error": message}, status=400)
        return mini_app._json_response(app, {"ok": True, "message": message, "match": match, "profile": mini_app._profile(app, user_id)})

    async def api_me(request):
        user_id, user, error = mini_app._auth_user(app, request)
        if not user_id:
            return mini_app._json_response(app, {"error": error}, status=401)
        return mini_app._json_response(app, {"user": user, "profile": mini_app._profile(app, user_id)})

    async def api_my_predictions(request):
        user_id, _, error = mini_app._auth_user(app, request)
        if not user_id:
            return mini_app._json_response(app, {"error": error}, status=401)
        limit = mini_app._int_query(request.query.get("limit"), 50, 1, 100)
        return mini_app._json_response(app, {"items": mini_app._my_predictions(app, user_id, limit)})

    async def api_leaderboard(_request):
        return mini_app._json_response(app, mini_app._leaderboard(app))

    async def api_backtest(request):
        limit = mini_app._int_query(request.query.get("limit"), 500, 1, 2000)
        if not hasattr(app, "run_ai_line_backtest"):
            return mini_app._json_response(app, {"error": "backtest_unavailable"}, status=503)
        return mini_app._json_response(app, app.run_ai_line_backtest(limit))

    async def api_promo_redeem(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        user_id, user, error = mini_app._auth_user(app, request, body)
        if not user_id:
            return mini_app._json_response(app, {"error": error}, status=401)
        ok, message, extra = growth_features.redeem_promo_code(app, int(user_id), str(body.get("code", "")))
        status = 200 if ok else 400
        return mini_app._json_response(app, {"ok": ok, "message": message, "extra": extra, "user": user, "profile": mini_app._profile(app, int(user_id))}, status=status)

    async def api_admin_promos(request):
        user_id, _, error = mini_app._auth_user(app, request)
        if not user_id:
            return mini_app._json_response(app, {"error": error}, status=401)
        if not app.is_admin(int(user_id)):
            return mini_app._json_response(app, {"error": "Недостаточно прав"}, status=403)
        return mini_app._json_response(app, {"items": growth_features.list_promo_codes(app, 50)})

    async def api_admin_promos_create(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        user_id, _, error = mini_app._auth_user(app, request, body)
        if not user_id:
            return mini_app._json_response(app, {"error": error}, status=401)
        if not app.is_admin(int(user_id)):
            return mini_app._json_response(app, {"error": "Недостаточно прав"}, status=403)
        code = str(body.get("code", ""))
        amount = int(body.get("amount") or 0)
        max_uses = int(body.get("max_uses") or 0)
        ok, message = growth_features.create_promo_code(app, code, amount, max_uses, int(user_id))
        status = 200 if ok else 400
        return mini_app._json_response(app, {"ok": ok, "message": message, "items": growth_features.list_promo_codes(app, 10), "profile": mini_app._profile(app, int(user_id))}, status=status)

    async def _set_menu_button() -> None:
        base = mini_app._public_base_url() if hasattr(mini_app, "_public_base_url") else ""
        url = f"{base}/app" if base else ""
        if not url:
            return
        try:
            from aiogram.types import MenuButtonWebApp, WebAppInfo
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Predictor", web_app=WebAppInfo(url=url)))
        except Exception as exc:
            logger = _log(app)
            if logger:
                logger.warning("growth admin: menu button setup failed: %s", exc)

    async def start_web_server():
        if app.PORT <= 0:
            return
        await _set_menu_button()
        web_app = app.web.Application()
        web_app.router.add_get("/", index)
        web_app.router.add_get("/health", index)
        web_app.router.add_get("/app", app_page)
        web_app.router.add_get("/api/summary", api_summary)
        web_app.router.add_get("/api/matches", api_matches)
        web_app.router.add_get(r"/api/matches/{match_id:\d+}", api_match)
        web_app.router.add_post("/api/predict", api_predict)
        web_app.router.add_get("/api/me", api_me)
        web_app.router.add_get("/api/me/predictions", api_my_predictions)
        web_app.router.add_get("/api/leaderboard", api_leaderboard)
        web_app.router.add_get("/api/backtest", api_backtest)
        web_app.router.add_post("/api/promo/redeem", api_promo_redeem)
        web_app.router.add_get("/api/admin/promos", api_admin_promos)
        web_app.router.add_post("/api/admin/promos", api_admin_promos_create)

        runner = app.web.AppRunner(web_app)
        await runner.setup()
        site = app.web.TCPSite(runner, host="0.0.0.0", port=app.PORT)
        await site.start()
        app.logger.info("growth admin mini app web server started on 0.0.0.0:%s", app.PORT)
        while True:
            await app.asyncio.sleep(3600)

    app.start_web_server = start_web_server
    app._GROWTH_ADMIN_WEB_PATCHED = True


def apply(app: Any) -> None:
    if getattr(app, "_GROWTH_MINIAPP_ADMIN_APPLIED", False):
        return
    try:
        import mini_app
        import growth_features
    except Exception as exc:
        logger = _log(app)
        if logger:
            logger.exception("growth admin import failed: %s", exc)
        return

    _patch_profile(app, mini_app, growth_features)
    _patch_index_html(app, mini_app)
    _patch_start_web_server(app, mini_app, growth_features)
    app._GROWTH_MINIAPP_ADMIN_APPLIED = True
    print(f"{VERSION}_APPLIED")
