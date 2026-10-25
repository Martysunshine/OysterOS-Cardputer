# Claude Code link: yes/no approvals and a usage line on the Cardputer

Lets the owner answer "may Claude Code run this tool?" (allow / deny) from the Cardputer, and see a simple
usage line, without going back to the PC. Backend code: `backend/oyster/claude/`. PC-side scripts:
`integrations/claude-code/`. Tests: `tests/claude/`. Setup in short: `integrations/claude-code/README.md`.

## Status, stated plainly

- **Backend and PC scripts only.** The Cardputer screens are not built yet (separate step); the contract they
  will need is in "Contract for the firmware screens". Until then the device routes can be driven with `curl`
  and a device token (see the README of the integration).
- **Never run against a live Claude Code session.** The tests use a fake Claude Code input (JSON in the shape
  the documentation specifies), a fake backend for the scripts, and the real app served by uvicorn on an
  ephemeral loopback port for the end-to-end checks. What Claude Code does with the hook's output is taken from
  its documentation and is **unverified** until the owner has tried it.
- **Nothing is enforced here.** An "allow" from the device only tells Claude Code to skip its own prompt for
  that one call. Claude Code still evaluates its deny and ask rules afterwards (the documentation says a hook's
  allow does not override a matching deny rule). The link never adds permanent permission rules.
- **Plan-level usage numbers are passed through, never produced.** See "Usage line".

## Documentation relied on

Read on 2026-10-25 from `https://code.claude.com/docs/en/hooks.md` (sections *PermissionRequest*,
*PermissionRequest decision control*, *Exit code output*, *Timeouts*, *Block the action when a hook fails*,
*Exec form and shell form*, *Matcher patterns*, *PreToolUse input*) and `https://code.claude.com/docs/en/statusline.md`
(*Manually configure a status line*, *Available data*). If those pages change, re-check the shapes below. The
scripts tolerate unknown fields and fall back to "no decision" on anything they do not understand.

### Event: `PermissionRequest`

Chosen over `PreToolUse` because it "runs only when Claude Code is about to ask you for permission", which is
exactly the question the owner wants to answer. `PreToolUse` fires before every tool call, including those
Claude Code would allow without asking, and would flood the device.

Input on stdin (fields the hook reads are marked with *; the rest is ignored):

```json
{
  "session_id": "abc123",                  *
  "transcript_path": "/…/session.jsonl",
  "cwd": "/…",
  "permission_mode": "default",
  "hook_event_name": "PermissionRequest",  *
  "tool_name": "Bash",                     *
  "tool_input": { "command": "rm -rf node_modules", "description": "…" },   *
  "permission_suggestions": [ { "type": "addRules", "…": "…" } ]
}
```

`tool_input` per tool (from *PreToolUse input*): `Bash` and `PowerShell` carry `command`; `Write` and `Edit`
carry `file_path` (always absolute, with backslashes on Windows) plus content fields that are never read.
`MultiEdit` and `NotebookEdit` are handled like `Edit` when they appear (`file_path` / `notebook_path`).

Output on stdout, exit code 0 (the only exit code this hook ever uses):

| Outcome | stdout | Effect documented by Claude Code |
| --- | --- | --- |
| allow | `{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}` | the permission is granted for this call |
| deny | `{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "deny", "message": "Denied from the Oyster device."}}}` | denied; `message` tells Claude why; `interrupt` is not set, so Claude is not stopped |
| no decision | nothing at all | exit 0 with no output is "no decision": the normal permission prompt appears |

Not used on purpose: `updatedInput` (the tool input is never altered), `updatedPermissions` (a key press on
the device never creates a standing rule), `interrupt`. Exit code 2 "isn't honored for this event"; any exit
code other than 0 or 2 is a non-blocking error, and the documentation warns that on Windows without Git Bash a
shell-form hook failing that way can block the call, which is why the hook always exits 0 and the example
settings use the exec form (`command` plus `args`, no shell). `onFailure: "block"` must **not** be set: on this
event it turns a failure into a deny.

`timeout` in the hook entry is in seconds (default 600). The example sets 110, a little above the default
wait of 90 s. When a hook times out Claude Code renders no decision, which is again the normal prompt.

