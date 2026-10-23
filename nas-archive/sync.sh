#!/bin/sh
# ---------------------------------------------------------------
# 把 GitHub Pages 仓库里的日报拉回 NAS 本地做归档。
#
# 采用「浅克隆 + reset」的方式，只取最新一版，不下载完整 git 历史，
# 因此每天一个 HTML 的体积几乎可以忽略（一年大约几十 MB）。
#
# 可用环境变量：
#   DEST_DIR   归档目录（容器内默认 /archive，通常挂载 NAS 共享文件夹）
#   REPO_URL   仓库地址
#   BRANCH     分支名
# ---------------------------------------------------------------
set -e

DEST="${DEST_DIR:-/archive}"
REPO="${REPO_URL:-https://github.com/GYoukino/game-daily-report.git}"
BRANCH="${BRANCH:-main}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "开始同步  源：$REPO  目标：$DEST"

if [ -d "$DEST/.git" ]; then
    cd "$DEST"
    git fetch --depth=1 origin "$BRANCH"
    git reset --hard "origin/$BRANCH"
else
    mkdir -p "$DEST"
    git clone --depth=1 -b "$BRANCH" "$REPO" "$DEST"
fi

# 统计一下，方便在容器日志里直接看到拉到了多少份
n=$(ls -1 "$DEST/daily" 2>/dev/null | wc -l)
latest=$(ls -1 "$DEST/daily" 2>/dev/null | sort | tail -1)
log "同步完成：共 $n 份日报，最新一份为 ${latest:-（无）}"
