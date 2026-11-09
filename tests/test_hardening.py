"""Competing HTTP/Telegram decisions and gateway recovery using isolated local state."""
import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from aiohttp.test_utils import TestClient, TestServer

from change_windows import tg
from change_windows.store import Store
from test_initdata import NOW
from test_miniapp import Harness
from test_tg_ptb import build_app, make_actions, propose, OWNER, button_update, Spawner


def race(*calls):
    barrier = threading.Barrier(len(calls))
    def run(call):
        barrier.wait(timeout=5)
        return call()
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run,calls))


def test_two_approvers_compete_over_http(hardening_store):
    h = Harness(hardening_store)
    h.settings['approver_telegram_ids'] = [42,43]
    a,b = h.propose(),h.propose()
    async def main():
        async with TestClient(TestServer(h.app())) as client:
            responses = await asyncio.gather(*[
                client.post(f'/api/windows/{w.id}/approve', headers=h.headers(user=u))
                for w,u in ((a,42),(b,43))])
            assert sorted(r.status for r in responses) == [200,409]
    asyncio.run(main())
    assert len(hardening_store.open_windows('k8s-prod',NOW)) == 1


@pytest.fixture
def hardening_store(store):
    return store


@pytest.mark.parametrize('transport',['callback','http'])
def test_approve_racing_close_never_leaves_permission(store, db_path, transport):
    h = Harness(store)
    h.settings['approver_telegram_ids'] = [42,43]
    w = h.propose()
    other = Store(lambda: sqlite3.connect(db_path,timeout=5))
    actions = tg.Actions(other, setting=h.actions.setting,tz=h.actions.tz,clock=h.actions.clock)
    close = lambda: actions.on_button(43,f'cw:close:{w.id}')
    if transport == 'callback':
        approve = lambda: h.actions.on_button(42,f'cw:approve:{w.id}')
        replies = race(approve,close)
        assert not replies[1].alert
    else:
        async def main():
            async with TestClient(TestServer(h.app())) as client:
                response, reply = await asyncio.gather(
                    client.post(f'/api/windows/{w.id}/approve',headers=h.headers()), asyncio.to_thread(close))
                assert response.status in (200,409) and not reply.alert
        asyncio.run(main())
    assert store.get(w.id).state in ('closed','cancelled')
    assert not store.open_windows('k8s-prod',NOW)


def test_double_callbacks_commit_one_decision_and_one_effect_pair(store):
    async def main():
        app,api = build_app()
        acts = make_actions(store)
        runtime = tg.Runtime(acts); runtime.bind(app)
        w = propose(store)
        await app.initialize()
        await asyncio.gather(*[runtime._on_button(button_update(app,OWNER,f'cw:approve:{w.id}',i),None)
                               for i in (1,2)])
        await app.shutdown()
        assert sorted(bool(p.get('show_alert')) for p in api.named('answerCallbackQuery')) == [False,True]
        assert store.get(w.id).state == 'approved'
        assert [k for _,k,_ in store.claim_due_effects(NOW+5000,kinds=('start','end'))] == ['start','end']
    asyncio.run(main())


def test_concurrent_identical_http_retries_have_one_durable_record(store):
    h = Harness(store); w = h.propose()
    async def main():
        async with TestClient(TestServer(h.app())) as client:
            responses = await asyncio.gather(*[client.post(f'/api/windows/{w.id}/approve',headers=h.headers())
                                               for _ in range(8)])
            data = [await r.json() for r in responses]
            assert all(r.status == 200 for r in responses) and all(d == data[0] for d in data)
        conn = store._conn()
        try:
            assert conn.execute('SELECT count(*) FROM api_actions').fetchone()[0] == 1
        finally: conn.close()
    asyncio.run(main())


def test_reconnect_waits_for_new_bot_and_retries_lost_send(store):
    clock = {'now':NOW}
    acts = make_actions(store,clock=lambda:clock['now'])
    async def main():
        app1,api1 = build_app(); app2,api2 = build_app()
        api1.fail.add('sendMessage')
        runtime = tg.Runtime(acts); runtime.bind(app1)
        w = propose(store)
        await app1.initialize(); await app1.start()
        assert await runtime.tick() == 0
        await app1.stop(); await app1.shutdown()
        runtime.bind(app2)
        clock['now'] += tg.ANNOUNCE_LEASE_S
        assert await runtime.tick() == 0 and not api2.calls
        await app2.initialize(); await app2.start()
        assert await runtime.tick() == 1
        assert await runtime.tick() == 0
        assert len(api1.named('sendMessage')) == len(api2.named('sendMessage')) == 1
        assert store.effect_result(w.id,'announce') == 'sent'
        await app2.stop(); await app2.shutdown()
    asyncio.run(main())


