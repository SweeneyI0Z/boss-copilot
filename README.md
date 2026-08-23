# boss-copilot · 求职作战室

简历驱动的 BOSS直聘全流程助手：**采集 → 评分（L1 电算 + L2 LLM 精评）→ 作战卡 → 招呼语 → 发送 → 消息管理 → 模拟面试**。本地 Web 应用，数据全部在 `~/.boss-copilot/`。

整合自：`boss-zhipin-scraper`（采集引擎，含 `--company` 公司定向）、`boss-helper`（发送/消息/护栏设计参考）、用户自有《岗位筛选评分规则》（L1/L2 全量落码）。

## 快速开始

```bash
cd ~/project/boss-copilot
.venv/bin/python -m unittest discover tests   # 回归（80 例，必须全绿）
.venv/bin/uvicorn backend.main:app --port 8787
# 浏览器打开 http://127.0.0.1:8787
```

## 首次配置（按顺序）

1. **设置**：填 LLM BYOK（OpenAI 兼容：Base URL / API Key / 模型，如 DeepSeek），可先点「测试连通性」验证
2. **简历档案**：粘贴简历全文（L2 精评、招呼语、模拟面试都以它为基线）
3. **采集中心**：导入 xlsx 岗位表（`~/Desktop/深圳_…岗位JD详情….xlsx`）或「根据简历生成策略」后按计划采集
4. **岗位列表**：「运行 L1 评分」→「L2 精评 (LLM)」
5. **账号管理**：按需开启双账号模式；开启时分别为采集号(9222)与沟通号(9223)登录，关闭时只需登录沟通号

## 账号管理

| 账号 | Chrome profile | CDP | 用途 |
|------|----------------|-----|------|
| 采集号 | `~/.boss-copilot/chrome-profile-collect` | 9222 | 全部采集（风控风险集中于此） |
| 沟通号 | `~/.boss-copilot/chrome-profile-communication` | 9223 | 招呼语发送与消息收发；单账号模式下也负责采集 |

双账号模式默认开启，以保持原有的风控隔离行为。关闭后，采集脚本与沟通功能都连接沟通号的 9223 端口。挂系统代理（Clash 等）时后端已自动对 localhost CDP 绕过代理。

点击「检测登录态」时，如果对应 Chrome 尚未运行，应用会临时启动它、打开登录页、完成检测并自动停止；原本已运行的 Chrome 不会被自动停止。结果及检测时间会保存并在页面刷新后继续显示。

## 数据目录与迁移

运行数据统一放在用户主目录的 `.boss-copilot` 下：macOS 当前为 `~/.boss-copilot`，Windows 自动使用 `%USERPROFILE%\.boss-copilot`。可用 `BOSS_COPILOT_HOME` 覆盖数据根目录，用 `BOSS_CHROME_PATH` 覆盖 Chrome，用 `BOSS_ZHIPIN_SCRAPER_HOME` 指定外部采集仓库。

启动时会把旧版的 `~/.boss-zhipin-scraper/chrome-profile`、`~/.boss-zhipin-scraper/job-result` 和 `~/.boss-copilot/chrome-profile-a` 迁入统一目录。迁移是幂等的：旧 profile 正在使用或目标已经存在时会跳过，绝不覆盖目标数据。

## 功能与里程碑

| 里程碑 | 功能 | 提交 |
|--------|------|------|
| M1 | SQLite 底座 + xlsx/JSON 导入 + 账号管理 | 2e59d1d |
| M2 | L1 电算评分（Gate/行业/薪资/E1/词典，黄金测试 4 岗位） | ecd16a9 |
| M3 | BYOK LLM + AI 采集策略 + L2 精评 + 作战卡 | 0b1f2fb |
| M4 | 按计划采集执行器 + 同步刷新（下架/HR 活跃度剔除）+ 简历变更重算 | 51cde89 |
| M5 | 招呼语生成/队列/发送（沟通号 UI 级操作 + 全护栏） | 847a127 |
| M6 | 消息中心（DOM 快照轮询 + AI 草稿 + 人工点发） | cdd9c82 |
| M7 | 模拟面试（出题/追问点评/报告） | 0de7372 |

## 发送护栏（不可关闭）

每日上限（默认 40，硬顶 110）· 随机 30-90s 间隔 · 同公司 30 天去重 · 命中「120/150 位 BOSS」或验证信号当日熔断 · 仅消费「人工批准」的招呼语 · 全程 UI 级点击（最接近真人）。

## 开发规矩（强约束）

1. **每个功能必须带测试用例**；任何 `backend/`、`frontend/` 改动后必须全量回归：
   `.venv/bin/python -m unittest discover tests`（60 例全绿才可提交）
2. **里程碑 = 一次 git commit**（Conventional Commits）
3. 数据永不物理删除：岗位用状态机（active/delisted/hr_inactive/excluded）
4. 导入的评分是基线，引擎评分不覆盖基线（`keep_imported`）
5. BOSS 写操作只走沟通号、只走 UI 级操作、必须有护栏；spike 脚本放 `tests/spike_*.py`（只读验证用）
6. 聊天页/搜索页后台标签必须开 `Emulation.setFocusEmulationEnabled`（BOSS SPA 无焦点不渲染）

## 目录

```
backend/
  main.py        FastAPI 入口（全部 REST 端点）
  db.py          SQLite schema + DAO（WAL，路径变化自动重连）
  importer.py    xlsx / scraper JSON 导入（评分基线保护）
  collector.py   在线采集执行器（后台线程，调 scraper，任务间隔 120s）
  sync.py        同步刷新：同词下架 diff / HR 活跃度剔除 / P 级漂移报告
  scoring/l1.py  L1 电算评分（《岗位筛选评分规则》全量落码）
  scoring/l2.py  L2 LLM 精评（LLM 只出维度分，算术代码合成）
  strategy.py    AI 采集策略（简历→搜索计划）
  greeting.py    招呼语生成（LLM 3 变体/模板兜底）+ 队列状态机
  sender.py      沟通号发送器（UI 级操作 + 护栏 + 熔断）
  chatpoll.py    消息中心（DOM 快照 + AI 草稿 + 点发）
  interview.py   模拟面试 agent
  boss/cdp.py    账号 CDP 管理（单/双账号切换 + 代理绕过）
frontend/        无构建 Vue3（app.js 单文件 + vendored vue.esm）
tests/           80 个单测 + spike 脚本（spike_m5/m6_*）
```

## 已知边界

- 发送/消息依赖沟通号登录态；未登录时相应接口优雅拒绝
- 消息方向（HR 说的 vs 我说的）在 DOM 快照中不做结构化区分，AI 草稿以整段上下文理解
- 公司页采集的岗位缺 industry/JD 字段时 L1 命中率记 0，补详情后重算即恢复
- LLM 未配置时：L1/导入/队列可用，L2/策略/招呼语(LLM)/草稿/面试降级并提示
