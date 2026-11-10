"""Real HTTP endpoint tests with generated Ed25519 signatures and a disposable SQLite file."""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from functools import partial

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from change_windows import miniapp, tg
from change_windows.initdata import verify_ed25519
from change_windows.store import Store
from test_initdata import signed, BOT_ID, NOW

OWNER = 42
KEY = 'request-key-00000001'


class Harness:
    def __init__(self, store):
        self.store = store
        self.key = Ed25519PrivateKey.generate()
        self.settings = {'approver_telegram_ids': [OWNER]}
        self.actions = tg.Actions(store, setting=lambda k, d=None: self.settings.get(k, d),
                                  tz=lambda: timezone.utc, clock=lambda: NOW)
        pub = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        self.verifier = partial(verify_ed25519, public_key_hex=pub)
        self.wakes = 0

    def wake(self):
        self.wakes += 1

    def app(self):
        return miniapp.create_app(self.actions, lambda: BOT_ID, verify=self.verifier, wake=self.wake)

    def headers(self, *, age=0, user=OWNER, key=KEY):
        fields = {'auth_date': str(NOW - age), 'user': json.dumps({'id': user, 'first_name': 'Test'})}
        return {'X-Telegram-Init-Data': signed(fields, self.key), 'Idempotency-Key': key}

    def propose(self, **kwargs):
        values = dict(name='web v2', scope='k8s-prod', start_ts=NOW, end_ts=NOW + 3600,
                      reason='<script>not markup</script>', proposed_by='agent:test', now=NOW)
        values.update(kwargs)
        return self.store.propose(**values)[0]


@pytest.fixture
def h(store):
    return Harness(store)


def test_http_decisions_and_persistent_replay(h, db_path):
    async def main():
        w = h.propose()
        path = f'/api/windows/{w.id}/approve'
        async with TestClient(TestServer(h.app())) as client:
            first = await client.post(path, headers=h.headers())
            assert first.status == 200
            result = await first.json()
            assert result['window']['state'] == 'approved'
            replay = await client.post(path, headers=h.headers())
            assert await replay.json() == result
            conflict = await client.post(f'/api/windows/{w.id}/close', headers=h.headers())
            assert conflict.status == 409
            closed = await client.post(f'/api/windows/{w.id}/close', headers=h.headers(key=KEY+'2'))
            assert (await closed.json())['window']['state'] == 'closed'
        h.actions.store = Store(lambda: sqlite3.connect(db_path))
        async with TestClient(TestServer(h.app())) as client:
            replay = await client.post(path, headers=h.headers())
            assert await replay.json() == result  # lost response + restart returns original result
            detail = await client.get(f'/api/windows/{w.id}', headers=h.headers())
            assert (await detail.json())['window']['state'] == 'closed'
            h.settings['approver_telegram_ids'] = []
            assert (await client.post(path, headers=h.headers())).status == 403
        assert not h.store.open_windows('k8s-prod', NOW)
    asyncio.run(main())


@pytest.mark.parametrize('case,status', [('missing',401), ('tampered',401), ('old',401),
    ('stranger',403), ('empty_allowlist',403), ('boolean_user',403), ('bad_key',400), ('no_key',400)])
def test_write_authentication_and_policy(h, case, status):
    async def main():
        w = h.propose()
        headers = h.headers()
        if case == 'missing': headers = {}
        if case == 'tampered': headers['X-Telegram-Init-Data'] = headers['X-Telegram-Init-Data'].replace('Test','Evil')
        if case == 'old': headers = h.headers(age=601)
        if case == 'stranger': headers = h.headers(user=99)
        if case == 'empty_allowlist': h.settings['approver_telegram_ids'] = []
        if case == 'boolean_user':
            h.settings['approver_telegram_ids'] = [1]
            headers = h.headers(user=True)
        if case == 'bad_key': headers['Idempotency-Key'] = 'short'
        if case == 'no_key': headers.pop('Idempotency-Key')
        async with TestClient(TestServer(h.app())) as client:
            response = await client.post(f'/api/windows/{w.id}/approve', headers=headers)
            assert response.status == status
            assert 'Access-Control-Allow-Origin' not in response.headers
            assert response.headers['Cache-Control'] == 'no-store'
        assert h.store.get(w.id).state == 'pending' and h.wakes == 0
    asyncio.run(main())


@pytest.mark.parametrize('method', ['get','head','put','delete','options'])
def test_only_post_can_write(h, method):
    async def main():
        w = h.propose()
        async with TestClient(TestServer(h.app())) as client:
            response = await getattr(client, method)(f'/api/windows/{w.id}/approve', headers=h.headers())
            assert response.status == 405
            assert 'Access-Control-Allow-Origin' not in response.headers
        assert h.store.get(w.id).state == 'pending' and h.wakes == 0
    asyncio.run(main())


