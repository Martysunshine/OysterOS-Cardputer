[OysterOS](../../README.md) / integrations / claude-code

# Claude Code link: PC-side scripts

Two small scripts (Python 3.12, **standard library only**) connect Claude Code to the Oyster backend on this PC:

| File | Claude Code feature | What it does |
| --- | --- | --- |
| `oyster_hook.py` | `PermissionRequest` hook | Sends a short, redacted summary of the tool call to the backend, waits for the owner to press allow or deny on the Cardputer, and prints the decision JSON Claude Code expects. On **any** problem it prints nothing, so Claude Code shows its normal permission prompt. |
| `oyster_statusline.py` | `statusLine` command | Prints a one-line status for the terminal (`Opus \| ctx 8% \| $0.01 \| 5h 24% 7d 41%`) and posts the same numbers to the backend so the Cardputer can show them. |
| `settings.example.json` | | The snippet to paste into your own Claude Code settings. |

Design, threat model, API and limits: [`docs/CLAUDE_CODE_LINK.md`](../../docs/CLAUDE_CODE_LINK.md).

## This is opt-in

Nothing in this repository edits any Claude Code settings file. The link only exists once **you** paste the
snippet from `settings.example.json` into your settings. Without it, Claude Code behaves exactly as before.

## Set up

1. Backend running (`oyster serve`) with `OYSTER_ADMIN_TOKEN` set in `backend/.env`, and your Cardputer paired.
2. Check the scripts without Claude Code: `python integrations/claude-code/oyster_hook.py --selftest`
   (starts a fake local server, prints one `ok` line per check, exits 1 if a check fails).
3. Open your Claude Code settings: `~/.claude/settings.json` (all projects) or `.claude/settings.local.json`
   (one project, not committed). Merge the `hooks` and `statusLine` blocks from `settings.example.json` and
   replace `<ABSOLUTE_PATH_TO_REPO>` with the absolute path of this checkout. Forward slashes are fine on
   Windows. If you already have a `statusLine` or `PermissionRequest` entry, keep yours and merge by hand.
4. Start a new Claude Code session, ask it to run a command that needs permission, and watch the device (or
   see "Answering without the Cardputer" below).

### Matchers

The `matcher` selects tool names (exact names joined by `|`, anything else is a regular expression):

- `"Bash"` — start here.
- `"Bash|PowerShell"` — on Windows, when Claude Code uses the PowerShell tool (on Windows without Git Bash
  it registers PowerShell instead of Bash, and a `Bash`-only matcher never fires there).
- `"Edit|Write|MultiEdit"` — file changes; only the **file name** is sent, never a path or content.
- `"mcp__.*"` — every MCP tool; only the tool name is sent.

The hook only sees tool calls for which Claude Code is *about to ask you*; calls your permission rules
already allow are never sent to the device.

### Settings of the scripts (environment variables, all optional)

| Variable | Default | Meaning |
| --- | --- | --- |
| `OYSTER_BACKEND` | `http://127.0.0.1:<OYSTER_HTTP_PORT or 8000>` | Must be a loopback URL: the owner token is sent there and the scripts refuse any other host. |
| `OYSTER_ADMIN_TOKEN` | read from the env file | The owner token. |
| `OYSTER_ENV_FILE` | `backend/.env` of this checkout | `.env` file that holds `OYSTER_ADMIN_TOKEN` (and optionally `OYSTER_HTTP_PORT`). |
| `OYSTER_WAIT_S` | `90` | Seconds to wait for the device, 1 to 120. A shorter value for a first try (for example `20`) is sensible. The example settings give the hook a `timeout` of 110 s; raise it if you raise the wait above 100. |
| `OYSTER_ON_TIMEOUT` | `ask` | `deny` makes "no answer in time" a deny instead of the normal prompt. Errors always fall back to the normal prompt. |
| `OYSTER_HOOK_DISABLE` | unset | `1` switches the hook off without editing the settings. |

Set them in the shell that starts Claude Code, or in the `env` block of your Claude Code settings. The token is
never printed, logged or written anywhere by these scripts.

### If your Claude Code is older

`settings.example.json` uses the "exec form" (`command` plus `args`, no shell, no quoting problems with spaces in
the path). If your Claude Code rejects `args`, use the shell form instead:
`"command": "python \"<ABSOLUTE_PATH_TO_REPO>/integrations/claude-code/oyster_hook.py\""` and no `args`.
Do not set `onFailure: "block"` on this hook: it would turn every failure (backend not running) into a deny.

## Answering without the Cardputer

On the Cardputer the requests are under Control Deck > Claude Code. You can also answer with the device token
on the loopback port (the loopback listener also serves the device routes). With `<DEVICE_TOKEN>` from your
pairing:

```
curl -s -H "Authorization: Bearer <DEVICE_TOKEN>" http://127.0.0.1:8000/v1/claude/approvals
curl -s -X POST -H "Authorization: Bearer <DEVICE_TOKEN>" -H "Content-Type: application/json" \
     -d '{"decision":"allow","nonce":"<NONCE FROM THE LIST>"}' \
     http://127.0.0.1:8000/v1/claude/approvals/<APPROVAL_ID>/decide
```

## What to expect

- Hook silent and prompt shown: backend not running, not configured, no answer within `OYSTER_WAIT_S`, or any error. That is the safe path.
- Hook prints `allow` or `deny`: only after the device answered that exact request.
- In sessions that cannot show a prompt (the documentation names background subagents in non-interactive
  mode), Claude Code itself denies a call when no `PermissionRequest` hook decides, so silence there means
  "denied".
- The documentation does not say whether the terminal's own permission dialog is already visible while the
  hook is waiting, or only appears after the hook returned. If it only appears afterwards, a hook that waits
  the full 90 s delays the dialog by that long when nobody answers on the device; a short `OYSTER_WAIT_S`
  limits that.

## Remove it

Delete the `PermissionRequest` entry and the `statusLine` entry from your Claude Code settings. To pause
without editing settings, set `OYSTER_HOOK_DISABLE=1`. To also drop the stored data, stop the backend and
delete `claude.db*` in its data directory (the audit trail is in that file).
