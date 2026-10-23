# -*- coding: utf-8 -*-
"""
前瞻资讯分类与时效性提取。

把各来源抓取的公告条目分为七类前瞻资讯（直播/版本前瞻/兑换码/联动/
周边/快闪/漫展），排除游戏内公告、系统维护等非前瞻内容，并提取时效性
信息（兑换码截止时间、活动起止日期、过期判定等）。

分类口径（与《游戏前瞻资讯汇总》目标格式一致）：
  - 前瞻直播 / 特别节目：版本前瞻直播、前瞻通讯、发布会等
  - 版本前瞻内容：版本 PV、新版本导览、研发通讯、新角色宣传物料
  - 限时兑换码：各类兑换码 / 礼包码 / 福利码及其有效期
  - 线下联动活动：品牌联动、线下主题合作（含游戏内联动，标注地点）
  - 官方周边上新：周边商品、旗舰店、手办、设定集、预售贩售
  - 快闪店：快闪店 / 主题店 / 限定店
  - 漫展展会：漫展、展会、参展、嘉年华、FES 等

排除口径：系统维护 / 停机 / 闪断、处罚公示、问题修复与补偿、
未成年人限时与防沉迷、卡池与频次等日常游戏内运营公告。
"""

import re
from datetime import datetime, timedelta

# ---------- 七类前瞻资讯（顺序即归类优先级） ----------

CATEGORY_DEFS = [
    ("livestream",       "📺 前瞻直播 / 特别节目"),
    ("version_preview",  "🗺️ 版本前瞻内容"),
    ("redeem_code",      "🎁 限时兑换码"),
    ("offline_collab",   "🤝 线下联动活动"),
    ("merch",            "🛍️ 官方周边上新"),
    ("popup",            "⚡ 快闪店活动"),
    ("expo",             "🎪 漫展 / 展会"),
]

KEYWORDS = {
    "livestream": ["前瞻直播", "前瞻特别节目", "前瞻", "特别节目", "特别通讯",
                   "发布会", "直播预告", "直播开启"],
    "version_preview": ["版本前瞻", "新版本导览", "研发通讯", "研发终端",
                        "开发者日志", "版本情报", "版本PV", "PV", "导览",
                        "角色PV", "共鸣者PV", "展示PV", "版本创作征集",
                        "新版本情报", "版本优化前瞻", "通讯", "PV｜",
                        "活动预告", "即将开启", "限时活动", "主线更新",
                        "祈愿", "跃迁", "卡池", "福利活动", "活动说明",
                        "版本活动", "双倍", "版本前瞻特别节目",
                        "新角色", "新剧情", "新玩法", "前瞻节目"],
    "redeem_code": ["兑换码", "礼包码", "福利码", "CDK", "cdkey", "口令码",
                    "福利兑换", "通用码", "兑换", "福利码"],
    "offline_collab": ["联动", "联名", "合作", "×"],
    "merch": ["周边", "旗舰店", "天猫", "京东", "手办", "设定集", "预售",
              "米游铺", "货全杂货铺", "贩售", "售卖", "尾款", "补款",
              "谷子", "补货", "官方微商城", "会员购", "商城", "商店上新",
              "上新", "特惠礼包", "特惠", "礼包", "服装", "周边上新"],
    "popup": ["快闪", "主题店", "限定店", "pop-up", "POP-UP", "popup"],
    "expo": ["漫展", "展会", "参展", "会展", "核聚变", "G-STAR", "GSTAR",
             "萤火虫", "ChinaJoy", "CJ", "BW", "CP", "ComicUp", "嘉年华",
             "FES", "同人展", "展位", "试玩",
             "BilibiliWorld", "Bilibili World", "COMICUP", "comicup",
             "gamescom", "The Game Awards", "TGA", "Summer Game Fest",
             "State of Play", "showcase", "Showcase", "direct", "Direct",
             "TGS", "PAX", "Anime Expo", "expo", "Expo",
             "tickets on sale", "Tickets on Sale"],
}

