"""AI 智能采集策略：LLM 读简历 → 生成采集计划（关键词/城市/公司/词典补充）。

输出 plan 结构（前端可编辑后保存执行）：
{
  "directions": ["嵌入式软件", "AI应用", "嵌入式×AI交叉"],
  "searches": [{"keyword": "...", "city": "深圳", "pages": 3, "reason": "..."}],
  "companies": [{"name": "...", "reason": "..."}],
  "dictionary_patch": {"embedded": ["..."], "ai_soft": ["..."]},
  "notes": "..."
}
"""
import json

from . import llm

PLAN_SCHEMA_HINT = """{
  "directions": ["求职主方向（1-3 个）"],
  "searches": [
    {"keyword": "搜索关键词（岗位名变体，含长尾词）", "city": "城市",
     "pages": 3, "reason": "为什么搜这个词（对应简历哪块经历）"}
  ],
  "companies": [
    {"name": "目标公司名（在 BOSS 有招聘主页的公司）", "reason": "为什么值得定向盯"}
  ],
  "dictionary_patch": {
    "embedded": ["简历里有但词典缺的嵌入式词"], "ai_soft": ["..."],
    "comm_iot": ["..."], "hardware": ["..."], "domain_algo": ["..."]
  },
  "notes": "策略说明（期望薪资定位/规避方向）"
}"""

SYSTEM_PROMPT = """你是求职采集策略师。根据用户简历与期望，设计 BOSS直聘 的岗位采集计划。

要求：
1. searches 生成 6-10 组搜索：覆盖岗位名的多种叫法（如 嵌入式软件工程师/固件工程师/MCU开发/AI应用工程师/LLM应用/Agent开发），含 2-3 个交叉方向词（如 边缘AI、智能硬件 AI）；keyword 要贴合 BOSS 搜索习惯，不要太长
2. companies 给 3-6 家值得定向监控的公司（业务与简历方向强相关，含理由）；不确定就少给，不要编造
3. dictionary_patch 只补充简历中真实出现、但缺于词典的技能词
4. 依据期望薪资设置合理的搜索薪资预期说明（写进 notes）
只输出 JSON。"""


def generate_plan(resume_text: str, expectations: dict, client=None) -> dict:
    if not resume_text or len(resume_text.strip()) < 50:
        raise llm.LLMError("简历内容过短，请先在「简历档案」粘贴完整简历")
    user = (f"# 简历\n{resume_text[:6000]}\n\n# 期望\n{json.dumps(expectations, ensure_ascii=False)}\n\n"
            f"按以下结构输出：\n{PLAN_SCHEMA_HINT}")
    plan = llm.chat_json(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": user}], client=client)
    _validate(plan)
    return plan


def _validate(plan: dict) -> None:
    if not isinstance(plan, dict):
        raise llm.LLMError("策略输出不是对象")
    searches = plan.get("searches")
    if not isinstance(searches, list) or not searches:
        raise llm.LLMError("策略缺少 searches 数组")
    for s in searches:
        if not isinstance(s, dict) or not (s.get("keyword") or "").strip():
            raise llm.LLMError("存在缺少 keyword 的搜索项")
        s.setdefault("city", "深圳")
        try:
            s["pages"] = min(max(int(s.get("pages", 3)), 1), 10)
        except (TypeError, ValueError):
            s["pages"] = 3
    plan.setdefault("companies", [])
    plan.setdefault("dictionary_patch", {})
    plan.setdefault("directions", [])
    plan.setdefault("notes", "")


def save_plan(plan: dict) -> None:
    from .db import set_setting
    set_setting("collect_plan", plan)


def get_plan() -> dict:
    from .db import get_setting
    return get_setting("collect_plan", {"searches": [], "companies": [],
                                        "directions": [], "dictionary_patch": {},
                                        "notes": ""})
