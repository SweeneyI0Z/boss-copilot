"""boss-copilot 全局配置：路径、双账号 profile、默认护栏参数。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("BOSS_COPILOT_HOME", Path.home() / ".boss-copilot"))
DB_PATH = DATA_DIR / "copilot.db"

# ── 双账号 CDP 隔离（互不连累：采集号承担风控，账号A只做沟通投递）──
ACCOUNTS = {
    # 采集号：复用 boss-zhipin-scraper 的隔离 profile 与端口
    "collect": {
        "label": "采集号（搜索/公司/详情采集，风控风险集中于此）",
        "profile_dir": Path.home() / ".boss-zhipin-scraper" / "chrome-profile",
        "cdp_port": 9222,
    },
    # 账号A：真实沟通投递号，独立 profile，仅发送/消息时使用
    "account_a": {
        "label": "账号A（真实沟通投递，只在发送与消息时启动）",
        "profile_dir": DATA_DIR / "chrome-profile-a",
        "cdp_port": 9223,
    },
}
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# ── 发送护栏（不可关闭部分；可调部分进 settings 表）──
DEFAULT_SETTINGS = {
    # LLM BYOK（OpenAI 兼容）
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model": "",
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
