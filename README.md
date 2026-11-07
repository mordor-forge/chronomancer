# change-windows

Approved time windows for production changes made by [Hermes Agent](https://github.com/NousResearch/hermes-agent).

> Status: **in development** (0.1.0-dev0). The guard, the four agent tools and the Telegram
> buttons (approve, decline, close, `/windows`), card release at window start and the start and
> end notices work and are tested. The Mini App is planned (see "Roadmap").

## Why

An agent that can run `kubectl apply` at 03:00 will, sooner or later, do it at 03:00. A rule in a
prompt or a skill is advice the model can forget. `change-windows` enforces the rule in a
`pre_tool_call` hook: a production write runs only inside a window the owner approved for its
scope. Outside a window the call is blocked, and the model is told which window comes next and
how to ask for one.

## How it works

1. **Rules** say which tool calls are production writes and which **scope** they need
   (`k8s-prod`, `gitops-merge`, `infra`, or your own). Defaults cover `kubectl`/`oc` writes and
   remote execution (`exec`, `cp`, `debug`, `run`), `helm` releases, `argocd app sync`,
   `tofu`/`terraform`/`pulumi` apply, every `git push`, and `gh`/`tea`/`glab` PR merges. You can
   add rules for MCP tools by name.
2. **Windows** have a scope, a start and an end, and can be exclusive (no overlap in the scope)
   or shared.
3. **Agents propose, owners approve.** `window_propose` stores a pending window and reports
   overlaps. No tool can approve. Approval needs the owner: a Telegram button from an
   allow-listed Telegram user id. There is no CLI approval.
4. **The guard** checks every tool call against the rules. An unmatched call costs one regular
   expression pass and never touches the store. A matched call needs an open window for every
   scope it touches; otherwise it is blocked. If the store fails, a matched call is blocked
   (fail closed). Matched calls are audited on a best-effort basis: tool, rule ids, scope, window
   and size; command text only if you opt in.

## Agent tools

| Tool | What it does |
|---|---|
| `window_propose` | Propose a window (pending until the owner approves). Links Kanban cards to release at start. |
| `window_list` | Windows in the next N days. |
| `window_next` | Is a window open now for a scope? If not, the next one and pending proposals. |
| `window_close` | Close an open window early, or withdraw a pending proposal. |

## Telegram

When the gateway connects to Telegram, the plugin adds its own buttons and one command to the
gateway's bot. It needs no bot token of its own.

- **Proposal message.** Each new proposal goes to the owner chat: name, scope, time, reason,
  linked cards, overlaps, and the buttons **Approve** and **Decline**. Proposals from any process
  of the profile (gateway chat, CLI chat, Kanban workers) are queued in the plugin database; the
  gateway sends them within about 10 seconds (at once when the proposal comes from the gateway).
- **Close now.** An approved window's message has a **Close now** button. Matched calls are
  blocked again from the next call on.
- **Window opens.** At the start of an approved window the plugin releases its linked Kanban
  cards and sends "Window OPEN" with the cards that are now eligible, the ones it could not release
  (unknown, triage, done) and a **Close now** button. Approving after the start opens it at once.
- **Window ends.** At the end: "Window ended", unless it was closed early. A start that the gateway
  missed completely (it was down for the whole window) is reported as missed; its cards stay as they
  were. An effect that fails 3 times (2 minutes apart) is reported to the owner chat.
- **`/windows`.** Today's and tomorrow's windows, with overlaps.
- **Who can act.** Only the Telegram user ids in `approver_telegram_ids`. Empty means nobody. Other
  users get "Only an approver can do this" on a button and no answer to `/windows`. A stale button
  (an old message) is checked against the current state and redrawn.
- Messages are plain text, so agent-written names and reasons cannot add formatting or links.

## Kanban cards

`window_propose` takes Kanban task ids (`cards`, optionally `board`). When the window opens, the
gateway's scheduler goes through the kanban tools (`ctx.dispatch_tool`): `kanban_show` re-reads the
card, `kanban_unblock` releases a `blocked` or `scheduled` card (it lands in `ready`, or `todo`
while parent cards are open), and `kanban_comment` adds one comment with the window id, scope and
end time. Cards that are already `ready`, `todo`, `review` or `running` get the comment only. The
comment never contains the window's name or reason: comments reach future workers' prompts.
The plugin promises that the card becomes eligible; the worker starts when the dispatcher has
capacity. A `triage` card cannot be released through any public surface, so it is reported.

## Settings

`plugins.entries.change-windows.settings` in `config.yaml`:

| Key | Default | Meaning |
|---|---|---|
| `mode` | `enforce` | `enforce` blocks; `warn` records and allows, except `self-protect` hits, which always block; `off` disables the guard. |
| `timezone` | system zone | IANA zone for times without an offset. |
| `rules` | `[]` | Extra rules: `{id, scope, tools: [fnmatch], match: regex, field: command\|code\|*}`. |
| `disabled_rules` | `[]` | Default rule ids to turn off. `self-protect` cannot be turned off. |
| `approver_telegram_ids` | `[]` | Telegram user ids that may approve, decline and close windows and read `/windows`. Empty = nobody. |
| `owner_chat_id` | `0` | Chat that gets proposal messages. `0` = the private chat of the first approver. |
| `max_window_hours` | `8` | Longest window an agent may propose. |
| `audit_command_preview` | `false` | Store a masked preview of matched `terminal` commands. Masking is best effort. |

Example rule for an MCP server (match write tools by name; no `match` = any call of those tools):

```yaml
rules:
  - id: mcp-k8s-write
    scope: k8s-prod
    tools: ["mcp__kubernetes__*delete*", "mcp__kubernetes__*create*",
            "mcp__kubernetes__*apply*", "mcp__kubernetes__*patch*", "mcp__kubernetes__*scale*"]
```

Rule fields: `match` (regex that must hit), `unless` (regex that cancels a hit), `field`
(`command`, `code` or `*` for all arguments). Quotes and backslashes are removed before matching,
so `"kubectl" 'apply'` counts as `kubectl apply`. A command segment ends at `|`, `;`, `&` or a new
line.

## Limits (read before you rely on it)

- **A seatbelt, not a sandbox.** Text rules catch honest commands. A caller that hides a command
  (variables, encodings, a script file) can get past a regular expression. Keep real
  credentials scoped: use read-only kubeconfigs outside windows if you can.
- **Opaque code tools.** For `execute_code` the default rules match the binary name anywhere in
  the code (`kubectl`, `helm`, `tofu`, ...), not a verb, and `git` with `push` (or `gh` with
  `merge`) anywhere. Code that names them is treated as a write.
- **What the hook sees.** Every tool call that goes through Hermes tool dispatch: the agent loop,
  `execute_code` sandbox calls, MCP tools, `tool_call` bridge calls, delegated subagents, Kanban
  workers and cron runs of a profile that has the plugin enabled. Not covered: `no_agent` cron
  scripts, shell hooks, safe-mode troubleshooting workers, `ctx.call_mcp` calls made by other
  plugins, external coding harnesses that run their own tools, and anything a person types in a
  terminal. If Hermes's own hook plumbing fails (not this plugin's callback), Hermes lets the call
  through; another plugin's `modify` directive can change arguments after this guard looked.
