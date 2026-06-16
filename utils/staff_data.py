import pandas as pd

STAFF_LIST = [
    # (name, years_exp, night_ok, day_leader_ok, night_leader_ok, night_count_min, night_count_max)
    ("小島 美保",    20, True,  True,  True,  3, 6),
    ("秋田 理沙",    10, True,  True,  True,  1, 1),
    ("西 優希",       5, True,  True,  False, 3, 6),
    ("阿部 真子",     4, True,  False, False, 3, 6),
    ("若佐 茉奈実",  13, True,  True,  True,  1, 1),
    ("新井 新之助",   6, True,  True,  True,  3, 6),
    ("二川 優",      12, True,  True,  True,  3, 6),
    ("古屋 裕美",    10, True,  False, False, 3, 6),
    ("杉田 ゆかり",  11, False, True,  False, 0, 0),
    ("濱 綾乃",       6, True,  True,  True,  3, 6),
    ("森澤 利江",    10, True,  True,  True,  3, 6),
    ("小浦方 萌夏",   5, True,  True,  True,  3, 6),
    ("久保 朋代",     8, True,  True,  True,  1, 1),
    ("浦 順彦",       6, True,  True,  True,  3, 6),
    ("武居 佳苗",     4, True,  False, False, 3, 6),
    ("河野 朱里",     7, True,  True,  True,  3, 6),
    ("奥山 恵理",     5, True,  True,  False, 3, 6),
    ("本田 琴音",     3, True,  False, False, 3, 6),
    ("岡本 真琴",     8, True,  True,  True,  3, 6),
    ("佐藤 大翔",     3, True,  False, False, 3, 6),
    ("吉山 優香",     6, True,  False, False, 3, 6),
    ("石川 沙弥加",   3, True,  False, False, 3, 6),
    ("井上 葉新",     6, True,  False, False, 3, 6),
    ("二本木 莉奈",   2, True,  False, False, 3, 6),
    ("大畠 優美花",  12, True,  True,  True,  3, 6),
    ("加藤 優来",     2, True,  False, False, 3, 6),
    ("倉橋 智哉",     5, True,  False, False, 3, 6),
    ("森山 優",       7, True,  False, False, 3, 6),
    ("佐々木 真菜",   5, True,  False, False, 3, 6),
    ("伊藤 磨理歩",   5, True,  False, False, 3, 6),
    ("信田奈緒",      1, True,  False, False, 3, 6),
    ("岡崎 愛莉",     1, True,  False, False, 3, 6),
    ("藤澤愛花",      1, True,  False, False, 3, 6),
    ("三留愛子",      1, True,  False, False, 3, 6),
    ("高浜 朱璃",     1, True,  False, False, 3, 6),
]


def load_staff() -> pd.DataFrame:
    df = pd.DataFrame(STAFF_LIST, columns=[
        "name", "years_exp", "night_ok",
        "day_leader_ok", "night_leader_ok",
        "night_count_min", "night_count_max",
    ])
    df.insert(0, "id", range(1, len(df) + 1))
    df["target_hours"] = 170.0
    # 保育園利用区分: "none"=利用なし, "day"=日中のみ, "night"=夜間保育あり
    df["daycare_type"] = "none"
    # 夜間保育必須フラグ: True=夜間保育受け入れ日のみ夜勤可, False=家族対応等で夜勤可
    df["nightcare_required"] = False
    # 夜間学童: True=夜間学童を利用
    df["gakudo"] = False
    # 夜間学童必須フラグ: True=夜間学童受け入れ日のみ夜勤可
    df["gakudo_required"] = False
    df["order"]   = range(1, len(df) + 1)  # 表示順（小さい順に表示）
    df["active"]  = True                   # 有効フラグ（False=一旦休止）
    return df
