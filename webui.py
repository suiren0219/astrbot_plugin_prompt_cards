"""提示词卡独立控制台（aiohttp，服务器直连场景与 AstrBot <4.24.1 的兜底）。

* 仅在插件配置开启时随插件启动，默认只绑定 127.0.0.1；
* 所有 /api/* 都要求 Authorization: Bearer <令牌>（面板 webui_token，
  留空则插件首次启动自动生成并写入配置、打印到日志）；
* 页面本体与面板页共用 pages/panel/index.html（同一页面双通道：
  面板里走官方 bridge，直连时走这里的 fetch+令牌）；
* 卡片校验/合并逻辑与面板页共用 page_api.apply_card_patch，避免两份规则漂移。
"""
import hmac
import json
import os
import sys

from aiohttp import web

# main.py 先加载 page_api 再加载本模块；找不到说明加载顺序被破坏，宁可不启用
_page_core = sys.modules.get("_prompt_cards_page_api")
if _page_core is None:
    raise ImportError("page_api.py 必须先于 webui.py 加载")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(PLUGIN_DIR, "web")              # 字体等静态资源
PANEL_DIR = os.path.join(PLUGIN_DIR, "pages", "panel") # 与面板页共用的页面
MIME = {".html": "text/html", ".ttf": "font/ttf", ".txt": "text/plain"}


def _dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False)


class CardWebUI:
    def __init__(self, plugin, host: str, port: int, token: str, logger):
        self.plugin = plugin
        self.host = host or "127.0.0.1"
        self.port = int(port or 6199)
        self.token = token
        self.logger = logger
        self.runner = None
        self._index_html = None

    # ---------- 生命周期 ----------
    async def start(self):
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/fonts/{name}", self._font)
        app.router.add_get("/api/state", self._state)
        app.router.add_post("/api/card", self._save_card)
        app.router.add_delete("/api/card/{name}", self._del_card)
        app.router.add_post("/api/session/clear", self._sess_clear)
        app.router.add_post("/api/session/off", self._sess_off)
        app.router.add_get("/api/peek", self._peek)
        app.router.add_post("/api/gen", self._gen)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
        self.runner = None

    # ---------- 鉴权与静态 ----------
    def _ok(self, request) -> bool:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth[7:].strip(), self.token)

    @staticmethod
    def _deny():
        return web.json_response({"error": "令牌无效"}, status=401)

    async def _index(self, request):
        if self._index_html is None:
            with open(os.path.join(PANEL_DIR, "index.html"), "r", encoding="utf-8") as f:
                self._index_html = f.read()
        return web.Response(text=self._index_html, content_type="text/html", charset="utf-8")

    async def _font(self, request):
        name = os.path.basename(request.match_info.get("name", ""))
        path = os.path.join(WEB_DIR, "fonts", name)
        if not os.path.isfile(path):
            return web.Response(status=404)
        ext = os.path.splitext(name)[1]
        return web.FileResponse(path, headers={
            "Content-Type": MIME.get(ext, "application/octet-stream"),
            "Cache-Control": "public, max-age=86400"})

    # ---------- API ----------
    async def _state(self, request):
        if not self._ok(request):
            return self._deny()
        p = self.plugin
        stats = {
            "cards": len(p._cards),
            "global": sum(1 for c in p._cards.values() if c.get("global")),
            "keyworded": sum(1 for c in p._cards.values() if c.get("keywords")),
            "sessions": len(p._sessions),
            "sessions_with_active": sum(1 for s in p._sessions.values() if s),
        }
        return web.json_response(
            {"cards": p._cards, "sessions": p._sessions, "stats": stats},
            dumps=_dumps)

    async def _save_card(self, request):
        if not self._ok(request):
            return self._deny()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求体不是有效 JSON"}, status=400)
        # 校验与合并走 page_api 的同一份 PATCH 语义，独立通道与面板页规则永不漂移
        card, err = _page_core.apply_card_patch(self.plugin, data)
        if err:
            return web.json_response({"error": err}, status=400)
        self.plugin._save()
        name = str(data.get("name") or "").strip()
        self.logger.info("[prompt_cards] WebUI 保存卡片「%s」（%d 字）",
                         name, len(str(card.get("content") or "")))
        return web.json_response({"ok": True, "name": name})

    async def _del_card(self, request):
        if not self._ok(request):
            return self._deny()
        name = request.match_info.get("name", "")
        p = self.plugin
        if name not in p._cards:
            return web.json_response({"error": "卡片不存在"}, status=404)
        del p._cards[name]
        for sess in p._sessions.values():
            sess.pop(name, None)
        p._save()
        self.logger.info("[prompt_cards] WebUI 删除卡片「%s」", name)
        return web.json_response({"ok": True})

    async def _sess_clear(self, request):
        if not self._ok(request):
            return self._deny()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求体不是有效 JSON"}, status=400)
        umo = str(data.get("umo") or "")
        if umo in self.plugin._sessions:
            self.plugin._sessions[umo] = {}
            self.plugin._save()
            self.logger.info("[prompt_cards] WebUI 清空会话 %s 的启用卡片", umo)
        return web.json_response({"ok": True})

    async def _sess_off(self, request):
        if not self._ok(request):
            return self._deny()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求体不是有效 JSON"}, status=400)
        umo, name = str(data.get("umo") or ""), str(data.get("name") or "")
        sess = self.plugin._sessions.get(umo)
        if sess and name in sess:
            del sess[name]
            self.plugin._save()
        return web.json_response({"ok": True})

    async def _peek(self, request):
        if not self._ok(request):
            return self._deny()
        p = self.plugin
        umo = request.query.get("umo", "")
        injection, names = p._build_injection(umo)
        temp, tsrc = p._effective_temperature(umo)
        return web.json_response(
            {"injection": injection, "names": names,
             "temperature": temp, "temperature_from": tsrc},
            dumps=_dumps)

    async def _gen(self, request):
        if not self._ok(request):
            return self._deny()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求体不是有效 JSON"}, status=400)
        source = str(data.get("source") or "").strip()
        name = str(data.get("name") or "").strip() or None
        if not source:
            return web.json_response({"error": "材料不能为空"}, status=400)
        if len(source) > 20000:
            return web.json_response({"error": "材料过长（上限 2 万字）"}, status=400)
        card_name, content, err = await self.plugin._llm_distill(source, name)
        if err:
            return web.json_response({"error": err}, status=400)
        return web.json_response({"name": card_name, "content": content}, dumps=_dumps)


def json_dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False)
