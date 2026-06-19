"""アプリ設定の永続化（settings.json への保存・読込）。"""
import datetime
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

# ── デフォルト値 ─────────────────────────────────────────────
DEFAULT_SOFT_WEIGHTS: Dict[str, int] = {
    "soft_req":       5,   # ソフト希望の尊重
    "exp_balance":    4,   # 経験年数バランス
    "night_spread":   3,   # 夜勤の月内散らばり
    "night_evenness": 2,   # スタッフ間の夜勤回数均等化
    "day_leader":     7,   # 日勤リーダー確保
    "night_leader":   6,   # 夜勤リーダー確保
}

DEFAULT_REQUIREMENTS: Dict[str, Any] = {
    "D": {"weekday": 8, "weekday_max": 20, "holiday": 6, "holiday_max": 20},
    "L": {"weekday": 1, "holiday": 1},
    "N": {"base": 4, "max": 5, "first_year_plus1": True},
}

# 保育園利用区分の選択肢
DAYCARE_TYPE_OPTIONS = ["none", "day", "night"]
DAYCARE_TYPE_LABELS  = {"none": "利用なし", "day": "日中のみ", "night": "夜間保育あり"}


def _ward_dir(ward: str) -> Path:
    """病棟別データディレクトリを返す（なければ作成）。"""
    d = Path(__file__).parent.parent / "data" / ward
    d.mkdir(parents=True, exist_ok=True)
    return d


