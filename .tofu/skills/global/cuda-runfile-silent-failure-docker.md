---
name: cuda-runfile-silent-failure-docker
description: CUDA .run installer silent failures in Docker/kaniko — diagnosis checklist
enabled: true
tags: [docker, cuda, kaniko, debugging]
created: 2026-04-18T07:09:30Z
updated: 2026-04-18T07:09:30Z
---

# CUDA runfile installer silent failures in Docker/kaniko builds

## Symptom
`RUN wget ... cuda_*.run && sh cuda_*.run --silent --toolkit` ends with no output and non-zero exit. Build takes long (multi-GB download) then silently dies.

## Root causes (most common)
1. **`wget -q`** hides everything, including proxy HTML error pages saved as the .run file → `sh` on HTML exits with no useful output.
2. **`--silent`** on the runfile sends errors to `/var/log/cuda-installer.log` and `/var/log/nvidia-installer.log` which never get printed.
3. **Disk pressure**: runfile unpacks ~10GB into `--tmpdir`. Build sandbox may be too small.
4. **Proxy corruption**: `http_proxy` env returning HTML error bodies for large downloads.

## Fix pattern
```dockerfile
# 1. Cache download as its own layer; verify integrity
ADD https://developer.download.nvidia.com/.../cuda_X.Y.Z_linux.run /opt/cuda-tmp/cuda.run
RUN echo "<sha256>  /opt/cuda-tmp/cuda.run" | sha256sum -c -

# 2. Install in a separate layer with logs surfaced on failure
RUN set -eo pipefail; cd /opt/cuda-tmp && \
    sh cuda.run --silent --toolkit --override --tmpdir=/opt/cuda-tmp \
        --no-opengl-libs --log-file=/opt/cuda-tmp/cuda-installer.log \
     || { cat /opt/cuda-tmp/cuda-installer.log /var/log/cuda-installer.log \
              /var/log/nvidia-installer.log 2>/dev/null; \
          df -h; free -m; exit 1; }

# 3. Cleanup in its own RUN so failures don't destroy diagnostic state
RUN rm -rf /usr/local/cuda-*/doc /usr/local/cuda-*/nsight-* ...
```

## General rules
- Never combine `wget -q` with `sh *.run --silent` — you will lose both channels of error output.
- Always `set -eo pipefail` in multi-step RUN chains.
- Always wrap silent installers in `|| { dump_logs; exit 1; }`.
- Always verify large downloads with sha256 — especially behind corporate proxies.
- Separate download / install / cleanup into distinct layers for cacheability and post-mortem.

