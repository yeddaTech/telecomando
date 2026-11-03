"""A minimal HTTP/1.1 and WebSocket (RFC 6455) server on top of asyncio.

It only does what Telecomando needs: a few small pages, a JSON API for the
control panel and one WebSocket per phone. Only devices of the local network
(private addresses) and this PC itself can connect.
"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import struct
from urllib.parse import parse_qsl, urlsplit

log = logging.getLogger('telecomando')

MAX_HEAD = 16 * 1024
MAX_BODY = 16 * 1024
MAX_MESSAGE = 64 * 1024
WS_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
REASONS = {
    200: 'OK', 400: 'Bad Request', 403: 'Forbidden', 404: 'Not Found',
    405: 'Method Not Allowed', 426: 'Upgrade Required', 500: 'Internal Server Error',
}
CLOSE, PING, PONG, TEXT, BINARY, CONTINUATION = 0x8, 0x9, 0xA, 0x1, 0x2, 0x0


class Request:
    def __init__(self, method, target, headers, body, peer, reader, writer):
        url = urlsplit(target)
        self.method, self.path = method, url.path
        self.query = dict(parse_qsl(url.query))
        self.headers, self.body, self.peer = headers, body, peer
        self.reader, self.writer = reader, writer

    def json(self):
        return json.loads(self.body.decode('utf-8') or 'null')


class Response:
    def __init__(self, status=200, body=b'', content_type='text/plain; charset=utf-8', headers=None):
        self.status = status
        self.body = body.encode('utf-8') if isinstance(body, str) else body
        self.content_type = content_type
        self.headers = headers or {}


def json_response(data, status=200):
    return Response(status, json.dumps(data), 'application/json')


async def _read_request(reader, writer, peer):
    head = await reader.readuntil(b'\r\n\r\n')
    lines = head.decode('latin-1').split('\r\n')
    method, target, version = lines[0].split(' ')
    if not version.startswith('HTTP/1.'):
        raise ValueError('not HTTP/1.x')
    headers = {}
    for line in lines[1:]:
        if line:
            name, colon, value = line.partition(':')
            if not colon:
                raise ValueError('bad header')
            headers[name.strip().lower()] = value.strip()
    if 'transfer-encoding' in headers:
        raise ValueError('chunked bodies are not supported')
    length = int(headers.get('content-length') or 0)
    if not 0 <= length <= MAX_BODY:
        raise ValueError('body too large')
    body = await reader.readexactly(length) if length else b''
    return Request(method, target, headers, body, peer, reader, writer)


async def send_response(writer, response):
    headers = {
        'Content-Type': response.content_type,
        'Content-Length': str(len(response.body)),
        'Cache-Control': 'no-store',
        'Connection': 'close',
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'no-referrer',
        'Cross-Origin-Resource-Policy': 'same-origin',
    }
    headers.update(response.headers)
    head = 'HTTP/1.1 %d %s\r\n' % (response.status, REASONS.get(response.status, 'OK'))
    head += ''.join('%s: %s\r\n' % item for item in headers.items()) + '\r\n'
    writer.write(head.encode('latin-1') + response.body)
    await writer.drain()


async def serve(handler, host, port):
    """Starts the server; `handler(request)` returns a Response, or None if it took over."""

    async def connection(reader, writer):
        try:
            try:
                peer = ipaddress.ip_address(writer.get_extra_info('peername')[0])
            except (TypeError, ValueError, IndexError):
                return
            if not (peer.is_private or peer.is_loopback):
                return  # not from the local network: hang up
            try:
                request = await asyncio.wait_for(_read_request(reader, writer, peer), 15)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                return
            except (asyncio.LimitOverrunError, ValueError):
                await send_response(writer, Response(400, 'Bad request'))
                return
            try:
                response = await handler(request)
            except (ConnectionError, OSError):
                return
            except Exception:
                log.exception('Error while answering %s %s', request.method, request.path)
                response = Response(500, 'Internal error')
            if response is not None:
                await send_response(writer, response)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(connection, host, port, limit=MAX_HEAD)


# ------------------------------------------------------------------ WebSocket

class ProtocolError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


async def accept_websocket(request):
    """Completes the WebSocket handshake; returns None (after answering) if it is not one."""
    h = request.headers
    key = h.get('sec-websocket-key', '')
    if (h.get('upgrade', '').lower() != 'websocket' or 'upgrade' not in h.get('connection', '').lower()
            or h.get('sec-websocket-version') != '13' or len(key) != 24):
        await send_response(request.writer, Response(426, 'WebSocket only', headers={'Sec-WebSocket-Version': '13'}))
        return None
    accept = base64.b64encode(hashlib.sha1(key.encode('latin-1') + WS_GUID).digest()).decode()
    request.writer.write(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                          'Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n' % accept).encode())
    await request.writer.drain()
    return WebSocket(request.reader, request.writer)


class WebSocket:
    """Server side of a WebSocket connection that exchanges text messages."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.closed = False
        self._lock = asyncio.Lock()
        self._loop = asyncio.get_running_loop()
        self._last_seen = self._loop.time()

    async def _write(self, opcode, payload=b''):
        n = len(payload)
        if n < 126:
            header = struct.pack('!BB', 0x80 | opcode, n)
        elif n < 65536:
            header = struct.pack('!BBH', 0x80 | opcode, 126, n)
        else:
            header = struct.pack('!BBQ', 0x80 | opcode, 127, n)
        async with self._lock:
            self.writer.write(header + payload)
            await self.writer.drain()

    async def send(self, text):
        if not self.closed:
            await self._write(TEXT, text.encode('utf-8'))

    async def close(self, code=1000, reason=''):
        if self.closed:
            return
        self.closed = True
        try:
            payload = struct.pack('!H', code) + reason.encode('utf-8')[:120]
            await asyncio.wait_for(self._write(CLOSE, payload), 2)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            pass
        self.writer.close()

    async def _frame(self):
        b0, b1 = await self.reader.readexactly(2)
        fin, opcode, length = bool(b0 & 0x80), b0 & 0x0F, b1 & 0x7F
        if b0 & 0x70 or not b1 & 0x80:
            raise ProtocolError(1002)  # no extensions were agreed, and clients must mask
        if length == 126:
            length, = struct.unpack('!H', await self.reader.readexactly(2))
        elif length == 127:
            length, = struct.unpack('!Q', await self.reader.readexactly(8))
        if length > MAX_MESSAGE:
            raise ProtocolError(1009)
        if opcode >= CLOSE and (length > 125 or not fin):
            raise ProtocolError(1002)
        mask = await self.reader.readexactly(4)
        data = await self.reader.readexactly(length)
        if length:
            key = (mask * (length // 4 + 1))[:length]
            data = (int.from_bytes(data, 'big') ^ int.from_bytes(key, 'big')).to_bytes(length, 'big')
        self._last_seen = self._loop.time()
        return fin, opcode, data

    async def recv(self):
        """The next text message, or None once the connection is over."""
        parts, kind, size = [], None, 0
        try:
            while True:
                fin, opcode, data = await self._frame()
                if opcode == CLOSE:
                    await self.close()
                    return None
                if opcode == PING:
                    await self._write(PONG, data)
                    continue
                if opcode == PONG:
                    continue
                if opcode in (TEXT, BINARY):
                    if kind is not None:
                        raise ProtocolError(1002)
                    kind = opcode
                elif opcode != CONTINUATION or kind is None:
                    raise ProtocolError(1002)
                size += len(data)
                if size > MAX_MESSAGE:
                    raise ProtocolError(1009)
                parts.append(data)
                if fin:
                    message, was_text = b''.join(parts), kind == TEXT
                    parts, kind, size = [], None, 0
                    if was_text:
                        try:
                            return message.decode('utf-8')
                        except UnicodeDecodeError:
                            raise ProtocolError(1007)
        except ProtocolError as e:
            await self.close(e.code)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self.closed = True
        return None

    async def keepalive(self, interval=20, timeout=60):
        """Pings the phone and hangs up if it stops answering (screen off, out of Wi-Fi...)."""
        while not self.closed:
            await asyncio.sleep(interval)
            if self._loop.time() - self._last_seen > timeout:
                self.closed = True
                self.writer.close()
                return
            try:
                await self._write(PING)
            except (ConnectionError, OSError):
                return
