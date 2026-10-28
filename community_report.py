# -*- coding: utf-8 -*-
"""
官方社区版前瞻资讯总览（独立新文件，不修改现有任何文件）。

官方渠道口径（2026-10-22 第二次折中，用户确认）：
  - 官方社区第一方：米游社 / 库街区 / 森空岛（需令牌）+ 官方微博
  - 恢复游戏官网新闻接口源（米哈游系 / 鹰角 / 库洛 / 深蓝官网）：
    官网公告本质是官方确认信息，且是明日方舟 / 终末地 / 鸣潮等
    无米游社板块游戏唯一可行的官方渠道；标注「官网·官方」
  - 补充：测试服爆料（米游社崩坏3官方Beta板块 + hakush 解包数据，第三方标注）
  - NGA 玩家社区（2026-10-22 用户要求恢复）：社区交叉参考，标注「NGA·社区」非官方
  - 保留：官方票务/展会平台（B站会员购、TGA / gamescom / SGF 官网）——非游戏官网，
    用于「漫展展会」板块
  - 微信公众号 / 小红书：无公开接口（风控），暂未接入，页面页脚有说明

筛选与标注规则：
  - 以今天为基准回溯过往帖子，仅保留「正在进行 / 将要开始」的信息，剔除已结束
  - 时间一律按正文解析出的「活动时间」标注，而非发帖时间；无活动时间的近期帖子
    标「档期待确认」
  - 每条标注来源类型（X·官方 / 测试服爆料·第三方 / 展会官方），标题跳转原始来源

产出：reports/官方社区前瞻总览_YYYY-MM-DD.html（四板块 + 快速导航 + 收藏 + 响应式）
"""

import html as html_mod
import json
import os
import re
import time
import tomllib
import urllib.request
import urllib.error
from datetime import datetime, timedelta

from classifier import (enrich_item, extract_dates, parse_publish_time,
                        _date_after_hint, _last_date_after_hint,
                        START_HINTS, DEADLINE_HINTS)
from fetchers import BiliMallFetcher, ExpoNextFetcher
from notifier import build_notifiers
from publisher import GitHubPagesPublisher

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.toml")
# 本地私密密钥文件（不进 git，见 .gitignore）。存在则覆盖 config.toml 的同名项
SECRETS_PATH = os.path.join(BASE_DIR, "secrets.local.toml")


def load_config():
    """按「config.toml → secrets.local.toml → 环境变量」三级覆盖加载配置。

    之所以分三处：config.toml 要能安全提交到 GitHub（公开仓库），所以只放占位符；
    真实密钥放 secrets.local.toml（已 gitignore，留在本机/NAS）或由 CI 的
    环境变量注入（GitHub Actions Secrets），两处都不会出现在版本库里。

    覆盖优先级：环境变量 > secrets.local.toml > config.toml
    """
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)

    if os.path.exists(SECRETS_PATH):
        with open(SECRETS_PATH, "rb") as f:
            _deep_merge(cfg, tomllib.load(f))

    # ---- 环境变量（CI / Docker / NAS 场景用）----
    def _env(name):
        return (os.environ.get(name) or "").strip()

    fs = cfg.setdefault("notify", {}).setdefault("feishu", {})
    if _env("FEISHU_WEBHOOK"):
        fs["webhook"] = _env("FEISHU_WEBHOOK")
    if _env("FEISHU_SECRET"):
        fs["secret"] = _env("FEISHU_SECRET")
    if _env("FEISHU_ENABLED"):
        fs["enabled"] = _env("FEISHU_ENABLED").lower() in ("1", "true", "yes", "on")

    sc = cfg.setdefault("notify", {}).setdefault("serverchan", {})
    if _env("SERVERCHAN_SENDKEY"):
        sc["enabled"] = True
        sc["send_key"] = _env("SERVERCHAN_SENDKEY")

    # GitHub Pages 发布：CI 里通常不需要脚本自己 git push（由 workflow 负责提交）
    gh = cfg.setdefault("publish", {}).setdefault("github", {})
    if _env("SITE_DIR"):
        gh["site_dir"] = _env("SITE_DIR")
    if _env("NO_PUSH"):
        gh["no_push"] = _env("NO_PUSH").lower() in ("1", "true", "yes", "on")
    if _env("GIT_REMOTE"):
        gh["remote"] = _env("GIT_REMOTE")
    return cfg


def _deep_merge(dst, src):
    """递归合并字典（src 覆盖 dst），用于 secrets.local.toml 覆盖 config.toml。"""
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


_classifier_cache = None
REPORT_DIR = os.path.join(BASE_DIR, "reports")

UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
UA_MB = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
         "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")

CONTENT_LIMIT = 2000
DEEP_LIMIT = 10          # 每个板块最多深取正文的帖数（命中前瞻关键词的优先）
FORUM_PAGES = 2          # 每个板块拉的页数
FORUM_PAGE_SIZE = 30     # 每页条数（实测 page_size>30 被接口截断，2页=60帖/板块）

# 需要深取正文的关键词（命中才调 getPostFull，控制请求量）
DEEP_HINTS = ["前瞻", "版本", "活动", "预告", "兑换码", "礼包码", "福利码", "CDK",
              "联动", "联名", "周边", "商城", "上新", "直播", "开启", "更新说明",
              "更新公告", "福利", "快闪", "漫展", "展会", "复刻", "卡池", "祈愿",
              "跃迁", "时装", "皮肤", "售卖", "贩售", "预售", " Beta", "测试服",
              "β", "表决", "公测"]

# ------------------------------------------------------------ 
# 通用 HTTP（带 Referer/UA 的 GET）
# ------------------------------------------------------------ 

def _get(url, headers=None, timeout=18):
    hdrs = {"User-Agent": UA_PC, "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _get_json(url, headers=None, timeout=18):
    return json.loads(_get(url, headers, timeout))


def _strip_html(raw):
    if not raw:
        return ""
    # 只剥「真标签」（<字母开头>），避免颜文字「> <」把整段正文误当标签吃掉
    text = re.sub(r"</?[a-zA-Z][^>]*>", " ", raw)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    return text.strip()


def _clip(text, limit=CONTENT_LIMIT):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "……"


def _mk(game, item_id, title, category, time_dt, url, content,
        source_label, is_official, is_leak=False, plain=False, image=""):
    return {
        "game": game,
        "item_id": str(item_id),
        "title": (title or "").strip(),
        "category": category,
        "time": time_dt.strftime("%Y-%m-%d %H:%M:%S") if time_dt else "",
        "url": url or "",
        "content": (content or "").strip()[:CONTENT_LIMIT] if plain
                   else _clip(_strip_html(content)),
        "source_label": source_label,
        "is_official": is_official,
        "is_leak": is_leak,
        "image": (image or "").strip(),   # 原帖封面图（森空岛官方讯息式呈现用）
    }


# ------------------------------------------------------------
# 采集器 1：米游社官方板块（原神/星铁/绝区零/崩坏3/未定）
#   forum 28=原神官方 53=星铁官方 58=绝区零官方 51=崩坏3官方 31=崩坏3测试服 33=未定官方
# ------------------------------------------------------------ 

MIYOUSHE_GAMES = [
    {"name": "原神",       "gids": 2, "site": "ys",  "forums": [(28, "官方公告")]},
    {"name": "崩坏：星穹铁道", "gids": 6, "site": "sr",  "forums": [(53, "官方公告")]},
    {"name": "绝区零",      "gids": 8, "site": "zzz", "forums": [(58, "官方公告")]},
    {"name": "崩坏3",      "gids": 3, "site": "bh3", "forums": [(51, "官方公告"),
                                                                 (31, "测试服爆料")]},
]


def _structured_text(structured_content):
    """米游社帖子 structured_content（Quill ops JSON 数组）-> 纯文本。"""
    if not structured_content:
        return ""
    try:
        ops = json.loads(structured_content)
    except (json.JSONDecodeError, TypeError):
        return ""
    parts = []
    if isinstance(ops, list):
        for op in ops:
            if isinstance(op, dict):
                ins = op.get("insert")
                if isinstance(ins, str):
                    parts.append(ins)
                elif isinstance(ins, dict) and isinstance(ins.get("text"), str):
                    parts.append(ins["text"])
    return "".join(parts)


def _miyoushe_post_text(full_data):
    """getPostFull 结果 -> (正文文本, 发布时间datetime|None)。"""
    wrapper = full_data.get("data") or {}
    post = wrapper.get("post") or {}
    meta = post.get("post") or {}
    text = _structured_text(meta.get("structured_content"))
    if not text:
        text = _strip_html(meta.get("content") or "")
    ts = meta.get("created_at")
    dt = None
    if ts:
        try:
            dt = datetime.fromtimestamp(int(ts))
        except (ValueError, TypeError, OSError):
            dt = None
    return text, dt


def fetch_miyoushe(errors):
    """抓取五个米游社官方板块，返回条目列表。"""
    out = []
    for g in MIYOUSHE_GAMES:
        gname, gids, site = g["name"], g["gids"], g["site"]
        for fid, board in g["forums"]:
            is_beta = board == "测试服爆料"
            posts, seen = [], set()
            for page in range(1, FORUM_PAGES + 1):
                url = ("https://bbs-api.miyoushe.com/post/wapi/getForumPostList"
                       f"?forum_id={fid}&gids={gids}&page={page}"
                       f"&page_size={FORUM_PAGE_SIZE}")
                try:
                    d = _get_json(url, headers={"Referer": "https://www.miyoushe.com/"})
                except Exception as e:
                    errors.append(f"米游社·{gname}(板块{fid}) 第{page}页: {e}")
                    break
                if d.get("retcode") != 0:
                    errors.append(f"米游社·{gname}(板块{fid}) 接口: {d.get('message')}")
                    break
                lst = (d.get("data") or {}).get("list") or []
                if not lst:
                    break
                for it in lst:
                    p = it.get("post") or {}
                    pid = p.get("post_id")
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    posts.append(p)
                time.sleep(0.15)
            # 正文：列表响应的 structured_content 即完整正文（Quill ops），
            # 无需逐帖调 getPostFull（避开米游社 1034 风控，请求量也更低）
            deep = 0
            for p in posts:
                pid = p.get("post_id")
                subject = str(p.get("subject") or "")
                if not subject or not pid:
                    continue
                text = _structured_text(p.get("structured_content"))
                brief = _strip_html(p.get("content") or "")
                # 极少数帖子列表不带正文：深取一次（命中前瞻关键词才值得）
                if not text and deep < DEEP_LIMIT and (
                        any(h in subject for h in DEEP_HINTS)
                        or any(h in brief for h in DEEP_HINTS)):
                    for attempt in range(2):  # 深取失败重试一次
                        try:
                            fd = _get_json(
                                "https://bbs-api.miyoushe.com/post/wapi/getPostFull"
                                f"?post_id={pid}&gids={gids}",
                                headers={"Referer": "https://www.miyoushe.com/"})
                            if fd.get("retcode") == 0:
                                text, _ = _miyoushe_post_text(fd)
                                deep += 1
                            break
                        except Exception:
                            if attempt == 0:
                                time.sleep(1)
                            continue
                ts = p.get("created_at")
                try:
                    dt = datetime.fromtimestamp(int(ts)) if ts else None
                except (ValueError, TypeError, OSError):
                    dt = None
                label = "米游社·测试服" if is_beta else "米游社"
                # 封面：post.cover 常为空，回退 images[0] / 列表层 cover.url / 正文首图
                cover = p.get("cover") or ""
                if not cover:
                    imgs = p.get("images") or []
                    cover = imgs[0] if imgs else ""
                if not cover and isinstance(it.get("cover"), dict):
                    cover = it["cover"].get("url") or ""
                if not cover:
                    cover = _first_img(text) or _first_img(p.get("content") or "")
                out.append(_mk(
                    game=gname, item_id=f"mys_{pid}", title=subject,
                    category=board, time_dt=dt,
                    url=f"https://www.miyoushe.com/{site}/article/{pid}",
                    content=text or brief, source_label=label,
                    is_official=True, is_leak=is_beta, plain=bool(text),
                    image=cover))
        print(f"  [米游社] {gname}: 累计 "
              f"{sum(1 for o in out if o['game'] == gname)} 条")
    return out


# ------------------------------------------------------------
# 采集器 2：测试服爆料（hakush.in 解包数据，第三方标注，沙箱/断网时优雅跳过）
# ------------------------------------------------------------ 

LEAK_SITES = [
    {"game": "原神",       "key": "gi",   "label": "原神"},
    {"game": "崩坏：星穹铁道", "key": "hsr",  "label": "星穹铁道"},
    {"game": "绝区零",      "key": "zzz",  "label": "绝区零"},
    {"game": "鸣潮",       "key": "wuwa", "label": "鸣潮"},
]


def fetch_leaks(errors):
    out = []
    for site in LEAK_SITES:
        url = f"https://api.hakush.in/{site['key']}/data/zh/new.json"
        try:
            body = _get(url, timeout=15)
            data = json.loads(body)
        except Exception as e:
            errors.append(f"测试服爆料·{site['game']}: {e}（本地网络可达时自动生效）")
            continue
        # 柔性解析：兼容 {id: entry} 映射或 {"characters": [...]} 等结构
        entries = []
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    entries.extend(x for x in v if isinstance(x, dict))
                elif isinstance(v, dict):
                    entries.append(v)
        elif isinstance(data, list):
            entries = [x for x in data if isinstance(x, dict)]
        count = 0
        for e in entries:
            name = (e.get("name") or e.get("zh") or e.get("CN")
                    or e.get("title") or "")
            if not name:
                continue
            rank = e.get("rank") or e.get("rarity") or ""
            etype = e.get("type") or e.get("itemType") or "新内容"
            eid = e.get("id") or e.get("Id") or ""
            out.append(_mk(
                game=site["game"], item_id=f"leak_{site['key']}_{eid}_{name}",
                title=f"测试服爆料：{name}（{rank} {etype}）",
                category="测试服爆料", time_dt=datetime.now(),
                url=f"https://hakush.in/{site['key']}/{eid}" if eid else
                    f"https://hakush.in/{site['key']}",
                content=f"{name}（{rank} {etype}）出现在测试服解包数据中，"
                        f"具体技能/上线时间以官方正式公布为准。",
                source_label="测试服爆料", is_official=False, is_leak=True))
            count += 1
        print(f"  [爆料] {site['game']}: {count} 条")
    return out


# ------------------------------------------------------------
# 采集器 3：官方微博（m.weibo.cn 容器接口；432 风控时可选 Cookie，优雅跳过）
# ------------------------------------------------------------ 

WEIBO_UIDS = {
    "原神": "6593199887",
    "崩坏：星穹铁道": "7643376782",
    "绝区零": "7632078520",
    "崩坏3": "5812573321",
    "明日方舟": "6279793937",
    "明日方舟：终末地": "7745672941",
    "鸣潮": "7730797357",
    "重返未来：1999": "7600886366",
    # 全部 8 款跟踪游戏官号 UID 已查证补齐（2026-10-22）；仍需 Cookie 才能抓取
}
WEIBO_FORWARD_HINTS = ["前瞻", "版本", "活动", "预告", "兑换码", "礼包", "联动",
                       "周边", "商城", "上新", "直播", "开启", "更新", "福利",
                       "快闪", "漫展", "展会", "复刻", "卡池", "祈愿", "跃迁",
                       "时装", "皮肤", "公测", "测试"]


def fetch_weibo(cfg, errors):
    cookie = (cfg.get("settings", {}).get("weibo_cookie") or "").strip()
    headers = {"User-Agent": UA_MB, "Referer": "https://m.weibo.cn/"}
    if cookie:
        headers["Cookie"] = cookie
    out = []
    for game, uid in WEIBO_UIDS.items():
        if not uid:
            continue
        url = (f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}"
               f"&containerid=107603{uid}")
        try:
            body = _get(url, headers=headers)
            d = json.loads(body)
        except Exception as e:
            errors.append(f"微博·{game}: {e}（432 风控时可在 config.toml "
                          f"[settings] weibo_cookie 填入微博 Cookie）")
            continue
        if d.get("ok") != 1:
            errors.append(f"微博·{game}: 接口返回 ok={d.get('ok')}")
            continue
        count = 0
        for card in (d.get("data") or {}).get("cards") or []:
            mb = card.get("mblog") or {}
            text_raw = mb.get("text") or ""
            plain = _strip_html(text_raw)
            if not plain or not any(h in plain for h in WEIBO_FORWARD_HINTS):
                continue
            created = mb.get("created_at") or ""
            try:
                dt = datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y")
                dt = dt.astimezone()
            except ValueError:
                dt = None
            mid = mb.get("id") or mb.get("mid") or ""
            out.append(_mk(
                game=game, item_id=f"wb_{mid}", title=plain[:48],
                category="微博", time_dt=dt,
                url=f"https://m.weibo.cn/status/{mid}" if mid else
                    f"https://weibo.com/u/{uid}",
                content=plain, source_label="微博", is_official=True))
            count += 1
        print(f"  [微博] {game}: {count} 条")
    return out


