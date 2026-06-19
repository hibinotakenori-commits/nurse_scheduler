"""スタッフ向け勤務希望入力ページ（モバイル最適化・管理機能なし）。"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import holidays as holidays_lib
import pandas as pd
import streamlit as st

from utils.settings import load_settings, staff_df_from_settings, load_requests, save_requests
from utils.time_utils import schedule_dates, REQUEST_TO_SHIFT
from ui.request_calendar import (
    _code_to_option,
    _FULL_TO_SHORT,
    _SHORT_TO_FULL,
    _BADGE_COLOR,
    SHORT_OPTIONS,
)

# set_page_config は app.py 側で呼ばれるため省略
# （?page=staff 経由で実行されるモジュールとして使用）

# ── UI完全隔離CSS ────────────────────────────────────────────
st.markdown("""
<style>
section[data-testid="stSidebar"]       { display: none !important; }
button[data-testid="collapsedControl"] { display: none !important; }
[data-testid="stSidebarNav"]           { display: none !important; }
#MainMenu                              { display: none !important; }
footer                                 { display: none !important; }
header[data-testid="stHeader"]         { display: none !important; }

/* モバイル向けコンパクト化 */
.block-container { padding: 0.6rem 0.8rem 2rem !important; max-width: 600px !important; }
div[data-testid="stSelectbox"] label { display: none !important; }
/* 列間の余白を詰める（内部レイアウトには干渉しない） */
div[data-testid="stHorizontalBlock"] { gap: 6px !important; }
div[data-testid="stColumn"] { padding: 0 !important; min-width: 0 !important; }
</style>
""", unsafe_allow_html=True)

WEEKDAY_NAMES = "月火水木金土日"
_HOL_COLOR  = "#d32f2f"
_SAT_COLOR  = "#1565c0"
_NORM_COLOR = "#212121"

# ── 初期化 ───────────────────────────────────────────────────
def _auto_target_period():
    today = datetime.date.today()
    if today.day > 10:
        month = today.month % 12 + 1
        year  = today.year + (1 if today.month == 12 else 0)
    else:
        year, month = today.year, today.month
    return year, month


def _init():
    # URL クエリから病棟を取得（例: ?page=staff&ward=3A）
    _ward = st.query_params.get("ward", "3A")

    # 病棟が切り替わった場合はリロード
    if st.session_state.get("_staff_ward") != _ward:
        for k in list(st.session_state.keys()):
            del st.session_state[k]

    if "_staff_page_loaded" not in st.session_state:
        _s = load_settings(ward=_ward)
        st.session_state._staff_page_loaded = True
        st.session_state._staff_ward  = _ward
        st.session_state.staff_df     = staff_df_from_settings(_s, ward=_ward)
        st.session_state.requests_df  = load_requests(ward=_ward)
        _auto_year, _auto_month = _auto_target_period()
        st.session_state["_staff_year"]  = _auto_year
        st.session_state["_staff_month"] = _auto_month
    if "requests_df" not in st.session_state:
        _ward = st.session_state.get("_staff_ward", "3A")
        st.session_state.requests_df = load_requests(ward=_ward)

_init()
_ward = st.session_state.get("_staff_ward", "3A")

staff_df = st.session_state.staff_df

# ── ヘッダー ─────────────────────────────────────────────────
st.markdown(f"""
<div style='background:#1565c0;color:white;padding:14px 18px;
            border-radius:8px;margin-bottom:16px'>
  <div style='font-size:20px;font-weight:bold'>📅 勤務希望入力</div>
  <div style='font-size:12px;opacity:0.85;margin-top:2px'>{_ward}病棟</div>
