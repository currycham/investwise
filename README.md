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

## 后台管理

访问 `/admin`（顶部导航的「后台」入口只有管理员可见，非管理员访问返回 404）。`.env` 里 `ADMIN_USERNAMES` 列出的用户名自动成为管理员，其他人可在后台设置。

| 页面 | 内容 |
|---|---|
| 概览 | DeepSeek / 博查余额、今日与近 7 天模型成本（估算）、用户和持仓统计、按环节/按天的调用与成本、检索队列 |
| 用户 | 持仓数、提问数、成本、注册与最近登录；设为/取消管理员、重置密码（生成临时密码）、停用/恢复 |
| 任务 | 每次检索的类型、用户、公司数、新增信息、用时和错误；立即全站检索、重新生成今日概览 |
| 持仓公司 | 全站去重后的公司、持有人数、抓取量；近 7 天没抓到新闻的标黄；可单独回溯 7 天 |

## 成本控制

- **博查按需调用**（`BOCHA_MODE=fallback`）：港股没有免费数据源，总是用博查；A股（AKShare）和美股（Finnhub + SEC）只在当天免费源没抓到信息时补搜。
- **不活跃用户暂停**（`INACTIVE_DAYS=14`）：超过 14 天没访问网站的用户，每日检索跳过其持仓；再次访问时自动回溯 7 天。
- **全站每日预算**（`DAILY_BUDGET_YUAN=10`）：模型和博查合计，用完后当天暂停检索、概览和提问；没分析完的新闻次日继续处理。
- 后台概览可以看预算使用、按环节（含博查）和按天的成本；单价在 `.env` 的 `DEEPSEEK_PRICE_*`、`BOCHA_PRICE_PER_CALL` 设置。

## 目录

```
app/
  main.py      接口、定时任务
  auth.py      登录与权限
  admin.py     后台管理接口
  digest.py    今日概览
  analyzer.py  每日抓取与分析流水线
  sources.py   数据源（AKShare / 博查 / Finnhub / SEC）
  parser.py    粘贴文本、Excel/CSV 解析与代码标准化
  prompts.py   识别、打分、提问助手的提示词（调效果主要改这里）
  llm.py       DeepSeek 调用（OpenAI 兼容，换模型改 .env）
  costs.py     成本记账与每日预算
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
