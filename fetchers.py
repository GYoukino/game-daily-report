# -*- coding: utf-8 -*-
"""
采集器模块：负责从各游戏官方来源抓取公告/活动/更新信息。

每种来源一个类，都实现同一个接口：
    fetch(game_cfg) -> list[dict]
返回的每一条字典字段：
    game        游戏名
    item_id     唯一编号（用于去重）
    title       标题
    category    分类（公告/活动/资讯/动态）
    time        发布时间（字符串）
    url         详情页链接
    content     正文纯文本（已去 HTML 标签，可能截断）

新增游戏来源时：写一个新类，实现 fetch()，再注册到 FETCHERS 字典即可。
"""

import json
import re
import time
import html as html_mod
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


# ------------------------------------------------------------
# 基础工具
# ------------------------------------------------------------

def http_get(url, timeout=20, user_agent="", retry=1, headers=None):
    """GET 请求，返回文本。失败时重试 retry 次。headers 可传额外请求头。"""
    last_err = None
    hdrs = {
        "User-Agent": user_agent or "Mozilla/5.0",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if headers:
        hdrs.update(headers)
    for i in range(retry + 1):
        try:
            req = Request(url, headers=hdrs)
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (URLError, HTTPError, TimeoutError, OSError) as e:
            last_err = e
            if i < retry:
                time.sleep(2)
    raise RuntimeError(f"GET 失败: {url} -> {last_err}")


def http_post_json(url, payload, timeout=20, user_agent="", retry=1):
    """POST JSON 请求，返回解析后的 JSON。"""
    last_err = None
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for i in range(retry + 1):
        try:
            req = Request(url, data=body, headers={
                "User-Agent": user_agent or "Mozilla/5.0",
                "Content-Type": "application/json; charset=utf-8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except (URLError, HTTPError, TimeoutError, OSError, json.JSONDecodeError) as e:
            last_err = e
            if i < retry:
                time.sleep(2)
    raise RuntimeError(f"POST 失败: {url} -> {last_err}")


def strip_html(raw):
    """把 HTML 正文转为纯文本（去脚本/样式/标签、压缩空白）。"""
    if not raw:
        return ""
    text = re.sub(r"<!--.*?-->", " ", raw, flags=re.S)          # 去注释
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text,
                  flags=re.S | re.I)                             # 去脚本（含内联统计代码）
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text,
                  flags=re.S | re.I)                             # 去样式
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)          # 去所有标签
    text = html_mod.unescape(text)               # 还原 &nbsp; 等实体
    text = re.sub(r"[ \t\u3000]+", " ", text)    # 压缩空白
    text = re.sub(r"\n\s*\n+", "\n", text)       # 压缩空行
    return text.strip()


