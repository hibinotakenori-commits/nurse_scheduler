import datetime
from functools import lru_cache
from typing import List

import holidays as holidays_lib

SHIFT_HOURS = {"D": 7.5, "L": 7.5, "N1": 8.0, "N2": 8.0, "O": 0.0, "P": 0.0,
               "T": 7.5, "I": 7.5, "C": 7.5}  # T=研修, I=委員会(/イ), C=認定
SHIFTS     = ["D", "L", "N1", "N2", "O", "P"]   # ソルバー用（T・I・C は D として扱う）
SHIFT_IDX  = {s: i for i, s in enumerate(SHIFTS)}

# ソルバー外でのみ使う特殊シフト（D として扱い、出力は元のコードで表示）
OVERLAY_SHIFTS = {"T", "I", "C"}  # 研修, 委員会, 認定

# 内部コード → 表示ラベル
SHIFT_LABEL = {
    "D": "日", "L": "オ2", "N1": "ヤ1", "N2": "ヤ2",
    "O": "休", "P": "有", "T": "研修", "I": "/イ", "C": "認",
}
# 表示ラベル → 内部コード
LABEL_SHIFT = {v: k for k, v in SHIFT_LABEL.items()}

# 希望入力で使うラベルとソルバー用コードのマッピング
# (shift_code, is_fixed)
REQUEST_OPTIONS = [
    "（なし）",
    "日勤希望", "夜勤希望", "夜勤確定", "深夜確定",
    "研修", "委員会", "認定",
    "休み希望", "有休申請",
]
REQUEST_TO_SHIFT: dict = {
    "日勤希望": ("D",  False),
    "夜勤希望": ("N1", False),
    "夜勤確定": ("N1", True),
    "深夜確定": ("N2", True),
    "研修":     ("T",  True),
    "委員会":   ("I",  True),
    "認定":     ("C",  True),
    "休み希望": ("O",  False),
    "有休申請": ("P",  True),
}
SHIFT_TO_REQUEST: dict = {v: k for k, v in REQUEST_TO_SHIFT.items()}


@lru_cache(maxsize=8)
def _jp_holidays(year: int):
    return holidays_lib.Japan(years=year)


def is_holiday(d: datetime.date, extra_holidays=()) -> bool:
    """土曜・日曜・日本の祝日、または病院独自休日を True とする。

    Args:
        extra_holidays: 病院独自休日の日付コレクション（任意）
    """
    if d.weekday() >= 5:
        return True
    if d in _jp_holidays(d.year):
        return True
    if extra_holidays and d in extra_holidays:
        return True
    return False


def is_weekday(d: datetime.date, extra_holidays=()) -> bool:
    """月〜金かつ祝日・病院独自休日でない日を True とする（遅出配置対象）。"""
    return not is_holiday(d, extra_holidays)


def schedule_dates(year: int, month: int) -> List[datetime.date]:
    """当月21日〜翌月20日の日付リストを返す。"""
    start = datetime.date(year, month, 21)
    if month == 12:
        end = datetime.date(year + 1, 1, 20)
    else:
        end = datetime.date(year, month + 1, 20)
    days = []
    d = start
    while d <= end:
        days.append(d)
        d += datetime.timedelta(days=1)
    return days


def calc_monthly_hours(schedule_row, dates) -> float:
    return sum(SHIFT_HOURS.get(schedule_row[d], 0.0) for d in dates)
