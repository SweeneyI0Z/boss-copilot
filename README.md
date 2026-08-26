# boss-copilot · 求职作战室

简历驱动的 BOSS直聘求职作战室：**采集 → 多简历评分 → 收藏工作台 → 招呼语 → BOSS 平台确认 → 数据分析 → 模拟面试**。本地 Web 应用，数据全部在 `~/.boss-copilot/`。

整合自：`boss-zhipin-scraper`（采集引擎，含 `--company` 公司定向）、`boss-helper`（发送/消息/护栏设计参考）、用户自有《岗位筛选评分规则》（L1/L2 全量落码）。

## 快速开始

```bash
cd ~/project/boss-copilot
.venv/bin/python -m unittest discover tests -v   # 回归（259 例，必须全绿）
.venv/bin/uvicorn backend.main:app --port 8787
# 浏览器打开 http://127.0.0.1:8787
```

## 首次配置（按顺序）

1. **设置**：填 LLM BYOK（OpenAI 兼容），也可先使用 L1、导入和人工流程
2. **简历档案**：建立一份或多份简历，维护期望、技能画像和 BOSS 简历标签
3. **账号管理**：双账号时采集号负责只读采集，沟通号只用于受护栏保护的自动招呼和按需状态核验
4. **采集中心**：配置关键词、全局城市和筛选条件，等待岗位列表与全部缺失 JD 采集完成
5. **岗位列表**：展开 JD 后收藏或排除；收藏岗位进入「收藏工作台」切换简历比较评分
6. **收藏工作台**：点「同步BOSS收藏」把两个账号在 BOSS「感兴趣」页标记的岗位增量合并进来（缺失 JD 可一键补齐），再批量生成招呼语
7. **招呼语**：收藏工作台可多选岗位一键批量生成（已生成的按钮置灰防重复），优先复制并到 BOSS 原平台人工发送，也可选择受全部护栏保护的自动批次

## 与 BOSS 原平台配合

- 本应用负责采集、分析、评分、收藏、招呼语生成和流程记录；最终发送与结果核验以 BOSS 原平台为准。
- 岗位列表、收藏工作台的「打开 BOSS」与招呼语页的「复制并打开 BOSS」会在沟通号 Chrome 中新开标签页打开岗位原链（Chrome 未运行时自动启动）。
- 「已打招呼」只统计自动确认成功或用户在 BOSS 发送后人工确认的记录。
- 「已投递」只统计岗位页明确显示“简历已发送”或用户人工确认的记录；unknown 不计数。
- 消息中心、历史会话同步和 AI 回复已下线：DOM 快照无法可靠保证完整历史和全部消息。

## 账号管理

| 账号 | Chrome profile | CDP | 用途 |
|------|----------------|-----|------|
| 采集号 | `~/.boss-copilot/chrome-profile-collect` | 9222 | 全部采集（风控风险集中于此） |
| 沟通号 | `~/.boss-copilot/chrome-profile-communication` | 9223 | 招呼语发送与按需投递核验；单账号模式下也负责采集 |

双账号模式默认开启，以保持原有的风控隔离行为。关闭后，采集脚本与沟通功能都连接沟通号的 9223 端口。挂系统代理（Clash 等）时后端已自动对 localhost CDP 绕过代理。

唯一的跨账号读操作是「同步BOSS收藏」：收藏工作台会分别只读打开两个账号的推荐页「感兴趣」Tab（`/web/geek/recommend?tab=4`），逐页导航 + DOM 读取，零点击零注入请求。同步按账号增量合并——新命中的岗位入库并点亮本地收藏，本地已取消的收藏不会被复活，BOSS 侧取消感兴趣也不会删除或下架本地岗位；同一岗位被两个账号收藏时只保留一条岗位记录并显示双方标签。

采集中心允许用户删除历史采集数据。删除前必须先读取影响预览并二次确认；删除会物理移除指定采集记录及仅归属于该记录的岗位数据，共享给其他采集记录的岗位会保留。无法可靠恢复归属的旧记录只删除采集记录，不猜测删除岗位；运行中、暂停中或正被补采任务引用的记录不能删除。

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
| M9 | 多简历/不可变评分基线/收藏排除/可靠状态模型 | b7aa1fc |
| M10 | 总览看板/行内 JD/收藏工作台/新前端 | 0caf997 |
| M11 | 多简历评分/三版招呼语/平台协同投递 | 2ab779b |
| M12 | 模块化采集/精确来源归因/数据分析 | bced668 |
| M13 | 双账号BOSS收藏同步/增量合并/JD补齐 | 本次提交 |
| M15 | 全模拟前端体验重构/向导/采集工作台/响应式布局 | 7fc0e4a |
| M16 | 前后端真实接入/采集暂停恢复与来源启停/岗位求职阶段 | 本次改动 |
| M17 | 采集数据确认删除/页面级 AI 任务队列/流式进度/OFFER 工作流 | 本次改动 |

