---
status: accepted
---

# No approval step: both plan, the owner pins

Version 0.1 let only the owner approve, through a channel an agent could not forge. The owner now wants the orchestrator to decide for him, and to change the plan himself at any time without a queue of requests. We decided that there is no approval step. The owner and the orchestrator change the same plan. Every change records its actor (`owner`, `agent:<profile>`, `timer`), and the orchestrator's tools tell it what the owner changed since it last looked. The owner can pin a slot; the agent tools refuse to move, run or clear a pinned slot.

## Consequences

- A pin is a courtesy between the owner and well-behaved agents. The Desktop backend trusts the local Desktop session, and code that runs as the same OS user can call it. The README says so.
- An agent that plans badly is corrected after the fact, from the change log, not before.
