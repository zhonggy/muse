"""muse.ai 页面元素定位规则与文案表（可被 data/selectors.json 覆盖）。"""
from __future__ import annotations

import json

from .config import DATA_DIR

#: 选择器（按优先级排列，取第一个命中的）
SELECTORS: dict[str, list[str]] = {
    "email_input": [
        'input[placeholder="手机号或邮箱"]',
        'input[autocomplete="username"]',
        'input[inputmode="email"]',
        'input[type="email"]',
        'input[name="email"]',
        'input[name="identifier"]',
        'input[name="username"]',
    ],
    "code_input": [
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
        'input[aria-label="6 位数安全码"]',
        'input[name="code"]',
        'input[name="otp"]',
        'input[placeholder*="验证码"]',
    ],
    "submit": ['button[type="submit"]'],
    "combobox": ['button[role="combobox"]'],
    "option": ['[role="option"]'],
    "any_button": ['button', '[role="button"]'],
    "card_number": [
        '[data-muse-fill="number"]',
        'input[autocomplete="cc-number"]',
        'input#cardNumber',
        'input[name="cardnumber"]',
        'input[name="card-number"]',
        'input[name="number"]',
        'input[placeholder*="卡号"]',
    ],
    "card_exp": [
        '[data-muse-fill="exp"]',
        'input[autocomplete="cc-exp"]',
        'input#cardExpiry',
        'input[name="exp-date"]',
        'input[placeholder*="MM"]',
    ],
    "card_cvc": [
        '[data-muse-fill="cvc"]',
        'input[autocomplete="cc-csc"]',
        'input#cardCvc',
        'input[name="cvc"]',
        'input[name="cvv"]',
    ],
    "card_name": [
        '[data-muse-fill="name"]',
        'input[autocomplete="cc-name"]',
        'input#billingName',
        'input[name="name"]',
    ],
    "card_postal": [
        '[data-muse-fill="postal"]',
        'input[autocomplete="postal-code"]',
        'input#billingPostalCode',
        'input[name="postal"]',
        'input[name="zip"]',
    ],
}

#: 按钮 / 文案（可多个候选，逐个尝试）
TEXTS: dict[str, list[str]] = {
    "expand_login": [
        "使用手机号或邮箱", "使用邮箱或手机号", "使用邮箱", "手机号或邮箱",
        "使用手机号", "登录", "Log in", "Sign in",
    ],
    "continue": ["继续", "下一步", "Continue", "Next"],
    "confirm": ["确认", "确定", "Confirm", "Submit"],
    "disclosure_start": ["开始"],
    "verify_age": ["验证年龄", "验证年龄以继续"],
    "open_checkout": ["打开安全结账", "打开结账", "Open secure checkout"],
    "card_submit": [
        "继续", "确认", "提交", "支付", "订阅", "开始使用", "完成", "保存",
        "Continue", "Confirm", "Submit", "Pay", "Subscribe", "Start", "Save",
    ],
    # 年龄验证页「已通过 / 回到主页」的判定文案
    "verified_ok": ["开始使用", "开始", "进入", "继续使用"],
}

#: 关键页面 URL 片段
URLS = {
    "home": "muse.ai",
    "disclosure": "/access/disclosure",
    "verification": "/access/verification",
}

#: 允许 data/selectors.json 覆盖
_OVERRIDE_FILE = DATA_DIR / "selectors.json"


def load_overrides() -> None:
    if not _OVERRIDE_FILE.exists():
        return
    try:
        raw = json.loads(_OVERRIDE_FILE.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for key, value in (raw.get("selectors") or {}).items():
        if isinstance(value, list) and value:
            SELECTORS[key] = value
        elif isinstance(value, str):
            SELECTORS[key] = [value]
    for key, value in (raw.get("texts") or {}).items():
        if isinstance(value, list) and value:
            TEXTS[key] = value
        elif isinstance(value, str):
            TEXTS[key] = [value]
    for key, value in (raw.get("urls") or {}).items():
        URLS[key] = value
