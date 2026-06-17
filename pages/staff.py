"""スタッフ向け勤務希望入力ページ（Streamlit マルチページ）。

URL: /staff
"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st

from utils.settings import load_settings, staff_df_from_settings, load_requests, save_requests
from utils.time_utils import schedule_dates
from ui.request_calendar import render_request_calendar

st.set_page_config(
    page_title="勤務希望入力 - 3A病棟",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# サイドバーのページナビゲーションを非表示
st.markdown("""
<style>
[data-testid="stSidebarNav"] { display: none; }
[data-testid="collapsedControl"] { display: none; }
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
            st.session_state["target_year"]  = _s["target_year"]
        if _s.get("target_month"):
            st.session_state["target_month"] = _s["target_month"]
    if "requests_df" not in st.session_state:
        st.session_state.requests_df = load_requests()

_init()

# ── ヘッダー ─────────────────────────────────────────────────
st.title("📅 勤務希望入力")
st.caption("3A病棟　希望を入力して「保存」ボタンを押してください。")
st.divider()

# ── 対象期間 ─────────────────────────────────────────────────
now = datetime.date.today()
_default_year  = st.session_state.get("target_year",  now.year)
_default_month = st.session_state.get("target_month", now.month)

_year_options = list(range(now.year - 1, now.year + 3))
_col1, _col2, _col3 = st.columns([1, 1, 4])
with _col1:
    _year = st.selectbox(
        "年",
        _year_options,
        index=_year_options.index(_default_year) if _default_year in _year_options else 1,
        key="target_year",
    )
with _col2:
    _month = st.selectbox("月", range(1, 13), index=_default_month - 1, key="target_month")

_dates = schedule_dates(_year, _month)
st.caption(f"入力期間: {_dates[0].strftime('%Y/%m/%d')} 〜 {_dates[-1].strftime('%Y/%m/%d')}")
st.divider()

# ── 希望カレンダー ───────────────────────────────────────────
render_request_calendar(st.session_state.staff_df, _dates)

st.divider()

# ── 保存ボタン ───────────────────────────────────────────────
_c1, _c2 = st.columns([1, 4])
with _c1:
    if st.button("💾 希望を保存", type="primary", use_container_width=True, key="staff_save"):
        save_requests(st.session_state.requests_df)
        st.success("✅ 希望を保存しました。")
with _c2:
    st.caption("保存すると担当師長の画面に反映されます。")
