# -*- coding: utf-8 -*-
"""
消息推送模块：多渠道，按优先级发送（第一个成功的渠道生效，其余作备用）。

当前支持的渠道（配置见 config.toml 的 [notify.*] 段）：
    飞书群机器人    [notify.feishu]     免费、无每日条数限制，推荐首选
    Server酱        [notify.serverchan] 免费版 5 条/天，作备用渠道

如何新增渠道：
    1. 仿照下面写一个类，要求有 name 属性、usable 属性、send(title, markdown, template) 方法；
    2. 在文件末尾的 build_notifiers() 里注册（注意优先级顺序）；
    3. 在 config.toml 里加一段 [notify.你的渠道名]。
"""

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


# ============================================================
# 消息内容组装（各渠道共用的 Markdown 文本）
# ============================================================

def format_forward_items(game, items, report_url=None):
    """组装某款游戏的前瞻资讯推送内容（Markdown）。

    items 需为 enrich 后的前瞻条目（带 fwd_cls / fwd_tags / fwd_start /
    fwd_deadline 字段）。
    report_url: 当日网页前瞻日报链接，有则附在末尾。
    """
    from classifier import CATEGORY_DEFS
    names = dict(CATEGORY_DEFS)
    lines = [f"**{game}** 新增前瞻资讯 {len(items)} 条：", ""]
    for it in items[:10]:  # 一次最多列 10 条，避免消息过长
        cls_name = names.get(it.get("fwd_cls"), "📌 资讯").split(" ", 1)[-1]
        time_hint = ""
        st, dl = it.get("fwd_start"), it.get("fwd_deadline")
        if st:
            time_hint = f"（{st:%m/%d %H:%M} 开始）"
        elif dl:
            time_hint = f"（{dl:%m/%d %H:%M} 截止）"
        title = it.get("title") or ""
        line = f"- 【{cls_name}】[{title}]({it['url']})" if it.get("url") \
            else f"- 【{cls_name}】{title}"
        lines.append(line + time_hint)
    if len(items) > 10:
        lines.append(f"- ……等共 {len(items)} 条，详见今日前瞻日报")
    if report_url:
        lines += ["", f"📄 [查看今日前瞻日报]({report_url})"]
    return "\n".join(lines)


def format_daily_summary(date_str, stats, total, report_url=None):
    """组装每日汇总推送内容（Markdown）。

    report_url: 当日网页前瞻日报链接（GitHub Pages），有则附在末尾。
    """
    lines = [f"今日（{date_str}）共收录 **{total}** 条前瞻资讯：", ""]
    for game, n in stats:
        lines.append(f"- {game}：{n} 条")
    lines.append("")
    if report_url:
        lines.append(f"📄 [查看今日前瞻日报]({report_url})")
    else:
        lines.append("完整内容见当日前瞻日报（reports 目录）。")
    return "\n".join(lines)


# ============================================================
# 渠道一：飞书群机器人（Webhook，免费，无每日条数限制）
# ============================================================