The documentation also states: "In sessions that can't show a prompt, such as background subagents in
non-interactive mode, Claude Code still runs these hooks, and if no hook returns a decision, it denies the tool
call." There, silence from this hook therefore means Claude Code's own default: deny.

### Status line

`statusLine` with `"type": "command"`; Claude Code writes a JSON object to stdin and shows what the script
prints to stdout. Fields read (all optional on the Claude Code side):

| Field | Used for |
| --- | --- |
| `session_id` | key of the stored line (the last 8 sessions are kept) |
| `model.display_name`, else `model.id` | model name |
| `cost.total_cost_usd` | "Estimated session cost in USD, computed client-side at list price … may differ from your actual bill" |
| `context_window.used_percentage` | context use (may be `null` early in a session; then left out) |
| `rate_limits.five_hour.used_percentage`, `rate_limits.seven_day.used_percentage` | **only when present**: the documentation says `rate_limits` "appears only for claude.ai Pro and Max subscribers … and only after the first API response" |

## Flow

```
Claude Code (PC)             oyster_hook.py              Backend (PC)                         Cardputer
      |  PermissionRequest        |                          |                                    |
      |--- JSON on stdin -------->|                          |                                    |
      |                           |-- POST /v1/claude/approvals (owner token, 127.0.0.1) -------->|
      |                           |   {session, tool, summary, wait_s}   creates appr-…, pending, |
      |                           |   (request stays open: long poll, expiry = now + wait_s)      |
      |                           |                          |<-- GET /v1/claude/approvals -------|  (device token, HTTPS)
      |                           |                          |--- tool, summary, expires_in_s, ---|->
      |                           |                          |    nonce (made for this device)    |  owner reads, presses
      |                           |                          |<-- POST …/appr-…/decide -----------|  Enter = allow, Back = deny
      |                           |                          |    {decision, nonce}               |
      |                           |                          |    nonce ok + still pending -> record, audit
      |                           |<-- {"decision": "allow"|"deny"|"timeout", "approval_id"} ----|
      |<-- allow / deny JSON -----|   (or nothing = normal prompt)                                |
```

Every other ending prints nothing: window closed (`timeout`), backend not running, token missing, URL not
loopback, bad JSON, any exception. A hard timer in the script ends the process silently if anything stalls.

## What gets sent, and what never is

The hook builds the summary, the backend redacts it a second time with the same helper the AI path uses
(`oyster.ai.redact`) plus the patterns below, and the device receives at most 80 printable ASCII characters.

| Tool | Summary |
| --- | --- |
| `Bash`, `PowerShell` | the command, redacted, whitespace collapsed; line breaks are shown as a literal `\n` so a hidden second line is visible. At most 80 characters; a longer command ends with `...[N]` where N is its full length |
| `Edit`, `Write`, `MultiEdit`, `NotebookEdit` | the file **name** only (no directory, no content) |
| anything else | empty; the device shows the tool name |

Redaction (best effort, deliberately greedy): PEM blocks, `Bearer …`, `key=value` and `key: value` where the key
looks like password, token, secret, api key, authorization, credential, cookie, private/access/session key or
signature; flags such as `--password x`; credentials inside URLs; vendor token shapes (`ghp_…`, `github_pat_…`,
`glpat-…`, `AKIA…`, `sk-…`, `gsk_…`, `AIza…`, JWTs); hex runs of 32+ characters; any run of 32+ token-like
characters containing both letters and digits; and the value of every `*_TOKEN`, `*_KEY`, `*_SECRET`,
`*_PASSWORD` variable in the backend's own environment. Redaction can miss a secret that has no recognisable
shape (a short password passed as a bare argument, for example): the summary is shown on the Cardputer screen
and kept in `claude.db`, so approve only what you are comfortable seeing there.

The owner token and device tokens are never stored or logged by this module, and nonces never appear in the
audit table or the owner views (a nonce lives in `claude.db` only while its approval is pending and is deleted
when the approval ends). The tests search the database files byte for byte for the tokens, and the audit rows
and owner views for the nonce.

## API

Errors are `{"error": "<code>", "detail": "<text>"}` as in `docs/API.md`: 401 missing or invalid token, 403 wrong
role, 404 unknown id, 409 state conflict, 413 body too large, 422 validation, 429 rate limit (with
`Retry-After`), 503 too many waiting (with `Retry-After`). The link is installed by `install(app)` in
`oyster.main.create_app`; the device routes are served on both listeners, the owner routes only on the loopback
listener.

