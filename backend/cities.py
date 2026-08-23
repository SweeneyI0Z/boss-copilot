"""采集中心使用的省市快照。

项目内维护常用城市作为离线兜底；外部 scraper 存在时只读加载其完整码表。
前端按省份分组展示，保存时始终落 city_code，避免同名或码表变化影响配置。
"""
import json

from . import config

CITY_GROUPS = [
    {"province": "全国", "cities": [{"name": "全国", "code": "100010000"}]},
    {"province": "北京市", "cities": [{"name": "北京", "code": "101010100"}]},
    {"province": "上海市", "cities": [{"name": "上海", "code": "101020100"}]},
    {"province": "天津市", "cities": [{"name": "天津", "code": "101030100"}]},
    {"province": "重庆市", "cities": [{"name": "重庆", "code": "101040100"}]},
    {"province": "黑龙江省", "cities": [{"name": "哈尔滨", "code": "101050100"}]},
    {"province": "吉林省", "cities": [{"name": "长春", "code": "101060100"}]},
    {"province": "辽宁省", "cities": [
        {"name": "沈阳", "code": "101070100"},
        {"name": "大连", "code": "101070200"},
    ]},
    {"province": "内蒙古自治区", "cities": [
        {"name": "呼和浩特", "code": "101080100"},
        {"name": "包头", "code": "101080200"},
    ]},
    {"province": "河北省", "cities": [
        {"name": "石家庄", "code": "101090100"},
        {"name": "保定", "code": "101090200"},
        {"name": "唐山", "code": "101090500"},
    ]},
    {"province": "山西省", "cities": [
        {"name": "太原", "code": "101100100"},
        {"name": "大同", "code": "101100200"},
    ]},
    {"province": "陕西省", "cities": [
        {"name": "西安", "code": "101110100"},
        {"name": "咸阳", "code": "101110200"},
    ]},
    {"province": "山东省", "cities": [
        {"name": "济南", "code": "101120100"},
        {"name": "青岛", "code": "101120200"},
        {"name": "烟台", "code": "101120500"},
    ]},
    {"province": "新疆维吾尔自治区", "cities": [
        {"name": "乌鲁木齐", "code": "101130100"},
    ]},
    {"province": "西藏自治区", "cities": [{"name": "拉萨", "code": "101140100"}]},
    {"province": "青海省", "cities": [{"name": "西宁", "code": "101150100"}]},
    {"province": "甘肃省", "cities": [{"name": "兰州", "code": "101160100"}]},
    {"province": "宁夏回族自治区", "cities": [{"name": "银川", "code": "101170100"}]},
    {"province": "河南省", "cities": [
        {"name": "郑州", "code": "101180100"},
        {"name": "洛阳", "code": "101180900"},
    ]},
    {"province": "江苏省", "cities": [
        {"name": "南京", "code": "101190100"},
        {"name": "无锡", "code": "101190200"},
        {"name": "苏州", "code": "101190400"},
        {"name": "南通", "code": "101190500"},
        {"name": "常州", "code": "101191100"},
    ]},
    {"province": "湖北省", "cities": [
        {"name": "武汉", "code": "101200100"},
        {"name": "宜昌", "code": "101200900"},
    ]},
    {"province": "浙江省", "cities": [
        {"name": "杭州", "code": "101210100"},
        {"name": "嘉兴", "code": "101210300"},
        {"name": "宁波", "code": "101210400"},
        {"name": "温州", "code": "101210700"},
    ]},
    {"province": "安徽省", "cities": [
        {"name": "合肥", "code": "101220100"},
        {"name": "芜湖", "code": "101220300"},
    ]},
    {"province": "福建省", "cities": [
        {"name": "福州", "code": "101230100"},
        {"name": "厦门", "code": "101230200"},
        {"name": "泉州", "code": "101230500"},
    ]},
    {"province": "江西省", "cities": [
        {"name": "南昌", "code": "101240100"},
        {"name": "赣州", "code": "101240700"},
    ]},
    {"province": "湖南省", "cities": [
        {"name": "长沙", "code": "101250100"},
        {"name": "株洲", "code": "101250300"},
    ]},
    {"province": "贵州省", "cities": [{"name": "贵阳", "code": "101260100"}]},
    {"province": "四川省", "cities": [
        {"name": "成都", "code": "101270100"},
        {"name": "绵阳", "code": "101270400"},
    ]},
    {"province": "广东省", "cities": [
        {"name": "广州", "code": "101280100"},
        {"name": "深圳", "code": "101280600"},
        {"name": "珠海", "code": "101280700"},
        {"name": "佛山", "code": "101280800"},
        {"name": "东莞", "code": "101281600"},
    ]},
    {"province": "云南省", "cities": [{"name": "昆明", "code": "101290100"}]},
    {"province": "广西壮族自治区", "cities": [
        {"name": "南宁", "code": "101300100"},
        {"name": "柳州", "code": "101300300"},
    ]},
    {"province": "海南省", "cities": [
        {"name": "海口", "code": "101310100"},
        {"name": "三亚", "code": "101310200"},
    ]},
    {"province": "香港特别行政区", "cities": [{"name": "香港", "code": "101320300"}]},
    {"province": "澳门特别行政区", "cities": [{"name": "澳门", "code": "101330100"}]},
    {"province": "台湾省", "cities": [{"name": "台湾", "code": "101341100"}]},
]

