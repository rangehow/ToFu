---
name: nccl-socket-retry-correct-env-vars
description: NCCL multi-node connection failures: correct socket env vars (NCCL_SOCKET_RETRY_CNT/NCCL_SOCKET_RETRY_SLEEP_MSEC), and critical bug where auto-detecting NCCL_SOCKET_IFNAME picks RDMA-only NIC (eth9) which blocks TCP bootstrap — use route-based detection instead
enabled: true
tags: [nccl, distributed-training, multi-node, debugging, environment-variables, bug-pattern, rdma, network-interface]
created: 2026-03-27T03:17:54Z
updated: 2026-04-08T08:12:21Z
---

# NCCL Multi-Node Connection Debugging

## Problem 1: Wrong NCCL env var names (silently ignored)

NCCL silently ignores unrecognized environment variables. Common wrong names:

| WRONG (silently ignored) ❌ | CORRECT ✅ | Since |
|---|---|---|
| `NCCL_SOCKET_NRETRY` | `NCCL_SOCKET_RETRY_CNT` | NCCL 2.24+ |
| `NCCL_CONNECT_TIMEOUT` | Does not exist at all | — |

The correct variables for socket-level retry:
- **`NCCL_SOCKET_RETRY_CNT`** — number of retries (default: 34). Shows as `(X/N)` in logs.
- **`NCCL_SOCKET_RETRY_SLEEP_MSEC`** — base sleep between retries (default: 100ms, scales linearly: retry N sleeps N×100ms)

## Problem 2: NCCL_SOCKET_IFNAME auto-detection picks RDMA-only NIC

**Root cause:** On H800/RDMA clusters, the container has many `eth*` interfaces. The RDMA/RoCE NICs (e.g. `eth9`) have IPv4 addresses but do NOT support TCP connections — only RDMA verbs. A naive "first eth*" heuristic picks these, causing NCCL TCP bootstrap to permanently time out.

**Symptoms:**
```
NCCL INFO NET/IB : Using [0]mlx5_1:1/RoCE [1]mlx5_6:1/RoCE ; OOB eth9:33.235.112.126<0>
NCCL INFO socketPollConnect: connect to 33.235.88.59<57017> returned Connection timed out
```

**Fix: Route-based interface detection**
```bash
IF_NAME=$(python3 -c "
import os, json, socket, subprocess
spec = json.loads(os.environ['AFO_ENV_CLUSTER_SPEC'])
role = spec['role']
my_idx = int(spec['index'])
workers = spec[role]
peer_idx = (my_idx + 1) % len(workers)
peer_host = workers[peer_idx].split(':')[0]
peer_ip = socket.gethostbyname(peer_host)
out = subprocess.check_output(['ip', 'route', 'get', peer_ip], text=True)
tokens = out.split()
print(tokens[tokens.index('dev') + 1])
" 2>/dev/null)
IF_NAME=${IF_NAME:-eth0}
export NCCL_SOCKET_IFNAME=${IF_NAME}
```

This asks the kernel which interface *routes* to a peer node, always returning the correct TCP-capable interface.

## Diagnosis checklist
1. Check `(X/N)` in retry logs — if N is still 34 after setting env vars, the var name is wrong
2. Check `OOB ethN:` in NCCL INFO — if it's a high-numbered eth (eth9, eth10), it's likely an RDMA-only NIC
3. The `.hope` template `NCCL_SOCKET_IFNAME = eth` is a prefix match — matches ALL eth* including RDMA NICs
