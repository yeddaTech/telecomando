"""Telecomando: use your phone as the touchpad and keyboard of this PC.

Starts a small web server that only the local network can reach. The phone
opens its page, sends touches and keys over a WebSocket, and they become a
virtual mouse and keyboard on the PC. Nothing goes through the internet.
"""

import argparse
import asyncio
import errno
import hmac
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__, autostart, linux_setup, qr
from .control import Control, NeedsSetup, Session
from .server import Response, accept_websocket, json_response, serve

DEFAULT_PORT = 8765
WEB = Path(__file__).resolve().parent / 'web'
PLATFORM = 'windows' if os.name == 'nt' else 'linux'
FILE_ONLY = {'file_only': True}
log = logging.getLogger('telecomando')

HTML = 'text/html; charset=utf-8'
STATIC = {
    '/': ('phone.html', HTML),
    '/manifest.webmanifest': ('manifest.webmanifest', 'application/manifest+json'),
    '/icon.svg': ('icon.svg', 'image/svg+xml'),
    '/icon-192.png': ('icon-192.png', 'image/png'),
    '/icon-512.png': ('icon-512.png', 'image/png'),
}
PAGE_HEADERS = {
    'Content-Security-Policy': "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self' ws:; base-uri 'none'; "
                               "form-action 'none'; frame-ancestors 'none'",
}


class Unsupported(Exception):
    pass


# --------------------------------------------------------------- utilities

def config_dir():
    if os.name == 'nt':
        return Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming') / 'Telecomando'
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'telecomando'


def save_token(directory, token):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'token'
    path.touch(mode=0o600)
    path.write_text(token + '\n', encoding='ascii')


def load_token(directory, renew=False):
    """The access code phones need; returns (code, True on the very first start)."""
    try:
        token = (directory / 'token').read_text(encoding='ascii').strip()
    except (OSError, UnicodeError):
        token = ''
    first_start = len(token) < 16
    if first_start or renew:
        token = secrets.token_urlsafe(12)
        save_token(directory, token)
    return token, first_start


def lan_ip():
    """The address of this PC on the local network."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(('192.0.2.1', 9))  # sends nothing: it only picks the network interface
            return s.getsockname()[0]
        except OSError:
            return '127.0.0.1'


def local_call(port, path, body=None):
    """Talks to a Telecomando already running on this PC; None if there is none."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path), data=data,
                                     headers={'X-Telecomando': '1', 'Content-Type': 'application/json'})
    try:
        with opener.open(request, timeout=3) as response:
            answer = json.loads(response.read().decode('utf-8'))
    except (OSError, ValueError):
        return None
    return answer if isinstance(answer, dict) and answer.get('app') == 'telecomando' else None


def open_panel(port):
    if os.name != 'nt' and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        return False  # no desktop here: do not start a text browser in the terminal
    try:
        return webbrowser.open('http://127.0.0.1:%d/pc' % port)
    except Exception:
        return False


def prepare_console():
    """Windows console: colours for the QR code, and no freeze when someone clicks in it."""
    if os.name != 'nt':
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        mode = ctypes.c_uint32()
        stdin = kernel32.GetStdHandle(-10)
        if kernel32.GetConsoleMode(stdin, ctypes.byref(mode)):
            kernel32.SetConsoleMode(stdin, (mode.value & ~0x0040) | 0x0080)  # QuickEdit off
        stdout = kernel32.GetStdHandle(-11)
        if not kernel32.GetConsoleMode(stdout, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(stdout, mode.value | 0x0004))  # ANSI sequences on
    except Exception:
        return False


