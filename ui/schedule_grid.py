"""勤務表グリッドの表示・編集。"""
import datetime
from typing import Dict, List, Set

import pandas as pd
import streamlit as st

from utils.time_utils import is_holiday, is_weekday, SHIFT_LABEL, LABEL_SHIFT, SHIFTS

# 表示ラベル一覧（ドロップダウン用）― 研修・/イ も手動編集で選べるように追加
DISPLAY_OPTIONS = (
    [""]                                                      # 空欄（作成前の初期状態）
    + [SHIFT_LABEL[s] for s in SHIFTS]                       # 日, オ2, ヤ1, ヤ2, 休, 有
    + [SHIFT_LABEL["T"], SHIFT_LABEL["I"], SHIFT_LABEL["C"]] # 研修, /イ, 認
)

LABEL_BG = {
    "日":  "#DDEEFF",
    "オ2": "#EEFFDD",
    "ヤ1": "#FFE0B2",
    "ヤ2": "#FFCC80",
    "休":  "#F5F5F5",
    "有":  "#FCE4EC",
    "研修": "#E8F5E9",
    "/イ": "#FFF9C4",   # 薄黄色
    "認":  "#E0F2F1",   # 薄ティール（認定看護師業務）
}


def _to_labels(df: pd.DataFrame) -> pd.DataFrame:
    """内部コード DataFrame → 表示ラベル DataFrame"""
    return df.map(lambda v: SHIFT_LABEL.get(v, v))


def _to_codes(df: pd.DataFrame) -> pd.DataFrame:
    """表示ラベル DataFrame → 内部コード DataFrame"""
    return df.map(lambda v: LABEL_SHIFT.get(v, v))


def render_grid(
    schedule_df: pd.DataFrame,
    staff_df: pd.DataFrame,
    dates: List[datetime.date],
    violations: List[Dict],
    key_prefix: str = "grid",
    hospital_holidays=(),
    hide_index: bool = False,
) -> pd.DataFrame:
    """
    勤務表グリッドを st.data_editor で表示（表示ラベル使用）。
    編集後の DataFrame（内部コード）を返す。

    hide_index=True のとき index（氏名列）を非表示にする
    （ドラッグリストを左隣に置く横並びレイアウト用）。
    """
    # order 列があればソート、なければ元の順序を維持
    if "order" in staff_df.columns:
        staff_sorted = staff_df.sort_values("order")
    else:
        staff_sorted = staff_df

    sid_to_name = {row["id"]: row["name"] for _, row in staff_sorted.iterrows()}

    # 違反セット
    violation_cells: Set = set()
    for v in violations:
        if v.get("staff_id") and v.get("date"):
            violation_cells.add((v["staff_id"], v["date"]))

    # 表示用 DataFrame — staff_id の順序を order に合わせて並べ替え
    ordered_ids = [row["id"] for _, row in staff_sorted.iterrows()
                   if row["id"] in schedule_df.index]
    display_df = _to_labels(schedule_df.reindex(ordered_ids).copy())
    display_df.index = [sid_to_name.get(i, str(i)) for i in display_df.index]
    import holidays as holidays_lib
    _jp_hols_cache = holidays_lib.Japan(years={d.year for d in dates})
    _hosp_hols_set = set(hospital_holidays)
    def _day_label(d):
        wname = '月火水木金土日'[d.weekday()]
        if d in _hosp_hols_set:
            return f"{d.month}/{d.day}(病休)"
        if d in _jp_hols_cache:
            return f"{d.month}/{d.day}(祝)"
        return f"{d.month}/{d.day}({wname})"

    display_df.columns = [_day_label(d) for d in dates]

    # 列設定（表示ラベルのドロップダウン）
    col_config = {
        col: st.column_config.SelectboxColumn(col, options=DISPLAY_OPTIONS, width="small")
        for col in display_df.columns
    }
    if not hide_index:
        col_config["_index"] = st.column_config.TextColumn("氏名", width="medium")

    n_rows = len(display_df)
    grid_height = min(60 + 35 * n_rows, 900)

    st.markdown("""
<style>
/* セル編集ドロップダウンのスタイル */
.ag-popup-editor {
    box-shadow: 0 6px 20px rgba(0,0,0,0.5) !important;
    border: 2px solid #4a90d9 !important;
    border-radius: 6px !important;
    overflow: hidden !important;
}
.ag-rich-select,
.ag-rich-select-list,
.ag-rich-select-virtual-list-viewport,
.ag-rich-select-virtual-list-item {
    background-color: #2b3a55 !important;
}
.ag-rich-select-row {
    background-color: #2b3a55 !important;
    color: #e8edf5 !important;
    font-size: 13px !important;
    min-height: 28px !important;
}
.ag-rich-select-row:hover {
    background-color: #4a90d9 !important;
    color: #ffffff !important;
}
.ag-rich-select-row.ag-rich-select-row-selected {
    background-color: #3a6fad !important;
    color: #ffffff !important;
    font-weight: 600 !important;
}
</style>
""", unsafe_allow_html=True)

    edited_labels = st.data_editor(
        display_df,
        column_config=col_config,
        use_container_width=True,
        height=grid_height,
        hide_index=hide_index,
        key=key_prefix,
    )

    # 氏名 → staff_id、表示ラベル → 内部コードに戻す（order ソート後の sid_to_name を使用）
    name_to_sid = {v: k for k, v in sid_to_name.items()}
    result = _to_codes(edited_labels)
    result.index = [name_to_sid.get(n, n) for n in result.index]
    result.columns = schedule_df.columns

    return result


def render_day_summary(
    schedule_df: pd.DataFrame,
    dates: List[datetime.date],
    requirements: Dict,
    hospital_holidays=(),
) -> None:
    """日別集計バーを表示。"""
    rows = []
    for d in dates:
        if d not in schedule_df.columns:
            continue
        col = schedule_df[d]
        day_type = "holiday" if is_holiday(d, hospital_holidays) else "weekday"
        req_d = requirements.get("D", {}).get(day_type, 0)
        rows.append({
            "日付":  f"{d.month}/{d.day}",
            "日":    int((col == "D").sum()),
            "オ2":  int((col == "L").sum()),
            "ヤ1":  int((col == "N1").sum()),
            "ヤ2":  int((col == "N2").sum()),
            "休+有": int(((col == "O") | (col == "P")).sum()),
            "日必要": req_d,
            "夜必要": requirements.get("N", {}).get("base", 4),
        })
    st.dataframe(pd.DataFrame(rows).set_index("日付").T, use_container_width=True, height=230)
