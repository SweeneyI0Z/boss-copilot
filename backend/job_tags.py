"""从岗位文本中统一识别福利、工时与外包标签。"""
import re
import unicodedata
from collections.abc import Mapping


TAG_FIELDS = (
    "has_benefits",
    "has_weekend",
    "has_eight_hour_weekend",
    "has_alternating_weekend",
    "is_outsourcing",
)


_ALTERNATING_NEGATION_RE = re.compile(
    r"(?:非|不是|并非|无|取消(?:了)?|告别|拒绝|不实行|不执行)\s*"
    r"(?:大小周|单双休)"
)
_ALTERNATING_RE = re.compile(
    r"大小周|单双休|隔周(?:单休|双休|周六(?:上班|出勤))|"
    r"周六隔周(?:上班|出勤)|"
    r"(?:一周|本周)\s*单休.{0,8}(?:一周|下周)\s*双休|"
    r"(?:一周|本周)\s*双休.{0,8}(?:一周|下周)\s*单休|"
    r"(?:单休.{0,8}双休|双休.{0,8}单休).{0,6}(?:轮换|交替)"
)

_WEEKEND_NEGATION_RE = re.compile(
    r"(?:不保证|无法保证|不能保证|不提供|不承诺|无|非|不是|并非|"
    r"不实行|不执行)\s*(?:固定)?\s*(?:周末)?\s*双休|"
    r"(?:周末)?\s*双休\s*(?:不固定|无法保证|不能保证)|单双休"
)
_FIXED_WEEKEND_RE = re.compile(
    r"(?:固定\s*)?(?:周末\s*)?双休|周休(?:二|2)日|"
    r"(?:做|上)(?:五|5)休(?:二|2)|(?:五|5)天工作制|"
    r"每周休息(?:两|2)天"
)
_EIGHT_HOUR_RE = re.compile(
    r"(?:(?:每日|每天|一天|日均)\s*(?:工作|工时)?\s*)?"
    r"(?:8|八)\s*(?:个)?\s*小时(?:工作制|工时|工作)?|"
    r"(?:工作制|工时|工作时长).{0,6}(?:8|八)\s*(?:个)?\s*小时|"
    r"\b8\s*h(?:our)?s?\b"
)

_WELFARE_WORDS = (
    "五险一金", "六险一金", "七险一金", "五险二金", "商业保险",
    "补充医疗", "补充公积金", "年终奖", "全勤奖", "绩效奖金", "项目奖金",
    "季度奖金", "股票期权", "员工持股", "股权激励", "期权激励",
    "带薪年假", "带薪病假", "法定节假日", "餐补", "饭补", "工作餐",
    "房补", "住房补贴", "租房补贴", "交通补贴", "通讯补贴", "高温补贴",
    "定期体检", "年度体检", "节日福利", "生日福利", "员工旅游", "团建",
    "弹性工作", "免费班车", "下午茶",
)
_WELFARE_RE = re.compile("|".join(re.escape(word) for word in _WELFARE_WORDS))
_WELFARE_ITEM_PATTERN = "(?:" + "|".join(
    re.escape(word) for word in _WELFARE_WORDS) + ")"
_WELFARE_NEGATION_RE = re.compile(
    r"(?:无|没有|不提供|不含|不缴纳|未缴纳|不享受)\s*" +
    _WELFARE_ITEM_PATTERN +
    r"(?:\s*(?:、|,|，|/|和|及|与)\s*" + _WELFARE_ITEM_PATTERN + r")*"
)
_WELFARE_SUMMARY_RE = re.compile(r"(?:公司|员工|职位)?福利(?:待遇|齐全|完善|优厚)")

_OUTSOURCING_NEGATION_RE = re.compile(
    r"(?:非|不是|并非|无|拒绝|不接受)\s*(?:任何)?\s*"
    r"外包(?:岗位|职位|编制|性质)?|"
    r"(?:无需|不用|非|不需要)\s*(?:客户)?\s*(?:驻场|派驻)"
)
_OUTSOURCING_MANAGEMENT_RE = re.compile(
    r"外包采购|外包服务采购|外包项目经验|"
    r"外包(?:供应商|服务商|团队|人员)(?:的)?(?:日常)?\s*"
    r"(?:管理|对接|协调|评估|选择)|"
    r"(?:管理|对接|协调|评估|选择)\s*外包(?:供应商|服务商|团队|人员)"
)
_OUTSOURCING_RE = re.compile(
    r"(?:人力|技术|软件|it|研发|项目|业务流程)?外包"
    r"(?:岗位|职位|编制|员工|人员|性质|合同|服务|项目)?|"
    r"劳务派遣|人力派遣|派遣制|"
    r"第三方(?:用工|合同|签约|派遣|人力)|"
    r"(?:驻场|驻点)(?:开发|测试|办公|工作|项目|客户|服务)?|"
    r"派驻(?:客户|甲方|项目现场)|客户现场(?:办公|工作|驻场)"
)


def _normalize_text(*parts: object) -> str:
    """NFKC 统一全半角后再匹配，避免全角数字漏判。"""
    raw = "\n".join(str(part or "") for part in parts)
    return unicodedata.normalize("NFKC", raw).casefold()


def _masked(text: str, *patterns: re.Pattern) -> str:
    for pattern in patterns:
        text = pattern.sub(" ", text)
    return text


def classify_job_tags(job: Mapping[str, object], jd: object = "",
                      skill_tags: object = "") -> dict[str, bool]:
    """返回列表和详情共用的五类标签；不写库，始终按最新文本计算。"""
    text = _normalize_text(
        job.get("title"), job.get("company"), job.get("skills"), jd, skill_tags)

    alternating_text = _masked(text, _ALTERNATING_NEGATION_RE)
    has_alternating = bool(_ALTERNATING_RE.search(alternating_text))

    weekend_text = _masked(text, _WEEKEND_NEGATION_RE)
    fixed_weekend = bool(_FIXED_WEEKEND_RE.search(weekend_text))
    # 大小周包含单双休字样时，以更具体且更保守的大小周标签为准。
    has_weekend = fixed_weekend and not has_alternating
    has_eight_hour_weekend = has_weekend and bool(_EIGHT_HOUR_RE.search(text))

    welfare_text = _masked(text, _WELFARE_NEGATION_RE)
    has_benefits = bool(
        _WELFARE_RE.search(welfare_text) or _WELFARE_SUMMARY_RE.search(welfare_text))

    outsourcing_text = _masked(
        text, _OUTSOURCING_NEGATION_RE, _OUTSOURCING_MANAGEMENT_RE)
    is_outsourcing = bool(_OUTSOURCING_RE.search(outsourcing_text))

    return {
        "has_benefits": has_benefits,
        "has_weekend": has_weekend,
        "has_eight_hour_weekend": has_eight_hour_weekend,
        "has_alternating_weekend": has_alternating,
        "is_outsourcing": is_outsourcing,
    }
