"""完成した勤務表の保存・読み込み。"""
import datetime
import json
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

SCHEDULES_DIR = Path(__file__).parent.parent / "saved_schedules"


def save_schedule(schedule_df: pd.DataFrame, year: int, month: int) -> Path:
    """勤務表を saved_schedules/YYYYMM.json に保存する。"""
    SCHEDULES_DIR.mkdir(exist_ok=True)
    path = SCHEDULES_DIR / f"{year}{month:02d}.json"

    schedule_data: Dict[str, Dict[str, str]] = {}
    for staff_id in schedule_df.index:
        row_data = {}
        for col in schedule_df.columns:
            val = schedule_df.at[staff_id, col]
            date_key = col.isoformat() if hasattr(col, "isoformat") else str(col)
            row_data[date_key] = str(val)
        schedule_data[str(int(staff_id))] = row_data

    data = {
        "year": year,
        "month": month,
        "saved_at": datetime.datetime.now().isoformat(),
        "schedule": schedule_data,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_schedule(year: int, month: int) -> Optional[pd.DataFrame]:
    """保存済み勤務表を DataFrame で返す。なければ None。"""
    path = SCHEDULES_DIR / f"{year}{month:02d}.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        schedule = data.get("schedule", {})
        if not schedule:
            return None

        records: Dict[int, Dict[datetime.date, str]] = {}
        for sid_str, date_dict in schedule.items():
            sid = int(sid_str)
            records[sid] = {
                datetime.date.fromisoformat(d): shift
                for d, shift in date_dict.items()
            }

        all_dates = sorted({d for row in records.values() for d in row.keys()})
        df = pd.DataFrame(index=sorted(records.keys()), columns=all_dates, dtype=object)
        for sid, date_dict in records.items():
            for d, shift in date_dict.items():
                df.at[sid, d] = shift
        df = df.fillna("O")
        return df
    except Exception:
        return None


def list_saved_schedules() -> List[Dict]:
    """保存済み勤務表の一覧を返す（新しい順）。"""
    if not SCHEDULES_DIR.exists():
        return []
    result = []
    for path in sorted(SCHEDULES_DIR.glob("*.json"), reverse=True):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            result.append({
                "year": data["year"],
                "month": data["month"],
                "saved_at": data.get("saved_at", ""),
                "path": str(path),
            })
        except Exception:
            pass
    return result


def get_prev_boundary(
    year: int,
    month: int,
    n_days: int = 3,
) -> Optional[Dict[int, Dict[datetime.date, str]]]:
    """
    前の勤務期間の末尾 n_days 日分のシフトを返す。
    ソルバーの境界制約（月またぎルール）に使用。

    例: month=7 のとき、6月期間（6/21〜7/20）の最後 n_days 日を返す。
    """
    prev_month = month - 1
    prev_year = year
    if prev_month == 0:
        prev_month = 12
        prev_year = year - 1

    df = load_schedule(prev_year, prev_month)
    if df is None:
        return None

    all_dates = sorted(df.columns)
    tail_dates = all_dates[-n_days:] if len(all_dates) >= n_days else all_dates

    result: Dict[int, Dict[datetime.date, str]] = {}
    for sid in df.index:
        result[int(sid)] = {d: str(df.at[sid, d]) for d in tail_dates}
    return result
