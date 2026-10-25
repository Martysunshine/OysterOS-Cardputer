"""Claude Code link: answer "may Claude Code run this tool?" from a paired device, and show a usage line.

Modules: models (wire shapes and limits), store (its own SQLite file, append-only audit), service (the
approval state machine, redaction, the long poll), api (routes), mount (install(app)). The PC-side scripts
that call the hook-facing routes live in integrations/claude-code/. See docs/CLAUDE_CODE_LINK.md.

Nothing here executes anything: the backend only records a human's allow or deny for a request the hook
made, and the hook falls back to Claude Code's normal prompt whenever there is no recorded device answer.
"""
