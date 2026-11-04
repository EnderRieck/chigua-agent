# 🍉 吃瓜智能体

一个在终端里帮你“吃瓜”的智能体：给它一个时事热点 / 网络事件，它会用 Grok 编排多平台检索
（DeepSeek 联网搜索 + 微博 + 知乎 + 小红书 + X），把证据统一编号，最后写出一份分事实 / 传闻 / 观点、
分平台舆论场、带来源索引的吃瓜报告。

```
你 > 最近金球奖的争议是怎么回事
▶ 规划检索            规划员（Grok）输出 JSON 检索计划：5~7 条跨平台查询
▶ 多平台采集（6 项并发） web_search ×2、weibo_search、zhihu_search、xiaohongshu_search、x_search
▶ 分析撰写            分析员（Grok + function calling）按需追加检索，流式写报告
# 🍉 …                 固定结构报告 + 自动生成的「信息来源」列表 [E1]…[En]
```

## 架构

```
用户输入 ──► Planner（规划员）──► 检索计划 JSON
                                   │  tool / args / why
                                   ▼
                      ToolRegistry 并发执行 ──► EvidenceStore（[E#] 统一编号、按 URL 去重）
                      ├ web_search / fetch_page   DeepSeek Search / Fetch API
                      ├ weibo_hot_search / weibo_search
                      ├ zhihu_hot_list / zhihu_search / zhihu_question_answers   (Cookie + x-zse-96 签名)
                      ├ xiaohongshu_search / xiaohongshu_note_detail   (xiaohongshu-mcp, 可选)
                      └ x_search                   xAI x_search → Live Search → DeepSeek 兜底
                                   │
                                   ▼
                      Analyst（分析员，流式 + 最多 N 轮工具调用）──► Markdown 报告 + 来源
```

- 沿用 C3 作业 v5 的“感知-决策-行动”骨架：`chigua/agent.py` 是编排核心，`chigua/tools/` 是行动层，`chigua/evidence.py` 是共享记忆。
- 每个平台适配器都带**降级链**，缺 Cookie / 没开 MCP / 中转站不支持某接口时自动退到 DeepSeek `site:` 搜索，并在证据说明里写明，分析员据此判断可信度。
- 详细的社媒接入调研与取舍见 [docs/社媒接入调研.md](docs/社媒接入调研.md)。

## 安装（pixi）

```bash
cd 吃瓜智能体
pixi install
cp .env.example .env    # 已自动生成过一份 .env，直接编辑即可
```

编辑 `.env`：

| 变量 | 必填 | 说明 |
|---|---|---|
| `XAI_API_KEY` / `XAI_BASE_URL` / `XAI_MODEL` | ✅ | Grok。国内中转站填中转站地址（通常以 `/v1` 结尾）和它的模型名；官方直连 `https://api.x.ai/v1` |
| `DEEPSEEK_API_KEY` | ✅ | DeepSeek Search / Fetch（2026-11-01 前免费，之后 $0.75/千次） |
| `X_SEARCH_MODE` | 可选 | `auto`（默认，依次试 responses → live → deepseek）。中转站用户一般最终会落到 `deepseek`，想省两次失败请求可直接设 `deepseek` |
| `WEIBO_COOKIE` | 可选 | 你自己浏览器里 weibo.com 的 Cookie，填了就直连微博热搜与站内搜索 |
| `ZHIHU_COOKIE` | 可选 | 你自己浏览器里 zhihu.com 的完整 Cookie（含 d_c0、z_c0），填了就直连知乎站内搜索和问题回答，签名在本地计算 |
| `XHS_MCP_URL` | 可选 | 本地 [xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) 地址，默认 `http://localhost:18060/mcp`，需先用它扫码登录 |
| `MAX_TOOL_ROUNDS` | 可选 | 分析员最多追加几轮检索，默认 4 |

## 使用

```bash
pixi run doctor            # 自检：Key、LLM、DeepSeek、微博/知乎热榜、小红书 MCP；加 --full 实测 X 链路
pixi run hot               # 看今日微博热搜 + 知乎热榜
pixi run ask 某某事件 --save   # 一次性分析并保存到 reports/
pixi run chigua            # 交互式对话（默认）
```

交互模式命令：`/hot` 看榜 → `/dig W3` 深挖第 3 条微博热搜（`Z5` 为知乎第 5 条）；`/sources` 列全部证据；
`/save` 保存报告；`/new` 换个瓜；`/quit` 退出。直接输入追问（如“知乎那边怎么看”）会复用已有证据，必要时才补检索。

## 报告结构

```
# 🍉 事件标题
## 一句话吃瓜
## 事件时间线                 每条事实后标 [E#]
## 关键当事方与各方说法        事实 / 传闻（未证实）/ 观点 三分开
## 各平台舆论场               微博 · 知乎 · 小红书 · X（缺的平台会明说“本次未获取到”）
## 争议焦点与待证实信息
## 吃瓜指南                   后续节点、辨别真假的提醒
## 信息来源                   系统根据引用自动生成
```

## 目录

```
吃瓜智能体/
├── pixi.toml            环境与任务（python 3.11/3.12, openai, httpx, rich, mcp, pytest）
├── .env.example         配置模板
├── chigua/
│   ├── __main__.py      CLI：chat / ask / hot / doctor
│   ├── agent.py         Planner → Gather → Analyst 编排
│   ├── llm.py           OpenAI 兼容封装（中转站、流式、function calling、JSON）
│   ├── prompts.py       规划员 / 分析员提示词
│   ├── evidence.py      证据库 [E#]
│   ├── config.py        .env 读取
│   └── tools/
│       ├── __init__.py          ToolSpec / ToolRegistry / 参数容错
│       ├── deepseek_search.py   web_search, fetch_page, site_search 兜底
│       ├── weibo.py / zhihu.py / xhs.py / x_search.py
│       ├── zhihu_sign.py        知乎 x-zse-96 签名的纯 Python 实现
│       └── mcp_client.py        MCP Streamable HTTP 客户端封装（兼容 mcp 1.x / 2.x）
├── tests/test_offline.py  离线单测（pixi run test）
├── docs/社媒接入调研.md
└── reports/               /save 的输出
```

## 合规与风险提示

- 微博、知乎、小红书均无官方开放搜索 API。本项目**不绕过**平台的访客校验，只用：公开镜像、公开接口、用户本人的 Cookie、
  用户自己扫码登录的 xiaohongshu-mcp。Cookie 等同密码，只放在本地 `.env`（已在 `.gitignore`）。
- X 链路要求模型接口返回真实引用，否则视为没联网并降级，避免“模型编推文”。
- 报告面向学习与研究；涉及隐私、未成年人、未经审判指控的话题，提示词要求分析员克制用词、不站队。