# ------------------------------------------------------------
# 采集器 4：库街区 / 森空岛（复用现有采集器，令牌只读自现有 config.toml）
# ------------------------------------------------------------ 

def fetch_token_sources(cfg, errors):
    """从现有 config.toml 读取 kurobbs / senkongdao 的令牌配置并复用采集器。"""
    out = []
    for g in cfg.get("games", []):
        if not g.get("enabled", False):
            continue
        src = g.get("source", "")
        if src not in ("kurobbs", "senkongdao"):
            continue
        params = g.get("params", {}) or {}
        has_token = bool((params.get("token") or params.get("cred") or "").strip())
        if not has_token:
            errors.append(f"{'库街区' if src == 'kurobbs' else '森空岛'}·{g['name']}: "
                          f"未填令牌（在现有 config.toml 对应段的 params."
                          f"{'token' if src == 'kurobbs' else 'cred'} 填入后自动生效）")
            continue
        from fetchers import FETCHERS
        try:
            items = FETCHERS[src]().fetch(g["name"], {
                **g, "_timeout": 18, "_ua": UA_PC, "_backfill": False})
            for it in items:
                it.setdefault("is_leak", False)
            out.extend(items)
            print(f"  [{'库街区' if src == 'kurobbs' else '森空岛'}] {g['name']}: "
                  f"{len(items)} 条")
        except Exception as e:
            errors.append(f"{g['name']}({src}): {e}")
    return out


# ------------------------------------------------------------
# 采集器 4b：游戏官网新闻接口（2026-10-22 折中恢复，官方确认信息）
#   复用现有采集器：mihoyo_content / arknights_web / endfield_web /
#   kuro_json / bluepoch_api；未定事件簿已按需求剔除
# ------------------------------------------------------------

OFFICIAL_WEB_SOURCES = ("mihoyo_content", "arknights_web", "endfield_web",
                        "kuro_json", "bluepoch_api")


def fetch_official_web(cfg, errors):
    """从现有 config.toml 读取官网源游戏块并复用采集器，标注「官网」。"""
    out = []
    for g in cfg.get("games", []):
        if not g.get("enabled", False):
            continue
        if g.get("source") not in OFFICIAL_WEB_SOURCES:
            continue
        if g.get("name") == "未定事件簿":
            continue
        gcfg = {**g, "_timeout": 18, "_ua": UA_PC, "_backfill": False}
        if g["source"] == "mihoyo_content":
            gcfg["source_label"] = "官网"   # 默认标签是「米游社」，这里改为官网
        try:
            from fetchers import FETCHERS
            items = FETCHERS[g["source"]]().fetch(g["name"], gcfg)
            for it in items:
                it.setdefault("is_leak", False)
                it.setdefault("image", "")
            out.extend(items)
            print(f"  [官网] {g['name']}: {len(items)} 条")
        except Exception as e:
            errors.append(f"官网·{g['name']}: {e}")
    return out


def dedup_items(items):
    """官网与米游社官方板块常发同一公告：按 (游戏, 标题) 去重。

    保留正文更完整的一条（正文长短决定分类/日期提取质量），
    只把另一条带的封面图回填过来，不因「有图」整条替换。
    """
    out, idx = [], {}
    for it in items:
        key = (it.get("game") or "", re.sub(r"\s+", "", it.get("title") or ""))
        if not key[1]:
            out.append(it)
            continue
        if key not in idx:
            idx[key] = len(out)
            out.append(it)
            continue
        pos = idx[key]
        cur, dup = out[pos], it
        keep = cur if len(cur.get("content") or "") >= len(dup.get("content") or "") else dup
        other = dup if keep is cur else cur
        if keep is not cur:
            out[pos] = keep
        if not out[pos].get("image") and other.get("image"):
            out[pos] = {**out[pos], "image": other["image"]}
    return out


# ------------------------------------------------------------
# 采集器 7：NGA 玩家社区（2026-10-22 用户要求恢复接入，社区交叉参考）
#   非官方来源，标注「NGA·社区」；NgaFetcher 用 Nga_Official UA
#   （实测该 UA 沙箱内可用），只保留命中前瞻关键词的帖子
# ------------------------------------------------------------

# NGA 版块帖以杂谈为主：按版面前缀 + 许愿句式剔除噪声，只留资讯向帖子
NGA_NOISE_PREFIX = ("[闲谈", "[闲聊", "[杂谈", "[吐槽", "[提问", "[求助",
                    "[讨论", "[考据", "[同人", "[剧情", "[攻略", "[整活",
                    "[发现", "[心得", "[评测", "[语音", "[绘画")
# 「氵」/「水」为 NGA 水帖版面标记（如 [周边氵]）
NGA_NOISE_TOKEN = ("氵]", "水]")
NGA_NEWS_KEYWORD = ("官宣", "公布", "公开", "开启", "上线", "预告", "直播",
                    "前瞻", "兑换码", "礼包码", "联动", "合作", "快闪",
                    "周边", "新品", "预售", "贩售", "漫展", "展会", "嘉年华",
                    "门票", "上新", "复刻", "主题店", "旗舰店", "立牌", "手办",
                    "爆料", "立绘", "解包", "内鬼", "新角色", "泄露")


def _nga_is_news(title):
    t = title or ""
    if t.startswith(NGA_NOISE_PREFIX) or any(k in t for k in NGA_NOISE_TOKEN):
        return False
    return any(k in t for k in NGA_NEWS_KEYWORD)


def fetch_nga(cfg, errors):
    out = []
    for g in cfg.get("games", []):
        if not g.get("enabled", False) or g.get("source") != "nga":
            continue
        from fetchers import FETCHERS
        try:
            items = FETCHERS["nga"]().fetch(g["name"], {
                **g, "_timeout": 18, "_ua": UA_PC, "_backfill": False})
            items = [it for it in items if _nga_is_news(it.get("title"))]
            for it in items:
                it.setdefault("is_leak", False)
                it["is_official"] = False
            out.extend(items)
            print(f"  [NGA] {g['name']}: {len(items)} 条")
        except Exception as e:
            errors.append(f"NGA·{g['name']}: {str(e)[:60]}")
    return out


# ------------------------------------------------------------
# 采集器 8：第三方资讯站聚合（2026-10-22 用户确认接入，交叉参考非官方）
#   对齐标准版式的资讯站来源；标题双过滤（游戏名 + 资讯关键词），
#   非官方标注「资讯站·站名」；抓不到 / 结构变化优雅跳过
# ------------------------------------------------------------

THIRD_PARTY_LABELS = {"第三方汇总", "vgover", "3DM", "17173", "IT之家",
                      "18183", "7724", "游侠", "巴哈姆特GNN"}

INFO_GAME_KEYS = [
    ("原神", ("原神",)),
    ("崩坏：星穹铁道", ("星穹铁道", "崩铁")),
    ("绝区零", ("绝区零",)),
    ("崩坏3", ("崩坏3", "崩坏三")),
    ("鸣潮", ("鸣潮",)),
    ("明日方舟", ("明日方舟",)),
    ("明日方舟：终末地", ("终末地",)),
    ("重返未来：1999", ("重返未来", "1999")),
]
INFO_NEWS_KEYS = ("前瞻", "直播", "预告", "官宣", "公布", "公开", "兑换码",
                  "礼包码", "联动", "合作", "快闪", "周边", "预售", "贩售",
                  "漫展", "展会", "嘉年华", "门票", "上新", "复刻", "主题店",
                  "爆料", "测试服", "公测", "立牌", "手办", "FES", "开服")

# (站名, 列表页, 锚点正则, url前缀, re.S)
INFO_STATIONS = [
    ("3DM", "https://www.3dmgame.com/news/",
     r'<a[^>]*href="(https://www\.3dmgame\.com/(?:news|bagua)/\d+[^"]*\.html)"[^>]*>(.*?)</a>',
     "", re.S),
    ("17173", "https://news.17173.com/",
     r'href="((?:https?:)?//news\.17173\.com/[^"]+\.shtml)"[^>]*>(.*?)</a>', "", re.S),
    ("18183", "https://news.18183.com/",
     r'href="(https?://[^"]+18183\.com/[^"]+\.html)"[^>]*>(.*?)</a>', "", re.S),
    ("7724", "https://www.7724.com/",
     r'href="(/YouXiZiXun/news/\d+\.html)"[^>]*>(.*?)</a>',
     "https://www.7724.com", re.S),
    ("游侠", "https://sj.ali213.net/",
     r'href="(https?://[^"]+ali213\.net/[^"]+\.html)"[^>]*>(.*?)</a>', "", re.S),
    ("巴哈姆特GNN", "https://gnn.gamer.com.tw/",
     r'href="(https?://gnn\.gamer\.com\.tw/[a-z0-9/_]+)"[^>]*>(.*?)</a>', "", re.S),
    ("vgover", "https://vgover.com/topic/label/%E5%85%91%E6%8D%A2%E7%A0%81",
     r'href="(/topic/\d+)"[^>]*>(.*?)</a>', "https://vgover.com", re.S),
]
# 锚点内层常混栏目名（「游戏新闻 手游资讯」等），剥掉再判定
_INFO_PREFIX_RE = re.compile(
    r"^(?:游戏新闻|手游资讯|新闻中心|网游资讯|单机资讯|最新|头条|要闻|推荐|新闻|资讯|手游|网游|单机|专区)+\s*")
# IT之家按拼音标签页直取（中文标签路径 404；绝区零/鸣潮等无对应标签）
ITHOME_TAGS = [("原神", "yuanshen"), ("崩坏：星穹铁道", "benghuai"),
               ("崩坏3", "benghuai3")]


# 资讯站列表页常挂历年归档旧闻（无日期字段），标题带过去年份的直接剔除
_INFO_OLDYEAR_RE = re.compile(r"20(?:1\d|2[0-5])年")


def _info_game(title):
    for game, keys in INFO_GAME_KEYS:
        if any(k in title for k in keys):
            return game
    return ""


def fetch_infostations(errors):
    import hashlib
    out = []

    def emit(label, game, title, url):
        out.append(_mk(game=game, item_id=f"info_{hashlib.md5(url.encode()).hexdigest()[:10]}",
                       title=title, category="资讯站", time_dt=datetime.now(),
                       url=url, content=title, source_label=label,
                       is_official=False, plain=True))

    for label, url, pat, prefix, flags in INFO_STATIONS:
        try:
            body = _get(url, headers={"User-Agent": UA_PC, "Referer": url})
        except Exception as e:
            errors.append(f"资讯站·{label}: {str(e)[:55]}（本地网络可达时自动生效）")
            continue
        n0 = len(out)
        seen = set()
        for m in re.finditer(pat, body, flags):
            href, title = m.group(1), m.group(2)
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title)).strip()
            title = _INFO_PREFIX_RE.sub("", title)
            if len(title) < 8 or _INFO_OLDYEAR_RE.search(title):
                continue
            # 已发生直播的回顾报道（「…节目播出」）无前瞻价值
            if re.search(r"节目播出|直播回顾|已播出", title):
                continue
            game = _info_game(title)
            if not game or not any(k in title for k in INFO_NEWS_KEYS):
                continue
            full = href if href.startswith("http") else prefix + href
            if full in seen:
                continue
            seen.add(full)
            emit(label, game, title, full)
        print(f"  [资讯站] {label}: {len(out) - n0} 条")

    # IT之家：标签页即游戏，标题只看资讯关键词
    for game, tag in ITHOME_TAGS:
        try:
            body = _get(f"https://www.ithome.com/tag/{tag}",
                        headers={"User-Agent": UA_PC})
        except Exception as e:
            errors.append(f"资讯站·IT之家({game}): {str(e)[:45]}")
            continue
        n0 = len(out)
        seen = set()
        for href, title in re.findall(
                r'<a[^>]*href="(https://www\.ithome\.com/0/\d+/\d+\.htm)"[^>]*>(.*?)</a>',
                body, re.S):
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title)).strip()
            if len(title) < 8 or not any(k in title for k in INFO_NEWS_KEYS):
                continue
            if href in seen:
                continue
            seen.add(href)
            emit("IT之家", game, title, href)
        if len(out) - n0:
            print(f"  [资讯站] IT之家·{game}: {len(out) - n0} 条")
    return out


