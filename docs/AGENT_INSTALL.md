# Install Tofu with a coding agent

This page is written for a coding agent (Codex, Claude Code, OpenCode, Cursor,
Tofu, or any other agent that can run shell commands) whose user asked it to
install Tofu. People start it by pasting one line into the agent's chat:

```text
Install Tofu on this machine: fetch https://raw.githubusercontent.com/rangehow/ToFu/main/docs/AGENT_INSTALL.md with curl and follow it.
```

Agent: run every step yourself, in order, and finish with the report in step 6.
Talk to the user in the language they wrote to you in.

The procedure installs the full Tofu workspace (web UI, durable storage and the
agent runtime) for the current user and starts it as a managed background
service. It uses the same `install.sh` and unattended receipt contract as
[INSTALL.md](INSTALL.md), needs no root access and needs no model API key. If
the user only wants the embeddable Python runtime, `pip install tofu-agent` is
the entire installation; see the README instead.

## Ground rules

- Never use `sudo`, never disable TLS or certificate verification, and never
  delete, move or overwrite anything this procedure did not create.
- Never put an API key in a command, a file or the chat. The user adds a model
  provider in the Tofu web UI after installation.
- Do not stop, restart or kill any process. Restarting Tofu needs the user's
  own confirmation.
- The installer downloads from GitHub and package indexes, and it writes
  outside your workspace: the install directory and, if `uv` is missing,
  `~/.local/bin/uv`. If your harness sandboxes commands or blocks network
  access, ask the user to approve running these commands outside the sandbox.

## 1. Check the machine

```bash
uname -sm; command -v curl; command -v git; df -Pk "$HOME" | tail -n 1
```

- Linux or macOS (`Darwin`) on x86_64/amd64 or aarch64/arm64: continue.
- Windows (`uname` is missing, or reports MINGW, MSYS or CYGWIN): stop. This
  installer does not run there. Give the user the Windows installer,
  `Tofu-Setup-*-win64.exe`, from
  <https://github.com/rangehow/ToFu/releases/latest>.