# ---------- 板块多归属（一条资讯可同时进多个板块） ----------
# 与七类前瞻资讯一一对应；兑换码板块在「抽到码」或标题/正文命中码词时归属。
FACET_KEYWORDS = {
    "livestream": ["前瞻直播", "前瞻特别节目", "特别节目", "特别通讯", "发布会",
                   "直播预告", "直播开启", "前瞻直播回顾", "直播回顾"],
    "version_preview": ["版本前瞻", "新版本导览", "研发通讯", "研发终端", "开发者日志",
                        "版本情报", "版本PV", "展示PV", "角色PV", "共鸣者PV",
                        "版本创作征集", "新版本情报", "版本优化前瞻", "版本活动",
                        "版本前瞻特别节目", "新角色", "新剧情", "新玩法", "前瞻节目",
                        "前瞻", "卡池", "祈愿", "跃迁", "活动预告", "即将开启",
                        "限时活动", "主线更新", "版本更新说明", "版本更新公告",
                        "新版本更新", "PV｜"],
    "redeem_code": ["兑换码", "礼包码", "福利码", "口令码", "CDK", "cdkey",
                    "福利兑换", "通用码", "兑换"],
    "offline_collab": ["联动", "联名", "合作", "×"],
    "merch": ["周边", "手办", "设定集", "旗舰店", "商城", "上新", "贩售", "预售",
              "会员购", "微商城", "谷子", "补货", "尾款", "补款", "货全杂货铺",
              "米游铺", "特惠礼包", "特惠", "礼包", "服装", "周边上新", "商店上新"],
    "popup": ["快闪", "主题店", "限定店", "pop-up", "POP-UP", "popup"],
    "expo": ["漫展", "展会", "参展", "会展", "核聚变", "G-STAR", "GSTAR",
             "萤火虫", "ChinaJoy", "BW", "CP", "ComicUp", "嘉年华", "FES",
             "同人展", "展位", "试玩", "BilibiliWorld", "Bilibili World",
             "COMICUP", "gamescom", "The Game Awards", "TGA", "Summer Game Fest",
             "State of Play", "showcase", "Direct", "TGS", "PAX", "Anime Expo",
             "expo", "Expo", "tickets on sale", "Tickets on Sale"],
}

# ---------- 排除：游戏内公告 / 系统维护等非前瞻内容 ----------

EXCLUDE_KEYWORDS = [
    "维护", "停机", "闪断", "处罚", "封号", "违规", "修复", "补偿",
    "未成年人", "防沉迷", "已知问题", "异常", "概率", "频段",
    "更新公告", "更新说明", "版本更新", "补给", "限时UP", "签到",
    "作战", "通行证", "赛季", "排位", "竞技", "调整", "下架补偿",
]

# ---------- 时效性提示词 ----------

ESTIMATE_HINTS = ["预计", "待定", "或将", "爆料", "猜想", "推测", "社区",
                  "推算", "待官宣", "泄露", "解包", "传闻", "或将"]
RUMOR_HINTS = ["爆料", "猜想", "泄露", "解包", "传闻", "测试服", "内鬼"]
DEADLINE_HINTS = ["有效期", "截止", "结束", "过期", "失效", "领完", "关闭",
                  "下架", "到期", "停止", "截至"]
START_HINTS = ["开启", "开始", "上线", "开售", "预售", "开票", "报名",
               "将于", "定档", "开幕", "发售", "更新时间", "更新后",
               "上线时间", "版本上线"]

# 版本更新公告 / 更新说明：标题里带这些词即视为版本前瞻（新角色 / 新剧情 / 新玩法），
# 但纯维护类（标题含维护 / 停服 / 补偿等）仍然排除。
VERSION_TITLE_PATTERNS = ["版本更新公告", "版本更新说明", "新版本更新",
                          "版本更新预告"]
