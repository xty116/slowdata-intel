"""时间窗工具：解析各类日期格式，判断是否落在提需时间前一周（默认 7 天）内。"""
from __future__ import annotations

import email.utils
import re
from datetime import date, datetime, timedelta


def parse_date(s: str | None) -> date | None:
    """尽力解析日期字符串，失败返回 None（视为时间未知）。"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    try:
        return email.utils.parsedate_to_datetime(s).date()
    except Exception:  # noqa: BLE001
        pass
    return None


def in_window(date_str: str | None, run_date: date | str, days: int = 7) -> bool | None:
    """True=窗内；False=超窗（早于窗口或未来）；None=时间未知。"""
    d = parse_date(date_str)
    if d is None:
        return None
    run = run_date if isinstance(run_date, date) else date.fromisoformat(str(run_date))
    return run - timedelta(days=days) <= d <= run