| Method and path | Auth | Request | Reply |
| --- | --- | --- | --- |
| `POST /v1/claude/approvals` | owner, loopback only | `{session` (1..64, `[A-Za-z0-9_.:-]`), `tool` (1..40, same set), `summary` (0..160), `wait_s` (integer 1..120, default 90)`}`, body <= 2 KiB, unknown fields 422 | long poll, then 200 `{"decision": "allow" \| "deny" \| "timeout", "approval_id": "appr-…"}`. 503 `busy` when 8 approvals already wait, 429 above 30 requests a minute |
| `GET /v1/claude/approvals` | device | none | 200 `{"approvals": [ … up to 5, newest first … ]}` |
| `POST /v1/claude/approvals/{id}/decide` | device | `{"decision": "allow" \| "deny", "nonce": "…"}` (nonce 8..128), body <= 4 KiB | 200 `{"approval_id", "state": "allowed" \| "denied", "decision"}`. 403 wrong or missing nonce, 404 unknown id, 409 `expired` or `already_decided`, 429 above 30 attempts a minute per device |
| `GET /v1/claude/status` | device or owner | none | 200 `{"configured": false}` or the usage line (below) |
| `POST /v1/claude/status` | owner, loopback only | `{session, model` (1..40 printable ASCII), `cost_usd` (number 0..1,000,000), `context_pct?`, `five_hour_pct?`, `seven_day_pct?` (numbers 0..100)`}`, body <= 1 KiB | 200 `{"stored": true}`; the last 8 sessions are kept |
| `GET /v1/claude/audit` | owner, loopback only | `limit` 1..50 (default 10), `before`, `approval_id` | 200 `{"entries": […], "next_before": n \| null}` |

Numbers must be real JSON numbers (a string such as `"1.5"`, a boolean, `NaN` or infinity is 422).

## Contract for the firmware screens

Device list item (`GET /v1/claude/approvals`), at most 5, worst case about 1.3 KiB for the whole reply (a test
keeps it under 4 KiB):

```json
{"approvals": [{"approval_id": "appr-0a1b2c3d4e5f6071", "tool": "Bash", "summary": "npm test",
                "age_s": 12, "expires_in_s": 78, "nonce": "Xk3p…22 characters"}]}
```

| Field | Size and meaning |
| --- | --- |
| `approval_id` | 21 characters, `appr-` plus 16 hex |
| `tool` | 1..40 characters, `[A-Za-z0-9_.:-]` (MCP tools look like `mcp__server__tool`) |
| `summary` | 0..80 printable ASCII characters, already redacted; may end in `...` or `...[N]` (clipped; N is the original length); may be empty (show the tool name only) |
| `age_s`, `expires_in_s` | integers; the window is at most 120 s. Count `expires_in_s` down locally between polls |
| `nonce` | 22 characters, made for **this device** and this approval the first time the device lists it; the same value on later lists while the approval is pending. Send it back unchanged |

Decision: `POST …/decide` with `{"decision":"allow"|"deny","nonce":"…"}`. Reply handling:

| Reply | Show |
| --- | --- |
| 200 | "Allowed" / "Denied" |
| 409 `expired` | "Too late: it timed out" (the PC already fell back to its own prompt) |
| 409 `already_decided` | "Already answered" (also what a retry after a lost reply gets: do not retry automatically) |
| 403 | "Rejected": the nonce is stale or foreign; list again |
| 404 | "No longer there" |
| 429 | wait `Retry-After` seconds |

Interaction proposal, following the module framework's confirm pages: Enter approves, Back rejects, the UI
ignores keys for the first 700 ms; an unanswered approval simply expires on the PC, so the device never needs a
"cancel". Poll while the screen is open (the device already polls every few seconds; the list limit is 120
requests a minute per device).

Usage line (`GET /v1/claude/status`), under 200 bytes:

```json
{"configured": true, "updated_s_ago": 7, "model": "Opus", "cost_usd": 0.0123,
 "context_pct": 8.0, "five_hour_pct": null, "seven_day_pct": null}
```

`context_pct`, `five_hour_pct` and `seven_day_pct` are `null` when Claude Code did not send them. Show
`updated_s_ago` so a stale line is recognisable, label the cost as an estimate, and show the two plan percentages
only when they are not `null`.

