"""Telegram layer with real python-telegram-bot objects, offline: a fake Bot API transport answers
every request, so nothing leaves the process. Skipped when PTB is not installed (run the tests with
``--with 'python-telegram-bot>=22,<23'``; see the README)."""
import asyncio
import importlib.util
import json
import logging
import sqlite3
import sys
import threading
import types
from datetime import timezone
from pathlib import Path

import pytest

pytest.importorskip("telegram")

from telegram import Update  # noqa: E402
from telegram.ext import Application, CallbackQueryHandler, CommandHandler  # noqa: E402
from telegram.request import BaseRequest  # noqa: E402

from change_windows import tg  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000
OWNER = 111111111
STRANGER = 222222222
BOT_ID = 999999999


class FakeBotAPI(BaseRequest):
    """Records Bot API calls and answers them like Telegram would. ``fail`` makes a method fail."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail: set[str] = set()
        self._next_id = 100

    @property
    def read_timeout(self):
        return 1.0

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **_):
        name = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        self.calls.append((name, params))
        if name in self.fail:
            return 400, json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: test"}).encode()
        if name == "getMe":
            result = {"id": BOT_ID, "is_bot": True, "first_name": "cw", "username": "cw_test_bot"}
        elif name in ("sendMessage", "editMessageText"):
            self._next_id += 1
            result = {"message_id": self._next_id, "date": NOW, "text": params.get("text", ""),
                      "chat": {"id": int(params.get("chat_id", OWNER)), "type": "private"}}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def named(self, name):
        return [p for n, p in self.calls if n == name]


def build_app(api=None):
    api = api or FakeBotAPI()
    app = (Application.builder().token("123456:TEST-NOT-A-TOKEN").request(api)
           .get_updates_request(FakeBotAPI()).updater(None).build())
    return app, api


def make_actions(store, approvers=(OWNER,), clock=lambda: NOW):
    settings = {"approver_telegram_ids": list(approvers)}
    return tg.Actions(store, setting=lambda k, d=None: settings.get(k, d), tz=lambda: timezone.utc, clock=clock)


def propose(store, name="web v2", start=NOW + 600, end=NOW + 4200):
    w, _ = store.propose(name=name, scope="k8s-prod", start_ts=start, end_ts=end, reason="roll out web v2",
                         proposed_by="agent:test", now=NOW)
    return w


def button_update(app, user_id, data, update_id=1):
    return Update.de_json({"update_id": update_id, "callback_query": {
        "id": f"cq{update_id}", "chat_instance": "ci", "data": data,
        "from": {"id": user_id, "is_bot": False, "first_name": "U"},
        "message": {"message_id": 5, "date": NOW, "text": "old", "chat": {"id": user_id, "type": "private"}}}},
        app.bot)


def command_update(app, user_id, text="/windows", update_id=2):
    return Update.de_json({"update_id": update_id, "message": {
        "message_id": 6, "date": NOW, "text": text, "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "U"},
        "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}}, app.bot)


def keyboard(params):
    markup = params.get("reply_markup")
    markup = json.loads(markup) if isinstance(markup, str) else markup
    return [[b["callback_data"] for b in row] for row in (markup or {}).get("inline_keyboard", [])]


@pytest.fixture(autouse=True)
def clean_registry():
    tg._RUNTIMES.clear()
    yield
    tg._RUNTIMES.clear()


class Spawner:
    """Stands in for ctx.spawn_task: a supervised task on the running loop."""

    def __init__(self):
        self.tasks: list[asyncio.Task] = []

    def __call__(self, coro, name=None):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task

    async def cancel_all(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def ours(app):
    return [h for h in app.handlers.get(0, []) if isinstance(h, (CallbackQueryHandler, CommandHandler))]


# -- gate: two factory calls start one scheduler ------------------------------------------------------

def test_two_factory_calls_start_one_scheduler(store):
    async def main():
        spawn = Spawner()
        factory = tg.make_factory(key=lambda: "/scratch/profile-a", actions=make_actions(store), spawn=spawn)
        app1, _ = build_app()
        app2, _ = build_app()  # the adapter rebuilds its Application after a failed connect attempt
        factory(app1, None)
        factory(app1, None)    # a re-wire on the same Application
        factory(app2, None)
        runtime = tg._RUNTIMES["/scratch/profile-a"]
        result = {"spawned": len(spawn.tasks), "binds": runtime.binds, "app_is_latest": runtime.app is app2,
                  "handlers_app1": len(ours(app1)), "handlers_app2": len(ours(app2)),
                  "task_alive": not runtime.task.done()}
        # a second profile in the same process gets its own runtime and scheduler
        tg.make_factory(key=lambda: "/scratch/profile-b", actions=make_actions(store), spawn=spawn)(app1, None)
        result["spawned_after_second_profile"] = len(spawn.tasks)
        await spawn.cancel_all()
        # after the scheduler dies (plugin unload cancels it), the next factory call starts a new one
        factory(app2, None)
        result["spawned_after_restart"] = len(spawn.tasks)
        await spawn.cancel_all()
        return result

    r = asyncio.run(main())
    assert r == {"spawned": 1, "binds": 3, "app_is_latest": True, "handlers_app1": 2, "handlers_app2": 2,
                 "task_alive": True, "spawned_after_second_profile": 2, "spawned_after_restart": 3}


def test_factory_without_a_running_loop_wires_buttons_but_no_scheduler(store, caplog):
    app, _ = build_app()
    factory = tg.make_factory(key=lambda: "/scratch/p", actions=make_actions(store), spawn=Spawner())
    with caplog.at_level(logging.WARNING):
        factory(app, None)
    assert len(ours(app)) == 2 and tg._RUNTIMES["/scratch/p"].task is None
    assert "scheduler not started" in caplog.text
    factory(None, None)  # platforms without a native client hand None: ignored


# -- callbacks change the store ----------------------------------------------------------------------

def test_buttons_change_the_store(store):
    async def main():
        app, api = build_app()
        runtime = tg.Runtime(make_actions(store))
        runtime.bind(app)
        await app.initialize()
        w = propose(store)
        await app.process_update(button_update(app, STRANGER, f"cw:approve:{w.id}", 1))
        after_stranger = store.get(w.id).state
        await app.process_update(button_update(app, OWNER, f"cw:approve:{w.id}", 2))
        after_owner = store.get(w.id).state
        await app.process_update(button_update(app, OWNER, f"cw:close:{w.id}", 3))
        after_close = store.get(w.id).state
        await app.shutdown()
        return w, api, after_stranger, after_owner, after_close

    w, api, after_stranger, after_owner, after_close = asyncio.run(main())
    assert (after_stranger, after_owner, after_close) == ("pending", "approved", "closed")
    answers = api.named("answerCallbackQuery")
    assert [a.get("show_alert") for a in answers] == [True, False, False]
    assert "Only an approver" in answers[0]["text"] and answers[1]["text"] == "Approved."
    edits = api.named("editMessageText")
    assert len(edits) == 2  # the stranger's press changes nothing, not even the message
    assert keyboard(edits[0]) == [[f"cw:close:{w.id}"]] and "Window approved" in edits[0]["text"]
    assert keyboard(edits[1]) == [] and "Window closed" in edits[1]["text"]
    assert "parse_mode" not in edits[0]  # plain text: agent-written names cannot become markup


def test_decline_and_a_stale_button(store):
    async def main():
        app, api = build_app()
        tg.Runtime(make_actions(store)).bind(app)
        await app.initialize()
        w = propose(store)
        await app.process_update(button_update(app, OWNER, f"cw:decline:{w.id}", 1))
        await app.process_update(button_update(app, OWNER, f"cw:approve:{w.id}", 2))
        await app.shutdown()
        return w, api

    w, api = asyncio.run(main())
    assert store.get(w.id).state == "declined"
    answers = api.named("answerCallbackQuery")
    assert answers[0]["text"] == "Declined." and answers[1]["show_alert"] is True
    assert "not pending" in answers[1]["text"]


def test_a_failed_edit_does_not_undo_the_decision(store):
    async def main():
        app, api = build_app()
        api.fail.add("editMessageText")  # e.g. "message is not modified" or a message too old to edit
        tg.Runtime(make_actions(store)).bind(app)
        await app.initialize()
        w = propose(store)
        await app.process_update(button_update(app, OWNER, f"cw:approve:{w.id}", 1))
        await app.shutdown()
        return w

    assert store.get(asyncio.run(main()).id).state == "approved"


def test_core_buttons_are_not_ours(store):
    handler = CallbackQueryHandler(lambda *_: None, pattern=tg.CALLBACK_PATTERN)
    app, _ = build_app()
    assert handler.check_update(button_update(app, OWNER, "cw:approve:WABCDEF"))
    assert not handler.check_update(button_update(app, OWNER, "approval:once:abc"))


# -- /windows ---------------------------------------------------------------------------------------------

def test_windows_command_answers_approvers_only(store):
    async def main():
        app, api = build_app()
        tg.Runtime(make_actions(store)).bind(app)
        await app.initialize()
        w = propose(store)
        await app.process_update(command_update(app, STRANGER, update_id=1))
        await app.process_update(command_update(app, OWNER, update_id=2))
        await app.shutdown()
        return w, api

    w, api = asyncio.run(main())
    sent = api.named("sendMessage")
    assert len(sent) == 1 and int(sent[0]["chat_id"]) == OWNER
    assert f"{w.id} pending" in sent[0]["text"]


# -- scheduler: proposal messages ------------------------------------------------------------------------

def test_scheduler_announces_a_proposal_once_and_only_when_running(store):
    async def main():
        app, api = build_app()
        runtime = tg.Runtime(make_actions(store), clock=lambda: NOW)
        runtime.bind(app)
        w = propose(store)
        before_start = await runtime.tick()  # the adapter has not started the Application yet
        await app.initialize()
        await app.start()
        first = await runtime.tick()
        second = await runtime.tick()
        await app.stop()
        await app.shutdown()
        return w, api, before_start, first, second

    w, api, before_start, first, second = asyncio.run(main())
    assert (before_start, first, second) == (0, 1, 0)
    sent = api.named("sendMessage")
    assert len(sent) == 1 and int(sent[0]["chat_id"]) == OWNER
    assert sent[0]["text"].startswith("Window proposal: web v2")
    assert keyboard(sent[0]) == [[f"cw:approve:{w.id}", f"cw:decline:{w.id}"]]


def test_scheduler_retries_a_failed_send_after_the_lease(store):
    clock = {"now": NOW}

    async def main():
        app, api = build_app()
        api.fail.add("sendMessage")
        runtime = tg.Runtime(make_actions(store), clock=lambda: clock["now"])
        runtime.bind(app)
        propose(store)
        await app.initialize()
        await app.start()
        results = [await runtime.tick()]
        api.fail.clear()
        results.append(await runtime.tick())                     # lease still held: no resend yet
        clock["now"] += tg.ANNOUNCE_LEASE_S + 1
        results.append(await runtime.tick())                     # lease expired: retried and sent
        await app.stop()
        await app.shutdown()
        return results, api

    results, api = asyncio.run(main())
    assert results == [0, 0, 1]
    assert len(api.named("sendMessage")) == 2


def test_scheduler_skips_a_proposal_that_is_no_longer_pending(store):
    async def main():
        app, api = build_app()
        runtime = tg.Runtime(make_actions(store), clock=lambda: NOW)
        runtime.bind(app)
        w = propose(store)
        store.close(w.id, by="agent:test", reason="not needed", now=NOW)
        await app.initialize()
        await app.start()
        sent = await runtime.tick()
        await app.stop()
        await app.shutdown()
        return sent, api

    sent, api = asyncio.run(main())
    assert sent == 0 and api.named("sendMessage") == []
    assert store.claim_due_effects(NOW + 10_000, kinds=(tg.ANNOUNCE,)) == []  # finished as skipped


def test_scheduler_without_approvers_sends_nothing(store, caplog):
    async def main():
        app, api = build_app()
        runtime = tg.Runtime(make_actions(store, approvers=()), clock=lambda: NOW)
        runtime.bind(app)
        propose(store)
        await app.initialize()
        await app.start()
        with caplog.at_level(logging.WARNING):
            results = [await runtime.tick(), await runtime.tick()]
        await app.stop()
        await app.shutdown()
        return results, api

    results, api = asyncio.run(main())
    assert results == [0, 0] and api.named("sendMessage") == []
    assert caplog.text.count("proposals are not sent") == 1
    assert len(store.claim_due_effects(NOW, kinds=(tg.ANNOUNCE,))) == 1  # still queued for later


def test_a_proposal_from_another_thread_wakes_the_scheduler(store):
    async def main():
        app, api = build_app()
        spawn = Spawner()
        factory = tg.make_factory(key=lambda: "/scratch/p", actions=make_actions(store), spawn=spawn)
        factory(app, None)
        runtime = tg._RUNTIMES["/scratch/p"]
        runtime.interval_s = 3600  # only a wake can make it send within the test
        await app.initialize()
        await app.start()
        await asyncio.sleep(0.05)  # first pass: nothing to send

        def agent_tool():  # the tool handler runs off the loop
            propose(store)
            tg.wake_all()

        threading.Thread(target=agent_tool).start()
        for _ in range(100):
            if api.named("sendMessage"):
                break
            await asyncio.sleep(0.02)
        await spawn.cancel_all()
        await app.stop()
        await app.shutdown()
        return api

    assert len(asyncio.run(main()).named("sendMessage")) == 1


# -- Fri 6: start and end effects through the scheduler ---------------------------------------------

def approved_window(store, cards=(), start=NOW + 600, end=NOW + 4200):
    w, _ = store.propose(name="web v2", scope="k8s-prod", start_ts=start, end_ts=end, reason="roll out web v2",
                         proposed_by="agent:test", now=NOW, cards=[{"task_id": t, "board": ""} for t in cards])
    store.decide(w.id, approve=True, by=f"telegram:{OWNER}", now=NOW)
    return w


def test_scheduler_opens_releases_and_ends_a_window(store):
    from test_effects import FakeKanban
    from change_windows import effects as fx

    clock = {"now": NOW}
    kanban = FakeKanban({"t_aa0001": "blocked"})

    async def main():
        app, api = build_app()
        acts = make_actions(store, clock=lambda: clock["now"])
        runtime = tg.Runtime(acts, effects=fx.Effects(store, acts, dispatch=kanban))
        runtime.bind(app)
        w = approved_window(store, cards=["t_aa0001"])
        await app.initialize()
        await app.start()
        counts = [await runtime.tick()]            # before the start: the proposal was approved directly
        clock["now"] = NOW + 600
        counts.append(await runtime.tick())        # start: release + open notice
        counts.append(await runtime.tick())        # nothing new
        clock["now"] = NOW + 4200
        counts.append(await runtime.tick())        # end notice
        await app.stop()
        await app.shutdown()
        return w, api, counts

    w, api, counts = asyncio.run(main())
    assert counts == [0, 1, 0, 1]
    opened, ended = api.named("sendMessage")
    assert opened["text"].startswith("Window OPEN: web v2") and "Cards eligible: t_aa0001 ready" in opened["text"]
    assert keyboard(opened) == [[f"cw:close:{w.id}"]] and "parse_mode" not in opened
    assert ended["text"].startswith("Window ended: web v2") and keyboard(ended) == []
    assert kanban.board["t_aa0001"]["status"] == "ready" and len(kanban.board["t_aa0001"]["comments"]) == 1


def test_an_effect_that_keeps_failing_is_reported_once(store, caplog):
    from change_windows import effects as fx

    clock = {"now": NOW + 600}

    async def main():
        app, api = build_app()
        acts = make_actions(store, clock=lambda: clock["now"])
        runtime = tg.Runtime(acts, effects=fx.Effects(store, acts, dispatch=None))  # no kanban tools
        runtime.bind(app)
        w = approved_window(store, cards=["t_aa0001"])
        await app.initialize()
        await app.start()
        counts = []
        with caplog.at_level(logging.WARNING):
            for _ in range(fx.MAX_ATTEMPTS + 2):
                counts.append(await runtime.tick())
                clock["now"] += fx.LEASE_S
        await app.stop()
        await app.shutdown()
        return w, api, counts

    w, api, counts = asyncio.run(main())
    assert counts == [0, 0, 0, 1, 0]
    [report] = api.named("sendMessage")
    assert report["text"].startswith(f"change-windows could not open (release the cards of) {w.id} after 3 attempts")
    assert "kanban tools are not available" in report["text"]
    assert caplog.text.count("start effect for") == 3
    assert store.effect_result(w.id, "start").startswith("failed after 3 attempts; reported")


def test_a_late_approval_opens_the_window_at_once(store):
    async def main():
        app, api = build_app()
        spawn = Spawner()
        factory = tg.make_factory(key=lambda: "/scratch/late", actions=make_actions(store), spawn=spawn)
        factory(app, None)
        tg._RUNTIMES["/scratch/late"].interval_s = 3600  # only the button's wake can open it in time
        w = propose(store, start=NOW - 60, end=NOW + 3600)  # its start has passed; still pending
        await app.initialize()
        await app.start()
        await asyncio.sleep(0.05)
        await app.process_update(button_update(app, OWNER, f"cw:approve:{w.id}"))
        for _ in range(100):
            if any(p["text"].startswith("Window OPEN") for p in api.named("sendMessage")):
                break
            await asyncio.sleep(0.02)
        await spawn.cancel_all()
        await app.stop()
        await app.shutdown()
        return api

    texts = [p["text"].splitlines()[0] for p in asyncio.run(main()).named("sendMessage")]
    assert texts == ["Window OPEN: web v2"]  # approved before its announce ran: the announce is skipped


# -- register(): isolation fallback, and the real factory from the plugin entry point -------------------

def load_plugin(tmp_path, monkeypatch):
    """Import the plugin package the way Hermes does, with a stand-in ``plugins.plugin_storage``."""
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = lambda name: tmp_path / "plugin-data" / name
    storage.plugin_db = lambda name: sqlite3.connect(tmp_path / "data.db", timeout=5)
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    name = "cw_plugin_under_test"
    for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        monkeypatch.delitem(sys.modules, mod)
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


class FakeCtx:
    profile_name = "test"

    def __init__(self, *, host_isolation=False):
        self.host_isolation = host_isolation
        self.tools, self.hooks, self.factories, self.spawned, self.dispatched = [], [], [], [], []
        self.config = {"approver_telegram_ids": [OWNER]}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, name, **_):
        self.tools.append(name)

    def register_hook(self, name, _callback):
        self.hooks.append(name)

    def register_platform_handler(self, platform, factory):
        if self.host_isolation:  # what the plugin host child raises (PluginHostUnsupported is a RuntimeError)
            raise RuntimeError("ctx.register_platform_handler() cannot run in the plugin host")
        self.factories.append((platform, factory))

    def spawn_task(self, coro, *, name=None):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.spawned.append(task)
        return task

    def dispatch_tool(self, tool_name, args, **_):
        self.dispatched.append((tool_name, args))
        return json.dumps({"error": f"task {args.get('task_id')} not found"})


def test_host_isolation_keeps_guard_and_tools(tmp_path, monkeypatch, caplog):
    plugin = load_plugin(tmp_path, monkeypatch)
    ctx = FakeCtx(host_isolation=True)
    with caplog.at_level(logging.WARNING):
        plugin.register(ctx)
    assert sorted(ctx.tools) == ["window_close", "window_list", "window_next", "window_propose"]
    assert ctx.hooks == ["pre_tool_call"] and ctx.factories == []
    assert "Telegram approvals and the scheduler are off" in caplog.text
    assert "plugins.isolation: in_process" in caplog.text


def test_registered_factory_starts_one_scheduler(tmp_path, monkeypatch):
    plugin = load_plugin(tmp_path, monkeypatch)
    ctx = FakeCtx()
    plugin.register(ctx)
    assert [p for p, _ in ctx.factories] == ["telegram"]
    factory = ctx.factories[0][1]

    async def main():
        app1, _ = build_app()
        app2, _ = build_app()
        factory(app1, None)
        factory(app2, None)
        spawned = len(ctx.spawned)
        names = [t.get_name() for t in ctx.spawned]
        for task in ctx.spawned:
            task.cancel()
        await asyncio.gather(*ctx.spawned, return_exceptions=True)
        return spawned, names, len(ours(app1)), len(ours(app2))

    assert asyncio.run(main()) == (1, ["change-windows:scheduler"], 2, 2)


def test_registered_factory_releases_cards_through_dispatch_tool(tmp_path, monkeypatch):
    plugin = load_plugin(tmp_path, monkeypatch)
    ctx = FakeCtx()
    plugin.register(ctx)

    async def main():
        app, _ = build_app()
        ctx.factories[0][1](app, None)
        [runtime] = sys.modules["cw_plugin_under_test.change_windows.tg"]._RUNTIMES.values()
        for task in ctx.spawned:
            task.cancel()
        await asyncio.gather(*ctx.spawned, return_exceptions=True)
        return runtime

    runtime = asyncio.run(main())
    store = runtime.actions.store
    w, _ = store.propose(name="n", scope="k8s-prod", start_ts=NOW, end_ts=NOW + 60, reason="r",
                         proposed_by="agent:test", now=NOW, cards=[{"task_id": "t_aa0001", "board": "ops"}])
    store.decide(w.id, approve=True, by=f"telegram:{OWNER}", now=NOW)
    outcome = runtime.effects.run(w.id, "start", NOW)
    assert ctx.dispatched == [("kanban_show", {"task_id": "t_aa0001", "board": "ops"})]
    assert "Not released: t_aa0001 (not found)" in outcome.text