def test_read_age_boundary_and_no_side_effects(h):
    async def main():
        w = h.propose()
        async with TestClient(TestServer(h.app())) as client:
            for route in ('/api/windows', f'/api/windows/{w.id}', '/api/audit'):
                assert (await client.get(route, headers=h.headers(age=3600))).status == 200
                assert (await client.get(route, headers=h.headers(age=3601))).status == 401
                assert (await client.get(route)).status == 401
            assert (await client.post(f'/api/windows/{w.id}/approve', headers=h.headers(age=600))).status == 200
        assert h.wakes == 1 and h.store.audit_tail() == []
    asyncio.run(main())


def test_overlap_badges_follow_scope_exclusivity_state_and_half_open_intervals(h):
    async def main():
        a = h.propose(exclusive=False)
        b = h.propose(exclusive=False)
        c = h.propose(exclusive=True)
        d = h.propose(scope='other')
        e = h.propose(start_ts=NOW + 3600, end_ts=NOW + 7200)
        async with TestClient(TestServer(h.app())) as client:
            async def rows():
                response = await client.get('/api/windows', headers=h.headers())
                return {w['id']: w for w in (await response.json())['windows']}
            data = await rows()
            assert data[a.id]['overlaps'] == [c.id]
            assert data[b.id]['overlaps'] == [c.id]
            assert set(data[c.id]['overlaps']) == {a.id,b.id}
            assert data[d.id]['overlaps'] == data[e.id]['overlaps'] == []
            response = await client.post(f'/api/windows/{c.id}/decline', headers=h.headers())
            assert response.status == 200
            assert all(not w['overlaps'] for w in (await rows()).values())
    asyncio.run(main())


def test_audit_excludes_previews_and_returns_only_last_twenty(h):
    async def main():
        for i in range(25):
            h.store.record_audit(now=NOW+i, verdict='blocked', tool='terminal', rule_ids=['k8s-write'],
                                 scopes=['k8s-prod'], preview='SYNTHETIC_PRIVATE_ARGUMENT_VALUE',
                                 session_id='not-for-miniapp', task_id='not-for-miniapp')
        async with TestClient(TestServer(h.app())) as client:
            response = await client.get('/api/audit', headers=h.headers())
            body = await response.json()
            assert len(body['audit']) == 20
            assert [r['ts'] for r in body['audit']] == list(range(NOW+24,NOW+4,-1))
            assert 'SYNTHETIC' not in json.dumps(body) and 'not-for-miniapp' not in json.dumps(body)
            assert 'preview' not in body['audit'][0]
    asyncio.run(main())


def test_window_responses_expose_only_display_fields(h, monkeypatch):
    from change_windows.store import Window
    original = Window.to_dict
    monkeypatch.setattr(Window, 'to_dict', lambda w: {**original(w), 'future_private_field': 'private'})
    fields = {'id', 'name', 'scope', 'start_ts', 'end_ts', 'exclusive', 'presence',
              'state', 'kind', 'reason', 'cards', 'checked_in_at', 'closed_at', 'decided_via'}

    async def main():
        w = h.propose()
        async with TestClient(TestServer(h.app())) as client:
            for _ in range(2):  # both the original decision and its persistent replay
                response = await client.post(f'/api/windows/{w.id}/approve', headers=h.headers())
                assert response.status == 200
                assert set((await response.json())['window']) == fields
            detail = await client.get(f'/api/windows/{w.id}', headers=h.headers())
            listing = await client.get('/api/windows', headers=h.headers())
            for row in ((await detail.json())['window'], (await listing.json())['windows'][0]):
                assert set(row) == fields | {'open', 'span', 'overlaps', 'conflicts', 'proposer', 'starts', 'decided_at',
                                             'decided_when', 'closed_when', 'card_details'}
                assert row['state'] == 'approved'
    asyncio.run(main())


def test_rate_limit_and_static_shell(h):
    async def main():
        w = h.propose()
        async with TestClient(TestServer(h.app())) as client:
            page = await client.get('/')
            assert page.status == 200 and 'Telegram' in await page.text()
            assert w.id not in await page.text()
            for _ in range(miniapp.WRITE_LIMIT):
                assert (await client.post(f'/api/windows/{w.id}/approve', headers=h.headers())).status == 200
            response = await client.post(f'/api/windows/{w.id}/approve', headers=h.headers())
            assert response.status == 429 and response.headers['Retry-After'] == '60'
            assert (await client.get('/api/windows', headers=h.headers())).status == 200
    asyncio.run(main())


def test_static_shell_is_loaded_once_per_application(h, monkeypatch):
    from pathlib import Path
    original = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        if path.name == 'miniapp.html':
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)

    async def main():
        async with TestClient(TestServer(h.app())) as client:
            bodies = [await (await client.get('/')).text() for _ in range(2)]
            assert bodies[0] == bodies[1] and 'Change windows' in bodies[0]
        assert len(reads) == 1
    asyncio.run(main())