def _common_dir() -> Path:
    """院内共通設定ディレクトリを返す（なければ作成）。"""
    d = Path(__file__).parent.parent / "data" / "common"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_common_settings() -> Dict[str, Any]:
    """
    院内共通設定（休日・保育園・夜間保育・学童）を data/common/hospital_settings.json から読む。
    なければ 3A の settings.json から移行する。
    """
    path = _common_dir() / "hospital_settings.json"

    # 3A の settings.json から自動マイグレーション
    if not path.exists():
        src = Path(__file__).parent.parent / "data" / "3A" / "settings.json"
        if not src.exists():
            src = Path(__file__).parent.parent / "settings.json"
        if src.exists():
            try:
                with open(src, "r", encoding="utf-8") as f:
                    _s = json.load(f)
                _migrate = {
                    "hospital_holidays": _s.get("hospital_holidays", []),
                    "daycare_closed":    _s.get("daycare_closed", []),
                    "nightcare_open":    _s.get("nightcare_open", []),
                    "gakudo_open":       _s.get("gakudo_open", []),
                }
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(_migrate, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    _default: Dict[str, Any] = {
        "hospital_holidays": [],
        "daycare_closed":    [],
        "nightcare_open":    [],
        "gakudo_open":       [],
        "target_year":       None,
        "target_month":      None,
    }
    if not path.exists():
        return _default

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return _default

    return {
        "hospital_holidays": [datetime.date.fromisoformat(s) for s in data.get("hospital_holidays", [])],
        "daycare_closed":    [datetime.date.fromisoformat(s) for s in data.get("daycare_closed", [])],
        "nightcare_open":    [datetime.date.fromisoformat(s) for s in data.get("nightcare_open", [])],
        "gakudo_open":       [datetime.date.fromisoformat(s) for s in data.get("gakudo_open", [])],
        "target_year":       data.get("target_year"),
        "target_month":      data.get("target_month"),
    }


def save_common_settings(
    hospital_holidays: List[datetime.date],
    daycare_closed: List[datetime.date],
    nightcare_open: List[datetime.date],
    gakudo_open: List[datetime.date],
    target_year: Optional[int] = None,
    target_month: Optional[int] = None,
) -> None:
    """院内共通設定を data/common/hospital_settings.json に保存する。"""
    path = _common_dir() / "hospital_settings.json"
    # 既存データを読んでマージ（target_year/month だけ更新するケースに対応）
    existing: Dict[str, Any] = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass
    data = {
        "hospital_holidays": [d.isoformat() for d in (hospital_holidays or [])],
        "daycare_closed":    [d.isoformat() for d in (daycare_closed or [])],
        "nightcare_open":    [d.isoformat() for d in (nightcare_open or [])],
        "gakudo_open":       [d.isoformat() for d in (gakudo_open or [])],
        "target_year":       target_year  if target_year  is not None else existing.get("target_year"),
        "target_month":      target_month if target_month is not None else existing.get("target_month"),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_settings(ward: str = "3A") -> Dict[str, Any]:
    """
    data/{ward}/settings.json を読み込む。
    ファイルがなく settings.json（ルート）が存在する場合は自動コピー（3A のみ）。

    Returns:
        {
            "requirements": dict,
            "soft_weights": dict,
            "hospital_holidays": List[datetime.date],
            "daycare_closed": List[datetime.date],
            "nightcare_open": List[datetime.date],
            "gakudo_open": List[datetime.date],
            "staff": List[dict] | None,
        }
    """
    settings_path = _ward_dir(ward) / "settings.json"

    # 3A 後方互換：ルートの settings.json を自動マイグレーション
    if not settings_path.exists() and ward == "3A":
        root_settings = Path(__file__).parent.parent / "settings.json"
        if root_settings.exists():
            shutil.copy2(root_settings, settings_path)

    _default = {
        "requirements":    _deep_copy_req(DEFAULT_REQUIREMENTS),
        "soft_weights":    dict(DEFAULT_SOFT_WEIGHTS),
        "hospital_holidays": [],
        "daycare_closed":  [],
        "nightcare_open":  [],
        "gakudo_open":     [],
        "hard_constraints":  [],
        "soft_constraints":  [],
        "staff":           None,
    }

    if not settings_path.exists():
        return _default

    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return _default

    # requirements の後方互換
    req = data.get("requirements", {})
    d_cfg = req.get("D", {})
    d_cfg.setdefault("weekday_max", DEFAULT_REQUIREMENTS["D"]["weekday_max"])
    d_cfg.setdefault("holiday_max", DEFAULT_REQUIREMENTS["D"]["holiday_max"])
    req["D"] = d_cfg

    # 日付文字列 → datetime.date
    data["hospital_holidays"] = [
        datetime.date.fromisoformat(s)
        for s in data.get("hospital_holidays", [])
    ]
    data["daycare_closed"] = [
        datetime.date.fromisoformat(s)
        for s in data.get("daycare_closed", [])
    ]
    data["nightcare_open"] = [
        datetime.date.fromisoformat(s)
        for s in data.get("nightcare_open", [])
    ]
    data["gakudo_open"] = [
        datetime.date.fromisoformat(s)
        for s in data.get("gakudo_open", [])
    ]
    data.setdefault("staff", None)
    data.setdefault("target_year", None)
    data.setdefault("target_month", None)
    data.setdefault("system_constraint_priorities", {})
    data.setdefault("user_constraints", [])
    # 旧フォーマット後方互換（hard_constraints/soft_constraints → user_constraints へ移行）
    if not data["user_constraints"]:
        old_hard = data.pop("hard_constraints", [])
        old_soft = data.pop("soft_constraints", [])
        migrated = [{"text": t, "priority": 5} for t in old_hard] + \
                   [{"text": t, "priority": 3} for t in old_soft]
        if migrated:
            data["user_constraints"] = migrated

    # soft_weights の後方互換
    sw = data.get("soft_weights", {})
    for k, v in DEFAULT_SOFT_WEIGHTS.items():
        sw.setdefault(k, v)
    data["soft_weights"] = sw

    return data


def save_settings(
    requirements: Dict[str, Any],
    soft_weights: Dict[str, int],
    hospital_holidays: List[datetime.date],
    daycare_closed: List[datetime.date],
    nightcare_open: List[datetime.date],
    staff_df: pd.DataFrame,
    target_year: Optional[int] = None,
    target_month: Optional[int] = None,
    gakudo_open: Optional[List[datetime.date]] = None,
    # 旧パラメータ（後方互換のため残す）
    hard_constraints: Optional[List[str]] = None,
    soft_constraints: Optional[List[str]] = None,
    # 新パラメータ
    system_constraint_priorities: Optional[Dict[str, int]] = None,
    user_constraints: Optional[List[dict]] = None,
    ward: str = "3A",
) -> None:
    """現在の設定を data/{ward}/settings.json に保存する。"""
    settings_path = _ward_dir(ward) / "settings.json"

    staff_records = []
    for r in staff_df.to_dict(orient="records"):
        cleaned = {}
        for k, v in r.items():
            if hasattr(v, "item"):
                cleaned[k] = v.item()
            elif isinstance(v, bool):
                cleaned[k] = bool(v)
            else:
                cleaned[k] = v
        staff_records.append(cleaned)

    data = {
        "version": 1,
        "requirements":    requirements,
        "soft_weights":    soft_weights,
        "hospital_holidays": [d.isoformat() for d in (hospital_holidays or [])],
        "daycare_closed":  [d.isoformat() for d in (daycare_closed or [])],
        "nightcare_open":  [d.isoformat() for d in (nightcare_open or [])],
        "gakudo_open":                   [d.isoformat() for d in (gakudo_open or [])],
        "system_constraint_priorities":  dict(system_constraint_priorities or {}),
        "user_constraints":              list(user_constraints or []),
        "staff": staff_records,
    }

    if target_year is not None:
        data["target_year"] = target_year
    if target_month is not None:
        data["target_month"] = target_month

    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _empty_staff_df() -> pd.DataFrame:
    """スタッフ未登録の空 DataFrame を返す。"""
    import pandas as pd
    cols = ["id", "name", "years_exp", "night_ok", "day_leader_ok", "night_leader_ok",
            "night_count_min", "night_count_max", "target_hours", "daycare_type",
            "nightcare_required", "nightcare_no_night", "gakudo", "gakudo_required",
            "order", "active"]
    return pd.DataFrame(columns=cols).astype({
        "id": int, "years_exp": int, "night_count_min": int, "night_count_max": int,
        "order": int, "target_hours": float,
        "night_ok": bool, "day_leader_ok": bool, "night_leader_ok": bool,
        "nightcare_required": bool, "nightcare_no_night": bool,
        "gakudo": bool, "gakudo_required": bool, "active": bool,
    })


def staff_df_from_settings(data: Dict[str, Any], ward: str = "3A") -> pd.DataFrame:
    """
    settings データから staff DataFrame を復元する。
    staff が None または空の場合、3A はデフォルトスタッフを返す。それ以外は空 DataFrame。
    """
    from utils.staff_data import load_staff

    staff_list: Optional[List[dict]] = data.get("staff")
    if not staff_list:
        return load_staff() if ward == "3A" else _empty_staff_df()

    df = pd.DataFrame(staff_list)

    # 型の整合
    int_cols   = ["id", "years_exp", "night_count_min", "night_count_max", "order"]
    float_cols = ["target_hours"]
    bool_cols  = ["night_ok", "day_leader_ok", "night_leader_ok"]

    for c in int_cols:
        if c in df.columns:
            df[c] = df[c].fillna(0).astype(int)
    if "order" not in df.columns:
        df["order"] = range(1, len(df) + 1)
    for c in bool_cols:
        if c in df.columns:
            df[c] = df[c].fillna(False).astype(bool)
        else:
            df[c] = False
    for c in float_cols:
        if c in df.columns:
            df[c] = df[c].fillna(170.0).astype(float)

    # 後方互換: 旧 daycare bool → daycare_type 文字列
    if "daycare_type" not in df.columns:
        if "daycare" in df.columns:
            df["daycare_type"] = df["daycare"].apply(
                lambda v: "day" if bool(v) else "none"
            )
            df.drop(columns=["daycare"], inplace=True)
        else:
            df["daycare_type"] = "none"
    else:
        df["daycare_type"] = df["daycare_type"].fillna("none").astype(str)

    # 後方互換: nightcare_required がなければデフォルト False
    if "nightcare_required" not in df.columns:
        df["nightcare_required"] = False
    else:
        df["nightcare_required"] = df["nightcare_required"].fillna(False).astype(bool)

    # 後方互換: gakudo / gakudo_required がなければデフォルト False
    if "gakudo" not in df.columns:
        df["gakudo"] = False
    else:
        df["gakudo"] = df["gakudo"].fillna(False).astype(bool)

    if "gakudo_required" not in df.columns:
        df["gakudo_required"] = False
    else:
        df["gakudo_required"] = df["gakudo_required"].fillna(False).astype(bool)

    if "active" not in df.columns:
        df["active"] = True
    else:
        df["active"] = df["active"].fillna(True).astype(bool)

    if "nightcare_no_night" not in df.columns:
        df["nightcare_no_night"] = False
    else:
        df["nightcare_no_night"] = df["nightcare_no_night"].fillna(False).astype(bool)

    return df


# ── 勤務希望の保存・読み込み ──────────────────────────────────

def save_requests(requests_df: pd.DataFrame, ward: str = "3A") -> None:
    """requests_df を data/{ward}/requests.json に保存する。"""
    requests_path = _ward_dir(ward) / "requests.json"
    records = []
    for r in requests_df.to_dict(orient="records"):
        cleaned = {}
        for k, v in r.items():
            if k == "date":
                cleaned[k] = v.isoformat() if hasattr(v, "isoformat") else str(v)
            elif hasattr(v, "item"):
                cleaned[k] = v.item()
            elif isinstance(v, bool):
                cleaned[k] = bool(v)
            else:
                cleaned[k] = v
        records.append(cleaned)
    with open(requests_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def load_requests(ward: str = "3A") -> pd.DataFrame:
    """data/{ward}/requests.json から requests_df を復元する。ファイルがなければ空 DataFrame を返す。"""
    requests_path = _ward_dir(ward) / "requests.json"
    empty = pd.DataFrame(columns=["staff_id", "date", "shift", "is_fixed"])

    # 3A 後方互換：ルートの requests.json を自動マイグレーション
    if not requests_path.exists() and ward == "3A":
        root_requests = Path(__file__).parent.parent / "requests.json"
        if root_requests.exists():
            shutil.copy2(root_requests, requests_path)

    if not requests_path.exists():
        return empty
    try:
        with open(requests_path, "r", encoding="utf-8") as f:
            records = json.load(f)
        if not records:
            return empty
        df = pd.DataFrame(records)
        df["date"]     = pd.to_datetime(df["date"]).dt.date
        df["staff_id"] = df["staff_id"].astype(int)
        df["is_fixed"] = df["is_fixed"].fillna(False).astype(bool)
        df["shift"]    = df["shift"].astype(str)
        return df.reset_index(drop=True)
    except Exception:
        return empty


# ── 内部ユーティリティ ────────────────────────────────────────

def _deep_copy_req(req: Dict) -> Dict:
    import copy
    return copy.deepcopy(req)
