---
name: vllm-cublas-invalid-value-fix
description: Fix for CUBLAS_STATUS_INVALID_VALUE errors during vLLM profile_run with large max_num_batched_tokens
enabled: true
tags: [vllm, cuda, cublas, debugging, gpu, deployment]
created: 2026-03-13T07:28:48Z
updated: 2026-03-13T07:28:48Z
---


# vLLM CUBLAS_STATUS_INVALID_VALUE During Profile Run

## Symptom
vLLM crashes during initialization (profile_run / _dummy_run) with:
```
RuntimeError: CUDA error: CUBLAS_STATUS_INVALID_VALUE when calling `cublasGemmEx(...)`
```

## Root Cause
- During `profile_run`, vLLM creates a dummy tensor of size `max_num_batched_tokens` to estimate GPU memory
- When `max_num_batched_tokens` is very large (e.g., 65536) and the model has layers with small output dimensions, it creates extremely asymmetric GEMM shapes
- cuBLAS has undocumented constraints on certain matrix dimension combinations with BF16

### Known Triggering Patterns
1. **GLM-5-FP8 with torch.compile**: inductor generates `extern_kernels.mm(buf, reinterpret_tensor(weight, (6144, 32), ...))` — the tiny `n=32` dimension causes issues
2. **Qwen3.5 linear_attention**: `in_proj_ba` with partitions=[64,64] (128 total output dim) × 65536 batch size
3. **FP8 block shape mismatches**: layers with `Allowing FP8 block shape mismatch` are particularly vulnerable

## Fixes (in order of preference)
1. **Lower `--max-num-batched-tokens`** to 8192 or 16384 or 32768 (reduce profile_run dummy tensor size)
2. **Add `--enforce-eager`** to disable torch.compile/inductor (avoids inductor-generated problematic GEMM kernels)
3. **Clean compile caches** before retrying:
   ```bash
   rm -rf ~/.cache/vllm/torch_compile_cache
   rm -rf /tmp/torchinductor_*
   rm -rf ~/.triton/cache
   ```

## Testing Strategy
Start with small `max_num_batched_tokens` (e.g., 8192), confirm startup succeeds, then gradually increase.

