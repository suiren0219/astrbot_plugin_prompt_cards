"""提示词卡面板页后端（AstrBot 官方「插件 Pages」机制，≥4.24.1）。

机制（对照实测过的 rumeng/陪伴插件写法）：
- 前端放插件目录 ``pages/panel/index.html``，Dashboard 用受限 iframe 加载，
  并注入 ``window.AstrBotPluginPage`` bridge；
- 前端 ``bridge.apiGet/apiPost(path)`` → ``/api/v1/plugins/extensions/<插件名>/<path>``；
- 后端 ``context.register_web_api(route, handler, methods, desc)``，
  handler 是**无参数 async 函数**，请求经 ``astrbot.api.web.request`` 上下文对象读取；
- route 必须带插件名前缀。本插件 metadata 名是 ``prompt_cards``、目录名是
  ``astrbot_plugin_prompt_cards``，AstrBot 转发用哪个取决于版本 → 四组前缀全注册。

旧版 AstrBot 没有 register_web_api：跳过注册（面板页不可用），不影响插件本体；
独立版控制台（webui.py 的 aiohttp 服务）仍在，作为旧环境的兜底。
"""
from __future__ import annotations

# AstrBot 的 web 辅助。旧版本没有 astrbot.api.web —— 那时不能构造正规响应对象，
# 退回裸 dict（AstrBot 会兜一层 JSON），至少保证接口不至于 500。
try:
    from astrbot.api.web import request as web_request
    from astrbot.api.web import json_response, error_response
    _WEB_API = True
except Exception:
    web_request = None  # type: ignore[assignment]
    json_response = None  # type: ignore[assignment]
    error_response = None  # type: ignore[assignment]
    _WEB_API = False

# 面板页路由前缀：插件名/目录名 × 带不带 /page，四组全注册，宁可多几条不让页面 404
PAGE_PREFIXES = (
    "/prompt_cards/page",
    "/astrbot_plugin_prompt_cards/page",
    "/prompt_cards",
    "/astrbot_plugin_prompt_cards",
)


def page_ok(payload: dict):
    return json_response(payload) if _WEB_API else payload


def page_err(message: str, status: int = 400):
    # 只传 message：error_response 的完整签名随版本变化，实测过的是单参调用
    if _WEB_API:
        return error_response(message)  # type: ignore[misc]
    return {"error": message}


async def read_json_body() -> dict:
    if _WEB_API and web_request is not None:
        try:
            data = await web_request.json(default={})
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def query_param(key: str, default: str = "") -> str:
    if _WEB_API and web_request is not None:
        try:
            return str(web_request.query.get(key, default))
        except Exception:
            return default
    return default


def state_payload(plugin) -> dict:
    stats = {
        "cards": len(plugin._cards),
        "global": sum(1 for c in plugin._cards.values() if c.get("global")),
        "keyworded": sum(1 for c in plugin._cards.values() if c.get("keywords")),
        "sessions": len(plugin._sessions),
        "sessions_with_active": sum(1 for s in plugin._sessions.values() if s),
    }
    return {"cards": plugin._cards, "sessions": plugin._sessions, "stats": stats}


def apply_card_patch(plugin, data: dict) -> tuple[dict | None, str | None]:
    """保存/新建卡片的校验与合并（PATCH 语义），独立控制台与面板页共用这一份。

    返回 (card, None) 表示校验通过且已写入 plugin._cards（未落盘），
    (None, 错误信息) 表示校验失败。
    """
    name = str(data.get("name") or "").strip()
    content = str(data.get("content") or "").strip()
    if not name or " " in name or len(name) > 32:
        return None, "名字不能为空、不含空格、不超过 32 字"
    if not content:
        return None, "内容不能为空"
    if len(content) > 20000:
        return None, "内容过长（上限 2 万字）"
    card = dict(plugin._cards.get(name) or {"keywords": [], "priority": 50, "enabled": True})
    card["content"] = content
    # 只覆盖请求体里显式出现的字段：缺席=保持原值（PATCH 语义），
    # 显式 null/空=用户清空。否则部分保存会悄悄抹掉没提交的设定。
    if "keywords" in data:
        card["keywords"] = [str(k).strip() for k in (data.get("keywords") or [])
                            if str(k).strip()]
    if "priority" in data:
        try:
            card["priority"] = int(data.get("priority"))
        except (TypeError, ValueError):
            card["priority"] = 50
    if "temperature" in data:
        temp = data.get("temperature")
        if temp in (None, ""):
            card.pop("temperature", None)
        else:
            try:
                temp = float(temp)
                if not 0 <= temp <= 2:
                    raise ValueError
                card["temperature"] = temp
            except (TypeError, ValueError):
                return None, "温度需在 0~2 之间"
    if "length_limit" in data:
        limit = data.get("length_limit")
        if limit in (None, ""):
            card.pop("length_limit", None)
        else:
            try:
                limit = int(limit)
                if limit < 20:
                    raise ValueError
                card["length_limit"] = limit
            except (TypeError, ValueError):
                return None, "输出长度需为不小于 20 的整数"
    if "enabled" in data:
        card["enabled"] = bool(data.get("enabled"))
    if "global" in data:
        card["global"] = bool(data.get("global"))
    if "mode" in data:
        if data.get("mode") in ("persona", "overlay"):
            card["mode"] = data.get("mode")
        elif data.get("mode") in (None, "", "overlay"):
            card.pop("mode", None)
        else:
            return None, "mode 仅支持 persona/overlay"
    if "dial" in data:
        dial = data.get("dial")
        if dial in ("保守", "激进"):
            card["dial"] = dial
        elif dial in (None, "", "标准"):
            card.pop("dial", None)
        else:
            return None, "dial 仅支持 保守/标准/激进"
    if "tone" in data:
        tone = data.get("tone")
        if tone in ("理性", "感性"):
            card["tone"] = tone
        elif tone in (None, "", "均衡"):
            card.pop("tone", None)
        else:
            return None, "tone 仅支持 理性/均衡/感性"
    plugin._cards[name] = card
    return card, None


