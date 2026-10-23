# 游戏前瞻日报 · NAS 本地归档容器
#
# 作用：每周把 GitHub 上的日报（daily/ 目录）拉回 NAS 自己的硬盘，
#       即便 GitHub 哪天出问题，你手里的历史日报也不会丢。
# 体积：只做浅克隆（--depth=1），不带 git 历史，一年几十 MB。
FROM alpine:3.20

RUN apk add --no-cache git ca-certificates tzdata

ENV TZ=Asia/Shanghai
ENV DEST_DIR=/archive
ENV REPO_URL=https://github.com/GYoukino/game-daily-report.git
ENV BRANCH=main

COPY sync.sh /usr/local/bin/sync-report.sh
RUN chmod +x /usr/local/bin/sync-report.sh \
    && echo "0 4 * * 1 /usr/local/bin/sync-report.sh >> /var/log/sync.log 2>&1" \
       > /etc/crontabs/root

VOLUME ["/archive"]

# 启动先同步一次（方便立刻验证有没有生效），之后由 cron 每周一 04:00 续拉
CMD ["sh", "-c", "/usr/local/bin/sync-report.sh 2>&1 | tee -a /var/log/sync.log; crond -f -l 2"]
