#!/bin/sh
# Usage: run_arm_all.sh <run-name> <arm> [doc ids...]
# Builds one deck per corpus document for one arm, then renders it.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
: "${SLIDES_BENCH_DATA:?set SLIDES_BENCH_DATA to the private data root}"
PY=${SLIDES_BENCH_PYTHON:-python3}
RUN=$1; ARM=$2; shift 2
DOCS=${*:-$(cd "$REPO" && PYTHONPATH=$REPO $PY -c 'from evaluations.slides_bench.corpus import document_ids; print(" ".join(document_ids()))')}
export PYTHONPATH=$REPO
for doc in $DOCS; do
  out=$SLIDES_BENCH_DATA/runs/$RUN/$ARM/$doc
  if [ -f "$out/render/slide-01.png" ]; then echo "skip $ARM/$doc"; continue; fi
  mkdir -p "$out"
  case $ARM in
    tofu) (cd "$REPO" && $PY -m evaluations.slides_bench.run_tofu "$doc" "$out") > "$out/stdout.log" 2>&1 ;;
    *) (cd "$REPO" && $PY -m evaluations.slides_bench.corpus_input "$doc" "$out/input.md" \
          && sh "$HERE/competitors/run_competitor.sh" "$ARM" "$out/input.md" "$out/deck.pptx" \
             "$($PY -m evaluations.slides_bench.corpus_input --field lang "$doc")" \
             "$($PY -m evaluations.slides_bench.corpus_input --field pages "$doc")") \
          > "$out/stdout.log" 2>&1 ;;
  esac
  rc=$?
  authored=$SLIDES_BENCH_DATA/runs/$RUN/tofu-authored/$doc
  if [ "$ARM" = tofu ] && [ -f "$authored/deck/deck.pptd" ]; then
    (cd "$REPO" && $PY -m evaluations.slides_bench.force_export "$authored" \
       && $PY -m evaluations.slides_bench.render "$authored/forced.pptx" "$authored/render_forced") \
       > "$authored/render.log" 2>&1 || echo "AUTHORED-EXPORT-FAIL $doc"
  fi
  pptx=$(ls "$out"/deck.pptx "$out"/deck/*.pptx 2>/dev/null | head -1)
  if [ $rc -ne 0 ] || [ -z "$pptx" ]; then
    if [ -d "$out/deck/preview/pages" ]; then
      mkdir -p "$out/render_preview"
      for f in "$out"/deck/preview/pages/*.png; do cp "$f" "$out/render_preview/slide-$(basename "$f")"; done
    fi
    if [ -f "$out/deck/deck.pptd" ] && (cd "$REPO" && $PY -m evaluations.slides_bench.force_export "$out") >> "$out/render.log" 2>&1; then
      (cd "$REPO" && $PY -m evaluations.slides_bench.render "$out/forced.pptx" "$out/render_forced") >> "$out/render.log" 2>&1
    fi
    echo "FAIL $ARM/$doc rc=$rc"; continue
  fi
  (cd "$REPO" && $PY -m evaluations.slides_bench.render "$pptx" "$out/render") > "$out/render.log" 2>&1 \
    && echo "ok $ARM/$doc $(ls "$out"/render/slide-*.png | wc -l) slides" || echo "RENDER-FAIL $ARM/$doc"
done