- Any other CPU architecture: stop and point the user to the Docker
  instructions in [INSTALL.md](INSTALL.md#docker).
- `curl` is required. `git` is optional; without it the installer takes its
  conda path, which provides git.
- The filesystem that will hold the install directory needs at least 2 GB free
  (4 GB recommended). The fourth column of the `df` line is free space in KiB.

## 2. Choose the install directory

- Use `$HOME/tofu` unless the user named another directory. If it already holds
  a Tofu installation, the same command updates and repairs it and keeps its
  data, settings, port and local edits.
- If your current directory is itself a Tofu checkout (it contains
  `install.sh`, `server.py`, `requirements.txt` and `lib/tasks_pkg/`), install
  that checkout with the second command in step 3.
- Always pass the directory explicitly, as both commands do, so the target
  never depends on the directory your shell happens to be in.

## 3. Start the installer

A first installation can take many minutes, longer than many harnesses let a
foreground command run. Start the installer detached from your command so a
tool timeout cannot kill it, then poll it in step 4:

```bash
R="${TMPDIR:-/tmp}/tofu-install-$(date +%Y%m%d-%H%M%S)-$$"
nohup $(command -v setsid) bash -c 'set -o pipefail; curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh | bash -s -- --non-interactive --result-json "$1" --dir "$2" "${@:3}"; echo "TOFU_INSTALL_EXIT=$?"' tofu-install "$R.json" "$HOME/tofu" >"$R.log" 2>&1 </dev/null &
echo "receipt=$R.json log=$R.log"
```

To install elsewhere, replace `"$HOME/tofu"`. To pass another installer option,
such as `--port 15080`, append it after the directory argument.

For an existing Tofu checkout (step 2), run this from the checkout instead. It
keeps the checkout's code unchanged:

```bash
R="${TMPDIR:-/tmp}/tofu-install-$(date +%Y%m%d-%H%M%S)-$$"
nohup $(command -v setsid) bash -c 'bash install.sh --non-interactive --no-update --result-json "$1" --dir "$2" "${@:3}"; echo "TOFU_INSTALL_EXIT=$?"' tofu-install "$R.json" "$PWD" >"$R.log" 2>&1 </dev/null &
echo "receipt=$R.json log=$R.log"
```

Note the two printed paths; your later commands may run in a fresh shell where
`$R` no longer exists. If your harness has its own way to run a command in the
background and read its output later, you may use it for the same `bash -c`
command. The installer never overwrites a receipt, so run the whole block again
for every rerun; it creates a new receipt path.

## 4. Wait for it to finish

Every 30 to 60 seconds (keep any `sleep` shorter than your command timeout):

```bash
tail -n 15 <log>; grep TOFU_INSTALL_EXIT <log>
```

The installer has finished when the log contains a `TOFU_INSTALL_EXIT=<code>`
line. Keep waiting while the log grows. Do not kill the installer: each step
has its own deadline, and a host that has to build a native library from source
can legitimately spend up to 30 minutes in one step.

If the log stops changing and has no `TOFU_INSTALL_EXIT` line, check whether
the installer still runs: `ps -ef | grep -F <receipt> | grep -v grep`. If
nothing is running, your harness ended it; continue with step 5.

## 5. Read the outcome

Read the receipt with `cat <receipt>`. It is JSON with `status`, `exit_code`,
`phase`, `install_dir`, `python`, `url` and `log`; see
[INSTALL.md](INSTALL.md#reruns-and-agent-result-contract).

| What you see | Meaning | Next |
|---|---|---|
| `status` is `ready` | Tofu is installed, running and verified at `url` | Step 6 |
| `status` is `restart-required` | Installed and verified, but a Tofu that was already running still serves its older code | Step 6, and ask the user to run `cd <install_dir> && <python> serverctl.py restart` in their own terminal; it asks them to confirm |
| `failed` with `exit_code` 130 or 143, or no receipt, no `TOFU_INSTALL_EXIT` line and nothing running | The installer was interrupted, usually by your harness | Rerun step 3 once; reruns are safe. If it is interrupted again, give the user the command for their own terminal (below) |
| `failed` with any other `exit_code` | The installer stopped during `phase` | Diagnose below |
| No receipt, but a `TOFU_INSTALL_EXIT` line | It failed before it could write a receipt, for example while downloading | Diagnose from `<log>` |

The command for the user's own terminal is
`curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh | bash -s -- --dir <install directory>`
(from a checkout: `bash install.sh --no-update`).

To diagnose, read the end of the receipt's `log` (or `<log>`), find the first
error, and match it:

- `Port N is already in use`: another program owns that port. Do not stop it;
  rerun step 3 with `--port <a free port>`.
- `Another install owns <path>`: if an installer is still running, wait for it.
  If none is (an earlier run was killed), remove only that exact empty
  directory with `rmdir <path>`, then rerun.
- `Install target is non-empty but is not a Tofu checkout`: ask the user for a
  new or empty directory and rerun with it. Never empty the existing one.
- `Not enough free disk space`: tell the user; rerun after they free space or
  choose another directory.
- Network, proxy or certificate errors: report them. Mirror and proxy settings
  are in [INSTALL.md](INSTALL.md#offline--mirror--proxy); never disable
  verification.
- Anything else: if the receipt has a `python`, run these read-only checks
  from `install_dir` and report what they show:
  `<python> serverctl.py status --json`, `<python> serverctl.py doctor --json`
  and `<python> serverctl.py logs -n 200`.

Rerun only after fixing a concrete cause, at most once per cause. The installer
already retries transient network failures itself.

## 6. Report to the user

Tell the user, with the values from the receipt:

- Tofu is running at `url`. Open it in a browser on this machine. In its default
  open mode Tofu answers only local (loopback) connections, so from another
  computer forward the port first, for example
  `ssh -L 15000:localhost:15000 <this machine>`.
- Next: in Tofu, open **Settings → Providers** and add a model provider with
  their API key.
- Optional: browser features need the companion extension; install it from
  **Settings → Search & Web Access → Install or upgrade extension**.
- Manage the service from `install_dir` with `<python> serverctl.py status`,
  `stop` or `restart`; `stop` and `restart` ask for confirmation.
- To update Tofu later, paste the same line into the agent again.
- The install log is at `log`.