# 硬排除：即便命中上面的版本公告，标题含这些词也不放行
HARD_EXCLUDE = ["维护", "停机", "停服", "闪断", "处罚", "封号", "违规",
                "修复", "补偿", "未成年人", "防沉迷", "概率",
                "频段", "补给", "限时UP", "签到", "作战", "排位", "下架"]

# ---------- 日期提取 ----------

_FULL_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")
# 数字日期：米哈游等官网常用 2026/09/23、2026-09-23、2026.09.23
_NUM_DATE = re.compile(r"(?<!\d)(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})(?!\d)")
_MONTH_DAY = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_TIME = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
_CODE_TOKEN = re.compile(r"[A-Za-z0-9]{5,16}")
# URL / 短链整体剔除（防误抓为兑换码）：带协议的地址、www. 开头、裸短链域名
_URL_FOR_CODES = re.compile(
    r"(?:https?://|ftp://)\S+"
    r"|(?<![A-Za-z0-9])www\.[A-Za-z0-9.-]+\S*"
    r"|(?<![A-Za-z0-9.])(?:t\.cn|url\.cn|dwz\.cn|b23\.tv|suo\.im|sourl\.cn"
    r"|weibo\.cn|mcb\.so)/[A-Za-z0-9]+",
    re.IGNORECASE)

# 英文日期（海外展会官网常见）："December 10, 2026" / "10 December 2026"
_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
           "june": 6, "july": 7, "august": 8, "september": 9,
           "october": 10, "november": 11, "december": 12}
_EN_DATE_MDY = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
    re.IGNORECASE)
_EN_DATE_DMY = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|"
    r"July|August|September|October|November|December),?\s+(\d{4})",
    re.IGNORECASE)
# 常见英文词/品牌名，不是兑换码（公开：抓取器做邻近过滤时也要用）
CODE_STOPWORDS = {"STEAM", "BILIBILI", "YOUTUBE", "TWITTER", "TWITCH",
                   "DISCORD", "GOOGLE", "APPLE", "SWITCH", "PLAYSTATION",
                   "XBOX", "MOBILE", "ANDROID", "IOS", "PCGAME", "TAPTAP",
                   "MIYOUSHE", "HYPERGRYPH", "MIHOYO", "HOYOVERSE",
                   "KUROGAME", "WECHAT", "WEIBO", "DOUYIN", "KUAISHOU",
                   "DOUYU", "XIAOHONGSHU", "HAOYOUKUAIBAO", "BEXCO",
                   "KINTEX", "ROUND1", "IPHONE", "AEON", "NGA", "GNN",
                   "MOZILLA", "WEBKIT", "SAFARI", "CHROME", "EDGE",
                   "GAMEKEE", "GAMERSKY", "ITHOME", "ZHIDX",
                   # 活动 / 品牌名（非兑换码，防误判）
                   "BILIBILIWORLD", "GAMESCOM", "THEGAMEAWARDS",
                   "SUMMERGAMEFEST", "STATEOFPLAY", "ANIMEEXPO",
                   "TGA", "SGF", "PAX", "FGS", "GSTAR", "CJ", "BW",
                   "STORE", "STORAGE", "SUPPORT", "SERVICE", "OFFICIAL",
                   "REWARDS", "EVENT", "EVENTS", "ACCOUNT", "PASSWORD",
                   "SECURITY", "VERSION", "UPDATE", "PREVIEW", "STREAM",
                   "LINK", "HTTPS", "HTTP", "FORUM", "TOPIC", "THREAD",
                   "GENSHIN", "STARRAIL", "ZZZ", "HONKAI", "KURO",
                   "TOWEROFFANTASY", "WUTHERING", "WAVES", "ARKNIGHTS",
                   "REVERSE", "BLUEPOCH", "SONKILL", "BILIBILI",
                   # 网页脚本/样式常见词（防御性保留）
                   "HTTPS", "HTTP", "CONTEXT", "CONTEXTS", "CAMBRIAN",
                   "BAIDU", "ZIYUAN", "DISPLAY", "FUNCTION", "DOCUMENT",
                   "ELEMENT", "WINDOW", "SCRIPT", "STYLE", "JAVASCRIPT",
                   "UNDEFINED", "STRING", "OBJECT", "NUMBER", "BOOLEAN",
                   "PROTOTYPE", "ADDEVENTLISTENER", "QUERYSELECTOR",
                   "INNERHTML", "SETATTRIBUTE", "APPENDCHILD",
                   "CREATEELEMENT", "BOLANG", "WAVE", "WRAPPER",
                   "RETURN", "CONSOLE", "LOADING", "CONTENT",
                   "TOWNFALL", "MIRROR", "NETFLIX"}


