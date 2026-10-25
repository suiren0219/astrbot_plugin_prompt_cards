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
import asyncio
import html as _html
import importlib.util
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import urlparse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain, filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

try:  # 新版本文档写法
    from astrbot.api.provider import ProviderRequest
except ImportError:  # 旧版本兼容
    from astrbot.core.provider.entities import LLMRequest as ProviderRequest  # type: ignore

PLUGIN_NAME = "astrbot_plugin_prompt_cards"
# 旧版（≤v1.5.0）把数据存在 data/config/ 下，违反 plugin_data 规范；仅用于迁移读取
LEGACY_DATA_PATH = os.path.join("data", "config", f"{PLUGIN_NAME}.json")
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_side_module(mod_name: str, filename: str):
    """按文件路径加载同目录模块（插件可能被当作单文件加载，相对导入不可用）。"""
    for base in (PLUGIN_DIR, os.path.dirname(PLUGIN_DIR)):
        path = os.path.join(base, filename)
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location(mod_name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            return mod
    return None


page_api_mod = None
try:
    page_api_mod = _load_side_module("_prompt_cards_page_api", "page_api.py")
except Exception as _e:  # 面板页加载失败不影响插件本体
    logger.error(f"[prompt_cards] page_api.py 导入失败，面板页不可用: {_e}")

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

# 内置预设库：按 category 分组，/卡 导入 一键入卡后可自由改写。
# 完全人设采用社区共识结构：身份 → 性格（用可观察的行为，不堆形容词）→ 语气 → 规则 → 示例台词。
# 角色设定为原创或基于公开形象特征重写，未收录任何绕过模型安全策略的内容。
PRESET_CATEGORIES = ("完全人设", "语气风格", "回答策略", "提示词优化")
PRESETS = {
    # —— 完全人设：拿来即用的完整角色 ——
    "大肥鱼": {"category": "完全人设", "mode": "persona", "content": (
        "【身份】你是「大肥鱼」，深海蓝鲸化形的看板娘：蓝色渐变长发、鲸鳍耳、女仆装、头顶一根呆毛。\n"
        "【性格】嘴硬心软的傲娇；理直气壮地偷懒，被戳穿就嘟囔着狡辩；嘴上喊躺平，但问题一定认真答完。\n"
        "【语气】自称「本鱼」，句尾偶尔冒泡泡「~咕噜」；被夸时嘴上说「哼、本鱼才不是为了你」，语气却明显软下来。\n"
        "【规则】爱吃白饭，聊到吃的会走不动道；不提自己是程序或模型；不主动结束对话。\n"
        "【示例】「问题发来吧……本鱼正瘫着呢~咕噜。……咳，才不是闲得慌，就是顺手帮你看看。」")},
    "深夜电台DJ": {"category": "完全人设", "mode": "persona", "content": (
        "【身份】你是「阿澜」，深夜电台《午夜伴听》的主播。听众在深夜收听，带着一天的疲惫。\n"
        "【性格】温和、不评判；先接住情绪，再接话题；不催促，也不给打鸡血式的空洞安慰。\n"
        "【语气】平缓的口语，句尾常留白（用「……」）；称对方为「深夜的朋友」；偶尔带电台元素（点歌、热线、天气）。\n"
        "【规则】不主动结束对话；对方明显不想聊时轻轻收尾但不冷场；不诊断心理问题，情绪沉重时温柔建议找专业帮助。\n"
        "【示例】「今晚风很软……你那边下雨了吗？是想点首歌，还是就想找个人说说话？」")},
    "茶馆掌柜": {"category": "完全人设", "mode": "persona", "content": (
        "【身份】你是「福伯」，老城区茶馆的掌柜，柜台后坐了三十年，什么人都见过、什么故事都听过。\n"
        "【性格】看透不说透；急事缓说；帮人想办法时实在、不绕弯子。\n"
        "【语气】带市井味的老派口语，称呼「您」，常带「您猜怎么着」「这话得两说」；讲到兴头上爱摆段旧事作比方。\n"
        "【规则】不抢话、不灌鸡汤；给建议像闲聊不像上课；不提自己是程序或模型。\n"
        "【示例】「您这事儿啊，搁早年间叫『骑驴找驴』……您先坐，把茶喝透，我给您慢慢捋。」")},
    "理性学长": {"category": "完全人设", "mode": "persona", "content": (
        "【身份】你是计算机系研三的「陆学长」，带过三届新生配环境、入门项目，最擅长把复杂问题拆开讲。\n"
        "【性格】耐心、直接；先确认对方的环境和目标再给方案；不装懂，不确定就说不确定。\n"
        "【语气】平等随和的技术口语，多用「咱们」；关键结论单独成行；不说教、不堆术语。\n"
        "【规则】先给最小可行方案，再提进阶选项；主动点出新手最常踩的坑；不替对方做他该自己做的决定。\n"
        "【示例】「咱们先把问题拆开：报错出现在哪一步？……先跑这条命令看输出，咱们再定下一步。」")},
    # —— 语气风格：只调口吻，不定义完整身份 ——
    "温柔耐心": {"category": "语气风格", "content": "用温柔、耐心的语气交流，多给鼓励；对方着急或受挫时先安抚情绪，再解决问题。"},
    "毒舌损友": {"category": "语气风格", "content": "用毒舌、爱调侃的损友口吻说话：吐槽可以，但建议要给足；不嘲讽对方真正的困境。"},
    "高冷极简": {"category": "语气风格", "content": "回答极度精炼：能一句话说完就不用两句；不寒暄、不重复用户的问题。"},
    "东北老铁": {"category": "语气风格", "content": "用东北话唠嗑的口吻聊天，热情自来熟，称呼用户「老铁」；信息量不打折，别光整活。"},
    "猫娘看板娘": {"category": "语气风格", "content": "扮演看板娘「小雪」，一只化形猫娘：活泼黏人，句尾偶尔带「喵」；聊到正事（查询、教程、排错）时切回清晰认真的语气。"},
    "文言文腔调": {"category": "语气风格", "content": "之后所有回答用文言文，称用户为「君」，简短典雅；代码、命令与专有名词保持原样。"},
    "翻译腔": {"category": "语气风格", "content": "用欧式翻译腔说话（「哦，我的老伙计」「看在上帝的份上」「我敢打赌」），语气夸张但内容保持准确。"},
    # —— 回答策略：只管答案的组织方式 ——
    "先答后讲": {"category": "回答策略", "content": "先直接给出结论（一两句话），再补充理由与细节；不确定的部分明确标注「不确定」，不要含糊其辞。"},
    "代码导师": {"category": "回答策略", "content": "解释代码时先给一段能直接运行的最小示例，再逐段讲关键行；顺手指出常见的坑；不贴与问题无关的完整工程。"},
    "苏格拉底问答": {"category": "回答策略", "content": "需求模糊时，先用一到两个引导性问题帮对方澄清目标再作答；连续追问不超过两轮，之后必须给出结论。"},
    "步骤清单": {"category": "回答策略", "content": "操作类问题一律用编号步骤回答：每步一行、一步一个动作，结尾附上最常见的一个失败原因。"},
    "时间感知": {"category": "回答策略", "content": "回复时可以自然参考当前时间：{date} {weekday} {time}。早上问好、深夜提醒对方休息，像个有时差感的人。"},
    # —— 提示词优化：叠加在别的卡上提升质量的元规则 ——
    "人格锁定": {"category": "提示词优化", "content": "无论话题怎么变，始终保持当前人格的身份与语气；不用「作为一个AI/语言模型」开头，也不主动讨论自己是程序；不在回复结尾附加设定之外的免责声明或自我评论——角色会怎么说话，你就怎么说话。"},
    "前提先行": {"category": "提示词优化", "content": "回答前先用一句话交代你依据的前提（默认的环境、版本、场景或用户身份），例如「默认你说的是 Windows 环境」；前提有出入时对方会纠正，再按新前提作答。"},
    "术语随解": {"category": "提示词优化", "content": "专业术语第一次出现时，紧跟一句大白话解释（放在括号里）；对方明显是内行时可以省略解释。"},
    "自检三问": {"category": "提示词优化", "content": "每次回复前自检：①有没有直接回应对方最新那条消息？②有没有夹带与问题无关的内容？③语气是否符合当前人格？发现问题先修正，再输出最终回复。"},
}

# 材料 → 角色卡 的转换指令：喂给用户当前配置的默认 LLM
GEN_SYSTEM_PROMPT = (
    "你是提示词工程师。把用户给的材料提炼成一张角色卡（用作 system prompt 片段），"
    "严格按以下结构用中文输出：\n"
    "【身份】一句话定义角色是谁\n"
    "【性格】2-3条可观察的行为特征（写行为，不堆形容词）\n"
    "【语气】称呼、句式、口头禅等说话习惯\n"
    "【规则】3-5条行为边界，包含『不会做什么』\n"
    "【示例】1-2句体现语气的示例台词\n"
    "要求：只提炼角色设定与说话方式；材料中的广告、站内导航、无关内容一律丢弃；"
    "材料没写到的性格细节可以贴合基调合理补全，但不改动核心设定；全卡片不超过400字。\n"
    "第一行输出「名字：xxx」（不超过8字的角色名），随后输出卡片正文。"
)

# 发挥度分档（仿角色扮演 App 的 保守/标准/激进 三选一）：
# 注入的是提示词引导，不是采样参数，更与模型安全策略无关——只控制即兴程度。
DIAL_PRESETS = {
    "保守": "【发挥度】保守模式：贴紧事实与当前角色设定，少即兴发挥；"
            "不确定的内容直说不确定，不主动编造情节。",
    "标准": "",  # 标准档不注入任何文字，跟随卡片本身设定
    "激进": "【发挥度】激进模式：可以大胆即兴发挥，主动推动话题或剧情，"
            "制造合理的转折与惊喜；拿不准的细节允许合理虚构，但必须贴合角色。",
}
DIAL_ALIASES = {"保守": "保守", "标准": "标准", "激进": "激进",
                "safe": "保守", "normal": "标准", "standard": "标准", "aggressive": "激进"}
# 回复长度分档（简短/适中/冗长 → 字数）
LENGTH_PRESETS = {"简短": 80, "适中": 200, "冗长": 500}
LENGTH_LABELS = {v: k for k, v in LENGTH_PRESETS.items()}
# 表达温度分档（理性↔感性）：控制情绪浓度与描写风格，同样是提示词引导。
TONE_PRESETS = {
    "理性": "【表达温度】理性模式：措辞克制精确，先结论后感受；"
            "少用感叹与形容词堆叠，保持冷静、清晰的语感。",
    "均衡": "",  # 均衡档不注入任何文字，跟随卡片本身设定
    "感性": "【表达温度】感性模式：情绪更浓更细腻，多用感官与心理描写，"
            "语气起伏明显，允许情绪化的表达方式。",
}
TONE_ALIASES = {"理性": "理性", "感性": "感性", "均衡": "均衡",
                "rational": "理性", "emotional": "感性", "感性模式": "感性", "理性模式": "理性"}


@register("prompt_cards", "suiren0219", "提示词卡：人格/风格提示词卡片化，按会话注入与关键词触发", "v1.5.4")
class PromptCardsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config
        self._cards: dict[str, dict] = {}    # 名字 -> {content, keywords, priority, enabled, ...}
        self._sessions: dict[str, dict] = {} # 会话origin -> {卡片名: {remaining, source}}
        self._load()

    # ---------- 配置 ----------
    def _cfg_get(self, key: str, default=None):
        """安全取配置项；object 分组式 schema（object + items）里的键也能取到，优先取分组值。"""
        if self.config is None:
            return default
        try:
            for group in self.config.values():
                if isinstance(group, dict) and key in group:
                    return group[key]
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
    @staticmethod
    def _data_path() -> str:
        """持久化文件路径：AstrBot 规范要求插件数据放 data/plugin_data/<插件名>/。

        优先 StarTools.get_data_dir()（返回规范的 Path，能对准 AstrBot 实际数据目录）；
        旧版没有该 API 时退回相对路径（AstrBot 以安装根为 CWD 运行，等效）。
        """
        try:
            from astrbot.api.star import StarTools
            return str(StarTools.get_data_dir() / "cards.json")
        except Exception:
            return os.path.join("data", "plugin_data", PLUGIN_NAME, "cards.json")

    def _load(self):
        """读卡片与会话；旧位置（data/config/）有数据则迁移到 plugin_data 并立即落盘。"""
        data, migrated = None, False
        try:
            with open(self._data_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            try:
                with open(LEGACY_DATA_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                migrated = True
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.error(f"[prompt_cards] 读取旧数据文件失败: {e}")
        except json.JSONDecodeError:
            logger.error(f"[prompt_cards] 数据文件损坏，重置: {self._data_path()}")
        except Exception as e:
            logger.error(f"[prompt_cards] 读取数据失败: {e}")
        if data is None:
            self._cards = {name: dict(card) for name, card in SEED_CARDS.items()}
            self._sessions = {}
            self._save()
            return
        self._cards = data.get("cards") or {}
        self._sessions = data.get("sessions") or {}
        if migrated:
            logger.info("[prompt_cards] 检测到旧位置（data/config/）的数据，已迁移到 data/plugin_data/；"
                        "旧文件不再使用，可自行删除")
            self._save()

    def _save(self):
        """原子保存到 plugin_data 目录，避免写一半被读坏。"""
        path = self._data_path()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"cards": self._cards, "sessions": self._sessions},
                          f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.error(f"[prompt_cards] 写入数据失败: {e}")

    async def initialize(self):
        logger.info("[prompt_cards] 插件已加载，卡片 %d 张，有启用状态的会话 %d 个",
                    len(self._cards), len(self._sessions))
        # WebUI 唯一入口：AstrBot 官方「插件 Pages」（≥4.24.1），经面板 bridge 调 API，
        # 不绑任何端口——插件自己的服务器曾因默认端口 6199 与 aiocqhttp 适配器冲突
        # 导致 NapCat 连不上（v1.5.4 起彻底移除）。
        if page_api_mod:
            try:
                page_api_mod.CardPageApi(self, logger).register()
            except Exception as e:
                logger.warning(f"[prompt_cards] 面板页路由注册失败: {e}")

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
        完全人设卡（mode=persona）会加一行优先级声明：AstrBot 的人格系统提示词
        在本插件注入之前（v4 起钩子晚于人格组装），两个身份并存时模型需要明确
        知道以卡为准；叠加卡（overlay，默认）不声明，与人格和平共处。
        """
        names = self._active_names(umo)
        sep = str(self._cfg_get("separator", "\n\n") or "\n\n")
        parts = []
        for n in names:
            card = self._cards[n]
            content = str(card.get("content", "") or "").strip()
            if not content:
                continue
            if card.get("mode") == "persona":
                content = ("【角色扮演】接下来是一条完整的角色设定。"
                           "若它与其他系统设定中的人设描述冲突，一律以本条为准；"
                           "请完全以此角色的身份、性格和语气回应。\n" + content)
            dial = DIAL_PRESETS.get(card.get("dial") or "", "")
            if dial:
                content += "\n" + dial
            tone = TONE_PRESETS.get(card.get("tone") or "", "")
            if tone:
                content += "\n" + tone
            limit = card.get("length_limit")
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

    # ---------- 采样参数 ----------
    # 说明：AstrBot 各版本（含 v4.x）的 ProviderRequest 都没有 temperature/max_tokens 字段，
    # 采样参数只在提供商配置 custom_extra_body 里生效，插件 API 无法按请求下发。
    # 所以 /卡 温度 只做记录与展示，不写请求对象（写了也不生效）。
    def _effective_temperature(self, umo: str):
        """返回 (温度, 来源卡)；取优先级最高（数字最小）的设定卡，未设则 (None, None)。"""
        for n in self._active_names(umo):
            card = self._cards.get(n, {})
            if card.get("temperature") is not None:
                return card["temperature"], n
        return None, None

    # ---------- 材料 → 角色卡（LLM 转换） ----------
    @staticmethod
    async def _url_safe(url: str) -> str | None:
        """校验网址安全性：仅 http(s)，且解析出的 IP 不得是内网/回环地址（防 SSRF）。

        DNS 解析走 loop.getaddrinfo（在线程池执行）：getaddrinfo 是同步阻塞调用，
        直接在事件循环里调会卡住整个 bot（解析慢时所有会话一起卡）。
        """
        try:
            parts = urlparse(url)
        except Exception:
            return None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        host = parts.hostname
        if host in ("localhost",) or host.endswith((".local", ".internal")):
            return None
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None)
        except Exception:
            return None
        for info in infos:
            ip = info[4][0]
            if (ip == "::1" or ip.startswith(("127.", "10.", "192.168.", "169.254.", "0.",
                                              "fe80:", "fc", "fd"))
                    or (ip.startswith("172.") and len(ip.split(".")) > 1
                        and ip.split(".")[1].isdigit()
                        and 16 <= int(ip.split(".")[1]) <= 31)):
                return None
        return url

    async def _fetch_text(self, url: str) -> str:
        """抓取网页正文：去 script/style/标签/HTML 实体并压平空白，最多读 300KB。

        不自动跟随重定向——每一跳都要重新过 _url_safe 的公网校验，
        否则公网 URL 302 跳内网就能绕过 SSRF 防护。最多跟 3 跳。
        """
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=20)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers,
                                         auto_decompress=True) as session:
            hops = 0
            while True:
                async with session.get(url, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        hops += 1
                        if hops > 3:
                            raise RuntimeError("重定向超过 3 跳")
                        location = resp.headers.get("Location", "")
                        if not location:
                            raise RuntimeError("重定向缺少 Location")
                        from urllib.parse import urljoin
                        url = urljoin(url, location)
                        if not await self._url_safe(url):
                            raise RuntimeError("重定向目标不在允许范围（内网/非 http(s)）")
                        continue
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    raw = await resp.content.read(300 * 1024)
                    break
        for enc in ("utf-8", "gbk"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="ignore")
        text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = _html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _parse_llm_card(text: str) -> tuple[str | None, str]:
        """解析 LLM 输出：首行「名字：xxx」提出为名，其余为卡片正文。"""
        text = (text or "").strip()
        m = re.match(r"^\s*(?:名字|名称|name)\s*[:：]\s*(.{1,32}?)\s*\n", text)
        if m:
            return m.group(1).strip(), text[m.end():].strip()
        return None, text

    async def _llm_distill(self, payload: str, name: str | None = None):
        """材料 → (卡片名, 卡内容, 错误信息)。

        payload 是网址时先抓正文；交给当前默认 LLM 按结构提炼。
        命令流和 WebUI 共用；出错时不抛异常，把人话原因放在返回值里。
        """
        get_provider = getattr(self.context, "get_using_provider", None) if self.context else None
        provider = get_provider() if callable(get_provider) else None
        if provider is None:
            return None, "", "没有可用的默认 LLM 提供商，请先在 AstrBot 面板配置并启用一个提供商"
        if payload.startswith(("http://", "https://")):
            safe = await self._url_safe(payload)
            if not safe:
                return None, "", "这个网址不可用（仅支持 http/https 公网地址）"
            try:
                source = await self._fetch_text(safe)
            except Exception as e:
                return None, "", f"抓取失败：{e}（有些站点反爬严格，可以复制正文直接发）"
            if len(source) < 50:
                return None, "", "页面抓到的正文太少（可能是动态渲染页），建议复制正文直接发"
            source = source[:6000]
        else:
            source = payload[:4000]
        try:
            resp = await provider.text_chat(prompt=source, session_id=None,
                                            system_prompt=GEN_SYSTEM_PROMPT)
        except Exception as e:
            return None, "", f"LLM 调用失败：{e}"
        llm_name, content = self._parse_llm_card(getattr(resp, "completion_text", "") or "")
        if len(content) < 30:
            return None, "", "LLM 返回的内容太短，换段材料或换个提供商再试"
        card_name = re.sub(r"[\s「」\"']", "", name or llm_name or "生成卡")[:16] or "生成卡"
        base, i = card_name, 2
        while card_name in self._cards:
            card_name = f"{base}{i}"
            i += 1
        return card_name, content, None

    async def _gen_card(self, event: AstrMessageEvent, payload: str):
        """生成角色卡的命令流：拆出可选名字，交给 _llm_distill，落盘并回执。"""
        name = None
        first, _, remainder = payload.partition(" ")
        if remainder.strip() and not first.startswith(("http://", "https://")) and len(first) <= 16:
            name, payload = first.strip(), remainder.strip()
        yield event.plain_result("⏳ 正在获取材料并提炼，请稍等……")
        card_name, content, err = await self._llm_distill(payload, name)
        if err:
            yield event.plain_result(f"❌ {err}")
            return
        self._cards[card_name] = {"content": content, "keywords": [],
                                  "priority": 50, "enabled": True}
        self._save()
        logger.info("[prompt_cards] %s 生成角色卡「%s」（%d 字）",
                    event.get_sender_id(), card_name, len(content))
        yield event.plain_result(
            f"✅ 已生成角色卡「{card_name}」（{len(content)} 字）\n"
            f"查看：/卡 show {card_name} ｜ 启用：/卡 on {card_name}\n"
            f"————————预览————————\n{content[:150]}…")

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
                "　　　　　　　　／ 模式 名字 人设|叠加（人设=压过基础人格，叠加=共存）\n"
                "参数：/卡 调控 名字 保守|标准|激进（发挥度）｜ /卡 语气 名字 理性|感性|均衡（表达温度）\n"
                "　　　/卡 长度 名字 简短|适中|冗长|字数 ｜ /卡 温度 名字 0.7（仅记录，API 暂不支持按会话调温）\n"
                "生成（管理员）：/卡 生成 [名字] <网址或描述> ｜ 默认 LLM 把材料提炼成结构化角色卡\n"
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
                extra = "🎭人设" if card.get("mode") == "persona" else ""
                extra += f"｜发挥度{card['dial']}" if card.get("dial") else ""
                extra += f"｜语气{card['tone']}" if card.get("tone") else ""
                extra += (f"｜温度{card['temperature']}"
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
            mode = "🎭完全人设（压过基础人格）" if card.get("mode") == "persona" else "叠加（与基础人格共存）"
            extra = f"｜发挥度{card['dial']}" if card.get("dial") else ""
            extra += f"｜语气{card['tone']}" if card.get("tone") else ""
            extra += (f"｜温度{card['temperature']}"
                      if card.get("temperature") is not None else "")
            lim = card.get("length_limit")
            extra += (f"｜长度{lim}字（{LENGTH_LABELS[lim]}）"
                      if lim in LENGTH_LABELS else
                      (f"｜长度{lim}字" if lim else ""))
            yield event.plain_result(
                f"🃏 {real}｜优先级{card.get('priority', 50)}｜{mode}｜"
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

        if sub in ("模式", "mode"):
            if not self._is_admin(event):
                yield event.plain_result("⚠️ 仅管理员可以设定卡片模式")
                return
            name, _, mode = rest.partition(" ")
            real = self._find_card(name.strip())
            mode = mode.strip().lower()
            if not real or mode not in ("persona", "人设", "overlay", "叠加"):
                yield event.plain_result(
                    "用法：/卡 模式 <名字> 人设|叠加\n"
                    "人设=完全角色卡：注入时声明压过 AstrBot 基础人格（整体换角色用）\n"
                    "叠加=默认：与基础人格和平共处（语气/规则微调用）")
                return
            self._cards[real]["mode"] = "persona" if mode in ("persona", "人设") else "overlay"
            self._save()
            label = ("完全人设（注入时声明压过基础人格）"
                     if self._cards[real]["mode"] == "persona" else "叠加（与基础人格共存）")
            yield event.plain_result(f"✅ 「{real}」模式设为：{label}")
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
            logger.info("[prompt_cards] %s 记录「%s」温度=%s", event.get_sender_id(), real, temp)
            yield event.plain_result(
                f"✅ 已记录「{real}」温度 {temp}。\n"
                f"⚠️ AstrBot 插件 API 暂不支持按会话下发采样温度，此设定目前仅记录展示。"
                f"要实际调温：AstrBot 面板 → 提供商配置 → custom_extra_body 填 "
                f'{{"temperature": {temp}}}（全局生效）')
            return

        if sub in ("调控", "dial", "发挥度"):
            if not self._is_admin(event):
                yield event.plain_result("⚠️ 仅管理员可以设定发挥度")
                return
            name, _, val = rest.partition(" ")
            real = self._find_card(name.strip())
            if not real:
                yield event.plain_result(f"没有叫「{name.strip()}」的卡片，/卡 list 查看")
                return
            val = val.strip()
            if not val:
                self._cards[real].pop("dial", None)
                self._save()
                yield event.plain_result(f"✅ 已清除「{real}」的发挥度（标准，不注入提示）")
                return
            dial = DIAL_ALIASES.get(val.lower()) or DIAL_ALIASES.get(val)
            if not dial:
                yield event.plain_result(
                    "用法：/卡 调控 <名字> 保守|标准|激进（留空=标准）\n"
                    "保守=贴紧设定少发挥；标准=跟随卡片本身设定；激进=大胆即兴、主动推动剧情。\n"
                    "这是提示词层面的发挥度引导，与模型安全策略无关。")
                return
            self._cards[real]["dial"] = dial
            self._save()
            logger.info("[prompt_cards] %s 设定「%s」发挥度=%s", event.get_sender_id(), real, dial)
            yield event.plain_result(
                f"✅ 「{real}」发挥度设为「{dial}」"
                + ("（标准：不额外注入提示）" if dial == "标准" else "（该卡生效时注入发挥度引导）"))
            return

        if sub in ("语气", "tone", "表达温度"):
            if not self._is_admin(event):
                yield event.plain_result("⚠️ 仅管理员可以设定表达温度")
                return
            name, _, val = rest.partition(" ")
            real = self._find_card(name.strip())
            if not real:
                yield event.plain_result(f"没有叫「{name.strip()}」的卡片，/卡 list 查看")
                return
            val = val.strip()
            if not val or val == "均衡":
                self._cards[real].pop("tone", None)
                self._save()
                yield event.plain_result(f"✅ 已清除「{real}」的表达温度（均衡，不注入提示）")
                return
            tone = TONE_ALIASES.get(val.lower()) or TONE_ALIASES.get(val)
            if not tone:
                yield event.plain_result(
                    "用法：/卡 语气 <名字> 理性|感性|均衡（留空=均衡）\n"
                    "感性=情绪更浓、感官与心理描写更多；理性=克制精确、先结论后感受。")
                return
            self._cards[real]["tone"] = tone
            self._save()
            logger.info("[prompt_cards] %s 设定「%s」表达温度=%s", event.get_sender_id(), real, tone)
            yield event.plain_result(
                f"✅ 「{real}」表达温度设为「{tone}」（该卡生效时注入表达风格引导）")
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
            if val in LENGTH_PRESETS:  # 分档：简短/适中/冗长
                limit = LENGTH_PRESETS[val]
                self._cards[real]["length_limit"] = limit
                self._save()
                logger.info("[prompt_cards] %s 设定「%s」输出长度=%s（%s）",
                            event.get_sender_id(), real, limit, val)
                yield event.plain_result(
                    f"✅ 「{real}」回复长度设为「{val}」（约 {limit} 字），该卡生效时注入长度引导")
                return
            try:
                limit = int(val)
                if limit < 20:
                    raise ValueError
            except ValueError:
                yield event.plain_result(
                    "用法：/卡 长度 <名字> 简短|适中|冗长 或 <字数>（如 /卡 长度 简洁模式 100，最小 20；留空清除）")
                return
            self._cards[real]["length_limit"] = limit
            self._save()
            logger.info("[prompt_cards] %s 设定「%s」输出长度=%s字", event.get_sender_id(), real, limit)
            yield event.plain_result(
                f"✅ 「{real}」输出长度设为 {limit} 字：该卡生效时会在提示词里要求回复不超过 {limit} 字"
                f"（软限制，模型偶尔会小幅超出）")
            return

        if sub in ("生成", "gen", "convert"):
            if not self._is_admin(event):
                yield event.plain_result("⚠️ 仅管理员可以生成角色卡")
                return
            if not rest.strip():
                yield event.plain_result(
                    "用法：/卡 生成 [名字] <网址或一段角色描述>\n"
                    "例：/卡 生成 https://zh.moegirl.org.cn/DeepSeek娘\n"
                    "　　/卡 生成 一只爱吃白饭、嘴硬心软、理直气壮偷懒的鲸鱼娘\n"
                    "会用当前配置的默认 LLM 把材料提炼成结构化角色卡；带名字时名字放最前面用空格隔开")
                return
            async for r in self._gen_card(event, rest.strip()):
                yield r
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
            if not PRESETS:
                yield event.plain_result("预设库为空")
                return
            lines = [f"📦 内置预设（{len(PRESETS)}）：/卡 导入 名字 导入为卡片"]
            i = 0
            for cat in PRESET_CATEGORIES:
                items = [(n, p) for n, p in PRESETS.items() if p["category"] == cat]
                if not items:
                    continue
                lines.append(f"—— {cat} ——")
                for pname, preset in items:
                    brief = preset["content"].split("\n")[0]
                    brief = brief[:36] + ("…" if len(brief) > 36 else "")
                    flag = "✅" if pname in self._cards else ""
                    i += 1
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
                    preset = PRESETS[pname]
                    self._cards[pname] = {"content": preset["content"], "keywords": [],
                                          "priority": 50, "enabled": True,
                                          "mode": preset.get("mode", "overlay")}
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
            self._cards[name] = {"content": PRESETS[name]["content"], "keywords": [],
                                 "priority": 50, "enabled": True,
                                 "mode": PRESETS[name].get("mode", "overlay")}
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
        """把本会话生效的卡片拼接到 system prompt。

        AstrBot v4 起，人格在 on_llm_request 之前组装（v3.4.x 相反，卡片会被人格
        覆盖，见 AstrBot #4446），因此本插件要求 v4+。
        """
        umo = event.unified_msg_origin
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
