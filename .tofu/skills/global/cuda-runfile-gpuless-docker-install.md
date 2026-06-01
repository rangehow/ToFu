---
name: cuda-runfile-gpuless-docker-install
description: CUDA runfile toolkit install in GPU-less Docker/kaniko builds — use --extract, never --silent
enabled: true
tags: [cuda, docker, kaniko, gpu]
created: 2026-04-23T05:41:08Z
updated: 2026-04-23T05:41:08Z
---

# Installing CUDA toolkit from .run in a Docker build WITHOUT a GPU

## The trap
`sh cuda.run --silent --toolkit` hangs indefinitely on GPU-less build hosts
around "Setting globalpath=". The installer script probes environment state
that doesn't exist and never times out. Don't use it in Dockerfiles.

## The right approach — `--extract` + cp
```bash
sh cuda.run --extract=/tmp/cuda-extract
```
This is a pure `tar xf`. No installer logic. Takes ~2 minutes for 5.4 GB,
works on any host, no GPU needed.

## CUDA 12.x extract layout (flat, not under builds/)
`/tmp/cuda-extract/` contains these sibling dirs, each with its own
`bin/ include/ lib64/ targets/x86_64-linux/{lib,include}/ nvvm/` subtree:
- cuda_cccl cuda_cudart cuda_cuobjdump cuda_cupti cuda_cuxxfilt
- cuda_nvcc cuda_nvdisasm cuda_nvml_dev cuda_nvprune cuda_nvrtc
- cuda_nvtx cuda_opencl cuda_profiler_api cuda_sandbox_dev
- libcublas libcufft libcufile libcurand libcusolver libcusparse
- libnpp libnvfatbin libnvjitlink libnvjpeg integration

Skip: nsight_*, cuda_nsight, cuda_nvvp, cuda_sanitizer_api, cuda_gdb,
cuda_demo_suite, cuda_documentation, nvidia_fs, NVIDIA-Linux-*.run.

## Merge pattern — NO find/head pipes (SIGPIPE + pipefail = exit 141)
```bash
for comp in $INCLUDE_COMPONENTS; do
    [ -d "/tmp/cuda-extract/$comp" ] && cp -a "/tmp/cuda-extract/$comp"/. /usr/local/cuda-12.9/
done
# Ensure lib64/ and include/ exist (they're normally symlinks to targets/x86_64-linux/)
[ -e /usr/local/cuda-12.9/lib64 ] || ln -s targets/x86_64-linux/lib /usr/local/cuda-12.9/lib64
[ -e /usr/local/cuda-12.9/include ] || ln -s targets/x86_64-linux/include /usr/local/cuda-12.9/include
```

## Dockerfile structural rules that saved days of debugging
- **Split into layers**: download (5.4 GB) as own RUN → extract/install as own RUN
  → trim + drop /opt/cuda-src as final RUN. Iteration only re-runs the failing layer.
- **Use `--output-document=/path` not `-O /path`**: editors split `-O` into `- O`.
- **Never use `ADD <url>` in kaniko**: kaniko ignores http_proxy, DNS fails.
- **Work in `/tmp` if it's a separate nvme mount**: 3 TB vs 50 GB overlay, and
  kaniko's snapshot diff doesn't scan /tmp.
- **`timeout --foreground N` wrap every silent install**: 20-30 min cap prevents
  the 12-hour silent hang.
- **Dump `/var/log/cuda-installer.log` and `/var/log/nvidia-installer.log` on failure.**
- **Avoid `set -o pipefail` with `cmd | head`**: SIGPIPE → exit 141 trip wire.

## CentOS 7 EOL sidebar
Custom base images often ship devtoolset-N in /opt/rh/. Check first:
`ls /opt/rh/`. If devtoolset-11+ is there, skip yum entirely and just set:
```
ENV PATH=/opt/rh/devtoolset-11/root/usr/bin:$PATH
    LD_LIBRARY_PATH=/opt/rh/devtoolset-11/root/usr/lib64:$LD_LIBRARY_PATH
```
CUDA 12.9 requires gcc 6..14. Stock CentOS 7 gcc 4.8.5 is too old.

## Proxy through kaniko
- `ENV http_proxy=...` is honored by `RUN wget/curl` ✅
- `ENV http_proxy=...` is IGNORED by `ADD <url>` ❌
- Add `.internal.corp.com` to `no_proxy` for internal mirrors

