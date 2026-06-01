---
name: kill-python-server-process
description: Commands to find and kill a Python server.py process on a server
enabled: true
tags: [linux, python, server, process-management, devops]
created: 2026-04-14T08:54:06Z
updated: 2026-04-14T08:54:06Z
---

# Killing a Python server.py Process

## Step-by-step approach
1. **Find the process ID:**
   ```bash
   ps aux | grep server.py
   ```

2. **Terminate it** (replace `[PID]` with the actual process ID):
   ```bash
   kill [PID]
   ```

## One-liner (force kill)
```bash
pkill -f server.py
```

## Notes
- Add `sudo` if you need administrator privileges.
- Always verify the process is correctly identified before terminating it.

