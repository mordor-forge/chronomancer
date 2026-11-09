# change-windows

Approved time windows for production changes made by [Hermes Agent](https://github.com/NousResearch/hermes-agent).

> Status: **in development** (0.1.0-dev0). The guard, the four agent tools and the Telegram
> buttons, card release, start/end notices and Mini App are implemented. Local tests include real
> HTTP requests and a phone-sized Chromium flow. The real Telegram phone/HTTPS check was waived
> by the owner, not passed; browser tests use synthetic Telegram signatures. Real phone, HTTPS
> routing and Telegram-signed initData remain unverified.

## Why

An agent that can run `kubectl apply` at 03:00 will, sooner or later, do it at 03:00. A rule in a
prompt or a skill is advice the model can forget. `change-windows` enforces the rule in a
`pre_tool_call` hook: a production write runs only inside a window the owner approved for its
scope. Outside a window the call is blocked, and the model is told which window comes next and
how to ask for one.

## How it works

1. **Rules** say which tool calls are production writes and which **scope** they need
   (`k8s-prod`, `gitops-merge`, `infra`, or your own). Defaults cover `kubectl`/`oc` writes and
   remote execution (`exec`, `cp`, `debug`, `run`), `helm` releases, `helmfile`
   apply/sync/destroy, Argo CD application, ApplicationSet, cluster, repository and project
   changes, `tofu`/`terraform`/`pulumi` apply and destroy, `terraform`/`tofu` state changes
   (`state rm|mv|push`, `taint`, `refresh`, `force-unlock`), every `git push`, and
   `gh`/`tea`/`glab` PR merges (also `gh api` with `PUT` on a `/merge` path). You can
   add rules for MCP tools by name.
2. **Windows** have a scope, a start and an end, and can be exclusive (no overlap in the scope)
   or shared.
3. **Agents propose, owners approve.** `window_propose` stores a pending window and reports
   overlaps. No tool can approve. Approval needs the owner: a Telegram button from an
   allow-listed Telegram user id, or signed Mini App initData from that user. There is no CLI approval.
4. **The guard** checks every tool call against the rules. An unmatched call costs a few regular
   expression checks and never touches the store. A matched call needs an open window for every
   scope it touches; otherwise it is blocked. If the store fails, a matched call is blocked
   (fail closed). Matched calls are audited on a best-effort basis: tool, rule ids, scope, window
   and size; command text only if you opt in.
   If rules cannot be loaded or matched, calls are blocked because their match is unknown;
   the four window tools remain available to inspect, propose or remove permission.
   `mode: "off"` is read before the rules, so it also ends that state.

## Agent tools

| Tool | What it does |
|---|---|
| `window_propose` | Propose a window (pending until the owner approves). Links Kanban cards to release at start. The name is kept on one line. At most 10 proposals can wait for a decision at a time (all scopes); further ones are refused until one is decided, withdrawn or past its end. |
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
  Undeliverable failure reports retry after 10 seconds, doubling up to one hour, and log at
  WARNING on every failed delivery (including at the cap). This report backoff is per process
  and resets on restart; successful delivery finishes the effect.
- **`/windows`.** Today's and tomorrow's windows, with overlaps, in private chats only. In a
  group, approvers receive only an instruction to use a private chat. With a Mini App URL configured,
  proposal messages and `/windows` include **Open list** in private chats. No bot menu, commands
  list or webhook is changed by the plugin.
- **Who can act.** Only the Telegram user ids in `approver_telegram_ids`. Empty means nobody. Other
  users get "Only an approver can do this" on a button and no answer to `/windows`. A stale button
  (an old message) is checked against the current state and redrawn.
- Messages are plain text, so agent-written names and reasons cannot add formatting or links.

## Mini App

Set `miniapp_url` to your HTTPS URL and proxy it to `127.0.0.1:18782` (or your configured loopback
host/port). There is no hosted service: you publish HTTPS yourself. Open **Open list** in a private
chat with the bot. The page uses Telegram's theme, shows today plus seven calendar days, flags
exclusive overlaps in red, and offers Approve / Decline / Close and the last 20 audit rows. Names
and reasons render as text. Command previews and session/task ids are never sent to this page.

Every API request verifies Telegram's Ed25519 signature and the current approver allow-list.
Read sessions last one hour; decisions require initData no older than ten minutes. Reopen the
Mini App when the session expires. Writes are POST-only with a client idempotency key: the decision
and its original result are stored atomically, so a retry after a lost response or restart cannot
apply it twice. Reusing a key for a different action is refused. Original replay results can be
stale; the page refreshes current state after every action. The plugin database stores each key
with the approver Telegram user id, window id, action, creation timestamp and the full original
window snapshot (including name, reason and decision metadata). Keys and snapshots older than
90 days are pruned at gateway startup and daily, alongside audit rows. After pruning, an old key
has no replay guarantee; current window-state checks still apply.
Requests are limited to 120 reads and 30 writes per approver per minute in each gateway process.
There are no cookies, CORS headers or HTTP access logs. Binding outside loopback is refused.

The listener is optional (`miniapp_url` empty disables it), one per profile, started with Telegram.
A reconnect reuses it and rebinds bot identity; plugin unload closes it. If the port is occupied,
check configuration and restart the gateway; the guard and Telegram buttons continue working.

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
| `mode` | `enforce` | `enforce` blocks; `warn` records and allows, except `self-protect` hits, which always block; `off` disables the guard (an unquoted YAML `off` is read as `false` and also counts as off). Any other value enforces. |
| `timezone` | system zone | IANA zone (for example `Europe/Prague`) for showing times and for times given without an offset; daylight-saving changes are applied per date. Empty: the system zone by name (`TZ`, then the `/etc/localtime` link). If no name is found, the plugin logs a warning and uses today's fixed offset, which is wrong across a daylight-saving change. |
| `rules` | `[]` | Extra rules: `{id, scope, tools: [fnmatch], match: regex, field: command\|code\|*}`. |
| `disabled_rules` | `[]` | Default rule ids to turn off. `self-protect` cannot be turned off. |
| `approver_telegram_ids` | `[]` | Telegram user ids that may approve, decline and close windows and read `/windows`. Empty = nobody. |
| `owner_chat_id` | `0` | Chat that gets proposal messages. `0` = the private chat of the first approver. |
| `miniapp_url` | empty | Your HTTPS URL; empty disables the listener and Open list buttons. |
| `miniapp_host` | `127.0.0.1` | Loopback listen address only (also accepts `::1` or `localhost`). |
| `miniapp_port` | `18782` | Listener port; choose a distinct port for each profile. |
| `telegram_bot_id` | `0` | Public bot id; 0 discovers it from the initialized bot. Never enter a token. |
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
- **Only the commands the rules name.** The default rules know the CLIs by name and a list of
  write verbs. They do not match a shell alias (`k apply`), a wrapper (`make deploy`,
  `./deploy.sh`), a verb they do not list (for example `oc new-app`, `pulumi refresh`, `helmfile`
  inside `execute_code`), a merge through another API client, or a command whose verb comes after
  a `|` inside `$(...)`. Add your own rules for the ways your agents really deploy.
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
- **Timed effects use bounded retries.** A message or comment can repeat after a crash between
  the remote action and the local record; delivery is not guaranteed. There are at most 3 attempts,
  with a 2-minute lease. A release re-reads the window and each card to avoid repeating completed
  work where it can observe the result. The scheduler runs in
  the gateway: with the gateway down, cards are released when it comes back (if the window has not
  ended), and the guard keeps working without it.
- **Telegram availability.** Approvals, Mini App and scheduled effects require the gateway
  connected to Telegram and `plugins.isolation: in_process`. With no Telegram or with `host`
  isolation, the guard and tools still work; new windows cannot be approved.
- **Admission only.** Closing a window blocks later calls; it does not stop a command that already
  started.

## Disclosure

- **Window records:** id, name, scope, start/end times, exclusive/presence flags, state, kind,
  reason, linked card ids/boards, creation/decision/check-in/close times and close reason. The
  proposing profile name is stored as `agent:<profile>`; a decision records the approver Telegram
  user id as `telegram:<id>`, and the close reason includes the closing actor. Window records
  have no automatic expiry. Each Mini App idempotency key stores a full decision snapshot plus
  approver id, window id, action and timestamp for 90 days (see Mini App above). The effects
  queue stores window id, effect kind, due/claim/completion times, attempt count and result/error
  text; it has no automatic expiry. Telegram `initData` is verified in memory and never stored.
- **Reads:** the arguments of every tool call, to match rules. For matched calls the plugin's own
  SQLite database (`<HERMES_HOME>/plugin-data/change-windows/`) stores the tool name, rule ids,
  scope, window, session/task ids and the argument size or names, not the values. With
  `audit_command_preview: true` it also stores a masked command preview (commands longer than 2000
  characters are omitted); masking is best effort, so a secret can remain. Turning the setting off
  does not clean old rows: `Store.purge_previews()` can blank stored previews. The gateway scheduler
  prunes audit rows older than 90 days at startup and daily (no pruning while the gateway is off);
  remember that SQLite and backups can keep old pages. Treat the database as sensitive.
- **Network:** messages to the configured owner chat, and answers to button presses and
  `/windows`, through the gateway's own Telegram bot. The Mini App page loads
  Telegram's `telegram-web-app.js` from telegram.org in the Telegram client.
- **Background work:** inside the gateway, when Telegram connects: one supervised task per profile
  (`ctx.spawn_task`) that sends proposal messages and the start and end notices, and releases
  linked cards. When configured, a second supervised task serves the Mini App on loopback
  (default `127.0.0.1`); you publish its HTTPS URL yourself.
- **Kanban:** unblocks and comments on the cards a window links, through the kanban tools
  (`kanban_show`, `kanban_unblock`, `kanban_comment`), when the window opens.
- **Credentials:** the plugin reads none and asks for none. The Mini App verifies Telegram
  `initData` with Telegram's published Ed25519 key and the public bot id, so the plugin never holds
  the bot token. (An opted-in command preview can contain a secret that masking missed.)
- **Plugin isolation:** the Telegram parts need `plugins.isolation: in_process` (the default). Under
  `host` isolation the guard and the tools work; Telegram, the Mini App and timers are off.
- **Shell commands:** none.

## Roadmap

- Later: timeline lanes and emergency windows.
- Later: reminders before a window, windows that open only after the owner checks in (the store
  has the field; no tool or button sets it today), `window_propose` refusing triage cards up front.
- CLI: `hermes change-windows list` (read-only; approvals stay in Telegram).

## Development

```bash
cd tests && uv run --no-project --with 'pytest>=8,<9' --with 'cryptography>=42,<51' --with 'pyyaml>=6,<7' \
  --with 'python-telegram-bot>=22,<23' --with 'aiohttp>=3.14,<4' \
  --with 'playwright>=1.55,<2' python -m pytest -q
cd .. && dev/scratch-env.sh hermes plugins doctor . --ci
dev/scratch-env.sh hermes plugins validate . --install-deps
```

The Telegram tests use real python-telegram-bot objects with an offline fake Bot API (no
network, no token). HTTP tests use generated signing keys; the browser test uses system `chromium`
and a synthetic Telegram SDK. Without PTB or Playwright/Chromium their respective tests are skipped.

`dev/scratch-env.sh` runs a command with a throwaway `HERMES_HOME` and Kanban DB, a clean
environment, `KUBECONFIG=/dev/null`, stub cluster and forge CLIs first in `PATH`, a `git` that
refuses `push`, no global git config, and a 2 GiB memory cap (it refuses to run without one). It
keeps your real `HOME`, because Hermes finds its dependency environment there. Use it for every
integration test, with synthetic fixtures only. For a demo against a throwaway local
[kind](https://kind.sigs.k8s.io/) cluster, set `CW_KIND_KUBECONFIG` to the kubeconfig that
`kind create cluster --kubeconfig <file>` wrote: only `kubectl` then reaches that cluster (loopback
servers and `kind-*` names only; options that pick another cluster or identity are refused).

The demo launcher used for the video is not part of the plugin.

## License

MIT
