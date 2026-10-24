"""AstrBot Star 插件：提示词卡（人格/风格提示词卡片化管理与注入）。

把多套 System Prompt 存成「卡片」，按会话开关、按关键词自动激活，
在每次 LLM 请求前把启用的卡片拼接注入到 system prompt：

* 卡内容完全由使用者自己维护（人格设定、语气文风、格式要求等常规提示词）；
* 本插件只做卡片管理与注入机制，不内置、也不针对绕过模型安全策略的内容。

命令（前缀 / 可省略，子命令与参数之间用空格）：
  /卡 help                  查看帮助
  /卡 list                  列出所有卡片与当前会话的启用状态
  /卡 show <名字>           查看卡片内容
  /卡 add <名字> <内容>     新建卡片（内容可换行）（管理员）
  /卡 set <名字> <内容>     修改卡片内容（管理员）
  /卡 kw <名字> <词,词>     设置触发关键词（逗号分隔，留空清除）（管理员）
  /卡 pri <名字> <数字>     设置注入优先级（数字小的先注入）（管理员）
  /卡 del <名字>            删除卡片（管理员）
  /卡 on <名字>             在当前会话启用卡片
  /卡 off <名字>            在当前会话停用卡片
  /卡 clear                 停用当前会话的所有卡片
  /卡 peek                  预览当前会话实际注入的提示词

注入时机：on_llm_request 钩子，追加（或前置）到现有人格 system prompt。
关键词触发：监听所有消息，命中卡片关键词即在当前会话自动激活，
N 条消息后自动失效（可配置），类似「世界书」的条件注入。
"""
import json
import os
import re
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain, filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

try:  # 新版本文档写法
    from astrbot.api.provider import ProviderRequest
except ImportError:  # 旧版本兼容
    from astrbot.core.provider.entities import LLMRequest as ProviderRequest  # type: ignore

PLUGIN_NAME = "astrbot_plugin_prompt_cards"
DATA_PATH = os.path.join("data", "config", f"{PLUGIN_NAME}.json")

CARD_NAME_MAX = 32  # 卡片名字长度上限（不含内容），防止一整段话被当成名字

# 首次运行的示例卡：只示范「语气/文风」类常规提示词的写法，可随意改删
SEED_CARDS = {
    "示例_简洁模式": {
        "content": "回答尽量精炼：先给结论，再给最关键的一两条理由；"
                   "与问题无关的展开、免责声明一律不写。",
        "keywords": [],
        "priority": 20,
        "enabled": True,
    },
    "示例_温柔耐心": {
        "content": "用温柔、耐心的语气交流，多给鼓励；对方着急或受挫时先安抚情绪，再解决问题。",
        "keywords": [],
        "priority": 30,
        "enabled": True,
    },
}

# 内置预设库：人格/语气/回答策略模板，/卡 导入 一键入卡后可自由改写
PRESETS = {
    # —— 语气与人格 ——
    "温柔耐心": "用温柔、耐心的语气交流，多给鼓励；对方着急或受挫时先安抚情绪，再解决问题。",
    "毒舌损友": "用毒舌、爱调侃的损友口吻说话：吐槽可以，但建议要给足；不嘲讽对方真正的困境。",
    "高冷极简": "回答极度精炼：能一句话说完就不用两句；不寒暄、不重复用户的问题。",
    "东北老铁": "用东北话唠嗑的口吻聊天，热情自来熟，称呼用户「老铁」；信息量不打折，别光整活。",
    "猫娘看板娘": "扮演看板娘「小雪」，一只化形猫娘：活泼黏人，句尾偶尔带「喵」；聊到正事（查询、教程、排错）时切回清晰认真的语气。",
    "文言文腔调": "之后所有回答用文言文，称用户为「君」，简短典雅；代码、命令与专有名词保持原样。",
    "翻译腔": "用欧式翻译腔说话（「哦，我的老伙计」「看在上帝的份上」「我敢打赌」），语气夸张但内容保持准确。",
    # —— 回答策略 ——
    "先答后讲": "先直接给出结论（一两句话），再补充理由与细节；不确定的部分明确标注「不确定」，不要含糊其辞。",
    "代码导师": "解释代码时先给一段能直接运行的最小示例，再逐段讲关键行；顺手指出常见的坑；不贴与问题无关的完整工程。",
    "苏格拉底问答": "需求模糊时，先用一到两个引导性问题帮对方澄清目标再作答；连续追问不超过两轮，之后必须给出结论。",
    "步骤清单": "操作类问题一律用编号步骤回答：每步一行、一步一个动作，结尾附上最常见的一个失败原因。",
    "时间感知": "回复时可以自然参考当前时间：{date} {weekday} {time}。早上问好、深夜提醒对方休息，像个有时差感的人。",
}


