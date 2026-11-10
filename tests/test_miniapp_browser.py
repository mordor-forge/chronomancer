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
            await page.get_by_role('button',name='<img src=x',exact=False).click()
            assert await page.locator('#detail-reason').text_content() == '<script>not markup</script>'
            await page.locator('#approve').click()
            await page.locator('#detail-state').filter(has_text='open now').wait_for()
            assert store.get(a.id).state == 'approved'
            await page.locator('#dismiss').click()
            await page.get_by_role('button',name='Alternative rollout',exact=False).click()
            await page.locator('#approve').click()
            await page.locator('#detail-status').filter(has_text="Can't approve 'Alternative rollout': it overlaps '<img src=x onerror=alert(1)>' (open, ").wait_for()
            assert store.get(b.id).state == 'pending'
            await page.locator('#decline').click()
            await page.locator('#detail-state').filter(has_text='declined').wait_for()
            await page.locator('#dismiss').click()
            await page.get_by_role('button',name='<img src=x',exact=False).click()
            await page.locator('#close').click()
            await page.locator('#detail-state').filter(has_text='closed').wait_for()
            assert not store.open_windows('k8s-prod', int(h.actions.clock()))
            assert errors == []
            await browser.close()
    asyncio.run(main())