def test_page_csp_allows_only_bundled_code_and_the_telegram_sdk(h):
    import base64
    import hashlib
    import re

    async def main():
        async with TestClient(TestServer(h.app())) as client:
            response = await client.get('/')
            html = await response.text()
            policy = response.headers['Content-Security-Policy']
            directives = {parts[0]: set(parts[1:]) for entry in policy.split(';')
                          if (parts := entry.split())}
            def digest(tag):
                [body] = re.findall(f'<{tag}>(.*?)</{tag}>', html, re.S)
                return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"
            assert directives['default-src'] == {"'none'"}
            assert directives['script-src'] == {digest('script'), 'https://telegram.org/js/telegram-web-app.js'}
            assert directives['style-src'] == {digest('style')}
            assert directives['connect-src'] == {"'self'"}
            assert directives['base-uri'] == directives['form-action'] == {"'none'"}
            assert 'unsafe-inline' not in policy and 'unsafe-eval' not in policy
    asyncio.run(main())


def test_eight_local_calendar_days(h):
    from zoneinfo import ZoneInfo
    h.actions.tz = lambda: ZoneInfo('Europe/Prague')
    now = int(datetime(2026,10,24,12,tzinfo=timezone.utc).timestamp())
    h.actions.clock = lambda: now  # includes the autumn DST change
    today = datetime(2026,10,24,tzinfo=h.actions.tz())
    start = int(today.timestamp())
    end = int(datetime(2026,11,1,tzinfo=h.actions.tz()).timestamp())
    inside = h.propose(start_ts=start-1,end_ts=start+1)
    h.propose(start_ts=start-100,end_ts=start)
    h.propose(start_ts=end,end_ts=end+100)
    async def main():
        fields = {'auth_date': str(now), 'user': json.dumps({'id': OWNER})}
        headers = {'X-Telegram-Init-Data': signed(fields,h.key)}
        async with TestClient(TestServer(h.app())) as client:
            response = await client.get('/api/windows', headers=headers)
            data = await response.json()
            assert (data['start'],data['end']) == (start,end)
            assert [w['id'] for w in data['windows']] == [inside.id]
    asyncio.run(main())


@pytest.mark.parametrize('host', ['0.0.0.0', '192.0.2.1', 'example.invalid'])
def test_listener_refuses_non_loopback_before_binding(h, host, monkeypatch, caplog):
    from aiohttp import web
    h.settings.update(miniapp_host=host, miniapp_port=18782)
    binds = []

    def forbidden_bind(*args, **kwargs):
        binds.append((args, kwargs))
        raise AssertionError('non-loopback listener must never be constructed')

    monkeypatch.setattr(web, 'TCPSite', forbidden_bind)

    async def main():
        task = asyncio.create_task(miniapp.serve(tg.Runtime(h.actions)))
        try:
            for _ in range(100):
                if caplog.records or task.done():
                    break
                await asyncio.sleep(.001)
            assert not task.done()  # failure waits for cancellation, rather than respawning
            assert binds == []
            assert caplog.text.count('Mini App listener unavailable') == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(main())


def test_authenticated_requests_never_persist_initdata(h, db_path):
    async def main():
        w = h.propose()
        headers = h.headers()
        assert 'auth_date=' in headers['X-Telegram-Init-Data']
        assert 'signature=' in headers['X-Telegram-Init-Data']
        async with TestClient(TestServer(h.app())) as client:
            assert (await client.post(f'/api/windows/{w.id}/approve', headers=headers)).status == 200
            assert (await client.get('/api/windows', headers=headers)).status == 200
        # Keep a connection open while inspecting SQLite/WAL, then inspect checkpointed data too.
        with sqlite3.connect(db_path) as conn:
            assert conn.execute('SELECT count(*) FROM api_actions').fetchone()[0] == 1
            for checkpoint in (False, True):
                if checkpoint:
                    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                data = b''.join(path.read_bytes() for path in db_path.parent.glob(db_path.name + '*'))
                assert b'auth_date' not in data and b'signature' not in data
                assert headers['X-Telegram-Init-Data'].encode() not in data
    asyncio.run(main())


def test_idempotency_keys_are_scoped_per_approver(h, db_path):
    h.settings['approver_telegram_ids'] = [42, 43]

    async def main():
        windows = [h.propose(exclusive=False), h.propose(exclusive=False)]
        async with TestClient(TestServer(h.app())) as client:
            for user, window in zip((42, 43), windows):
                path = f'/api/windows/{window.id}/approve'
                first = await client.post(path, headers=h.headers(user=user))
                assert first.status == 200
                body = await first.json()
                assert body['window']['id'] == window.id and body['window']['state'] == 'approved'
                replay = await client.post(path, headers=h.headers(user=user))
                assert replay.status == 200 and await replay.json() == body
        with sqlite3.connect(db_path) as conn:
            assert conn.execute('SELECT user_id, request_key FROM api_actions ORDER BY user_id').fetchall() == [
                (42, KEY), (43, KEY)]
    asyncio.run(main())