def _to_datetime(y, m, d, hh=None, mm=None):
    try:
        return datetime(y, m, d, hh or 0, mm or 0)
    except ValueError:
        return None


def _with_time(text, m, dt):
    """若日期片段后面紧跟着 HH:MM，则补上时分（官网常写 2026/09/23 06:00）。"""
    if dt is None:
        return None
    tm = _TIME.search(text[m.end():m.end() + 12])
    if tm:
        hh, mm = int(tm.group(1)), int(tm.group(2))
        if hh <= 23 and mm <= 59:
            return _to_datetime(dt.year, dt.month, dt.day, hh, mm)
    return dt


def extract_dates(text, now=None, ref=None):
    """提取文本中的全部日期，返回 [(datetime, 原文片段), ...]（按出现顺序）。

    ref: 参考时间（通常是条目发布时间）。月日日期推断年份时以 ref 为基准：
    取与 ref 最接近且不早于 ref 前 60 天的年份，避免把旧文章里的日期
    误推断成未来时间。
    """
    now = now or datetime.now()
    ref = ref or now
    out, seen_spans = [], set()
    for m in _FULL_DATE.finditer(text):
        dt = _with_time(text, m, _to_datetime(int(m.group(1)), int(m.group(2)),
                                              int(m.group(3))))
        if dt and m.group(0) not in seen_spans:
            out.append((dt, m.group(0)))
            seen_spans.add(m.group(0))
    # 数字日期（2026/09/23 等，官网最常用）
    for m in _NUM_DATE.finditer(text):
        dt = _with_time(text, m, _to_datetime(int(m.group(1)), int(m.group(2)),
                                              int(m.group(3))))
        if dt and m.group(0) not in seen_spans:
            out.append((dt, m.group(0)))
            seen_spans.add(m.group(0))
    # 英文日期（直接带年份，无需推断）
    for m in _EN_DATE_MDY.finditer(text):
        dt = _to_datetime(int(m.group(3)), _MONTHS.get(m.group(1).lower(), 0),
                          int(m.group(2)))
        if dt and m.group(0) not in seen_spans:
            out.append((dt, m.group(0)))
            seen_spans.add(m.group(0))
    for m in _EN_DATE_DMY.finditer(text):
        dt = _to_datetime(int(m.group(3)), _MONTHS.get(m.group(2).lower(), 0),
                          int(m.group(1)))
        if dt and m.group(0) not in seen_spans:
            out.append((dt, m.group(0)))
            seen_spans.add(m.group(0))
    for m in _MONTH_DAY.finditer(text):
        span = m.group(0)
        if span in seen_spans or any(span in s for s in seen_spans):
            continue
        month, day = int(m.group(1)), int(m.group(2))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        # 以 ref 为基准选年份：不早于 ref 前 60 天的最近年份
        dt = None
        for year in (ref.year, ref.year + 1, ref.year - 1):
            cand = _to_datetime(year, month, day)
            if cand and cand >= ref - timedelta(days=60):
                dt = cand
                break
        if dt is None:
            dt = _to_datetime(ref.year, month, day)
        # 找附近的时间（同片段往后 12 字符内）
        tail = text[m.end():m.end() + 12]
        tm = _TIME.search(tail)
        if tm:
            hh, mm = int(tm.group(1)), int(tm.group(2))
            if hh <= 23 and mm <= 59:
                dt = _to_datetime(dt.year, dt.month, dt.day, hh, mm)
        if dt:
            out.append((dt, span))
            seen_spans.add(span)
    return out


