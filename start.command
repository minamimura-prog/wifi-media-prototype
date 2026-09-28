#!/bin/bash
cd "$(dirname "$0")"

# Python 3 があるか確認
if ! command -v python3 >/dev/null 2>&1; then
  echo ""
  echo "Python 3 が必要です。"
  echo "https://www.python.org/downloads/macos/ からPython 3を入れてください。"
  echo ""
  read -n 1 -s -r -p "Enterキーを押してください..."
  exit 1
fi

# 5050〜5090の空いている番号を探す
PORT=""
for p in $(seq 5050 5090); do
  if ! lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1; then
    PORT=$p
    break
  fi
done

if [ -z "$PORT" ]; then
  echo "使えるポートが見つかりませんでした。"
  read -n 1 -s -r -p "Enterキーを押してください..."
  exit 1
fi

echo ""
echo "Wi-Fi MEDIA を起動しています..."
echo "使う番号：$PORT"
echo ""

PORT=$PORT python3 server.py &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null' EXIT

sleep 1
open "http://127.0.0.1:$PORT/admin"

echo ""
echo "管理画面：http://127.0.0.1:$PORT/admin"
echo "公開ページ：http://127.0.0.1:$PORT/"
echo ""
echo "この黒い画面は閉じないでください。"
echo "Wi-Fi MEDIAを終了するときは Ctrl+C を押してください。"
echo ""

wait $SERVER_PID
