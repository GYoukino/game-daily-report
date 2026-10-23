# -*- coding: utf-8 -*-
"""
网页日报发布模块：把当日 HTML 日报发布到 GitHub Pages，
飞书推送卡片里附上网址，点开即可在飞书内置浏览器里阅读完整日报。

为什么走这条路：
    飞书对"个人账号"的自建应用收紧了 API 访问（写云文档/多维表格会被
    平台策略拦截，错误码 21001），而群机器人 Webhook 不受影响。
    GitHub Pages 免费、无需应用审核，生成的静态网页在飞书里
    直接可读（排版、可点击链接全部保留）。

工作方式：
    1. 把 reports/ 里的当日报告拷到 pages-site/daily/YYYY-MM-DD.html
       （URL 用 ASCII 文件名，中文名报告只留在本地 reports/ 目录）
    2. 生成 pages-site/index.html 门户页（今日入口 + 历史日报列表）
    3. 用 git 提交并推送到 GitHub 仓库（首次自动 init + 关联远端）
    4. GitHub Pages 自动发布，链接形如
       https://用户名.github.io/仓库名/daily/2026-10-22.html

任何失败只记日志、返回 None，不影响监测主流程。
配置见 config.toml 的 [publish.github] 段。
"""

import os
import shutil
import subprocess
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# git 可执行文件候选（PATH 里没有时按 Git for Windows 常见安装路径找）
GIT_CANDIDATES = [
    "git",
    r"C:\Program Files\Git\cmd\git.exe",
    r"C:\Program Files (x86)\Git\cmd\git.exe",
    r"C:\Program Files\Git\bin\git.exe",
]


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [发布] {msg}", flush=True)


def _find_git():
    """返回可用的 git 可执行文件路径，找不到返回 None。"""
    for cand in GIT_CANDIDATES:
        try:
            subprocess.run([cand, "--version"], capture_output=True, timeout=10)
            return cand
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


INDEX_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>官方社区前瞻总览</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
       background: #f5f6f8; color: #262626; padding: 24px; }
.wrap { max-width: 680px; margin: 0 auto; }
header { background: #fff; border-radius: 12px; padding: 24px 28px;
         box-shadow: 0 1px 3px rgba(0,0,0,.06); margin-bottom: 16px; }
