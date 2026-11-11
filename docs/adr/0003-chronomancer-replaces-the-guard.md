---
status: accepted
---

# Chronomancer replaces the guard and its Telegram half

Version 0.1 was `change-windows`: a `pre_tool_call` guard that blocked production writes outside an owner-approved window, with approvals in Telegram. ADR 0001 removed its purpose. We decided to remove the guard, the rules, the windows, the approval flow and every Telegram part, and to rename the plugin to `chronomancer`. One plugin now tells one story: a shared plan for the Kanban. The guard also had open problems (a self-protect rule that blocked files that only mention it) that we no longer carry.

## Consequences

- The old code stays in the git history and on the branch `guard-v0.1`.
- The plugin registers no hook. Nothing it does can block a tool call.
