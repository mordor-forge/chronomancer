"""Phone-check harness using the pinned scratch Hermes plugin manager and native PTB seam.

The normal Hermes Telegram adapter sets bot commands on connect, so this harness uses a plain
Application and getUpdates directly. It does not start an agent/model, change a menu or webhook,
or run kubectl. Only /pair, plugin callbacks and /windows are handled. Use --smoke without a phone.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def prepare() -> tuple[Path, dict]:
    scratch = Path(os.environ['CW_DEMO_SCRATCH']).resolve()
    home = Path(os.environ['HERMES_HOME']).resolve()
    if not str(scratch).startswith('/tmp/cw-scratch.demo.') or home != scratch / 'home':
        raise RuntimeError('run only through dev/run-demo.sh and dev/scratch-env.sh')
    if Path(os.environ['HOME']).resolve() != scratch / 'userhome':
        raise RuntimeError('pinned-core scratch HOME required')
    kube = os.environ.get('KUBECONFIG')
    if kube != '/dev/null' and kube != str(scratch / 'kind-kubeconfig'):
        raise RuntimeError('scratch kubeconfig required')
    url = os.environ['CW_DEMO_URL']
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise RuntimeError('valid HTTPS Mini App URL required')
    source = Path(os.environ['CW_DEMO_SOURCE']).resolve()
    destination = home / 'plugins' / 'change-windows'
    destination.mkdir(parents=True)
    # Explicit source allow-list. Never copy local secret/sandbox markers or Git metadata.
    for name in ('__init__.py','plugin.yaml','README.md','LICENSE'):
        shutil.copy2(source / name, destination / name)
    shutil.copytree(source / 'change_windows', destination / 'change_windows',
                    ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    config = {'plugins': {'enabled': ['change-windows'], 'isolation':'in_process', 'entries': {
        'change-windows': {'settings': {'mode':'enforce','timezone':'Europe/Prague',
            'miniapp_url':url,'miniapp_host':'127.0.0.1','miniapp_port':18782,'approver_telegram_ids':[]}}}}}
    save_config(home, config)
    return home, config


def save_config(home: Path, config: dict) -> None:
    # JSON is a YAML subset, accepted by Hermes without another demo dependency.
    temporary = home / 'config.yaml.tmp'
    temporary.write_text(json.dumps(config))
    temporary.replace(home / 'config.yaml')


def read_token() -> str:
    # Used only inside the clean scratch environment. Never print or persist the token.
    path = Path(os.environ['CW_DEMO_TOKEN_FILE'])
    if path.stat().st_mode & 0o077:
        raise RuntimeError('demo token file must have private permissions')
    for line in path.read_text().splitlines():
        key, _, value = line.strip().removeprefix('export ').partition('=')
        if key.strip() == 'TELEGRAM_BOT_TOKEN':
            token = value.strip().strip('\"\'')
            if token:
                return token
    raise RuntimeError('demo bot token unavailable')


async def main(smoke: bool, wait: bool = False) -> None:
    from aiohttp import ClientSession
    from telegram.ext import Application, CommandHandler

    home, config = prepare()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    app = Application.builder().token(read_token()).updater(None).build()
    initialized = started = False
    manager = None
    poll_task = None
    try:
        await app.initialize()
        initialized = True
        hook = await app.bot.get_webhook_info()
        if hook.url:
            raise RuntimeError('shared demo bot has a webhook; refusing to change or poll it')
        # Integration harness only: plugin source uses public ctx APIs, not these core imports.
        from hermes_cli.plugins import discover_plugins, get_plugin_manager
        import model_tools
        discover_plugins()
        manager = get_plugin_manager()
        factories = manager.get_platform_handler_factories('telegram')
        if len(factories) != 1:
            raise RuntimeError('expected exactly one scratch Telegram plugin factory')
        factories[0][0](app, None)
        await app.start()
        started = True
        async with ClientSession() as client:
            for _ in range(100):
                try:
                    async with client.get('http://127.0.0.1:18782/') as response:
                        assert response.status == 200 and 'Change windows' in await response.text()
                    break
                except OSError:
                    await asyncio.sleep(.05)
            else:
                raise RuntimeError('Mini App did not bind on loopback')
            async with client.get('http://127.0.0.1:18782/api/windows') as response:
                assert response.status == 401
        print('READY: scratch Hermes plugin loaded; loopback page=200; unauthenticated API=401.', flush=True)
        print(f'STOP COMMAND: kill -TERM {os.getpid()}', flush=True)
        if smoke:
            print('SMOKE PASS: bot initialized; no updates polled and no messages sent.', flush=True)
            if wait:
                await stop.wait()
            return

        paired = False
        pair_code = secrets.token_hex(4)
        pairing_deadline = time.monotonic() + 30 * 60
        wrong_attempts = 0

        def call(name, args):
            return json.loads(model_tools.handle_function_call(name, args, task_id='cw-demo'))

        async def pair(update, context):
            nonlocal paired, wrong_attempts
            if paired or time.monotonic() > pairing_deadline or not update.effective_user:
                return
            if update.effective_chat.type != 'private':
                return
            if context.args != [pair_code]:
                wrong_attempts += 1
                if wrong_attempts >= 5:
                    print('Pairing stopped after five incorrect codes.', flush=True)
                    stop.set()
                return
            paired = True
            config['plugins']['entries']['change-windows']['settings']['approver_telegram_ids'] = [update.effective_user.id]
            save_config(home, config)
            await update.effective_message.reply_text('Paired for this scratch run. Use /windows to open the list.')
            now = int(time.time())
            for name in ('Roll out web v2', 'Alternative rollout'):
                result = await asyncio.to_thread(call, 'window_propose', {
                    'name':name, 'scope':'k8s-prod', 'start':datetime.fromtimestamp(now,timezone.utc).isoformat(),
                    'duration_minutes':30,'reason':'Synthetic phone check; no cluster commands run.'})
                if not result.get('ok'):
                    raise RuntimeError('could not seed the phone-check proposals')
            print('PAIRED: two overlapping proposals queued. Approve one, decline the other, then close.', flush=True)

        app.add_handler(CommandHandler('pair',pair))
        # Direct polling deliberately avoids Updater.start_polling's deleteWebhook bootstrap.
        async def poll():
            offset = None
            while not stop.is_set():
                try:
                    updates = await app.bot.get_updates(offset=offset, timeout=10,
                                                        allowed_updates=['message','callback_query'])
                    for update in updates:
                        offset = update.update_id + 1
                        await app.process_update(update)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f'Polling interrupted ({type(exc).__name__}); retrying.', flush=True)
                    await asyncio.sleep(2)
                if not paired and time.monotonic() > pairing_deadline:
                    print('Pairing expired; stopping.', flush=True)
                    stop.set()
        print(f'PAIR: send /pair {pair_code} in the demo bot private chat. Expires in 30 minutes.', flush=True)
        print('STOP: press Ctrl-C (or SIGTERM this process).', flush=True)
        poll_task = asyncio.create_task(poll())
        await stop.wait()
    finally:
        if poll_task:
            poll_task.cancel()
            await asyncio.gather(poll_task, return_exceptions=True)
        tasks = [t for t in asyncio.all_tasks() if t.get_name().startswith('change-windows:')]
        if manager:
            manager.unload('change-windows')
        await asyncio.gather(*tasks, return_exceptions=True)
        if started:
            await app.stop()
        if initialized:
            await app.shutdown()
        print('STOPPED: scratch bot and Mini App listener shut down.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--wait', action='store_true', help='keep --smoke running until SIGTERM/Ctrl-C')
    args = parser.parse_args()
    if args.wait and not args.smoke:
        parser.error('--wait requires --smoke')
    # HTTP clients include credential-bearing request URLs in logs. This harness never emits them.
    logging.basicConfig(level=logging.CRITICAL)
    try:
        asyncio.run(main(args.smoke, args.wait))
    except Exception as exc:
        print(f'Demo stopped ({type(exc).__name__}); check scratch configuration and connectivity. Details suppressed.')
        raise SystemExit(1)