def notify(message):
    """Shows an error to someone who started Telecomando from the desktop, not a terminal."""
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, 'Telecomando', 0x10)
        except Exception:
            pass
    elif shutil.which('notify-send'):
        try:
            subprocess.run(['notify-send', '-a', 'Telecomando', 'Telecomando', message], timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


def fatal(message):
    print(message, file=sys.stderr)
    if log.handlers:
        log.error(message, extra=FILE_ONLY)
    if not sys.stderr.isatty():
        notify(message)
    return 1


def set_autostart(args, enabled):
    """Turns the start at login on or off, repeating the options given now."""
    extra = []
    if args.port != DEFAULT_PORT:
        extra += ['--port', str(args.port)]
    if args.layout:
        extra += ['--layout', args.layout]
    if args.name:
        extra += ['--name', args.name]
    try:
        if enabled:
            autostart.enable(extra)
        else:
            autostart.disable()
    except OSError as e:
        log.warning('Cannot change the automatic start: %s', e)


def keep_autostart(args, first_start):
    """The downloaded app starts at every login, unless the user turns that off on the PC page."""
    if not getattr(sys, 'frozen', False) or args.dry_run:
        return  # started from the source code: the user decides
    if first_start:
        set_autostart(args, True)
    elif not args.background and autostart.is_enabled():
        set_autostart(args, True)  # points it at this copy, in case the folder was moved


def create_backend(args):
    if args.dry_run:
        from .input_dry import DryInput
        return DryInput(log)
    if os.name == 'nt':
        from .input_windows import WindowsInput
        return WindowsInput(log)
    if sys.platform.startswith('linux'):
        from .input_linux import LinuxInput
        return LinuxInput(args.layout, log)
    raise Unsupported()


# ---------------------------------------------------------------------- app

class App:
    def __init__(self, args, token, directory):
        self.args, self.port, self.token, self.directory = args, args.port, token, directory
        self.name = args.name or socket.gethostname()
        self.backend = self.control = None
        self.problem, self.detail = None, ''
        self.phones = {}  # WebSocket -> address of the phone
        self.stopping = None
        self.setup_running = False
        self._qr = (None, None)

    # -- mouse and keyboard

    def start_backend(self):
        try:
            self.backend = create_backend(self.args)
        except NeedsSetup:
            self.problem = 'setup'
            return False
        except Unsupported:
            self.problem = 'unsupported'
            return False
        except Exception as e:
            log.exception('Cannot create the virtual mouse and keyboard')
            self.problem, self.detail = 'error', str(e)
            return False
        self.problem, self.detail = None, ''
        self.control = Control(self.backend)
        return True

    async def wait_for_permission(self):
        """After the Linux setup (from a terminal or the panel) the devices become available."""
        while self.backend is None and self.problem == 'setup':
            await asyncio.sleep(2)
            if os.access('/dev/uinput', os.W_OK) and self.start_backend():
                log.info('Permission granted: the phone can now control this PC.')
                await self.broadcast_hello()

    def hello(self):
        backend = self.backend
        return {'t': 'hello', 'host': self.name, 'os': PLATFORM, 'ready': backend is not None,
                'layout': backend.layout if backend else None,
                'keys': sorted(backend.keys) if backend else []}

    async def broadcast_hello(self):
        message = json.dumps(self.hello())
        for ws in list(self.phones):
            try:
                await ws.send(message)
            except (ConnectionError, OSError):
                pass

    # -- web server

    def phone_url(self):
        return 'http://%s:%d/?k=%s' % (lan_ip(), self.port, self.token)

    async def handle(self, request):
        if request.path == '/ws':
            return await self.phone(request)
        if request.path == '/pc' or request.path.startswith('/pc/'):
            return await self.panel(request)
        if request.path in STATIC:
            if request.method != 'GET':
                return Response(405, 'Method not allowed')
            name, kind = STATIC[request.path]
            return Response(200, (WEB / name).read_bytes(), kind, PAGE_HEADERS if kind == HTML else None)
        return Response(404, 'Not found')

    async def phone(self, request):
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.headers.get('host'):
            return Response(403, 'Connections from other sites are not allowed.')
        ws = await accept_websocket(request)
        if ws is None:
            return None
        if not hmac.compare_digest(request.query.get('k', '').encode(), self.token.encode()):
            await ws.close(4401, 'invalid access code')
            return None

        address, session = str(request.peer), Session()
        self.phones[ws] = address
        log.info('Phone connected: %s', address)
        keepalive = asyncio.ensure_future(ws.keepalive())
        try:
            if self.backend:
                self.backend.refresh()
            await ws.send(json.dumps(self.hello()))
            while True:
                text = await ws.recv()
                if text is None:
                    break
                control = self.control
                if control is None:
                    continue
                try:
                    await control.handle(json.loads(text), session)
                except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                    pass  # a malformed message: ignore it
        except (ConnectionError, OSError):
            pass
        finally:
            keepalive.cancel()
            if self.control:
                self.control.release_all(session)
            self.phones.pop(ws, None)
            await ws.close()
            log.info('Phone disconnected: %s', address)
        return None

    def is_panel_request(self, request):
        host = request.headers.get('host', '')
        return request.peer.is_loopback and host in ('127.0.0.1:%d' % self.port, 'localhost:%d' % self.port)

    async def panel(self, request):
        if not self.is_panel_request(request):
            return Response(403, 'The control panel only opens on this PC.')
        path = request.path
        if request.method == 'GET':
            if path == '/pc':
                return Response(200, (WEB / 'pc.html').read_bytes(), HTML, PAGE_HEADERS)
            if path == '/pc/state':
                return json_response(self.state())
            return Response(404, 'Not found')
        if request.method != 'POST':
            return Response(405, 'Method not allowed')
        if request.headers.get('x-telecomando') != '1':
            return Response(403, 'Missing header')  # blocks forms and scripts of other sites
        try:
            body = request.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}

        if path == '/pc/stop':
            asyncio.get_running_loop().call_later(0.2, self.stopping.set)
            return json_response({'app': 'telecomando', 'stopping': True})
        if path == '/pc/new-code':
            await self.new_code()
        elif path == '/pc/autostart':
            set_autostart(self.args, bool(body.get('enabled')))
        elif path == '/pc/setup':
            await self.run_setup()
        else:
            return Response(404, 'Not found')
        return json_response(self.state())

    def state(self):
        url = self.phone_url()
        if self._qr[0] != url:
            try:
                self._qr = (url, qr.svg(url))
            except ValueError:
                self._qr = (url, None)
        return {
            'app': 'telecomando', 'version': __version__, 'os': PLATFORM, 'host': self.name,
            'port': self.port, 'url': url, 'qr': self._qr[1], 'phones': sorted(self.phones.values()),
            'ready': self.backend is not None, 'problem': self.problem, 'detail': self.detail,
            'setupRunning': self.setup_running, 'setupCommand': linux_setup.command_hint(self.port),
            'autostart': autostart.is_enabled() if autostart.supported() else None,
            'dryRun': self.args.dry_run,
        }

    # -- actions of the control panel

    async def new_code(self):
        self.token = secrets.token_urlsafe(12)
        save_token(self.directory, self.token)
        for ws in list(self.phones):
            await ws.close(4401, 'new access code')
        log.info('New access code: phones have to scan the new QR code.')

    async def run_setup(self):
        if PLATFORM != 'linux' or self.setup_running or self.backend is not None:
            return
        self.setup_running = True
        try:
            done = await linux_setup.run_graphical(self.port)
        finally:
            self.setup_running = False
        if done and self.backend is None and self.start_backend():
            log.info('Permission granted: the phone can now control this PC.')
            await self.broadcast_hello()

    # -- life cycle

    def announce(self, first_run):
        url = self.phone_url()
        log.info('Telecomando %s started, phone address: %s', __version__, url, extra=FILE_ONLY)
        terminal = sys.stdout.isatty()
        if terminal:
            print('\nTelecomando %s is running.\n\n'
                  'On your phone, connected to the same Wi-Fi as this PC, scan the code or open:\n\n'
                  '    %s\n' % (__version__, url))
            try:
                if prepare_console():
                    print(qr.terminal(url) + '\n')
            except (UnicodeError, ValueError, OSError):
                pass
            print('Control panel on this PC:  http://127.0.0.1:%d/pc' % self.port)
            if self.problem == 'setup':
                print('\nOne more step, needed only once, so the phone can move the mouse:\n'
                      '    %s' % linux_setup.command_hint(self.port))
            elif self.problem:
                print('\nThis PC cannot be controlled yet (%s). See the control panel.'
                      % (self.detail or self.problem))
            print('\nPress Ctrl+C to stop.\n', flush=True)
        if not (self.args.background or self.args.no_browser) and (first_run or not terminal):
            asyncio.get_running_loop().run_in_executor(None, open_panel, self.port)

    async def run(self, first_run):
        self.stopping = asyncio.Event()
        self.start_backend()
        server = await serve(self.handle, '0.0.0.0', self.port)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stopping.set)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # Windows: Ctrl+C still works through KeyboardInterrupt
        waiter = asyncio.ensure_future(self.wait_for_permission()) if self.problem == 'setup' else None
        self.announce(first_run)
        try:
            await self.stopping.wait()
        finally:
            log.info('Stopping Telecomando.')
            if waiter:
                waiter.cancel()
            server.close()
            for ws in list(self.phones):
                await ws.close(1001, 'Telecomando stopped')
            await asyncio.sleep(0.1)  # lets each phone release what it was holding
            try:
                await asyncio.wait_for(server.wait_closed(), 3)
            except asyncio.TimeoutError:
                pass
            if self.backend:
                self.backend.close()


