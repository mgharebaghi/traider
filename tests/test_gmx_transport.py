"""Exercise the real subprocess pipe boundary without calling GMX or signing."""
import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_bot import settings_at
from gmx_client import GmxClient


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        s = settings_at(Path(temp.name), exchange_id='gmx', entry_post_only=False,
            gmx_live_enabled=False, gmx_private_key='must-not-reach-read-process')
        self.client = GmxClient(s)
        self.addAsyncCleanup(self.client.close)
        self.spawn = asyncio.create_subprocess_exec

    def child(self, code):
        async def spawn(*_, **kwargs):
            self.child_env = kwargs['env']
            return await self.spawn(sys.executable, '-u', '-c', code, **kwargs)
        return patch('gmx_client.asyncio.create_subprocess_exec', side_effect=spawn)

    async def test_valid_response_does_not_wait_for_process_exit(self):
        code = "import sys,time; sys.stdin.buffer.read(); print('{\"ok\":true,\"result\":[1]}',flush=True); time.sleep(30)"
        with self.child(code):
            result = await asyncio.wait_for(self.client.rpc('candles'), 5)
        self.assertEqual(result, [1])
        self.assertFalse(self.client._processes)
        self.assertNotIn('GMX_PRIVATE_KEY', self.child_env)

    async def test_large_response_is_not_limited_to_default_pipe_line_size(self):
        code = "import sys,json; sys.stdin.buffer.read(); print(json.dumps({'ok':True,'result':'x'*200000}),flush=True)"
        with self.child(code):
            result = await self.client.rpc('markets')
        self.assertEqual(len(result), 200000)

    async def test_timeout_has_operation_and_cleanup(self):
        self.client.bridge_timeout_sec = .1
        with self.child('import time; time.sleep(30)'):
            with self.assertRaisesRegex(TimeoutError, 'candles') as caught:
                await self.client.rpc('candles')
        self.assertTrue(str(caught.exception))
        self.assertFalse(self.client._processes)

    async def test_empty_output_explains_bridge_failure(self):
        with self.child('import sys; sys.stdin.buffer.read()'):
            with self.assertRaisesRegex(RuntimeError, 'no valid response'):
                await self.client.rpc('candles')

    async def test_public_network_error_does_not_blame_wallet(self):
        response = json.dumps(dict(ok=False, error='AbortError'))
        code = f'import sys; sys.stdin.buffer.read(); print({response!r},flush=True)'
        with self.child(code):
            with self.assertRaisesRegex(RuntimeError, 'AbortError'):
                await self.client.rpc('candles')

    async def test_overlapping_public_reads_share_inflight_request(self):
        release = asyncio.Event()
        async def fetch(*_, **__):
            await release.wait()
            return [1]
        self.client._rpc_once = AsyncMock(side_effect=fetch)
        first = asyncio.create_task(self.client.rpc('candles', timeframe='1m'))
        second = asyncio.create_task(self.client.rpc('candles', timeframe='1m'))
        await asyncio.sleep(.01)
        release.set()
        self.assertEqual(await asyncio.gather(first, second), [[1], [1]])
        self.client._rpc_once.assert_awaited_once()

    async def test_submit_does_not_share_or_retry(self):
        self.client._rpc_once = AsyncMock(side_effect=TimeoutError('unknown submission'))
        with self.assertRaises(TimeoutError):
            await self.client.rpc('submit', prepared='opaque')
        self.client._rpc_once.assert_awaited_once()