_PROVINCE_BY_PREFIX = {
    "10001": "全国", "10101": "北京市", "10102": "上海市", "10103": "天津市",
    "10104": "重庆市", "10105": "黑龙江省", "10106": "吉林省", "10107": "辽宁省",
    "10108": "内蒙古自治区", "10109": "河北省", "10110": "山西省",
    "10111": "陕西省", "10112": "山东省", "10113": "新疆维吾尔自治区",
    "10114": "西藏自治区", "10115": "青海省", "10116": "甘肃省",
    "10117": "宁夏回族自治区", "10118": "河南省", "10119": "江苏省",
    "10120": "湖北省", "10121": "浙江省", "10122": "安徽省", "10123": "福建省",
    "10124": "江西省", "10125": "湖南省", "10126": "贵州省", "10127": "四川省",
    "10128": "广东省", "10129": "云南省", "10130": "广西壮族自治区",
    "10131": "海南省", "10132": "香港特别行政区", "10133": "澳门特别行政区",
    "10134": "台湾省",
}


def _full_city_groups() -> list:
    """外部完整码表可用时扩展；任何异常都安静回退到项目快照。"""
    path = config.SCRAPER_DIR / "data" / "city_codes.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return CITY_GROUPS
    if not isinstance(raw, dict) or len(raw) < 100:
        return CITY_GROUPS
    grouped = {province: [] for province in _PROVINCE_BY_PREFIX.values()}
    for name, code in raw.items():
        name, code = str(name).strip(), str(code).strip()
        province = _PROVINCE_BY_PREFIX.get(code[:5])
        if name and province:
            grouped[province].append({"name": name, "code": code})
    result = []
    for province in _PROVINCE_BY_PREFIX.values():
        values = grouped.get(province) or []
        if values:
            result.append({"province": province, "cities": values})
    return result if sum(len(group["cities"]) for group in result) >= 100 else CITY_GROUPS


def _indexes(groups: list) -> tuple[dict, dict]:
    by_code, by_name = {}, {}
    for group in groups:
        for city in group["cities"]:
            item = {"province": group["province"], "city": city["name"],
                    "city_code": city["code"]}
            by_code[city["code"]] = item
            by_name[city["name"]] = item
    return by_code, by_name


def city_groups() -> list:
    """返回可直接供前端渲染的副本。"""
    groups = _full_city_groups()
    return [{"province": group["province"],
             "cities": [dict(city) for city in group["cities"]]}
            for group in groups]


def resolve_city(value) -> dict:
    """把城市名、代码或保存对象规范化为省/市/code 三元组。"""
    by_code, by_name = _indexes(_full_city_groups())
    if isinstance(value, dict):
        code = str(value.get("city_code") or value.get("code") or "").strip()
        name = str(value.get("city") or value.get("name") or "").strip()
        found = by_code.get(code) or by_name.get(name)
        if found:
            return dict(found)
    else:
        text = str(value or "").strip()
        found = by_code.get(text) or by_name.get(text)
        if found:
            return dict(found)
    raise ValueError(f"不支持的城市: {value}")
