---
status: accepted
---

# One plan per board, kept in the orchestrator's profile

The owner wants to see and plan the cards of every profile. A Hermes plugin and its data belong to one profile, and a guard-style plugin would need a copy in each profile plus a reader that merges them. But the Kanban board is one database shared by all profiles on a gateway, and its tools return every profile's cards. We decided that the board is the cross-profile layer: the plugin runs in one profile (the orchestrator's, which is also the profile the owner's Desktop connects to), keeps one plan for the board there, and needs nothing in the other profiles.

## Considered options

- A store per profile and a merging reader: more moving parts, and two plans can disagree.
- A store outside any profile: no public plugin API gives such a place.

## Consequences

- Enabling the plugin in two profiles gives two plans for one board. The README says: one profile.
- More than one board, and more than one gateway, wait for a later version.