- **Per profile.** Windows live in the profile's plugin data. Enable the plugin in every profile
  that can reach production.
- **Not an identity boundary.** Code that runs as your OS user can edit Hermes's config or the
  plugin's database. The `self-protect` rule blocks the obvious agent attempts (the plugin's data
  path, `hermes change-windows <subcommand>` except `list`/`show`/`status`/`help`,
  `hermes plugins disable|remove|... change-windows`, edits of a config under `.hermes/` that name
  `change-windows`), also in warn mode, but it is a regex too. A config edit that sets `mode: off`
  without naming the plugin is not caught. Approval is only through Telegram from an allow-listed
  user id, never through a CLI an agent could drive.
- **Conservative on purpose.** Every `git push` needs a window (refspecs, upstreams and wildcards
  are not parsed); disable `git-push` and add a narrower rule if your agent pushes feature branches.
  `kubectl exec` counts as a write. A resource named `delete` in `kubectl get configmap delete` is
  blocked. Turn rules off with `disabled_rules`.
- **Timed effects are at least once.** A message can repeat after a crash between the send and
  the record; a failed effect is retried after 2 minutes, at most 3 times. A release re-reads the
  window and each card first, so a retry does not unblock or comment twice. The scheduler runs in
  the gateway: with the gateway down, cards are released when it comes back (if the window has not
  ended), and the guard keeps working without it.
