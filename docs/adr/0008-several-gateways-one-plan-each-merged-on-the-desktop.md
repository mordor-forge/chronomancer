---
status: proposed
---

# Several gateways: one plan per gateway, merged on the Desktop

A Kanban board is one SQLite file under the Hermes root of one host. A gateway on another machine has its own boards, and nothing syncs them. An owner who runs several gateways wants one timeline for all of them. We propose, and have not built: each gateway runs the plugin and keeps its own plan, so ADR 0004 stays true for each gateway. The Desktop page reads the plan of every gateway it is connected to and shows one timeline, with the lanes grouped by gateway. Each gateway finds its own clashes; the page compares slots of different gateways that share a one-at-a-time label by name.

## Considered options

- One gateway plans the boards of the others through a remote call: it needs a transport and a trust model between gateways, and one gateway that is down stops the plan of all.

## Consequences

- Every gateway needs a Hermes version at the plugin's floor.
- A release still happens on the gateway that owns the card, so a gateway that is down delays only its own cards.
- Version 0.1 plans one board on one gateway. The README says so.
