# Change windows (working name)

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

### The board

**Board**:
A Hermes Kanban board. All profiles on a gateway share it.

**Card**:
One piece of work on a board, assigned to one profile.
_Avoid_: Task, ticket, issue

**Release**:
To make a parked card eligible to run.
_Avoid_: Wake, unblock, start

### The plan

**Plan**:
All slots of one board.
_Avoid_: Schedule, calendar, roadmap

**Slot**:
The planned start time and the estimate of one card. A card has at most one slot.
_Avoid_: Booking, time window, schedule entry

**Estimate**:
The expected duration of the work on a card. It is a guess, not a limit.

**Horizon**:
The time span that the owner looks at: the next day, the next two days, or the week.
_Avoid_: Time window, range, interval

**Clash**:
Two slots that overlap in time and touch the same thing.
_Avoid_: Conflict, overlap, collision

**Label**:
A named piece of metadata on a card, such as a repository, a project, or a kind of operation.
_Avoid_: Category, tag

### Version 0.1 terms

**Window**:
An owner-approved time range for one scope. Matched production writes are allowed only inside it.
_Avoid_: Slot (a slot belongs to one card; a window belongs to one scope)

**Scope**:
The name of the thing that a window protects, such as one cluster or one repository.