def _date_after_hint(text, hints, now=None, ref=None):
    """取提示词之后出现的第一个日期。"""
    best = None
    for h in hints:
        idx = text.find(h)
        while idx != -1:
            dates = extract_dates(text[idx + len(h):], now, ref)
            if dates:
                if best is None or dates[0][0] < best[0]:
                    best = dates[0]
                break
            idx = text.find(h, idx + 1)
    return best


def _last_date_after_hint(text, hints, now=None, ref=None):
    """取提示词之后出现的最后一个日期（截止时间通常写在末尾）。"""
    best = None
    for h in hints:
        idx = text.find(h)
        while idx != -1:
            dates = extract_dates(text[idx + len(h):], now, ref)
            if dates:
                cand = dates[-1]
                if best is None or cand[0] > best[0]:
                    best = cand
                break
            idx = text.find(h, idx + 1)
    return best


def extract_codes(text, limit=6):
    """提取文本中的兑换码（字母数字组合或中文码）。

    严格化以避免误报：
      - 字母数字 token：纯字母且无数字的大概率是英文单词（如 OPTIONS / STORE），
        仅保留含数字或已在白名单的；常见词在 CODE_STOPWORDS 中排除。
      - URL / 短链不是兑换码：先整段剔除（http(s)://…、www.…、裸短链域名），
        再跳过紧跟 / . : = ? & 等 URI 字符的 token——PV 音乐帖里的
        `QQ音乐收听地址：http://t.cn/A6SdwHOR` 曾被误抓成兑换码（2026-10-23 修复）。
      - 中文引号串：仅当含字母/数字时才可能是码（纯中文多为版本名/活动名）。
    """
    text = _URL_FOR_CODES.sub(" ", text)
    codes = []
    for m in _CODE_TOKEN.finditer(text):
        tok = m.group(0)
        if tok.upper() in CODE_STOPWORDS:
            continue
        if not any(c.isalpha() for c in tok):
            continue
        # 纯字母无数字：基本是英文单词而非兑换码，排除（白名单词已在 STOPWORDS）
        if tok.isalpha() and not any(c.isdigit() for c in tok):
            continue
        # 紧跟 URI 字符（/ . : = ? & 等）的是 URL 路径段 / 参数，不是码
        prev = text[m.start() - 1] if m.start() else ""
        if prev in "/.:=?&#%~*+,":
            continue
        codes.append(tok)
    # 中文码：「」或 “” 包裹、且含字母或数字的短串，上下文含“码”
    for m in re.finditer(r"[「“\"']([^」”\"']{4,20})[」”\"']", text):
        tok = m.group(1).strip()
        if not re.search(r"[A-Za-z0-9]", tok):   # 纯中文串（版本名/活动名）排除
            continue
        if re.search(r"[-×·—|/]", tok):
            continue
        ctx = text[max(0, m.start() - 6):m.end() + 6]
        if "码" in ctx:
            codes.append(tok)
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:6]


def parse_publish_time(it):
    """解析条目的发布时间，失败返回 None。"""
    raw = (it.get("time") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt)
        except ValueError:
            continue
    return None


def classify_item(it):
    """返回条目所属前瞻类别 key，非前瞻返回 None。"""
    title = it.get("title") or ""
    # 版本更新公告 / 说明：新版本上线节点，属前瞻内容（纯维护类除外）
    if any(k in title for k in VERSION_TITLE_PATTERNS) \
            and not any(k in title for k in HARD_EXCLUDE):
        return "version_preview"
    if any(k in title for k in EXCLUDE_KEYWORDS):
        return None
    for cls, _ in CATEGORY_DEFS:
        if any(k in title for k in KEYWORDS[cls]):
            return cls
    return None


