#!/usr/bin/env bash
# 端到端操作演示（需先启动服务：docker compose up 或 uvicorn app.main:app）。
# 用法： bash samples/demo_api.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
DATA="$(cd "$(dirname "$0")/data" && pwd)"

echo "== 1. 健康检查 =="
curl -sS "$BASE/health" | python3 -m json.tool

echo "== 2. 上传常规网格 + 站点，创建任务 =="
TASK=$(curl -sS -X POST "$BASE/api/tasks" \
  -F "file=@$DATA/demo_grid.nc" \
  -F "variable=tas" \
  -F "stations_file=@$DATA/stations.csv")
echo "$TASK" | python3 -m json.tool
TASK_ID=$(echo "$TASK" | python3 -c "import sys,json;print(json.load(sys.stdin)['task_id'])")

echo "== 3. 轮询状态直到结束 =="
while true; do
  STATUS=$(curl -sS "$BASE/api/tasks/$TASK_ID")
  echo "$STATUS" | python3 -m json.tool
  STATE=$(echo "$STATUS" | python3 -c "import sys,json;print(json.load(sys.stdin)['state'])")
  case "$STATE" in
    completed|failed|cancelled|timeout) break ;;
  esac
  sleep 1
done

echo "== 4. 下载 CSV（前 6 行） =="
curl -sS "$BASE/api/tasks/$TASK_ID/download" | head -n 6 || true

echo "== 5. 慢提取任务：创建后取消 =="
SLOW=$(curl -sS -X POST "$BASE/api/tasks" \
  -F "file=@$DATA/demo_slow_cancel.nc" \
  -F "variable=tas" \
  -F "stations_file=@$DATA/stations.csv")
SLOW_ID=$(echo "$SLOW" | python3 -c "import sys,json;print(json.load(sys.stdin)['task_id'])")
sleep 1
curl -sS -X POST "$BASE/api/tasks/$SLOW_ID/cancel" | python3 -m json.tool
sleep 3
curl -sS "$BASE/api/tasks/$SLOW_ID" | python3 -m json.tool
echo "取消后尝试下载（应返回 409）："
curl -sS -o /dev/null -w "HTTP %{http_code}\n" "$BASE/api/tasks/$SLOW_ID/download"
