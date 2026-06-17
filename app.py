"""3A病棟 勤務表作成アプリ - メインエントリポイント"""

# ── ソルバーが常に守るハード制約（システム固定・削除不可） ──────────────
# システム制約：テキスト → デフォルト優先度（1〜5）
SYSTEM_CONSTRAINTS = [
    {"text": "夜勤明け（N2）の翌日はN1か公休のみ",             "default_priority": 5},
    {"text": "遅出（L）の翌日はN1か公休のみ",                  "default_priority": 5},
    {"text": "2連続夜勤（N1→N2→N1→N2）の後の2日間は公休",    "default_priority": 3},
]
PRIORITY_LABELS = {1: "1（低）", 2: "2", 3: "3", 4: "4", 5: "5（高）"}
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import streamlit as st

from scheduler.solver import solve, analyze_infeasibility
from scheduler.validator import validate
from ui.schedule_grid import render_grid, render_day_summary
from ui.summary import render_summary
from utils.excel_export import export_excel, export_csv
from utils.staff_data import load_staff
from utils.settings import (load_settings, save_settings, staff_df_from_settings,
                             DEFAULT_SOFT_WEIGHTS, DAYCARE_TYPE_OPTIONS, DAYCARE_TYPE_LABELS,
                             load_requests, save_requests)
from streamlit_sortables import sort_items
from ui.request_calendar import render_request_calendar
from utils.time_utils import schedule_dates
from utils.schedule_store import save_schedule, list_saved_schedules, get_prev_boundary
from utils.schedule_import import parse_excel, parse_image, schedule_dict_to_df

st.set_page_config(
    page_title="3A病棟 勤務表作成",
    page_icon="🏥",
    layout="wide",
)

# ── セッションステートの初期化 ──────────────────────────────

def init_state():
    # settings.json から保存済み設定を読み込む（初回のみ）
    if "_settings_loaded" not in st.session_state:
        _s = load_settings()
        st.session_state._settings_loaded = True
        st.session_state.staff_df      = staff_df_from_settings(_s)
        st.session_state.requirements  = _s["requirements"]
        st.session_state.soft_weights  = _s.get("soft_weights", dict(DEFAULT_SOFT_WEIGHTS))
        st.session_state.hospital_holidays = _s["hospital_holidays"]
        st.session_state.daycare_closed    = _s["daycare_closed"]
        st.session_state.nightcare_open    = _s.get("nightcare_open", [])
        st.session_state.gakudo_open         = _s.get("gakudo_open", [])
        st.session_state.system_constraint_priorities = _s.get(
            "system_constraint_priorities",
            {c["text"]: c["default_priority"] for c in SYSTEM_CONSTRAINTS},
        )
        st.session_state.user_constraints    = _s.get("user_constraints", [])
        # 保存済みの年月があればセッションステートに復元
        if _s.get("target_year") is not None:
            st.session_state["target_year"] = _s["target_year"]
        if _s.get("target_month") is not None:
            st.session_state["target_month"] = _s["target_month"]

    if "requests_df" not in st.session_state:
        st.session_state.requests_df = load_requests()
    if "schedule_df" not in st.session_state:
        st.session_state.schedule_df = None
    if "edited_schedule_df" not in st.session_state:
        st.session_state.edited_schedule_df = None
    if "solver_status" not in st.session_state:
        st.session_state.solver_status = None
    if "solver_warnings" not in st.session_state:
        st.session_state.solver_warnings = []
    if "infeasibility_reasons" not in st.session_state:
        st.session_state.infeasibility_reasons = []
    if "violations" not in st.session_state:
        st.session_state.violations = []
    if "soft_weights" not in st.session_state:
        st.session_state.soft_weights = dict(DEFAULT_SOFT_WEIGHTS)
    if "nightcare_open" not in st.session_state:
        st.session_state.nightcare_open = []
    if "gakudo_open" not in st.session_state:
        st.session_state.gakudo_open = []
    if "system_constraint_priorities" not in st.session_state:
        st.session_state.system_constraint_priorities = {
            c["text"]: c["default_priority"] for c in SYSTEM_CONSTRAINTS
        }
    if "user_constraints" not in st.session_state:
        st.session_state.user_constraints = []

    # 必要人数の number_input 専用キー（保存済み値 or デフォルト）
    _req = st.session_state.requirements
    _req_defaults = {
        "req_d_weekday":     _req.get("D", {}).get("weekday",     8),
        "req_d_weekday_max": _req.get("D", {}).get("weekday_max", 20),
        "req_d_holiday":     _req.get("D", {}).get("holiday",     6),
        "req_d_holiday_max": _req.get("D", {}).get("holiday_max", 20),
        "req_l_weekday":     _req.get("L", {}).get("weekday",     1),
        "req_l_holiday":     _req.get("L", {}).get("holiday",     1),
        "req_n_base":        _req.get("N", {}).get("base",        4),
        "req_n_max":         _req.get("N", {}).get("max",         5),
        "req_n_fy_max":      _req.get("N", {}).get("first_year_max", 1),
    }
    for k, v in _req_defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()


def _apply_night_pairing(
    prev: pd.DataFrame,
    edited: pd.DataFrame,
    dates: list,
) -> pd.DataFrame:
    """
    手動編集後にヤ1/ヤ2のペアを自動補完する。

    ルール:
    - ヤ1 を新たにセット → 翌日を自動で ヤ2 に（翌日が期間内の場合）
    - ヤ1 を解除       → 翌日が ヤ2 だったなら 休 に戻す
    - ヤ2 を新たにセット → 前日が ヤ1 でなければ自動で ヤ1 に
    """
    result = edited.copy()

    for staff_id in edited.index:
        for i, d in enumerate(dates):
            prev_val = prev.at[staff_id, d] if d in prev.columns else "O"
            new_val  = edited.at[staff_id, d]  # 元の編集値（result でなく edited 参照）

            # ── ヤ1 の変化 ──
            if new_val == "N1" and prev_val != "N1":
                # ヤ1 を新規セット → 翌日を ヤ2 に
                if i + 1 < len(dates):
                    result.at[staff_id, dates[i + 1]] = "N2"

            elif prev_val == "N1" and new_val != "N1":
                # ヤ1 を解除 → 翌日がまだ ヤ2 なら 休 に戻す
                if i + 1 < len(dates) and edited.at[staff_id, dates[i + 1]] == "N2":
                    result.at[staff_id, dates[i + 1]] = "O"

            # ── ヤ2 の変化 ──
            if new_val == "N2" and prev_val != "N2":
                # ヤ2 を新規セット → 前日が ヤ1 でなければ ヤ1 を自動セット
                if i > 0 and edited.at[staff_id, dates[i - 1]] != "N1":
                    result.at[staff_id, dates[i - 1]] = "N1"

    return result


def _last_day(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)).day


# ── サイドバー ─────────────────────────────────────────────