# --------------------------------------------------------------------- main

def setup_logging(directory):
    log.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter('%(message)s'))
    console.addFilter(lambda record: not getattr(record, 'file_only', False))
    log.addHandler(console)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        logfile = logging.FileHandler(str(directory / 'telecomando.log'), mode='w', encoding='utf-8')
    except OSError:
        return
    logfile.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.addHandler(logfile)


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog='telecomando',
        description='Use your phone as the touchpad and keyboard of this PC, over your own Wi-Fi.')
    p.add_argument('--port', type=int, default=DEFAULT_PORT, help='network port (default: %(default)s)')
    p.add_argument('--name', help='name of this PC shown on the phone (default: the computer name)')
    p.add_argument('--layout', help='Linux: keyboard layout to type with, e.g. "de" or "fr:azerty" '
                                    '(default: the one the desktop is using)')
    p.add_argument('--new-code', action='store_true',
                   help='create a new access code (paired phones will have to scan the QR code again)')
    p.add_argument('--background', action='store_true', help='start without opening the control panel')
    p.add_argument('--no-browser', action='store_true', help='do not open the control panel in the browser')
    p.add_argument('--stop', action='store_true', help='stop the copy of Telecomando that is running')
    p.add_argument('--autostart', choices=('on', 'off'), help='start automatically at login, or stop doing it')
    p.add_argument('--setup', action='store_true',
                   help='Linux: one-time setup, asks for the administrator password')
    p.add_argument('--remove-setup', action='store_true', help='Linux: undo --setup')
    p.add_argument('--dry-run', action='store_true',
                   help='do not touch mouse and keyboard, only log what would happen')
    p.add_argument('--version', action='version', version='Telecomando ' + __version__)
    args = p.parse_args(argv)
    if not 1 <= args.port <= 65535:
        p.error('the port must be between 1 and 65535')
    return args


