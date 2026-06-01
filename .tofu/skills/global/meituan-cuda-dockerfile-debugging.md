---
name: meituan-cuda-dockerfile-debugging
description: Meituan CentOS 7 + glibc 2.35 base image has devtoolset-11 pre-installed; debug pattern for kaniko Dockerfile builds
enabled: true
tags: [docker, cuda, centos7, kaniko, meituan, debugging]
created: 2026-04-19T15:47:29Z
updated: 2026-04-19T15:47:29Z
---

# Meituan/Sankuai CUDA Dockerfile Debugging

## Base image `training_centos7_glibc2.35_*` already has:
- `/opt/rh/devtoolset-11/root/usr/bin/gcc` (gcc 11) — just activate via PATH, DO NOT `yum install`
- `/opt/rh/devtoolset-4` (old, ignore)
- `/usr/bin/gcc` (4.8.5, too old for CUDA 12+)
- glibc 2.35 active (custom layer overwrites stock 2.17)
- Pre-configured yum repos: `sankuai.repo`, `centos-sclo-rh.repo` (mirrors.sankuai.com), `aliyun-sclo-rh.repo` (via proxy), `epel.repo`

## Network facts on their build hosts
- `http://mirrors.sankuai.com/*` → bypass proxy (direct 200)
- `http://repos.sankuai.com/*` → bypass proxy (direct 200)
- `http://mirrors.aliyun.com/*` → via proxy 10.229.18.27:8412 only (direct fails)
- `https://developer.download.nvidia.com/*` → via proxy
- `vault.centos.org` returns 301 → damaged repomd in yum; avoid
- `mirror.centos.org` is EOL/dead (CentOS 7 EOL 2024-06-30)

## Required no_proxy
`no_proxy=localhost,127.0.0.1,.sankuai.com,.meituan.com` — without this the proxy 400s on internal mirrors

## Silent-failure anti-pattern in CUDA runfile install
Avoid `wget -q` + `sh cuda.run --silent` without log-dump. Fix:
```
sh cuda.run --silent --toolkit --override --tmpdir=/tmp --no-opengl-libs \
    --log-file=/tmp/cuda-installer.log \
|| { cat /tmp/cuda-installer.log /var/log/cuda-installer.log /var/log/nvidia-installer.log 2>/dev/null; df -h; exit 1; }
```

## CUDA 12.9 requirements
- gcc 6..14 (devtoolset-11 works)
- glibc >= 2.17
- ~12 GB free in --tmpdir (runfile unpacks ~10 GB)

## Remote image inspection without root/mount
If podman/docker can't unpack (shared host, no subuid/mount priv):
```
# Install skopeo from conda
mamba install -n ENV -c conda-forge skopeo
# Copy to OCI dir (no mount needed)
skopeo copy --src-tls-verify=false docker://REGISTRY/IMAGE:TAG oci:/tmp/out:TAG
# Parse: /tmp/out/index.json → manifest → layers are /tmp/out/blobs/sha256/*
# Inspect any file:
tar xzf /tmp/out/blobs/sha256/LAYERHASH -O path/inside/image | head
```

## Kaniko-specific
- `ADD <URL>` caches layer by URL — use it instead of `wget` for bulky downloads
- `fastestmirror` plugin probes dead mirrors forever; disable with
  `sed -i 's/^enabled=1/enabled=0/' /etc/yum/pluginconf.d/fastestmirror.conf`
  and `yum --disableplugin=fastestmirror`
- Writing repo files via `printf '%s\n' ...` is more reliable than heredoc when line continuations may get mangled
- Separate install from cleanup RUNs — cleanup in same layer destroys diagnostic evidence on failure