def compute_facets(title, content, codes):
    """计算条目所属的「前瞻板块」集合（可多归属，用于六板块分栏）。"""
    facets = []
    for cls, kws in FACET_KEYWORDS.items():
        if cls == "version_preview":
            # 纯维护 / 补偿类不归入版本前瞻
            if any(k in title for k in HARD_EXCLUDE):
                continue
            if any(k in title for k in kws):
                facets.append(cls)
        elif cls == "redeem_code":
            if codes or any(k in title or k in content for k in kws):
                facets.append(cls)
        else:
            if any(k in title or k in content for k in kws):
                facets.append(cls)
    return facets


def enrich_item(it, now=None):
    """为条目附加分类与时效性字段（不改变原字典其他字段）。

    新增字段：
      fwd_cls       前瞻类别 key（非前瞻为 None，仅用于粗筛）
      fwd_facets    板块多归属列表（六板块分栏用；可同时进多个板块）
      fwd_tags      时效性标签列表（官宣/社区/爆料/待官宣/第三方/已过期/即将/档期待确认）
      fwd_deadline  截止时间（datetime 或 None）
      fwd_start     活动开始时间（datetime 或 None）
      fwd_codes     提取到的兑换码列表
    来源可信度：条目自带 is_official 字段 -> 标「官宣」；社区来源 -> 标「社区」。
    """
    now = now or datetime.now()
    it = dict(it)
    title = it.get("title") or ""
    content = re.sub(r"<[^>]+>", " ", it.get("content") or "")
    cls = classify_item(it)
    it["fwd_cls"] = cls
    it["fwd_tags"] = []
    it["fwd_deadline"] = None
    it["fwd_start"] = None
    it["fwd_codes"] = []
    it["fwd_facets"] = []
    if cls is None:
        return it

    # 兑换码：仅对「前瞻类」条目（直播 / 版本前瞻 / 兑换码）抽取正文，
    # 避免漫展 / 周边 / 快闪等条目的活动名、品牌名（如 BilibiliWorld、gamescom）
    # 被误判为兑换码泄漏进兑换码板块。前瞻直播回顾帖里的码仍能抓到。
    if cls in ("livestream", "version_preview", "redeem_code"):
        codes = extract_codes(content) or extract_codes(title)
    else:
        codes = []
    it["fwd_codes"] = codes

    # 来源可信度标签：官方第一方 -> 官宣；社区/第三方 -> 社区/第三方
    if it.get("is_official"):
        it["fwd_tags"].append("官宣")
    elif it.get("source_kind") == "third":
        it["fwd_tags"].append("第三方")
    else:
        it["fwd_tags"].append("社区")

    if any(h in title for h in RUMOR_HINTS):
        it["fwd_tags"].append("爆料")
    elif any(h in title for h in ESTIMATE_HINTS):
        it["fwd_tags"].append("待官宣")

    text = f"{title} {content}"
    ref = parse_publish_time(it) or now

    # 板块多归属
    it["fwd_facets"] = compute_facets(title, content, codes)

    # 时间节点提取（兑换码板块优先取截止时间；其余取开始/截止）
    if "redeem_code" in it["fwd_facets"]:
        dl = _last_date_after_hint(text, DEADLINE_HINTS, now, ref)
        if dl is None:
            dl = _date_after_hint(text, DEADLINE_HINTS, now, ref)
        if dl is None:
            dates = extract_dates(text, now, ref)
            dl = dates[-1] if dates else None
        if dl:
            it["fwd_deadline"] = dl[0]
    else:
        dl = _date_after_hint(text, DEADLINE_HINTS, now, ref)
        if dl:
            it["fwd_deadline"] = dl[0]
        st = _date_after_hint(text, START_HINTS, now, ref)
        dates = extract_dates(text, now, ref)
        today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        future = [d for d in dates if d[0] >= today0]
        if st is None or st[0] < today0:
            st = future[0] if future else (dates[0] if dates else st)
        if st:
            it["fwd_start"] = st[0]
        if it.get("fwd_deadline") and it.get("fwd_start") \
                and it["fwd_deadline"] < it["fwd_start"]:
            it["fwd_deadline"] = None

    # 过期 / 结束 / 即将 / 档期待确认 判定
    event_cls = ("livestream" in it["fwd_facets"] or "offline_collab" in it["fwd_facets"]
                 or "popup" in it["fwd_facets"] or "expo" in it["fwd_facets"])
    deadline = it["fwd_deadline"]
    if deadline and deadline < now:
        if "已过期" not in it["fwd_tags"]:
            it["fwd_tags"].append("已过期")
    elif event_cls and it["fwd_start"] and it["fwd_start"] < now - timedelta(days=2) \
            and not it["fwd_deadline"]:
        it["fwd_tags"].append("已结束")
    elif (it["fwd_start"] or deadline) and (it["fwd_start"] or deadline) <= now + timedelta(days=3):
        it["fwd_tags"].append("即将")
    elif not deadline and not it["fwd_start"]:
        it["fwd_tags"].append("档期待确认")
    return it