# ------------------------------------------------------------
# 采集器 6：第三方兑换码聚合（2026-10-22 用户确认破例接入，标注「第三方汇总」）
#   vg247 codes 页结构稳定（月度更新）；带 Referer 重试，抓不到优雅跳过
# ------------------------------------------------------------

THIRDPARTY_CODE_PAGES = [
    ("原神", "https://www.vg247.com/genshin-impact-codes"),
    ("崩坏：星穹铁道", "https://www.vg247.com/honkai-star-rail-codes"),
    ("绝区零", "https://www.vg247.com/zenless-zone-zero-codes"),
    ("明日方舟：终末地", "https://www.vg247.com/arknights-endfield-codes"),
]
_CODE_RE = re.compile(r"\b[A-Za-z0-9]{6,18}\b")


def _looks_like_code(s):
    """码串判定：字母+数字混合（原神/星铁/绝区零/终末地的兑换码均为大写字母
    数字混合串）；排除常见英文噪声、网页元数据词与披露文本里的普通英文词
    （purchase/commission/editorial 等曾被小写兜底误收，2026-10-23 收紧）。"""
    if re.search(r"(CHROME|WIN|BIT|HTTPS|HTTP|LOGIN|PASSWORD|ACCOUNT)", s, re.I):
        return False
    if s.upper() in _CODE_NOISE_WORDS:
        return False
    return len(s) >= 6 and re.search(r"[A-Z]", s) and re.search(r"[0-9]", s)


# 网页元数据 / 导航常见词：不是兑换码（JSON-LD、栏目名、页脚链接等）
_CODE_NOISE_WORDS = {
    "INDUSTRY", "DESCRIPTION", "HEADLINE", "KEYWORDS", "PUBLISHED", "MODIFIED",
    "ARTICLE", "SECTION", "ASSETS", "IMAGE", "LOGO", "ORGANIZATION", "PERSON",
    "WEBSITE", "WEBPAGE", "NEWSARTICLE", "BREADCRUMB", "NAVIGATION", "MENU",
    "FOOTER", "HEADER", "SIDEBAR", "RELATED", "LATEST", "FEATURED", "TRENDING",
    "NEWSLETTER", "SUBSCRIBE", "COOKIE", "COOKIES", "PRIVACY", "TERMS",
    "CONTACT", "ABOUT", "CAREERS", "ADVERTISE", "DISCLAIMER", "PRIMOGEMS",
    "GENSHIN", "IMPACT", "HONKAI", "STARRAIL", "ZENLESS", "ZONEZERO",
    "ARKNIGHTS", "ENDFIELD", "REDEEM", "WORKING", "EXPIRED", "REWARDS",
}


def fetch_thirdparty_codes(errors):
    out = []
    for game, url in THIRDPARTY_CODE_PAGES:
        body = None
        for hdrs in ({"User-Agent": UA_PC, "Referer": "https://www.vg247.com/"},
                     {"User-Agent": UA_PC}):
            try:
                body = _get(url, headers=hdrs)
                break
            except Exception as e:
                last_err = e
        if body is None:
            errors.append(f"第三方码·{game}: {str(last_err)[:60]}（本地网络可达时自动生效）")
            continue
        # 先剥 <script>/<style> 块：JSON-LD 元数据与 CSS 类名不是兑换码
        # （vg247 页头的 articleSection/industry、CSS 的 copyable/checklist
        #  曾被误收成「8 个码」，2026-10-23 修复）
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ",
                                          re.sub(r"<(script|style)[^>]*>.*?</\1>",
                                                 " ", body, flags=re.S)))
        # 只在 redeem/code/gift/expired 上下文窗口里找码串，避免误抓数字 ID / 英文词
        found, seen = [], set()
        for m in re.finditer(r"[^;{}]{0,140}?(?:redeem|code|gift|expired|working)"
                             r"[^;{}]{0,260}", text, re.I):
            for c in _CODE_RE.findall(m.group(0)):
                if c in seen or not _looks_like_code(c):
                    continue
                seen.add(c)
                found.append(c)
            if len(found) >= 8:
                break
        if not found:
            continue
        content = ("第三方兑换码汇总（多为国际服 / 常驻码，时效与区服不定，失效属正常，"
                   "以游戏内实际兑换结果为准）。当前收录："
                   + "、".join(found))
        out.append(_mk(game=game, item_id=f"tp_{game}",
                       title="兑换码汇总（第三方）", category="兑换码",
                       time_dt=datetime.now(), url=url, content=content,
                       source_label="第三方汇总", is_official=False))
        print(f"  [第三方码] {game}: {len(found)} 个")
    return out


# ------------------------------------------------------------
# 采集器 4b：G-STAR 官网（韩国釜山 BEXCO，英文站静态可抓）
#   档期在概况页文本里，原文为英式写法「Nov.18(Wed) ~ Nov.22(Sun), 2026」，
#   韩式/数字写法「2026.11.19~22」也兼容；解析后转中文日期写入正文，
#   供 classifier 统一抽取开始/截止时间。图片沿用官网官方展讯图作封面。
# ------------------------------------------------------------
_GSTAR_URL = "https://www.gstar.or.kr/eng/gstar/gstar_info.do"
_GSTAR_IMG = "https://www.gstar.or.kr/resources/renew/images/img_exhibition.png"
# 韩式/数字档期：2026.11.19~22 / 2026.11.19-2026.11.22 / 2026년11월19일~22일
_GSTAR_DATE_RE = re.compile(
    r"(20\d{2})\s*[.\s년]*(\d{1,2})\s*[.\s월]*(\d{1,2})\s*[.\s일]*[~\-–]\s*"
    r"(?:(20\d{2})\s*[.\s년]*(\d{1,2})\s*[.\s월]*)?(\d{1,2})")
_GSTAR_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                 "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
# 英文档期：Nov.18(Wed) ~ Nov.22(Sun), 2026
_GSTAR_EN_RE = re.compile(
    r"([A-Za-z]{3,9})\.?\s*(\d{1,2})\s*(?:[\(\[][^\)\]]*[\)\]])?\s*"
    r"[~\-\u2013]\s*([A-Za-z]{3,9})\.?\s*(\d{1,2})\s*"
    r"(?:[\(\[][^\)\]]*[\)\]])?,?\s*(\d{4})")


def _parse_gstar_range(text):
    """解析 G-STAR 档期起止，返回 (start_dt, end_dt)，识别不到返回 (None, None)。"""
    m = _GSTAR_DATE_RE.search(text)
    if m:
        y1, mo1, d1 = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y2 = int(m.group(4)) if m.group(4) else y1
        mo2 = int(m.group(5)) if m.group(5) else mo1
        d2 = int(m.group(6))
        try:
            return datetime(y1, mo1, d1, 9, 0), datetime(y2, mo2, d2, 18, 0)
        except ValueError:
            pass
    em = _GSTAR_EN_RE.search(text)
    if em:
        mo1 = _GSTAR_MONTHS.get(em.group(1)[:3].lower())
        mo2 = _GSTAR_MONTHS.get(em.group(3)[:3].lower())
        if mo1 and mo2:
            y = int(em.group(5))
            try:
                return (datetime(y, mo1, int(em.group(2)), 9, 0),
                        datetime(y, mo2, int(em.group(4)), 18, 0))
            except ValueError:
                pass
    return None, None


def fetch_gstar(errors):
    try:
        body = _get(_GSTAR_URL, timeout=15)
    except Exception as e:
        errors.append(f"G-STAR官网: {str(e)[:60]}")
        return []
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
    start, end = _parse_gstar_range(plain)
    base = ("G-STAR 2026 官方概况页：韩国釜山 BEXCO 举办。米哈游此前官宣参展"
            "（《绝区零》专属展台、《原神》《崩坏：星穹铁道》舞台演出）。"
            "具体展位与活动细节以官网后续公告为准。")
    if start:
        title = (f"G-STAR 2026（{start.month}月{start.day}日–{end.day}日"
                 f"·韩国釜山 BEXCO）")
        # 正文写中文数字日期，确保 classifier 能统一抽出开始/截止时间
        content = (f"档期：{start.year}年{start.month}月{start.day}日–"
                   f"{end.year}年{end.month}月{end.day}日，韩国釜山 BEXCO。" + base)
        dt = start
        note = f"含档期 {start:%m/%d}–{end:%m/%d}"
    else:
        title = "G-STAR 2026（韩国釜山 BEXCO）"
        content = "档期暂未见文本，待官宣。" + base
        dt = datetime.now()
        note = "（档期未识别，待官宣）"
    print(f"  [G-STAR] 概况页抓取{note}")
    return [_mk(game="", item_id="gstar_2026", title=title, category="展会",
                time_dt=dt, url=_GSTAR_URL, content=content,
                source_label="G-STAR官网", is_official=True, image=_GSTAR_IMG)]


# ------------------------------------------------------------
# 采集器 4c：日服官网柔性接入（明日方舟 JP / 终末地 JP）
#   沙箱代理 502，本地网络可达时自动生效；解析失败优雅跳过
# ------------------------------------------------------------
JP_NEWS_PAGES = [
    ("明日方舟（日服）", "https://ak.hypergryph.jp/news", "https://ak.hypergryph.jp"),
    ("明日方舟：终末地（日服）", "https://endfield.hypergryph.jp/news",
     "https://endfield.hypergryph.jp"),
]
_JP_ANCHOR_RE = re.compile(
    r'<a[^>]*href="(/(?:news|topics)/[a-z0-9/_-]+)"[^>]*>(.*?)</a>', re.S)
_JP_DATE_RE = re.compile(r"(20\d{2})[./年]\s*(\d{1,2})[./月]\s*(\d{1,2})")


def fetch_jp_officials(errors):
    out = []
    for name, url, base in JP_NEWS_PAGES:
        try:
            body = _get(url, timeout=15)
        except Exception as e:
            errors.append(f"{name}: {str(e)[:60]}（本地网络可达时自动生效）")
            continue
        rows = []
        for m in _JP_ANCHOR_RE.finditer(body):
            href, inner = m.group(1), m.group(2)
            t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", inner)).strip()
            ctx = body[max(0, m.start() - 240):m.start()]
            d = _JP_DATE_RE.search(ctx)
            if t and len(t) >= 8:
                rows.append((t, base + href, d))
        n = 0
        for t, link, d in rows[:12]:
            dt = (datetime(int(d.group(1)), int(d.group(2)), int(d.group(3)))
                  if d else None)
            content = f"{name}官网新闻：{t}。"
            if dt:
                content += f"发布于 {dt:%Y年%m月%d日}。"
            out.append(_mk(game=name, item_id=f"jp_{abs(hash(link)) % 10**10}",
                           title=t[:60], category="官网公告",
                           time_dt=dt or datetime.now(), url=link,
                           content=content, source_label="日服官网",
                           is_official=True))
            n += 1
        print(f"  [日服官网] {name}: {n} 条")
    return out


# ------------------------------------------------------------
# 采集器 4d：米游社兑换码历史回溯（版块内标题含码词的旧帖，补常驻/往期码）
# ------------------------------------------------------------
_CODE_TITLE_RE = re.compile(r"兑换码|礼包码|CDK|cdkey|福利码", re.I)
CODE_SWEEP_PAGES = 3


def fetch_miyoushe_code_sweep(errors):
    out = []
    now = datetime.now()
    for g in MIYOUSHE_GAMES:
        seen_ids = set()
        n = 0
        for fid, _label in g["forums"]:
            for page in range(1, CODE_SWEEP_PAGES + 1):
                api = ("https://bbs-api.miyoushe.com/post/wapi/getForumPostList"
                       f"?forum_id={fid}&gids={g['gids']}&page={page}"
                       f"&page_size=30")
                try:
                    d = json.loads(_get(api, headers={"Referer": "https://www.miyoushe.com/"}))
                except Exception as e:
                    errors.append(f"米游社码回溯·{g['name']}: {str(e)[:50]}")
                    continue
                if (d.get("retcode") or 0) != 0:
                    break
                lst = (d.get("data") or {}).get("list") or []
                if not lst:
                    break
                for it in lst:
                    post = it.get("post") or {}
                    pid = post.get("post_id") or ""
                    if pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    subj = post.get("subject") or ""
                    if not _CODE_TITLE_RE.search(subj):
                        continue
                    ts = post.get("created_at")
                    dt = (datetime.fromtimestamp(int(ts)) if ts else now)
                    content = _structured_text(it.get("structured_content")) or subj
                    out.append(_mk(
                        game=g["name"], item_id=f"mysweep_{pid}",
                        title=subj, category="兑换码", time_dt=dt,
                        url=f"https://www.miyoushe.com/{g['site']}/article/{pid}",
                        content=content or subj,
                        source_label="米游社·码回溯", is_official=True,
                        image=(post.get("cover")
                               or ((post.get("images") or [""])[0])
                               or ((it.get("cover") or {}).get("url", "")
                                   if isinstance(it.get("cover"), dict) else ""))))
                    n += 1
        if n:
            print(f"  [码回溯] {g['name']}: {n} 条")
    return out


# ------------------------------------------------------------
# 采集器 5：官方票务 / 展会平台（复用现有采集器，非游戏官网）
# ------------------------------------------------------------ 

EXPO_CONFIGS = [
    {"name": "BilibiliWorld 2026", "source": "bili_mall",
     "params": {"project_ids": "1001653"},
     "detail_url": "https://show.bilibili.com/platform/detail.html?id=1001653"},
    {"name": "The Game Awards", "source": "expo_next",
     "url": "https://thegameawards.com/",
     "params": {"mode": "flight", "link_prefix": "https://thegameawards.com"}},
    {"name": "gamescom", "source": "expo_next",
     "url": "https://www.gamescom.global/en/gamescom/news",
     "params": {"mode": "flight", "link_prefix": "https://www.gamescom.global",
                "title_include": "gamescom",
                "title_exclude": "studio,Livestreams,GDQ,Done Quick"}},
    {"name": "Summer Game Fest", "source": "expo_next",
     "url": "https://www.summergamefest.com/events",
     "params": {"mode": "anchors", "block_marker": "cms-event-item",
                "title_marker": "event-name-title",
                "desc_marker": "event-desc-text",
                "link_prefix": "https://www.summergamefest.com"}},
]


def fetch_expo(errors):
    out = []
    for g in EXPO_CONFIGS:
        cls = BiliMallFetcher if g["source"] == "bili_mall" else ExpoNextFetcher
        try:
            items = cls().fetch(g["name"], {
                **g, "_timeout": 18, "_ua": UA_PC, "_backfill": False})
            for it in items:
                it.setdefault("is_leak", False)
            out.extend(items)
            print(f"  [展会] {g['name']}: {len(items)} 条")
        except Exception as e:
            errors.append(f"展会·{g['name']}: {e}")
    return out


# ------------------------------------------------------------
# 汇总 / 过滤（进行中 + 将开始，按活动时间标注）
# ------------------------------------------------------------ 

