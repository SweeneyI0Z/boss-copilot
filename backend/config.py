"""boss-copilot 全局配置：跨平台路径、账号 profile、默认护栏参数。"""
import ntpath
import os
import platform
import shutil
import socket
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
_DATA_HOME_OVERRIDE = os.environ.get("BOSS_COPILOT_HOME", "").strip()
DATA_DIR = Path(_DATA_HOME_OVERRIDE).expanduser() if _DATA_HOME_OVERRIDE else \
    Path.home() / ".boss-copilot"
DB_PATH = DATA_DIR / "copilot.db"
COLLECT_PROFILE_DIR = DATA_DIR / "chrome-profile-collect"
COMMUNICATION_PROFILE_DIR = DATA_DIR / "chrome-profile-communication"
COLLECT_RESULT_DIR = DATA_DIR / "job-result"


def default_chrome_path(system=None, environ=None) -> str:
    """按系统解析 Chrome；可用 BOSS_CHROME_PATH 显式覆盖。"""
    env = environ or os.environ
    override = env.get("BOSS_CHROME_PATH", "").strip()
    if override:
        return str(Path(override).expanduser())
    system = system or platform.system()
    if system == "Darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    elif system == "Windows":
        candidates = []
        local = env.get("LOCALAPPDATA")
        if local:
            candidates.append(ntpath.join(local, "Google", "Chrome", "Application",
                                          "chrome.exe"))
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            if env.get(key):
                candidates.append(ntpath.join(env[key], "Google", "Chrome", "Application",
                                              "chrome.exe"))
    else:
        candidates = ["/usr/bin/google-chrome", "/usr/bin/chromium-browser",
                      "/usr/bin/chromium"]
    return next((p for p in candidates if Path(p).exists()),
                candidates[0] if candidates else "chrome")


CHROME_PATH = default_chrome_path()
SCRAPER_DIR = Path(os.environ.get(
    "BOSS_ZHIPIN_SCRAPER_HOME", str(BASE_DIR.parent / "boss-zhipin-scraper"))).expanduser()
SCRAPER_PY = SCRAPER_DIR / ".venv" / ("Scripts/python.exe" if os.name == "nt"
                                      else "bin/python")
SCRAPER_SCRIPT = SCRAPER_DIR / "scripts" / "boss_cdp_raw.py"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def migrate_legacy_data(data_dir=None, home_dir=None, check_ports=True) -> list:
    """把旧 profile/结果目录迁入统一数据目录；目标存在时绝不覆盖。"""
    target_root = Path(data_dir) if data_dir is not None else DATA_DIR
    user_home = Path(home_dir) if home_dir is not None else Path.home()
    migrations = [
        (target_root / "chrome-profile-a",
         target_root / "chrome-profile-communication", 9223),
    ]
    # 测试或便携部署显式覆盖数据目录时，不触碰用户主目录里的历史数据。
    if home_dir is not None or not _DATA_HOME_OVERRIDE:
        legacy_root = user_home / ".boss-zhipin-scraper"
        migrations += [
            (legacy_root / "chrome-profile", target_root / "chrome-profile-collect", 9222),
            (legacy_root / "job-result", target_root / "job-result", None),
        ]

    report = []
    target_root.mkdir(parents=True, exist_ok=True)
    for source, target, port in migrations:
        if not source.exists():
            continue
        if target.exists():
            report.append({"status": "skipped_target_exists", "source": str(source),
                           "target": str(target)})
            continue
        if port and check_ports and _port_open(port):
            report.append({"status": "skipped_in_use", "source": str(source),
                           "target": str(target)})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(source), str(target))
            report.append({"status": "moved", "source": str(source),
                           "target": str(target)})
        except OSError as e:
            report.append({"status": "failed", "source": str(source),
                           "target": str(target), "error": str(e)[:200]})
    return report

# ── 账号 CDP 隔离（双账号时采集号承担风控，沟通号只做沟通投递）──
ACCOUNTS = {
    # 采集号：独立 profile 与端口
    "collect": {
        "label": "采集号",
        "description": "搜索、公司与详情采集，风控风险集中于此",
        "profile_dir": COLLECT_PROFILE_DIR,
        "cdp_port": 9222,
    },
    # 沟通号：真实沟通投递号；单账号模式下也承担采集
    "account_a": {
        "label": "沟通号",
        "description": "发送招呼语与收发消息",
        "profile_dir": COMMUNICATION_PROFILE_DIR,
        "cdp_port": 9223,
    },
}
# ── 发送护栏（不可关闭部分；可调部分进 settings 表）──
DEFAULT_SETTINGS = {
    # LLM BYOK（OpenAI 兼容）
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model": "",
    # 账号管理：关闭后，采集和沟通统一使用沟通号
    "dual_account_enabled": True,
    # 发送护栏
    "send_daily_limit": 40,        # 每日招呼语发送上限（人工确认模式下的软上限）
    "send_daily_hard_cap": 110,    # 硬顶（BOSS 120 软限制前必须停）
    "send_gap_min_sec": 30,        # 两条发送之间的随机间隔下限
    "send_gap_max_sec": 90,
    # 同步刷新
    "hr_inactive_days": 14,        # HR 活跃度早于 N 天 → 剔除出推荐池
    # 评分
    "l2_top_n": 60,                # L1 后取 Top N 进 L2
}

DEFAULT_SKILL_DICTIONARY = {
    # 摘自《岗位筛选评分规则.md》附录（简历基线：本科/4年/25-30K/AI应用×嵌入式）
    "embedded": ["C语言", "STM32", "GD32", "Cortex-M", "8051", "51单片机", "芯海", "Keil",
                 "IAR", "STM32CubeIDE", "J-Link", "LVGL", "MicroPython", "QuecPython",
                 "单片机", "MCU", "固件", "OTA", "驱动", "嵌入式"],
    "comm_iot": ["4G", "LTE", "蓝牙", "BLE", "2.4G", "RF", "AT指令", "MQTT", "HTTP",
                 "TCP/IP", "通信协议", "物联网", "IoT", "模组", "移远", "Quectel", "BG95",
                 "配对", "加密"],
    "hardware": ["原理图", "PCB", "Altium", "PADS", "EMC", "安规", "示波器", "逻辑分析仪",
                 "频谱仪", "BOM", "选型"],
    "ai_soft": ["Python", "FastAPI", "LangChain", "Agent", "LLM", "大模型", "MCP", "Prompt",
                "Codex", "Claude", "TypeScript", "React", "Electron", "自动化测试", "Git", "全栈"],
    "domain_algo": ["医疗器械", "GB 9706", "注册", "法规", "血氧", "血压", "体重秤", "TENS",
                    "理疗", "康复", "PID", "滤波器"],
    # 半权重项（项目接触非主业）
    "half_weight": ["RTOS", "Linux", "WebSocket", "TypeScript", "React", "Bootloader", "C++"],
}