def test_restarted_scheduler_recovers_crashed_claim_on_first_pass(store,db_path):
    w = propose(store)
    assert len(store.claim_due_effects(NOW,kinds=('announce',),lease_s=tg.ANNOUNCE_LEASE_S)) == 1
    restarted = Store(lambda:sqlite3.connect(db_path,timeout=5))
    async def main():
        app,api = build_app(); spawn = Spawner()
        acts = make_actions(restarted,clock=lambda:NOW+tg.ANNOUNCE_LEASE_S)
        runtime = tg.Runtime(acts,interval_s=3600); runtime.bind(app)
        await app.initialize(); await app.start()
        runtime.ensure_scheduler(spawn)
        for _ in range(100):
            if restarted.effect_result(w.id,'announce') == 'sent': break
            await asyncio.sleep(.01)
        assert len(api.named('sendMessage')) == 1
        assert restarted.effect_result(w.id,'announce') == 'sent'
        await spawn.cancel_all(); await app.stop(); await app.shutdown()
    asyncio.run(main())


def test_api_action_rolls_back_state_effects_and_key_together(store,db_path):
    h = Harness(store); w = h.propose()
    class BrokenStore(Store):
        def _decide(self,*args,**kwargs):
            super()._decide(*args,**kwargs)
            raise RuntimeError('simulated crash before recording key')
    broken = BrokenStore(lambda:sqlite3.connect(db_path))
    with pytest.raises(RuntimeError):
        broken.api_action(42,'rollback-key-0000001',w.id,'approve',NOW)
    assert store.get(w.id).state == 'pending'
    assert store.claim_due_effects(NOW+9999,kinds=('start','end')) == []
    assert store.api_action(42,'rollback-key-0000001',w.id,'approve',NOW)['state'] == 'approved'


def test_audit_retention_runs_without_telegram_connection(store):
    keep = NOW-90*86400
    for ts in (keep-1,keep,NOW):
        store.record_audit(now=ts,verdict='blocked',tool='terminal',rule_ids=[],scopes=[],preview='')
    runtime = tg.Runtime(make_actions(store))
    asyncio.run(runtime.tick())
    assert [r['ts'] for r in store.audit_tail()] == [NOW,keep]


def test_api_action_retention_at_startup_and_daily_without_telegram(store, db_path):
    keep = NOW - 90 * 86400
    h = Harness(store)
    for i, ts in enumerate((keep - 1, keep, NOW)):
        w = h.propose()
        store.api_action(42, f'retention-key-{i:016d}', w.id, 'decline', ts)
    clock = {'now': NOW}
    runtime = tg.Runtime(make_actions(store, clock=lambda: clock['now']))

    def retained():
        with sqlite3.connect(db_path) as conn:
            return [row[0] for row in conn.execute('SELECT created_at FROM api_actions ORDER BY created_at')]

    assert retained() == [keep - 1, keep, NOW]
    asyncio.run(runtime.tick())
    assert retained() == [keep, NOW]
    clock['now'] += 86400 - 1
    asyncio.run(runtime.tick())
    assert retained() == [keep, NOW]
    clock['now'] += 1
    asyncio.run(runtime.tick())
    assert retained() == [NOW]


def test_v2_database_adds_durable_actions_without_losing_windows(store,db_path):
    h = Harness(store); w = h.propose()
    with sqlite3.connect(db_path) as conn:
        conn.execute('DROP TABLE api_actions')
        conn.execute("UPDATE meta SET v='2' WHERE k='schema_version'")
    upgraded = Store(lambda:sqlite3.connect(db_path))
    assert upgraded.get(w.id).state == 'pending'
    assert upgraded.api_action(42,'upgrade-key-0000001',w.id,'approve',NOW)['state'] == 'approved'
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone() == ('3',)
