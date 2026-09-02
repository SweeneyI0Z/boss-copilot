# AGENTS.md

求职作战室：简历驱动的 BOSS直聘全流程本地 Web 应用（采集 → L1/L2 评分 → 作战卡 → 招呼语 → 发送 → 消息 → 模拟面试）。全项目中文注释/中文 UI，新增代码沿用中文注释惯例。改任何行为前先读 `README.md`（运行手册 + 全部强约束）。

## 常用命令

```bash
# 回归测试（356 例，任何 backend/ 或 frontend/ 改动后必须全绿才可提交）
.venv/bin/python -m unittest discover tests -v

# 启动（浏览器打开 http://127.0.0.1:8787）
.venv/bin/python -m uvicorn backend.main:app --port 8787
```

- 无 lint/typecheck 配置；风格靠现有代码约束（纯标准库 + fastapi/openai/openpyxl，4 空格缩进）。
- 依赖直接装在 `.venv`（无 requirements.txt）；改动依赖后口头同步给用户。

## 架构边界

- `backend/main.py`：**全部** REST 端点 + 前端静态托管，单文件新增端点。
- `backend/db.py`：SQLite（WAL、thread-local 连接、DB_PATH 变化自动重连）。原则：岗位常规生命周期不物理删除，使用状态机 active/delisted/hr_inactive/excluded；唯一例外是用户在采集中心二次确认删除采集记录及其独占岗位数据，共享岗位必须保留。评分字段同时存 JSON 明细与标量摘要（列表页不解析 JSON）。
- `backend/scoring/l1.py` 规则电算 / `l2.py` LLM 精评（LLM 只出维度分，算术由代码合成，绝不 LLM 算总分）。
- `backend/sender.py` 发送器 / `chatpoll.py` 消息中心 / `interview.py` 模拟面试 / `collector.py` 采集执行器（subprocess 调外部仓库）/ `favorites.py` 双账号BOSS收藏同步（推荐页感兴趣Tab只读 + 增量合并）。
- `backend/boss/cdp.py`：双账号 Chrome CDP 管理。
- `frontend/`：无构建 Vue3，`app.js` 单文件（hash 路由）+ vendored `vue.esm-browser.prod.js`，不引入 npm/构建链。
- `tests/`：单测（可跑回归；存量按里程碑 `test_m*_*.py` 命名，新增一律 `test_功能描述.py`，见硬约束 2）；`spike_*.py` 是只读探针脚本（需真实登录态，不进回归）。

## 硬约束（违反即返工）

1. **每个功能必须带测试**；提交前全量回归通过。
2. 里程碑 = 一次 git commit，Conventional Commits。**自「使用指南」里程碑完成后停用里程碑编号**：新提交不得再用 `feat(Mxx)` 形式（写 `feat: 描述`），新增测试文件与类/用例名也不得以 Mxx 命名（文件用 `test_功能描述.py`，如 `test_collect_pace.py`）；存量 ≤M20 的 Mxx 命名保持原样不改写。
3. 导入的评分是基线，引擎评分不得覆盖（`keep_imported` 语义）。
4. BOSS 写操作只走账号A（CDP 9223）、只走 UI 级点击、必须过护栏；护栏不可关闭（每日上限 40/硬顶 110、随机 30-90s 间隔、同公司 30 天去重、命中风控信号当日熔断）。
5. 采集/公司页等读操作只走采集号（CDP 9222），风控风险集中在该号。唯一例外：「同步BOSS收藏」（`favorites.py`）会按用户明确要求只读访问沟通号的推荐页感兴趣 Tab（仅 Page.navigate + DOM 读取，零点击）；除此以外沟通号仍只做受护栏保护的写操作。
6. 聊天页/搜索页后台标签必须开 `Emulation.setFocusEmulationEnabled`（BOSS SPA 无焦点不渲染）。
7. LLM 未配置时所有 LLM 功能必须优雅降级（L1/导入/队列仍可用），不得抛异常。

## 易踩的坑

- **测试隔离**：单测必须在 import backend 之前设 `os.environ["BOSS_COPILOT_HOME"] = 临时目录` 并改写 `config.DATA_DIR/DB_PATH`（见 `tests/test_m1_importer.py` 头部模板），绝不碰 `~/.boss-copilot` 真实数据。
- **CDP 绝不走系统代理**：本机 127.0.0.1 CDP 请求用空 ProxyHandler（用户挂 Clash 会把 localhost 劫持成 502）。
- `collector.py` 默认以同级目录 `../boss-zhipin-scraper` 作为外部采集仓库（可用 `BOSS_ZHIPIN_SCRAPER_HOME` 覆盖；用其独立 venv 跑 `scripts/boss_cdp_raw.py`）；该仓库不存在时采集不可用。
- Chrome 路径硬编码 macOS `/Applications/Google Chrome.app/...`；只按隔离 user-data-dir 精准启停，**绝不触碰用户主 Chrome**。
- 设置接口只接受 `config.DEFAULT_SETTINGS` 白名单内的 key。
- 数据目录 `~/.boss-copilot/`（可用 `BOSS_COPILOT_HOME` 覆盖）；采集号 profile 复用 `~/.boss-zhipin-scraper/chrome-profile`。
- 消息方向（HR vs 本人）在 DOM 快照中不做结构化区分，AI 草稿按整段上下文理解——勿尝试"修复"为强解析。