def collect_all(cfg):
    errors = []
    items = []
    print("== 抓取官方社区与官网来源 ==")
    items += fetch_miyoushe(errors)
    items += fetch_official_web(cfg, errors)
    items += fetch_leaks(errors)
    items += fetch_weibo(cfg, errors)
    items += fetch_token_sources(cfg, errors)
    items += fetch_nga(cfg, errors)
    items += fetch_infostations(errors)
    items += fetch_thirdparty_codes(errors)
    items += fetch_expo(errors)
    print("== 抓取补充来源 ==")
    items += fetch_gstar(errors)
    items += fetch_jp_officials(errors)
    items += fetch_miyoushe_code_sweep(errors)
    return dedup_items(items), errors


def _ensure_activity_time(e, now):
    """兜底：classifier 对 redeem_code 分支只提截止时间，这里补齐开始/截止提取，
    保证「活动时间标注」尽量来自正文（新文件内兜底，不改动现有 classifier）。"""
    if e.get("fwd_start") is not None or e.get("fwd_deadline") is not None:
        return
    if e.get("is_leak"):
        return
    text = f"{e.get('title') or ''} {re.sub(r'</?[a-zA-Z][^>]*>', ' ', e.get('content') or '')}"
    ref = parse_publish_time(e) or now
    dl = (_last_date_after_hint(text, DEADLINE_HINTS, now, ref)
          or _date_after_hint(text, DEADLINE_HINTS, now, ref))
    if dl:
        e["fwd_deadline"] = dl[0]
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    st = _date_after_hint(text, START_HINTS, now, ref)
    if st is None or st[0] < today0:
        future = [d for d in extract_dates(text, now, ref) if d[0] >= today0]
        st = future[0] if future else None
    if st:
        e["fwd_start"] = st[0]
    if e["fwd_deadline"] and e["fwd_start"] and e["fwd_deadline"] < e["fwd_start"]:
        e["fwd_deadline"] = None


# 线下救援关键词：classifier 未识别、但明显是线下展会 / 嘉年华类官宣的帖子，
# 补标 expo facet 保留（例：《绝区零》☆Zenless LIVE 2026——FES专场）
OFFLINE_RESCUE_KEYWORDS = ("FES", "fes", "嘉年华", "周年庆典", "线下",
                           "门票", "购票", "大会展", "展览中心", "主题展",
                           "Zenless LIVE", "ZLIVE")


def filter_ongoing(items, now):
    """只保留进行中 / 将要开始的信息；时间按活动时间标注。"""
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    kept = []
    for it in items:
        e = enrich_item(it, now)
        if e.get("fwd_cls") is None and not e.get("is_leak"):
            title = e.get("title") or ""
            if any(k in title for k in OFFLINE_RESCUE_KEYWORDS):
                # 线下救援：classifier 词表未覆盖的展会 / 嘉年华官宣，补标后保留
                e["fwd_cls"] = "expo"
                e["fwd_facets"] = ["expo"]
            else:
                continue  # 非前瞻条目（测试服爆料始终保留）
        # 兜底提取活动时间（兑换码分支等 classifier 未覆盖的情形）
        _ensure_activity_time(e, now)
        tags = e.get("fwd_tags") or []
        if "已过期" in tags or "已结束" in tags:
            continue
        dl, st = e.get("fwd_deadline"), e.get("fwd_start")
        if dl:
            if dl < today0:
                continue
        elif st:
            if st < today0 - timedelta(days=45):
                continue  # 开始且无截止、又过旧：视为已结束
        else:
            if e.get("source_label") in THIRD_PARTY_LABELS:
                # 第三方资讯站的线下/周边条目：本报告只以官方渠道为时效依据，
                # 发布时间不可知（含抓取兜底填的当前时间）或已早于 45 天，
                # 一律视为旧闻 / 无法判定是否当前活动，不再收录
                fac0 = set(e.get("fwd_facets") or [])
                if fac0 & {"offline_collab", "popup", "expo", "merch"}:
                    pub0 = parse_publish_time(e)
                    if (pub0 is None
                            or abs((now - pub0).total_seconds()) < 180
                            or (now - pub0).days > 45):
                        continue
                # 第三方来源无明确时效：不剔除（列表页本身即最新内容）
                if e.get("source_label") == "第三方汇总" and "长期可试" not in tags:
                    tags.append("长期可试")
                if "档期待确认" not in tags:
                    tags.append("档期待确认")
            else:
                pub_dt = parse_publish_time(e)
                if pub_dt and abs((now - pub_dt).total_seconds()) < 180:
                    continue  # 发布时间只是抓取兜底、且无活动时间：无法判定进行中，剔除
                try:
                    pdt = datetime.strptime((e.get("time") or "")[:19],
                                            "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if pdt < now - timedelta(days=7):
                    continue
                if "档期待确认" not in tags:
                    tags.append("档期待确认")
        if st and dl is None and st < today0:
            # 已开始且无截止：进行中，去掉可能存在的"档期待确认"
            tags = [t for t in tags if t != "档期待确认"]
            e["fwd_tags"] = tags
        if st and st >= today0 and dl is None:
            # 已官宣未来具体开始时间：定档明确，同样去掉"档期待确认"（不再标"预计"）
            tags = [t for t in tags if t != "档期待确认"]
            e["fwd_tags"] = tags
        e["fwd_tags"] = tags
        kept.append(e)
    return kept


def _fmt_short(dt):
    """标准时间格式（对齐《游戏前瞻资讯汇总》版式）：
    10/23（周五）20:00；跨年带年份（2027/01/09）；00:00 视为无时刻。"""
    if not dt:
        return ""
    year = f"{dt.year}/" if dt.year != datetime.now().year else ""
    base = f"{year}{dt.month:02d}/{dt.day:02d}（{WEEKDAYS[dt.weekday()]}）"
    if dt.hour or dt.minute:
        return f"{base} {dt.strftime('%H:%M')}"
    return base


# ---------- 标准文件字段提取（2026-10-22 对齐《游戏前瞻资讯汇总》版式） ----------

_VENUE_RE = re.compile(
    r"[^，。;；！!？?\n]{0,24}?(?:会展中心|博览中心|展览中心|体育馆|文化馆"
    r"|BEXCO|KINTEX|幕张|首钢|乐园|旗舰店|门店|主题店)[^，。;；！!？?\n]{0,12}")
# 精确场馆：中文/英文名 + 场馆词（如「杭州大会展中心」「一山 KINTEX」）
_VENUE_PRECISE = re.compile(
    r"[\u4e00-\u9fa5]{2,8}(?:国际)?(?:会展中心|博览中心|展览中心|体育馆)"
    r"|[\u4e00-\u9fa5]{1,6}\s?(?:KINTEX|BEXCO|幕张)"
    r"|[\u4e00-\u9fa5A-Za-z0-9·]{1,10}「?[\u4e00-\u9fa5A-Za-z0-9]{0,8}」?"
    r"(?:旗舰店|主题店|快闪店|门店)")
_REWARD_RE = re.compile(
    r"(\d{2,4})\s*[×xX*]?\s*(原石|星琼|星声|菲林|水晶|摩拉|纠缠之缘|折金票|嵌晶玉)")
_PLATFORMS = ("B站", "哔哩哔哩", "抖音", "米游社", "斗鱼", "虎牙", "快手", "微博",
              "微信视频号", "视频号", "小红书", "Twitch", "YouTube")
_VER_RE = re.compile(r"(\d+\.\d+)\s*版本[「『]?([^「』」，。;；\s]{1,20})")


def _extract_venue(text):
    """地点/场馆：优先精确场馆名（杭州大会展中心 / miHoYo旗舰店），再泛匹配。"""
    text = text or ""
    m = _VENUE_PRECISE.search(text)
    if m:
        v = m.group(0).strip(" ：:，")
        v = re.sub(r"^(?:将|即将|宣布)?(?:于)?日?在", "", v)  # 去「29日在」类前缀
        if "「" in v and "」" not in v:
            v += "」"  # 补全被截断的引号
        return v
    m = _VENUE_RE.search(text)
    return m.group(0).strip() if m else ""


def _extract_reward(text):
    """码量/奖励单位：如「300 原石」「300 星琼」。"""
    m = _REWARD_RE.search(text or "")
    return f"{m.group(1)} {m.group(2)}" if m else ""


def _extract_platforms(text):
    """前瞻直播观看入口：正文命中的平台清单。"""
    hits = [p for p in _PLATFORMS if p in (text or "")]
    return "、".join(hits[:8])


_PRICE_RE = re.compile(r"(\d[\d,]{0,7})\s*(元|日元|円)")
_CHANNEL_RE = re.compile(r"(?:天猫|米游社|抖音|官网)「([^」]{2,24})」|(米哈游旗舰店|miHoYo旗舰店)")


def _extract_price(text):
    """周边价格：159 元 / 13,000 日元。"""
    m = _PRICE_RE.search(text or "")
    if not m:
        return ""
    return f"{m.group(1)} {m.group(2)}"


def _extract_channel(text):
    """周边售卖渠道：天猫「miHoYo旗舰店」等店名（最多 2 个）。"""
    seen, out = set(), []
    for m in _CHANNEL_RE.finditer(text or ""):
        c = m.group(1) or m.group(2)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
        if len(out) >= 2:
            break
    return " / ".join(out)


def _version_tag(title):
    """版本号+版本名：7.2「坏灭肇始之时」。"""
    m = _VER_RE.search(title or "")
    return f"{m.group(1)}「{m.group(2)}」" if m else ""


# ---------- 封面图：三层兜底（接口字段 → 正文首图 → 详情页 og:image） ----------

_IMG_SRC_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)
_OG_IMAGE_RES = (
    re.compile(r"<meta[^>]+(?:property|name)=[\"']og:image[\"'][^>]+"
               r"content=[\"']([^\"']+)[\"']", re.I),
    re.compile(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+"
               r"(?:property|name)=[\"']og:image[\"']", re.I),
)
# 明显是站徽 / 图标 / 占位的小图，不作为封面
_IMG_NOISE = ("logo", "icon", "favicon", "sprite", "avatar", "qr",
              "placeholder", "blank", "pixel")
_COVER_CACHE = {}


def _img_ok(url):
    u = (url or "").strip()
    if not u or u.startswith("data:"):
        return False
    low = u.lower()
    if low.endswith(".svg"):
        return False
    return not any(k in low for k in _IMG_NOISE)


def _first_img(html):
    """从正文 HTML 取第一张可用图片（跳过图标/站徽/懒加载占位）。"""
    if not html or "<img" not in html.lower():
        return ""
    for m in _IMG_SRC_RE.finditer(html):
        u = m.group(1).strip()
        if not _img_ok(u):
            continue
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            continue  # 相对路径无法在静态报告中稳定加载
        return u
    return ""


def _page_cover(url, timeout=8):
    """兜底：抓详情页 og:image（部分站点正文 SSG 化后仍带 meta）或首图。"""
    if not url or url in _COVER_CACHE:
        return _COVER_CACHE.get(url, "")
    got = ""
    try:
        body = _get(url, timeout=timeout)
        for rgx in _OG_IMAGE_RES:
            m = rgx.search(body)
            if m and _img_ok(m.group(1)):
                got = m.group(1).strip()
                if got.startswith("//"):
                    got = "https:" + got
                break
        if not got:
            # 正文首图：截取 <body> 之后的片段，避免抓到 <head> 里的站徽
            pos = body.lower().find("<body")
            got = _first_img(body[pos:] if pos > 0 else body)
    except Exception:
        got = ""
    _COVER_CACHE[url] = got
    return got


def enrich_card_covers(items, limit=26):
    """为板块3/4 的卡片补齐封面：接口字段 → 正文首图 → 详情页 og:image。

    逐条独立失败不影响主流程；最多补 limit 条，避免拖慢日报生成。
    """
    tried = 0
    for e in items:
        if e.get("image"):
            continue
        fac = set(e.get("fwd_facets") or [])
        if not fac & {"offline_collab", "popup", "expo", "merch"}:
            continue
        img = _first_img(e.get("content") or "")
        if not img and tried < limit:
            tried += 1
            img = _page_cover(e.get("url") or "")
        if img:
            e["image"] = img
    return items


TIMELINE_WINDOW_DAYS = 100  # 时间线覆盖：今天起未来 100 天

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 全部跟踪的游戏（含暂无数据的；未定事件簿已按需求移除）
GAMES_META = [
    {"name": "原神", "note": ""},
    {"name": "崩坏：星穹铁道", "note": ""},
    {"name": "绝区零", "note": ""},
    {"name": "崩坏3", "note": ""},
    {"name": "鸣潮", "note": "官网源已接入；库街区填 token 后增强"},
    {"name": "明日方舟", "note": "官网源已接入；森空岛填 cred 后增强"},
    {"name": "明日方舟：终末地", "note": "官网源已接入；森空岛填 cred 后增强"},
    {"name": "重返未来：1999", "note": "官网接口为全球服公告；国服动态待官方社媒接入"},
]


def _act_date(e):
    """条目的活动时间锚点：优先开始时间，其次截止时间。"""
    return e.get("fwd_start") or e.get("fwd_deadline")


def _badge(e):
    """时间线徽标：官方确认=官宣，社区/爆料/待确认=待官宣。"""
    if e.get("is_leak"):
        return "pending"
    return "official" if e.get("is_official") else "pending"


# ------------------------------------------------------------
# 整体内容范围（2026-10-22 用户确认）：页面重点 = 游戏线下官方讯息 +
# 大版本前瞻时间预告 + 兑换码；游戏内公告与中小活动一律不收录
# ------------------------------------------------------------
PREVIEW_TITLE_HINTS = ("前瞻", "版本预告", "新版本导览", "研发通讯",
                       "研发终端", "开发者日志", "版本情报")
MERCH_KEEP = ("周边", "手办", "新品情报", "新品预告", "贩售", "预售",
              "旗舰店", "设定集", "模型", "毛绒", "抱枕", "挂件", "钥匙扣",
              "徽章", "盲盒", "键盘", "主题店", "礼盒")
# 第三方爆料通道（NGA / 资讯站）：标题命中即放行进板块2爆料分组
LEAK_TITLE_HINTS = ("爆料", "解包", "内鬼", "泄露", "立绘", "新角色",
                    "技能演示", "实机演示", "疑似", "或为")
# 官网版本更新长文准入：标题含「版本」且正文含新内容特征词 → 进板块2
VERSION_CONTENT_HINTS = ("新角色", "新地图", "新S级", "新五星", "新代理人",
                         "新协同者", "新玩法", "新圣痕", "新神之键", "新武器",
                         "新章节", "新篇章", "主线第", "新皮肤", "新服装")