def select_forward_items(all_items, now=None, max_per_game_cls=6, fresh_days=30):
    """从全量条目中筛选出当期前瞻资讯（去重、限额、过滤陈旧条目）。

    规则：
      - 非前瞻条目直接剔除
      - 发布时间超过 fresh_days 且没有未来时间节点的条目剔除（防陈旧信息堆积）
      - 每款游戏每类最多保留最新 max_per_game_cls 条
    返回按 (类别顺序, 发布时间倒序) 排好的 enriched 条目列表。
    """
    now = now or datetime.now()
    cls_order = {cls: i for i, (cls, _) in enumerate(CATEGORY_DEFS)}
    best = {}
    for it in all_items:
        pub = parse_publish_time(it)
        if pub is None:
            continue
        enriched = enrich_item(it, now)
        if enriched["fwd_cls"] is None:
            continue
        # 陈旧过滤：发布时间超过 fresh_days 的条目，只有在"还有未来时间节点"
        # （未来开始 / 未到期的截止）时才保留，避免旧版本公告堆积
        dl, stt = enriched["fwd_deadline"], enriched["fwd_start"]
        has_future = (dl and dl >= now) or (stt and stt >= now - timedelta(days=1))
        if pub < now - timedelta(days=fresh_days) and not has_future:
            continue
        key = (enriched["game"], enriched["fwd_cls"])
        bucket = best.setdefault(key, [])
        bucket.append((pub, enriched))

    picked = []
    for (game, cls), bucket in best.items():
        bucket.sort(key=lambda x: x[0], reverse=True)
        # 同一标题可能在官网出现多次（列表 + 首页活动位），去重：
        # 保留信息更全的那条（有明确时间节点的优先）
        uniq = {}
        for _, e in bucket[:max_per_game_cls * 2]:
            t = e.get("title", "")
            old = uniq.get(t)
            if old is None or (not (old.get("fwd_start") or old.get("fwd_deadline"))
                               and (e.get("fwd_start") or e.get("fwd_deadline"))):
                uniq[t] = e
        picked.extend(uniq.values())
    picked.sort(key=lambda e: (cls_order.get(e["fwd_cls"], 99),
                               -(parse_publish_time(e) or now).timestamp()))
    return picked


def timeline_entries(items, now=None, horizon_days=100, limit=40):
    """生成关键节点时间线：[(日期datetime, 条目), ...]。

    口径：**从今天 00:00 起**到未来 horizon_days 天之内的活动节点。
    已过期 / 已结束的条目一律不进时间线（历史信息留在各分类板块里）。
    """
    now = now or datetime.now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    horizon = today0 + timedelta(days=horizon_days)
    rows = []
    for it in items:
        tags = it.get("fwd_tags", [])
        if "已过期" in tags or "已结束" in tags:
            continue
        dt = it.get("fwd_start") or it.get("fwd_deadline")
        if not dt:
            continue  # 档期待确认的条目不占时间线
        if dt < today0 or dt > horizon:
            continue
        rows.append((dt, it))
    rows.sort(key=lambda x: x[0])
    return rows[:limit]
