"""
CP-SAT ソルバーによる勤務表自動作成。

勤務コード:  D=日勤  L=遅出  N1=ヤ1(入り)  N2=ヤ2(明け)  O=休み  P=有休
シフトインデックス: 0=D 1=L 2=N1 3=N2 4=O 5=P
"""
import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from ortools.sat.python import cp_model

from utils.time_utils import SHIFTS, SHIFT_IDX, is_holiday, is_weekday, schedule_dates, REQUEST_TO_SHIFT

D, L, N1, N2, O, P = 0, 1, 2, 3, 4, 5

# 夜勤1回あたりの実働時間（N1+N2合計16h を各8hに分割）
NIGHT_HOURS_EACH = 8.0
SHIFT_HOURS = [7.5, 7.5, 8.0, 8.0, 0.0, 0.0]


_DEFAULT_SOFT_WEIGHTS: Dict[str, int] = {
    "soft_req":       50,
    "exp_balance":    40,
    "night_spread":   30,
    "night_evenness": 20,
    "day_leader":    200,
    "night_leader":  150,
}


def solve(
    staff_df: pd.DataFrame,
    requests_df: pd.DataFrame,
    requirements: Dict[str, Dict[str, int]],  # {"D": {"weekday":8,"holiday":6}, ...}
    year: int,
    month: int,
    dedicated_first: Optional[int],   # 前半夜勤専任 staff id (21〜末日)
    dedicated_second: Optional[int],  # 後半夜勤専任 staff id (1〜20)
    daycare_closed_dates: Optional[List] = None,   # 保育園休園日リスト（日中利用スタッフ）
    nightcare_open_dates: Optional[List] = None,   # 夜間保育受け入れ日リスト
    gakudo_open_dates: Optional[List] = None,      # 夜間学童受け入れ日リスト
    hospital_holidays: Optional[List] = None,      # 病院独自休日リスト
    time_limit_sec: int = 60,
    soft_weights: Optional[Dict[str, int]] = None,  # ソフト制約の重み
) -> Tuple[Optional[pd.DataFrame], str, List[Dict]]:
    """
    Returns:
        schedule_df: DataFrame(index=staff_id, columns=date, values=shift_code)
        status: "OPTIMAL" | "FEASIBLE" | "INFEASIBLE" | "UNKNOWN"
        warnings: 充足できなかったソフト制約の警告リスト
    """
    # ソフト制約の重みを解決（未指定キーはデフォルト値で補完）
    _sw = dict(_DEFAULT_SOFT_WEIGHTS)
    if soft_weights:
        _sw.update(soft_weights)
    W_SOFT_REQ       = _sw["soft_req"]
    W_EXP_BALANCE    = _sw["exp_balance"]
    W_NIGHT_SPREAD   = _sw["night_spread"]
    W_NIGHT_EVENNESS = _sw["night_evenness"]
    W_DAY_LEADER     = _sw["day_leader"]
    W_NIGHT_LEADER   = _sw["night_leader"]

    dates = schedule_dates(year, month)
    n_days = len(dates)
    staff_ids = staff_df["id"].tolist()
    n_staff = len(staff_ids)
    sid_to_idx = {sid: i for i, sid in enumerate(staff_ids)}

    # 前半/後半の分割インデックス
    split_date = datetime.date(year, month + 1 if month < 12 else 1,
                               1 if month < 12 else 1)
    # 前半: dates[d].month == year/month (21〜末日)
    # 後半: dates[d].month != year/month (1〜20)
    def is_first_half(d_idx: int) -> bool:
        return dates[d_idx].month == month

    # 夜勤専任のstaff_idxセット（前半・後半）
    ded_first_idx  = sid_to_idx[dedicated_first]  if dedicated_first  in sid_to_idx else None
    ded_second_idx = sid_to_idx[dedicated_second] if dedicated_second in sid_to_idx else None

    # 1年目スタッフのインデックスセット
    first_year_idx = set(
        sid_to_idx[row["id"]]
        for _, row in staff_df.iterrows()
        if row["years_exp"] == 1
    )

    # 保育園利用スタッフのインデックスセット（日中・夜間）
    # daycare_type: "day"=日中のみ（休園日に勤務不可）, "night"=夜間保育あり（夜間保育受け入れ日のみ夜勤可）
    # 後方互換: 旧 daycare bool 列もサポート
    def _daycare_type(row):
        if "daycare_type" in row:
            return str(row.get("daycare_type", "none"))
        # 旧形式
        return "day" if bool(row.get("daycare", False)) else "none"

    daycare_day_idx = set(
        sid_to_idx[row["id"]]
        for _, row in staff_df.iterrows()
        if _daycare_type(row) == "day" and row["id"] in sid_to_idx
    )
    # nightcare_required=True のスタッフのみ夜間保育日制約の対象
    # nightcare_required=False は家族対応等で夜間保育なしでも夜勤可
    daycare_night_idx = set(
        sid_to_idx[row["id"]]
        for _, row in staff_df.iterrows()
        if _daycare_type(row) == "night"
        and bool(row.get("nightcare_required", True))
        and row["id"] in sid_to_idx
    )
    # 日中・夜間どちらも「休園日は勤務不可」制約の対象
    daycare_staff_idx = daycare_day_idx | daycare_night_idx

    # 夜間学童: gakudo_required=True のスタッフのみ受け入れ日制約の対象
    gakudo_required_idx = set(
        sid_to_idx[row["id"]]
        for _, row in staff_df.iterrows()
        if bool(row.get("gakudo", False))
        and bool(row.get("gakudo_required", False))
        and row["id"] in sid_to_idx
    )

    # 希望シフトを分類
    # fixed_requests:  ハード確定 {(s_idx, d_idx) -> shift_idx}  ※ソルバー用
    # soft_requests:   ソフト希望 {(s_idx, d_idx) -> shift_idx}
    # fixed_overlays:  出力復元用 {(s_idx, d_idx) -> 元のシフトコード} (T/I など)
    fixed_requests: Dict[Tuple[int, int], int] = {}
    soft_requests:  Dict[Tuple[int, int], int] = {}
    fixed_overlays: Dict[Tuple[int, int], str] = {}  # T/I を出力に復元するため保持
    for _, row in requests_df.iterrows():
        d = row["date"]
        if d not in dates:
            continue
        sidx = sid_to_idx.get(row["staff_id"])
        if sidx is None:
            continue
        didx = dates.index(d)
        shift_code = row["shift"]
        # T（研修）・I（委員会）→ ソルバー上は D として扱い、出力で元のコードに戻す
        from utils.time_utils import OVERLAY_SHIFTS
        if shift_code in OVERLAY_SHIFTS:
            if row["is_fixed"]:
                fixed_overlays[(sidx, didx)] = shift_code
            shift_code = "D"
        if shift_code not in SHIFT_IDX:
            continue
        if row["is_fixed"]:
            fixed_requests[(sidx, didx)] = SHIFT_IDX[shift_code]
        else:
            soft_requests[(sidx, didx)] = SHIFT_IDX[shift_code]

    # 病院独自休日をセットに変換（is_holiday へ渡す）
    _hosp_hols = set(hospital_holidays) if hospital_holidays else set()

    # 夜勤人数設定
    _n_cfg = requirements.get("N", {})
    n_night_base = int(_n_cfg.get("base", 4))
    n_night_max  = int(_n_cfg.get("max",  n_night_base))
    n_night_max  = max(n_night_max, n_night_base)   # max >= base を保証
    n_fy_plus1   = bool(_n_cfg.get("first_year_plus1", True))

    # 遅出設定
    l_first_year_ok = bool(requirements.get("L", {}).get("first_year_ok", False))

    model = cp_model.CpModel()

    # ── 変数 ──────────────────────────────────────────────────
    # x[s][d][k] = 1 ならスタッフs の d日目 が シフトk
    x = [
        [[model.new_bool_var(f"x_{s}_{d}_{k}") for k in range(6)]
         for d in range(n_days)]
        for s in range(n_staff)
    ]

    # ── ハード制約 ─────────────────────────────────────────────

    for s in range(n_staff):
        row = staff_df.iloc[s]
        night_ok = bool(row["night_ok"])
        night_min = int(row["night_count_min"])
        night_max = int(row["night_count_max"])

        # 夜勤専任フラグ（期間ごと）
        def is_dedicated(s_idx: int, d_idx: int) -> bool:
            if is_first_half(d_idx) and s_idx == ded_first_idx:
                return True
            if not is_first_half(d_idx) and s_idx == ded_second_idx:
                return True
            return False

        for d in range(n_days):
            # H8: 同日1シフトのみ
            model.add_exactly_one(x[s][d])

            # H5: 夜勤不可
            if not night_ok:
                model.add(x[s][d][N1] == 0)
                model.add(x[s][d][N2] == 0)

            # H_l_fy: 1年目遅出不可（設定がオフの場合）
            if not l_first_year_ok and s in first_year_idx:
                model.add(x[s][d][L] == 0)

            # H6: 夜勤専任は N1・N2 のみ（対象期間）
            if is_dedicated(s, d):
                model.add(x[s][d][D] == 0)
                model.add(x[s][d][L] == 0)
                model.add(x[s][d][P] == 0)

            # H9: 希望確定
            if (s, d) in fixed_requests:
                model.add(x[s][d][fixed_requests[(s, d)]] == 1)

        # H1: N1の翌日は必ずN2（スケジュール期間内のみ）
        for d in range(n_days - 1):
            model.add(x[s][d + 1][N2] == 1).only_enforce_if(x[s][d][N1])
            # N2の前日はN1（d=0 の N2 は前月からの持ち越しなので除外）
            model.add(x[s][d][N1] == 1).only_enforce_if(x[s][d + 1][N2])
        # 最終日(n_days-1)のN1は翌月へ持ち越し → 禁止しない
        # 初日(d=0)のN2は前月からの持ち越し → 前日N1なしで許容済み（上のループがd=0のN2をカバーしない）

        # H2: N2の翌日はN1またはO
        for d in range(n_days - 1):
            # N2[d]=1 → N1[d+1] + O[d+1] = 1
            b_n2 = x[s][d][N2]
            model.add(x[s][d + 1][N1] + x[s][d + 1][O] == 1).only_enforce_if(b_n2)

        # H_late: 遅出（L）の翌日はN1または休（O）のみ
        for d in range(n_days - 1):
            model.add(x[s][d + 1][N1] + x[s][d + 1][O] == 1).only_enforce_if(x[s][d][L])

        # H3: 2連続夜勤後の翌日・翌々日は休み
        # N2[d-2]=1 かつ N2[d]=1 → O[d+1], O[d+2]
        # (H1によりパターンは N2[d-2]→O/N1, N1[d-1]→N2[d] なので d-2がN2かつdがN2＝2連続確定)
        for d in range(2, n_days):
            b_prev2 = x[s][d - 2][N2]
            b_cur   = x[s][d][N2]
            if d + 1 < n_days:
                # b_prev2=1 AND b_cur=1 → O[d+1]
                model.add(x[s][d + 1][O] == 1).only_enforce_if([b_prev2, b_cur])
            if d + 2 < n_days:
                model.add(x[s][d + 2][O] == 1).only_enforce_if([b_prev2, b_cur])

        # H10/H11/H12: 月間夜勤回数（N1の合計 = 夜勤回数）
        total_n1 = sum(x[s][d][N1] for d in range(n_days))

        # 夜勤専任（前半または後半として指定されているか判断）
        is_ded_any = (s == ded_first_idx or s == ded_second_idx)
        if is_ded_any and not night_ok:
            pass  # 矛盾する設定は無視
        elif is_ded_any:
            model.add(total_n1 == 7)
        else:
            model.add(total_n1 >= night_min)
            model.add(total_n1 <= night_max)

    # H_dc: 保育園利用スタッフは保育園休園日に勤務不可（D/L/N1/N2 禁止）
    if daycare_closed_dates and daycare_staff_idx:
        closed_didx = [
            dates.index(dc) for dc in daycare_closed_dates
            if dc in dates
        ]
        for sidx in daycare_staff_idx:
            for didx in closed_didx:
                for shift in [D, L, N1, N2]:
                    model.add(x[sidx][didx][shift] == 0)

    # H_nc: 夜間保育利用スタッフは夜間保育受け入れ日以外に夜勤不可（N1/N2 禁止）
    if daycare_night_idx:
        open_set  = set(nightcare_open_dates or [])
        non_open_didx = [dates.index(d) for d in dates if d not in open_set]
        for sidx in daycare_night_idx:
            for didx in non_open_didx:
                model.add(x[sidx][didx][N1] == 0)
                model.add(x[sidx][didx][N2] == 0)

    # H_gk: 夜間学童利用スタッフは学童受け入れ日以外に夜勤不可（N1/N2 禁止）
    if gakudo_required_idx:
        gk_open_set = set(gakudo_open_dates or [])
        non_gk_didx = [dates.index(d) for d in dates if d not in gk_open_set]
        for sidx in gakudo_required_idx:
            for didx in non_gk_didx:
                model.add(x[sidx][didx][N1] == 0)
                model.add(x[sidx][didx][N2] == 0)

    # H7: 各日の必要人数（N1・N2）
    # 注意: 最終日はN1禁止のため N1人数制約の対象外
    #       初日はN2が来れないため N2人数制約の対象外
    for d in range(n_days):
        day_type = "holiday" if is_holiday(dates[d], _hosp_hols) else "weekday"

        # 日勤（下限）
        d_req = requirements.get("D", {}).get(day_type, 0)
        model.add(sum(x[s][d][D] for s in range(n_staff)) >= d_req)

        # 日勤（上限）- オプション
        d_req_max_key = f"{day_type}_max"
        d_req_max = requirements.get("D", {}).get(d_req_max_key)
        if d_req_max is not None:
            model.add(sum(x[s][d][D] for s in range(n_staff)) <= d_req_max)

        # 遅出: 平日（月〜金・祝日除く）のみ1名。土日祝は0名（ハード）
        l_count = sum(x[s][d][L] for s in range(n_staff))
        if is_weekday(dates[d], _hosp_hols):
            l_req = requirements.get("L", {}).get("weekday", 1)
            model.add(l_count >= l_req)
            model.add(l_count <= l_req)   # ちょうど l_req 名
        else:
            model.add(l_count == 0)       # 土日祝は遅出なし

        # N1 制約: [n_night_base, n_night_max]（1年目在籍かつfirst_year_plus1なら各+1）
        n1_count = sum(x[s][d][N1] for s in range(n_staff))
        first_year_in_n1 = [x[s][d][N1] for s in first_year_idx]
        any_first_year = model.new_bool_var(f"fy_n1_{d}")
        if first_year_in_n1 and n_fy_plus1:
            model.add(sum(first_year_in_n1) >= 1).only_enforce_if(any_first_year)
            model.add(sum(first_year_in_n1) == 0).only_enforce_if(any_first_year.negated())
            model.add(n1_count >= n_night_base + 1).only_enforce_if(any_first_year)
            model.add(n1_count <= n_night_max  + 1).only_enforce_if(any_first_year)
            model.add(n1_count >= n_night_base).only_enforce_if(any_first_year.negated())
            model.add(n1_count <= n_night_max ).only_enforce_if(any_first_year.negated())
        else:
            model.add(n1_count >= n_night_base)
            model.add(n1_count <= n_night_max)

        # N2 制約: 前日N1と同じ範囲
        n2_count = sum(x[s][d][N2] for s in range(n_staff))
        if d > 0:
            prev_fy_in_n1 = [x[s][d - 1][N1] for s in first_year_idx]
            if prev_fy_in_n1 and n_fy_plus1:
                any_prev_fy = model.new_bool_var(f"prev_fy_n1_{d}")
                model.add(sum(prev_fy_in_n1) >= 1).only_enforce_if(any_prev_fy)
                model.add(sum(prev_fy_in_n1) == 0).only_enforce_if(any_prev_fy.negated())
                model.add(n2_count >= n_night_base + 1).only_enforce_if(any_prev_fy)
                model.add(n2_count <= n_night_max  + 1).only_enforce_if(any_prev_fy)
                model.add(n2_count >= n_night_base).only_enforce_if(any_prev_fy.negated())
                model.add(n2_count <= n_night_max ).only_enforce_if(any_prev_fy.negated())
            else:
                model.add(n2_count >= n_night_base)
                model.add(n2_count <= n_night_max)
        else:
            model.add(n2_count >= n_night_base)
            model.add(n2_count <= n_night_max)

    # ── ソフト制約（ペナルティ） ──────────────────────────────

    penalty_terms = []

    for s in range(n_staff):
        row = staff_df.iloc[s]

        # H_consec: 連続勤務は最大5暦日（夜勤N1/N2もそれぞれ1日としてカウント）
        # 例: D D D D D N1 N2 = 7連勤 → 禁止。D D D N1 N2 = 5連勤 → 許容
        for d in range(n_days - 5):
            model.add(
                sum(x[s][d + i][k] for i in range(6) for k in [D, L, N1, N2]) <= 5
            )

        # S2: 月間勤務時間の目標からの乖離（重み: 10 per hour, 整数近似）
        target = int(row["target_hours"] * 2)  # 0.5h単位で整数化
        actual = sum(
            x[s][d][k] * int(SHIFT_HOURS[k] * 2)
            for d in range(n_days)
            for k in range(6)
        )
        diff_over  = model.new_int_var(0, 100, f"over_{s}")
        diff_under = model.new_int_var(0, 100, f"under_{s}")
        model.add(actual - target == diff_over - diff_under)
        penalty_terms.append((diff_over,  5))
        penalty_terms.append((diff_under, 5))

        # S5: 連続勤務5日以上でペナルティ（H_consecと連動してなるべく短くなるよう誘導）
        work_shifts = [D, L, N1, N2]
        for d in range(n_days - 4):
            consec_w = sum(x[s][d + i][k] for i in range(5) for k in work_shifts)
            over5 = model.new_bool_var(f"cons5_{s}_{d}")
            model.add(consec_w >= 5).only_enforce_if(over5)
            model.add(consec_w <= 4).only_enforce_if(over5.negated())
            penalty_terms.append((over5, 20))

    # S3: 毎日 日勤・遅出にリーダー1名以上（重み: 200）
    day_leader_idxs = [
        s for s in range(n_staff)
        if staff_df.iloc[s]["day_leader_ok"]
    ]
    night_leader_idxs = [
        s for s in range(n_staff)
        if staff_df.iloc[s]["night_leader_ok"]
    ]
    for d in range(n_days):
        # 日勤リーダー
        if day_leader_idxs:
            dl_count = sum(x[s][d][D] + x[s][d][L] for s in day_leader_idxs)
            dl_short = model.new_bool_var(f"dl_short_{d}")
            model.add(dl_count == 0).only_enforce_if(dl_short)
            model.add(dl_count >= 1).only_enforce_if(dl_short.negated())
            penalty_terms.append((dl_short, W_DAY_LEADER))

        # 夜勤リーダー
        if night_leader_idxs:
            nl_count = sum(x[s][d][N1] for s in night_leader_idxs)
            nl_short = model.new_bool_var(f"nl_short_{d}")
            model.add(nl_count == 0).only_enforce_if(nl_short)
            model.add(nl_count >= 1).only_enforce_if(nl_short.negated())
            penalty_terms.append((nl_short, W_NIGHT_LEADER))

    # S6: 夜勤回数のばらつきを最小化（最大−最小, 重み: 20）
    night_eligible = [
        s for s in range(n_staff)
        if staff_df.iloc[s]["night_ok"]
        and staff_df.iloc[s]["night_count_max"] > 0
    ]
    if night_eligible:
        max_nights = model.new_int_var(0, 15, "max_nights")
        min_nights = model.new_int_var(0, 15, "min_nights")
        night_totals = [sum(x[s][d][N1] for d in range(n_days)) for s in night_eligible]
        model.add_max_equality(max_nights, night_totals)
        model.add_min_equality(min_nights, night_totals)
        spread = model.new_int_var(0, 15, "night_spread")
        model.add(spread == max_nights - min_nights)
        penalty_terms.append((spread, W_NIGHT_EVENNESS))

    # S7: 夜勤の月内散らばりを促進（非専任・夜勤可能スタッフ）
    # 期間を前期・中期・後期に三分割し、各セグメントの N1 回数の最大−最小差をペナルティ化
    # 重み: 30（S6=20 より強く、S8ソフト希望=50 より弱い）
    seg1 = n_days // 3           # ~10 日
    seg2 = 2 * n_days // 3       # ~21 日
    for s in range(n_staff):
        # 専任スタッフは専用ルールがあるので除外
        if s in {ded_first_idx, ded_second_idx}:
            continue
        if not staff_df.iloc[s]["night_ok"]:
            continue
        if int(staff_df.iloc[s]["night_count_min"]) == 0:
            continue  # 夜勤なしスタッフは対象外

        c_early = sum(x[s][d][N1] for d in range(seg1))
        c_mid   = sum(x[s][d][N1] for d in range(seg1, seg2))
        c_late  = sum(x[s][d][N1] for d in range(seg2, n_days))

        seg_max = model.new_int_var(0, 8, f"seg_max_{s}")
        seg_min = model.new_int_var(0, 8, f"seg_min_{s}")
        model.add_max_equality(seg_max, [c_early, c_mid, c_late])
        model.add_min_equality(seg_min, [c_early, c_mid, c_late])
        seg_spread = model.new_int_var(0, 8, f"seg_spread_{s}")
        model.add(seg_spread == seg_max - seg_min)
        penalty_terms.append((seg_spread, W_NIGHT_SPREAD))

    # S8: 日勤帯の経験年数バランス（若い看護師だけの日を避ける）
    # 各日の D+L 勤務者のうち「経験3年以上」の人数が不足するとペナルティ
    # soft_senior_min: 日勤必要人数の約 1/3 を目安にベテランを確保
    EXP_SENIOR_THRESHOLD = 3    # 経験3年以上を「ベテラン」とみなす
    # W_EXP_BALANCE is set from soft_weights above
    senior_idxs = [
        s for s in range(n_staff)
        if int(staff_df.iloc[s]["years_exp"]) >= EXP_SENIOR_THRESHOLD
    ]
    if senior_idxs:
        req_d_wd = requirements.get("D", {}).get("weekday", 8)
        soft_senior_min = max(2, req_d_wd // 3)   # 例: 8名日勤 → 最低2名ベテラン

        for d in range(n_days):
            senior_dl = sum(
                x[s][d][D] + x[s][d][L]
                for s in senior_idxs
            )
            shortfall = model.new_int_var(0, soft_senior_min, f"exp_short_{d}")
            # shortfall = max(soft_senior_min - senior_dl, 0)
            model.add(shortfall >= soft_senior_min - senior_dl)
            model.add(shortfall >= 0)
            penalty_terms.append((shortfall, W_EXP_BALANCE))

    # S9: ソフト希望（希望シフトと異なる場合にペナルティ）
    # ソフト希望マッチングは最優先のソフト制約（重み: 50）
    for (sidx, didx), preferred_shift_idx in soft_requests.items():
        mismatch = model.new_bool_var(f"soft_mismatch_{sidx}_{didx}")
        model.add(x[sidx][didx][preferred_shift_idx] == 0).only_enforce_if(mismatch)
        model.add(x[sidx][didx][preferred_shift_idx] == 1).only_enforce_if(mismatch.negated())
        penalty_terms.append((mismatch, W_SOFT_REQ))

    # ── 目的関数 ──────────────────────────────────────────────
    model.minimize(sum(var * weight for var, weight in penalty_terms))

    # ── 求解 ──────────────────────────────────────────────────
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_sec
    solver.parameters.num_search_workers = 4
    status_code = solver.solve(model)

    STATUS_MAP = {
        cp_model.OPTIMAL:   "OPTIMAL",
        cp_model.FEASIBLE:  "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.UNKNOWN:   "UNKNOWN",
    }
    status = STATUS_MAP.get(status_code, "UNKNOWN")

    if status_code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, status, []  # 呼び出し元で analyze_infeasibility() を別途呼ぶ

    # ── 結果を DataFrame に変換 ──────────────────────────────
    records = {}
    for s in range(n_staff):
        row_data = {}
        for d, date in enumerate(dates):
            assigned = "O"
            for k, shift_name in enumerate(SHIFTS):
                if solver.value(x[s][d][k]):
                    assigned = shift_name
                    break
            # T（研修）・I（委員会）は D として解いたが、出力では元のコードに戻す
            if (s, d) in fixed_overlays:
                assigned = fixed_overlays[(s, d)]
            row_data[date] = assigned
        records[staff_ids[s]] = row_data

    schedule_df = pd.DataFrame(records, index=dates).T
    schedule_df.index.name = "staff_id"

    # ── 警告リスト（ソフト制約違反のサマリー） ────────────────
    warnings = []
    for d, date in enumerate(dates):
        day_type = "holiday" if is_holiday(date, _hosp_hols) else "weekday"
        n1_vals = [solver.value(x[s][d][N1]) for s in range(n_staff)]
        n1_total = sum(n1_vals)
        req_n1 = 4
        fy_in = any(solver.value(x[s][d][N1]) for s in first_year_idx)
        if fy_in:
            req_n1 = 5
        if n1_total < req_n1:
            warnings.append({"type": "夜勤人数不足", "date": date,
                             "actual": n1_total, "required": req_n1})

        if day_leader_idxs:
            dl = sum(solver.value(x[s][d][D]) + solver.value(x[s][d][L])
                     for s in day_leader_idxs)
            if dl == 0:
                warnings.append({"type": "日勤リーダー不在", "date": date})

        if night_leader_idxs:
            nl = sum(solver.value(x[s][d][N1]) for s in night_leader_idxs)
            if nl == 0:
                warnings.append({"type": "夜勤リーダー不在", "date": date})

    return schedule_df, status, warnings


# ── 最小矛盾確定希望の探索 ────────────────────────────────────────────────────

def _find_minimal_fixed_conflicts(
    issues: List[Dict],
    requests_df: pd.DataFrame,
    dates: list,
    sid_to_name: Dict,
    staff_df: pd.DataFrame,
    requirements: Dict,
    year: int,
    month: int,
    dedicated_first,
    dedicated_second,
    daycare_closed_dates,
    nightcare_open_dates,
    gakudo_open_dates,
    hospital_holidays,
    progress_callback,
) -> None:
    """
    確定希望を外せば解ける場合に、最小限の修正提案を issues に追加する。

    手順:
      1. 各確定希望を1件ずつソフト化して解を試みる（単独修正）
      2. 単独で解決しない場合はペア (2件) を試みる（上位 PAIR_BUDGET 件）
      3. それでも解決しない場合は「複数修正が必要」と案内する
    """
    from utils.time_utils import SHIFT_LABEL, OVERLAY_SHIFTS

    SINGLE_LIMIT = 5   # 1テスト当たり秒数
    PAIR_LIMIT   = 5
    PAIR_BUDGET  = 8   # ペア探索対象の候補上限
    FEASIBLE_STATUSES = ("OPTIMAL", "FEASIBLE")

    def _solve_with(req_df: pd.DataFrame, limit: int) -> bool:
        _, st_code, _ = solve(
            staff_df=staff_df, requests_df=req_df,
            requirements=requirements, year=year, month=month,
            dedicated_first=dedicated_first, dedicated_second=dedicated_second,
            daycare_closed_dates=daycare_closed_dates,
            nightcare_open_dates=nightcare_open_dates,
            gakudo_open_dates=gakudo_open_dates,
            hospital_holidays=hospital_holidays,
            time_limit_sec=limit,
        )
        return st_code in FEASIBLE_STATUSES

    def _label(row) -> str:
        sid  = row["staff_id"]
        name = sid_to_name.get(sid, str(sid))
        d    = row["date"]
        code = row["shift"]
        if code in OVERLAY_SHIFTS:
            code = "D"
        label = SHIFT_LABEL.get(code, code)
        return f"**{name}** {d.month}/{d.day}（{label}）確定"

    fixed_rows = requests_df[requests_df["is_fixed"] == True]
    n_fixed = len(fixed_rows)
    if n_fixed == 0:
        return

    # ── フェーズ1: 単独ソフト化テスト ──────────────────────────
    single_fixes: List[int] = []   # requests_df の index リスト
    for idx, row in fixed_rows.iterrows():
        if progress_callback:
            progress_callback(f"{_label(row)} を外して確認中…")
        test_req = requests_df.copy()
        test_req.at[idx, "is_fixed"] = False
        if _solve_with(test_req, SINGLE_LIMIT):
            single_fixes.append(idx)

    if single_fixes:
        lines = ["以下のうち **いずれか1件** を「希望（ソフト）」に変更するだけで作成できます：\n"]
        for idx in single_fixes:
            lines.append(f"- {_label(fixed_rows.loc[idx])}")
        issues.append({
            "title": "✅ 確定希望の最小修正提案（1件変更で解決）",
            "detail": "\n".join(lines),
            "severity": "high",
        })
        return

    # ── フェーズ2: ペアのソフト化テスト ────────────────────────
    # 候補を絞る（全組み合わせは時間がかかるため上位 PAIR_BUDGET 件の組み合わせのみ）
    candidates = list(fixed_rows.index[:PAIR_BUDGET])
    pair_fixes: List[Tuple[int, int]] = []
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            idx_i, idx_j = candidates[i], candidates[j]
            if progress_callback:
                li = _label(fixed_rows.loc[idx_i])
                lj = _label(fixed_rows.loc[idx_j])
                progress_callback(f"{li} ＋ {lj} を外して確認中…")
            test_req = requests_df.copy()
            test_req.at[idx_i, "is_fixed"] = False
            test_req.at[idx_j, "is_fixed"] = False
            if _solve_with(test_req, PAIR_LIMIT):
                pair_fixes.append((idx_i, idx_j))

    if pair_fixes:
        lines = ["以下のうち **いずれかの組み合わせ（2件）** を「希望（ソフト）」に変更すると作成できます：\n"]
        for idx_i, idx_j in pair_fixes[:3]:  # 最大3組まで表示
            li = _label(fixed_rows.loc[idx_i])
            lj = _label(fixed_rows.loc[idx_j])
            lines.append(f"- {li} ＋ {lj}")
        issues.append({
            "title": "✅ 確定希望の最小修正提案（2件変更で解決）",
            "detail": "\n".join(lines),
            "severity": "high",
        })
        return

    # ── フェーズ3: 3件以上の修正が必要な場合 ──────────────────
    issues.append({
        "title": "確定希望が矛盾の原因（複数の修正が必要）",
        "detail": (
            "確定希望をすべて取り除くと勤務表を作成できますが、"
            "2件以下の変更では解決できませんでした。\n\n"
            "上記の個別チェック結果を参照し、問題のある確定希望を「希望（ソフト）」に変えてください。"
        ),
        "severity": "high",
    })


# ── 不充足原因分析 ─────────────────────────────────────────────────────────────

def analyze_infeasibility(
    staff_df: pd.DataFrame,
    requests_df: pd.DataFrame,
    requirements: Dict[str, Any],
    year: int,
    month: int,
    dedicated_first: Optional[int],
    dedicated_second: Optional[int],
    daycare_closed_dates: Optional[List] = None,
    nightcare_open_dates: Optional[List] = None,
    gakudo_open_dates: Optional[List] = None,
    hospital_holidays: Optional[List] = None,
    progress_callback=None,   # Callable[[str], None] — UIへの進捗通知用
) -> List[Dict]:
    """
    INFEASIBLE 時にどの制約が原因かを分析して返す。

    静的チェック（即時）と制約緩和テスト（ソルバー再実行）を組み合わせる。

    Returns:
        [
          {
            "title": str,           # 見出し（日本語）
            "detail": str,          # 詳細説明
            "severity": "high"|"medium"|"low",
          },
          ...
        ]
    """
    from utils.time_utils import OVERLAY_SHIFTS
    from collections import Counter

    dates = schedule_dates(year, month)
    n_days = len(dates)
    sid_to_name = {row["id"]: row["name"] for _, row in staff_df.iterrows()}
    sid_to_idx  = {row["id"]: i for i, row in enumerate(staff_df.to_dict("records"))}
    _hosp_hols  = set(hospital_holidays) if hospital_holidays else set()

    issues: List[Dict] = []
    FEASIBLE_STATUSES = ("OPTIMAL", "FEASIBLE")
    SHORT_LIMIT = 10   # 緩和テスト1回あたり最大10秒

    # ──────────────────────────────────────────────────
    # ① 静的チェック（ルールベース・即時）
    # ──────────────────────────────────────────────────

    def _static():
        # 確定希望を (sidx, didx) → shift_code に変換
        f_shift: Dict[Tuple[int, int], str] = {}
        for _, row in requests_df.iterrows():
            if not row["is_fixed"]:
                continue
            d = row["date"]
            if d not in dates:
                continue
            sid = row["staff_id"]
            if sid not in sid_to_idx:
                continue
            sidx = sid_to_idx[sid]
            didx = dates.index(d)
            code = row["shift"]
            if code in OVERLAY_SHIFTS:
                code = "D"
            f_shift[(sidx, didx)] = code

        # 1-a. N1 → 翌日 N2 ルール違反
        for (sidx, didx), code in f_shift.items():
            if code != "N1":
                continue
            nxt = didx + 1
            if nxt < n_days:
                nxt_code = f_shift.get((sidx, nxt))
                if nxt_code is not None and nxt_code != "N2":
                    name = sid_to_name.get(
                        staff_df.iloc[sidx]["id"], f"staff#{sidx}")
                    issues.append({
                        "title": "確定希望：夜勤連続ルール違反",
                        "detail": (
                            f"**{name}** の {dates[didx]:%m/%d}（ヤ1）確定の翌日 "
                            f"{dates[nxt]:%m/%d} に **{nxt_code}** が確定されています。"
                            f"ヤ1の翌日はヤ2でなければなりません。"
                        ),
                        "severity": "high",
                    })

        # 1-b. N2 → 翌日 N1 or O ルール違反
        for (sidx, didx), code in f_shift.items():
            if code != "N2":
                continue
            nxt = didx + 1
            if nxt < n_days:
                nxt_code = f_shift.get((sidx, nxt))
                if nxt_code is not None and nxt_code not in ("N1", "O", "P"):
                    name = sid_to_name.get(
                        staff_df.iloc[sidx]["id"], f"staff#{sidx}")
                    issues.append({
                        "title": "確定希望：夜勤明け翌日ルール違反",
                        "detail": (
                            f"**{name}** の {dates[didx]:%m/%d}（ヤ2）確定の翌日 "
                            f"{dates[nxt]:%m/%d} に **{nxt_code}** が確定されています。"
                            f"ヤ2の翌日はヤ1か休みでなければなりません。"
                        ),
                        "severity": "high",
                    })

        # 1-c. 夜勤可能スタッフ数 vs 毎日の必要夜勤人数
        night_ok_count = int((staff_df["night_ok"] == True).sum())
        _n_cfg = requirements.get("N", {})
        n_base = int(_n_cfg.get("base", 4))
        # N1+N2 で2倍の人数が必要（一人が連続2日占有）
        min_needed = n_base * 2
        if night_ok_count < min_needed:
            issues.append({
                "title": "夜勤可能スタッフ数が不足",
                "detail": (
                    f"夜勤可能スタッフが **{night_ok_count} 名** です。"
                    f"毎日 ヤ1 {n_base} 名・ヤ2 {n_base} 名を確保するには"
                    f"最低 **{min_needed} 名** 必要です。"
                    f"「必要夜勤人数（基本）」を下げるか、夜勤可能なスタッフを増やしてください。"
                ),
                "severity": "high",
            })

        # 1-d. 同一日に同じスタッフへ矛盾する確定希望（N1 と非N2 が翌日に存在する）
        # （上の1-aで既に検出済みなのでここでは追加チェックのみ）

        # 1-e. 夜勤不可スタッフの N1/N2 確定希望
        staff_night_ok = {
            sid_to_idx[row["id"]]: bool(row["night_ok"])
            for _, row in staff_df.iterrows()
            if row["id"] in sid_to_idx
        }
        for (sidx, didx), code in f_shift.items():
            if code in ("N1", "N2") and not staff_night_ok.get(sidx, True):
                name = sid_to_name.get(
                    staff_df.iloc[sidx]["id"], f"staff#{sidx}")
                issues.append({
                    "title": "確定希望：夜勤不可スタッフへの夜勤固定",
                    "detail": (
                        f"**{name}** は夜勤不可に設定されていますが、"
                        f"{dates[didx]:%m/%d} に {code} が確定希望として登録されています。"
                    ),
                    "severity": "high",
                })

        # 1-f. スタッフの夜勤 min/max 設定が論理的に矛盾
        for _, row in staff_df.iterrows():
            if not row["night_ok"]:
                continue
            nm = int(row["night_count_min"])
            mx = int(row["night_count_max"])
            if nm > mx:
                name = sid_to_name.get(row["id"], str(row["id"]))
                issues.append({
                    "title": "スタッフ設定：夜勤回数 min > max",
                    "detail": (
                        f"**{name}** の夜勤回数設定が"
                        f"最小 {nm} 回 > 最大 {mx} 回 になっています。"
                        f"スタッフ設定を修正してください。"
                    ),
                    "severity": "high",
                })

    _static()

    # ──────────────────────────────────────────────────
    # ② 制約緩和テスト（ソルバー再実行）
    # ──────────────────────────────────────────────────

    def _try(label: str, **kwargs) -> bool:
        """緩和パラメータで解が見つかるか試す。見つかれば True。"""
        if progress_callback:
            progress_callback(label)
        params = dict(
            staff_df=staff_df, requests_df=requests_df,
            requirements=requirements, year=year, month=month,
            dedicated_first=dedicated_first, dedicated_second=dedicated_second,
            daycare_closed_dates=daycare_closed_dates,
            nightcare_open_dates=nightcare_open_dates,
            gakudo_open_dates=gakudo_open_dates,
            hospital_holidays=hospital_holidays,
            time_limit_sec=SHORT_LIMIT,
        )
        params.update(kwargs)
        _, st_code, _ = solve(**params)
        return st_code in FEASIBLE_STATUSES

    _n_cfg = requirements.get("N", {})
    n_base = int(_n_cfg.get("base", 4))
    night_ok_count = int((staff_df["night_ok"] == True).sum())

    # テスト A: 確定希望をすべて外す → 通れば最小修正セットを探索
    empty_req = pd.DataFrame(columns=["staff_id", "date", "shift", "is_fixed"])
    if _try("確定希望なしで再試行中…", requests_df=empty_req):
        _find_minimal_fixed_conflicts(
            issues, requests_df, dates, sid_to_name,
            staff_df, requirements, year, month,
            dedicated_first, dedicated_second,
            daycare_closed_dates, nightcare_open_dates,
            gakudo_open_dates,
            hospital_holidays, progress_callback,
        )
    else:
        # テスト B: 夜勤人数要件を緩和
        relaxed_n = {
            **requirements,
            "N": {"base": 1, "max": night_ok_count,
                  "first_year_plus1": False},
        }
        if _try("夜勤人数要件を緩和して再試行中…", requirements=relaxed_n):
            issues.append({
                "title": "夜勤必要人数の設定が厳しすぎる可能性",
                "detail": (
                    f"夜勤必要人数の設定を緩和すると勤務表を作成できます。"
                    f"現在の設定（基本 **{n_base} 名**）に対して、"
                    f"夜勤可能スタッフ数（{night_ok_count} 名）や確定希望が不足しています。"
                    f"「必要夜勤人数（基本）」を下げるか、"
                    f"「1年目+1」設定を外すことを検討してください。"
                ),
                "severity": "high",
            })

        # テスト C: 日勤・遅出人数要件を緩和
        relaxed_d = {
            **requirements,
            "D": {"weekday": 1, "holiday": 1},
            "L": {"weekday": 0, "holiday": 0},
        }
        if _try("日勤・遅出人数要件を緩和して再試行中…", requirements=relaxed_d):
            issues.append({
                "title": "日勤・遅出の必要人数が確保できない日がある",
                "detail": (
                    "日勤・遅出の必要人数設定を下げると勤務表を作成できます。"
                    "確定希望や夜勤配置・保育園制約により、"
                    "特定の日に日勤・遅出スタッフが不足しています。"
                    "日勤必要人数の設定を下げるか、確定希望を調整してください。"
                ),
                "severity": "medium",
            })

    # テスト D: 保育園休園日の制約を外す
    if daycare_closed_dates:
        if _try("保育園制約を外して再試行中…", daycare_closed_dates=None):
            closed_str = "、".join(
                f"{d.month}/{d.day}"
                for d in sorted(daycare_closed_dates)[:5]
            )
            issues.append({
                "title": "保育園休園日の制約が影響している",
                "detail": (
                    f"保育園休園日（{closed_str}…）の制約を外すと勤務表を作成できます。"
                    f"保育園利用スタッフが休園日に出勤できないため、"
                    f"その日の人員が不足しています。"
                    f"休園日の設定を見直すか、確定希望を調整してください。"
                ),
                "severity": "medium",
            })

    # テスト E: 夜間保育の制約を外す
    if nightcare_open_dates is not None:
        if _try("夜間保育制約を外して再試行中…", nightcare_open_dates=None):
            issues.append({
                "title": "夜間保育の受け入れ日設定が影響している",
                "detail": (
                    "夜間保育受け入れ日の制約を外すと勤務表を作成できます。"
                    "夜間保育利用スタッフが夜勤できる日が少なすぎます。"
                    "夜間保育受け入れ日を追加するか、該当スタッフの夜勤設定を調整してください。"
                ),
                "severity": "medium",
            })

    # テスト E2: 夜間学童の制約を外す
    if gakudo_open_dates is not None:
        if _try("夜間学童制約を外して再試行中…", gakudo_open_dates=None):
            issues.append({
                "title": "夜間学童の受け入れ日設定が影響している",
                "detail": (
                    "夜間学童受け入れ日の制約を外すと勤務表を作成できます。"
                    "夜間学童利用スタッフが夜勤できる日が少なすぎます。"
                    "夜間学童受け入れ日を追加するか、該当スタッフの夜勤設定を調整してください。"
                ),
                "severity": "medium",
            })

    # テスト E: 夜勤専任設定を外す
    if dedicated_first is not None or dedicated_second is not None:
        if _try("夜勤専任設定を外して再試行中…",
                dedicated_first=None, dedicated_second=None):
            names = []
            if dedicated_first and dedicated_first in sid_to_name:
                names.append(f"{sid_to_name[dedicated_first]}（前半）")
            if dedicated_second and dedicated_second in sid_to_name:
                names.append(f"{sid_to_name[dedicated_second]}（後半）")
            issues.append({
                "title": "夜勤専任設定が矛盾を引き起こしている",
                "detail": (
                    f"夜勤専任（{' / '.join(names)}）の設定を外すと勤務表を作成できます。"
                    f"専任スタッフが日勤・遅出から除外されることで、"
                    f"特定の日の日勤人員が不足しています。"
                ),
                "severity": "medium",
            })

    # どれにも引っかからなかった場合
    if not issues:
        issues.append({
            "title": "原因を自動特定できませんでした",
            "detail": (
                "制約の組み合わせが複雑で、自動分析では原因を絞り込めませんでした。"
                "以下を手動で確認してください：\n"
                "- スタッフの夜勤 最小/最大回数設定\n"
                "- 確定希望の内容（特に夜勤関連）\n"
                "- 必要人数と実際の夜勤可能スタッフ数のバランス"
            ),
            "severity": "low",
        })

    return issues
