# Chronomancer

One plan for the Kanban cards of every profile on a Hermes gateway (0.1.0).

Our agents work a shared Kanban board, and many cards must not start before a set time: a
production change after a review, a migration after its backup. The Kanban can park a card, but it
has no timer. So we released parked cards by hand: one reschedule needed 55 dated cron jobs, and
when one day moved, 34 of them had to be paused and rebooked one by one. We could not see the
result either. The plan lived in a chat.

Chronomancer gives a card a **slot**: a planned start and an estimate. It parks the card, releases it
at the start, and shows the slots of every profile on one timeline, with the places where they
clash. You plan by dragging. Your orchestrator agent plans through five tools. Both change the
same plan, and each change records who made it.

## Limits

Read these first.

- **It advises. It does not enforce.** A clash is shown and explained, and nothing is refused
  because of it. Anyone who can use the Kanban can still start any card.
- **It is a timer for parked cards, not a scheduler of agents.** At the slot's start the card
  becomes ready. From then on Hermes's own dispatcher decides when a worker takes it.
- **The timer needs a running gateway.** A slot that starts while the gateway is down is released
  late if the gateway is back within the slot's estimate (at least 15 minutes). After that the slot
  is marked missed and you decide.
- **An estimate is a guess.** A card that runs past it is marked as over its estimate. Nothing
  stops it.
- **A pin is a courtesy, not a lock.** The agent tools refuse to move a slot that you pinned. The
  Desktop page trusts the local Desktop session, and code that runs as your OS user can call the
  same local API.
- **One board.** The plugin plans the board in its `board` setting. Several boards at once and
  several gateways are not supported.
- **Reassigning a card or changing its model** stays on the Kanban board. The Kanban tools that a
  plugin may call cannot do either.
- The narrow layout was tested in Node only. Nothing was tested on a phone.

## Install

```bash
hermes plugins install mordor-forge/chronomancer
hermes -p <profile> plugins enable chronomancer
hermes gateway restart
```

Enable it in **one** profile: the one that plans the work of the others (your orchestrator), which
is also the profile your Hermes Desktop connects to. The Kanban board is shared by every profile on
the gateway, so this one instance sees and plans all cards. The other profiles need nothing.

In Hermes Desktop: **Capabilities** -> **Plugins** -> **FILTERS** -> **Installed**, turn on
**Chronomancer**. Turn on **Kanban** too if you want the slot badges on the cards. The page is the
**Chronomancer** row in the sidebar.

Needs Hermes 0.22.7 or newer. Checked on 0.23.0 and 0.23.1.

## How it works

A slot belongs to one card. What the plugin does to the card depends on the card's status:

| Card status | When it gets a future slot | At the slot's start |
|---|---|---|
| `ready`, `todo` | parked (`scheduled`), so it cannot start early | released |
| `blocked`, `scheduled` | left as it is: it already waits | released |
| `running`, `review`, `done`, `triage` | refused, with the reason | |

- **Park** is `kanban_schedule`, with the marker `SCHEDULED_UNTIL=<start>` that the Kanban documents
  for this. **Release** is `kanban_unblock`: the card becomes `ready`, or `todo` while its parent
  cards are open. The plugin then adds one fixed-text comment with the planned start (setting
  `comment_on_release`).
- **Run now** releases the card at once and moves its slot to now.
- **Unplan** removes the slot. The card is not touched: a parked card stays parked until it gets a
  new slot or is run now.
- **The timer** is one thread in each Hermes process that loads the plugin; in practice, the
  gateway. It wakes at the next slot start, and at least once a minute. Each due slot is leased to
  one caller in the plan database, so a card is released once even when several processes look.

**Clashes.** A label can be marked "one card at a time" (a cluster, a database). Two slots that
overlap in time and share such a label **clash**. Two overlapping slots of one profile that share
no such label are only **busy**. A card that runs past its estimate keeps its time until now, so
it can clash with the slot it runs into. Every result that reports a clash also gives the next
start without one.

**Labels** belong to the plugin (the Kanban has none): `repo:web`, `project:shop`, `op:upgrade`.
The orchestrator creates them as it needs them. A card's tenant and project from the board show up
as labels too.

