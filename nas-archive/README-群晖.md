# NAS 本地归档容器（群晖 DSM 专用说明）

这个容器只做一件事：**每周一凌晨 4 点，把 GitHub 上的日报（daily/ 目录）拉回你的 NAS 硬盘**。

它不是「抓取」，真正的抓取由 GitHub Actions 完成；它是给已经抓好的报告做本地备份，
这样即便 GitHub 哪天访问不畅或仓库出问题，你手里攒的几十上百份日报也不会丢。

体积很小：浅克隆不带 git 历史，一份日报几十 KB，一年下来几十 MB。

---

## 一、先在 NAS 上建个放日报的文件夹

1. 打开群晖 **File Station**
2. 进 `docker` 共享文件夹，新建子文件夹，命名为 `game-report-archive`
   （建议路径：`/volume1/docker/game-report-archive`）
3. 把本机 `nas-archive` 目录下的三个文件传进去：

   ```
   Dockerfile
   docker-compose.yml
   sync.sh
   ```

## 二、在 Container Manager 里跑起来

1. 打开 **Container Manager**（DSM 7 的容器套件）
2. 左侧 **项目** → 右上角 **新增**
3. 项目名称填 `game-report-archive`
4. 路径选第一步建的文件夹 `/docker/game-report-archive`
5. 来源选「使用现有 docker-compose.yml」，会自动识别到 `docker-compose.yml`
6. 一路下一步 → 完成。**首次会构建镜像，等一两分钟**

> 如果你的 DSM 版本是 Container Manager 且不支持直接 build，
> 见下面「常见问题」。

## 三、确认它真的在干活

1. 容器列表里点 `game-report-archive` → **日志**
2. 看到类似这样的输出就成功了：

   ```
   [2026-10-23 23:50:12] 开始同步  源：https://github.com/...  目标：/archive
   [2026-10-23 23:50:20] 同步完成：共 2 份日报，最新一份为 2026-10-23.html
   ```

3. 再去 File Station 看 `/docker/game-report-archive/daily/`，应该有按日期命名的 HTML

之后每周一凌晨四点会自动续拉，日志持续滚动就是正常的。

---

## 常见问题

**Q：Container Manager 不支持构建，一直报错怎么办？**

DSM 7.2 之前的版本部分不支持 compose 里 build。用 SSH 登录 NAS 手动来：

```bash
cd /volume1/docker/game-report-archive
sudo docker build -t game-report-archive:latest .
sudo docker run -d --name game-report-archive --restart unless-stopped \
  -v /volume1/docker/game-report-archive:/archive \
  -e TZ=Asia/Shanghai \
  game-report-archive:latest
```

**Q：拉取 GitHub 很慢或者超时？**

大多数是网络问题，把 `docker-compose.yml` 里的 `REPO_URL` 换成加速镜像再重启容器：

```yaml
REPO_URL: https://ghfast.top/https://github.com/GYoukino/game-daily-report.git
```

**Q：想改成每天都拉，或者换个时间？**

改 `Dockerfile` 里这一行的 cron 表达式（默认 `0 4 * * 1` = 每周一 04:00）：

| 想多久拉一次 | 表达式 |
| --- | --- |
| 每天凌晨 4 点 | `0 4 * * *` |
| 每周一凌晨 4 点（默认） | `0 4 * * 1` |
| 每月 1 号凌晨 4 点 | `0 4 1 * *` |

改完要重建容器。

**Q：日报在 NAS 上怎么看？**

File Station 里双击 HTML 下载回来用浏览器打开；
或者在群晖装个 **Web Station**，把 `/docker/game-report-archive` 配成静态站点，
内网直接 `http://你的NAS地址/日报/` 就能访问，手机也能看。
