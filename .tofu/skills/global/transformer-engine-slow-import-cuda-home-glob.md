---
name: transformer-engine-slow-import-cuda-home-glob
description: TransformerEngine import takes 2500s when CUDA_HOME points to entire conda directory on network filesystem — fix with lightweight cuda-home symlink directory
enabled: true
tags: [python, cuda, performance, transformer-engine, NFS, debugging]
created: 2026-03-17T10:42:02Z
updated: 2026-03-17T10:42:02Z
---

## Problem

`import transformer_engine` takes ~2500 seconds (41 minutes) on GPU servers when conda is on a network filesystem (NFS/dolphinfs).

## Root Cause

1. `.bashrc` or conda activation sets `CUDA_HOME=$CONDA_PREFIX` (e.g. `/mnt/.../miniforge/`)
2. TransformerEngine's `_load_cuda_library_from_system()` does `glob(CUDA_HOME + "/**/libcudnn.so*", recursive=True)`
3. This recursively scans the **entire conda environment** (100k+ files across `envs/`, `site-packages/`, `pkgs/`) on a network filesystem
4. Each library (cudnn, nvrtc, curand, cublas, etc.) triggers a separate recursive glob → **3x 850s = 2550s**

## Fix: Lightweight `cuda-home` symlink directory

Create `$CONDA_PREFIX/cuda-home/` with only symlinks to CUDA libs (~100 files):

```bash
CONDA_BASE="$CONDA_PREFIX"
CUDA_HOME_DIR="$CONDA_BASE/cuda-home"
mkdir -p "$CUDA_HOME_DIR/bin" "$CUDA_HOME_DIR/lib64" "$CUDA_HOME_DIR/include"

# Binaries
for bin in nvcc ptxas cicc fatbinary; do
    [ -f "$CONDA_BASE/bin/$bin" ] && ln -sf "$CONDA_BASE/bin/$bin" "$CUDA_HOME_DIR/bin/$bin"
done

# CUDA .so libraries
for f in "$CONDA_BASE"/lib/libcuda*.so* "$CONDA_BASE"/lib/libnv*.so* \
         "$CONDA_BASE"/lib/libcublas*.so* "$CONDA_BASE"/lib/libcufft*.so* \
         "$CONDA_BASE"/lib/libcurand*.so* "$CONDA_BASE"/lib/libcusparse*.so* \
         "$CONDA_BASE"/lib/libcusolver*.so* "$CONDA_BASE"/lib/libcudnn*.so*; do
    [ -f "$f" ] || [ -L "$f" ] && ln -sf "$f" "$CUDA_HOME_DIR/lib64/$(basename $f)"
done

# lib -> lib64 symlink (TE searches both)
ln -sf "$CUDA_HOME_DIR/lib64" "$CUDA_HOME_DIR/lib"

# nvvm (needed by nvcc)
[ -d "$CONDA_BASE/nvvm" ] && ln -sf "$CONDA_BASE/nvvm" "$CUDA_HOME_DIR/nvvm"
```

In Python scripts, set before any imports:
```python
import os
_conda_prefix = os.environ.get('CONDA_PREFIX', '')
_cuda_home = os.path.join(_conda_prefix, 'cuda-home')
if os.path.isdir(_cuda_home):
    os.environ['CUDA_HOME'] = _cuda_home
```

## Result
- glob time: **850s → 0.15s** per library
- Total TE import: **2500s → ~15s**

