"""提示词卡面板页后端（AstrBot 官方「插件 Pages」机制，≥4.24.1）。

实现方式对齐官方文档与社区成熟插件（keyou-weixiao）：插件**不绑任何端口、不跑
自己的服务器**，只在 initialize 里 register_web_api 注册接口，页面经面板 bridge
调用；静态资源全部放在 pages/panel/ 下用相对路径（AstrBot 带 asset_token 代理）。
v1.5.4 前的独立 aiohttp 服务器已删除——其默认端口 6199 曾与 aiocqhttp 适配器的
WS 端口冲突，导致 NapCat 连不上、bot 收不到消息。

机制：
- 前端 ``pages/panel/index.html`` 由 Dashboard 用受限 iframe 加载并注入
  ``window.AstrBotPluginPage`` bridge（先 ``await ready()`` 再调 API）；
- 前端 ``bridge.apiGet/apiPost(path)`` → ``/api/v1/plugins/extensions/<插件名>/<path>``；
- 后端 ``context.register_web_api(route, handler, methods, desc)``，handler 是
  无参数 async 函数，请求经 ``astrbot.api.web.request`` 上下文对象读取；
- route 必须带插件名前缀。metadata 名（astrbot_plugin_prompt_cards）与 @register
  名（prompt_cards）各注册一组，两个前缀都覆盖，谁命中都行。
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

# 面板页路由前缀：metadata 名与 @register 名各一组（不同版本转发用哪个有差异）
PAGE_PREFIXES = (
    "/astrbot_plugin_prompt_cards",
    "/prompt_cards",
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

    # Gemini 安全阈值白名单（键与取值对照 gemini_source 的映射，防任意配置注入）
    SAFETY_KEYS = ("harassment", "hate_speech", "sexually_explicit", "dangerous_content")
    SAFETY_THRESHOLDS = ("BLOCK_NONE", "BLOCK_ONLY_HIGH",
                         "BLOCK_MEDIUM_AND_ABOVE", "BLOCK_LOW_AND_ABOVE")
    # 暧昧模式预设：骚扰与露骨两项放行，其余不动（改的是 Gemini 分类器拦截，
    # 不是模型自身政策——露骨内容 Gemini 仍可能拒答）
    FLIRTY_PRESET = {"harassment": "BLOCK_NONE", "sexually_explicit": "BLOCK_NONE"}

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
            ("/export", self.h_export, ["GET"], "提示词卡面板：导出全部数据"),
            ("/import", self.h_import, ["POST"], "提示词卡面板：导入数据（合并/覆盖）"),
            ("/providers", self.h_providers, ["GET"], "提示词卡面板：提供商列表与当前调参"),
            ("/llm/tune", self.h_llm_tune, ["POST"], "提示词卡面板：温度与 Gemini 安全阈值调参"),
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

    # ---------- 导出 / 导入 ----------
    async def h_export(self):
        from datetime import datetime
        return page_ok({
            "cards": self.plugin._cards,
            "sessions": self.plugin._sessions,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
        })

    async def h_import(self):
        data = await read_json_body()
        incoming_cards = data.get("cards")
        incoming_sessions = data.get("sessions")
        mode = data.get("mode") or "merge"
        if mode not in ("merge", "replace"):
            return page_err("mode 仅支持 merge/replace")
        if not isinstance(incoming_cards, dict) and not isinstance(incoming_sessions, dict):
            return page_err("导入内容为空：需要 cards 或 sessions 字段")

        imported, skipped = 0, []
        if isinstance(incoming_cards, dict):
            if mode == "replace":
                self.plugin._cards.clear()
            for name, card in incoming_cards.items():
                if not isinstance(card, dict):
                    skipped.append(str(name))
                    continue
                payload = dict(card)
                payload["name"] = str(name)
                _, err = apply_card_patch(self.plugin, payload)
                if err:
                    skipped.append(f"{name}（{err}）")
                    continue
                imported += 1
        if isinstance(incoming_sessions, dict):
            clean_sessions = {}
            for umo, cards in incoming_sessions.items():
                if not isinstance(umo, str) or not isinstance(cards, dict):
                    continue
                clean = {n: v for n, v in cards.items()
                         if isinstance(n, str) and isinstance(v, dict)}
                clean_sessions[umo] = clean
            if mode == "replace" or clean_sessions:
                self.plugin._sessions = clean_sessions if mode == "replace" \
                    else {**self.plugin._sessions, **clean_sessions}
        self.plugin._save()
        self._log("info", f"[prompt_cards] 面板导入 {imported} 张卡（模式 {mode}，跳过 {len(skipped)}）")
        return page_ok({"imported": imported, "skipped": skipped, "mode": mode})

    # ---------- 提供商调参（温度 / Gemini 安全阈值） ----------
    def _pm(self):
        """拿 ProviderManager；拿不到（旧版/异常）返回 None。"""
        pm = getattr(self.plugin.context, "provider_manager", None) if self.plugin.context else None
        return pm if callable(getattr(pm, "update_provider", None)) else None

    def _provider_view(self, cfg: dict) -> dict:
        ptype = str(cfg.get("type") or "")
        pid = str(cfg.get("id") or "")
        is_gemini = "gemini" in ptype.lower() or "gemini" in pid.lower()
        return {
            "id": pid,
            "type": ptype,
            "enable": bool(cfg.get("enable")),
            "is_gemini": is_gemini,
            "custom_extra_body": cfg.get("custom_extra_body")
            if isinstance(cfg.get("custom_extra_body"), dict) else {},
            "gm_safety_settings": cfg.get("gm_safety_settings")
            if isinstance(cfg.get("gm_safety_settings"), dict) else None,
        }

    async def h_providers(self):
        pm = self._pm()
        if not pm:
            return page_err("当前 AstrBot 无法访问提供商管理器")
        try:
            providers = [self._provider_view(c) for c in pm.providers_config]
        except Exception as e:
            return page_err(f"读取提供商失败：{type(e).__name__}: {e}")
        return page_ok({"providers": providers,
                        "safety_keys": list(self.SAFETY_KEYS),
                        "thresholds": list(self.SAFETY_THRESHOLDS)})

    async def h_llm_tune(self):
        data = await read_json_body()
        provider_id = str(data.get("provider_id") or "").strip()
        if not provider_id:
            return page_err("缺少 provider_id")
        pm = self._pm()
        if not pm:
            return page_err("当前 AstrBot 无法访问提供商管理器")
        try:
            cfg = pm.get_provider_config_by_id(provider_id)
        except Exception as e:
            return page_err(f"读取提供商失败：{type(e).__name__}: {e}")
        if not cfg:
            return page_err("提供商不存在")

        if "temperature" in data:
            temp = data.get("temperature")
            body = cfg.get("custom_extra_body") if isinstance(cfg.get("custom_extra_body"), dict) else {}
            if temp is None or temp == "":
                body.pop("temperature", None)
            else:
                try:
                    temp = float(temp)
                    if not 0 <= temp <= 2:
                        raise ValueError
                except (TypeError, ValueError):
                    return page_err("温度需在 0~2 之间")
                body["temperature"] = temp
            cfg["custom_extra_body"] = body

        if "gm_safety" in data:
            gm = data.get("gm_safety")
            if gm is None:
                cfg.pop("gm_safety_settings", None)
            elif isinstance(gm, dict):
                clean = {}
                for key in self.SAFETY_KEYS:
                    val = gm.get(key)
                    if val is None or val == "":
                        continue
                    if val not in self.SAFETY_THRESHOLDS:
                        return page_err(f"阈值取值不支持：{key}={val}")
                    clean[key] = val
                if clean:
                    cfg["gm_safety_settings"] = clean
                else:
                    cfg.pop("gm_safety_settings", None)
            else:
                return page_err("gm_safety 需为对象或 null")

        try:
            await pm.update_provider(provider_id, cfg)
        except Exception as e:
            return page_err(f"写入提供商失败：{type(e).__name__}: {e}")
        view = self._provider_view(pm.get_provider_config_by_id(provider_id) or {})
        self._log("info", f"[prompt_cards] 面板调参提供商 {provider_id}（已实时生效）")
        return page_ok({"provider": view})
