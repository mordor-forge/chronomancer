"""Chromium UI check against the real API; only Telegram SDK/signatures are synthetic."""
import asyncio
import json
import os
import shutil

import pytest
from aiohttp.test_utils import TestServer

pytest.importorskip('playwright.async_api')
from playwright.async_api import async_playwright
from test_miniapp import Harness


def test_phone_size_browser_decisions_and_safe_text(store, tmp_path):
    async def main():
        h = Harness(store)
        a = h.propose(name='<img src=x onerror=alert(1)>')
        b = h.propose(name='Alternative rollout')
        async with TestServer(h.app()) as server, async_playwright() as p:
            executable = shutil.which('chromium')
            if not executable:
                pytest.skip('system chromium unavailable')
            browser = await p.chromium.launch(executable_path=executable, headless=True)
            page = await browser.new_page(viewport={'width':390,'height':844}, device_scale_factor=1)
            errors = []
            page.on('pageerror',lambda error:errors.append(str(error)))
            script = 'window.Telegram={WebApp:{initData:' + json.dumps(h.headers()['X-Telegram-Init-Data']) + \
                     ',themeParams:{bg_color:"#f5f6f8"},onEvent(){},ready(){},expand(){}}};'
            async def route(request):
                if request.request.url == 'https://telegram.org/js/telegram-web-app.js':
                    await request.fulfill(content_type='application/javascript',body=script)
                elif request.request.url.startswith(str(server.make_url('/'))):
                    await request.continue_()
                else:
                    await request.abort()
            await page.route('**/*',route)
            await page.goto(str(server.make_url('/')))
            await page.locator('.window').first.wait_for()
            assert await page.locator('.window').count() == 2
            assert await page.locator('.badge').count() == 2
            assert await page.locator('img').count() == 0
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.screenshot(path=os.environ.get('CW_SCREENSHOT', str(tmp_path/'miniapp.png')),full_page=True)
            await page.locator('.window',has_text='<img src=x').click()
            assert await page.locator('#detail-reason').text_content() == '<script>not markup</script>'
            await page.locator('#approve').click()
            await page.locator('#detail-state').filter(has_text='open now').wait_for()
            assert store.get(a.id).state == 'approved'
            await page.locator('#dismiss').click()
            await page.locator('.window',has_text='Alternative rollout').click()
            await page.locator('#approve').click()
            await page.locator('#detail-status').filter(has_text="Can't approve 'Alternative rollout': it overlaps '<img src=x onerror=alert(1)>' (open, ").wait_for()
            assert store.get(b.id).state == 'pending'
            await page.locator('#decline').click()
            await page.locator('#detail-state').filter(has_text='declined').wait_for()
            await page.locator('#dismiss').click()
            await page.locator('.window',has_text='<img src=x').click()
            await page.locator('#close').click()
            await page.locator('#detail-state').filter(has_text='closed').wait_for()
            assert not store.open_windows('k8s-prod', int(h.actions.clock()))
            assert errors == []
            await browser.close()
    asyncio.run(main())


