"""Smart Control Deck: owner-defined macro profiles (slots 1..9) that run allowlisted, typed actions.

Modules: actions (the allowlist and the executor seam), profiles (validation of owner-defined profiles),
jobs (state machine, idempotency, confirmation), audit (append-only audit and history), store (its own
SQLite file), runner (bounded single worker), api (routes), mount (install(app)).

Only a mock executor ships: nothing here contacts or changes a real host. See docs/CONTROL_DECK.md.
"""
