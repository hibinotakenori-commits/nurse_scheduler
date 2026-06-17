"""スタッフ向け勤務希望入力ページ（管理機能なし）。"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import holidays as holidays_lib
import pandas as pd
import streamlit as st

from utils.settings import load_settings, staff_df_from_settings, load_requests, save_requests
from utils.time_utils import schedule_dates, REQUEST_OPTIONS, REQUEST_TO_SHIFT
from ui.request_calendar import (
    _render_month_calendar,
    _render_staff_summary,
    _code_to_option,
    _FULL_TO_SHORT,
    _SHORT_TO_FULL,
    _BADGE_COLOR,
    SHORT_OPTIONS,
)

st.set_page_config(
    page_title="勤務希望入力 - 3A病棟",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# サイドバー・ページナビゲーションを完全非表示
st.markdown("""
<style>
section[data-testid="stSidebar"]       { display: none !important; }
button[data-testid="collapsedControl"] { display: none !important; }
[data-testid="stSidebarNav"]           { display: none !important; }
#MainMenu                              { display: none !important; }
footer                                 { display: none !important; }
header[data-testid="stHeader"]         { display: none !important; }
</style>
""", unsafe_allow_html=True)


# ── 初期化 ───────────────────────────────────────────────────
def _init():
    if "_staff_page_loaded" not in st.session_state:
        _s = load_settings()
        st.session_state._staff_page_loaded = True
        st.session_state.staff_df    = staff_df_from_settings(_s)
        st.session_state.requests_df = load_requests()
        if _s.get("target_year"):
            st.session_state["_staff_year"]  = _s["target_year"]
        if _s.get("target_month"):
            st.session_state["_staff_month"] = _s["target_month"]
    if "requests_df" not in st.session_state:
        st.session_state.requests_df = load_requests()

_init()

staff_df = st.session_state.staff_df

# ── ヘッダー ─────────────────────────────────────────────────
st.markdown("""
<div style='background:#1565c0;color:white;padding:16px 24px;
            border-radius:8px;margin-bottom:20px'>
  <span style='font-size:22px;font-weight:bold'>📅 勤務希望入力</span>
  <span style='font-size:13px;margin-left:16px;opacity:0.85'>3A病棟</span>
</div>
""", unsafe_allow_html=True)

# ── 名前選択 ─────────────────────────────────────────────────
_names = staff_df.sort_values("order")["name"].tolist()
_name_col, _period_col = st.columns([2, 3])
with _name_col:
    selected_name = st.selectbox(
        "👤 あなたの名前を選んでください",
        ["（選択してください）"] + _names,
        key="staff_self_name",
    )

if selected_name == "（選択してください）":
    st.info("まず上のメニューからあなたの名前を選択してください。")
    st.stop()

_sid_map = {row["name"]: int(row["id"]) for _, row in staff_df.iterrows()}
selected_sid = _sid_map[selected_name]

# ── 対象期間 ─────────────────────────────────────────────────
now = datetime.date.today()
_default_year  = st.session_state.get("_staff_year",  now.year)
_default_month = st.session_state.get("_staff_month", now.month)
_year_options  = list(range(now.year - 1, now.year + 3))

with _period_col:
    _pc1, _pc2 = st.columns(2)
    with _pc1:
        _year = st.selectbox(
            "📆 年",
            _year_options,
            index=_year_options.index(_default_year) if _default_year in _year_options else 1,
            key="_staff_year",
        )
    with _pc2:
        _month = st.selectbox("月", range(1, 13), index=_default_month - 1, key="_staff_month")

dates = schedule_dates(_year, _month)
st.caption(f"入力期間: {dates[0].strftime('%Y/%m/%d')} 〜 {dates[-1].strftime('%Y/%m/%d')}")

# ── ウィジェット初期化 ────────────────────────────────────────
existing = st.session_state.requests_df.copy()
_dates_key = f"{dates[0]}_{dates[-1]}"

if (st.session_state.get("_sp_prev_sid") != selected_sid
        or st.session_state.get("_sp_prev_dates") != _dates_key):
    staff_reqs = existing[existing["staff_id"] == selected_sid]
    existing_prefs = {
        row["date"]: _code_to_option(row["shift"], row["is_fixed"])
        for _, row in staff_reqs.iterrows()
    }
    for d in dates:
        wkey = f"req_cal_w_{selected_sid}_{d}"
        short_val = _FULL_TO_SHORT.get(existing_prefs.get(d, "（なし）"), "－")
        if wkey in st.session_state:
            del st.session_state[wkey]
        st.session_state[wkey] = short_val
    st.session_state["_sp_prev_sid"]   = selected_sid
    st.session_state["_sp_prev_dates"] = _dates_key

# ── 凡例 ─────────────────────────────────────────────────────
legend_html = "　".join(
    f'<span style="background:{_BADGE_COLOR[s][0]};color:{_BADGE_COLOR[s][1]};'
    f'border-radius:3px;padding:1px 6px;font-size:12px;font-weight:bold">{s}</span>'
    for s in SHORT_OPTIONS[1:]
)
st.markdown(legend_html, unsafe_allow_html=True)
st.markdown("<div style='margin:8px 0'></div>", unsafe_allow_html=True)

# ── カレンダー + 一覧 ─────────────────────────────────────────
jp_hols = holidays_lib.Japan(years={d.year for d in dates})
schedule_dates_set = set(dates)
months = list(dict.fromkeys((d.year, d.month) for d in dates))

col_cal, col_summary = st.columns([3, 2])

with col_cal:
    for ym in months:
        _render_month_calendar(ym[0], ym[1], schedule_dates_set, selected_sid, jp_hols)

# ウィジェットキーから new_rows を構築
new_rows = []
for d in dates:
    wkey = f"req_cal_w_{selected_sid}_{d}"
    short = st.session_state.get(wkey, "－")
    if short != "－":
        full = _SHORT_TO_FULL.get(short, "（なし）")
        if full in REQUEST_TO_SHIFT:
            shift_code, is_fixed = REQUEST_TO_SHIFT[full]
            new_rows.append({
                "staff_id": selected_sid,
                "date":     d,
                "shift":    shift_code,
                "is_fixed": is_fixed,
            })

# requests_df を更新（他スタッフ・期間外レコードは保持）
dates_set = set(dates)
other = existing[existing["staff_id"] != selected_sid]
selected_outside = existing[
    (existing["staff_id"] == selected_sid) & ~existing["date"].isin(dates_set)
]
merged = pd.concat(
    [other, selected_outside,
     pd.DataFrame(new_rows, columns=["staff_id", "date", "shift", "is_fixed"])]
    if new_rows else [other, selected_outside],
    ignore_index=True,
)
if not merged.reset_index(drop=True).equals(
        st.session_state.requests_df.reset_index(drop=True)):
    st.session_state.requests_df = merged

with col_summary:
    st.markdown(
        f"<div style='font-size:15px;font-weight:bold;color:#333;"
        f"margin-bottom:6px'>👤 {selected_name}</div>",
        unsafe_allow_html=True,
    )
    _render_staff_summary(selected_name, selected_sid, new_rows, jp_hols)

# ── 保存ボタン ───────────────────────────────────────────────
st.divider()
_s1, _s2 = st.columns([1, 4])
with _s1:
    if st.button("💾 希望を保存", type="primary", use_container_width=True, key="staff_save"):
        save_requests(st.session_state.requests_df)
        st.success("✅ 希望を保存しました。")
with _s2:
    st.caption("保存すると担当師長の画面に反映されます。")