# Telegram's own default palettes (themeParams), light and dark.
THEMES = {
    'light': {'bg_color': '#ffffff', 'text_color': '#000000', 'hint_color': '#999999', 'secondary_bg_color': '#f0f0f0',
              'button_color': '#2481cc', 'button_text_color': '#ffffff', 'destructive_text_color': '#ff3b30'},
    'dark': {'bg_color': '#17212b', 'text_color': '#f5f5f5', 'hint_color': '#708499', 'secondary_bg_color': '#232e3c',
             'button_color': '#5288c1', 'button_text_color': '#ffffff', 'destructive_text_color': '#ec3942'},
}
CONTRAST_JS = """() => {
  const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
  const ctx = canvas.getContext('2d');
  const rgb = c => { ctx.clearRect(0,0,1,1); ctx.fillStyle = c; ctx.fillRect(0,0,1,1); return [...ctx.getImageData(0,0,1,1).data].slice(0,3); };
  const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126*f(c[0]) + 0.7152*f(c[1]) + 0.0722*f(c[2]); };
  const ratio = (a, b) => { const [x, y] = [lum(rgb(a)), lum(rgb(b))].sort((p, q) => q - p); return Math.round((x + 0.05) / (y + 0.05) * 100) / 100; };
  const css = getComputedStyle(document.documentElement), v = n => css.getPropertyValue(n).trim();
  const colour = sel => getComputedStyle(document.querySelector(sel));
  return {
    'text on page': ratio(colour('#door-title').color, v('--bg')),
    'text on card': ratio(colour('.window strong').color, v('--card')),
    'primary button label': ratio(colour('#approve').color, colour('#approve').backgroundColor),
    'secondary button label': ratio(colour('#refresh').color, colour('#refresh').backgroundColor),
    'shape: bars, needle, borders on card': ratio(v('--text'), v('--card')),
    'shape: open bar fill on card (outlined in text colour)': ratio(v('--button'), v('--card')),
    'shape: close button border on page': ratio(v('--danger'), v('--bg')),
  };
}"""


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_door_runway_and_the_conflict_sheet_in_a_light_and_a_dark_telegram_theme(store, tmp_path, monkeypatch, theme):
    from change_windows import cards
    monkeypatch.setattr(cards, '_reader', cards.CardReader(lambda tool, args: {'task': {
        'title': '<i>Roll out</i> ingress 1.11', 'status': 'ready', 'assignee': 'ops'}}))
    shots = os.environ.get('CW_SCREENSHOT_DIR', str(tmp_path))

    async def main():
        h = Harness(store)
        now = int(h.actions.clock())
        a = h.propose(name='Ingress controller rollout', start_ts=now - 600, end_ts=now + 2400, reason='Roll out ingress 1.11',
                      cards=[{'task_id': 't_19e1d9c7', 'board': ''}])
        b = h.propose(name='Postgres minor upgrade', scope='db-prod', start_ts=now + 7200, end_ts=now + 10800, reason='16.3 to 16.4')
        c = h.propose(name='Index rebuild on orders', scope='db-prod', start_ts=now + 9000, end_ts=now + 12600, reason='bloat',
                      proposed_by='agent:dba')
        d = h.propose(name='Web v2 rollout', start_ts=now + 86400, end_ts=now + 90000, reason='v2')
        for w in (a, d):
            store.decide(w.id, approve=True, by='telegram:42', now=now - 700, via='telegram')
        async with TestServer(h.app()) as server, async_playwright() as p:
            executable = shutil.which('chromium')
            if not executable:
                pytest.skip('system chromium unavailable')
            browser = await p.chromium.launch(executable_path=executable, headless=True)
            page = await browser.new_page(viewport={'width': 390, 'height': 844}, device_scale_factor=2,
                                          color_scheme=theme)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
            script = ('window.Telegram={WebApp:{initData:' + json.dumps(h.headers()['X-Telegram-Init-Data']) +
                      ',themeParams:' + json.dumps(THEMES[theme]) + ',onEvent(){},ready(){},expand(){}}};')

            async def route(request):
                if request.request.url == 'https://telegram.org/js/telegram-web-app.js':
                    await request.fulfill(content_type='application/javascript', body=script)
                elif request.request.url.startswith(str(server.make_url('/'))):
                    await request.continue_()
                else:
                    await request.abort()
            await page.route('**/*', route)
            await page.goto(str(server.make_url('/')))
            await page.locator('.window').first.wait_for()

            # the door: one sentence and a glyph, in the plugin's zone
            assert await page.locator('#door').get_attribute('data-door') == 'open'
            assert await page.locator('#door-title').text_content() == 'Open: Ingress controller rollout, 40 min left'
            sub = await page.locator('#door-sub').text_content()
            assert sub == ('Matched writes in scope k8s-prod are allowed until 08:40. Other scopes stay blocked. '
                           '2 proposals wait for your decision.')
            # the runway: today and tomorrow, one bar per window, one overlap band, a needle on today
            assert await page.locator('#runway svg').count() == 2
            kinds = await page.evaluate("[...document.querySelectorAll('[data-bar]')].map(e => e.getAttribute('class'))")
            assert kinds == ['bar open', 'bar pending', 'bar pending', 'bar approved']
            assert await page.locator('.band').count() == 1 and await page.locator('.needle').count() == 1
            assert await page.locator('.nowlabel').text_content() == 'now 08:00'
            assert await page.evaluate("[...document.querySelectorAll('[data-bar]')].every(e => e.getAttribute('role') === 'button' && e.tabIndex === 0)")
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            # colours come from the Telegram theme
            assert await page.evaluate("getComputedStyle(document.body).backgroundColor") == \
                'rgb(%d, %d, %d)' % tuple(int(THEMES[theme]['bg_color'][i:i + 2], 16) for i in (1, 3, 5))
            contrast = await page.evaluate(CONTRAST_JS)
            assert all(contrast[k] >= 4.5 for k in contrast if not k.startswith('shape')), contrast
            assert contrast['shape: bars, needle, borders on card'] >= 3, contrast
            with open(os.path.join(shots, f'miniapp-{theme}-contrast.json'), 'w') as out:
                json.dump(contrast, out, indent=1)
            await page.screenshot(path=os.path.join(shots, f'miniapp-{theme}-page.png'), full_page=True)

            # a bar is a keyboard button: focus reads it out, Enter opens its window
            await page.locator(f'[data-bar="{b.id}"]').focus()
            assert (await page.locator('#readout').text_content()).startswith('Postgres minor upgrade · pending · ')
            await page.keyboard.press('Enter')
            assert await page.locator('#detail-name').text_content() == 'Postgres minor upgrade'
            assert await page.locator('#detail-stamp').text_content() == 'Awaiting your decision'
            assert await page.locator('#detail-id').text_content() == b.id and await page.locator('#back').is_hidden()
            # the sheet covers the whole page: no list text shows under its bottom edge
            box = await page.locator('#detail').bounding_box()
            assert box['y'] == 0 and box['height'] == 844 and box['width'] == 390
            # tapping the overlap opens the OTHER window with the explanation; Back returns
            await page.locator('#detail-overlaps .badge').click()
            assert await page.locator('#detail-name').text_content() == 'Index rebuild on orders'
            explain = await page.locator('#detail-explain').inner_text()
            for text in ("This window and 'Postgres minor upgrade' overlap", 'Postgres minor upgrade · pending · ',
                         "scope db-prod · proposed by profile 'test'", '10:30-11:00, 30 min', 'same scope db-prod; both windows are exclusive',
                         'Only one of them can be approved', 'Decline one of them, or ask the agent to propose another time.'):
                assert text in explain, (text, explain)
            assert await page.locator('.item.lit').count() == 2 and await page.locator('.bar.lit').count() == 2
            await page.screenshot(path=os.path.join(shots, f'miniapp-{theme}-conflict-sheet.png'))
            await page.locator('#back').click()
            assert await page.locator('#detail-name').text_content() == 'Postgres minor upgrade'
            assert await page.locator('#detail-explain').is_hidden()
            await page.locator('#dismiss').click()
            assert await page.locator('.item.lit').count() == 0
            # from the list, the overlap opens the other window too, and Back closes the sheet
            await page.locator(f'.item[data-window="{c.id}"] .badge').click()
            assert await page.locator('#detail-name').text_content() == 'Postgres minor upgrade'
            assert "This window and 'Index rebuild on orders' overlap" in await page.locator('#detail-explain').inner_text()
            await page.locator('#back').click()
            assert not await page.evaluate("document.getElementById('detail').open")
            # a card title is board-written text: shown with its status, never as markup
            await page.locator('.window', has_text='Ingress controller rollout').click()
            assert await page.locator('#detail-cards').inner_text() == '<i>Roll out</i> ingress 1.11 · ready · ops t_19e1d9c7'
            assert await page.locator('dialog i').count() == 0
            assert await page.locator('#detail-stamp').text_content() == 'Approved in Telegram, 07:48'
            await page.screenshot(path=os.path.join(shots, f'miniapp-{theme}-window-sheet.png'))
            # the refusal is a sentence
            await page.locator('#dismiss').click()
            store.decide(b.id, approve=True, by='telegram:42', now=now, via='telegram')
            await page.locator('#refresh').click()
            await page.locator('.window', has_text='Index rebuild on orders').click()
            await page.locator('#approve').click()
            await page.locator('#detail-status').filter(has_text="Can't approve 'Index rebuild on orders': it overlaps "
                                                        "'Postgres minor upgrade' (approved, 10:00-11:00, db-prod) for 30 min.").wait_for()
            # the browser logs the refused approval (409) itself; nothing else may be logged
            assert [e for e in errors if '409 (Conflict)' not in e] == [] and len(errors) == 1
            await browser.close()
    asyncio.run(main())


@pytest.mark.parametrize('mode,title', [('warn', 'Warn mode: production writes are recorded, not blocked'),
                                        ('off', 'Guard off: production writes are not checked'),
                                        ('enforce', 'Closed: production writes are blocked')])
def test_the_list_reports_the_guard_mode_for_the_door(store, mode, title):
    from aiohttp.test_utils import TestClient

    async def main():
        h = Harness(store)
        h.settings['mode'] = mode
        async with TestClient(TestServer(h.app())) as client:
            data = await (await client.get('/api/windows', headers=h.headers())).json()
            assert data['mode'] == mode and [d['label'] for d in data['days']] == ['Fri 15 Jan', 'Sat 16 Jan']
            html = await (await client.get('/')).text()
            assert title in html  # the page has the sentence for this mode
    asyncio.run(main())
