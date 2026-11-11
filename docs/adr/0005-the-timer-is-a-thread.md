---
status: accepted
---

# The timer is a thread, not the Kanban tick hook

The Kanban has no timer, so the plugin must release a card when its slot starts. The obvious seam is the hook `on_kanban_dispatch_tick`, which fires every 60 seconds. It does not work for us: the dispatcher runs its tick outside any profile scope, so the hook reaches only the plugins of the gateway's own profile, and ADR 0004 puts the plugin in another profile. We decided to start one daemon thread when the plugin loads. It wakes at the next slot start and calls a release pass that is safe to repeat.

## Consequences

- The thread never reads the configuration or the plugin data path, because outside a profile scope both resolve to the wrong profile. The path is fixed at load time, and settings are read only inside a tool call, a Desktop request or the load itself.
- Every process that loads the plugin runs the thread, except Kanban workers. Each due slot is leased to one caller in the plan database, so a card is released once.
- Checked on a gateway that serves two profiles, with the plugin enabled only in the second: the thread released a due card with no other activity.
