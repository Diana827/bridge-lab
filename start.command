#!/bin/bash
# BRIDGE LAB local demo — start script (macOS / Linux)
# Run from this folder:  bash start.sh   (or double-click start.command on macOS)
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3가 필요합니다. https://www.python.org 에서 Python 3.11+ 를 설치하세요."
  exit 1
fi

if [ -f .env ]; then
  set -a; . ./.env; set +a
fi
echo "Meta Model API 키를 입력하세요. 입력하는 동안 화면에 표시되지 않습니다."
printf "API 키: "
read -r -s MODEL_API_KEY
echo
if [ -z "${MODEL_API_KEY}" ]; then
  echo "키가 입력되지 않아 종료합니다."
  exit 1
fi
export MODEL_API_KEY

echo "연결 테스트 중..."
python3 test_connection.py || exit 1

PORT="${BRIDGE_PORT:-8791}"
PORT_SOURCE="${BRIDGE_PORT:-}"
find_free_port() {
  if ! command -v lsof >/dev/null 2>&1; then return; fi
  while lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; do
    PORT=$((PORT + 1))
  done
}
if command -v curl >/dev/null 2>&1; then
  HEALTH="$(curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)"
  case "$HEALTH" in
    *'"server": "bridge-lab-local-v4"'*|*'"server":"bridge-lab-local-v4"'*)
      echo "현재 프로젝트 서버를 사용합니다: http://localhost:$PORT"
      (command -v open >/dev/null && open "http://localhost:$PORT/") || true
      exit 0
      ;;
    '')
      if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        if [ -n "$PORT_SOURCE" ]; then
          echo "지정한 포트 $PORT가 사용 중입니다. 다른 BRIDGE_PORT를 지정하세요."
          exit 1
        fi
        find_free_port
        echo "기존 포트가 사용 중이어서 $PORT 포트로 시작합니다."
      fi
      ;;
    *)
      if [ -n "$PORT_SOURCE" ]; then
        echo "지정한 포트 $PORT에 다른 서버가 실행 중입니다. 기존 서버를 종료하거나 BRIDGE_PORT를 바꿔 다시 실행하세요."
        exit 1
      fi
      find_free_port
      echo "다른 서버가 포트를 사용 중이어서 $PORT 포트로 시작합니다."
      ;;
  esac
fi
echo "서버 시작: http://localhost:$PORT  (종료: Ctrl+C)"
( sleep 2; (command -v open >/dev/null && open "http://localhost:$PORT/") || \
  (command -v xdg-open >/dev/null && xdg-open "http://localhost:$PORT/") || true ) &
BRIDGE_PORT="$PORT" python3 server/bridge_server.py