class FeishuNotifier:
    """飞书群自定义机器人。

    官方文档：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
    限制：单机器人每分钟最多约 100 条（个人使用远远够）；
          消息发到飞书群聊，需先在群里"添加机器人 -> 自定义机器人"。
    安全设置二选一：
        - 签名校验（推荐）：创建机器人时保存好签名密钥，填到 secret；
        - 自定义关键词：secret 留空，但每条消息必须包含该关键词（当前模板不含，不推荐）。
    """

    name = "飞书"

    def __init__(self, enabled, webhook, secret="", timeout=15):
        self.enabled = bool(enabled)
        self.webhook = (webhook or "").strip()
        self.secret = (secret or "").strip()
        self.timeout = timeout

    @property
    def usable(self):
        return self.enabled and self.webhook.startswith("https://")

    @staticmethod
    def gen_sign(secret, timestamp):
        """飞书官方签名算法（见 open.feishu.cn 自定义机器人文档）：

        1. 拼接待签名字符串：timestamp + "\\n" + secret
        2. 以该字符串为**密钥**，对**空字符串**做 HmacSHA256
        3. 结果 Base64 编码

        注意：密钥和消息的角色容易搞反（钉钉的用法是密钥=secret、消息=待签串，
        飞书正好相反），搞反会报 code=19021 sign match fail。
        """
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(string_to_sign.encode("utf-8"),
                          digestmod=hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _build_payload(self, title, markdown, template):
        """组装交互式卡片消息（lark_md 支持加粗/链接/换行）。"""
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title[:60]},
                    "template": template,
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": markdown}},
                ],
            },
        }
        if self.secret:  # 启用签名校验时附带 timestamp + sign
            ts = str(int(time.time()))
            payload["timestamp"] = ts
            payload["sign"] = self.gen_sign(self.secret, ts)
        return payload

    def send(self, title, markdown, template="blue"):
        """发送一条卡片消息。返回 (成功?, 说明)。"""
        if not self.usable:
            return False, "未启用（enabled=false 或未填 webhook），已跳过"
        body = json.dumps(self._build_payload(title, markdown, template),
                          ensure_ascii=False).encode("utf-8")
        try:
            req = Request(self.webhook, data=body,
                          headers={"Content-Type": "application/json; charset=utf-8"})
            with urlopen(req, timeout=self.timeout) as resp:
                obj = json.loads(resp.read().decode("utf-8", errors="replace"))
            # 飞书成功返回 {"code":0,"msg":"success"}（旧接口为 {"StatusCode":0}）
            if obj.get("code") == 0 or obj.get("StatusCode") == 0:
                return True, "推送成功"
            return False, f"推送失败: code={obj.get('code')} msg={obj.get('msg') or obj}"
        except (URLError, HTTPError, TimeoutError, OSError, json.JSONDecodeError) as e:
            return False, f"推送网络错误: {e}"


# ============================================================
# 渠道二：Server酱（消息发送到微信，免费版 5 条/天）
# ============================================================

class ServerChanNotifier:
    """Server酱（https://sct.ftqq.com/）。免费版每天 5 条，作备用渠道。"""

    name = "Server酱"

    def __init__(self, enabled, send_key, timeout=15):
        self.enabled = bool(enabled)
        self.send_key = (send_key or "").strip()
        self.timeout = timeout

    @property
    def usable(self):
        return self.enabled and bool(self.send_key)

    def send(self, title, markdown, template=None):
        """发送一条消息（template 参数飞书专用，此处忽略）。返回 (成功?, 说明)。"""
        if not self.usable:
            return False, "未启用（enabled=false 或未填 send_key），已跳过"
        url = f"https://sctapi.ftqq.com/{self.send_key}.send"
        body = urlencode({
            "title": title[:32],       # 标题最长 32 字符
            "desp": markdown or "",
        }).encode("utf-8")
        try:
            req = Request(url, data=body,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urlopen(req, timeout=self.timeout) as resp:
                obj = json.loads(resp.read().decode("utf-8", errors="replace"))
            if obj.get("code") == 0:
                return True, "推送成功"
            return False, f"推送失败: {obj.get('message') or obj}"
        except (URLError, HTTPError, TimeoutError, OSError, json.JSONDecodeError) as e:
            return False, f"推送网络错误: {e}"


# ============================================================
# 渠道注册表（按优先级排列，前面失败自动尝试下一个）
# ============================================================

def build_notifiers(notify_cfg, timeout=15):
    """按优先级构建推送渠道列表。notify_cfg 即 config.toml 的 [notify] 段。"""
    n = notify_cfg or {}
    feishu = n.get("feishu", {})
    sc = n.get("serverchan", {})
    return [
        FeishuNotifier(feishu.get("enabled"), feishu.get("webhook"),
                       feishu.get("secret", ""), timeout),
        ServerChanNotifier(sc.get("enabled"), sc.get("send_key"), timeout),
    ]