**Who decides.** There is no approval step. You and the orchestrator both change the plan; the
timer changes it at a slot's start. The change log names the actor of each change, the page shows
your own changes in bold, and `chrono_plan` tells the orchestrator what you and the timer changed
since it last looked. You can pin a slot.

## Agent tools

| Tool | What it does |
|---|---|
| `chrono_plan` | The plan for a horizon (`day`, `2d`, `week`): slots, clashes, unplanned cards, labels, and what changed since the caller last looked. Filters: `profile`, `label`. |
| `chrono_slot` | Give a card a slot or move it. `start` is ISO 8601 or a delay (`+90m`, `+2h`, `+1d`). `dry_run` only reports the clashes and the next free start. |
| `chrono_now` | Release a card at once. |
| `chrono_clear` | Remove a card's slot. |
| `chrono_label` | Create or change a label (`one_at_a_time`), put it on cards, take it off, delete it. |

A tool never raises. A refusal is `{"ok": false, "error": "<a sentence>"}`.

## The Desktop page

- **The header**: what runs next, counts (running, clashes, busy, needs a look, planned,
  unplanned), and the horizon switch: Day, 2 days, Week.
- **The timeline**: a lane per profile, a bar per slot. Outlined: planned. Dashed: released.
  Shimmering: the card runs. Faded: done. Red dashed: missed or not released. A clash is hatched on
  both bars and joined by a red dashed line. Drag a bar to move its slot in steps of 15 minutes, or
  focus it and press the left or right arrow key.
- **Unplanned**: the open cards without a slot. Drag one onto the timeline. A long list scrolls in
  place, and a field narrows it by title, card id, status, profile or label.
- **Selected card**: its state in one sentence, its clashes, the next free start, Run now (asks
  first), Move, Pin, Unplan, and its labels.
- **Changes**: the newest moves; label changes on request.
- A status-bar chip, a palette command, and a badge with the slot on the cards of the Kanban board.

Filters: profiles, labels, clashes only. Colour comes from your theme; every state also has a
word, a glyph or a pattern. Motion stops under "reduce motion".

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `board` | `""` | Slug of the board to plan. Empty: the active board. |
| `timezone` | `""` | IANA zone for reading and showing times, for example `Europe/Prague`. Empty: the system zone. |
| `default_estimate_minutes` | `60` | The estimate of a new slot when none is given. |
| `comment_on_release` | `true` | Add one fixed-text comment to a card when the plugin releases it. |

```bash
hermes -p <profile> config set plugins.entries.chronomancer.settings.timezone Europe/Prague
```

## Disclosure

- **Network:** none.
- **Outside its own data:** it reads and writes the Kanban board, only through Hermes's kanban
  tools (`kanban_list`, `kanban_show`, `kanban_schedule`, `kanban_unblock`, `kanban_comment`). The
  agent half calls them with `ctx.dispatch_tool`. The Desktop backend runs in the dashboard
  process, which has no plugin context, so it imports `tools.kanban_tools` (a private core module)
  to register those tools there. `hermes plugins validate` warns about this import.
- **Stored:** `plan.db` in the plugin's data directory: slots, labels, and the change log. The log
  holds card ids, card titles, profile names and label names. Entries older than 30 days are
  removed.
- **Background:** one daemon thread per process that loads the plugin (not in Kanban workers). It
  sleeps until the next slot start.
- **Shell commands, credentials, environment variables:** none.
- **Desktop events:** `plugin.chronomancer.plan.changed`, with an empty payload, when the plan
  changes.

## Development

```bash
cd tests && uv run --no-project --with pytest --with pyyaml --with fastapi --with httpx python -m pytest -q
```

- `tests/` runs without Hermes, on a stand-in for the kanban tools. The Desktop half runs in Node
  against stand-ins for the SDK, on the view that the backend serves for the demo scenario.
- `dev/contract_check.py` makes the same moves on a real board in a scratch Hermes, so the stand-in
  cannot drift from the core.
- `dev/seed_demo.py` fills an empty demo board with an invented team's week.
- `dev/scratch-env.sh` runs a command in a throwaway Hermes environment.

Words are defined in `GLOSSARY.md`. Decisions are in `docs/adr/`.

## License

MIT. Chronomancer builds on the Hermes Kanban, which does the hard part: durable cards, atomic
claims, and the dispatcher.
