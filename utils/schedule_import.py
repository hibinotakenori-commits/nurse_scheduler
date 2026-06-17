"""前月勤務表の取り込み（Excel・画像）。"""
import base64
import datetime
import io
import json
import re
from typing import Dict, List, Optional, Tuple

import pandas as pd


# ── シフト記号の正規化マップ ──────────────────────────────────
# 表示ラベル → 内部コード
_LABEL_TO_CODE = {
    # 日勤
    "日": "D", "D": "D", "d": "D",
    # 遅出
    "遅": "L", "L": "L", "l": "L", "遅出": "L",
    # 夜勤
    "ヤ1": "N1", "夜1": "N1", "N1": "N1", "n1": "N1", "夜勤1": "N1",
    "ヤ2": "N2", "夜2": "N2", "N2": "N2", "n2": "N2", "夜勤2": "N2", "明": "N2",
    # 休み
    "休": "O", "O": "O", "o": "O", "公休": "O", "有": "O", "有休": "O",
    "研修": "O", "/イ": "O", "": "O",
}

WORK_CODES = {"D", "L", "N1", "N2"}


def normalize_shift(raw: str) -> str:
    """表示シフト文字列を内部コードに変換する。"""
    if not isinstance(raw, str):
        return "O"
    v = raw.strip()
    return _LABEL_TO_CODE.get(v, "O")


# ── Excel 取り込み ────────────────────────────────────────────

def _name_similarity(a: str, b: str) -> int:
    """スペース除去後の共通文字数（簡易マッチ）。"""
    a2 = a.replace(" ", "").replace("　", "")
    b2 = b.replace(" ", "").replace("　", "")
    return sum(1 for c in a2 if c in b2)


def _match_name(name: str, staff_df: pd.DataFrame) -> Optional[int]:
    """スタッフ名から staff_id を返す（None = 不一致）。"""
    name_clean = name.strip().replace(" ", "").replace("　", "")
    if not name_clean:
        return None
    best_id, best_score = None, 0
    for _, row in staff_df.iterrows():
        cand = str(row["name"]).replace(" ", "").replace("　", "")
        if name_clean == cand:
            return int(row["id"])
        score = _name_similarity(name_clean, cand)
        if score > best_score and score >= 2:
            best_score = score
            best_id = int(row["id"])
    return best_id


def parse_excel(
    file_bytes: bytes,
    staff_df: pd.DataFrame,
    year: int,
    month: int,
) -> Tuple[Optional[Dict[int, Dict[datetime.date, str]]], List[str]]:
    """
    Excel ファイルから勤務表を解析する。

    Returns:
        (schedule_dict, warnings)
        schedule_dict: {staff_id: {date: shift_code}}
    """
    warnings: List[str] = []
    try:
        xl = pd.ExcelFile(io.BytesIO(file_bytes), engine="openpyxl")
        sheet_name = xl.sheet_names[0]
        raw = xl.parse(sheet_name, header=None)
    except Exception as e:
        return None, [f"Excel の読み込みに失敗しました: {e}"]

    # ── ヘッダー行（日付が並んでいる行）を探す ──
    date_row_idx = None
    date_col_start = None
    name_col = None

    for ri in range(min(10, len(raw))):
        date_cols = []
        for ci in range(len(raw.columns)):
            cell = raw.iloc[ri, ci]
            if isinstance(cell, (datetime.datetime, datetime.date)):
                date_cols.append(ci)
            elif isinstance(cell, (int, float)) and 1 <= int(cell) <= 31:
                date_cols.append(ci)
        if len(date_cols) >= 10:
            date_row_idx = ri
            date_col_start = date_cols[0]
            # 名前列は日付列より左にある最初の列と仮定
            name_col = date_col_start - 1 if date_col_start > 0 else 0
            break

    if date_row_idx is None:
        return None, ["日付行が見つかりませんでした。1行目〜10行目に日付（1〜31）が並んでいることを確認してください。"]

    # ── 日付の特定 ──
    date_map: Dict[int, datetime.date] = {}  # col_index → date
    for ci in range(date_col_start, len(raw.columns)):
        cell = raw.iloc[date_row_idx, ci]
        if isinstance(cell, datetime.datetime):
            date_map[ci] = cell.date()
        elif isinstance(cell, datetime.date):
            date_map[ci] = cell
        elif isinstance(cell, (int, float)) and 1 <= int(cell) <= 31:
            try:
                date_map[ci] = datetime.date(year, month, int(cell))
            except ValueError:
                pass

    if not date_map:
        return None, ["日付列の解析に失敗しました。"]

    # ── スタッフ行を解析 ──
    schedule: Dict[int, Dict[datetime.date, str]] = {}
    unmatched: List[str] = []

    for ri in range(date_row_idx + 1, len(raw)):
        name_cell = raw.iloc[ri, name_col]
        if not isinstance(name_cell, str) or not name_cell.strip():
            continue

        staff_id = _match_name(name_cell, staff_df)
        if staff_id is None:
            unmatched.append(name_cell.strip())
            continue

        row_shifts: Dict[datetime.date, str] = {}
        for ci, date in date_map.items():
            cell = raw.iloc[ri, ci]
            shift = normalize_shift(str(cell) if not pd.isna(cell) else "")
            row_shifts[date] = shift

        if row_shifts:
            schedule[staff_id] = row_shifts

    if unmatched:
        warnings.append(f"名前が一致しなかったスタッフ: {', '.join(unmatched)}")
    if not schedule:
        return None, ["スタッフデータが取得できませんでした。"] + warnings

    return schedule, warnings