</div>
""", unsafe_allow_html=True)

# ── 名前選択 ─────────────────────────────────────────────────
_names = staff_df.sort_values("order")["name"].tolist()
selected_name = st.selectbox(
    "👤 あなたの名前",
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
legend_parts = []
for s in SHORT_OPTIONS[1:]:
    bg, fg = _BADGE_COLOR[s]
    legend_parts.append(
        f'<span style="background:{bg};color:{fg};border-radius:4px;'
        f'padding:3px 7px;font-size:12px;font-weight:bold;'
        f'display:inline-block;margin:2px 2px">{s}</span>'
    )
st.markdown(
    f"<div style='line-height:2'>" + "".join(legend_parts) + "</div>",
    unsafe_allow_html=True,
)
st.markdown("<div style='margin:8px 0'></div>", unsafe_allow_html=True)

# ── カレンダー（縦リスト形式） ───────────────────────────────
jp_hols = holidays_lib.Japan(years={d.year for d in dates})
months = list(dict.fromkeys((d.year, d.month) for d in dates))
schedule_dates_set = set(dates)

for ym in months:
    yr, mo = ym
    st.markdown(
        f"<div style='font-size:16px;font-weight:bold;margin:12px 0 6px;"
        f"padding:8px 14px;background:#1565c0;color:white;border-radius:8px'>"
        f"📅 {yr}年{mo}月</div>",
        unsafe_allow_html=True,
    )
    month_dates = [d for d in dates if d.year == yr and d.month == mo]
    for d in month_dates:
        wkey = f"req_cal_w_{selected_sid}_{d}"
        chosen = st.session_state.get(wkey, "－")

        # 曜日・背景色
        wd = WEEKDAY_NAMES[d.weekday()]
        is_hol = d.weekday() == 6 or d in jp_hols
        is_sat = d.weekday() == 5

        if is_hol:
            row_bg   = "#fff5f5"
            day_color = "#c62828"
            wd_color  = "#c62828"
        elif is_sat:
            row_bg   = "#f0f4ff"
            day_color = "#1565c0"
            wd_color  = "#1565c0"
        else:
            row_bg   = "#ffffff"
            day_color = "#212121"
            wd_color  = "#555555"

        # 選択済みバッジ
        if chosen != "－" and chosen in _BADGE_COLOR:
            bg, fg = _BADGE_COLOR[chosen]
            badge_html = (
                f'<span style="background:{bg};color:{fg};border-radius:4px;'
                f'padding:3px 10px;font-size:13px;font-weight:bold">{chosen}</span>'
            )
        else:
            badge_html = '<span style="color:#bbb;font-size:13px">―</span>'

        border_color = '#c62828' if is_hol else ('#1565c0' if is_sat else '#e0e0e0')
        col_date, col_sel = st.columns([2, 3])
        with col_date:
            st.markdown(
                f"<div style='background:{row_bg};border-radius:4px;"
                f"padding:6px 8px 4px;border-left:4px solid {border_color};height:100%'>"
                f"<div style='color:{day_color};font-size:15px;font-weight:bold;line-height:1.2'>"
                f"{mo}/{d.day}"
                f"<span style='color:{wd_color};font-size:12px;margin-left:4px'>({wd})</span>"
                f"</div>"
                f"<div style='margin-top:2px'>{badge_html}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        with col_sel:
            st.selectbox(
                f"_{d}",
                SHORT_OPTIONS,
                index=SHORT_OPTIONS.index(chosen) if chosen in SHORT_OPTIONS else 0,
                key=wkey,
                label_visibility="collapsed",
            )

st.markdown("<div style='margin:12px 0'></div>", unsafe_allow_html=True)

# ── new_rows 構築 ────────────────────────────────────────────
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

# ── 入力済み一覧（折りたたみ） ──────────────────────────────
if new_rows:
    fixed_cnt = sum(1 for r in new_rows if r["is_fixed"])
    soft_cnt  = len(new_rows) - fixed_cnt
    with st.expander(
        f"📋 入力済み {len(new_rows)}件（🔒確定{fixed_cnt} / 💭希望{soft_cnt}）",
        expanded=False,
    ):
        for row in sorted(new_rows, key=lambda r: r["date"]):
            d  = row["date"]
            wd = WEEKDAY_NAMES[d.weekday()]
            is_hol = d.weekday() == 6 or d in jp_hols
            is_sat = d.weekday() == 5
            dc = _HOL_COLOR if is_hol else (_SAT_COLOR if is_sat else _NORM_COLOR)
            short = _FULL_TO_SHORT.get(_code_to_option(row["shift"], row["is_fixed"]), "?")
            if short in _BADGE_COLOR:
                bg, fg = _BADGE_COLOR[short]
                b = (f'<span style="background:{bg};color:{fg};border-radius:4px;'
                     f'padding:2px 8px;font-size:13px;font-weight:bold">{short}</span>')
            else:
                b = short
            kind = "🔒 確定" if row["is_fixed"] else "💭 希望"
            st.markdown(
                f"<div style='padding:4px 0;border-bottom:1px solid #eee'>"
                f"<span style='color:{dc};font-weight:bold'>{d.month}/{d.day}（{wd}）</span>"
                f"　{b}　<span style='font-size:12px;color:#666'>{kind}</span></div>",
                unsafe_allow_html=True,
            )

# ── 保存ボタン ───────────────────────────────────────────────
st.divider()
if st.button("💾 希望を保存する", type="primary", use_container_width=True, key="staff_save"):
    save_requests(st.session_state.requests_df, ward=_ward)
    st.success("✅ 希望を保存しました。担当師長の画面に反映されます。")