@register("prompt_cards", "suiren0219", "提示词卡：人格/风格提示词卡片化，按会话注入与关键词触发", "v1.0.0")
class PromptCardsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config
        self._cards: dict[str, dict] = {}    # 名字 -> {content, keywords, priority, enabled}
        self._sessions: dict[str, dict] = {} # 会话origin -> {卡片名: {remaining, source}}
        self._load()

    # ---------- 配置 ----------
    def _cfg_get(self, key: str, default=None):
        """安全取配置项；config 为 None（旧版本不注入配置）时用默认值。"""
        if self.config is None:
            return default
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _is_admin(self, event) -> bool:
        """管理员判定：优先 event.is_admin()，失败时回退到配置的 admin_ids 名单。"""
        if not self._cfg_get("admin_only_mutate", True):
            return True
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        raw = str(self._cfg_get("admin_ids", "") or "")
        ids = {x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()}
        return str(event.get_sender_id()) in ids

    # ---------- 数据读写 ----------
    def _load(self):
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._cards = data.get("cards") or {}
            self._sessions = data.get("sessions") or {}
        except (FileNotFoundError, json.JSONDecodeError):
            self._cards = {name: dict(card) for name, card in SEED_CARDS.items()}
            self._sessions = {}
            self._save()
        except Exception as e:
            logger.error(f"[prompt_cards] 读取数据失败: {e}")
            self._cards, self._sessions = {}, {}

    def _save(self):
        """原子保存，避免写一半被读坏。"""
        try:
            os.makedirs(os.path.dirname(DATA_PATH) or ".", exist_ok=True)
            tmp = DATA_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"cards": self._cards, "sessions": self._sessions},
                          f, ensure_ascii=False, indent=2)
            os.replace(tmp, DATA_PATH)
        except Exception as e:
            logger.error(f"[prompt_cards] 写入数据失败: {e}")

    async def initialize(self):
        logger.info("[prompt_cards] 插件已加载，卡片 %d 张，有启用状态的会话 %d 个",
                    len(self._cards), len(self._sessions))

    async def terminate(self):
        self._save()

    # ---------- 指令解析 ----------
    @staticmethod
    def _split_cmd(event) -> tuple[str, str]:
        """返回 (子命令, 剩余原文)。兼容带 / 前缀；命令词「卡」可能保留也可能被框架去掉。

        例：消息「/卡 add 新卡 你好」 -> ("add", "新卡 你好")；「/卡」 -> ("help", "")。
        """
        text = (event.message_str or "").strip().lstrip("/").strip()
        for prefix in ("卡", "pcard"):
            if text == prefix:
                return "help", ""
            if text.startswith(prefix + " ") or text.startswith(prefix + "\n"):
                text = text[len(prefix):].lstrip()
                break
        sub, _, rest = text.partition(" ")
        return (sub.strip() or "help"), rest.strip()

    @staticmethod
    def _name_content(rest: str) -> tuple[str, str]:
        """把「名字 内容…」拆开：名字取首行首个分词，内容=同行剩余+后续所有行（保留换行）。"""
        lines = rest.split("\n")
        name, _, first = lines[0].partition(" ")
        name = name.strip()
        content = "\n".join([first] + lines[1:]).strip()
        return name, content

    def _find_card(self, name: str):
        """按名字找卡片，找不到时精确名优先、再退回唯一前缀匹配。"""
        if name in self._cards:
            return name
        hits = [n for n in self._cards if n.startswith(name)] if name else []
        return hits[0] if len(hits) == 1 else None

    @staticmethod
    def _fill_vars(text: str, event=None) -> str:
        """卡片内容里的变量插值：{user} {time} {date} {weekday}，注入时替换。

        只做精确子串替换，不跑 format：卡内容里出现其他花括号也不会炸。
        """
        if not text or "{" not in text:
            return text
        now = datetime.now()
        name = ""
        if event is not None:
            try:
                name = event.get_sender_name() or ""
            except Exception:
                name = ""
        for k, v in (("{user}", name),
                     ("{time}", now.strftime("%H:%M")),
                     ("{date}", now.strftime("%Y-%m-%d")),
                     ("{weekday}", "星期" + "一二三四五六日"[now.weekday()])):
            text = text.replace(k, v)
        return text

    @staticmethod
    def _kw_hit(kw: str, text: str) -> bool:
        """关键词命中；re: 前缀按正则匹配（正则写错只影响该条关键词，不抛出）。"""
        if kw.startswith("re:"):
            try:
                return re.search(kw[3:], text) is not None
            except re.error:
                return False
        return kw in text

    # ---------- 会话启用状态 ----------
    def _sess(self, umo: str) -> dict:
        return self._sessions.setdefault(umo, {})

    def _active_names(self, umo: str) -> list[str]:
        """当前会话生效的卡片名（全局卡 + 本会话启用卡），按优先级升序（小的先注入）。"""
        sess = self._sessions.get(umo) or {}
        names = {n for n, card in self._cards.items()
                 if card.get("enabled", True) and (card.get("global") or n in sess)}
        return sorted(names, key=lambda n: self._cards[n].get("priority", 50))

    def _build_injection(self, umo: str) -> tuple[str, list[str]]:
        """拼出实际注入文本，返回 (文本, 生效卡片名列表)。

        设了输出长度的卡会在内容后附带一句长度要求——控制回复文字长度
        靠提示词引导（max_tokens 只会把话说砍半截，管不住啰嗦）。
        """
        names = self._active_names(umo)
        sep = str(self._cfg_get("separator", "\n\n") or "\n\n")
        parts = []
        for n in names:
            content = str(self._cards[n].get("content", "") or "").strip()
            if not content:
                continue
            limit = self._cards[n].get("length_limit")
            if limit:
                content += f"\n【输出长度】该卡生效期间，回复保持在 {limit} 字以内。"
            parts.append(content)
        return sep.join(parts), names

    def _tick(self, umo: str):
        """关键词激活的卡片按条数倒计时；手动 /卡 on 的永不自动失效。"""
        sess = self._sessions.get(umo)
        if not sess:
            return
        expired = []
        for name, state in sess.items():
            remaining = (state or {}).get("remaining")
            if remaining is None:
                continue
            remaining -= 1
            if remaining <= 0:
                expired.append(name)
            else:
                state["remaining"] = remaining
        for name in expired:
            del sess[name]
            logger.info("[prompt_cards] 会话 %s 的卡片「%s」已到期限自动停用", umo, name)
        if expired:
            self._save()

    # ---------- 采样参数（温度）与输出长度 ----------
    def _effective_temperature(self, umo: str):
        """返回 (温度, 来源卡)；取优先级最高（数字最小）的设定卡，未设则 (None, None)。

        输出长度不走这里：它随卡内容注入提示词，见 _build_injection。
        """
        for n in self._active_names(umo):
            card = self._cards.get(n, {})
            if card.get("temperature") is not None:
                return card["temperature"], n
        return None, None

    def _apply_sampling(self, req, umo: str) -> None:
        """把生效卡片的温度设定写进 LLM 请求；没设定则跟随人格/模型默认。"""
        temp, _src = self._effective_temperature(umo)
        if temp is None:
            return
        try:
            self._set_req_param(req, "temperature", float(temp))
        except Exception as e:
            logger.warning("[prompt_cards] 温度写入失败: %s", e)

    def _set_req_param(self, req, key: str, value) -> None:
        """兼容不同 AstrBot 版本的请求对象：优先写 dataclass 字段，退而写 model_config。"""
        if hasattr(req, key):
            setattr(req, key, value)
        elif hasattr(req, "model_config") and isinstance(getattr(req, "model_config"), dict):
            req.model_config[key] = value
        else:
            if not getattr(self, "_sampling_warned", False):
                self._sampling_warned = True
                logger.warning("[prompt_cards] 当前 AstrBot 的请求对象不含 %s 字段，"
                               "卡片温度设定未生效（建议升级 AstrBot）", key)

    # ---------- 指令入口 ----------
    @filter.command("卡")
    async def cmd_card(self, event: AstrMessageEvent):
        """提示词卡管理入口：/卡 help 查看全部子命令"""
        async for r in self._h_card(event):
            yield r

    async def _h_card(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        sub, rest = self._split_cmd(event)

        if sub in ("help", "帮助"):
            yield event.plain_result(
                "🃏 提示词卡：把多套提示词存成卡片，按会话开关、按关键词自动注入\n"
                "查看：/卡 list ｜ /卡 show 名字 ｜ /卡 peek 预览本会话注入内容\n"
                "启用：/卡 on 名字 ｜ 停用：/卡 off 名字 ｜ 全停：/卡 clear\n"
                "预设：/卡 预设 查看内置模板 ｜ /卡 导入 名字 一键入卡（「导入 全部」全要）\n"
                "管理（管理员）：/卡 add 名字 内容 ／ set ／ kw 名字 词1,词2（支持 re:正则）\n"
                "　　　　　　　　／ pri 名字 数字 ／ del 名字 ／ 全局 on|off 名字\n"
                "参数（管理员）：/卡 温度 名字 0.7（采样温度）｜ /卡 长度 名字 300（回复字数上限，留空清除）\n"
                "卡片内容支持变量：{user} {time} {date} {weekday}，注入时自动替换\n"
                "关键词卡：命中关键词的会话自动启用该卡，N 条消息后自动失效（面板可调）")
            return

        if sub in ("list", "列表"):
            if not self._cards:
                yield event.plain_result("还没有任何卡片，用 /卡 add 名字 内容 新建")
                return
            sess = self._sessions.get(umo) or {}
            lines = [f"🃏 提示词卡（{len(self._cards)}）："]
            for i, (name, card) in enumerate(
                    sorted(self._cards.items(), key=lambda kv: kv[1].get("priority", 50)), 1):
                kws = "、".join(card.get("keywords") or []) or "无"
                flag = "" if card.get("enabled", True) else "（已全局停用）"
                extra = (f"｜温度{card['temperature']}"
                         if card.get("temperature") is not None else "")
                extra += (f"｜长度{card['length_limit']}字"
                          if card.get("length_limit") else "")
                if card.get("global"):
                    state = "🌐全局生效"
                elif name in sess:
                    state = "✅本会话生效中"
                else:
                    state = "未启用"
                lines.append(f"{i}. {name}｜优先级{card.get('priority', 50)}｜"
                             f"关键词:{kws}｜{state}{flag}{extra}")
            yield event.plain_result("\n".join(lines))
            return

        if sub in ("show", "看"):
            name, _ = self._name_content(rest)
            real = self._find_card(name)
            if not real:
                yield event.plain_result(f"没有叫「{name}」的卡片，/卡 list 查看")
                return
            card = self._cards[real]
            kws = "、".join(card.get("keywords") or []) or "无"
            extra = (f"｜温度{card['temperature']}"
                     if card.get("temperature") is not None else "")
            extra += (f"｜长度{card['length_limit']}字"
                      if card.get("length_limit") else "")
            yield event.plain_result(
                f"🃏 {real}｜优先级{card.get('priority', 50)}｜"
                f"{'启用' if card.get('enabled', True) else '全局停用'}｜关键词:{kws}{extra}\n"
                f"————————\n{card.get('content', '')}")
            return

        if sub in ("add", "new", "新建"):
            if not self._is_admin(event):
                yield event.plain_result("⚠️ 仅管理员可以新建卡片")
                return
            name, content = self._name_content(rest)
            if not name or len(name) > CARD_NAME_MAX or " " in name:
                yield event.plain_result(f"用法：/卡 add <名字> <内容>（名字≤{CARD_NAME_MAX}字、不含空格，内容跟在后面可换行）")
                return
            if not content:
                yield event.plain_result("内容不能为空。例：/卡 add 简洁模式 回答先给结论，再给理由")
                return
            if name in self._cards:
                yield event.plain_result(f"已有卡片「{name}」，想改内容用 /卡 set {name} 新内容")
                return
            self._cards[name] = {"content": content, "keywords": [],
                                 "priority": 50, "enabled": True}
            self._save()
            logger.info("[prompt_cards] %s 新建卡片「%s」（%d 字）", event.get_sender_id(), name, len(content))
            yield event.plain_result(f"✅ 已新建「{name}」（优先级50）。"
                                     f"本会话启用：/卡 on {name}；"
                                     f"设触发关键词：/卡 kw {name} 词1,词2")
            return

        if sub in ("set", "edit", "改"):
            if not self._is_admin(event):
                yield event.plain_result("⚠️ 仅管理员可以修改卡片")
                return
            name, content = self._name_content(rest)
            real = self._find_card(name)
            if not real:
                yield event.plain_result(f"没有叫「{name}」的卡片，/卡 list 查看")
                return
            if not content:
                yield event.plain_result(f"用法：/卡 set {real} <新内容>")
                return
            self._cards[real]["content"] = content
            self._save()
            yield event.plain_result(f"✅ 已更新「{real}」")
            return

        if sub in ("kw", "关键词"):
            if not self._is_admin(event):
                yield event.plain_result("⚠️ 仅管理员可以修改关键词")
                return
            name, kws_raw = self._name_content(rest)
            real = self._find_card(name)
            if not real:
                yield event.plain_result(f"没有叫「{name}」的卡片，/卡 list 查看")
                return
            kws = [k.strip() for k in re.split(r"[,，、\s]+", kws_raw) if k.strip()]
            self._cards[real]["keywords"] = kws
            self._save()
            if kws:
                hint = "命中任一关键词的会话会自动启用这张卡"
                if not self._cfg_get("keyword_trigger", True):
                    hint += "（注意：面板里关键词触发当前是关闭的）"
                yield event.plain_result(f"✅ 「{real}」触发关键词：{'、'.join(kws)}\n{hint}")
            else:
                yield event.plain_result(f"✅ 已清除「{real}」的触发关键词")
            return

        if sub in ("pri", "优先级"):
            if not self._is_admin(event):
                yield event.plain_result("⚠️ 仅管理员可以修改优先级")
                return
            name, _, num = rest.partition(" ")
            real = self._find_card(name.strip())
            if not real or not num.strip().lstrip("-").isdigit():
                yield event.plain_result("用法：/卡 pri <名字> <数字>（数字小的先注入）")
                return
            self._cards[real]["priority"] = int(num.strip())
            self._save()
            yield event.plain_result(f"✅ 「{real}」优先级已设为 {num.strip()}")
            return

        if sub in ("del", "rm", "删除"):
            if not self._is_admin(event):
                yield event.plain_result("⚠️ 仅管理员可以删除卡片")
                return
            name, _ = self._name_content(rest)
            real = self._find_card(name)
            if not real:
                yield event.plain_result(f"没有叫「{name}」的卡片，/卡 list 查看")
                return
            del self._cards[real]
            for sess in self._sessions.values():
                sess.pop(real, None)
            self._save()
            yield event.plain_result(f"✅ 已删除「{real}」")
            return

        if sub in ("on", "开", "启用"):
            name, _ = self._name_content(rest)
            real = self._find_card(name)
            if not real:
                yield event.plain_result(f"没有叫「{name}」的卡片，/卡 list 查看")
                return
            if not self._cards[real].get("enabled", True):
                yield event.plain_result(f"「{real}」已被管理员全局停用，先在面板或让管理员处理")
                return
            self._sess(umo)[real] = {"remaining": None, "source": "manual"}
            self._save()
            yield event.plain_result(f"✅ 本会话已启用「{real}」（发 /卡 off {real} 停用）")
            return

        if sub in ("off", "关", "停用"):
            name, _ = self._name_content(rest)
            real = self._find_card(name)
            if real and real in self._sess(umo):
                del self._sess(umo)[real]
                self._save()
                yield event.plain_result(f"✅ 本会话已停用「{real}」")
            else:
                yield event.plain_result("该卡片在当前会话本来就没启用")
            return

        if sub in ("clear", "全停"):
            n = len(self._sess(umo))
            self._sessions[umo] = {}
            self._save()
            yield event.plain_result(f"✅ 已停用本会话的 {n} 张卡片")
            return

        if sub == "全局":
            if not self._is_admin(event):
                yield event.plain_result("⚠️ 仅管理员可以设置全局卡")
                return
            action, _, name_raw = rest.partition(" ")
            real = self._find_card(name_raw.strip())
            if action.strip() not in ("on", "off", "开", "关") or not real:
                yield event.plain_result("用法：/卡 全局 on|off <名字>\n全局卡对所有会话默认生效，不必逐群 /卡 on")
                return
            on = action.strip() in ("on", "开")
            self._cards[real]["global"] = on
            self._save()
            logger.info("[prompt_cards] %s 将「%s」设为全局=%s", event.get_sender_id(), real, on)
            yield event.plain_result(
                f"✅ 「{real}」已改为{'全局生效（所有会话默认启用）' if on else '仅手动启用'}")
            return

        if sub in ("温度", "temp"):
            if not self._is_admin(event):
                yield event.plain_result("⚠️ 仅管理员可以设定温度")
                return
            name, _, val = rest.partition(" ")
            real = self._find_card(name.strip())
            if not real:
                yield event.plain_result(f"没有叫「{name.strip()}」的卡片，/卡 list 查看")
                return
            val = val.strip()
            if not val:
                self._cards[real].pop("temperature", None)
                self._save()
                yield event.plain_result(f"✅ 已清除「{real}」的温度（跟随人格/模型默认）")
                return
            try:
                temp = float(val)
                if not 0 <= temp <= 2:
                    raise ValueError
            except ValueError:
                yield event.plain_result("用法：/卡 温度 <名字> <0~2>（留空清除；越低越稳定，越高越发散）")
                return
            self._cards[real]["temperature"] = temp
            self._save()
            logger.info("[prompt_cards] %s 设定「%s」温度=%s", event.get_sender_id(), real, temp)
            yield event.plain_result(
                f"✅ 「{real}」温度设为 {temp}。该卡生效时与其他卡的设定冲突，"
                f"优先级最高（数字最小）的说了算")
            return

        if sub in ("长度", "len", "输出长度"):
            if not self._is_admin(event):
                yield event.plain_result("⚠️ 仅管理员可以设定输出长度")
                return
            name, _, val = rest.partition(" ")
            real = self._find_card(name.strip())
            if not real:
                yield event.plain_result(f"没有叫「{name.strip()}」的卡片，/卡 list 查看")
                return
            val = val.strip()
            if not val:
                self._cards[real].pop("length_limit", None)
                self._save()
                yield event.plain_result(f"✅ 已清除「{real}」的输出长度（不再限制）")
                return
            try:
                limit = int(val)
                if limit < 20:
                    raise ValueError
            except ValueError:
                yield event.plain_result(
                    "用法：/卡 长度 <名字> <字数>（如 /卡 长度 简洁模式 100，最小 20；留空清除）")
                return
            self._cards[real]["length_limit"] = limit
            self._save()
            logger.info("[prompt_cards] %s 设定「%s」输出长度=%s字", event.get_sender_id(), real, limit)
            yield event.plain_result(
                f"✅ 「{real}」输出长度设为 {limit} 字：该卡生效时会在提示词里要求回复不超过 {limit} 字"
                f"（软限制，模型偶尔会小幅超出）")
            return

        if sub in ("peek", "预览"):
            injection, names = self._build_injection(umo)
            if not injection:
                yield event.plain_result(
                    "当前会话没有生效的卡片。\n用 /卡 on 名字 启用，或给卡片设关键词自动触发")
                return
            pos = "前" if self._cfg_get("inject_position", "append") == "prepend" else "后"
            try:
                limit = int(self._cfg_get("max_inject_chars", 0) or 0)
            except (TypeError, ValueError):
                limit = 0
            note = f"（共 {len(injection)} 字，注入在人格提示词{pos}）"
            if limit and len(injection) > limit:
                note += f"\n⚠️ 超过面板设定的 {limit} 字：上下文可能吃紧，建议精简卡片或调高上限"
            temp, tsrc = self._effective_temperature(umo)
            if temp is not None:
                note += f"\n🎛 温度 {temp}（来自「{tsrc}」）；输出长度见注入内容里的【输出长度】行"
            yield event.plain_result(
                f"📋 本会话生效：{'、'.join(names)}{note}\n"
                f"————————以下为注入内容————————\n{injection}")
            return

        if sub in ("预设", "preset", "模板"):
            if not self._cards and not PRESETS:
                yield event.plain_result("预设库为空")
                return
            lines = [f"📦 内置预设（{len(PRESETS)}）：/卡 导入 名字 导入为卡片"]
            for i, (pname, content) in enumerate(PRESETS.items(), 1):
                brief = content.split("\n")[0]
                brief = brief[:40] + ("…" if len(brief) > 40 else "")
                flag = "（已导入）" if pname in self._cards else ""
                lines.append(f"{i}. {pname}{flag}：{brief}")
            yield event.plain_result("\n".join(lines))
            return

        if sub in ("导入", "import"):
            if not self._is_admin(event):
                yield event.plain_result("⚠️ 仅管理员可以导入预设")
                return
            name = rest.strip()
            if not name:
                yield event.plain_result("用法：/卡 导入 <预设名>（/卡 预设 查看全部；「/卡 导入 全部」一次全要）")
                return
            if name in ("全部", "all"):
                added = [p for p in PRESETS if p not in self._cards]
                skipped = [p for p in PRESETS if p in self._cards]
                for pname in added:
                    self._cards[pname] = {"content": PRESETS[pname], "keywords": [],
                                          "priority": 50, "enabled": True}
                self._save()
                logger.info("[prompt_cards] %s 导入预设 %d 张（跳过 %d）",
                            event.get_sender_id(), len(added), len(skipped))
                msg = f"✅ 已导入 {len(added)} 张预设卡"
                if added:
                    msg += "：" + "、".join(added)
                if skipped:
                    msg += f"\n☑️ 已存在跳过：{'、'.join(skipped)}"
                msg += "\n本会话试一下：/卡 on 文言文腔调"
                yield event.plain_result(msg)
                return
            if name not in PRESETS:
                yield event.plain_result(f"没有预设「{name}」，/卡 预设 查看全部")
                return
            if name in self._cards:
                yield event.plain_result(f"已有卡片「{name}」，想改内容用 /卡 set {name} 新内容")
                return
            self._cards[name] = {"content": PRESETS[name], "keywords": [],
                                 "priority": 50, "enabled": True}
            self._save()
            logger.info("[prompt_cards] %s 导入预设「%s」", event.get_sender_id(), name)
            yield event.plain_result(f"✅ 已导入预设「{name}」，本会话启用：/卡 on {name}")
            return

        yield event.plain_result(f"没看懂子命令「{sub}」。发 /卡 help 查看用法")

    # ---------- 关键词触发 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def keyword_watcher(self, event: AstrMessageEvent):
        """命中关键词即在当前会话激活对应卡片（世界书式条件注入）。

        只做激活，不拦截消息：正常管线继续走，同一条消息触发的 LLM 请求
        就会带上这张卡。已是启用状态时不重复激活、不刷新倒计时。
        """
        if not self._cfg_get("keyword_trigger", True):
            return
        text = (event.message_str or "").strip().lstrip("/")
        if not text or text.startswith("卡"):
            return  # 空消息和本插件指令不参与匹配
        if event.get_extra("parsed_params") is not None:
            return  # 已被命令管线接手的消息不参与匹配
        umo = event.unified_msg_origin
        sess = self._sess(umo)
        expire = self._cfg_get("keyword_expire_messages", 30)
        try:
            expire = max(0, int(expire or 0))
        except (TypeError, ValueError):
            expire = 30
        hit = []
        for name, card in self._cards.items():
            if name in sess or not card.get("enabled", True):
                continue
            kws = card.get("keywords") or []
            if kws and any(k and self._kw_hit(k, text) for k in kws):
                sess[name] = {"remaining": expire if expire > 0 else None,
                              "source": "keyword"}
                hit.append(name)
        if not hit:
            return
        self._save()
        logger.info("[prompt_cards] 关键词命中，会话 %s 激活: %s", umo, "、".join(hit))
        if self._cfg_get("keyword_notify", False):
            try:
                await self.context.send_message(
                    umo, MessageChain().message(f"🃏 关键词命中，已启用：{'、'.join(hit)}"))
            except Exception as e:
                logger.warning(f"[prompt_cards] 触发提示发送失败: {e}")

    # ---------- LLM 请求注入 ----------
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """把本会话生效的卡片拼接到 system prompt，并应用采样设定。"""
        umo = event.unified_msg_origin
        self._apply_sampling(req, umo)
        injection, names = self._build_injection(umo)
        if not injection:
            self._tick(umo)  # 没有内容也照常倒计时，避免空内容卡片永不过期
            return
        sep = str(self._cfg_get("separator", "\n\n") or "\n\n")
        injection = self._fill_vars(injection, event)
        try:
            limit = int(self._cfg_get("max_inject_chars", 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit and len(injection) > limit:
            logger.debug("[prompt_cards] 注入内容 %d 字超过面板上限 %d", len(injection), limit)
        sp = req.system_prompt or ""
        if self._cfg_get("inject_position", "append") == "prepend":
            req.system_prompt = injection + sep + sp
        else:
            req.system_prompt = sp + sep + injection
        self._anchor_context(req)
        logger.debug("[prompt_cards] 已注入 %d 张卡片: %s", len(names), "、".join(names))
        self._tick(umo)

    def _anchor_context(self, req) -> None:
        """长对话锚点：在最后一条用户消息前插一句提醒，缓解超长对话的人格漂移。

        只在面板开启 context_anchor 时生效；contexts 结构不符合预期时静默跳过。
        """
        if not self._cfg_get("context_anchor", False):
            return
        try:
            ctx = getattr(req, "contexts", None)
            if not isinstance(ctx, list) or not ctx:
                return
            anchor = str(self._cfg_get(
                "context_anchor_text",
                "（系统提醒：请持续保持系统提示词中设定的人设与语气。）") or "")
            if not anchor:
                return
            msg = {"role": "system", "content": anchor}
            if isinstance(ctx[-1], dict) and ctx[-1].get("role") == "user":
                ctx.insert(len(ctx) - 1, msg)
            else:
                ctx.append(msg)
        except Exception as e:
            logger.debug("[prompt_cards] 锚点注入跳过: %s", e)
