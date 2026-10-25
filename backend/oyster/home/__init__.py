"""Smart Home: Home Assistant control for the Cardputer's Smart Home tile, behind an owner-written allowlist.

Modules: config (environment: URL, token, CA, allowlist path), allowlist (rooms > entities, validation, the
action-to-service map), adapter (the Home Assistant REST client), store (its own SQLite file: append-only
audit and idempotent replays), service (the rules), api (routes), mount (install(app)).

Nothing outside the allowlist can be read or controlled. The token and the URL come from the environment
(backend/.env), are never logged and never leave the backend. Not verified against a real Home Assistant
instance yet: see docs/HOME_ASSISTANT.md.
"""
