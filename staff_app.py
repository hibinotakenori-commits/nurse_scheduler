"""3A病棟 スタッフ向け勤務希望入力アプリ。

起動方法:
    streamlit run staff_app.py --server.port 8502
"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import streamlit as st

from utils.settings import load_settings, staff_df_from_settings, load_requests, save_requests
from utils.time_utils import schedule_dates
from ui.request_calendar import render_request_calendar

st.set_page_config(
    page_title="勤務希望入力 - 3A病棟",
    page_icon="📅",
    layout="wide",
)

# ── 初期化 ───────────────────────────────────────────────────
def init():
    if "_staff_app_loaded" not in st.session_state:
        _s = load_settings()
        st.session_state._staff_app_loaded = True
        st.session_state.staff_df     = staff_df_from_settings(_s)
        st.session_state.requests_df  = load_requests()
        if _s.get("target_year"):
            st.session_state["target_year"]  = _s["target_year"]
        if _s.get("target_month"):
            st.session_state["target_month"] = _s["target_month"]
    if "requests_df" not in st.session_state:
        st.session_state.requests_df = load_requests()

init()

# ── ヘッダー ─────────────────────────────────────────────────
st.title("📅 勤務希望入力")
st.caption("3A病棟 勤務希望入力フォームです。希望を入力して「保存」ボタンを押してください。")
st.divider()

# ── 対象期間の表示 ───────────────────────────────────────────
now = datetime.date.today()
_default_year  = st.session_state.get("target_year",  now.year)
_default_month = st.session_state.get("target_month", now.month)

col_period, col_space = st.columns([2, 4])
with col_period:
    _year  = st.selectbox("対象年", range(now.year - 1, now.year + 3),
                          index=list(range(now.year - 1, now.year + 3)).index(_default_year)
                                if _default_year in range(now.year - 1, now.year + 3) else 1,
                          key="target_year")
    _month = st.selectbox("対象月", range(1, 13),
                          index=_default_month - 1,
                          key="target_month")

dates = schedule_dates(_year, _month)
st.caption(f"入力期間: {dates[0].strftime('%Y/%m/%d')}（月）〜 {dates[-1].strftime('%Y/%m/%d')}（日）")

st.divider()

# ── 希望カレンダー ───────────────────────────────────────────
render_request_calendar(st.session_state.staff_df, dates)

st.divider()

# ── 保存ボタン ───────────────────────────────────────────────
col_save, col_info = st.columns([1, 4])
with col_save:
    if st.button("💾 希望を保存", type="primary", use_container_width=True, key="staff_save_btn"):
        save_requests(st.session_state.requests_df)
        st.success("✅ 希望を保存しました。担当者に反映されます。")
with col_info:
    st.caption("保存後は担当師長に伝えてください。")
