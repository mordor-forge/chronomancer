---
status: accepted
---

# A clash needs a shared one-at-a-time label

Two slots that overlap in time are normal: agents work in parallel. A warning on every overlap would be noise, and the owner would stop reading it. We decided that two slots clash only when they overlap and share a label marked "one card at a time" (a cluster, a database). Two overlapping slots of one profile that share no such label are only "busy": a softer mark.

## Consequences

- The plan finds no clash until someone marks a label as one-at-a-time. The orchestrator can do that through a tool, and so can the owner.
- The rule knows nothing about what a card really touches. A wrong or missing label gives a wrong answer.