MERCH_EXCLUDE = ("法务", "维权", "盗版", "获刑", "赔偿", "查处", "查获",
                 "假冒", "起诉", "律师", "警方", "判决", "退市", "召回")

# 与「前瞻资讯」无关的常规运营内容（用户确认不收，2026-10-23）：
# 米游币 / 积分兑换中心的商品上新属社区商城日常运营，不是前瞻讯息
MISC_EXCLUDE = ("米游币", "兑换中心", "积分兑换", "兑换商城", "商城上新",
                "签到", "每日任务", "兑换所")

# 游戏本体 / 游戏内活动特征词：标题命中即视为游戏内公告 / 小活动，不收录
# （正文会因提到"兑换码 / 联动"等词被 classifier 误打 facet，因此以标题为准）
GAME_INTERNAL_EXCLUDE = ("更新公告", "更新说明", "更新介绍", "更新通知",
                         "活动说明", "活动介绍", "活动预告", "商店上新",
                         "商店", "商城", "累消", "签到", "复刻", "水晶",
                         "礼包", "奖励", "赢取", "参与得", "参与获取",
                         "参与活动", "任务", "服装", "祈愿", "调频", "寻访",
                         "跃迁", "卡池", "版本更新", "版本正式上线",
                         "角色公开", "补给", "预下载", "养成指南", "概率",
                         "封禁", "同人", "月历", "壁纸", "音乐平台",
                         "生日快乐", "招募", "征集", "星琼", "原石", "菲林",
                         "丁尼", "体力", "补偿", "维护", "回归")


def _in_scope(e):
    """整体范围过滤：线下官方讯息 + 大版本前瞻 + 兑换码；剔除游戏内公告/活动。"""
    title = e.get("title") or ""
    fac = set(e.get("fwd_facets") or [])
    # 米游币 / 积分兑换等社区商城日常运营内容：一律不收
    if any(k in title for k in MISC_EXCLUDE):
        return False
    if e.get("is_leak"):
        return True
    src = e.get("source_label") or ""
    if src == "G-STAR官网":
        return True  # G-STAR 官方概况页，信任流水线
    if not e.get("is_official") and any(k in title for k in LEAK_TITLE_HINTS):
        return True  # 第三方爆料通道：允许标题提及卡池/复刻等词
    if not e.get("is_official") and ("？" in title or "?" in title):
        return False  # 玩家讨论/吐槽帖（标题带问号），不是前瞻讯息
    if "livestream" in fac:
        return "回顾" not in title
    if any(k in title for k in PREVIEW_TITLE_HINTS):
        return True
    # 官网版本更新长文：标题含「版本」且正文描述新内容 → 版本前瞻内容收录
    if "版本" in title and any(k in (e.get("content") or "")
                               for k in VERSION_CONTENT_HINTS):
        return True
    # 剩余情形（兑换码 / 线下 / 周边 facet）：标题含游戏内特征词一律剔除
    if any(k in title for k in GAME_INTERNAL_EXCLUDE):
        return False
    if fac & {"offline_collab", "popup", "expo"}:
        # NGA 社区帖进线下板块需命中强联动词（「合作」等泛词会混入杂谈）
        if not e.get("is_official") and (e.get("source_label") or "").startswith("NGA"):
            strong = ("联动", "联名", "快闪", "漫展", "展会", "门票",
                      "主题店", "线下", "FES", "周边", "手办")
            if not any(k in title for k in strong):
                return False
        return True
    if "redeem_code" in fac:
        return True
    if "merch" in fac:
        return (any(k in title for k in MERCH_KEEP)
                and not any(k in title for k in MERCH_EXCLUDE))
    return False


def _title_tag(e):
    """按类型判定【类型】前缀标签。"""
    title = e.get("title") or ""
    content = e.get("content") or ""
    fac = set(e.get("fwd_facets") or [])
    if "livestream" in fac or any(k in title for k in PREVIEW_TITLE_HINTS):
        return "【前瞻直播】"
    if "版本" in title and any(k in content for k in VERSION_CONTENT_HINTS):
        return "【版本更新】"
    if "redeem_code" in fac and not any(k in title for k in ("周边", "上新", "商品", "手办", "优惠")):
        return "【兑换码】"
    if "offline_collab" in fac:
        return "【联动】"
    if "popup" in fac:
        return "【快闪店】"
    if "expo" in fac:
        return "【漫展】"
    if "merch" in fac:
        return "【周边优惠】" if any(k in (title + content) for k in ("优惠", "折扣", "满减", "限时")) else "【周边上新】"
    return "【资讯】"


def _unify_title(e):
    """统一格式前缀【类型】，正文保留原链接标题原文；原标题未含游戏名时补在标签后。"""
    title = (e.get("title") or "").strip()
    game = e.get("game") or ""
    tag = _title_tag(e)
    if not title:
        return f"{tag}{game or '资讯'}"
    if game and game not in title:
        return f"{tag}{game} {title}"
    return f"{tag}{title}"


# 时间信息完整度排序：开始时间 / 活动区间 > 截止时间 > 其他
_TIME_RANK = {"开始时间": 3, "活动时间": 2, "截止时间": 1}

# 合并时标题/链接的优先级：事件型（前瞻直播/版本更新）> 兑换码 > 资讯/其他。
# 同一版本常有多帖（预告帖/正片帖/兑换码帖），标题应指向事件本身而非附属帖
_TITLE_RANK = {"【前瞻直播】": 4, "【版本更新】": 3, "【兑换码】": 2}


def _merge_dup_rows(rows):
    """把同一条事件的多行记录合并成一行（用户要求：同一个东西不要出现好几条）。

    合并规则：
      - 时间：取信息最全的一格（开始时间优先于截止时间）；另一格若带截止时间，
        补成「10/23（周五）20:00 开始 · 截止 10/26（周一）12:00」；
      - 兑换码：取并集；
      - 标题 / 链接 / 版本：按类型优先级取事件型一行的（前瞻直播 > 版本更新 >
        兑换码 > 资讯），保证合并后指向的是事件本身而非附属帖；
      - 封面 / 平台 / 奖励 / 地点：互补（谁有就用谁的）；
      - 来源：优先官方渠道，第三方汇总排最后。
    """
    out, idx = [], {}
    for r in rows:
        k = r.pop("key", None)
        if k not in idx:
            idx[k] = len(out)
            out.append(r)
            continue
        cur = out[idx[k]]
        if _TIME_RANK.get(r.get("timeKind"), 0) > _TIME_RANK.get(cur.get("timeKind"), 0):
            cur["timeDisplay"], cur["timeKind"] = r["timeDisplay"], r["timeKind"]
        # 另一行带来的截止时间补进行内（预告帖只有开始时间、正片帖只有截止时间）
        other_dl = r.get("deadline") or ""
        if (other_dl and cur.get("timeKind") in ("开始时间", "活动时间")
                and other_dl not in (cur.get("timeDisplay") or "")):
            cur["timeDisplay"] += f" · 截止 {other_dl}"
        for c in r.get("codes") or []:
            if c not in (cur.get("codes") or []):
                cur.setdefault("codes", []).append(c)
        # 标题/链接指向事件本身：类型优先级高的替换优先级低的
        rk = _TITLE_RANK.get(_tag_of_title(r.get("title") or ""), 0)
        ck = _TITLE_RANK.get(_tag_of_title(cur.get("title") or ""), 0)
        if rk > ck:
            for f in ("title", "url", "ver", "id"):
                if r.get(f):
                    cur[f] = r[f]
        for f in ("url", "image", "platforms", "reward", "ver", "location"):
            if not cur.get(f) and r.get(f):
                cur[f] = r[f]
        if cur.get("source") == "第三方汇总" and r.get("source") not in (None, "第三方汇总"):
            cur["source"] = r["source"]
        cur["official"] = bool(cur.get("official")) or bool(r.get("official"))
        cur["hasCodeCtx"] = bool(cur.get("hasCodeCtx")) or bool(r.get("hasCodeCtx"))
    return out


def _tag_of_title(title):
    """取标题的【类型】前缀（不存在返回空串）。"""
    m = re.match(r"^【[^】]*】", title or "")
    return m.group(0) if m else ""