def main(argv=None):
    for stream in ('stdout', 'stderr'):
        if getattr(sys, stream) is None:  # Windows program without a console
            setattr(sys, stream, open(os.devnull, 'w'))
    args = parse_args(argv)

    if args.setup or args.remove_setup:
        if PLATFORM != 'linux':
            print('Nothing to set up on this system.')
            return 0
        return 0 if linux_setup.run(remove=args.remove_setup, port=args.port) else 1

    if args.autostart:
        set_autostart(args, args.autostart == 'on')
        print('Automatic start at login: %s.' % ('on' if autostart.is_enabled() else 'off'))
        return 0

    running = local_call(args.port, '/pc/state')
    if args.stop:
        stopped = running and local_call(args.port, '/pc/stop', {})
        if stopped:
            for _ in range(25):  # wait until it has really let go of the port
                time.sleep(0.2)
                if not local_call(args.port, '/pc/state'):
                    break
        print('Telecomando stopped.' if stopped else 'Telecomando is not running.')
        return 0
    if running:
        if args.new_code:
            local_call(args.port, '/pc/new-code', {})
        if not args.background:
            print('Telecomando is already running: opening its control panel.')
            open_panel(args.port)
        return 0

    if PLATFORM == 'linux' and os.geteuid() == 0:
        return fatal('Do not start Telecomando as administrator (root or sudo). For the one-time '
                     'setup, run without sudo:  %s' % linux_setup.command_hint(args.port))

    directory = config_dir()
    setup_logging(directory)
    try:
        token, first_start = load_token(directory, renew=args.new_code)
        keep_autostart(args, first_start)
        asyncio.run(App(args, token, directory).run(first_start))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, getattr(errno, 'WSAEADDRINUSE', 10048)):
            return fatal('Port %d is used by another program. Start Telecomando with another port, '
                         'for example:  --port %d' % (args.port, args.port + 1))
        log.exception('Telecomando stopped because of an error')
        return fatal('Telecomando stopped because of an error: %s' % e)
    except Exception as e:
        log.exception('Telecomando stopped because of an error')
        return fatal('Telecomando stopped because of an error: %s' % e)
    return 0
