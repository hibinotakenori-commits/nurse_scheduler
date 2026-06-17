#!/bin/bash
# 3A病棟 勤務表アプリ 起動スクリプト
# 師長用アプリ (8501) とスタッフ希望入力アプリ (8502) を同時に起動します

cd "$(dirname "$0")"

echo "================================================"
echo "  3A病棟 勤務表アプリを起動しています..."
echo "================================================"

# ローカルIPを取得
LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "不明")

echo ""
echo "師長用アプリ  → http://localhost:8501"
echo "スタッフ用アプリ → http://${LOCAL_IP}:8502"
echo ""
echo "スタッフには http://${LOCAL_IP}:8502 をスマホで開いてもらってください。"
echo "（QRコードは師長用アプリの「📅 希望入力」タブに表示されます）"
echo ""
echo "終了するには Ctrl+C を2回押してください。"
echo "================================================"

# 師長用アプリ（localhost のみ）
streamlit run app.py \
  --server.port 8501 \
  --server.headless true \
  &
PID1=$!

# スタッフ用アプリ（LAN全体に公開）
streamlit run staff_app.py \
  --server.port 8502 \
  --server.address 0.0.0.0 \
  --server.headless true \
  &
PID2=$!

# 両プロセスが終了するまで待つ
wait $PID1 $PID2