with st.sidebar:
    st.title("🏥 3A病棟 勤務表")
    st.divider()

    now = datetime.date.today()
    if now.day > 20:
        _default_month = now.month % 12 + 1  # 翌月（12月なら1月）
        _default_year_index = 2 if now.month == 12 else 1
    else:
        _default_month = now.month
        _default_year_index = 1
    col1, col2 = st.columns(2)
    with col1:
        target_year = st.selectbox("年", list(range(now.year - 1, now.year + 3)),
                                   index=_default_year_index, key="target_year")
    with col2:
        target_month = st.selectbox("開始月", list(range(1, 13)),
                                    index=_default_month - 1, key="target_month")

    dates = schedule_dates(target_year, target_month)
    end_date = dates[-1]
    st.caption(f"対象期間: {dates[0].strftime('%Y/%m/%d')} 〜 {end_date.strftime('%Y/%m/%d')}")

    st.divider()
    st.subheader("夜勤専任（月次指定）")

    # 有効スタッフのみをスケジューリングに使用（休止中は除外）
    staff_df = st.session_state.staff_df
    if "active" in staff_df.columns:
        staff_df = staff_df[staff_df["active"].fillna(True).astype(bool)]
    night_ok_staff = staff_df[staff_df["night_ok"]]["name"].tolist()
    none_option = ["（なし）"]

    # 前半: 当月21日〜末日
    first_half_end = datetime.date(target_year, target_month,
                                   _last_day(target_year, target_month))
    st.caption(f"前半 ({target_month}/{21}〜{first_half_end.strftime('%m/%d')})")
    ded_first_name = st.selectbox(
        "前半 夜勤専任", none_option + night_ok_staff, key="ded_first"
    )

    # 後半: 翌月1日〜20日
    next_month = target_month % 12 + 1
    next_year = target_year + (1 if target_month == 12 else 0)
    st.caption(f"後半 ({next_month}/1〜{next_month}/20)")
    ded_second_name = st.selectbox(
        "後半 夜勤専任", none_option + night_ok_staff, key="ded_second"
    )

    name_to_sid = {row["name"]: row["id"] for _, row in staff_df.iterrows()}
    ded_first_id  = name_to_sid.get(ded_first_name)
    ded_second_id = name_to_sid.get(ded_second_name)

    st.divider()
    time_limit = st.slider("ソルバー制限時間（秒）", 10, 300, 60, step=10)

    run_solver = st.button("▶ 勤務表を自動作成", type="primary", use_container_width=True)

    st.divider()
    if st.session_state.edited_schedule_df is not None:
        sdf = st.session_state.edited_schedule_df
        viol = st.session_state.violations
        xlsx_bytes = export_excel(sdf, staff_df, dates, viol,
                                   hospital_holidays=st.session_state.hospital_holidays)
        st.download_button(
            "📥 Excel ダウンロード",
            data=xlsx_bytes,
            file_name=f"勤務表_{target_year}{target_month:02d}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

        st.download_button(
            "📥 CSV ダウンロード",
            data=export_csv(sdf, staff_df, dates,
                            hospital_holidays=st.session_state.hospital_holidays),
            file_name=f"勤務表_{target_year}{target_month:02d}.csv",
            mime="text/csv",
            use_container_width=True,
        )


# ── ソルバー実行 ───────────────────────────────────────────

if run_solver:
    st.session_state.infeasibility_reasons = []  # 前回の分析結果をリセット
    _prev_boundary = get_prev_boundary(target_year, target_month)
    _solver_kwargs = dict(
        staff_df=staff_df,
        requests_df=st.session_state.requests_df,
        requirements=st.session_state.requirements,
        year=target_year,
        month=target_month,
        dedicated_first=ded_first_id,
        dedicated_second=ded_second_id,
        daycare_closed_dates=st.session_state.daycare_closed,
        nightcare_open_dates=st.session_state.nightcare_open,
        gakudo_open_dates=st.session_state.gakudo_open,
        hospital_holidays=st.session_state.hospital_holidays,
        time_limit_sec=time_limit,
        soft_weights=st.session_state.soft_weights,
        prev_schedule=_prev_boundary,
    )
    with st.spinner("最適化中...（しばらくお待ちください）"):
        sdf, status, warnings = solve(**_solver_kwargs)
    st.session_state.solver_status = status
    st.session_state.solver_warnings = warnings
    if sdf is not None:
        st.session_state.schedule_df = sdf
        st.session_state.edited_schedule_df = sdf.copy()
        first_year_ids = staff_df[staff_df["years_exp"] == 1]["id"].tolist()
        st.session_state.violations = validate(
            sdf, staff_df, dates, st.session_state.requirements, first_year_ids,
            daycare_closed_dates=st.session_state.daycare_closed,
            hospital_holidays=st.session_state.hospital_holidays,
        )
    else:
        st.session_state.schedule_df = None
        st.session_state.edited_schedule_df = None
        # ── INFEASIBLE 時: 原因を自動分析 ──
        if status == "INFEASIBLE":
            _progress_area = st.empty()
            def _on_progress(msg: str):
                _progress_area.info(f"🔍 {msg}")
            with st.spinner("制約の矛盾原因を分析中...（最大60秒）"):
                reasons = analyze_infeasibility(
                    staff_df=staff_df,
                    requests_df=st.session_state.requests_df,
                    requirements=st.session_state.requirements,
                    year=target_year,
                    month=target_month,
                    dedicated_first=ded_first_id,
                    dedicated_second=ded_second_id,
                    daycare_closed_dates=st.session_state.daycare_closed,
                    nightcare_open_dates=st.session_state.nightcare_open,
                    gakudo_open_dates=st.session_state.gakudo_open,
                    hospital_holidays=st.session_state.hospital_holidays,
                    progress_callback=_on_progress,
                )
            _progress_area.empty()
            st.session_state.infeasibility_reasons = reasons

# ── ステータス表示 ─────────────────────────────────────────

if st.session_state.solver_status:
    status = st.session_state.solver_status
    if status == "OPTIMAL":
        st.success("✅ 最適解が見つかりました")
    elif status == "FEASIBLE":
        st.warning("⚠️ 実行可能解が見つかりました（最適でない可能性があります）")
    elif status == "INFEASIBLE":
        st.error("❌ 制約を満たす勤務表が作成できませんでした。")
        reasons = st.session_state.get("infeasibility_reasons", [])
        if reasons:
            st.markdown("#### 🔍 原因の分析結果")
            for r in reasons:
                sev = r.get("severity", "medium")
                if sev == "high":
                    icon = "🔴"
                elif sev == "medium":
                    icon = "🟡"
                else:
                    icon = "🔵"
                with st.expander(f"{icon} {r['title']}", expanded=(sev == "high")):
                    st.markdown(r["detail"])
    else:
        st.error(f"❌ ソルバーエラー: {status}")

    for w in st.session_state.solver_warnings[:5]:
        st.caption(f"⚠️ {w['type']} ({w.get('date', '')})")

# ── メインエリア ───────────────────────────────────────────

tab_schedule, tab_summary, tab_requests, tab_staff, tab_ward, tab_common = st.tabs(["📋 勤務表", "📊 集計", "📅 希望入力", "👤 スタッフ", "🏨 病棟独自設定", "🏥 院内共通設定"])

with tab_schedule:
    _solver_done = st.session_state.edited_schedule_df is not None

    if not _solver_done:
        # ソルバー未実行：全員「休」の空の票を表示
        sdf = pd.DataFrame(
            "O",
            index=staff_df["id"].tolist(),
            columns=dates,
        )
        viol = []
        st.caption("💡 サイドバーの「▶ 勤務表を自動作成」で生成するか、セルをクリックして直接入力できます。")
    else:
        sdf = st.session_state.edited_schedule_df
        viol = st.session_state.violations

        # 違反バッジ
        if viol:
            st.error(f"⚠️ 制約違反 {len(viol)} 件")
            with st.expander("違反一覧を表示"):
                for v in viol:
                    st.write(f"- [{v.get('date','')}] {v['type']}  {v.get('detail','')}")
        else:
            st.success("制約違反なし ✅　　💡 セルをクリックするとシフトを変更できます。")

    # グリッド（常に表示・常に編集可能）
    edited = render_grid(sdf, staff_df, dates, viol, key_prefix="schedule_editor",
                         hospital_holidays=st.session_state.hospital_holidays)

    if not edited.equals(sdf):
        # ヤ1/ヤ2 ペア自動補完
        edited = _apply_night_pairing(sdf, edited, dates)
        st.session_state.edited_schedule_df = edited
        first_year_ids = staff_df[staff_df["years_exp"] == 1]["id"].tolist()
        st.session_state.violations = validate(
            edited, staff_df, dates, st.session_state.requirements, first_year_ids,
            daycare_closed_dates=st.session_state.daycare_closed,
            hospital_holidays=st.session_state.hospital_holidays,
        )
        st.rerun()

    # 保存・リセット・ダウンロードボタン（編集データがある場合のみ）
    if st.session_state.edited_schedule_df is not None:
        col_save, col_reset, col_xlsx, col_csv, col_space = st.columns([2, 2, 1, 1, 2])
        with col_save:
            if st.button("💾 勤務表を保存", type="primary", use_container_width=True):
                save_schedule(st.session_state.edited_schedule_df, target_year, target_month)
                st.success(f"✅ {target_year}/{target_month:02d}期の勤務表を保存しました")
        with col_reset:
            if st.session_state.schedule_df is not None:
                if st.button("↩ ソルバー結果に戻す", use_container_width=True):
                    st.session_state.edited_schedule_df = st.session_state.schedule_df.copy()
                    st.session_state.violations = validate(
                        st.session_state.schedule_df, staff_df, dates,
                        st.session_state.requirements,
                        staff_df[staff_df["years_exp"] == 1]["id"].tolist(),
                        daycare_closed_dates=st.session_state.daycare_closed,
                        hospital_holidays=st.session_state.hospital_holidays,
                    )
                    st.rerun()
        with col_xlsx:
            _xlsx = export_excel(edited, staff_df, dates,
                                 st.session_state.violations,
                                 hospital_holidays=st.session_state.hospital_holidays)
            st.download_button(
                "📊",
                data=_xlsx,
                file_name=f"勤務表_{target_year}{target_month:02d}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                help="Excel ダウンロード",
                use_container_width=True,
            )
        with col_csv:
            st.download_button(
                "📄",
                data=export_csv(edited, staff_df, dates,
                                hospital_holidays=st.session_state.hospital_holidays),
                file_name=f"勤務表_{target_year}{target_month:02d}.csv",
                mime="text/csv",
                help="CSV ダウンロード",
                use_container_width=True,
            )

        # 保存済み一覧
        _saved_list = list_saved_schedules()
        if _saved_list:
            with st.expander(f"📁 保存済み勤務表（{len(_saved_list)}件）"):
                for _sv in _saved_list:
                    _at = _sv["saved_at"][:16].replace("T", " ") if _sv["saved_at"] else "―"
                    st.caption(f"・{_sv['year']}/{_sv['month']:02d}期　保存日時: {_at}")

    st.divider()
    st.subheader("日別集計")
    render_day_summary(edited, dates, st.session_state.requirements,
                       hospital_holidays=st.session_state.hospital_holidays)

    # ── 前月勤務表の取り込み ──────────────────────────────────
    st.divider()
    with st.expander("📥 前月勤務表の取り込み（月またぎ制約に使用）"):
        st.caption("前月末のシフトを読み込んで、連続勤務・夜勤ペアなどの月またぎ制約を正しく守ります。")

        # 取り込み対象の年月を選択（デフォルトは現在の対象月の前月）
        _imp_prev_month = target_month - 1 if target_month > 1 else 12
        _imp_prev_year  = target_year if target_month > 1 else target_year - 1
        _imp_col1, _imp_col2 = st.columns(2)
        with _imp_col1:
            _imp_year  = st.selectbox("取り込む勤務表の年", range(target_year - 2, target_year + 1),
                                      index=2, key="imp_year")
        with _imp_col2:
            _imp_month = st.selectbox("取り込む勤務表の月", range(1, 13),
                                      index=_imp_prev_month - 1, key="imp_month")

        _imp_file = st.file_uploader(
            "Excelまたは画像ファイルをアップロード",
            type=["xlsx", "xls", "png", "jpg", "jpeg"],
            key="imp_file_uploader",
        )

        if _imp_file is not None:
            _imp_bytes = _imp_file.read()
            _imp_name  = _imp_file.name.lower()
            _imp_schedule = None
            _imp_warns: list = []

            with st.spinner("解析中..."):
                if _imp_name.endswith((".xlsx", ".xls")):
                    _imp_schedule, _imp_warns = parse_excel(
                        _imp_bytes, staff_df, _imp_year, _imp_month
                    )
                else:
                    _mime = "image/png" if _imp_name.endswith(".png") else "image/jpeg"
                    _imp_schedule, _imp_warns = parse_image(
                        _imp_bytes, _mime, staff_df, _imp_year, _imp_month
                    )

            for _w in _imp_warns:
                st.warning(_w)

            if _imp_schedule:
                _imp_df = schedule_dict_to_df(_imp_schedule, staff_df)
                st.success(f"✅ {len(_imp_schedule)} 名分のシフトを読み込みました（{len(_imp_df.columns)} 日間）")
                with st.expander("取り込み内容を確認"):
                    st.dataframe(_imp_df, use_container_width=True)

                if st.button("💾 前月勤務表として保存する", type="primary", key="imp_save_btn"):
                    # staff_id をインデックスにした DataFrame を save_schedule に渡す
                    _id_to_name = dict(zip(staff_df["name"], staff_df["id"]))
                    _imp_df_by_id = _imp_df.copy()
                    _imp_df_by_id.index = [
                        _imp_schedule_key
                        for _imp_schedule_key in _imp_schedule.keys()
                    ]
                    # schedule_dict → DataFrame（index=staff_id）
                    import datetime as _dt
                    _all_dates = sorted({d for v in _imp_schedule.values() for d in v.keys()})
                    _sdf_for_save = pd.DataFrame(
                        index=sorted(_imp_schedule.keys()),
                        columns=_all_dates,
                        dtype=object,
                    )
                    for _sid, _shifts in _imp_schedule.items():
                        for _d, _s in _shifts.items():
                            _sdf_for_save.at[_sid, _d] = _s
                    _sdf_for_save = _sdf_for_save.fillna("O")
                    save_schedule(_sdf_for_save, _imp_year, _imp_month)
                    st.success(f"✅ {_imp_year}/{_imp_month:02d}期の勤務表を保存しました。次回の勤務表作成時に月またぎ制約として自動で使用されます。")
            else:
                st.error("シフトデータを取得できませんでした。")

with tab_summary:
    if st.session_state.edited_schedule_df is None:
        st.info("勤務表を生成してから確認してください。")
    else:
        render_summary(st.session_state.edited_schedule_df, staff_df, dates)

with tab_requests:
    # ── スタッフ用QRコード ──
    import socket, io as _io
    import qrcode
    def _local_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"
    _ip = _local_ip()
    _staff_url = f"http://{_ip}:8502"

    with st.expander("📱 スタッフ向け希望入力QRコード", expanded=True):
        _qr_col, _info_col = st.columns([1, 2])
        with _qr_col:
            _qr = qrcode.make(_staff_url)
            _buf = _io.BytesIO()
            _qr.save(_buf, format="PNG")
            st.image(_buf.getvalue(), width=180)
        with _info_col:
            st.markdown(f"**スタッフ向けURL**")
            st.code(_staff_url)
            st.caption(
                "スタッフはスマホのカメラでQRコードを読み取るか、"
                "上のURLをブラウザで開いて希望を入力できます。\n\n"
                "**起動方法**（ターミナルで）:\n"
                "```\nbash start.sh\n```\n"
                "または\n"
                "```\nstreamlit run staff_app.py --server.port 8502 --server.address 0.0.0.0\n```"
            )
    st.divider()
    render_request_calendar(staff_df, dates)
    st.divider()
    col_save_req, col_save_info = st.columns([1, 4])
    with col_save_req:
        if st.button("💾 希望を保存", type="primary", key="save_requests_btn"):
            save_requests(st.session_state.requests_df)
            # 年月も同時に保存して次回起動時に同じ期間を表示する
            save_settings(
                requirements=st.session_state.requirements,
                soft_weights=st.session_state.soft_weights,
                hospital_holidays=st.session_state.hospital_holidays,
                daycare_closed=st.session_state.daycare_closed,
                nightcare_open=st.session_state.nightcare_open,
                gakudo_open=st.session_state.gakudo_open,
                system_constraint_priorities=st.session_state.system_constraint_priorities,
                user_constraints=st.session_state.user_constraints,
                staff_df=st.session_state.staff_df,
                target_year=st.session_state.get("target_year"),
                target_month=st.session_state.get("target_month"),
            )
            st.success(f"✅ {len(st.session_state.requests_df)} 件の希望を保存しました。次回起動時も反映されます。")

with tab_staff:
    # ── ① スタッフ一覧（概要表） ──────────────────────────────
    st.subheader("スタッフ一覧（概要）")
    st.caption("「有効」のチェックを外すと一旦休止（スケジュール対象外）になります。夜勤可・日勤L・夜勤L・夜勤下限・夜勤上限 も直接変更できます。")
    _summary_rows = []
    for _, _r in st.session_state.staff_df.sort_values("order").iterrows():
        _dc = str(_r.get("daycare_type", "none"))
        _nc_req = bool(_r.get("nightcare_required", False))
        if _dc == "night":
            _dc_label = "🌙夜間（必須）" if _nc_req else "🌙夜間（任意）"
        else:
            _dc_label = {"none": "－", "day": "🌤日中"}.get(_dc, _dc)
        _gakudo = bool(_r.get("gakudo", False))
        _gakudo_req = bool(_r.get("gakudo_required", False))
        _gakudo_label = ("🏫（必須）" if _gakudo_req else "🏫（任意）") if _gakudo else "－"
        _is_active = bool(_r.get("active", True))
        _summary_rows.append({
            "有効":        _is_active,
            "氏名":        _r["name"],
            "経験年数":    int(_r["years_exp"]),
            "夜勤可":      bool(_r.get("night_ok", False)),
            "日勤L":       bool(_r.get("day_leader_ok", False)),
            "夜勤L":       bool(_r.get("night_leader_ok", False)),
            # Safari の NumberColumn 非対応を回避するため文字列で保持
            "夜勤下限":    str(int(_r.get("night_count_min", 0))),
            "夜勤上限":    str(int(_r.get("night_count_max", 0))),
            "目標時間(h)": float(_r.get("target_hours", 170.0)),
            "保育園":      _dc_label,
            "夜間学童":    _gakudo_label,
        })
    _summary_df = pd.DataFrame(_summary_rows)
    _edited_summary = st.data_editor(
        _summary_df,
        column_config={
            "有効":        st.column_config.CheckboxColumn("有効", help="チェックを外すと一旦休止（スケジュール対象外）"),
            "氏名":        st.column_config.TextColumn(disabled=True),
            "経験年数":    st.column_config.NumberColumn(disabled=True),
            "夜勤可":      st.column_config.CheckboxColumn("夜勤可"),
            "日勤L":       st.column_config.CheckboxColumn("日勤L"),
            "夜勤L":       st.column_config.CheckboxColumn("夜勤L"),
            "夜勤下限":    st.column_config.TextColumn("夜勤下限", help="数字を入力してEnter"),
            "夜勤上限":    st.column_config.TextColumn("夜勤上限", help="数字を入力してEnter"),
            "目標時間(h)": st.column_config.NumberColumn(disabled=True),
            "保育園":      st.column_config.TextColumn(disabled=True),
            "夜間学童":    st.column_config.TextColumn(disabled=True),
        },
        hide_index=True,
        use_container_width=True,
        height=min(60 + 35 * len(_summary_df), 700),
        key="staff_summary_editor",
    )

    # 変更を検出 → staff_df に即反映・保存
    def _summary_changed(edited: pd.DataFrame, original: pd.DataFrame) -> bool:
        for col in ["有効", "夜勤可", "日勤L", "夜勤L"]:
            if not (edited[col].astype(bool) == original[col].astype(bool)).all():
                return True
        for col in ["夜勤下限", "夜勤上限"]:
            try:
                e = edited[col].fillna("0").apply(lambda v: int(str(v).strip() or "0"))
                o = original[col].fillna("0").apply(lambda v: int(str(v).strip() or "0"))
                if not (e == o).all():
                    return True
            except (ValueError, TypeError):
                return True
        return False

    if _summary_changed(_edited_summary, _summary_df):
        for _, _erow in _edited_summary.iterrows():
            _mask = st.session_state.staff_df["name"] == _erow["氏名"]
            st.session_state.staff_df.loc[_mask, "active"]          = bool(_erow["有効"])
            st.session_state.staff_df.loc[_mask, "night_ok"]        = bool(_erow["夜勤可"])
            st.session_state.staff_df.loc[_mask, "day_leader_ok"]   = bool(_erow["日勤L"])
            st.session_state.staff_df.loc[_mask, "night_leader_ok"] = bool(_erow["夜勤L"])
            try:
                _min = int(str(_erow["夜勤下限"]).strip() or "0")
                _max = int(str(_erow["夜勤上限"]).strip() or "0")
            except (ValueError, TypeError):
                _min = int(st.session_state.staff_df.loc[_mask, "night_count_min"].values[0])
                _max = int(st.session_state.staff_df.loc[_mask, "night_count_max"].values[0])
            st.session_state.staff_df.loc[_mask, "night_count_min"] = _min
            st.session_state.staff_df.loc[_mask, "night_count_max"] = _max
        save_settings(
            requirements=st.session_state.requirements,
            soft_weights=st.session_state.soft_weights,
            hospital_holidays=st.session_state.hospital_holidays,
            daycare_closed=st.session_state.daycare_closed,
            nightcare_open=st.session_state.nightcare_open,
            gakudo_open=st.session_state.gakudo_open,
            system_constraint_priorities=st.session_state.system_constraint_priorities,
            user_constraints=st.session_state.user_constraints,
            staff_df=st.session_state.staff_df,
            target_year=st.session_state.get("target_year"),
            target_month=st.session_state.get("target_month"),
        )
        st.session_state.pop("staff_summary_editor", None)
        st.rerun()

    st.divider()

    # ── ② 個人詳細：設定 ＋ シフト希望一覧 ─────────────────────
    st.subheader("個人詳細")
    _staff_names = st.session_state.staff_df.sort_values("order")["name"].tolist()
    _sel_name = st.selectbox("スタッフを選択", _staff_names, key="staff_detail_select")
    _sel_row_mask = st.session_state.staff_df["name"] == _sel_name
    _sel_idx = st.session_state.staff_df.index[_sel_row_mask][0]
    _sel = st.session_state.staff_df.loc[_sel_idx]

    col_settings, col_requests = st.columns([1, 1], gap="large")

    with col_settings:
        st.markdown("**設定**")
        with st.form(key=f"staff_form_{_sel_name}"):
            _f_name = st.text_input("氏名", value=str(_sel["name"]))
            _f_years = st.number_input("経験年数", min_value=0, step=1,
                                       value=int(_sel["years_exp"]))
            _f_night_ok = st.checkbox("夜勤可", value=bool(_sel.get("night_ok", False)))
            _f_day_l    = st.checkbox("日勤リーダー可", value=bool(_sel.get("day_leader_ok", False)))
            _f_night_l  = st.checkbox("夜勤リーダー可", value=bool(_sel.get("night_leader_ok", False)))
            _col_min, _col_max = st.columns(2)
            with _col_min:
                _f_nc_min = st.number_input("夜勤下限", min_value=0, step=1,
                                             value=int(_sel.get("night_count_min", 0)))
            with _col_max:
                _f_nc_max = st.number_input("夜勤上限", min_value=0, step=1,
                                             value=int(_sel.get("night_count_max", 0)))
            _f_hours = st.number_input("目標時間(h)", min_value=0.0, step=0.5,
                                       value=float(_sel.get("target_hours", 170.0)))
            _dc_idx = DAYCARE_TYPE_OPTIONS.index(
                str(_sel.get("daycare_type", "none"))
                if str(_sel.get("daycare_type", "none")) in DAYCARE_TYPE_OPTIONS else "none"
            )
            _f_dc = st.selectbox(
                "保育園利用",
                options=DAYCARE_TYPE_OPTIONS,
                format_func=lambda v: DAYCARE_TYPE_LABELS.get(v, v),
                index=_dc_idx,
            )
            # 夜間保育必須フラグ
            _nc_req_current = bool(_sel.get("nightcare_required", False))
            _f_nc_req = st.checkbox(
                "夜間保育があるときのみ夜勤可",
                value=_nc_req_current,
                help="ON：夜間保育受け入れ日以外は夜勤不可。OFF：家族対応等で夜間保育なしでも夜勤可。",
            )

            st.markdown("---")
            # 夜間学童
            _f_gakudo = st.checkbox(
                "夜間学童を利用",
                value=bool(_sel.get("gakudo", False)),
                help="夜間学童を利用しているスタッフにチェックしてください。",
            )
            _f_gakudo_req = st.checkbox(
                "夜間学童があるときのみ夜勤可",
                value=bool(_sel.get("gakudo_required", False)),
                help="ON：夜間学童受け入れ日以外は夜勤不可。OFF：家族対応等で夜間学童なしでも夜勤可。",
            )

            if st.form_submit_button("💾 保存", type="primary"):
                st.session_state.staff_df.loc[_sel_idx, "name"]               = _f_name
                st.session_state.staff_df.loc[_sel_idx, "years_exp"]          = _f_years
                st.session_state.staff_df.loc[_sel_idx, "night_ok"]           = _f_night_ok
                st.session_state.staff_df.loc[_sel_idx, "day_leader_ok"]      = _f_day_l
                st.session_state.staff_df.loc[_sel_idx, "night_leader_ok"]    = _f_night_l
                st.session_state.staff_df.loc[_sel_idx, "night_count_min"]    = _f_nc_min
                st.session_state.staff_df.loc[_sel_idx, "night_count_max"]    = _f_nc_max
                st.session_state.staff_df.loc[_sel_idx, "target_hours"]       = _f_hours
                st.session_state.staff_df.loc[_sel_idx, "daycare_type"]       = _f_dc
                st.session_state.staff_df.loc[_sel_idx, "nightcare_required"] = _f_nc_req
                st.session_state.staff_df.loc[_sel_idx, "gakudo"]             = _f_gakudo
                st.session_state.staff_df.loc[_sel_idx, "gakudo_required"]    = _f_gakudo_req
                save_settings(
                    requirements=st.session_state.requirements,
                    soft_weights=st.session_state.soft_weights,
                    hospital_holidays=st.session_state.hospital_holidays,
                    daycare_closed=st.session_state.daycare_closed,
                    nightcare_open=st.session_state.nightcare_open,
                    gakudo_open=st.session_state.gakudo_open,
                    system_constraint_priorities=st.session_state.system_constraint_priorities,
                    user_constraints=st.session_state.user_constraints,
                    staff_df=st.session_state.staff_df,
                    target_year=st.session_state.get("target_year"),
                    target_month=st.session_state.get("target_month"),
                )
                st.success("保存しました。")
                st.rerun()

        # ── スタッフ追加 ──
        st.divider()
        with st.expander("➕ スタッフを追加"):
            with st.form("add_staff_form"):
                _ac1, _ac2 = st.columns(2)
                _new_name  = _ac1.text_input("氏名")
                _new_years = _ac2.number_input("経験年数", min_value=0, step=1, value=1)
                _bc1, _bc2, _bc3 = st.columns(3)
                _new_night_ok = _bc1.checkbox("夜勤可", value=True)
                _new_day_l    = _bc2.checkbox("日勤リーダー可", value=False)
                _new_night_l  = _bc3.checkbox("夜勤リーダー可", value=False)
                _cc1, _cc2, _cc3 = st.columns(3)
                _new_nc_min = _cc1.number_input("夜勤下限", min_value=0, step=1, value=3)
                _new_nc_max = _cc2.number_input("夜勤上限", min_value=0, step=1, value=6)
                _new_hours  = _cc3.number_input("目標時間(h)", min_value=0.0, step=0.5, value=170.0)
                if st.form_submit_button("✅ 追加", type="primary"):
                    if not _new_name.strip():
                        st.error("氏名を入力してください。")
                    elif _new_name.strip() in st.session_state.staff_df["name"].tolist():
                        st.error("同じ氏名のスタッフが既に登録されています。")
                    else:
                        _new_id    = int(st.session_state.staff_df["id"].max()) + 1
                        _new_order = int(st.session_state.staff_df["order"].max()) + 1
                        _new_row = pd.DataFrame([{
                            "id": _new_id, "name": _new_name.strip(),
                            "years_exp": _new_years, "night_ok": _new_night_ok,
                            "day_leader_ok": _new_day_l, "night_leader_ok": _new_night_l,
                            "night_count_min": _new_nc_min, "night_count_max": _new_nc_max,
                            "target_hours": float(_new_hours),
                            "daycare_type": "none", "nightcare_required": False,
                            "gakudo": False, "gakudo_required": False,
                            "order": _new_order, "active": True,
                        }])
                        st.session_state.staff_df = pd.concat(
                            [st.session_state.staff_df, _new_row], ignore_index=True
                        )
                        save_settings(
                            requirements=st.session_state.requirements,
                            soft_weights=st.session_state.soft_weights,
                            hospital_holidays=st.session_state.hospital_holidays,
                            daycare_closed=st.session_state.daycare_closed,
                            nightcare_open=st.session_state.nightcare_open,
                            gakudo_open=st.session_state.gakudo_open,
                            system_constraint_priorities=st.session_state.system_constraint_priorities,
                            user_constraints=st.session_state.user_constraints,
                            staff_df=st.session_state.staff_df,
                            target_year=st.session_state.get("target_year"),
                            target_month=st.session_state.get("target_month"),
                        )
                        st.success(f"「{_new_name.strip()}」を追加しました。")
                        st.rerun()

        # ── 完全削除 ──
        st.divider()
        with st.expander("⚠️ 完全削除（取り消し不可）"):
            st.warning(f"「{_sel_name}」をスタッフ一覧から完全に削除します。この操作は元に戻せません。一時的に外す場合は「スタッフ一覧」の「有効」チェックを外して休止してください。")
            _confirm_del = st.checkbox("削除することを確認しました", key=f"confirm_del_{_sel_name}")
            if _confirm_del:
                if st.button("🗑️ 完全削除を実行", type="primary", key=f"exec_del_{_sel_name}"):
                    _del_id = int(_sel["id"])
                    st.session_state.staff_df = st.session_state.staff_df[
                        st.session_state.staff_df["id"] != _del_id
                    ].reset_index(drop=True)
                    st.session_state.requests_df = st.session_state.requests_df[
                        st.session_state.requests_df["staff_id"] != _del_id
                    ].reset_index(drop=True)
                    save_settings(
                        requirements=st.session_state.requirements,
                        soft_weights=st.session_state.soft_weights,
                        hospital_holidays=st.session_state.hospital_holidays,
                        daycare_closed=st.session_state.daycare_closed,
                        nightcare_open=st.session_state.nightcare_open,
                        gakudo_open=st.session_state.gakudo_open,
                        system_constraint_priorities=st.session_state.system_constraint_priorities,
                        user_constraints=st.session_state.user_constraints,
                        staff_df=st.session_state.staff_df,
                        target_year=st.session_state.get("target_year"),
                        target_month=st.session_state.get("target_month"),
                    )
                    save_requests(st.session_state.requests_df)
                    st.success(f"「{_sel_name}」を削除しました。")
                    st.rerun()

    with col_requests:
        st.markdown("**シフト希望一覧（今月）**")
        _sel_id = int(_sel["id"])
        _req_df = st.session_state.requests_df
        if len(_req_df) > 0 and "staff_id" in _req_df.columns:
            _staff_reqs = _req_df[_req_df["staff_id"] == _sel_id].copy()
        else:
            _staff_reqs = pd.DataFrame(columns=["staff_id", "date", "shift", "is_fixed"])

        if len(_staff_reqs) > 0:
            from utils.time_utils import SHIFT_LABEL
            _staff_reqs = _staff_reqs.sort_values("date")
            _req_rows = []
            for _, _rr in _staff_reqs.iterrows():
                _wname = "月火水木金土日"[_rr["date"].weekday()]
                _shift_label = SHIFT_LABEL.get(_rr["shift"], _rr["shift"])
                _kind = "確定" if _rr.get("is_fixed", False) else "希望"
                _req_rows.append({
                    "日付":   f"{_rr['date'].month}/{_rr['date'].day}（{_wname}）",
                    "シフト": _shift_label,
                    "区分":   _kind,
                })
            st.dataframe(
                pd.DataFrame(_req_rows).set_index("日付"),
                use_container_width=True,
                height=min(60 + 35 * len(_req_rows), 600),
            )
        else:
            st.info("希望が登録されていません。")

with tab_common:
    # ══════════════════════════════════════════════════════
    # 院内共通設定：夜間保育・夜間学童・病院独自休日
    # ══════════════════════════════════════════════════════
    import holidays as holidays_lib

    # ── 保育園設定 ──────────────────────────────────
    st.subheader("🏫 保育園設定")

    _dc_df = st.session_state.staff_df
    if "daycare_type" in _dc_df.columns:
        _day_users   = _dc_df[_dc_df["daycare_type"] == "day"]["name"].tolist()
        _night_users = _dc_df[_dc_df["daycare_type"] == "night"]["name"].tolist()
    else:
        _day_users, _night_users = [], []
    if _day_users:
        st.caption(f"🌤 日中のみ利用: {', '.join(_day_users)}")
    if _night_users:
        st.caption(f"🌙 夜間保育利用: {', '.join(_night_users)}")
    if not _day_users and not _night_users:
        st.caption("保育園利用スタッフ: なし（スタッフ一覧の「保育園利用」列で設定してください）")

    # 保育園休園日
    st.markdown("**保育園休園日**（日中・夜間保育とも全スタッフ勤務不可）")
    col_dc1, col_dc2 = st.columns([2, 1])
    with col_dc1:
        new_closed = st.date_input(
            "休園日を追加",
            value=None,
            key="daycare_add_date",
        )
    with col_dc2:
        st.write("")
        st.write("")
        if st.button("追加", key="daycare_add_btn"):
            if new_closed and new_closed not in st.session_state.daycare_closed:
                st.session_state.daycare_closed = sorted(
                    st.session_state.daycare_closed + [new_closed]
                )
                st.rerun()

    if st.session_state.daycare_closed:
        jp_hols_dc = holidays_lib.Japan(years={d.year for d in st.session_state.daycare_closed})
        wnames = "月火水木金土日"
        remove_date = None
        for dc in sorted(st.session_state.daycare_closed):
            wn = wnames[dc.weekday()]
            tag = "（祝）" if dc in jp_hols_dc else ""
            col_a, col_b = st.columns([3, 1])
            col_a.write(f"🔴 {dc.strftime('%Y/%m/%d')}（{wn}）{tag}")
            if col_b.button("削除", key=f"dc_del_{dc}"):
                remove_date = dc
        if remove_date:
            st.session_state.daycare_closed = [
                d for d in st.session_state.daycare_closed if d != remove_date
            ]
            st.rerun()
    else:
        st.info("休園日が登録されていません。")

    # 夜間保育受け入れ日
    st.markdown("**夜間保育受け入れ日**（夜間保育利用スタッフはこの日のみ夜勤可）")
    if not _night_users:
        st.caption("夜間保育利用スタッフが設定されていないため、この設定は無効です。")
    col_nc1, col_nc2 = st.columns([2, 1])
    with col_nc1:
        new_nc = st.date_input(
            "受け入れ日を追加",
            value=None,
            key="nightcare_add_date",
        )
    with col_nc2:
        st.write("")
        st.write("")
        if st.button("追加", key="nightcare_add_btn"):
            if new_nc and new_nc not in st.session_state.nightcare_open:
                st.session_state.nightcare_open = sorted(
                    st.session_state.nightcare_open + [new_nc]
                )
                st.rerun()

    if st.session_state.nightcare_open:
        jp_hols_nc = holidays_lib.Japan(years={d.year for d in st.session_state.nightcare_open})
        wnames_nc = "月火水木金土日"
        remove_nc = None
        for nc in sorted(st.session_state.nightcare_open):
            wn = wnames_nc[nc.weekday()]
            tag = "（祝）" if nc in jp_hols_nc else ""
            col_a, col_b = st.columns([3, 1])
            col_a.write(f"🌙 {nc.strftime('%Y/%m/%d')}（{wn}）{tag}")
            if col_b.button("削除", key=f"nc_del_{nc}"):
                remove_nc = nc
        if remove_nc:
            st.session_state.nightcare_open = [
                d for d in st.session_state.nightcare_open if d != remove_nc
            ]
            st.rerun()
    else:
        if _night_users:
            st.warning("受け入れ日が登録されていません。夜間保育利用スタッフは夜勤に入れません。")
        else:
            st.info("受け入れ日が登録されていません。")

    st.divider()

    # ── 夜間学童設定 ─────────────────────────────────
    st.subheader("🏫 夜間学童設定")

    _gakudo_df = st.session_state.staff_df
    if "gakudo" in _gakudo_df.columns:
        _gakudo_users = _gakudo_df[_gakudo_df["gakudo"] == True]["name"].tolist()
    else:
        _gakudo_users = []
    if _gakudo_users:
        st.caption(f"🏫 夜間学童利用: {', '.join(_gakudo_users)}")
    else:
        st.caption("夜間学童利用スタッフ: なし（スタッフ一覧の「夜間学童を利用」で設定してください）")

    st.markdown("**夜間学童受け入れ日**（「夜間学童があるときのみ夜勤可」のスタッフはこの日のみ夜勤可）")
    if not _gakudo_users:
        st.caption("夜間学童利用スタッフが設定されていないため、この設定は無効です。")
    col_gk1, col_gk2 = st.columns([2, 1])
    with col_gk1:
        new_gk = st.date_input(
            "受け入れ日を追加",
            value=None,
            key="gakudo_add_date",
        )
    with col_gk2:
        st.write("")
        st.write("")
        if st.button("追加", key="gakudo_add_btn"):
            if new_gk and new_gk not in st.session_state.gakudo_open:
                st.session_state.gakudo_open = sorted(
                    st.session_state.gakudo_open + [new_gk]
                )
                st.rerun()

    if st.session_state.gakudo_open:
        jp_hols_gk = holidays_lib.Japan(years={d.year for d in st.session_state.gakudo_open})
        wnames_gk = "月火水木金土日"
        remove_gk = None
        for gk in sorted(st.session_state.gakudo_open):
            wn = wnames_gk[gk.weekday()]
            tag = "（祝）" if gk in jp_hols_gk else ""
            col_a, col_b = st.columns([3, 1])
            col_a.write(f"🏫 {gk.strftime('%Y/%m/%d')}（{wn}）{tag}")
            if col_b.button("削除", key=f"gk_del_{gk}"):
                remove_gk = gk
        if remove_gk:
            st.session_state.gakudo_open = [
                d for d in st.session_state.gakudo_open if d != remove_gk
            ]
            st.rerun()
    else:
        if _gakudo_users:
            st.warning("受け入れ日が登録されていません。「夜間学童があるときのみ夜勤可」スタッフは夜勤に入れません。")
        else:
            st.info("受け入れ日が登録されていません。")

    st.divider()

    # ── 病院独自休日設定 ─────────────────────────────
    st.subheader("🏥 病院独自休日設定")
    st.caption("日本の祝日・土日とは別に、病院として休日扱いにする日を設定します。"
               "（日勤・遅出の必要人数が「休日」設定に切り替わり、遅出は0名になります）")

    col_hh1, col_hh2 = st.columns([2, 1])
    with col_hh1:
        new_hosp_hol = st.date_input(
            "病院独自休日を追加",
            value=None,
            key="hospital_holiday_add_date",
        )
    with col_hh2:
        st.write("")
        st.write("")
        if st.button("追加", key="hospital_holiday_add_btn"):
            if new_hosp_hol and new_hosp_hol not in st.session_state.hospital_holidays:
                st.session_state.hospital_holidays = sorted(
                    st.session_state.hospital_holidays + [new_hosp_hol]
                )
                st.rerun()

    if st.session_state.hospital_holidays:
        st.markdown("**登録済み病院独自休日**")
        jp_hols_hh = holidays_lib.Japan(
            years={d.year for d in st.session_state.hospital_holidays}
        )
        wnames_hh = "月火水木金土日"
        remove_hh = None
        for hh in sorted(st.session_state.hospital_holidays):
            wn = wnames_hh[hh.weekday()]
            jp_tag = "（祝日と重複）" if hh in jp_hols_hh else ""
            col_a, col_b = st.columns([3, 1])
            col_a.write(f"🏥 {hh.strftime('%Y/%m/%d')}（{wn}）{jp_tag}")
            if col_b.button("削除", key=f"hh_del_{hh}"):
                remove_hh = hh
        if remove_hh:
            st.session_state.hospital_holidays = [
                d for d in st.session_state.hospital_holidays if d != remove_hh
            ]
            st.rerun()
    else:
        st.info("病院独自休日が登録されていません。")

    st.divider()
    st.subheader("💾 設定の保存")
    st.caption("院内共通設定を settings.json に保存します。次回起動時に自動で読み込まれます。")
    if st.button("院内共通設定を保存", type="primary", key="save_common_settings_btn"):
        try:
            save_settings(
                requirements=st.session_state.requirements,
                soft_weights=st.session_state.soft_weights,
                hospital_holidays=st.session_state.hospital_holidays,
                daycare_closed=st.session_state.daycare_closed,
                nightcare_open=st.session_state.nightcare_open,
                gakudo_open=st.session_state.gakudo_open,
                system_constraint_priorities=st.session_state.system_constraint_priorities,
                user_constraints=st.session_state.user_constraints,
                staff_df=st.session_state.staff_df,
                target_year=st.session_state.get("target_year"),
                target_month=st.session_state.get("target_month"),
            )
            st.success("✅ 院内共通設定を保存しました。")
        except Exception as _e:
            st.error(f"❌ 保存に失敗しました: {_e}")

with tab_ward:
    # ══════════════════════════════════════════════════════
    # 病棟独自設定：必要人数・スタッフ表示順・ソフト制約の重み
    # ══════════════════════════════════════════════════════
    # ── 必要人数設定 ─────────────────────────────────
    st.subheader("必要人数設定")

    st.markdown("**日勤**")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.number_input("下限（平日）",  min_value=1, max_value=30, step=1, key="req_d_weekday")
    with c2:
        st.number_input("最大（平日）",  min_value=1, max_value=30, step=1, key="req_d_weekday_max")
    with c3:
        st.number_input("下限（休日）",  min_value=1, max_value=30, step=1, key="req_d_holiday")
    with c4:
        st.number_input("最大（休日）",  min_value=1, max_value=30, step=1, key="req_d_holiday_max")

    if st.session_state.req_d_weekday_max < st.session_state.req_d_weekday:
        st.warning("日勤の平日最大人数が下限を下回っています。")
    if st.session_state.req_d_holiday_max < st.session_state.req_d_holiday:
        st.warning("日勤の休日最大人数が下限を下回っています。")

    st.markdown("**遅出**")
    c5, c6, c7, _ = st.columns(4)
    with c5:
        st.number_input("遅出（平日）",  min_value=0, max_value=10, step=1, key="req_l_weekday")
    with c6:
        st.number_input("遅出（休日）",  min_value=0, max_value=10, step=1, key="req_l_holiday")
    with c7:
        l_first_year_ok = st.checkbox(
            "1年目も遅出可",
            value=bool(st.session_state.requirements.get("L", {}).get("first_year_ok", False)),
            key="req_l_fy_ok",
            help="オフにすると1年目看護師は遅出（L）に入れません",
        )

    st.markdown("**夜勤**")
    c5, c6, c7 = st.columns(3)
    with c5:
        st.number_input("夜勤（下限人数）", min_value=2, max_value=8, step=1, key="req_n_base")
    with c6:
        st.number_input("夜勤（上限人数）", min_value=2, max_value=8, step=1, key="req_n_max")
    with c7:
        fy_plus1 = st.checkbox(
            "1年目在籍時は+1名",
            value=bool(st.session_state.requirements.get("N", {}).get("first_year_plus1", True)),
            key="req_n_fy_plus1",
            help="夜勤に1年目看護師が入る日は上下限を各+1にします（ハード条件）",
        )
        st.number_input(
            "1年目の夜勤上限",
            min_value=1, max_value=3, step=1,
            value=int(st.session_state.requirements.get("N", {}).get("first_year_max", 1)),
            key="req_n_fy_max",
            help="1回の夜勤（N1）に入れる1年目看護師の最大人数",
        )

    if st.session_state.req_n_max < st.session_state.req_n_base:
        st.warning("上限人数が下限人数を下回っています。")

    # session_state キーから requirements dict を組み立てて更新
    st.session_state.requirements = {
        "D": {"weekday":     st.session_state.req_d_weekday,
              "weekday_max": st.session_state.req_d_weekday_max,
              "holiday":     st.session_state.req_d_holiday,
              "holiday_max": st.session_state.req_d_holiday_max},
        "L": {"weekday": st.session_state.req_l_weekday,
              "holiday": st.session_state.req_l_holiday,
              "first_year_ok": l_first_year_ok},
        "N": {"base":             st.session_state.req_n_base,
              "max":              st.session_state.req_n_max,
              "first_year_plus1": fy_plus1,
              "first_year_max":   st.session_state.req_n_fy_max},
    }

    st.divider()

    # ── スタッフ表示順 ────────────────────────────────
    st.subheader("↕️ スタッフ表示順")
    st.caption("ドラッグ＆ドロップで表示順を変更できます。")
    _sorted_staff = st.session_state.staff_df.sort_values("order").reset_index(drop=True)
    _label_to_name = {}
    _current_labels = []
    for _, _row in _sorted_staff.iterrows():
        _lbl = f"{_row['name']}（{int(_row['years_exp'])}）"
        _current_labels.append(_lbl)
        _label_to_name[_lbl] = _row["name"]
    _current_names = _sorted_staff["name"].tolist()
    _SORT_STYLE = """
    .sortable-item, .sortable-item:hover {
        background-color: #eceff1;
        color: #37474f;
        border: 1px solid #cfd8dc;
        border-radius: 4px;
        font-size: 13px;
        box-shadow: none;
    }
    """
    _new_labels = sort_items(
        _current_labels, direction="vertical",
        custom_style=_SORT_STYLE, key="staff_order_drag"
    )
    _new_names = [_label_to_name.get(_lbl, _lbl) for _lbl in _new_labels]
    if _new_names != _current_names:
        _name_to_id = dict(zip(_sorted_staff["name"], _sorted_staff["id"]))
        _df_reorder = st.session_state.staff_df.copy()
        for _new_pos, _nm in enumerate(_new_names, start=1):
            _sid = _name_to_id.get(_nm)
            if _sid is not None:
                _df_reorder.loc[_df_reorder["id"] == _sid, "order"] = _new_pos
        st.session_state.staff_df = _df_reorder
        st.rerun()

    st.divider()

    # ── ソフト制約の重み ──────────────────────────────
    st.subheader("⚖️ ソフト制約の重み設定")
    st.caption("各ソフト制約の優先度（重み）を設定します。値が大きいほど優先されます。")
    _sw = st.session_state.soft_weights
    _sw_items = [
        ("soft_req",       "希望シフト尊重",        5, "スタッフのシフト希望をどれだけ優先するか"),
        ("exp_balance",    "経験年数バランス",       4, "ベテラン看護師が各日に分散して入るよう誘導する強さ"),
        ("night_spread",   "夜勤の月内散らばり",     3, "月の前半・中盤・後半に夜勤が均等に散らばるよう誘導する強さ"),
        ("night_evenness", "スタッフ間夜勤均等化",   2, "スタッフ間の夜勤回数のばらつきを抑える強さ"),
        ("day_leader",     "日勤リーダー確保",       7, "各日の日勤にリーダー適性者が入るよう誘導する強さ"),
        ("night_leader",   "夜勤リーダー確保",       6, "各夜勤にリーダー適性者が入るよう誘導する強さ"),
    ]
    _sw_col1, _sw_gap, _sw_col2 = st.columns([5, 1, 5])
    for _swi, (_sw_key, _sw_label, _sw_default, _sw_help) in enumerate(_sw_items):
        _cur_val = int(_sw.get(_sw_key, _sw_default))
        _cur_val = max(1, min(10, _cur_val))
        _col = _sw_col1 if _swi % 2 == 0 else _sw_col2
        _sw[_sw_key] = _col.slider(
            _sw_label,
            min_value=1, max_value=10, step=1,
            value=_cur_val,
            key=f"sw_{_sw_key}",
            help=_sw_help,
        )
    st.session_state.soft_weights = _sw

    st.divider()

    # ── 制約条件メモ ──────────────────────────────────────────
    st.subheader("📝 制約条件メモ")
    st.caption("勤務表作成時に考慮すべき条件を記録しておく欄です。ソルバーへの直接入力ではなく、担当者向けのメモとして活用してください。")

    _con_col_hard, _con_col_soft = st.columns(2, gap="large")

    def _save_constraints():
        save_settings(
            requirements=st.session_state.requirements,
            soft_weights=st.session_state.soft_weights,
            hospital_holidays=st.session_state.hospital_holidays,
            daycare_closed=st.session_state.daycare_closed,
            nightcare_open=st.session_state.nightcare_open,
            gakudo_open=st.session_state.gakudo_open,
            system_constraint_priorities=st.session_state.system_constraint_priorities,
            user_constraints=st.session_state.user_constraints,
            staff_df=st.session_state.staff_df,
            target_year=st.session_state.get("target_year"),
            target_month=st.session_state.get("target_month"),
        )

    # ── もともとの条件 ──
    with _con_col_hard:
        st.markdown("### 🔒 もともとの条件")
        st.caption("ソルバーに組み込まれているルールです。優先度を設定できます。")

        _sys_prio = st.session_state.system_constraint_priorities
        _sys_changed = False
        for _ci, _cdef in enumerate(SYSTEM_CONSTRAINTS):
            _ctext = _cdef["text"]
            _cur_prio = _sys_prio.get(_ctext, _cdef["default_priority"])
            _cc1, _cc2 = st.columns([3, 2])
            _cc1.markdown(f"**{_ctext}**")
            _new_prio = _cc2.select_slider(
                "優先度",
                options=[1, 2, 3, 4, 5],
                value=_cur_prio,
                format_func=lambda v: PRIORITY_LABELS[v],
                key=f"sys_prio_{_ci}",
                label_visibility="collapsed",
            )
            if _new_prio != _cur_prio:
                _sys_prio[_ctext] = _new_prio
                _sys_changed = True
        if _sys_changed:
            _save_constraints()
            st.rerun()

    # ── フリー記載の条件 ──
    with _con_col_soft:
        st.markdown("### 📝 フリー記載の条件")
        st.caption("自由に条件を追加できます。優先度 1（低）〜 5（高）で設定してください。")

        # 登録済みリスト
        _del_uc = None
        for _ui, _uc in enumerate(st.session_state.user_constraints):
            _uca, _ucb, _ucc = st.columns([4, 2, 1])
            _uca.markdown(f"**{_ui + 1}.** {_uc['text']}")
            _new_uprio = _ucb.select_slider(
                "優先度",
                options=[1, 2, 3, 4, 5],
                value=int(_uc.get("priority", 3)),
                format_func=lambda v: PRIORITY_LABELS[v],
                key=f"user_prio_{_ui}",
                label_visibility="collapsed",
            )
            if _new_uprio != int(_uc.get("priority", 3)):
                st.session_state.user_constraints[_ui]["priority"] = _new_uprio
                _save_constraints()
                st.rerun()
            if _ucc.button("削除", key=f"del_uc_{_ui}"):
                _del_uc = _ui
        if _del_uc is not None:
            st.session_state.user_constraints.pop(_del_uc)
            _save_constraints()
            st.rerun()

        if not st.session_state.user_constraints:
            st.caption("（登録なし）")

        st.markdown("---")
        # 追加フォーム
        _add_col1, _add_col2 = st.columns([3, 2])
        _new_uc_text = _add_col1.text_area(
            "条件を入力",
            value="",
            height=80,
            placeholder="例：〇〇さんと△△さんは同じ夜勤に入れない",
            key="input_user_constraint",
            label_visibility="collapsed",
        )
        _new_uc_prio = _add_col2.select_slider(
            "優先度",
            options=[1, 2, 3, 4, 5],
            value=3,
            format_func=lambda v: PRIORITY_LABELS[v],
            key="input_user_prio",
        )
        if st.button("➕ 追加", key="add_uc_btn", type="secondary"):
            _lines = [l.strip() for l in _new_uc_text.splitlines() if l.strip()]
            if _lines:
                for _l in _lines:
                    st.session_state.user_constraints.append(
                        {"text": _l, "priority": _new_uc_prio}
                    )
                _save_constraints()
                st.rerun()

    st.divider()
    st.subheader("💾 設定の保存")
    st.caption("病棟独自設定を settings.json に保存します。次回起動時に自動で読み込まれます。")
    if st.button("病棟独自設定を保存", type="primary", key="save_settings_btn"):
        try:
            save_settings(
                requirements=st.session_state.requirements,
                soft_weights=st.session_state.soft_weights,
                hospital_holidays=st.session_state.hospital_holidays,
                daycare_closed=st.session_state.daycare_closed,
                nightcare_open=st.session_state.nightcare_open,
                gakudo_open=st.session_state.gakudo_open,
                system_constraint_priorities=st.session_state.system_constraint_priorities,
                user_constraints=st.session_state.user_constraints,
                staff_df=st.session_state.staff_df,
                target_year=st.session_state.get("target_year"),
                target_month=st.session_state.get("target_month"),
            )
            st.success("✅ 病棟独自設定を保存しました。")
        except Exception as _e:
            st.error(f"❌ 保存に失敗しました: {_e}")

