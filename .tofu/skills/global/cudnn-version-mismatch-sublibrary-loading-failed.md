---
name: cudnn-version-mismatch-sublibrary-loading-failed
description: Fix CUDNN_STATUS_SUBLIBRARY_LOADING_FAILED caused by conda+pip cuDNN version coexistence and TransformerEngine's reverse-sorted glob loading
enabled: true
tags: [cuda, cudnn, transformer-engine, debugging, python, version-mismatch]
created: 2026-03-17T13:21:59Z
updated: 2026-03-17T13:21:59Z
---

## cuDNN SUBLIBRARY_LOADING_FAILED: Version Mismatch Root Cause

### Symptom
```
RuntimeError: cuDNN Error: CUDNN_BACKEND_TENSOR_DESCRIPTOR cudnnFinalize failed
  cudnn_status: CUDNN_STATUS_SUBLIBRARY_LOADING_FAILED
```
Occurs during TransformerEngine fused attention (`tex.fused_attn_fwd`).

### Root Cause
When both **conda cuDNN** (e.g. 9.10.2) and **pip nvidia-cudnn-cu12** (e.g. 9.13.0) are installed:

1. conda installs `libcudnn.so.9.10.2` (real file) + `libcudnn.so.9` (symlink → .9.10.2)
2. pip `nvidia-cudnn-cu12` overwrites `.so.9` files with its version (9.13.0 content), but `.so.9.10.2` real files remain
3. **TransformerEngine** `_load_cuda_library_from_system("cudnn")` does:
   ```python
   libs = glob.glob(f"{CUDA_HOME}/**/libcudnn.so*", recursive=True)
   libs.sort(reverse=True, key=os.path.basename)
   ctypes.CDLL(libs[0], mode=ctypes.RTLD_GLOBAL)
   ```
4. String sort: `"libcudnn.so.9.10.2" > "libcudnn.so.9"` → loads the **old 9.10.2** main library!
5. Sub-libraries (`.so.9`) are 9.13.0 → **version mismatch** → SUBLIBRARY_LOADING_FAILED

### Fix
```bash
# Remove stale conda versioned cuDNN files
rm miniforge/lib/libcudnn*.so.9.10.2  # or whatever the old version is

# Delete cuda-home symlink dir so it gets rebuilt cleanly
rm -rf miniforge/cuda-home
```

### Prevention
- After pip installs `nvidia-cudnn-cu12`, always clean up conda's versioned `.so.X.Y.Z` cuDNN files
- The unversioned `.so` and `.so.9` files are sufficient for runtime
- conda-forge cuDNN 9.13+ requires `cuda-version >=13` (doesn't exist yet), so can't "upgrade" conda to match pip