## Usage line: what is and is not available

An early plan for this feature assumed that plan-level usage limits cannot be read from Claude Code. The
current status-line documentation says otherwise for **claude.ai Pro and Max subscribers**: the status-line JSON
then carries `rate_limits.five_hour.used_percentage` and `rate_limits.seven_day.used_percentage` (0..100,
"after the first API response in the session", each window possibly absent). The scripts forward exactly those
two numbers when present, store them as given and show them as given. Nothing else is derived: no remaining
quota, no reset countdown, no projection, and no value when Claude Code sent none (API-key or gateway sessions
send no such field, so those users see model, context and cost only). `cost_usd` is Claude Code's own estimate
at list price. If the percentages are not wanted, drop the two lines for them in `oyster_statusline.py`
(`read_values`); the backend fields are optional.

## Threat model

Assets: the owner's PC (what Claude Code may do), the owner token (`OYSTER_ADMIN_TOKEN`), device tokens.

| Who | Can | Cannot |
| --- | --- | --- |
| The hook (owner token, `127.0.0.1` only) | create an approval, report status, read the audit | decide an approval: the decide route needs a device token and a nonce the owner token cannot obtain (403) |
| A paired device (device token, HTTPS) | list pending approvals, answer one it listed within its window, read the usage line | create an approval or status, read other devices' nonces, answer after the window, answer twice |
| A LAN host without a token | nothing (401; 10 failures a minute from one address block it for 60 s) | reach any owner route: the LAN listener does not serve them, and the owner token is refused there (403) |
| Another paired device | list and answer like the first one (approvals are not tied to one device; the first answer wins) | overturn an answer: a second decision is 409 |
| Whoever holds the admin token on the PC | create approvals, read the audit | **not protected**: that person can also edit Claude Code's settings, remove the hook or approve in the terminal; this feature is a convenience path, not a stronger gate |

Properties and how they are enforced:

- **A device cannot approve something it did not see.** The decide route needs the nonce issued to that device
  for that approval, and a nonce is only issued by listing (which is what puts the request on its screen). A
  nonce is bound to (device, approval): another device's nonce, another approval's nonce, a guessed one and a
  missing one are all 403, compared in constant time, never echoed. Honest limit: the nonce proves the device
  fetched the request, not that a human looked at it or pressed a key; the key press is not observable by the
  backend (same as the Control Deck, ADR-005).
- **No allow without a device decision.** `allowed` is reachable only through `decide`, which records the deciding
  device; the table has a CHECK that rejects an `allowed` or `denied` row without a device, and the long poll
  maps `expired` and `cancelled` to `timeout`. The hook additionally ignores an allow reply that lacks an
  approval id.
- **Expiry.** An approval lives `wait_s` seconds (default 90, at most 120). After that, listing hides it and a
  decision is 409 `expired`. A backend restart or stop ends every pending approval (`backend_restarted`,
  `backend_stopping`), because nobody waits for the answer any more.
- **The hook going away cancels the approval.** If the hook process dies or its connection drops, the waiting
  request notices and the approval becomes `cancelled`; a late answer is 409.
- **Loopback-only hook side.** The owner routes exist only on the 127.0.0.1 listener; the scripts refuse a
  non-loopback `OYSTER_BACKEND`, so the owner token is never sent off the machine, and they talk to the backend
  with `http.client`, which ignores proxy settings.
- **Bounded.** At most 8 approvals wait at once (503 beyond), 30 hook requests, 30 decisions per device and 120
  lists per device a minute, bodies 1 to 4 KiB, strings length- and character-bounded, no unknown fields.
- **Audit.** One append-only row per transition (`approval.created`, `.allowed`, `.denied`, `.expired`,
  `.cancelled`) and per refused request (`request.denied` with reasons `unknown_approval`, `expired`,
  `not_pending`, `bad_nonce`, `busy`), written in the same transaction as the state change, with actor, device
  id, tool and approval id, never a summary, token or nonce. The table refuses UPDATE and DELETE at the database
  level. Finished approvals are dropped from `approvals` after 7 days; audit rows are never pruned (about 200
  bytes per transition).
- **No execution.** The backend never runs, forwards or stores a command for execution; it stores a redacted
  one-line description and a human's answer.

What this does **not** protect against:

