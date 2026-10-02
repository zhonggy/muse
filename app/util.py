"""通用小工具。"""
from __future__ import annotations

import calendar
import random


def random_birthday(settings: dict) -> str:
    """在 [birthday_year_min, birthday_year_max] 年内随机生成一个合法生日。

    生日不再由用户填写，每个任务独立随机。
    """
    try:
        ymin = int(settings.get("birthday_year_min") or 1995)
        ymax = int(settings.get("birthday_year_max") or 2002)
    except (TypeError, ValueError):
        ymin, ymax = 1995, 2002
    if ymin > ymax:
        ymin, ymax = ymax, ymin
    year = random.randint(ymin, ymax)
    month = random.randint(1, 12)
    day = random.randint(1, calendar.monthrange(year, month)[1])
    return f"{year:04d}-{month:02d}-{day:02d}"
