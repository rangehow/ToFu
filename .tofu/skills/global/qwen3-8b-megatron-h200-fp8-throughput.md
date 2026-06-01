---
name: qwen3-8b-megatron-h200-fp8-throughput
description: Ballpark TFLOPS/GPU and MFU for Qwen3-8B on H200 + Megatron in FP8
enabled: true
tags: [megatron, training, mfu, fp8, h200, qwen3]
created: 2026-05-04T05:12:18Z
updated: 2026-05-04T05:12:18Z
---

# Qwen3-8B training throughput on H200 + Megatron-LM, FP8

Qwen3-8B ≈ Llama-3-8B architecturally (dense, GQA, 36 layers, hidden 4096),
so public Llama-3-8B Hopper numbers transfer ~1:1. H200 has the **same
compute as H100** (989 TFLOPS BF16 dense / 1979 TFLOPS FP8 dense peak, no
sparsity) but 141GB @ 4.8TB/s HBM3e → typically only ~5-15% gain over H100
for an 8B run (bigger mbs, less recompute).

## Ballpark for a well-tuned Megatron-LM / Megatron-Core FP8 run (seq 8K)

- **Throughput**: ~650-900 TFLOPS/GPU (model-FLOPs counted 6·N·D)
- **FP8 speedup vs BF16**: 1.25-1.35× (NeMo benchmarks report 1.30× for Llama-3 8B on H100 FP8-current-scaling)
- **MFU vs FP8 peak (1979)**: ~33-45%
- **MFU vs BF16 peak (989)** — i.e. "model FLOPs utilization" DeepSeek/NeMo-style: ~65-80%
- BF16 baseline for comparison: ~450-550 TFLOPS/GPU (~45-55% BF16 MFU)

Rule of thumb: **~750 TFLOPS/GPU, ~38% FP8-MFU, ~75% BF16-MFU, 1.3× over BF16**.

## What moves the needle
- Seq 8K >> 4K for FP8 speedup (larger GEMMs).
- TP=1/PP=1 pure DP is optimal for 8B; any TP>1 costs 5-15%.
- Selective activation recompute is ~free; full recompute costs 10-20%.
- TE FP8 recipes: current-scaling (tensorwise) fastest on Hopper; blockwise ~5-10% slower but more stable.
- `--overlap-grad-reduce --overlap-param-gather --tp-comm-overlap` matters at DP>32.
- CP only helps at ≥16K seq.

## If you see <500 TFLOPS/GPU in FP8, check
- Unnecessary TP>1
- Full activation recompute left on
- FP8 silently disabled for some layers (check TE logs)
- GBS too small → DP comm exposed
- Dataloader/tokenizer bottleneck (GPU idle in profiler)

