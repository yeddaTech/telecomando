"""Tests that need only the standard library:  python -m unittest discover -s tests"""

import asyncio
import base64
import json
import os
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telecomando import app, autostart, qr  # noqa: E402
from telecomando.control import Control, Session  # noqa: E402
from telecomando.input_dry import DryInput  # noqa: E402


# ---------------------------------------------------------------------- QR

def format_bits(m):
    """The two copies of the 15 format bits, as placed by the QR specification."""
    n = len(m)
    first = [(i, 8) for i in range(6)] + [(7, 8), (8, 8), (8, 7)] + [(8, 14 - i) for i in range(9, 15)]
    second = [(8, n - 1 - i) for i in range(8)] + [(n - 15 + i, 8) for i in range(8, 15)]
    return [sum(m[r][c] << i for i, (r, c) in enumerate(cells)) for cells in (first, second)]


class QrTest(unittest.TestCase):
    def test_version_follows_length(self):
        self.assertEqual(len(qr.matrix('x' * 14)), 21)
        self.assertEqual(len(qr.matrix('x' * 15)), 25)
        self.assertEqual(len(qr.matrix('x' * 213)), 57)
        with self.assertRaises(ValueError):
            qr.matrix('x' * 214)

    def test_fixed_patterns_and_format(self):
        m = qr.matrix('http://192.168.1.20:8765/?k=Wq7Zk2pXo9LmB4tR')
        n = len(m)
        finder = [[max(abs(x - 3), abs(y - 3)) not in (2, 4) for x in range(7)] for y in range(7)]
        for ox, oy in ((0, 0), (n - 7, 0), (0, n - 7)):
            self.assertEqual([row[ox:ox + 7] for row in m[oy:oy + 7]], finder)
        self.assertEqual([m[6][x] for x in range(8, n - 8)], [x % 2 == 0 for x in range(8, n - 8)])
        self.assertTrue(m[n - 8][8])
        a, b = format_bits(m)
        self.assertEqual(a, b)
        data = (a ^ 0x5412) >> 10
        self.assertEqual(data >> 3, 0)  # error correction level M
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        self.assertEqual(a ^ 0x5412, data << 10 | rem)

    def test_outputs(self):
        self.assertTrue(qr.svg('hello').startswith('<svg'))
        lines = qr.terminal('hello').splitlines()
        self.assertEqual(len(lines), (21 + 4 + 1) // 2)


# ----------------------------------------------------------------- control

class SlowRepeat(DryInput):
    repeat = (0.03, 0.01)


class ControlTest(unittest.TestCase):
    def run_messages(self, backend, messages, then=None):
        async def go():
            control, session = Control(backend), Session()
            for message in messages:
                await control.handle(message, session)
            if then:
                await then(control, session)
        asyncio.run(go())
        return backend.events

    def test_numbers_are_clamped(self):
        events = self.run_messages(DryInput(), [{'t': 'move', 'x': 1e12, 'y': '5'},
                                                {'t': 'scroll', 'x': None, 'y': float('nan')}])
        self.assertEqual(events, [('move', 10000, 5), ('scroll', 0, 0)])

    def test_unknown_things_are_ignored(self):
        events = self.run_messages(DryInput(), [{'t': 'key', 'k': 'power'}, {'t': 'btn', 'b': 'x'},
                                                {'t': 'combo', 'keys': 'ctrl+c'}, [1, 2], 'text'])
        self.assertEqual(events, [])

    def test_text(self):
        events = self.run_messages(DryInput(), [{'t': 'text', 's': 'a\r\nb'}])
        self.assertEqual(events, [('char', repr('a')), ('char', repr('\n')), ('char', repr('b'))])

    def test_release_when_the_phone_goes_away(self):
        async def leave(control, session):
            control.release_all(session)
        events = self.run_messages(DryInput(), [{'t': 'key', 'k': 'ctrl', 'a': 'down'},
                                                {'t': 'btn', 'b': 'left', 'a': 'down'}], leave)
        self.assertEqual(events[2:], [('key', 'ctrl', 'up'), ('button', 'left', 'up')])

    def test_software_repeat(self):
        async def hold(control, session):
            await asyncio.sleep(0.1)
            await control.handle({'t': 'key', 'k': 'backspace', 'a': 'up'}, session)
            await asyncio.sleep(0.05)
        events = self.run_messages(SlowRepeat(), [{'t': 'key', 'k': 'backspace', 'a': 'down'},
                                                  {'t': 'key', 'k': 'shift', 'a': 'down'}], hold)
        downs = events.count(('key', 'backspace', 'down'))
        self.assertGreaterEqual(downs, 3)
        self.assertEqual(events[-1], ('key', 'backspace', 'up'))
        self.assertEqual(events.count(('key', 'shift', 'down')), 1)  # modifiers never repeat


# ------------------------------------------------------------------ server

def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


async def http(port, path, method='GET', host=None, headers=(), body=b''):
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    head = '%s %s HTTP/1.1\r\nHost: %s\r\n' % (method, path, host or '127.0.0.1:%d' % port)
    head += ''.join('%s: %s\r\n' % h for h in headers)
    head += 'Content-Length: %d\r\n\r\n' % len(body)
    writer.write(head.encode() + body)
    data = await reader.read()
    await close(writer)
    return int(data.split(b' ', 2)[1]), data.partition(b'\r\n\r\n')[2]


async def close(writer):
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass


class WsClient:
    async def connect(self, port, token):
        self.reader, self.writer = await asyncio.open_connection('127.0.0.1', port)
        key = base64.b64encode(os.urandom(16)).decode()
        self.writer.write(('GET /ws?k=%s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n'
                           'Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n'
                           'Origin: http://127.0.0.1:%d\r\n\r\n' % (token, port, key, port)).encode())
        head = await self.reader.readuntil(b'\r\n\r\n')
        return head.startswith(b'HTTP/1.1 101')

    def send(self, text, opcode=1):
        data, mask = text.encode(), os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.writer.write(struct.pack('!BB', 0x80 | opcode, 0x80 | len(data)) + mask + masked)

    async def recv(self):
        b0, b1 = await self.reader.readexactly(2)
        n = b1 & 0x7F
        if n == 126:
            n, = struct.unpack('!H', await self.reader.readexactly(2))
        return b0 & 0x0F, await self.reader.readexactly(n)


class ServerTest(unittest.TestCase):
    def test_server(self):
        port = free_port()
        with tempfile.TemporaryDirectory() as directory:
            args = app.parse_args(['--port', str(port), '--dry-run', '--background', '--name', 'Test'])
            token, _ = app.load_token(Path(directory))
            instance = app.App(args, token, Path(directory))
            asyncio.run(self.scenario(instance, port, token))

    async def scenario(self, instance, port, token):
        task = asyncio.ensure_future(instance.run(False))
        await asyncio.sleep(0.3)
        try:
            status, body = await http(port, '/')
            self.assertEqual(status, 200)
            self.assertIn(b'<title>Telecomando</title>', body)
            self.assertEqual((await http(port, '/pc'))[0], 200)
            self.assertEqual((await http(port, '/pc', host='attacker.example:%d' % port))[0], 403)
            status, body = await http(port, '/pc/state')
            state = json.loads(body)
            self.assertTrue(state['url'].endswith('?k=' + token))
            self.assertTrue(state['qr'].startswith('<svg'))

            wrong = WsClient()
            self.assertTrue(await wrong.connect(port, 'wrong'))
            opcode, payload = await wrong.recv()
            self.assertEqual((opcode, struct.unpack('!H', payload[:2])[0]), (8, 4401))
            await close(wrong.writer)

            phone = WsClient()
            self.assertTrue(await phone.connect(port, token))
            opcode, payload = await phone.recv()
            hello = json.loads(payload)
            self.assertEqual((hello['t'], hello['host'], hello['ready']), ('hello', 'Test', True))
            phone.send(json.dumps({'t': 'text', 's': 'hi'}))
            phone.send('not json')
            phone.send(json.dumps({'t': 'key', 'k': 'shift', 'a': 'down'}))
            await asyncio.sleep(0.2)
            self.assertEqual(instance.backend.events[:2], [('char', "'h'"), ('char', "'i'")])

            self.assertEqual((await http(port, '/pc/new-code', 'POST', body=b'{}'))[0], 403)
            status, body = await http(port, '/pc/new-code', 'POST', headers=[('X-Telecomando', '1')], body=b'{}')
            self.assertEqual(status, 200)
            self.assertNotEqual(instance.token, token)
            opcode, payload = await phone.recv()
            self.assertEqual((opcode, struct.unpack('!H', payload[:2])[0]), (8, 4401))
            await asyncio.sleep(0.2)
            self.assertEqual(instance.backend.events[-1], ('key', 'shift', 'up'))  # released on disconnect
            await close(phone.writer)

            status, body = await http(port, '/pc/stop', 'POST', headers=[('X-Telecomando', '1')], body=b'{}')
            self.assertEqual(json.loads(body)['app'], 'telecomando')
            await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                instance.stopping.set()
                await task


# ----------------------------------------------------------------- systems

class AutostartTest(unittest.TestCase):
    def test_desktop_quoting(self):
        self.assertEqual(autostart._desktop_quote('/usr/bin/telecomando'), '/usr/bin/telecomando')
        self.assertEqual(autostart._desktop_quote('/opt/my apps/x%'), '"/opt/my apps/x%%"')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux only')
    def test_linux(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': directory}):
            self.assertFalse(autostart.is_enabled())
            autostart.enable(['--port', '9999'])
            text = (Path(directory) / 'autostart' / 'telecomando.desktop').read_text()
            self.assertIn('--background --port 9999', text)
            self.assertTrue(autostart.is_enabled())
            autostart.disable()
            self.assertFalse(autostart.is_enabled())

    @unittest.skipUnless(os.name == 'nt', 'Windows only')
    def test_windows(self):
        if autostart.is_enabled():
            self.skipTest('automatic start is already on for this user: leaving it alone')
        autostart.enable(['--port', '9999'])
        try:
            self.assertTrue(autostart.is_enabled())
        finally:
            autostart.disable()
        self.assertFalse(autostart.is_enabled())


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux only')
class LinuxTest(unittest.TestCase):
    def test_layout_override(self):
        from telecomando.input_linux import detect_layout
        self.assertEqual(detect_layout('de:nodeadkeys'), ('de', 'nodeadkeys', ''))

    def test_keymap(self):
        from telecomando.input_linux import compile_keymap
        try:
            keymap = compile_keymap('us', '', '')
        except OSError:
            self.skipTest('libxkbcommon is not installed')
        self.assertEqual(keymap['a'], (30, ()))
        self.assertEqual(keymap['A'], (30, (42,)))
        self.assertEqual(keymap['@'], (3, (42,)))


@unittest.skipUnless(os.name == 'nt', 'Windows only')
class WindowsTest(unittest.TestCase):
    def test_backend(self):
        import ctypes
        from telecomando.input_windows import INPUT, VK, WindowsInput
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(ctypes.sizeof(INPUT), 40)
        backend = WindowsInput()
        delay, interval = backend.repeat
        self.assertTrue(0.25 <= delay <= 1.0 and 0.03 <= interval <= 0.5)
        self.assertEqual(backend._resolve('ctrl'), [VK['ctrl']])
        self.assertEqual(backend._resolve('a')[-1], (0x41, False))
        # Shift down and up: no visible effect, but proves Windows accepts the structures.
        sent = backend._send(backend._vk(*VK['shift']), backend._vk(*VK['shift'], up=True))
        if sent != 2:
            self.assertNotEqual(ctypes.get_last_error(), 87)  # 87 = wrong structure size
        backend.close()


if __name__ == '__main__':
    unittest.main()
