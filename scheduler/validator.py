"""師長が手動編集した勤務表の制約違反チェック。"""
import datetime
from typing import Dict, List, Optional

import pandas as pd

from utils.time_utils import is_holiday, is_weekday


def validate(
    schedule_df: pd.DataFrame,
    staff_df: pd.DataFrame,
    dates: List[datetime.date],
    requirements: Dict[str, Dict[str, int]],
    first_year_ids: List[int],
    daycare_closed_dates: Optional[List] = None,
    hospital_holidays: Optional[List] = None,
) -> List[Dict]:
    """
    違反リストを返す。各要素は:
      {"type": str, "staff_id": int|None, "date": date|None, "detail": str}
    """
    violations = []
    _hosp_hols = set(hospital_holidays) if hospital_holidays else set()
    sid_to_row = {row["id"]: row for _, row in staff_df.iterrows()}

    for staff_id in schedule_df.index:
        row = sid_to_row.get(staff_id)
        if row is None:
            continue
        shifts = [schedule_df.loc[staff_id, d] for d in dates]
        n_days = len(dates)

        for d, date in enumerate(dates):
            s = shifts[d]

            # H1: N1の翌日はN2（最終日のN1は翌月への持ち越しなので除外）
            if s == "N1" and d < n_days - 1 and shifts[d + 1] != "N2":
                violations.append({
                    "type": "H1: N1翌日がN2でない",
                    "staff_id": staff_id,
                    "date": date,
                    "detail": f"N1の翌日={shifts[d+1]}",
                })
            # H1逆: N2の前日はN1（初日のN2は前月からの持ち越しなので除外）
            if s == "N2" and d > 0 and shifts[d - 1] != "N1":
                violations.append({
                    "type": "H1: N2前日がN1でない",
                    "staff_id": staff_id,
                    "date": date,
                    "detail": f"N2の前日={shifts[d-1]}",
                })

            # H2: N2の翌日はN1またはO
            if s == "N2" and d < n_days - 1 and shifts[d + 1] not in ("N1", "O"):
                violations.append({
                    "type": "H2: N2翌日がN1/O以外",
                    "staff_id": staff_id,
                    "date": date,
                    "detail": f"N2の翌日={shifts[d+1]}",
                })

            # H3: 2連続夜勤後の2日休み
            if d >= 2 and d + 2 < n_days:
                if shifts[d - 2] == "N2" and s == "N2":
                    if shifts[d + 1] != "O":
                        violations.append({
                            "type": "H3: 2連続夜勤後の翌日がO以外",
                            "staff_id": staff_id,
                            "date": date,
                            "detail": f"翌日={shifts[d+1]}",
                        })
                    if shifts[d + 2] != "O":
                        violations.append({
                            "type": "H3: 2連続夜勤後の翌々日がO以外",
                            "staff_id": staff_id,
                            "date": date,
                            "detail": f"翌々日={shifts[d+2]}",
                        })

            # H5: 夜勤不可
            if not row["night_ok"] and s in ("N1", "N2"):
                violations.append({
                    "type": "H5: 夜勤不可スタッフが夜勤",
                    "staff_id": staff_id,
                    "date": date,
                    "detail": "",
                })

        # S1: 日勤連続5日以上
        for d in range(n_days - 4):
            if all(shifts[d + i] == "D" for i in range(5)):
                violations.append({
                    "type": "S1: 日勤5連続以上",
                    "staff_id": staff_id,
                    "date": dates[d],
                    "detail": f"{dates[d]}〜{dates[d+4]}",
                })

    # H7: 必要人数チェック
    for d, date in enumerate(dates):
        day_type = "holiday" if is_holiday(date, _hosp_hols) else "weekday"
        day_shifts = schedule_df[date]

        # N1 チェック（全日：最終日は翌月への夜勤入りが必要）
        fy_in_n1 = any(
            day_shifts.get(sid) == "N1"
            for sid in first_year_ids
            if sid in day_shifts.index
        )
        req_n1 = 5 if fy_in_n1 else 4
        actual_n1 = (day_shifts == "N1").sum()
        if actual_n1 < req_n1:
            violations.append({
                "type": "H7: 夜勤入り人数不足",
                "staff_id": None,
                "date": date,
                "detail": f"N1={actual_n1}名（必要{req_n1}名）",
            })

        # N2 チェック（全日：初日は前月からの夜勤明けが必要）
        # N2人数は前日のN1で1年目がいたかどうかで決まる
        if d > 0:
            prev_date = dates[d - 1]
            prev_shifts = schedule_df[prev_date]
            fy_in_prev_n1 = any(
                prev_shifts.get(sid) == "N1"
                for sid in first_year_ids
                if sid in prev_shifts.index
            )
        else:
            fy_in_prev_n1 = False  # 初日: 前月情報不明のため4名基準
        req_n2 = 5 if fy_in_prev_n1 else 4
        actual_n2 = (day_shifts == "N2").sum()
        if actual_n2 < req_n2:
            violations.append({
                "type": "H7: 夜勤明け人数不足",
                "staff_id": None,
                "date": date,
                "detail": f"N2={actual_n2}名（必要{req_n2}名）",
            })

        # 日勤人数チェック
        d_req = requirements.get("D", {}).get(day_type, 0)
        actual_d = (day_shifts == "D").sum()
        if actual_d < d_req:
            violations.append({
                "type": "H7: 日勤人数不足",
                "staff_id": None,
                "date": date,
                "detail": f"D={actual_d}名（必要{d_req}名）",
            })

        # 遅出チェック: 平日のみ1名、土日祝は0名
        actual_l = (day_shifts == "L").sum()
        if is_weekday(date, _hosp_hols):
            l_req = requirements.get("L", {}).get("weekday", 1)
            if actual_l != l_req:
                violations.append({
                    "type": "H7: 遅出人数違反",
                    "staff_id": None,
                    "date": date,
                    "detail": f"オ2={actual_l}名（平日は{l_req}名）",
                })
        else:
            if actual_l > 0:
                violations.append({
                    "type": "H7: 土日祝に遅出あり",
                    "staff_id": None,
                    "date": date,
                    "detail": f"オ2={actual_l}名（土日祝は0名）",
                })

    # 保育園利用スタッフが休園日に勤務していないかチェック
    if daycare_closed_dates:
        daycare_ids = staff_df[staff_df.get("daycare", pd.Series(False, index=staff_df.index)).astype(bool)]["id"].tolist() \
            if "daycare" in staff_df.columns else []
        work_shifts = {"D", "L", "N1", "N2"}
        for sid in daycare_ids:
            if sid not in schedule_df.index:
                continue
            name = staff_df.loc[staff_df["id"] == sid, "name"].values[0]
            for dc in daycare_closed_dates:
                if dc not in schedule_df.columns:
                    continue
                shift = schedule_df.loc[sid, dc]
                if shift in work_shifts:
                    violations.append({
                        "type": "保育園: 休園日に勤務",
                        "staff_id": sid,
                        "date": dc,
                        "detail": f"{name} 保育園休園日（{dc}）に {shift} が割り当たっています",
                    })

    return violations
