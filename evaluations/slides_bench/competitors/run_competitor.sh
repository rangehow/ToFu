#!/bin/sh
# Usage: run_competitor.sh <tool> <input.md> <out.pptx> <lang> <pages>
# Starts the loopback relay to Tofu's routing, runs one competitor arm with
# its own isolated environment under $SLIDES_BENCH_DATA/competitors, stops
# the relay.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
TOOL=$1; shift
PORT=$((20000 + $$ % 20000))
PY=${SLIDES_BENCH_PYTHON:-python3}
(cd "$REPO" && PYTHONPATH=$REPO exec $PY -m evaluations.slides_bench.openai_proxy $PORT) 2>"$2.relay.log" &
RELAY=$!
trap 'kill $RELAY 2>/dev/null' EXIT INT TERM
sleep 1
export OPENAI_API_BASE=http://127.0.0.1:$PORT/v1 OPENAI_BASE_URL=http://127.0.0.1:$PORT/v1 OPENAI_API_KEY=tofu-local
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
# Tools that convert PPTX themselves (PPTAgent) find the shared LibreOffice.
PATH=$SLIDES_BENCH_DATA/tools/squashfs-root/opt/libreoffice25.8/program:$PATH
export FONTCONFIG_FILE=$SLIDES_BENCH_DATA/tools/fonts.conf
unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy
C=$SLIDES_BENCH_DATA/competitors
case $TOOL in
  slidedeckai) "$C/sdai-venv/bin/python" "$HERE/run_slidedeckai.py" "$@" ;;
  pptagent) "$C/pptagent-venv/bin/python" "$HERE/run_pptagent.py" "$@" ;;
  presenton) sh "$HERE/run_presenton.sh" "$@" ;;
  *) echo "unknown tool $TOOL" >&2; exit 2 ;;
esac
