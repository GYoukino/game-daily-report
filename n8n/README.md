# 用 n8n 给日报加个哨兵（可选）

GitHub Actions 出问题（比如某天 GitHub 抽风、密钥失效）时它会默不作声。
这个文件让你能对这一件事放心：**当天该出的日报没出来，就往飞书推一条告警**。

## 方式一：直接导入（快）

1. 打开你的 n8n → 右上角 **Create workflow** → 右上角三个点 → **Import from File**
2. 选同目录下的 `日报缺失告警.json`
3. 导入后改两处地方（都写着"在这里填"）：
   - **生成飞书签名** 节点：`SECRET = '你的飞书签名密钥'`
   - **推送告警到飞书** 节点：URL 里换上你自己的 webhook 后缀
4. 右上角打开 **Active** 开关

> 如果导入报错（不同 n8n 版本节点格式会有差异），用下面的手动搭法，花五分钟也就好了。

## 方式二：手动搭（稳）

一共三个核心节点：

**① Schedule Trigger**
- Trigger Interval：`Cron`
- Expression：`0 4 * * *`（UTC 04:00，也就是北京时间中午 12 点，给 Actions 留足排队时间）

**② HTTP Request**
- Method：`GET`
- URL：
  ```
  {{ 'https://GYoukino.github.io/game-daily-report/daily/' + $now.toFormat('yyyy-MM-dd') + '.html' }}
  ```
- Options → Response → **Never Error** 打开（这样 404 不会中断流程）

**③ IF**
- 条件：`{{ $json.statusCode }}` **not equal** `200`
- true 分支接告警推送

true 分支上再接一个 Code 节点算飞书签名，最后 HTTP Request 推到飞书 webhook。
Code 节点的脚本：

```js
const crypto = require('crypto');
const SECRET = '你的飞书签名密钥';
const ts = Math.floor(Date.now() / 1000);
const sign = crypto.createHmac('sha256', ts + '\n' + SECRET).update('').digest('base64');
return [{ json: { timestamp: ts, sign } }];
```

## 顺带一提

你 NAS 上那个 LobeChat 也能用起来：把 `https://GYoukino.github.io/game-daily-report/`
加到它的联网/检索来源里，就能用自然语言问「原神下次前瞻什么时候」「最近有什么联动」这类问题。
这一步纯配置，不需要改任何代码。
