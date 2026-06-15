"""スタッフ別・日別集計タブ。"""
import datetime
from typing import List

import pandas as pd
import streamlit as st

SHIFT_HOURS = {"D": 7.5, "L": 7.5, "N1": 8.0, "N2": 8.0, "O": 0.0, "P": 0.0,
               "T": 7.5, "I": 7.5}


def render_summary(
    schedule_df: pd.DataFrame,
    staff_df: pd.DataFrame,
    dates: List[datetime.date],
) -> None:
    sid_to_name = {row["id"]: row["name"] for _, row in staff_df.iterrows()}
    sid_to_target = {row["id"]: row["target_hours"] for _, row in staff_df.iterrows()}

    rows = []
    for staff_id in schedule_df.index:
        shifts = [schedule_df.loc[staff_id, d] for d in dates if d in schedule_df.columns]
        counts = {s: shifts.count(s) for s in ["D", "L", "N1", "N2", "O", "P", "T", "I"]}
        total_h = sum(SHIFT_HOURS.get(s, 0.0) for s in shifts)
        target = sid_to_target.get(staff_id, 170.0)
        rows.append({
            "氏名":       sid_to_name.get(staff_id, str(staff_id)),
            "日":         counts["D"],
            "/イ":        counts["I"],
            "研修":       counts["T"],
            "オ2":        counts["L"],
            "ヤ1":        counts["N1"],
            "ヤ2":        counts["N2"],
            "夜勤回数":   counts["N1"],
            "休":         counts["O"],
            "有":         counts["P"],
            "勤務時間(h)": round(total_h, 1),
            "目標時間(h)": target,
            "差異(h)":    round(total_h - target, 1),
        })

    df = pd.DataFrame(rows)

    def color_diff(val):
        if val > 10:
            return "background-color:#FFCCBC"
        if val < -10:
            return "background-color:#BBDEFB"
        return ""

    styled = df.style.map(color_diff, subset=["差異(h)"]).format({"差異(h)": "{:+.1f}"})
    st.dataframe(styled, width="stretch", height=min(80 + 35 * len(rows), 900))

    # 統計サマリー
    st.caption(
        f"平均勤務時間: {df['勤務時間(h)'].mean():.1f}h　"
        f"最大: {df['勤務時間(h)'].max():.1f}h　"
        f"最小: {df['勤務時間(h)'].min():.1f}h　"
        f"平均夜勤: {df['夜勤回数'].mean():.1f}回"
    )