class CardPageApi:
    """面板页后端：路由注册 + 各接口 handler（无参 async，AstrBot Pages 约定）。"""

    def __init__(self, plugin, logger=None):
        self.plugin = plugin
        self.logger = logger or getattr(plugin, "_log", None)

    def _log(self, level: str, msg: str):
        if self.logger is not None:
            try:
                getattr(self.logger, level)(msg)
            except Exception:
                pass

    # ---------- 注册 ----------
    def route_bindings(self):
        """(bridge 路径, handler, methods, 描述)。bridge 路径不含插件名前缀。"""
        return [
            ("/state", self.h_state, ["GET"], "提示词卡面板：卡片与会话总览"),
            ("/card", self.h_card_save, ["POST"], "提示词卡面板：保存卡片"),
            ("/card/del", self.h_card_del, ["POST"], "提示词卡面板：删除卡片"),
            ("/session/clear", self.h_sess_clear, ["POST"], "提示词卡面板：清空会话启用"),
            ("/session/off", self.h_sess_off, ["POST"], "提示词卡面板：停用会话卡片"),
            ("/peek", self.h_peek, ["GET"], "提示词卡面板：注入预览"),
            ("/gen", self.h_gen, ["POST"], "提示词卡面板：AI 提炼角色卡"),
        ]

    def register(self) -> int:
        """注册路由（四组前缀全注册），返回成功条数。旧版 AstrBot 返回 0。"""
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            self._log("info", "[prompt_cards] 当前 AstrBot 无 register_web_api（<4.24.1），"
                              "面板页不可用；独立控制台仍可用")
            return 0
        count = 0
        for path, handler, methods, desc in self.route_bindings():
            for prefix in PAGE_PREFIXES:
                try:
                    register(f"{prefix}{path}", handler, methods, desc)
                    count += 1
                except Exception as e:
                    self._log("warning", f"[prompt_cards] 面板路由注册失败 {prefix}{path}: {e}")
        self._log("info", f"[prompt_cards] 面板页 API 已注册 {count} 条路由（pages/panel/）")
        return count

    # ---------- handlers ----------
    async def h_state(self):
        try:
            return page_ok(state_payload(self.plugin))
        except Exception as e:
            return page_err(f"状态读取失败：{type(e).__name__}: {e}", 500)

    async def h_card_save(self):
        data = await read_json_body()
        card, err = apply_card_patch(self.plugin, data)
        if err:
            return page_err(err)
        self.plugin._save()
        self._log("info", f"[prompt_cards] 面板保存卡片「{data.get('name')}」"
                          f"（{len(str(card.get('content') or ''))} 字）")
        return page_ok({"ok": True, "name": data.get("name")})

    async def h_card_del(self):
        data = await read_json_body()
        name = str(data.get("name") or "").strip()
        if name not in self.plugin._cards:
            return page_err("卡片不存在", 404)
        del self.plugin._cards[name]
        for sess in self.plugin._sessions.values():
            sess.pop(name, None)
        self.plugin._save()
        self._log("info", f"[prompt_cards] 面板删除卡片「{name}」")
        return page_ok({"ok": True})

    async def h_sess_clear(self):
        data = await read_json_body()
        umo = str(data.get("umo") or "")
        if umo in self.plugin._sessions:
            self.plugin._sessions[umo] = {}
            self.plugin._save()
            self._log("info", f"[prompt_cards] 面板清空会话 {umo} 的启用卡片")
        return page_ok({"ok": True})

    async def h_sess_off(self):
        data = await read_json_body()
        umo, name = str(data.get("umo") or ""), str(data.get("name") or "")
        sess = self.plugin._sessions.get(umo)
        if sess and name in sess:
            del sess[name]
            self.plugin._save()
        return page_ok({"ok": True})

    async def h_peek(self):
        p = self.plugin
        umo = query_param("umo")
        injection, names = p._build_injection(umo)
        temp, tsrc = p._effective_temperature(umo)
        return page_ok({"injection": injection, "names": names,
                        "temperature": temp, "temperature_from": tsrc})

    async def h_gen(self):
        data = await read_json_body()
        source = str(data.get("source") or "").strip()
        name = str(data.get("name") or "").strip() or None
        if not source:
            return page_err("材料不能为空")
        if len(source) > 20000:
            return page_err("材料过长（上限 2 万字）")
        card_name, content, err = await self.plugin._llm_distill(source, name)
        if err:
            return page_err(err)
        return page_ok({"name": card_name, "content": content})
