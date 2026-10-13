"""Opt-in order execution for the Triple Screen (see CLAUDE.md, "Execution layer").

The scanner decides; this package turns the decisions of ONE horizon into
Hyperliquid orders, through an API wallet that can trade but not withdraw:

- `orders`   — order/cloid types and price helpers (pure).
- `planner`  — what to do this cycle, from the snapshot + the account (pure).
- `gateway`  — where orders go: a dry run that sends nothing, or Hyperliquid.
- `account`  — the account as the exchange reports it (public info reads).
- `state`    — what the bot remembers between runs (owned positions, events).
- `engine`   — runs a cycle: read, plan, send, remember, alert.
"""