- Anything running as the PC user: it can read `backend/.env`, edit the Claude Code settings or press keys.
- A stolen, unlocked and still-enrolled Cardputer: it can answer approvals that exist while their window is
  open (never create them). Revoke the device token and use the lock screen.
- A summary that hides the dangerous part. The device sees at most 80 characters of a command. A longer command
  is marked `...[N]` and its line breaks are visible, but the owner decides on what is shown: **deny anything
  you cannot judge from the device and answer it at the PC**. A prompt-injected command can be written so that
  its harmless-looking start fills the 80 characters.
- A second process listening on the backend port before the backend starts: the hook would send the owner token
  to it (loopback ports are not exclusive on Windows). Same trust boundary as every other local owner tool.
- Anyone who can see the Cardputer screen (summaries are shown there) or read `claude.db` (summaries are stored
  redacted, not encrypted).

## Setup for the owner

1. Backend running with `OYSTER_ADMIN_TOKEN` set (existing setup) and the device paired.
2. `python integrations/claude-code/oyster_hook.py --selftest` (13 checks against a fake local server).
3. Merge `integrations/claude-code/settings.example.json` into your Claude Code settings with the absolute path
   filled in (details, matchers, environment variables, removal: `integrations/claude-code/README.md`).
4. Start with `Bash` only and `OYSTER_WAIT_S=20`, run a command that prompts, answer it on the device (or with
   `curl` until the screens exist), and watch `GET /v1/claude/audit` with the owner token.

## Limits and unverified points

| Point | State |
| --- | --- |
| Behaviour inside a real Claude Code session (output accepted, prompt falls back, allow honoured) | **unverified**; matches the documentation read on 2026-10-25 |
| Whether the terminal dialog is visible while the hook waits | **unknown**: the documentation does not say. If it is only shown after the hook returns, an unanswered approval delays the dialog by `OYSTER_WAIT_S` |
| Exec form (`args`) in the owner's Claude Code version | documented in the current hooks page; version needed is not stated. The README gives the shell form as fallback |
| Parallel tool calls | each call runs its own hook process and gets its own approval; the device can hold up to 5 on screen and 8 waiting |
| Cardputer screens for approvals and the usage line | not built |
| Stopping `oyster serve` (Ctrl+C) while a hook waits | uvicorn lets an open request finish: measured 11.8 s of a 12 s wait. `app.state.claude.stop()` ends all waiting requests at once (measured 0.1 s) but `oyster serve` does not call it yet; a second Ctrl+C forces the exit, and pending approvals are ended at the next start either way |
| Delay when the backend is not running | up to about 1 s per prompt on Windows (the script's connect timeout); on a stopped backend nothing else happens |
| Status line cost on Windows | the script starts Python on each refresh; measure it before keeping it |
| Secret redaction | best effort, see above |
| Plan usage | forwarded only when Claude Code sends it (claude.ai Pro and Max) |

## Tests

`python -m pytest tests/claude -o addopts="" -q -p no:cacheprovider` (about one minute). A fake clock drives every
deadline; the hook and status scripts are tested against a fake loopback server and, end to end, as real
subprocesses against the real app served by uvicorn on an ephemeral `127.0.0.1` port.

| Area | File |
| --- | --- |
| State machine: create, list, allow, deny, double decision, expiry at the exact deadline, wrong nonce, wrong device, replay, cancel, capacity, pruning, restart, database guards | `test_claude_service.py` |
| HTTP flow and the long poll | `test_claude_flow.py`, `test_claude_longpoll.py` (concurrent waiters, disconnect cancels, task cancellation, shutdown, "allow only after a device decision") |
| Authorization matrix, listener separation, input bounds, rate limits | `test_claude_api_authz.py` |
| Redaction (nothing secret stored, listed, audited or on disk) | `test_claude_redaction.py` |
| Usage line | `test_claude_status.py` |
| Wiring, lifespan, shared runtime, own database file | `test_claude_mount.py` |
| `oyster_hook.py`: exact output JSON, every failure silent, loopback rule, redaction, summary rules, environment, command line, self-test, standard library only | `test_claude_hook.py` |
| `oyster_statusline.py` | `test_claude_statusline.py` |
| Real script against real server: allow, deny, no answer, secret, killed hook, two hooks, backend stop | `test_claude_e2e.py` |