def build_payload(kept, now):
    # 整体范围过滤：剔除游戏内公告与中小活动（用户确认口径，2026-10-22）
    kept = [e for e in kept if _in_scope(e)]

    """产出页面 payload：紧凑时间线分组 + 2-4 板块完整讯息列表（森空岛式）。"""
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    horizon = today0 + timedelta(days=TIMELINE_WINDOW_DAYS)

    # ---- 1. 时间线：今天起 100 天内有活动时间的节点，按日期分组 ----
    # 同一事件的重复来源（官网与米游社同题、预告帖与正片帖）只保留一条：
    # 版本类（前瞻直播 / 版本更新 / 兑换码）按「游戏 + 类型 + 版本号」判重，
    # 其余（联动 / 周边 / 漫展）按「游戏 + 类型 + 标题归一」判重。
    rows = []
    for e in kept:
        d = _act_date(e)
        if d is None or not (today0 <= d <= horizon):
            continue
        dl, st = e.get("fwd_deadline"), e.get("fwd_start")
        if st and st >= today0:
            t, kind = st, "start"
        else:
            t, kind = dl, "deadline"
        hm = t.strftime("%H:%M")
        title = _unify_title(e)  # 已含【类型】前缀；游戏名在标签后补齐
        tags = e.get("fwd_tags") or []
        rows.append((d, e, {
            "time": "" if hm == "00:00" else hm,
            "kind": kind,
            "title": title,
            "url": e.get("url") or "",
            "badge": _badge(e),
            "codes": e.get("fwd_codes") or [],
            "venue": _extract_venue((e.get("content") or "") + " " + title),
            "reward": _extract_reward((e.get("content") or "") + " " + title),
            "expect": "档期待确认" in tags,
            "hasCodeCtx": ("兑换码" in (e.get("content") or "") + title
                           or "redeem_code" in set(e.get("fwd_facets") or [])),
            "id": f"{e.get('game')}|{e.get('item_id')}",
        }))

    def _entry_key(e, title, keep_tag=True):
        """同一条「事件」的判重键（时间线 / 板块2 / 卡片板块共用）。

        版本类（前瞻直播 / 版本更新 / 兑换码 / 资讯）带版本号时按「游戏 + 版本号」
        判重，这样同一版本的「预告帖 / 正片帖 / 兑换码帖」会合并成一行
        （用户 2026-10-23 要求：类似情况一律合并）；
        keep_tag=False 时进一步忽略类型标签，让前瞻直播 / 版本更新 / 兑换码 / 资讯
        同版本全部并成一行（板块2 用）；
        其余（联动 / 周边 / 漫展）按「游戏 + 类型 + 标题归一」判重。
        """
        tag = _title_tag(e)
        if tag in ("【前瞻直播】", "【版本更新】", "【兑换码】", "【资讯】"):
            vm = re.search(r"\d+(?:\.\d+)?", _version_tag(title) or "")
            if vm:
                return (e.get("game") or "",
                        (tag, "ver") if keep_tag else "ver",
                        vm.group(0))
        plain = re.sub(r"【[^】]*】", "", title)
        plain = re.sub(r"预览|预告|回顾|即将|上线|开启|特别节目|\(预计\)|（预计）",
                       "", plain)
        return (e.get("game") or "", tag, re.sub(r"[\s\W_]+", "", plain))

    best, code_pool = {}, {}
    for d, e, row in rows:
        k = _entry_key(e, row["title"], keep_tag=False)
        code_pool.setdefault(k, [])
        for c in row["codes"]:
            if c not in code_pool[k]:
                code_pool[k].append(c)
        score = (1 if row["badge"] == "official" else 0,
                 1 if row["kind"] == "start" else 0)
        cur = best.get(k)
        # 同事件多来源：优先级 = 官宣 > 待官宣，开播/开始 > 截止，日期更早者优先
        if cur is None or (score, -d.toordinal()) > (cur[0], -cur[1].toordinal()):
            best[k] = (score, d, row)
    # 同事件兑换码并集回填胜出行（前瞻直播帖本身无码、码在兑换码帖里）
    for k, (_score, _d, row) in best.items():
        for c in code_pool.get(k) or []:
            if c not in row["codes"]:
                row["codes"].append(c)

    by_date = {}
    seen_row = set()
    for _score, d, row in best.values():
        sig = (d.date(), row["title"], row["time"])
        if sig in seen_row:  # 兜底：完全相同的行不再重复
            continue
        seen_row.add(sig)
        by_date.setdefault(d.date(), []).append(row)
    for d in by_date:
        by_date[d].sort(key=lambda r: (r["time"] or "99:99", r["title"]))

    timeline = []
    for d in sorted(by_date):
        timeline.append({
            "label": f"{d.month:02d}/{d.day:02d}（{WEEKDAYS[d.weekday()]}）",
            "items": by_date[d],
        })

    # ---- 2. 版本前瞻汇总：表格（游戏名+前瞻标题跳转+前瞻时间+兑换码+来源） ----
    sections = []

    def _time_bits(e):
        dl, st = e.get("fwd_deadline"), e.get("fwd_start")
        if dl and st and dl > st:
            return f"{_fmt_short(st)} – {_fmt_short(dl)}", "活动时间"
        if dl:
            return f"截止 {_fmt_short(dl)}", "截止时间"
        if st:
            return f"{_fmt_short(st)} 开始", "开始时间"
        if "redeem_code" in (e.get("fwd_facets") or []):
            return "尽快兑换（时效不定）", "第三方码"
        pub = e.get("time") or ""
        return (f"发布 {pub[5:16]}" if pub else ""), "发布时间"

    def _is_leakish(e):
        """爆料判定：测试服来源，或第三方来源标题命中爆料词。"""
        if e.get("is_leak"):
            return True
        return (not e.get("is_official")
                and any(k in (e.get("title") or "") for k in LEAK_TITLE_HINTS))

    ver_members = []
    for e in kept:
        title = e.get("title") or ""
        fac = set(e.get("fwd_facets") or [])
        # 板块2 准入：官方 / 第三方码汇总 / 第三方爆料帖
        unofficial_ok = e.get("is_official") or \
            (e.get("source_label") in ("第三方汇总",)) or \
            _is_leakish(e)
        if not unofficial_ok:
            continue
        if _is_leakish(e) or "livestream" in fac \
                or any(k in title for k in PREVIEW_TITLE_HINTS):
            ver_members.append(e)
        elif "版本" in title and any(k in (e.get("content") or "")
                                     for k in VERSION_CONTENT_HINTS):
            ver_members.append(e)  # 官网版本更新长文
        elif "redeem_code" in fac and not any(
                k in title for k in ("周边", "上新", "商品", "手办", "优惠")):
            ver_members.append(e)  # 纯兑换码；周边兑换/商品促销归板块4
    ver_members.sort(key=lambda e: _act_date(e) or now)
    rows = []
    for e in ver_members:
        td, tk = _time_bits(e)
        title = _unify_title(e)
        content = e.get("content") or ""
        rows.append({
            "key": _entry_key(e, title, keep_tag=False),
            "id": f"{e.get('game')}|{e.get('item_id')}",
            "game": e.get("game") or "",
            "title": title,
            "url": e.get("url") or "",
            "ver": _version_tag(title),
            "timeDisplay": td, "timeKind": tk,
            "codes": e.get("fwd_codes") or [],
            "deadline": _fmt_short(e.get("fwd_deadline"))
                        if e.get("fwd_deadline") else "",
            "reward": _extract_reward(content + " " + title),
            "platforms": (_extract_platforms(content)
                          if "livestream" in set(e.get("fwd_facets") or [])
                          else ""),
            "source": e.get("source_label") or "官方",
            "official": bool(e.get("is_official")),
            "leak": _is_leakish(e),
            # 正文/标题提及兑换码（前瞻直播的码只在直播画面公布，正文无码串）
            "hasCodeCtx": ("兑换码" in title + content
                           or "redeem_code" in set(e.get("fwd_facets") or [])),
        })
    digest = _merge_dup_rows(rows)
    sections.append({
        "key": "version", "num": "2", "title": "游戏版本前瞻内容汇总",
        "desc": "表格：游戏 · 前瞻标题（点击跳原文） · 前瞻时间 · 兑换码 · 来源",
        "count": len(digest), "items": digest})

    # ---- 3-4. 板块：森空岛式卡片（原帖封面图 + 标题跳转 + 概览） ----
    SUB_OF = {"offline_collab": "collab", "popup": "popup", "expo": "expo"}
    CARD_SECTIONS = [
        {"key": "collab", "num": "3", "title": "联动活动资讯",
         "desc": "联动活动 / 快闪店 / 漫展展会 三个子分页切换",
         "facets": {"offline_collab", "popup", "expo"}, "include_leak": False},
        {"key": "merch", "num": "4", "title": "官方周边资讯",
         "desc": "周边上新 · 限时优惠 · 盲袋活动",
         "facets": {"merch"}, "include_leak": False},
    ]
    loc_re = re.compile(r"地点[：:]\s*([^，。；\n]{2,24})")
    for sd in CARD_SECTIONS:
        members = []
        for e in kept:
            fac = set(e.get("fwd_facets") or [])
            if not fac & sd["facets"]:
                continue
            if e.get("is_leak") and not sd["include_leak"]:
                continue
            # 卡片板块只收主题内容：版本更新公告/说明类（正文提及联动/周边误打
            # facet）归板块2 版本前瞻，不进板块3/4
            if any(k in (e.get("title") or "") for k in
                   ("更新公告", "更新说明", "版本更新", "预下载")):
                continue
            members.append(e)
        members.sort(key=lambda e: _act_date(e) or now)
        cards = []
        for e in members:
            td, tk = _time_bits(e)
            fac = e.get("fwd_facets") or []
            sub = next((SUB_OF[f] for f in fac if f in SUB_OF), "")
            body = (e.get("content") or "") + " " + (e.get("title") or "")
            location = _extract_venue(body) or (loc_re.search(body) or [None, ""])[1]
            location = location.strip() if isinstance(location, str) else ""
            if not location and sub == "expo":
                location = "以官方公告为准"
            title = _unify_title(e)
            cards.append({
                # 同游戏+同版本(带版本号时)/同标题 → 合并（用户 2026-10-23 要求）
                "key": _entry_key(e, title),
                "id": f"{e.get('game')}|{e.get('item_id')}",
                "game": e.get("game") or "",
                "title": title,
                "url": e.get("url") or "",
                "timeDisplay": td,
                "timeKind": tk,
                "source": e.get("source_label") or "官方",
                "official": bool(e.get("is_official")),
                "leak": bool(e.get("is_leak")),
                "facets": fac,
                "codes": e.get("fwd_codes") or [],
                "tags": e.get("fwd_tags") or [],
                "image": e.get("image") or _first_img(e.get("content") or ""),
                "price": _extract_price(body),
                "channel": _extract_channel(body),
                "sub": sub,
                "location": location,
            })
        cards = _merge_dup_rows(cards)
        sections.append({
            "key": sd["key"], "num": sd["num"], "title": sd["title"],
            "desc": sd["desc"], "count": len(cards), "items": cards,
        })
    return {"timeline": timeline, "sections": sections, "games": GAMES_META,
            "captured_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "generated": now.strftime("%Y-%m-%d %H:%M GMT+8")}



# ------------------------------------------------------------
# HTML 模板（四板块 + 导航 + 收藏 + 响应式）
# ------------------------------------------------------------ 

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>官方社区前瞻总览</title>
<style>
  :root{
    --bg:#f4f6fb; --card:#fff; --ink:#1e2430; --sub:#68738a;
    --line:#e6eaf4; --accent:#3f6df5; --accent2:#8b5cf6;
    --official:#0f9d6e; --official-bg:#e5f7ef;
    --leak:#d9541e; --leak-bg:#fdeee4;
    --expo:#8a63d2; --expo-bg:#f1ebfc;
    --shadow:0 5px 20px rgba(35,52,100,.08); --radius:14px;
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    background:var(--bg);color:var(--ink);line-height:1.55;-webkit-text-size-adjust:100%}
  a{color:inherit;text-decoration:none}
  header.hero{background:linear-gradient(120deg,#3f6df5,#8b5cf6);color:#fff;padding:24px 18px 20px;text-align:center}
  header.hero h1{margin:0;font-size:23px}
  header.hero p{margin:7px 0 0;font-size:12.5px;opacity:.92}
  nav.topnav{position:sticky;top:0;z-index:50;background:rgba(255,255,255,.94);backdrop-filter:blur(10px);
    border-bottom:1px solid var(--line);box-shadow:var(--shadow)}
  nav.topnav .wrap{max-width:1160px;margin:0 auto;display:flex;gap:2px;align-items:center;padding:7px 10px;overflow-x:auto;scrollbar-width:thin}
  nav.topnav a{flex:0 0 auto;padding:8px 13px;border-radius:999px;font-size:13.5px;color:var(--sub);white-space:nowrap}
  nav.topnav a:hover{background:#eef2ff;color:var(--accent)}
  nav.topnav a.fav{margin-left:auto;color:var(--leak);font-weight:600}
  nav.topnav a.fav.active{background:var(--leak-bg)}
  /* ===== 信息汇总版块（顶部全量列表，带「新」已读标记） ===== */
  #sec-digest{background:linear-gradient(180deg,#f7f9ff,#fff);border:1px solid var(--line);
    border-radius:var(--radius);padding:16px 16px 18px;box-shadow:var(--shadow);scroll-margin-top:58px}
  .digest-head{display:flex;align-items:baseline;gap:10px;margin:0 0 12px;flex-wrap:wrap}
  .digest-head h2{font-size:19px;margin:0}
  .digest-head .desc{color:var(--sub);font-size:12.5px}
  #digestCount{color:var(--accent);font-weight:600;font-size:12.5px}
  .digest-list{display:flex;flex-direction:column;gap:7px;max-height:360px;overflow:auto;padding-right:4px}
  .digest-item{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:10px;
    background:#fff;border:1px solid var(--line);font-size:13.5px;line-height:1.3}
  .digest-item.read{opacity:.7}
  .new-badge{flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;
    min-width:20px;height:20px;padding:0 5px;border-radius:6px;background:#ff3b30;color:#fff;
    font-size:11px;font-weight:700;line-height:1}
  .digest-item.read .new-badge{display:none}
  .di-type{flex:0 0 auto;color:var(--accent);font-weight:600;font-size:12px;
    background:#eef2ff;border-radius:6px;padding:2px 7px;white-space:nowrap}
  .di-game{flex:0 0 auto;color:var(--sub);font-size:12.5px;white-space:nowrap}
  .di-title{flex:1 1 auto;color:#1f2733;text-decoration:none;font-weight:500;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
  .di-title:hover{color:var(--accent);text-decoration:underline}
  .di-src{flex:0 0 auto;display:flex;gap:4px}
  .di-time{flex:0 0 auto;color:#a7aec2;font-size:11.5px;white-space:nowrap}
  @media(max-width:680px){
    .digest-item{flex-wrap:wrap;gap:6px}
    .di-title{flex:1 1 100%;order:5}
    .di-time{flex:1 1 100%;order:6}
  }
  main{max-width:1160px;margin:0 auto;padding:20px 14px 60px}
  section{margin:28px 0;scroll-margin-top:58px}
  .sec-head{display:flex;align-items:baseline;gap:9px;margin:0 0 13px;flex-wrap:wrap}
  .sec-head h2{font-size:19px;margin:0}
  .sec-head .desc{color:var(--sub);font-size:12.5px}
  .sec-num{display:inline-flex;align-items:center;justify-content:center;width:25px;height:25px;border-radius:8px;
    background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;font-size:13px;font-weight:700;flex:0 0 auto;align-self:center}
  .card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:13px 15px;box-shadow:var(--shadow);transition:.15s}
  .card:hover{transform:translateY(-2px);border-color:#c9d4ef}
  .row{display:flex;gap:9px;align-items:flex-start}
  .title-link{font-weight:600;font-size:15px;color:var(--ink);flex:1}
  .title-link:hover{color:var(--accent)}
  .title-link .ext{font-size:11px;color:var(--sub);margin-left:4px;font-weight:400}
  .meta{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin-top:7px;font-size:12px;color:var(--sub)}
  .tag{padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:600;white-space:nowrap}
  .tag.official{background:var(--official-bg);color:var(--official)}
  .tag.leak{background:var(--leak-bg);color:var(--leak)}
  .tag.expo{background:var(--expo-bg);color:var(--expo)}
  .tag.status{background:#eef2ff;color:var(--accent)}
  .tag.ft{background:#f0f3fa;color:#5a6377}
  .tag.code{background:#fff1f0;color:#d4380d;border:1px dashed #ffb3ae;font-family:ui-monospace,Menlo,Consolas,monospace}
  .game-pill{background:#eef2ff;color:var(--accent);padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:600}
  .when{font-weight:700;color:var(--accent);font-variant-numeric:tabular-nums;font-size:12.5px}
  .empty{color:var(--sub);font-size:13.5px;padding:16px;text-align:center;border:1px dashed var(--line);border-radius:12px;background:#fafbff}
  .star{flex:0 0 auto;width:29px;height:29px;border-radius:50%;border:1px solid var(--line);background:#fff;cursor:pointer;
    font-size:14px;display:flex;align-items:center;justify-content:center;color:#c3c9d8;transition:.15s;user-select:none}
  .star:hover{color:var(--leak);border-color:var(--leak)}
  .star.on{color:#fff;background:var(--leak);border-color:var(--leak)}
  /* 紧凑时间线（关键节点式：日期头 + 文字行 + 徽标） */
  .tline{position:relative;margin-left:6px;padding-left:26px}
  .tline::before{content:"";position:absolute;left:7px;top:6px;bottom:6px;width:2px;border-radius:2px;
    background:linear-gradient(var(--accent),var(--accent2))}
  .tl-node{position:relative;margin-bottom:15px}
  .tl-node::before{content:"";position:absolute;left:-26px;top:7px;width:9px;height:9px;border-radius:50%;
    background:#fff;border:3px solid var(--accent);box-shadow:0 0 0 3px #fff}
  .tl-date{font-size:14px;font-weight:700;color:var(--accent);margin-bottom:3px;font-variant-numeric:tabular-nums}
  .tl-text{font-size:13.5px;color:var(--ink);line-height:1.95}
  .tl-time{color:var(--accent);font-weight:600;font-variant-numeric:tabular-nums}
  .tl-line{display:block;margin:4px 0;padding-left:2px;border-left:2px solid #eef2ff}
  .tl-link{color:inherit;transition:.15s;border-bottom:1px dashed transparent}
  .tl-link:hover{color:var(--accent);border-bottom-color:var(--accent)}
  .badge-off,.badge-pend{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;
    vertical-align:1px;margin-left:4px;white-space:nowrap}
  .badge-off{background:var(--official-bg);color:var(--official)}
  .badge-pend{background:#fff7e6;color:#d48806}
  .code-pill{display:inline-block;padding:0 7px;border-radius:6px;font-size:11.5px;color:#d4380d;background:#fff1f0;
    border:1px dashed #ffb3ae;font-family:ui-monospace,Menlo,Consolas,monospace;margin:0 2px;vertical-align:1px}
  /* 兑换码未以文字公布时（直播画面公布）的说明 */
  .tl-note{display:inline-block;padding:0 7px;border-radius:6px;font-size:11.5px;color:#8a6236;
    background:#fdf6ec;border:1px dashed #e8c48a;margin:0 2px;vertical-align:1px;cursor:help}
  .vcode-note{display:inline-block;padding:0 7px;border-radius:6px;font-size:11.5px;color:#8a6236;
    background:#fdf6ec;border:1px dashed #e8c48a;cursor:help}
  .star.mini{width:22px;height:22px;font-size:11px;vertical-align:middle;margin-left:5px;border:none;background:transparent}
  /* 森空岛式讯息卡：原帖封面图 + 标题跳转 + 概览 */
  .skcard{position:relative;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
    box-shadow:var(--shadow);overflow:hidden;display:flex;flex-direction:column;transition:.15s}
  .skcard:hover{transform:translateY(-2px);border-color:#c9d4ef}
  .skcard .cover{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:#eef2ff}
  .skcard .sk-body{padding:12px 14px 13px;display:flex;flex-direction:column}
  .skcard .star{position:absolute;top:9px;right:9px;background:rgba(255,255,255,.93);box-shadow:0 1px 6px rgba(0,0,0,.14)}
  .tabs{display:flex;gap:6px;overflow-x:auto;padding-bottom:6px;margin-bottom:13px;scrollbar-width:thin}
  .tabs button{flex:0 0 auto;padding:8px 15px;border:1px solid var(--line);background:#fff;border-radius:999px;
    font-size:13.5px;color:var(--sub);cursor:pointer;white-space:nowrap;transition:.15s}
  .tabs button:hover{border-color:var(--accent);color:var(--accent)}
  .tabs button.active{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border-color:transparent;font-weight:600}
  .sub-h{font-size:14px;font-weight:700;margin:15px 0 8px;display:flex;align-items:center;gap:6px}
  .sub-h .bar{width:4px;height:15px;border-radius:3px;background:var(--accent)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:11px}
  /* 板块2：汇总表格（游戏 · 前瞻标题跳转 · 前瞻时间 · 兑换码 · 来源） */
  .vtable-wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);
    border-radius:var(--radius);box-shadow:var(--shadow)}
  table.vtable{width:100%;border-collapse:collapse;font-size:13px;min-width:760px}
  .vtable thead th{background:#e8effc;color:#2c4a8a;font-weight:700;font-size:12.5px;
    text-align:left;padding:10px 12px;border-bottom:2px solid #d5e1f7;white-space:nowrap}
  .vtable tbody td{padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:top;line-height:1.6}
  .vtable tbody tr:last-child td{border-bottom:none}
  .vtable tbody tr:hover{background:#f6f9ff}
  .vtable .vgame{font-weight:700;color:var(--accent);white-space:nowrap;font-size:12.5px}
  .vtable .vtitle{font-weight:600;font-size:13.5px}
  .vtable .vtitle a{color:var(--ink);border-bottom:1px dashed transparent}
  .vtable .vtitle a:hover{color:var(--accent);border-bottom-color:var(--accent)}
  .vtable .vtitle .ver{font-weight:400;color:var(--sub);font-size:12px;display:block;margin-top:1px}
  .vtable .vtime{color:var(--accent);font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap;font-size:12.5px}
  .vtable .vtime small{display:block;color:#a7aec2;font-weight:400}
  .vtable .vcodes{max-width:170px}
  .vtable .vsrc{white-space:nowrap}
  .vtable .vsrc .star{margin-top:2px}
  .dg-reward{color:#9a6206;background:#fff3dd;padding:1px 8px;border-radius:999px;
    font-size:11.5px;font-weight:600;white-space:nowrap}
  .dg-plat{margin-top:4px;font-size:12px;color:var(--accent)}
  .tl-venue{color:var(--sub);font-size:12.5px}
  .tl-reward{color:#9a6206;font-size:12px;font-weight:600}
  /* 卡片封面：无图时用统一占位，保持外观一致 */
  .skcard .cover{width:100%;aspect-ratio:16/9;object-fit:cover;object-position:center;
    display:block;background:#eef2ff}
  .skcard .cover-empty{width:100%;aspect-ratio:16/9;display:flex;flex-direction:column;
    align-items:center;justify-content:center;gap:5px;
    background:linear-gradient(135deg,#eef2ff,#f4f0ff);color:#93a0bd}
  .skcard .cover-empty .ph-i{font-size:20px;opacity:.55}
  .skcard .cover-empty .ph-g{font-size:14.5px;font-weight:700;color:#7b88a8;letter-spacing:.4px}
  .skcard .cover-empty .ph-t{font-size:11.5px;color:#a9b4cc}
  footer{max-width:1160px;margin:0 auto;padding:22px 16px 50px;color:var(--sub);font-size:12.5px;text-align:center}
  footer .note{background:#fff;border:1px solid var(--line);border-radius:12px;padding:13px 16px;text-align:left;box-shadow:var(--shadow)}
  @media (max-width:768px){
    header.hero h1{font-size:19px}
    main{padding:14px 11px 50px}
    .grid{grid-template-columns:1fr}
    nav.topnav a{padding:7px 11px;font-size:12.5px}
    .sec-head h2{font-size:17px}
    .title-link{font-size:14.5px}
    .tl-text{font-size:13px}
  }
  body.fav-only .skcard:not(.faved){display:none}
  body.fav-only .vtable tbody tr:not(.faved){display:none}
</style>
</head>
<body>
<header class="hero">
  <h1>📡 官方社区前瞻总览</h1>
  <p id="subtitle">来源：米游社 / 官网 / 官方微博 / NGA社区 / 测试服爆料 / 官方票务展会平台 / 资讯站交叉参考 / 第三方码汇总</p>
  <p id="genMeta" style="margin:4px 0 0;font-size:12px;opacity:.85"></p>
</header>

<nav class="topnav">
  <div class="wrap">
    <a href="#sec-digest">❰ 信息汇总</a>
    <a href="#sec-timeline">① 资讯时间线</a>
    <a href="#sec-version">② 版本前瞻汇总</a>
    <a href="#sec-collab">③ 联动活动</a>
    <a href="#sec-merch">④ 官方周边</a>
    <a href="#" class="fav" id="favToggle">★ 我的收藏 <span id="favCount">(0)</span></a>
  </div>
</nav>

<main>
  <section id="sec-digest">
    <div class="digest-head">
      <span class="sec-num">❰</span><h2>信息汇总 · 每日全量抓取</h2>
      <span class="desc">全部带链接的前瞻资讯 · 按抓取时间倒序 · 点击跳转后「新」自动标记已读</span>
      <span id="digestCount"></span>
    </div>
    <div class="digest-list" id="digestList"></div>
  </section>

  <section id="sec-timeline">
    <div class="sec-head"><span class="sec-num">1</span><h2>主要资讯时间线速览</h2>
      <span class="desc">未来约 100 天 · 关键节点 · 按活动时间排序</span></div>
    <div class="tline" id="timeline"></div>
  </section>

  <section id="sec-version">
    <div class="sec-head"><span class="sec-num">2</span><h2>游戏版本前瞻内容汇总</h2>
      <span class="desc">表格：游戏 · 前瞻标题（点击跳原文） · 前瞻时间 · 兑换码 · 来源</span></div>
    <div id="versionBody"></div>
  </section>

  <section id="sec-collab">
    <div class="sec-head"><span class="sec-num">3</span><h2>联动活动资讯</h2>
      <span class="desc">联动活动 / 快闪店 / 漫展展会 子分页切换</span></div>
    <div class="tabs" id="collabTabs"></div>
    <div class="grid" id="collabList"></div>
  </section>

  <section id="sec-merch">
    <div class="sec-head"><span class="sec-num">4</span><h2>官方周边资讯</h2>
      <span class="desc">周边上新 · 限时优惠 · 盲袋活动</span></div>
    <div class="grid" id="merchGrid"></div>
  </section>
</main>

<footer>
  <div class="note">
    <b>标注说明：</b>时间线为<b>纯信息速览</b>（无链接），「官宣」= 官方渠道确认，「待官宣」= 社区信息 / 测试服爆料 / 待官方确认；
    板块2 为<b>汇总表格</b>（游戏 · 前瞻标题可点击跳转原文 · 前瞻时间 · 兑换码 · 来源）；
    板块3-4 每条讯息统一以封面图（原帖无图时显示占位图）+ 标题（点击跳转原始来源）呈现。
    「X·官方」= 官方社区第一方渠道；「官网·官方」= 游戏官网新闻公告；「官方票务/展会」= B站会员购与展会官网；
    「测试服爆料·第三方」非官方确认，请以官方公告为准。
    时间一律按<b>实际活动时间</b>标注，时间线仅收录未来约 100 天内正在进行 / 将要开始的信息。<br>
    <b>分级图例：</b><span class="tag official">官宣</span>官方渠道已公布 ｜
    <span class="tag leak">爆料</span>测试服 / 社区消息未经确认 ｜
    <span class="tag status">待官宣</span>官方已预告但档期未定（尚未公布具体时间）｜
    <span class="tag ft">第三方汇总</span>兑换码来自第三方聚合站，时效 / 区服不定，失效属正常。
    防骗提示：官方福利码全部免费，凡「付费代兑 / 内部码 / 扫码领原石 / 索要账号密码验证码」均为诈骗。
    数据生成于 <span id="genTime"></span>。
  </div>
</footer>

<script type="application/json" id="payload">__PAYLOAD__</script>
<script>
(function(){
  "use strict";
  var PAYLOAD = JSON.parse(document.getElementById("payload").textContent);
  var DATA = PAYLOAD.timeline || [];
  var SECTIONS = {};
  (PAYLOAD.sections || []).forEach(function(s){SECTIONS[s.key]=s;});
  var GAMES_META = PAYLOAD.games || [];
  var FACET_LABEL = {
    livestream:"前瞻直播", version_preview:"版本前瞻", redeem_code:"兑换码",
    offline_collab:"联动", merch:"周边", popup:"快闪店", expo:"漫展展会"
  };
  var FAV_KEY = "gmon_fav_comm_v3";
  var favSet = new Set(JSON.parse(localStorage.getItem(FAV_KEY) || "[]"));
  var favOnly = false;
  var DIGEST_READ_KEY = "gmon_digest_read_v1";
  var digestRead = new Set(JSON.parse(localStorage.getItem(DIGEST_READ_KEY) || "[]"));
  function markDigestRead(id){
    digestRead.add(id);
    try{localStorage.setItem(DIGEST_READ_KEY, JSON.stringify([...digestRead]));}catch(e){}
  }

  function esc(s){return (s||"").replace(/[&<>"]/g,function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c];});}

  function saveFav(){localStorage.setItem(FAV_KEY, JSON.stringify([...favSet]));
    document.getElementById("favCount").textContent="("+favSet.size+")";}
  function toggleFav(id,btn){
    if(favSet.has(id)){favSet.delete(id);if(btn){btn.classList.remove("on");btn.textContent="☆";}}
    else{favSet.add(id);if(btn){btn.classList.add("on");btn.textContent="★";}}
    saveFav();
    toggleFav(id);   // 时间线不再参与收藏，卡片/汇总行由各自渲染函数刷新
  }
  function starBtn(id){
    var on=favSet.has(id);
    var b=document.createElement("button");
    b.className="star"+(on?" on":"");b.textContent=on?"★":"☆";b.title="特别关注 / 收藏";
    b.setAttribute("data-id",id);
    b.onclick=function(e){e.stopPropagation();e.preventDefault();toggleFav(id,b);};
    return b;}
  function starHtml(id){
    return '<button class="star mini'+(favSet.has(id)?" on":"")
      +'" data-id="'+esc(id)+'" title="特别关注 / 收藏">'+(favSet.has(id)?"★":"☆")+'</button>';}

  function sourceBadge(it){
    if(it.leak) return '<span class="tag leak">测试服爆料·第三方</span>';
    if(it.official){
      var s=it.source||"官方";
      if(/会员购|展会/.test(s)) s=s+"·官方票务/展会";
      else s=s+"·官方";
      return '<span class="tag official">'+esc(s)+'</span>';
    }
    return '<span class="tag leak">'+esc(it.source||"社区")+'</span>';}
  function statusTags(it){
    var h="";
    (it.tags||[]).forEach(function(t){
      if(t==="官宣"||t==="社区"||t==="第三方")return;
      h+='<span class="tag status">'+esc(t)+'</span>';});
    return h;}
  function facetTags(it){
    var h="";
    (it.facets||[]).forEach(function(f){
      h+='<span class="tag ft">'+(FACET_LABEL[f]||f)+'</span>';});
    return h;}
  function codeTags(it){
    var h="";
    (it.codes||[]).slice(0,4).forEach(function(c){
      h+='<span class="tag code">'+esc(c)+'</span>';});
    return h;}

  /* 森空岛式讯息卡：封面图（无图占位）+ 标题跳转 + 简介 */
  function skCard(it){
    var c=document.createElement("div");
    c.className="skcard"+(favSet.has(it.id)?" faved":"");
    if(it.image){
      var img=document.createElement("img");
      img.className="cover";img.loading="lazy";img.referrerPolicy="no-referrer";
      img.src=it.image;img.alt="";
      c.appendChild(img);
    }else{
      var ph=document.createElement("div");
      ph.className="cover-empty";
      var tg=(it.title||"").match(/^【([^】]*)】/);
      ph.innerHTML='<span class="ph-i">🖼</span>'
        +'<span class="ph-g">'+esc(it.game||"官方")+'</span>'
        +'<span class="ph-t">'+esc(tg?tg[1]:"官方讯息")+' · 原帖未提供封面图</span>';
      c.appendChild(ph);
    }
    var body=document.createElement("div");body.className="sk-body";
    var link=it.url
      ? '<a class="title-link" href="'+esc(it.url)+'" target="_blank" rel="noopener">'+esc(it.title)
        +'<span class="ext">↗</span></a>'
      : '<div class="title-link">'+esc(it.title)+'</div>';
    var meta='<div class="meta">'
      +'<span class="game-pill">'+esc(it.game)+'</span>'
      +sourceBadge(it)+statusTags(it)+facetTags(it)+codeTags(it)
      +'<span class="when">🕐 '+esc(it.timeDisplay||"")+'</span>'
      +(it.timeDisplay?'<span style="color:#a7aec2">'+esc(it.timeKind)+'</span>':'')
      +(it.location?'<span style="color:#a7aec2">📍 '+esc(it.location)+'</span>':'')
      +(it.price?'<span class="dg-reward">💰 '+esc(it.price)+'</span>':'')
      +(it.channel?'<span style="color:#a7aec2">🏬 '+esc(it.channel)+'</span>':'')
      +'</div>';
    body.innerHTML='<div class="row">'+link+'</div>'+meta;
    c.appendChild(body);
    c.appendChild(starBtn(it.id));
    return c;}

  /* ===== 1. 时间线：紧凑关键节点式（日期头 + 文字行 + 徽标，标题点击跳原文） ===== */
  function renderTimeline(){
    var box=document.getElementById("timeline");
    box.innerHTML="";
    var nodes=DATA;
    if(!nodes.length){
      box.innerHTML='<div class="empty">未来约 100 天内暂无带明确活动时间的前瞻资讯</div>';
      return;}
    nodes.forEach(function(nd){
      var node=document.createElement("div");node.className="tl-node";
      var h=document.createElement("div");h.className="tl-date";
      h.textContent="📅 "+nd.label;
      node.appendChild(h);
      var html=nd.items.map(function(it){
        var s="";
        if(it.kind==="start"&&it.time) s+='<span class="tl-time">'+esc(it.time)+'</span> ';
        s+= it.url
          ? '<a class="tl-link" href="'+esc(it.url)+'" target="_blank" rel="noopener">'+esc(it.title)+'</a>'
          : esc(it.title);
        if(it.venue) s+=' <span class="tl-venue">📍 '+esc(it.venue)+'</span>';
        if(it.reward) s+=' <span class="tl-reward">🎁 '+esc(it.reward)+'</span>';
        if(it.kind==="deadline") s+=' <span class="tl-time">'+esc(it.time||"00:00")+' 截止</span>';
        (it.codes||[]).slice(0,3).forEach(function(c){
          s+=' <span class="code-pill">'+esc(c)+'</span>';});
        if(!(it.codes||[]).length&&it.hasCodeCtx)
          s+=' <span class="tl-note" title="官方在前瞻直播中公布兑换码，正文与回顾长图未以文字形式给出">直播期间公布</span>';
        s+= it.badge==="official"
          ? ' <span class="badge-off">官宣</span>'
          : ' <span class="badge-pend" title="社区信息 / 测试服爆料 / 待官方确认">待官宣</span>';
        return '<div class="tl-line">'+s+'</div>';
      }).join('');
      var t=document.createElement("div");t.className="tl-text";t.innerHTML=html;
      node.appendChild(t);
      box.appendChild(node);
    });
  }

  /* ===== 2. 版本前瞻汇总：表格（游戏 · 前瞻标题跳转 · 前瞻时间 · 兑换码 · 来源） ===== */
  function renderVersion(){
    var body=document.getElementById("versionBody");
    var sec=SECTIONS.version||{items:[]};
    body.innerHTML="";
    if(!sec.items.length){
      body.innerHTML='<div class="empty">暂无进行中/将开始的大版本前瞻与兑换码</div>';
      return;}
    var wrap=document.createElement("div");wrap.className="vtable-wrap";
    var t=document.createElement("table");t.className="vtable";
    t.innerHTML="<thead><tr><th>游戏</th><th>前瞻标题</th><th>前瞻时间</th>"
      +"<th>兑换码</th><th>来源</th></tr></thead>";
    var tb=document.createElement("tbody");
    sec.items.forEach(function(it){
      var tr=document.createElement("tr");
      tr.className=favSet.has(it.id)?"faved":"";
      var codes="";
      (it.codes||[]).slice(0,4).forEach(function(c){
        codes+='<span class="tag code">'+esc(c)+'</span> ';});
      if(!(it.codes||[]).length&&it.hasCodeCtx)
        codes+='<span class="vcode-note" title="官方在前瞻直播中公布兑换码，正文与回顾长图未以文字形式给出">直播期间公布</span>';
      if(it.deadline&&(it.codes.length||it.hasCodeCtx)&&it.timeKind!=="截止时间"
         &&(it.timeDisplay||"").indexOf("截止")<0){
        codes+='<div style="color:#c0392b;font-size:11.5px;margin-top:3px">'+esc(it.deadline)+' 截止</div>';}
      var ver=it.ver?'<span class="ver">'+esc(it.ver)+'</span>':'';
      var plat=it.platforms?'<div class="dg-plat">📺 '+esc(it.platforms)+'</div>':'';
      var reward=it.reward?' <span class="dg-reward">🎁 '+esc(it.reward)+'</span>':'';
      var title=it.url
        ? '<a href="'+esc(it.url)+'" target="_blank" rel="noopener">'+esc(it.title)+' ↗</a>'
        : esc(it.title);
      var src=it.leak
        ? '<span class="tag leak">测试服爆料</span>'
        : (it.official
          ? '<span class="tag official">'+esc(it.source||"官方")+'</span>'
          : '<span class="tag ft">'+esc(it.source||"第三方")+'</span>');
      tr.innerHTML='<td class="vgame">'+esc(it.game)+'<div style="margin-top:4px">'+starHtml(it.id)+'</div></td>'
        +'<td class="vtitle">'+title+ver+plat+reward+'</td>'
        +'<td class="vtime">'+esc(it.timeDisplay||"档期待确认")+'<small>'+esc(it.timeKind||"")+'</small></td>'
        +'<td class="vcodes">'+(codes||'<span style="color:#a7aec2">—</span>')+'</td>'
        +'<td class="vsrc">'+src+'</td>';
      tb.appendChild(tr);
    });
    t.appendChild(tb);wrap.appendChild(t);body.appendChild(wrap);
  }

  /* ===== 3. 联动活动：三个子分页（联动 / 快闪店 / 漫展展会，卡片带封面图） ===== */
  var COLLAB_TABS=[["collab","联动活动"],["popup","快闪店"],["expo","漫展展会"]];
  function renderCollab(){
    var tabs=document.getElementById("collabTabs");
    var box=document.getElementById("collabList");
    var sec=SECTIONS.collab||{items:[]};
    var labelOf={};COLLAB_TABS.forEach(function(p){labelOf[p[0]]=p[1];});
    function draw(sub){
      box.innerHTML="";
      var arr=sec.items.filter(function(it){return it.sub===sub;});
      if(!arr.length){
        box.innerHTML='<div class="empty">'+esc(labelOf[sub])
          +'：暂无进行中/将开始的资讯（快闪店 / 漫展通常随大版本或档期由官方公告，'
          +'建议关注官方微博 / 公众号与会员购）</div>';
        return;}
      arr.forEach(function(it){box.appendChild(skCard(it));});
    }
    tabs.innerHTML="";
    var firstNonEmpty=COLLAB_TABS[0][0];
    COLLAB_TABS.forEach(function(pair,i){
      var n=sec.items.filter(function(it){return it.sub===pair[0];}).length;
      if(i===0){
        for(var j=0;j<COLLAB_TABS.length;j++){
          if(sec.items.some(function(it){return it.sub===COLLAB_TABS[j][0];})){
            firstNonEmpty=COLLAB_TABS[j][0];break;}}}
      var b=document.createElement("button");
      b.textContent=pair[1]+" ("+n+")";
      if(pair[0]===firstNonEmpty)b.classList.add("active");
      b.onclick=function(){[...tabs.children].forEach(function(x){x.classList.remove("active");});
        b.classList.add("active");draw(pair[0]);};
      tabs.appendChild(b);
    });
    draw(firstNonEmpty);
  }

  /* ===== 4. 官方周边（森空岛式卡片） ===== */
  function renderMerch(){
    var box=document.getElementById("merchGrid");
    var sec=SECTIONS.merch||{items:[]};
    if(!sec.items.length){box.innerHTML='<div class="empty">暂无官方周边资讯</div>';return;}
    sec.items.forEach(function(it){box.appendChild(skCard(it));});
  }

  /* ===== 0. 信息汇总：顶部全量列表（带链接，按抓取时间倒序，不合并，含「新」已读标记） ===== */
  function secTypeLabel(sec, it){
    if(sec==="version") return "版本前瞻";
    if(sec==="merch") return "官方周边";
    var m={"collab":"联动活动","popup":"快闪店","expo":"漫展展会"};
    return m[it.sub]||"联动活动";
  }
  function refreshDigestCount(){
    var items=document.querySelectorAll("#digestList .digest-item");
    var unread=0;
    items.forEach(function(r){ if(!r.classList.contains("read")) unread++; });
    var cnt=document.getElementById("digestCount");
    if(cnt) cnt.textContent="("+items.length+(unread?" · 未读 "+unread:"")+")";
  }
  function renderDigest(){
    var box=document.getElementById("digestList");
    if(!box) return;
    box.innerHTML="";
    var all=[];
    ["version","collab","merch"].forEach(function(k){
      (SECTIONS[k]?SECTIONS[k].items:[]).forEach(function(it){
        if(it && it.url) all.push({it:it, sec:k});
      });
    });
    if(!all.length){
      box.innerHTML='<div class="empty">今日暂无带链接的前瞻资讯</div>';
      refreshDigestCount();
      return;
    }
    var cap=PAYLOAD.captured_at||"";
    all.sort(function(a,b){ return (cap).localeCompare(cap); }); // 同一批次抓取时间相同 → 保持稳定顺序
    all.forEach(function(o){
      var it=o.it, id=it.id;
      var row=document.createElement("div");
      row.className="digest-item"+(digestRead.has(id)?" read":"");
      var badge=document.createElement("span");
      badge.className="new-badge";badge.textContent="新";
      row.appendChild(badge);
      var type=document.createElement("span");type.className="di-type";
      type.textContent=secTypeLabel(o.sec,it);row.appendChild(type);
      var game=document.createElement("span");game.className="di-game";
      game.textContent=it.game||"";row.appendChild(game);
      var a=document.createElement("a");a.className="di-title";
      a.href=esc(it.url);a.target="_blank";a.rel="noopener";
      a.textContent=it.title||"(无标题)";
      a.onclick=function(){
        if(!digestRead.has(id)){
          markDigestRead(id);
          row.classList.add("read");
          refreshDigestCount();
        }
      };
      row.appendChild(a);
      var src=document.createElement("span");src.className="di-src";
      src.innerHTML=sourceBadge(it);row.appendChild(src);
      var time=document.createElement("span");time.className="di-time";
      time.textContent="🕐 "+(it.timeDisplay||"")+(cap?" · 抓取 "+cap:"");
      row.appendChild(time);
      box.appendChild(row);
    });
    refreshDigestCount();
  }

  document.getElementById("favToggle").onclick=function(e){
    e.preventDefault();
    if(!favOnly&&!favSet.size){
      alert("你还没有收藏任何资讯，点击条目/卡片右上角的 ☆ 即可特别关注。");return;}
    favOnly=!favOnly;
    document.body.classList.toggle("fav-only",favOnly);
    this.classList.toggle("active",favOnly);
  };

  document.getElementById("favCount").textContent="("+favSet.size+")";
  document.getElementById("genTime").textContent=PAYLOAD.generated||new Date().toLocaleString("zh-CN");
  (function(){
    var g={};
    (PAYLOAD.timeline||[]).forEach(function(nd){nd.items.forEach(function(it){
      g[it.title.split(" ")[0]]=1;});});
    (PAYLOAD.sections||[]).forEach(function(sec){sec.items.forEach(function(it){
      g[it.game||it.title]=1;});});
    var names=Object.keys(g).filter(function(k){return k&&k.length<12;});
    document.getElementById("genMeta").textContent="数据截至 "+(PAYLOAD.generated||"")
      +(names.length?" ｜ 覆盖："+names.slice(0,10).join("、"):"");
  })();
  document.getElementById("subtitle").textContent="关键节点时间线（未来约 100 天，标题可点击跳原文）· "
    +"板块2 汇总表格（标题跳原文+前瞻时间+兑换码+来源）· 板块3/4 统一封面图卡片（无图占位）";
  renderDigest();renderTimeline();renderVersion();renderCollab();renderMerch();
})();
</script>
</body>
</html>
"""


def main():
    os.makedirs(REPORT_DIR, exist_ok=True)
    cfg = load_config()          # config.toml → secrets.local.toml → 环境变量
    now = datetime.now()
    raw, errors = collect_all(cfg)
    kept = filter_ongoing(raw, now)
    # 补齐板块3/4 卡片封面（接口字段 → 正文首图 → 详情页 og:image）
    enrich_card_covers(kept)
    payload = build_payload(kept, now)
    html = TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    date_str = now.strftime("%Y-%m-%d")
    out_path = os.path.join(REPORT_DIR, f"官方社区前瞻总览_{date_str}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    # ---- 发布到 GitHub Pages（复用 publisher，config.toml [publish.github]） ----
    report_url = None
    try:
        pub_cfg = cfg.get("publish", {}).get("github", {})
        publisher = GitHubPagesPublisher(pub_cfg) if pub_cfg.get("enabled") else None
        if publisher and publisher.usable:
            report_url = publisher.publish(date_str, out_path)
            if report_url:
                print(f"[发布] 已发布网页：{report_url}")
            else:
                print("[发布] 本次未发布（网络异常或提交为空），飞书卡片将附本地路径")
        else:
            print("[发布] 未配置 [publish.github] 或未检测到 git，跳过网页发布")
    except Exception as e:
        print(f"[发布] 异常（不影响报告生成与推送）：{e}")

    games = {}
    all_cards = [c for sd in payload["sections"] for c in sd["items"]]
    for c in all_cards:
        games[c["game"]] = games.get(c["game"], 0) + 1
    official = sum(1 for c in all_cards if c["official"] and not c["leak"])
    leaks = sum(1 for c in all_cards if c["leak"])
    n_official_tl = sum(1 for nd in payload["timeline"] for it in nd["items"]
                        if it["badge"] == "official")
    print(f"\n渲染完成：{out_path}")
    print(f"时间线节点组 {len(payload['timeline'])} 组 / 条目 "
          f"{sum(len(nd['items']) for nd in payload['timeline'])} 条"
          f"（官宣 {n_official_tl} / 待官宣 "
          f"{sum(len(nd['items']) for nd in payload['timeline'])-n_official_tl}）")
    print(f"2-4 板块讯息共 {len(all_cards)} 条（官方 {official} / 测试服爆料 {leaks}）")
    for sd in payload["sections"]:
        if sd["key"] == "version":
            print(f"  板块[{sd['num']}] {sd['title']}: {sd['count']} 条（表格）")
        else:
            print(f"  板块[{sd['num']}] {sd['title']}: {sd['count']} 条"
                  f"（带封面图 {sum(1 for c in sd['items'] if c['image'])} 条，"
                  f"其余占位图）")
    if errors:
        print(f"\n未生效来源 {len(errors)} 条：")
        for e in errors:
            print(f"  · {e}")

    # ---- 飞书推送（复用 notifier，读 config.toml [notify]） ----
    try:
        notifiers = build_notifiers(cfg.get("notify", {}))
        usable = [n for n in notifiers if n.usable]
        if not usable:
            print("[推送] 未配置可用渠道（请在 config.toml [notify.feishu] 填 webhook），跳过")
        else:
            n_off = sum(1 for nd in payload["timeline"] for it in nd["items"]
                        if it["badge"] == "official")
            nt = sum(len(nd["items"]) for nd in payload["timeline"])
            sec = {sd["key"]: sd for sd in payload["sections"]}
            md = (f"**时间线**：{len(payload['timeline'])} 组 / {nt} 条"
                  f"（官宣 {n_off} / 待官宣 {nt - n_off}）\n")
            for k in ("version", "collab", "merch"):
                sd = sec.get(k)
                if sd:
                    md += f"- 板块{sd['num']} {sd['title']}：{sd['count']} 条\n"
            near = []
            for nd in payload["timeline"]:
                for it in nd["items"]:
                    if it["badge"] == "official":
                        near.append((nd["label"], it))
            if near:
                md += "\n**近期重点官宣**：\n"
                for label, it in near[:8]:
                    extra = ""
                    if it.get("venue"):
                        extra += f" · {it['venue']}"
                    if it.get("reward"):
                        extra += f" · {it['reward']}"
                    if it.get("url"):
                        md += f"- [{label}]({it['url']}) {it['title']}{extra}\n"
                    else:
                        md += f"- {label} {it['title']}{extra}\n"
            if report_url:
                md += f"\n📄 [点击查看完整网页]({report_url})"
            else:
                md += f"\n📄 本地报告：reports/官方社区前瞻总览_{date_str}.html"
            title = f"游戏前瞻总览 {date_str}"
            pushed = False
            for n in usable:
                ok, msg = n.send(title, md, template="blue")
                print(f"[推送] {n.name}: {msg}")
                if ok:
                    pushed = True
                    break
            if not pushed:
                print("[推送] 所有渠道发送失败，详见上方说明")
    except Exception as e:
        print(f"[推送] 异常（不影响报告生成）：{e}")


if __name__ == "__main__":
    main()