## 发送护栏（不可关闭）

每日上限（默认 40，硬顶 110）· 随机 30-90s 间隔 · 同公司 30 天去重 · 命中「120/150 位 BOSS」或验证信号当日熔断 · 仅消费「人工批准」的招呼语 · 全程 UI 级点击（最接近真人）。

## 开发规矩（强约束）

1. **每个功能必须带测试用例**；任何 `backend/`、`frontend/` 改动后必须全量回归：
   `.venv/bin/python -m unittest discover tests -v`
2. **里程碑 = 一次 git commit**（Conventional Commits）
3. 常规岗位生命周期不物理删除，使用状态机（active/delisted/hr_inactive/excluded）。唯一例外是用户在「采集中心」主动发起并二次确认删除：允许物理删除指定采集记录及仅归属于该记录的采集数据；仍归属于其他采集记录的岗位必须保留
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
  favorites.py   双账号BOSS收藏同步（推荐页感兴趣Tab只读 + 增量合并）
  collection_runs.py 采集来源归属、启停、删除与 xlsx 导出
  workflow.py    岗位待开始/已打招呼/已投递/已面试/OFFER 阶段聚合
  ai_tasks.py    岗位页与工作台独立 AI 队列（单页并发 5、取消、SSE、429 退避）
  sync.py        同步刷新：同词下架 diff / HR 活跃度剔除 / P 级漂移报告
  scoring/l1.py  L1 电算评分（《岗位筛选评分规则》全量落码）
  scoring/l2.py  L2 LLM 精评（LLM 只出维度分，算术代码合成）
  strategy.py    AI 采集策略（简历→搜索计划）
  resumes.py     多简历、修订与岗位×简历评分
  dashboard.py   总览统计与缓存系统状态
  analytics.py   当前岗位分布分析；来源筛选与趋势只使用可靠采集关系
  greeting.py    三版招呼语（LLM/模板兜底）+ 队列状态机
  sender.py      沟通号发送器（UI 级操作 + 护栏 + 熔断）
  interview.py   模拟面试 agent
  boss/cdp.py    账号 CDP 管理（单/双账号切换 + 代理绕过）
frontend/        无构建 Vue3（app.js 单文件 + vendored vue.esm）
tests/           259 个单测 + 真实登录态只读 spike 脚本
```

## 已知边界

- 自动招呼与按需投递状态探测依赖沟通号；未登录时优雅拒绝，推荐直接去 BOSS 原平台操作
- 平台投递探测只认明确完成态，失败保持 unknown；不会读取或同步聊天历史
- 公司页采集的岗位缺 industry/JD 字段时 L1 命中率记 0，补详情后重算即恢复
- 列表文件每次增量落盘、详情文件每新增一条都会立即导入 SQLite；`/api/collect/status` 与 `/api/favorites/sync/status` 的 `progress.detail_completed/detail_total` 提供单条进度。详情重试只提交数据库中仍缺 JD 的岗位，已完成项不会重复访问
- LLM 未配置时：L1/导入/采集/人工流程可用，L2/策略/招呼语(LLM)/面试降级并提示
- 岗位列表与收藏工作台的生成任务互相独立，均可在运行时继续追加；单页最大并发为 5，遇到 429 会降低该页并发并按 `Retry-After` 或指数退避重试，连续成功后逐步恢复
- 数据分析默认覆盖全部当前岗位；旧导入和收藏岗位也会进入属性分布，但关键词、城市、日期筛选与采集趋势只使用 M12 可靠来源关系
- 收藏同步依赖推荐页「感兴趣」Tab 的列表组件（实测为专用 `li.item-boss` 卡片，岗位链接自带 encryptJobId；搜索系 `job-card-box` 作为兼容家族一并支持）；若 BOSS 改版解析为空，可跑 `tests/spike_m13_favorites.py`（只读）核对 DOM 后调整解析启发式。每页翻页用独立后台标签（Chrome 会回收闲置后台标签），列表不足一页时第 2 页返回空态自动停止。同步入库的新岗位默认无 JD，状态条会提示缺失数量并可一键补齐（采集号只读执行）
