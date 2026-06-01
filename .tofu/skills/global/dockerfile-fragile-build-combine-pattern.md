---
name: dockerfile-fragile-build-combine-pattern
description: Pattern for combining fragile Dockerfiles: preserve working half verbatim, append second half
enabled: true
tags: [docker, centos7, glibc, build]
created: 2026-05-03T03:36:50Z
updated: 2026-05-03T03:36:50Z
---

# Combining fragile multi-stage Dockerfiles

When a user has a known-working Dockerfile and wants to append another known-working fragment:

## Principles
1. **Preserve the working half byte-for-byte** unless the user explicitly says a step is broken.
2. Only remove/modify a step when the user identifies it as broken (e.g. "ldd still 2.28" → glibc 2.32 build silently failed).
3. **glibc is backward-compatible**: binaries compiled against an older glibc (e.g. 2.28) keep working after an upgrade to newer glibc (e.g. 2.35). So it's safe to upgrade glibc *after* compiling other tools like gcc, binutils.
4. Don't attempt to deduplicate "redundant" work (e.g. gcc 11 compiled from source + devtoolset-11's gcc 11) unless asked — fragile builds punish cleverness.

## Typical CentOS 7 + modern glibc upgrade pattern
- Base: CentOS 7 → glibc 2.28
- devtoolset-10/11 provides gcc 10/11 under /opt/rh (enable via `source scl_source enable` or `source /opt/rh/devtoolset-11/enable`)
- glibc 2.35 requires: devtoolset-11 (gcc 11+), make >= 4.0, bison, gawk, texinfo, python3, kernel-headers
- Install via: `make install DESTDIR=/tmp/glibc-install`, then `cp -a --remove-destination /tmp/glibc-install/lib64/* /lib64/` to avoid breaking in-flight RUN command
- Verify with `ldd --version | grep "2\.35"` — fail the build if mismatched (critical: silent glibc install failures are common)

## Red flags in existing glibc-from-source steps
- No `make install DESTDIR=...` staging — directly `make install` into `/` during a RUN may partially succeed and break subsequent commands
- No post-install `ldd --version` verification
- Missing `--with-headers=/usr/include` or `--enable-kernel=` flags

## Aliyun CentOS 7 SCL repo (for devtoolset-11 behind a proxy)
```
[aliyun-sclo-rh]
baseurl=http://mirrors.aliyun.com/centos-vault/7.9.2009/sclo/$basearch/rh/
gpgcheck=0
enabled=1
proxy=http://<proxy>:<port>
```