# ── 画像取り込み（Claude vision） ────────────────────────────

def parse_image(
    file_bytes: bytes,
    mime_type: str,
    staff_df: pd.DataFrame,
    year: int,
    month: int,
) -> Tuple[Optional[Dict[int, Dict[datetime.date, str]]], List[str]]:
    """
    画像から Claude vision API で勤務表を解析する。

    Returns:
        (schedule_dict, warnings)
    """
    try:
        import anthropic
    except ImportError:
        return None, ["anthropic ライブラリがインストールされていません。"]

    staff_names = staff_df["name"].tolist()
    names_str = "、".join(staff_names)

    prompt = f"""この画像は{year}年{month}月の看護師勤務表です。
スタッフ一覧（参考）: {names_str}

勤務表から以下の形式のJSONを抽出してください。
キー: スタッフ名、値: {{日付(YYYY-MM-DD): シフト記号}} の辞書

シフト記号の変換ルール:
- 日勤（日、D）→ "D"
- 遅出（遅、L）→ "L"
- 夜勤1（ヤ1、夜1、N1）→ "N1"
- 夜勤2（ヤ2、夜2、N2、明）→ "N2"
- 休み・有休・公休・その他 → "O"

出力はJSONのみ（説明不要）:
{{"スタッフ名": {{"YYYY-MM-DD": "シフト記号", ...}}, ...}}"""

    try:
        client = anthropic.Anthropic()
        b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        text = message.content[0].text.strip()
    except Exception as e:
        return None, [f"Claude API エラー: {e}"]

    # JSON 抽出
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if not json_match:
        return None, [f"JSON の抽出に失敗しました。Claude の応答: {text[:200]}"]

    try:
        raw_data = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        return None, [f"JSON のパースに失敗しました: {e}"]

    schedule: Dict[int, Dict[datetime.date, str]] = {}
    warnings: List[str] = []
    unmatched: List[str] = []

    for name, date_shifts in raw_data.items():
        staff_id = _match_name(name, staff_df)
        if staff_id is None:
            unmatched.append(name)
            continue
        row: Dict[datetime.date, str] = {}
        for date_str, shift_raw in date_shifts.items():
            try:
                d = datetime.date.fromisoformat(date_str)
                row[d] = normalize_shift(shift_raw)
            except ValueError:
                pass
        if row:
            schedule[staff_id] = row

    if unmatched:
        warnings.append(f"名前が一致しなかったスタッフ: {', '.join(unmatched)}")
    if not schedule:
        return None, ["スタッフデータが取得できませんでした。"] + warnings

    return schedule, warnings


# ── schedule_dict → DataFrame ────────────────────────────────

def schedule_dict_to_df(
    schedule: Dict[int, Dict[datetime.date, str]],
    staff_df: pd.DataFrame,
) -> pd.DataFrame:
    """取り込んだ schedule_dict を表示用 DataFrame に変換する。"""
    all_dates = sorted({d for shifts in schedule.values() for d in shifts.keys()})
    all_ids = sorted(schedule.keys())
    df = pd.DataFrame(index=all_ids, columns=all_dates, dtype=object)
    for sid, shifts in schedule.items():
        for d, s in shifts.items():
            df.at[sid, d] = s
    df = df.fillna("O")

    # スタッフ名を付与
    id_to_name = dict(zip(staff_df["id"], staff_df["name"]))
    df.index = [id_to_name.get(i, str(i)) for i in df.index]
    return df