def clip(text, limit=300):
    """截断过长正文，用于报告/通知里展示摘要。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "……"


# 正文保留长度：日报展示只用前 ~110 字，但时效性提取（活动起止/截止日期）
# 需要看更深的正文，所以入库时保留较多字符（单条上限约 2KB，存储可控）
CONTENT_LIMIT = 2000


def make_item(game, item_id, title, category, time, url, content="",
              source_label="", is_official=True, image=""):
    """构造一条条目。

    source_label : 来源平台名（米游社 / 森空岛 / 库街区 / TapTap / NGA / 官网…）
    is_official  : 是否官方第一方渠道（决定报告徽标「X·官方」还是「X」）
    image        : 原帖封面图 URL（列表接口 / 正文首图，报告卡片用）
    """
    return {
        "game": game,
        "item_id": str(item_id),
        "title": (title or "").strip(),
        "category": category or "动态",
        "time": time or "",
        "url": url or "",
        "content": clip(strip_html(content), CONTENT_LIMIT),
        "source_label": source_label,
        "is_official": is_official,
        "image": (image or "").strip(),
    }


_IMG_TAG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)
_IMG_SKIP = ("logo", "icon", "favicon", "sprite", "avatar", "qr", "blank", "pixel")


def first_image(html):
    """正文 HTML 里的第一张可用图片（正文入库前先取，纯文本化后就没了）。"""
    if not html or "<img" not in html.lower():
        return ""
    for m in _IMG_TAG_RE.finditer(html):
        u = m.group(1).strip()
        low = u.lower()
        if not u or u.startswith("data:") or u.startswith("/") or low.endswith(".svg"):
            continue
        if any(k in low for k in _IMG_SKIP):
            continue
        if u.startswith("//"):
            u = "https:" + u
        return u
    return ""


def backfill_pages(cfg, params, default=1):
    """本轮要抓的页数：常规运行 1 页；`--backfill` 模式用 params.backfill_pages。"""
    if not cfg.get("_backfill"):
        return int(params.get("pages", 1) or 1)
    return max(int(params.get("pages", 1) or 1),
               int(params.get("backfill_pages", default) or default))


def _next_flight_blob(page):
    """拼接 Next.js 页面里的 self.__next_f.push flight 数据（还原为明文 JSON 片段）。"""
    chunks = re.findall(r"self\.__next_f\.push\(\[(.*?)\]\)</script>", page, re.S)
    blob = ""
    for c in chunks:
        parts = re.findall(r'"((?:[^"\\]|\\.)*)"', c)
        blob += "".join(parts)
    return blob.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


# ------------------------------------------------------------
# 采集器 1：米哈游 content_v2 接口（原神/星铁/绝区零/崩坏3/未定）
# ------------------------------------------------------------

class MihoyoContentFetcher:
    """米哈游系通用：GET content_v2 接口，解析 JSON 列表。

    这是米哈游官方社区「米游社」对应游戏官方标签的后端接口，属第一方官方渠道。

    支持翻页回溯历史：常规运行只取第 1 页；`--backfill` 模式下按
    `params.backfill_pages`（默认 6）连续翻页，可回溯到数年前的历史公告。

    多频道：config 里可用 `params.iChanIds`（逗号分隔，如 "721,720,723"）
    一次抓取多个官方标签频道并合并去重；不配置时沿用单个 `iChanId`。
    兑换码常出现在「前瞻特别节目预告」等帖正文，本采集器返回完整正文，
    由 classifier 在 enrich 阶段抽取，前瞻直播回顾帖里的码也能抓到。
    """

    source = "mihoyo_content"

    def fetch(self, game, cfg):
        params = dict(cfg.get("params", {}))
        detail_tpl = cfg.get("detail_url", "")
        source_label = cfg.get("source_label") or "米游社"
        # 多频道支持：iChanIds 优先，否则单 iChanId
        chan_ids = [c.strip() for c in str(params.pop("iChanIds", "")).split(",")
                    if c.strip()]
        if not chan_ids:
            chan_ids = [str(params.get("iChanId") or "").strip()]
        chan_ids = [c for c in chan_ids if c]
        pages = backfill_pages(cfg, params, default=6)
        start_page = int(params.pop("iPage", 1) or 1)
        result, seen = [], set()
        for chan in chan_ids:
            for offset in range(pages):
                p = dict(params, iChanId=chan, iPage=start_page + offset)
                url = cfg["url"] + ("&" if "?" in cfg["url"] else "?") + urlencode(p)
                try:
                    obj = json.loads(http_get(url, timeout=cfg["_timeout"],
                                              user_agent=cfg["_ua"]))
                except Exception:
                    break  # 翻到没有数据的页，或该频道无数据，跳到下一频道
                if obj.get("retcode") != 0:
                    if offset == 0:
                        # 单频道异常不打断其它频道
                        break
                    break
                items = (obj.get("data") or {}).get("list") or []
                if not items:
                    break
                for it in items:
                    iid = it.get("iInfoId")
                    if not iid or iid in seen:
                        continue
                    seen.add(iid)
                    result.append(make_item(
                        game=game,
                        item_id=iid,
                        title=it.get("sTitle"),
                        category=it.get("sCategoryName") or "公告",
                        time=it.get("dtCreateTime"),
                        url=detail_tpl.replace("{id}", str(iid)) if detail_tpl else "",
                        content=it.get("sContent") or it.get("sIntro") or "",
                        source_label=source_label,
                        is_official=True,
                        image=first_image(it.get("sContent") or ""),
                    ))
        return result


# ------------------------------------------------------------
# 采集器 2：明日方舟官网（Next.js 页面，数据藏在 __next_f 里）
# ------------------------------------------------------------

# 鹰角官网页面内嵌的两种条目结构：
#   A 资讯列表：{"cid","tab","sticky","title","author","displayTime","cover","extraCover","brief"}
#   B 首页活动位/banner：{"cid","tab","lang","title","desc","pcCover","mobileCover","link","ts"}
#   （B 类常是"限时活动即将开启"这类前瞻信息，必须一并收录）
_HG_PAT_A = re.compile(
    r'\{"cid":"(\d+)","tab":"([^"]+)","sticky":(?:true|false),'
    r'"title":"((?:[^"\\]|\\.)*)","author":"((?:[^"\\]|\\.)*)",'
    r'"displayTime":(\d+),"cover":"((?:[^"\\]|\\.)*)",'
    r'"extraCover":"((?:[^"\\]|\\.)*)",'
    r'"brief":"((?:[^"\\]|\\.)*)"\}'
)
_HG_PAT_B = re.compile(
    r'\{"cid":"(\d+)","tab":"([^"]+)","lang":"[^"]*",'
    r'"title":"((?:[^"\\]|\\.)*)","desc":"((?:[^"\\]|\\.)*)",'
    r'"pcCover":"((?:[^"\\]|\\.)*)","mobileCover":"((?:[^"\\]|\\.)*)",'
    r'"link":"([^"]*)","ts":(\d+)\}'
)


def _hypergryph_entries(blob):
    """解析鹰角页面内嵌数据，产出
    [(cid, tab, title, ts, link, brief, image), ...]。

    A 类（资讯列表）与 B 类（首页活动位）可能指向同一条资讯：合并为一条，
    正文 brief 以 A 类为准，封面图 / 链接互相兜底（A 类 cover 常为空）。
    """
    rows, seen = [], {}
    for cid, tab, title, _author, ts, cover, extra, brief in _HG_PAT_A.findall(blob):
        if cid in seen:
            continue
        seen[cid] = len(rows)
        rows.append([cid, tab, title, ts, "", brief, cover or extra])
    for cid, tab, title, desc, pc, mob, link, ts in _HG_PAT_B.findall(blob):
        img = pc or mob
        if cid in seen:
            row = rows[seen[cid]]
            if not row[6]:
                row[6] = img
            if link and not row[4]:
                row[4] = link
            continue
        seen[cid] = len(rows)
        rows.append([cid, tab, title, ts, link, desc or title, img])
    return [tuple(r) for r in rows]


def _ts_to_str(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return ""


class ArknightsFetcher:
    """明日方舟：解析 ak.hypergryph.com/news 页面内嵌数据（含首页活动位 banner）。"""

    source = "arknights_web"

    def fetch(self, game, cfg):
        page = http_get(cfg["url"], timeout=cfg["_timeout"], user_agent=cfg["_ua"])
        blob = _next_flight_blob(page)
        detail_tpl = cfg.get("detail_url", "")
        tab_names = {"0": "综合", "1": "公告", "2": "活动", "3": "新闻"}
        result = []
        for cid, tab, title, ts, link, brief, cover in _hypergryph_entries(blob):
            url = link or (detail_tpl.replace("{id}", cid) if detail_tpl else "")
            result.append(make_item(
                game=game,
                item_id=cid,
                title=title,
                category=tab_names.get(tab, "综合"),
                time=_ts_to_str(ts),
                url=url,
                content=brief or title,
                source_label="鹰角官网",
                is_official=True,
                image="https:" + cover if cover.startswith("//") else cover,
            ))
        return result


# ------------------------------------------------------------
# 采集器 3：明日方舟：终末地官网（服务端渲染 HTML，直接解析）
# ------------------------------------------------------------

class EndfieldFetcher:
    """终末地：解析官网 /news 页面内嵌数据（Next.js flight 结构，与方舟类似）。"""

    source = "endfield_web"

    def fetch(self, game, cfg):
        page = http_get(cfg["url"], timeout=cfg["_timeout"], user_agent=cfg["_ua"])
        blob = _next_flight_blob(page)
        detail_tpl = cfg.get("detail_url", "")
        tab_names = {"notices": "公告", "news": "资讯", "events": "活动"}
        result = []
        for cid, tab, title, ts, link, brief, cover in _hypergryph_entries(blob):
            url = link or (detail_tpl.replace("{id}", cid) if detail_tpl else "")
            result.append(make_item(
                game=game,
                item_id=cid,
                title=title,
                category=tab_names.get(tab, "动态"),
                time=_ts_to_str(ts),
                url=url,
                content=brief or title,
                source_label="鹰角官网",
                is_official=True,
                image="https:" + cover if cover.startswith("//") else cover,
            ))
        return result


# ------------------------------------------------------------
# 采集器 4：鸣潮官网（静态 JSON CDN）
# ------------------------------------------------------------

class KuroJsonFetcher:
    """鸣潮：官网把文章列表做成静态 JSON，一次拉全量。"""

    source = "kuro_json"

    def fetch(self, game, cfg):
        data = http_get(cfg["url"], timeout=cfg["_timeout"], user_agent=cfg["_ua"])
        arr = json.loads(data)
        if not isinstance(arr, list):
            raise RuntimeError("返回格式不是列表")
        detail_tpl = cfg.get("detail_url", "")
        type_names = {"52": "公告", "53": "活动", "54": "资讯"}
        result = []
        for it in arr:
            aid = it.get("articleId")
            if not aid:
                continue
            atype = str(it.get("articleType", ""))
            result.append(make_item(
                game=game,
                item_id=aid,
                title=it.get("articleTitle"),
                category=type_names.get(atype, "公告"),
                time=it.get("createTime"),
                url=detail_tpl,  # 鸣潮详情页链接规则未确认，统一给新闻页
                content=it.get("articleContent") or "",
                source_label="库洛官网",
                is_official=True,
                image=(it.get("articleCover") or it.get("cover")
                       or first_image(it.get("articleContent") or "")),
            ))
        return result


# ------------------------------------------------------------
# 采集器 5：重返未来：1999 官网（POST 接口）
# ------------------------------------------------------------

class BluepochFetcher:
    """重返未来：1999：POST 官网资讯接口（支持翻页回溯历史）。"""

    source = "bluepoch_api"

    def fetch(self, game, cfg):
        payload = dict(cfg.get("params", {}))
        start_page = int(payload.pop("current", 1) or 1)
        page_size = int(payload.get("pageSize", 20) or 20)
        pages = backfill_pages(cfg, payload, default=5)
        detail_tpl = cfg.get("detail_url", "")
        result, seen = [], set()
        for offset in range(pages):
            pl = dict(payload, current=start_page + offset, pageSize=page_size)
            try:
                obj = http_post_json(cfg["url"], pl, timeout=cfg["_timeout"],
                                     user_agent=cfg["_ua"])
            except Exception:
                break
            if obj.get("code") != 200:
                if offset == 0:
                    raise RuntimeError(f"接口返回异常: {obj.get('msg')}")
                break
            items = (obj.get("data") or {}).get("pageData") or []
            if not items:
                break
            for it in items:
                iid = it.get("id")
                if not iid or iid in seen:
                    continue
                seen.add(iid)
                result.append(make_item(
                    game=game,
                    item_id=iid,
                    title=it.get("title"),
                    category="公告",
                    time=it.get("onlineTime"),
                    url=detail_tpl.replace("{id}", str(iid)) if detail_tpl else "",
                    content=it.get("content") or "",
                    source_label="深蓝官网",
                    is_official=True,
                    image=(it.get("cover") or it.get("coverUrl")
                           or first_image(it.get("content") or "")),
                ))
            if len(items) < page_size:
                break
        return result


# ------------------------------------------------------------
# 采集器 6：哔哩哔哩会员购票务项目（show.bilibili.com 开放接口）
# ------------------------------------------------------------

class BiliMallFetcher:
    """会员购票务项目：按项目 ID 调用开放接口，产出展会/票务条目。

    config.toml [[games]] 配置：
        source    = "bili_mall"
        params    = { project_ids = "1001653,1001654" }   # 逗号分隔，可多个
        detail_url = "https://show.bilibili.com/platform/detail.html?id=1001653"

    说明：
      - 接口免登录，但需带 Referer（会员购站点域名）
      - 项目 ID 发现方式：浏览器打开会员购对应活动页，地址栏
        detail.html?id= 后面的数字（BW2026 = 1001653）
      - 抓取失败只记日志，不影响其他来源
    """

    source = "bili_mall"
    API = "https://show.bilibili.com/api/ticket/project/getV2"
    REFERER = "https://show.bilibili.com/"

    def fetch(self, game, cfg):
        raw_ids = str(cfg.get("params", {}).get("project_ids") or "").strip()
        if not raw_ids:
            return []
        detail_tpl = cfg.get("detail_url", "")
        result = []
        for pid in [s.strip() for s in raw_ids.split(",") if s.strip()]:
            url = f"{self.API}?id={pid}"
            data = http_get(url, timeout=cfg["_timeout"], user_agent=cfg["_ua"],
                            retry=1, headers={"Referer": self.REFERER})
            obj = json.loads(data)
            if not obj.get("data"):
                raise RuntimeError(f"接口返回异常: {obj.get('msg') or obj.get('message')}")
            d = obj["data"]
            name = (d.get("name") or "").strip()
            if not name:
                continue
            result.append(make_item(
                game=game,
                item_id=f"mall_{pid}",
                title=name,
                category="票务",
                time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                url=detail_tpl.replace("{id}", pid) if detail_tpl else url,
                content=self._describe(d),
                source_label="B站会员购",
                is_official=True,
            ))
        return result

    @staticmethod
    def _describe(d):
        """把项目字段整理成含中文日期的描述文本（供分类器提取时效性）。"""
        def fmt_ts(ts):
            try:
                return datetime.fromtimestamp(int(ts)).strftime("%Y年%m月%d日%H:%M")
            except (TypeError, ValueError, OSError):
                return ""
        parts = []
        st, ed = fmt_ts(d.get("start_time")), fmt_ts(d.get("end_time"))
        if st:
            parts.append(f"开始时间：{st}")
        if ed:
            parts.append(f"结束时间：{ed}")
        venue = d.get("venue_info") if isinstance(d.get("venue_info"), dict) else {}
        place = d.get("place_info") if isinstance(d.get("place_info"), dict) else {}
        venue_name = (venue.get("name") or "").strip()
        place_name = (place.get("name") or "").strip()
        if venue_name:
            parts.append(f"场馆：{venue_name}{(' ' + place_name) if place_name else ''}")
        lo, hi = d.get("price_low"), d.get("price_high")
        if lo:
            price = (f"票价：{lo / 100:.0f} 元起" if not hi or hi == lo
                     else f"票价：{lo / 100:.0f}-{hi / 100:.0f} 元")
            parts.append(price)
        sale_st = fmt_ts(d.get("sale_start"))
        if sale_st:
            parts.append(f"开售时间：{sale_st}")
        desc = strip_html(d.get("description") or "")[:200]
        if desc:
            parts.append(f"简介：{desc}")
        return "；".join(parts)


# ------------------------------------------------------------
# 采集器 7：海外展会官网新闻/活动列表（Next.js SSR 或静态页）
# ------------------------------------------------------------

class ExpoNextFetcher:
    """海外展会官网（gamescom / TGA / Summer Game Fest 等）新闻与活动列表。

    config.toml [[games]] 配置（两种模式二选一）：
        # Next.js 服务端渲染页面（TGA / gamescom 官网）
        source = "expo_next"
        url    = "https://thegameawards.com/news"
        params = { mode = "flight", link_prefix = "https://thegameawards.com" }

        # 静态 HTML 页面（Summer Game Fest 官网）
        source = "expo_next"
        url    = "https://www.summergamefest.com/events"
        params = { mode = "anchors", block_marker = "cms-event-item",
                   title_marker = "event-name-title", desc_marker = "event-desc-text",
                   link_prefix = "https://www.summergamefest.com" }

    说明：
      - flight 模式条目自带发布日期的用官网日期，否则用抓取时间兜底
      - flight 模式可用 title_exclude 过滤标题含指定词的条目（如回放类噪声），
        或用 title_include 只保留标题含指定词的条目（如展会官方节目）
      - anchors 模式跳过含 "Rewatch" 的块（已结束的活动）
      - 抓取失败只记日志，不影响其他来源
    """

    source = "expo_next"

    def fetch(self, game, cfg):
        page = http_get(cfg["url"], timeout=cfg["_timeout"], user_agent=cfg["_ua"])
        mode = str(cfg.get("params", {}).get("mode") or "flight")
        if mode == "anchors":
            return self._from_anchors(game, cfg, page)
        return self._from_flight(game, cfg, page)

    # ---- Next.js flight 数据（TGA / gamescom） ----

    def _from_flight(self, game, cfg, page):
        blob = _next_flight_blob(page)
        prefix = str(cfg.get("params", {}).get("link_prefix") or "").rstrip("/")
        excl = [s.strip().lower() for s in
                str(cfg.get("params", {}).get("title_exclude") or "").split(",")
                if s.strip()]
        incl = [s.strip().lower() for s in
                str(cfg.get("params", {}).get("title_include") or "").split(",")
                if s.strip()]
        result, seen = [], set()
        for m in re.finditer(r'"slug":"([a-z0-9][a-z0-9-]{5,})"', blob):
            slug = m.group(1)
            if slug in seen:
                continue
            seen.add(slug)
            seg = blob[m.start():m.start() + 600]
            tm = re.search(r'"title":"((?:[^"\\]|\\.)*)"', seg)
            if not tm:
                continue
            title = tm.group(1).replace('\\"', '"').strip()
            if len(title) < 6:
                continue
            if any(x in title.lower() for x in excl):
                continue
            if incl and not any(x in title.lower() for x in incl):
                continue
            um = re.search(r'"(?:uri|videoUrl)":"([^"]+)"', seg)
            link = um.group(1) if um else ""
            if link.startswith("/"):
                link = prefix + link
            dm = re.search(r'"date":"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})', seg)
            pub = (f"{dm.group(1)} {dm.group(2)}:00" if dm
                   else datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            result.append(make_item(
                game=game, item_id=slug, title=title,
                category="展会动态", time=pub, url=link, content=title,
                source_label="展会官网", is_official=True,
            ))
        return result

    # ---- 静态页按块解析（Summer Game Fest） ----

    def _from_anchors(self, game, cfg, page):
        p = cfg.get("params", {})
        block_marker = str(p.get("block_marker") or "")
        title_marker = str(p.get("title_marker") or "")
        desc_marker = str(p.get("desc_marker") or "")
        prefix = str(p.get("link_prefix") or "").rstrip("/")
        if not block_marker or not title_marker:
            return []
        result, seen = [], set()
        for block in page.split(block_marker)[1:]:
            block = block[:4000]
            if "Rewatch" in block:  # 已结束的活动，跳过
                continue
            tm = re.search(re.escape(title_marker) + r'[^>]*>([^<]{4,120})<', block)
            if not tm:
                continue
            title = strip_html(tm.group(1))
            lm = re.search(r'href="(/[^"#?][^"]*)"', block)
            slug = lm.group(1).rstrip("/") if lm else ""
            key = slug or title
            if key in seen:
                continue
            seen.add(key)
            desc = ""
            if desc_marker:
                dm = re.search(re.escape(desc_marker) + r'[^>]*>([^<]{4,300})<', block)
                if dm:
                    desc = strip_html(dm.group(1))
            link = (prefix + slug) if slug.startswith("/") else (slug or cfg["url"])
            item_id = re.sub(r"[^a-z0-9]", "", slug.lower())[-24:] or title[:24]
            result.append(make_item(
                game=game, item_id=f"expo_{item_id}", title=title,
                category="展会动态",
                time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                url=link, content=desc or title,
                source_label="展会官网", is_official=True,
            ))
        return result


# ------------------------------------------------------------
# 社区采集器（第一方官方社区 + 玩家社区交叉参考）
#
# 设计原则（与用户确认）：
#   - 米游社 / 森空岛 / 库街区 / TapTap 官方动态 属「官方第一方渠道」，
#     标 source_label 且 is_official=True -> 报告显示「X·官方」。
#   - NGA 属玩家社区交叉参考，is_official=False -> 报告显示「NGA」并标注来源，
#     不代表官方确认。
#   - 需要登录令牌（森空岛 cred / 库街区 token）的源：未配置令牌时优雅返回空，
#     并在日志提示需填令牌，不影响其它源。
# ------------------------------------------------------------


class SenkongdaoFetcher:
    """森空岛（鹰角网络官方社区）帖子列表。

    属官方第一方渠道（source_label=森空岛，is_official=True）。
    森空岛论坛接口需要登录凭据 cred（由鹰角通行证 OAuth 换取），纯公开抓取不可行。

    config.toml 配置：
        source      = "senkongdao"
        name        = "明日方舟"            # 与游戏同名，便于报告归并
        params      = { cred = "你的skland_cred",
                        forum_id = "0" }     # forum_id 留空用默认
        url         = "https://zonai.skland.com/api/v1/forum/post/list"

    未配置 cred 时返回空（日志提示），不影响其它源。
    """

    source = "senkongdao"

    def fetch(self, game, cfg):
        cred = (cfg.get("params", {}).get("cred") or "").strip()
        if not cred:
            raise RuntimeError("森空岛需要登录凭据 cred（请在 config.toml 填 params.cred）")
        api_url = cfg.get("url") or "https://zonai.skland.com/api/v1/forum/post/list"
        forum_id = str(cfg.get("params", {}).get("forum_id") or "").strip()
        headers = {"cred": cred, "Accept": "application/json"}
        try:
            obj = json.loads(http_get(api_url, timeout=cfg["_timeout"],
                                      user_agent=cfg["_ua"], headers=headers))
        except Exception as e:
            raise RuntimeError(f"森空岛接口请求失败: {e}")
        # 不同版本字段名兼容：postList / list / data
        data = (obj.get("data") or obj) if isinstance(obj, dict) else obj
        posts = (data.get("postList") if isinstance(data, dict) else None) \
            or (data.get("list") if isinstance(data, dict) else None) \
            or (obj.get("data") if isinstance(obj, dict) else None) or []
        if not isinstance(posts, list):
            posts = []
        result = []
        for it in posts:
            pid = it.get("postId") or it.get("id") or it.get("tid")
            if not pid:
                continue
            title = it.get("title") or it.get("subject") or ""
            if not title:
                continue
            ts = it.get("publishTime") or it.get("createTime") or it.get("postDate")
            time_str = (_ts_to_str(int(ts)) if str(ts).isdigit() and ts
                        else (ts or ""))
            content = it.get("content") or it.get("brief") or it.get("summary") or ""
            result.append(make_item(
                game=game, item_id=f"sk_{pid}", title=title,
                category="社区帖", time=time_str,
                url=f"https://www.skland.com/forum/post/{pid}",
                content=content, source_label="森空岛", is_official=True,
            ))
        return result


class KurobbsFetcher:
    """库街区（库洛官方社区）帖子列表。

    属官方第一方渠道（source_label=库街区，is_official=True）。
    `forum/list` 接口需要登录令牌 token（库洛账号）。

    config.toml 配置（鸣潮示例）：
        source   = "kurobbs"
        name     = "鸣潮"
        params   = { token = "你的库街区token",
                     game_id = 3,        # 鸣潮=3，战双=2
                     forum_id = 9,       # 鸣潮推荐=9，天诚茶馆=10，同人=11
                     page_size = 20 }
        url      = "https://api.kurobbs.com/forum/list"

    未配置 token 时返回空（日志提示），不影响其它源。
    """

    source = "kurobbs"

    def fetch(self, game, cfg):
        token = (cfg.get("params", {}).get("token") or "").strip()
        if not token:
            raise RuntimeError("库街区需要登录令牌 token（请在 config.toml 填 params.token）")
        api_url = cfg.get("url") or "https://api.kurobbs.com/forum/list"
        p = cfg.get("params", {})
        game_id = int(p.get("game_id", 3) or 3)
        forum_id = int(p.get("forum_id", 9) or 9)
        page_size = int(p.get("page_size", 20) or 20)
        page_index = int(p.get("page_index", 1) or 1)
        search_type = int(p.get("search_type", 2) or 2)  # 推荐板块默认最新回复
        body = (f"forumId={forum_id}&gameId={game_id}&pageIndex={page_index}"
                f"&pageSize={page_size}&searchType={search_type}&timeType=0&topicId=0")
        headers = {
            "token": token,
            "osversion": "Android",
            "devcode": "2fba3859fe9bfe9099f2696b8648c2c6",
            "distinct_id": "765485e7-30ce-4496-9a9c-a2ac1c03c02c",
            "countrycode": "CN",
            "ip": "10.0.2.233",
            "model": "2211133C",
            "source": "android",
            "lang": "zh-Hans",
            "version": "1.0.9",
            "versioncode": "1090",
            "content-type": "application/x-www-form-urlencoded",
            "accept-encoding": "gzip",
            "user-agent": "okhttp/3.10.0",
        }
        try:
            raw = http_get(api_url, timeout=cfg["_timeout"],
                           user_agent=cfg["_ua"], headers=headers)
        except Exception as e:
            raise RuntimeError(f"库街区接口请求失败: {e}")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            # 库街区部分接口返回 gzip，urllib 已解；仍失败则视为无数据
            return []
        if int(obj.get("code", 200)) != 200:
            raise RuntimeError(f"库街区接口返回异常: {obj.get('msg')}")
        data = obj.get("data") or {}
        posts = data.get("postList") or []
        result = []
        for it in posts:
            pid = it.get("postId") or it.get("id")
            if not pid:
                continue
            title = it.get("title") or it.get("topic") or ""
            if not title:
                continue
            content = it.get("content") or it.get("brief") or ""
            result.append(make_item(
                game=game, item_id=f"kb_{pid}", title=title,
                category="社区帖", time="",
                url=f"https://www.kurobbs.com/post/{pid}",
                content=content, source_label="库街区", is_official=True,
            ))
        return result


class TaptapFetcher:
    """TapTap 官方动态 / 社区帖子列表（交叉参考）。

    属官方第一方动态源（source_label=TapTap，is_official=True）。
    TapTap 的公开网页接口历史上多次变更（webapiv2 等路径曾失效），
    这里用可配置 url 做最佳努力抓取：成功则解析，失败则优雅返回空。

    config.toml 配置（鸣潮示例）：
        source = "taptap"
        name   = "鸣潮"
        params = { app_id = "xxx" }   # TapTap 游戏 id
        url    = "https://www.taptap.cn/webapiv2/app/moment/v2/by_app?app_id=xxx&limit=10&offset=0"

    说明：如接口路径随版本变化，请更新上面的 url；本采集器不影响其它源。
    """

    source = "taptap"

    def fetch(self, game, cfg):
        api_url = cfg.get("url")
        if not api_url:
            # 未显式给 url 时，尝试用 app_id 拼一个常见端点
            app_id = (cfg.get("params", {}).get("app_id") or "").strip()
            if not app_id:
                raise RuntimeError("TapTap 需要配置 url 或 params.app_id")
            api_url = (f"https://www.taptap.cn/webapiv2/app/moment/v2/by_app"
                       f"?app_id={app_id}&limit=10&offset=0")
        try:
            raw = http_get(api_url, timeout=cfg["_timeout"], user_agent=cfg["_ua"])
        except Exception as e:
            raise RuntimeError(f"TapTap 接口请求失败: {e}")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return []
        # 兼容多种返回结构：data.list / data.data / 直接数组
        data = obj.get("data") if isinstance(obj, dict) else None
        if isinstance(data, dict):
            posts = data.get("list") or data.get("moment_list") or data.get("data") or []
        elif isinstance(data, list):
            posts = data
        else:
            posts = []
        if not isinstance(posts, list):
            posts = []
        result = []
        for it in posts:
            if not isinstance(it, dict):
                continue
            pid = (it.get("id") or it.get("moment_id")
                   or it.get("post_id") or it.get("tid"))
            if not pid:
                continue
            title = (it.get("title") or it.get("content") or it.get("text")
                     or "").strip()
            if not title:
                continue
            # 时间字段兼容（秒/毫秒）
            ts = it.get("created_at") or it.get("create_time") or it.get("postDate")
            time_str = ""
            if ts:
                try:
                    iv = int(ts)
                    if iv > 10**11:  # 毫秒
                        iv //= 1000
                    time_str = _ts_to_str(iv)
                except (ValueError, TypeError):
                    time_str = str(ts)
            content = it.get("content") or it.get("text") or ""
            result.append(make_item(
                game=game, item_id=f"tt_{pid}", title=title,
                category="动态", time=time_str,
                url=it.get("url") or f"https://www.taptap.cn/app/{cfg.get('params', {}).get('app_id', '')}",
                content=content, source_label="TapTap", is_official=True,
            ))
        return result


class NgaFetcher:
    """NGA 玩家社区某子版帖子列表（社区交叉参考，非官方）。

    source_label=NGA，is_official=False -> 报告显示「NGA」并标注来源，
    不代表官方确认。

    config.toml 配置：
        source = "nga"
        name   = "原神"                 # 与游戏同名，便于报告归并
        params = { fid = 650 }          # NGA 子版 fid（原神=650，绝区零=853，
                                         # 鸣潮=854，明日方舟=-34587507）

    说明：NGA 帖子以攻略/杂谈为主，分类器只保留命中前瞻关键词（兑换码/联动/
    周边/快闪/漫展/前瞻）的帖子，自动过滤无关内容。
    """

    source = "nga"
    HOST = "https://nga.178.com"
    # NGA 旧接口需要特定 UA，否则 403
    UA = "Nga_Official"

    def fetch(self, game, cfg):
        fid = str(cfg.get("params", {}).get("fid") or "").strip()
        if not fid:
            raise RuntimeError("NGA 需要配置 params.fid（子版 id）")
        url = (f"{self.HOST}/thread.php?fid={fid}"
               f"&__output=11&__inchst=UTF8&page=1")
        # 本采集器固定用 Nga_Official UA（覆盖传入的普通 UA）
        raw = http_get(url, timeout=cfg["_timeout"], user_agent=self.UA)
        # 响应为 JSON 片段：{"__T":[{tid,subject,author,postdate,...}],"__ROWS":N}
        # 注意主题里常含方括号（如「[版本公告]」），且响应不一定含 __ROWS 字段，
        # 故用「括号配平」精确截取 __T 对应的数组（正确处理字符串内的 [ ]）。
        i = raw.find('__T":')
        if i < 0:
            return []
        start = raw.find("[", i)
        if start < 0:
            return []
        depth, in_str, esc = 0, False, False
        end = -1
        for k in range(start, len(raw)):
            c = raw[k]
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    end = k
                    break
        if end < 0:
            return []
        try:
            arr = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return []
        result = []
        for it in arr:
            if not isinstance(it, dict):
                continue
            tid = it.get("tid")
            if not tid:
                continue
            subject = html_mod.unescape((it.get("subject") or "").strip())
            if not subject:
                continue
            author = html_mod.unescape(it.get("author") or "")
            ts = it.get("postdate") or it.get("lastpost")
            time_str = ""
            if ts:
                try:
                    time_str = _ts_to_str(int(ts))
                except (ValueError, TypeError):
                    time_str = ""
            title = subject + (f"（@{author}）" if author else "")
            result.append(make_item(
                game=game, item_id=f"nga_{tid}", title=title,
                category="社区帖", time=time_str,
                url=f"https://bbs.nga.cn/read.php?tid={tid}",
                content=subject, source_label="NGA", is_official=False,
            ))
        return result


# ------------------------------------------------------------
# 注册表：config.toml 里的 source 值 -> 采集器类
# ------------------------------------------------------------

FETCHERS = {
    "mihoyo_content": MihoyoContentFetcher,
    "arknights_web": ArknightsFetcher,
    "endfield_web": EndfieldFetcher,
    "kuro_json": KuroJsonFetcher,
    "bluepoch_api": BluepochFetcher,
    "bili_mall": BiliMallFetcher,
    "expo_next": ExpoNextFetcher,
    "senkongdao": SenkongdaoFetcher,
    "kurobbs": KurobbsFetcher,
    "taptap": TaptapFetcher,
    "nga": NgaFetcher,
}
