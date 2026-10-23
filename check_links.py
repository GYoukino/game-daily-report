# -*- coding: utf-8 -*-
"""链接健康检查：防止「SPA 假 200」类 404 回归。

背景（2026-10-23 用户反馈）：
米哈游各官网是 SPA，服务端对任意路径都返回 200 + 空壳 HTML，
前端再按路由渲染；路由不存在时才显示 404。所以「HTTP 200」≠ 链接有效。

本脚本两层校验：
1. 静态：config.toml 里各 detail_url 必须命中已核实的官网路由白名单；
2. 动态：抓取最新日报 HTML 内全部外链，HTTP 状态必须为 200
   （这一步只兜网络层错误，SPA 路由是否有效靠第 1 层保证）。

用法：
    python check_links.py              # 检查 config + 最新日报
    python check_links.py --config     # 只检查 config
"""
import glob
import json
import os
import re
import ssl
import sys
import tomllib
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "config.toml")

# 已逐一核实的官网详情页路由（2026-10-23，来源：官网 SPA 路由表 + 搜索引擎规范地址）
#   原神   ys.mihoyo.com  /main/m/news/detail/{id}
#   星铁   sr.mihoyo.com  /news/{id}
#   绝区零 zzz.mihoyo.com /news/{id}
#   崩坏3  bh3.mihoyo.com /news/cate_0/{id}   （cate_0 = 频道 693，官网 JS 映射 {693:"cate_0",…}）
VERIFIED_DETAIL = {
    "ys.mihoyo.com": "https://ys.mihoyo.com/main/m/news/detail/{id}",
    "sr.mihoyo.com": "https://sr.mihoyo.com/news/{id}",
    "zzz.mihoyo.com": "https://zzz.mihoyo.com/news/{id}",
    "bh3.mihoyo.com": "https://bh3.mihoyo.com/news/cate_0/{id}",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def check_config():
    """校验 config.toml 中 mihoyo_content 来源的 detail_url。"""
    with open(CONFIG, "rb") as f:
        cfg = tomllib.load(f)
    problems = []
    for g in cfg.get("games", []):
        if g.get("source") != "mihoyo_content":
            continue
        tpl = g.get("detail_url") or ""
        if "{id}" not in tpl:
            continue  # 首页/无详情页的源（如未定事件簿）跳过
        host = re.sub(r"^https?://", "", tpl).split("/")[0]
        expect = VERIFIED_DETAIL.get(host)
        if expect is None:
            problems.append(f"[跳过] {g['name']}: {host} 不在白名单，请人工核实后再加入")
            continue
        if tpl != expect:
            problems.append(f"[错误] {g['name']}: {tpl}\n       应为: {expect}")
    return problems


def latest_report():
    files = sorted(glob.glob(os.path.join(BASE, "reports", "官方社区前瞻总览_*.html")))
    return files[-1] if files else None


def report_urls(path):
    html = open(path, encoding="utf-8").read()
    m = re.search(r'<script type="application/json" id="payload">([\s\S]*?)</script>', html)
    if not m:
        return []
    d = json.loads(m.group(1))
    urls = set()
    for g in d.get("timeline") or []:
        for it in g["items"]:
            if it.get("url"):
                urls.add(it["url"])
    for sec in d.get("sections") or []:
        for it in sec.get("items") or []:
            if it.get("url"):
                urls.add(it["url"])
    return sorted(urls)


def check_urls(urls):
    bad = []
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15, context=_CTX) as r:
                if r.status != 200:
                    bad.append((u, f"HTTP {r.status}"))
        except urllib.error.HTTPError as e:
            bad.append((u, f"HTTP {e.code}"))
        except Exception as e:
            bad.append((u, f"{type(e).__name__}: {str(e)[:50]}"))
    return bad


def main():
    only_config = "--config" in sys.argv
    print("== 1) config.toml 详情页路由校验 ==")
    problems = check_config()
    for p in problems:
        print("   ", p)
    if not problems:
        print("    OK：全部命中已核实路由")
    print()

    if only_config:
        return 1 if any(p.startswith("[错误]") for p in problems) else 0

    rep = latest_report()
    if not rep:
        print("== 2) 未找到日报文件，跳过链接检查 ==")
        return 1 if any(p.startswith("[错误]") for p in problems) else 0
    print(f"== 2) 日报链接检查：{os.path.basename(rep)} ==")
    urls = report_urls(rep)
    bad = check_urls(urls)
    print(f"    共 {len(urls)} 个链接，异常 {len(bad)} 个")
    for u, why in bad:
        print(f"    [异常] {why} | {u}")
    if not bad:
        print("    OK：全部返回 200")
    print()
    print("注意：米哈游官网为 SPA，任意路径都返回 200；"
          "若前端 404 需回到第 1 层核对官网路由。")
    return 1 if (bad or any(p.startswith("[错误]") for p in problems)) else 0


if __name__ == "__main__":
    sys.exit(main())