- **Admission only.** Closing a window blocks later calls; it does not stop a command that already
  started.

## Disclosure

- **Reads:** the arguments of every tool call, to match rules. For matched calls the plugin's own
  SQLite database (`<HERMES_HOME>/plugin-data/change-windows/`) stores the tool name, rule ids,
  scope, window, session/task ids and the argument size or names, not the values. With
  `audit_command_preview: true` it also stores a masked command preview (commands longer than 2000
  characters are omitted); masking is best effort, so a secret can remain. Turning the setting off
  does not clean old rows: purge them (planned owner action; `Store.purge_previews()` today) and
  remember that SQLite and backups can keep old pages. Treat the database as sensitive.
- **Network:** messages to the configured owner chat, and answers to button presses and
  `/windows`, through the gateway's own Telegram bot. Planned: a Mini App page that loads
  Telegram's `telegram-web-app.js` in the Telegram client.
- **Background work:** inside the gateway, when Telegram connects: one supervised task per profile
  (`ctx.spawn_task`) that sends proposal messages and the start and end notices, and releases
  linked cards. Planned: one HTTP listener on `127.0.0.1` for the Mini App, which you publish
  yourself.
- **Kanban:** unblocks and comments on the cards a window links, through the kanban tools
  (`kanban_show`, `kanban_unblock`, `kanban_comment`), when the window opens.
- **Credentials:** the plugin reads none and asks for none. The planned Mini App verifies Telegram
  `initData` with Telegram's published Ed25519 key and the public bot id, so the plugin never holds
  the bot token. (An opted-in command preview can contain a secret that masking missed.)
- **Plugin isolation:** the Telegram parts need `plugins.isolation: in_process` (the default). Under
  `host` isolation the guard and the tools work; Telegram, the Mini App and timers are off.
- **Shell commands:** none.

## Roadmap

- Mini App: a list of windows with overlap badges, approve / decline / close, recent audit rows.
  Later: timeline lanes, emergency window.
- Later: reminders before a window, check-in for `presence_required` windows (today such a window
  does not open, and its cards are not released), `window_propose` refusing triage cards up front.
- CLI: `hermes change-windows list` (read-only; approvals stay in Telegram).

## Development

```bash
cd tests && uv run --no-project --with 'pytest>=8,<9' --with 'cryptography>=42,<51' --with 'pyyaml>=6,<7' \
  --with 'python-telegram-bot>=22,<23' python -m pytest -q
cd .. && dev/scratch-env.sh hermes plugins doctor . --ci
dev/scratch-env.sh hermes plugins validate . --install-deps
```

The Telegram tests use real python-telegram-bot objects with an offline fake Bot API (no
network, no token); without `python-telegram-bot` they are skipped.

`dev/scratch-env.sh` runs a command with a throwaway `HERMES_HOME` and Kanban DB, a clean
environment, `KUBECONFIG=/dev/null`, stub cluster and forge CLIs first in `PATH`, a `git` that
refuses `push`, no global git config, and a 2 GiB memory cap (it refuses to run without one). It
keeps your real `HOME`, because Hermes finds its dependency environment there. Use it for every
integration test, with synthetic fixtures only.

## License

MIT
