# change-windows

Approved time windows for production changes made by [Hermes Agent](https://github.com/NousResearch/hermes-agent).

> Status: **skeleton** (0.1.0-dev0). The guard and the four agent tools work and are tested.
> The Telegram approval flow, the Mini App and the scheduler are planned (see "Roadmap").

## Why

An agent that can run `kubectl apply` at 03:00 will, sooner or later, do it at 03:00. A rule in a
prompt or a skill is advice the model can forget. `change-windows` enforces the rule in a
`pre_tool_call` hook: a production write runs only inside a window the owner approved for its
scope. Outside a window the call is blocked, and the model is told which window comes next and
how to ask for one.

## How it works

1. **Rules** say which tool calls are production writes and which **scope** they need
   (`k8s-prod`, `gitops-merge`, `infra`, or your own). Defaults cover `kubectl`/`oc` writes,
   `helm` releases, `argocd app sync`, `tofu`/`terraform`/`pulumi` apply, protected or forced
   `git push`, and `gh`/`tea`/`glab` PR merges. You can add rules for MCP tools by name.
2. **Windows** have a scope, a start and an end, and can be exclusive (no overlap in the scope)
   or shared. A window can require the owner to check in before it opens.
3. **Agents propose, owners approve.** `window_propose` stores a pending window and reports
   overlaps. No tool can approve. Approval needs the owner (planned: Telegram button or Mini App,
   or the CLI on a real terminal).
4. **The guard** checks every tool call against the rules. An unmatched call costs one regular
   expression pass and never touches the store. A matched call needs an open window for every
   scope it touches; otherwise it is blocked. If the store fails, a matched call is blocked
   (fail closed). Blocked and allowed production calls go to an audit log, with obvious
   secrets masked.

## Agent tools

| Tool | What it does |
|---|---|
| `window_propose` | Propose a window (pending until the owner approves). Links Kanban cards to release at start. |
| `window_list` | Windows in the next N days. |
| `window_next` | Is a window open now for a scope? If not, the next one and pending proposals. |
| `window_close` | Close an open window early, or withdraw a pending proposal. |

## Settings

`plugins.entries.change-windows.settings` in `config.yaml`:

| Key | Default | Meaning |
|---|---|---|
| `mode` | `warn` (skeleton) | `enforce` blocks, `warn` only records, `off` disables the guard. |
| `timezone` | system zone | IANA zone for times without an offset. |
| `rules` | `[]` | Extra rules: `{id, scope, tools: [fnmatch], match: regex, field: command\|code\|*}`. |
| `disabled_rules` | `[]` | Default rule ids to turn off. `self-protect` cannot be turned off. |
| `max_window_hours` | `8` | Longest window an agent may propose. |

Example rule for an MCP server:

```yaml
rules:
  - id: mcp-k8s-write
    scope: k8s-prod
    tools: ["mcp__kubernetes__*"]
    match: "(create|delete|patch|apply|scale)"
    field: "*"
```

## Limits (read before you rely on it)

- **A seatbelt, not a sandbox.** Text rules catch honest commands. A caller that hides a command
  (variables, encodings, a script file) can get past a regular expression. Keep real
  credentials scoped: use read-only kubeconfigs outside windows if you can.
- **Opaque code tools.** For `execute_code` the default rules match the binary name anywhere in
  the code (`kubectl`, `helm`, `tofu`, ...), not a verb. Code that names them is treated as a write.
- **What the hook sees.** Every tool call that goes through Hermes tool dispatch: the agent loop,
  `execute_code` sandbox calls, MCP tools, `tool_call` bridge calls, delegated subagents, Kanban
  workers and cron runs of a profile that has the plugin enabled. Not covered: `no_agent` cron
  scripts, shell hooks, external coding harnesses that run their own tools, and anything a person
  types in a terminal.
- **Per profile.** Windows live in the profile's plugin data. Enable the plugin in every profile
  that can reach production.

## Disclosure

- **Reads:** the arguments of every tool call, to match rules. A masked preview (400 characters)
  of matched calls is stored in the plugin's own SQLite database under
  `<HERMES_HOME>/plugin-data/change-windows/`.
- **Network:** none in the skeleton. Planned: messages to the owner through the gateway's own
  Telegram bot, and a Mini App page that loads Telegram's `telegram-web-app.js` in the Telegram
  client.
- **Background work:** none in the skeleton. Planned: one supervised task in the gateway (timers)
  and one HTTP listener on `127.0.0.1` for the Mini App, which you publish yourself.
- **Credentials:** none. The planned Mini App verifies Telegram `initData` with Telegram's
  published Ed25519 key and the public bot id, so the plugin never holds the bot token.
- **Shell commands:** none.

## Roadmap

- Telegram: approve/decline buttons on proposals, `/windows`, reminders, check-in.
- Mini App: day/week timeline with conflicts, approve/decline, emergency window, close now, audit log.
- Scheduler: release linked Kanban cards at start, comment on them, miss report when a window passes unused.
- CLI: `hermes change-windows list|approve` (approve only on a real TTY).

## Development

```bash
cd tests && uv run --no-project --with 'pytest>=8,<9' --with 'cryptography>=42,<51' --with 'pyyaml>=6,<7' python -m pytest -q
hermes plugins doctor . --ci
hermes plugins validate . --install-deps
```

## License

MIT