h1 { font-size: 22px; color: #1a1a1a; }
.meta { color: #8c8c8c; font-size: 13px; margin-top: 8px; }
.latest { display: block; background: #d4380d; color: #fff; text-align: center;
          border-radius: 10px; padding: 14px; font-size: 16px; font-weight: 600;
          text-decoration: none; margin-top: 16px; }
.latest:hover { background: #b32d0a; }
ul { background: #fff; border-radius: 12px; padding: 12px 28px;
     box-shadow: 0 1px 3px rgba(0,0,0,.06); list-style: none; }
li { border-top: 1px solid #f0f0f0; padding: 10px 0; font-size: 14px; }
li:first-child { border-top: none; }
li a { color: #1677ff; text-decoration: none; }
li a:hover { text-decoration: underline; }
li.latest-day a { color: #d4380d; font-weight: 600; }
footer { text-align: center; color: #bfbfbf; font-size: 12px; padding: 12px 0 24px; }
</style></head><body><div class="wrap">
<header><h1>🎮 官方社区前瞻总览</h1>
<div class="meta">由游戏活动监测程序自动生成并发布 · 只采官方渠道</div>
<a class="latest" href="daily/__DATE__.html">进入今日总览（__DATE__）</a></header>
<ul>
__ROWS__
</ul>
<footer>数据来自各游戏官方网站及官方票务 / 展会平台，仅供参考，请以官方发布为准。</footer>
</div></body></html>
"""


class GitHubPagesPublisher:
    """把 HTML 日报发布到 GitHub Pages。"""

    def __init__(self, cfg, timeout=120):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled"))
        self.username = (cfg.get("username") or "").strip()
        self.repo = (cfg.get("repo") or "").strip()
        self.branch = (cfg.get("branch") or "main").strip() or "main"
        site_dir = (cfg.get("site_dir") or "pages-site").strip() or "pages-site"
        raw_dir = (site_dir if os.path.isabs(site_dir)
                   else os.path.join(BASE_DIR, site_dir))
        self.site_dir = os.path.normpath(raw_dir)
        self.timeout = int(cfg.get("timeout") or timeout)
        # 高级选项：自定义 git 远端（默认按 username/repo 拼 https 地址）
        self.remote = (cfg.get("remote") or "").strip()
        # no_push（CI 模式）：只把文件写进 site_dir，不碰 git。
        # 用于 GitHub Actions —— runner 自带 git 身份与权限，提交交给 workflow 做更干净。
        self.no_push = bool(cfg.get("no_push"))
        self._git = None
        self._git_checked = False

    # ---------- 基础信息 ----------

    @property
    def usable(self):
        """配置完整且本机有 git 才可用（no_push 模式不要求 git）。"""
        if not (self.enabled and self.username and self.repo):
            return False
        return True if self.no_push else self._git_exe() is not None

    def _git_exe(self):
        if not self._git_checked:
            self._git = _find_git()
            self._git_checked = True
        return self._git

    @property
    def remote_url(self):
        return self.remote or f"https://github.com/{self.username}/{self.repo}.git"

    @property
    def base_url(self):
        return f"https://{self.username}.github.io/{self.repo}"

    def page_url(self, date_str):
        """某天日报的公开网址。"""
        return f"{self.base_url}/daily/{date_str}.html"

    def url_if_published(self, date_str):
        """当天日报已发布过（本地有副本）则返回链接，否则 None。"""
        p = os.path.join(self.site_dir, "daily", f"{date_str}.html")
        return self.page_url(date_str) if os.path.exists(p) else None

    # ---------- 发布 ----------

    def publish(self, date_str, report_path):
        """发布当日报告，返回网页链接；任何失败返回 None（不影响主流程）。"""
        if not self.usable:
            return None
        try:
            self._stage_files(date_str, report_path)
            if self.no_push:
                log(f"已写入 {self.site_dir}（no_push 模式，提交交给 CI）")
                return self.page_url(date_str)
            if not self._git_sync(f"daily report {date_str}"):
                return None
            url = self.page_url(date_str)
            log(f"已发布 {date_str} 日报 -> {url}")
            return url
        except Exception as e:
            log(f"发布失败（不影响监测和推送）: {e}")
            return None

    def _stage_files(self, date_str, report_path):
        """把报告、门户页、.nojekyll 放进展点目录。"""
        daily_dir = os.path.join(self.site_dir, "daily")
        os.makedirs(daily_dir, exist_ok=True)
        shutil.copyfile(report_path, os.path.join(daily_dir, f"{date_str}.html"))
        with open(os.path.join(self.site_dir, "index.html"), "w",
                  encoding="utf-8") as f:
            f.write(self._build_index(date_str))
        # .nojekyll：让 GitHub Pages 直接使用原始文件，不经 Jekyll 处理
        with open(os.path.join(self.site_dir, ".nojekyll"), "w",
                  encoding="utf-8") as f:
            f.write("")

    def _build_index(self, date_str):
        """门户页：今日入口 + 历史日报列表（扫 daily/ 目录，最多列 30 天）。

        “今日”以 daily/ 目录里最新的日期为准（补发历史日报时不会错标）。
        注意：模板里有 CSS 花括号，不能用 str.format()，用占位符替换。
        """
        daily_dir = os.path.join(self.site_dir, "daily")
        days = []
        if os.path.isdir(daily_dir):
            days = sorted((f[:-5] for f in os.listdir(daily_dir)
                           if f.endswith(".html")), reverse=True)
        latest = days[0] if days else date_str
        rows = "\n".join(
            f'<li class="{"latest-day" if d == latest else ""}">'
            f'<a href="daily/{d}.html">{d} 总览</a>'
            f'{"（今日）" if d == latest else ""}</li>'
            for d in days[:30]
        ) or "<li>暂无历史日报</li>"
        return (INDEX_PAGE
                .replace("__DATE__", latest)
                .replace("__ROWS__", rows))

    # ---------- git 操作 ----------

    def _run_git(self, *args):
        """执行一条 git 命令，返回 (退出码, 输出文本)。"""
        cmd = [self._git_exe(), *args]
        env = dict(os.environ)
        # 没有缓存凭证时快速失败，避免计划任务里弹窗卡住
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            p = subprocess.run(cmd, cwd=self.site_dir, capture_output=True,
                               timeout=self.timeout, env=env)
        except subprocess.TimeoutExpired:
            return 124, f"git {' '.join(args)} 超时（>{self.timeout}s）"
        out = ((p.stdout or b"") + (p.stderr or b"")).decode(
            "utf-8", errors="replace")
        return p.returncode, out

    def _ensure_identity(self):
        """确保仓库内有提交身份，缺失会导致 git commit 静默失败。

        换机器、重新克隆后仓库里往往没有 user.email，这里统一补上
        （已有配置时不覆盖，尊重用户自己的身份设置）。
        """
        rc, out = self._run_git("config", "user.email")
        if rc != 0 or not (out or "").strip():
            self._run_git("config", "user.name", "game-monitor")
            self._run_git("config", "user.email", "game-monitor@localhost")

    def _ensure_repo(self):
        """站点目录不是 git 仓库时，初始化并关联远端（首次发布）。"""
        if not os.path.exists(os.path.join(self.site_dir, ".git")):
            log("首次发布：初始化本地仓库并关联远端…")
            rc, out = self._run_git("init", "-b", self.branch)
            if rc != 0:
                log(f"git init 失败: {out.strip()}")
                return False
            rc, out = self._run_git("remote", "add", "origin", self.remote_url)
            if rc != 0:
                log(f"关联远端失败: {out.strip()}")
                return False
        self._ensure_identity()
        return True

    # 网络类错误关键词：这类抖动值得自动重试。
    # 注意：git 的 HTTP 错误（403/404）也带 "unable to access" 字样，
    # 先用 _is_network_error() 排除"服务器有响应"的情况，避免对认证失败盲目重试。
    _NET_ERRORS = ("reset", "timed out", "timeout", "502", "503", "504",
                   "could not resolve", "failed to connect", "unable to access",
                   "connection", "network unreachable", "try again")

    @classmethod
    def _is_network_error(cls, out):
        """判断 git 输出是否为网络波动（值得重试）。

        "The requested URL returned error: 403/404" 这类说明服务器有响应、
        是认证或仓库不存在的问题，重试无意义，返回 False。
        """
        low = (out or "").lower()
        if "returned error" in low:
            return False
        return any(k in low for k in cls._NET_ERRORS)

    def _git_push(self):
        """推送（带 -u 建立上游关联），网络类错误自动重试。

        国内网络访问 GitHub 时有瞬时的连接重置/超时，重试 2-3 次
        通常就能过去。返回 (是否成功, 最后一次输出)。
        """
        last_out = ""
        for attempt in range(1, 4):
            rc, out = self._run_git("push", "-u", "origin", self.branch)
            last_out = out
            if rc == 0:
                return True, out
            if not self._is_network_error(out):
                return False, out
            if attempt < 3:
                wait = 5 * attempt
                log(f"推送遇到网络波动，{wait} 秒后重试（第 {attempt}/3 次）…")
                time.sleep(wait)
        return False, last_out

    def _git_sync(self, message):
        """提交并推送。返回是否成功。"""
        if not self._ensure_repo():
            return False
        # 只 add 网页相关的三处。site_dir 现在常常就是脚本所在的仓库根目录，
        # 用 add -A 会把整个工程塞进「daily report」这一个提交里。
        rc, out = self._run_git("add", "-A", "daily", "index.html", ".nojekyll")
        if rc != 0:
            log(f"git add 失败: {out.strip()}")
            return False
        rc, out = self._run_git("commit", "-m", message)
        if rc != 0:
            if "nothing to commit" in out or "nothing added" in out:
                log("内容没有变化，跳过本次提交")
            else:
                log(f"git commit 失败: {out.strip()}")
                return False
        # 每次都带 -u：首次推送建立上游分支关联，之后无害
        ok, out = self._git_push()
        if ok:
            return True
        # 远端领先（如在 GitHub 网页上直接改过文件）：先合并再推一次
        low = out.lower()
        if ("rejected" in low or "fetch first" in low
                or "non-fast-forward" in low):
            log("推送被拒（远端有本地没有的提交），先拉取合并再重试…")
            self._run_git("pull", "--rebase", "--allow-unrelated-histories",
                          "origin", self.branch)
            ok, out = self._git_push()
            if ok:
                log("合并后推送成功")
                return True
        log(f"git push 失败: {out.strip()}")
        log("常见原因：没联网；首次推送没登录 GitHub（请先手动跑一次 "
            "run_test_publish.bat 完成登录）；仓库不存在或填错了用户名/仓库名。")
        return False
