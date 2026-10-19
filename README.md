# InvestWise 投资助手

每日跟踪持仓公司的重要信息，用多框架 AI 评分给出利好/利空判断与预警，并用苏格拉底式提问帮你想清楚一家公司值不值得投。

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # 填写 API Key 和邀请码
.venv/bin/uvicorn app.main:app --port 8000
```

打开 http://127.0.0.1:8000 ，用 `.env` 里的 `INVITE_CODE` 注册。

## API Key

| 变量 | 用途 | 是否必需 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 公司识别、信息过滤与打分、提问助手 | 必需 |
| `BOCHA_API_KEY` | 博查搜索：港股主要数据源，A股/美股补充 | 强烈建议（没有它港股基本没有数据） |
| `FINNHUB_API_KEY` | 美股公司新闻 | 建议 |
| `SEC_USER_AGENT` | 美股 SEC 公告（免费，无需 key，填邮箱即可） | 建议 |

A股新闻通过 AKShare（东方财富）获取，免费无需 key。

## 工作流程

1. **每日 `DAILY_RUN_TIME`（默认 07:30 北京时间）** 自动执行，也可点页面上的「立即检索」。
2. 抓取：所有用户的公司去重后抓取一次，存入 `raw_news`。
3. 分析：每家持仓公司把新信息交给 DeepSeek，过滤噪音、合并同一事件，按 价值投资 / 成长投资 / 管理层与治理 / 催化剂与风险 四个框架打分（-10~+10），并对照用户填写的投资逻辑判断"加强/削弱"。
   当天没有重要信息则不生成记录。来源链接只取自真实抓取的数据，模型编造的来源会被丢弃。
4. 预警：`|评分| ≥ 用户阈值` 且置信度 ≥ 0.6，或"投资逻辑被证伪"时，置顶高亮。阈值调整后历史信息会重新判定。

## 目录

```
app/
  main.py      接口、登录、定时任务
  analyzer.py  每日抓取与分析流水线
  sources.py   数据源（AKShare / 博查 / Finnhub / SEC）
  parser.py    粘贴文本、Excel/CSV 解析与代码标准化
  prompts.py   识别、打分、提问助手的提示词（调效果主要改这里）
  llm.py       DeepSeek 调用（OpenAI 兼容，换模型改 .env）
  db.py        SQLite 表结构
static/        前端页面（无外部 CDN 依赖）
```

## 部署

线上地址：https://investwise.waywise.tech 。脚本和配置在 `deploy/`：systemd 常驻 `investwise` 服务（uvicorn 只监听 127.0.0.1:8100，单 worker，每日检索的定时任务在进程内），由服务器上现有的 Caddy 反向代理并自动签发 HTTPS 证书。
代码在 `/srv/investwise/app`，数据库在 `/srv/investwise/data`，每天备份到 `/srv/investwise/backup`（保留 14 天）。

```bash
# 第一次：初始化服务器 + 上传 .env
DEPLOY_HOST=ubuntu@<服务器IP> DEPLOY_KEY=~/.ssh/<你的密钥> deploy/deploy.sh --setup --env .env
# 以后每次更新（改了 .env 就加 --env .env）
DEPLOY_HOST=ubuntu@<服务器IP> DEPLOY_KEY=~/.ssh/<你的密钥> deploy/deploy.sh
```

看日志：`ssh ubuntu@<服务器IP> sudo journalctl -u investwise -f`
