# SigmX — AI 投研智能体平台

<p align="center">
  <b>面向 A 股投研场景的多智能体系统 · 个股深度报告 · 基金套利分析 · 量化因子</b>
</p>

---

## ✨ 项目简介

SigmX 是一套**面向 A 股为主的多智能体投研平台**，在 [Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 开源基础上扩展，内置完整的 Web 平台、用户体系与积分计费，开箱即用。

核心能力：
- **AlphaForge**：16-Agent 流水线，输入股票代码 → 自动产出多维度投研深度报告（技术面 / 基本面 / 新闻 / 情绪 / 政策 / 资金 / 解禁 + 多空辩论 + 交易决策 + 风控 + 最终裁决）
- **基金套利分析**：全市场 LOF/ETF 折溢价扫描 + 单基金 6-Agent 深度套利报告
- **量化因子工厂**：452 个预构建 Alpha 因子（alpha101 / gtja191 / qlib158 / academic）
- **多智能体协作**：基于 DAG 的 swarm 编排，支持并行 / 辩论 / 裁决
- **用户体系**：注册登录 + 免责声明 + 积分计费 + 兑换码 + 管理员权限
- **消息推送**：飞书 / 钉钉 / 企业微信群机器人通知

---

## 🧩 功能模块

| 模块 | 路径 | 说明 |
|------|------|------|
| 今日总览 | `/` | 机会 / 报告 / 回测汇总，缓存秒开 |
| 智能体 | `/agent` | 自然语言对话式投研，可读数据 / 网页 / 文件 |
| AlphaForge | `/alpha-forge` | 16-Agent 个股深度投研报告（消耗积分）|
| 套利机会 | `/fund-opportunity` | 全市场 LOF/ETF 折溢价扫描排行 |
| 套利分析 | `/fund-arbitrage` | 6-Agent 单基金深度套利报告（消耗积分）|
| 因子工厂 | `/alpha-zoo` | 452 个 Alpha 因子浏览 / 回测（**仅管理员**）|
| 跟踪看板 | `/tracking-dashboard` | 趋势 / 资金 / 事件 / 风险多维度仓位审视 |
| 机会清单 | `/opportunity` | 系统扫描的候选标的 |
| 逻辑链 | `/logic-chain` | 宏观到交易的分层推理 |
| 新闻 / 事件 | `/news` `/events` | 市场情报 |
| 个人中心 | `/account` | 账户 / 积分 / 兑换码 / 流水 / 改密 |
| 设置 | `/settings` | 模型与数据源（**仅管理员**）+ 通知配置 |

---

## 🚀 快速开始

### 方式 A：Docker 部署（推荐生产环境）

```bash
git clone https://github.com/GGwujun/SigmX.git
cd SigmX

# 配置环境变量
cp agent/.env.example agent/.env
# 编辑 agent/.env，至少配置 LLM、管理员账号、JWT_SECRET

# 构建并启动
docker compose up -d --build
```

访问 `http://服务器IP:8899`，默认管理员账号 `admin@sigmx.local / admin123`（**生产环境务必在 .env 改密码**）。

### 方式 B：本地开发

```bash
# 后端
pip install -r agent/requirements.txt
pip install -e .
vibe-trading serve          # 启动 API + 前端 dist（默认 8000 端口）

# 前端热更新（可选）
cd frontend
npm install
npm run dev                 # 5899 端口
```

---

## 🔐 用户与权限

- **注册登录**：邮箱 + 密码，本地访问也需登录，JWT 24h
- **免责声明**：注册勾选 + 登录后弹窗确认，全页水印「仅供学习研究，不构成投资建议」
- **积分体系**：AlphaForge 报告 50 积分，基金套利 20 积分；余额不足拒绝；分析失败自动退还
- **兑换码**：管理员用脚本批量生成，用户兑换获得积分
- **管理员**：默认账号 `admin@sigmx.local`，可查看因子工厂与系统配置

### 生成兑换码

```bash
python agent/scripts/gen_codes.py --credits 100 --count 50 --days 90
# → 写入 credits.db + 导出 ~/credits_codes_<时间戳>.csv
```

---

## 🔔 消息推送

设置页 → 通知配置，支持三平台群机器人：
- **飞书**：自定义机器人 + 签名校验
- **钉钉**：自定义机器人 + 加签
- **企业微信**：群机器人（key 鉴权）

配置后点「测试发送」会推送实时行情摘要到群。

---

## ⚙️ 环境变量

关键配置（`agent/.env`）：

| 变量 | 说明 | 必填 |
|------|------|------|
| `LANGCHAIN_PROVIDER` / `LANGCHAIN_MODEL_NAME` | LLM 供应商与模型 | ✅ |
| `ZHIPU_API_KEY` / `OPENAI_API_KEY` 等 | 对应供应商的 key | ✅ |
| `TUSHARE_TOKEN` | A 股数据（tushare）| 分析 A 股需要 |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | 管理员账号 | 建议 |
| `JWT_SECRET` | JWT 签名密钥（固定值，否则重启 token 失效）| 建议 |
| `API_AUTH_KEY` | 远程访问备用 key | 公网部署建议 |

---

## 🏗️ 技术栈

- **后端**：Python 3.11+ / FastAPI / SQLite（users.db / credits.db / sessions.db）
- **前端**：React + TypeScript + Vite + Tailwind
- **多智能体**：自研 swarm DAG 编排框架（YAML 预设）
- **数据源**：mootdx / akshare / tushare（自动回退）
- **LLM**：OpenAI 兼容接口（智谱 GLM / DeepSeek / OpenAI / Moonshot 等）

---

## 📂 项目结构

```
├── agent/                  后端
│   ├── src/
│   │   ├── api/            HTTP 路由（auth/credits/fund/notify/alpha_forge...）
│   │   ├── auth/           用户认证（JWT + bcrypt + users.db）
│   │   ├── credits/        积分体系（balance/transactions/redeem）
│   │   ├── notify/         消息推送（飞书/钉钉/企业微信）
│   │   ├── data/           数据层（fund_premium 折溢价等）
│   │   ├── swarm/          多智能体 DAG 编排 + presets/
│   │   └── factors/zoo/    452 个 Alpha 因子
│   ├── api_server.py       FastAPI 入口
│   └── scripts/            兑换码生成等工具
├── frontend/               前端（React + TS）
│   └── src/pages/          页面（AlphaForge/FundArbitrage/Account...）
├── Dockerfile / docker-compose.yml
└── pyproject.toml
```

---

## ⚠️ 免责声明

本系统生成的市场分析、交易观点、回测结果和对话内容**仅供学习研究与信息参考**，不构成任何投资建议、收益承诺或交易指令。金融市场存在风险，历史表现不代表未来结果，请结合自身风险承受能力独立判断。

---

## 📜 致谢

本项目基于 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 二次开发，感谢原作者的开源贡献。

## License

MIT
