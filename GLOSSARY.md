# Chronomancer

A Hermes plugin. It lets an owner and an orchestrator plan when the Kanban cards of every profile on a gateway run, and it shows where the planned work clashes.

## Language

### People and agents

**Owner**:
The person who runs the gateway and has the final say on the plan.
_Avoid_: User, operator, human

**Profile**:
A named Hermes agent on a gateway, with its own configuration and memory. Cards are assigned to profiles.
_Avoid_: Bot, worker, agent (when you mean one named profile)

**Orchestrator**:
The profile that plans the work of the other profiles for the owner.
_Avoid_: Foreman, manager, assistant

**Gateway**:
One Hermes process that serves one or more profiles.

**Actor**:
Who made a change of the plan: the owner, an agent profile, or the timer.

### The board

**Board**:
A Hermes Kanban board. All profiles on a gateway share it.

**Card**:
One piece of work on a board, assigned to one profile.
_Avoid_: Task, ticket, issue

**Park**:
To make a card wait, so that it cannot start before its slot.
_Avoid_: Schedule, hold, freeze

**Release**:
To make a parked card eligible to run.
_Avoid_: Wake, unblock, start

### The plan

**Plan**:
All slots of one board.
_Avoid_: Schedule, calendar, roadmap

**Slot**:
The planned start and the estimate of one card. A card has at most one slot.
_Avoid_: Booking, time window, schedule entry

**Estimate**:
The expected duration of the work on a card. It is a guess, not a limit.

**Horizon**:
The time span that the owner looks at: the next day, the next two days, or the week.
_Avoid_: Time window, range, interval

**Clash**:
Two slots that overlap in time and share a one-at-a-time label.
_Avoid_: Conflict, overlap, collision

**Busy**:
Two slots of one profile that overlap in time and do not clash.

**Label**:
A named piece of metadata on a card, such as a repository, a project, or a kind of operation.
_Avoid_: Category, tag

**One-at-a-time label**:
A label that names a thing only one card may touch at a time, such as a cluster or a database.
_Avoid_: Exclusive label, lock, resource

**Pin**:
The owner's mark on a slot that the orchestrator must not move.
_Avoid_: Lock, freeze

**Run now**:
To release a card at once and move its slot to now.

**Timer**:
The part of the plugin that releases a card when its slot starts.
_Avoid_: Scheduler, cron, wake job

**Change log**:
The record of every change of the plan, with its actor. The page calls it "Changes".
_Avoid_: Audit, history, ledger

**Tray**:
The open cards that have no slot. The page calls it "Unplanned".
_Avoid_: Backlog, inbox
