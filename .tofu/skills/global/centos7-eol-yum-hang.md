---
name: centos7-eol-yum-hang
description: CentOS 7 EOL: yum hangs on centos-release-scl / fastestmirror — drop repo files manually
enabled: true
tags: [centos7, yum, docker, cuda, kaniko]
created: 2026-04-19T03:42:51Z
updated: 2026-04-19T03:42:51Z
---

# CentOS 7 EOL yum hang — root causes & fixes

CentOS 7 went EOL 2024-06-30. `mirror.centos.org` is mostly dead. Symptoms:

1. `Determining fastest mirrors` hangs forever
   → disable plugin: `sed -i 's/^enabled=1/enabled=0/' /etc/yum/pluginconf.d/fastestmirror.conf`
2. `Processing Dependency: centos-release-scl-rh` hangs
   → `centos-release-scl` package drops .repo files; skip the package, write them directly

## Minimal vault-based SCL repo files (CentOS 7.9.2009)

```
# /etc/yum.repos.d/CentOS-SCLo-scl.repo
[centos-sclo-sclo]
name=CentOS-7 - SCLo sclo
baseurl=http://vault.centos.org/7.9.2009/sclo/x86_64/sclo/
gpgcheck=0
enabled=1

# /etc/yum.repos.d/CentOS-SCLo-scl-rh.repo
[centos-sclo-rh]
name=CentOS-7 - SCLo rh
baseurl=http://vault.centos.org/7.9.2009/sclo/x86_64/rh/
gpgcheck=0
enabled=1
```

## Proxy-aware yum settings (add to /etc/yum.conf)
```
proxy=http://PROXY:PORT
timeout=30
retries=2
```

## Verify proxy can reach vault BEFORE yum:
```
curl -x $http_proxy -sI http://vault.centos.org/7.9.2009/os/x86_64/repodata/repomd.xml
```

## Alternative: skip SCL, install gcc differently
- conda: `conda install -c conda-forge gxx_linux-64=11`
- download devtoolset RPMs directly from vault with wget + `rpm -Uvh`
- check if base image already has newer gcc hiding (find / -name 'gcc-*' in /opt)

## For CUDA 12.9 specifically
Requires gcc 6..14. Stock CentOS 7 gcc 4.8.5 is TOO OLD.
Custom images with upgraded glibc often already have a newer gcc in /opt/rh/ or /opt/gcc-* — search first before installing.

