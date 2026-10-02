"""全局配置：环境变量 + 可持久化设置（存 data/settings.json）。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

DATA_DIR = Path(os.getenv("MUSE_DATA_DIR", str(BASE_DIR / "data")))
SESSION_DIR = DATA_DIR / "sessions"
SHOT_DIR = DATA_DIR / "shots"


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


#: 默认设置（首次启动写入 data/settings.json，之后以文件为准）
DEFAULT_SETTINGS: dict = {
    # --- skymail 收码 ---
    "skymail_base_url": os.getenv("SKYMAIL_BASE_URL", "https://skymail.ink"),
    "skymail_email": os.getenv("SKYMAIL_EMAIL", ""),
    "skymail_password": os.getenv("SKYMAIL_PASSWORD", ""),
    "code_timeout": _int("MUSE_CODE_TIMEOUT", 240),          # 等待验证码超时（秒）
    "code_poll_interval": _float("MUSE_CODE_POLL_INTERVAL", 3.0),

    # --- 注册资料 ---
    # 生日不再由用户填写，每个任务在 [min, max] 年内随机生成
    "birthday_year_min": _int("MUSE_BIRTHDAY_YEAR_MIN", 1995),
    "birthday_year_max": _int("MUSE_BIRTHDAY_YEAR_MAX", 2002),
    "email_domain": os.getenv("MUSE_EMAIL_DOMAIN", ""),   # 留空则自动取 skymail 已有域名
    "default_card_id": "",                                # 默认使用的卡

    # --- Resin 代理池 ---
    # resin_url 含代理基础地址与 Token，形如 http://127.0.0.1:2260/my-token
    "resin_url": os.getenv("RESIN_URL", ""),
    "resin_platform_name": os.getenv("RESIN_PLATFORM_NAME", "Default"),
    # 配了 resin_url 就默认启用；想临时绕过（调试）可以关掉
    "resin_enabled": _bool("RESIN_ENABLED", True),

    # --- 浏览器 ---
    "headless": _bool("MUSE_HEADLESS", True),
    "slow_mo": _int("MUSE_SLOW_MO", 0),
    # 反检测：抹掉自动化特征（启动参数 + 页面注入）
    "stealth": _bool("MUSE_STEALTH", True),
    # 仅在未配置 Resin 时生效（Resin 优先级更高，见 app/resin.py）
    "proxy": os.getenv("MUSE_PROXY", ""),
    "locale": os.getenv("MUSE_LOCALE", "zh-CN"),
    "timezone": os.getenv("MUSE_TIMEZONE", "Asia/Shanghai"),
    "viewport_w": _int("MUSE_VIEWPORT_W", 1280),
    "viewport_h": _int("MUSE_VIEWPORT_H", 820),
    "muse_url": os.getenv("MUSE_URL", "https://muse.ai/"),
    "user_agent": os.getenv("MUSE_USER_AGENT", ""),

    # --- 任务 ---
    "concurrency": _int("MUSE_CONCURRENCY", 1),              # 并行任务数
    "step_timeout": _int("MUSE_STEP_TIMEOUT", 60),           # 单步超时
    "screenshot_interval": _float("MUSE_SCREENSHOT_INTERVAL", 1.5),
    "screenshot_quality": _int("MUSE_SCREENSHOT_QUALITY", 55),
    "stop_at_verification": _bool("MUSE_STOP_AT_VERIFICATION", False),
    "auto_fill_card": _bool("MUSE_AUTO_FILL_CARD", True),
    "manual_takeover": _bool("MUSE_MANUAL_TAKEOVER", True),  # 允许控制台点击接管
    "live_view": _bool("MUSE_LIVE_VIEW", True),             # 是否推送实时画面
}

#: 控制台鉴权（留空则不需要登录）
CONSOLE_USER = os.getenv("MUSE_CONSOLE_USER", "admin")
CONSOLE_PASSWORD = os.getenv("MUSE_CONSOLE_PASSWORD", "")


def ensure_dirs() -> None:
    for d in (DATA_DIR, SESSION_DIR, SHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)
