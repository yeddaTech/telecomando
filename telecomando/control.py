"""Turns the messages sent by the phone page into mouse and keyboard actions.

Every backend (Linux, Windows, dry run) offers the same small set of methods:
move, scroll, button, key, tap, combo, type_char, lock, refresh and close.
"""

import asyncio

BUTTONS = ('left', 'right', 'middle')
MODIFIERS = ('ctrl', 'shift', 'alt', 'altgr', 'super')
KEYS = MODIFIERS + (
    'esc', 'backspace', 'tab', 'enter', 'space', 'menu', 'print', 'insert', 'delete',
    'home', 'end', 'pageup', 'pagedown', 'up', 'down', 'left', 'right',
    'mute', 'voldown', 'volup', 'next', 'playpause', 'prev', 'brightdown', 'brightup',
) + tuple('f%d' % i for i in range(1, 13))

MAX_TEXT = 5000
MAX_COMBO = 6


class NeedsSetup(Exception):
    """The system does not let Telecomando create its virtual mouse and keyboard yet."""


def number(value, limit=10000):
    """An integer between -limit and +limit, whatever the phone sent."""
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if value != value:  # NaN
        return 0
    return int(max(-limit, min(limit, value)))


class Session:
    """What one phone is holding down, so it can be released if the phone disappears."""

    def __init__(self):
        self.buttons = set()
        self.keys = {}  # key name -> task that repeats it, or None


class Control:
    def __init__(self, backend):
        self.backend = backend
        self.typing = asyncio.Lock()  # text from two phones never gets mixed up

    async def handle(self, msg, session):
        if not isinstance(msg, dict):
            return
        backend, kind = self.backend, msg.get('t')

        if kind == 'move':
            backend.move(number(msg.get('x')), number(msg.get('y')))

        elif kind == 'scroll':  # in 1/120 of a wheel notch, for smooth scrolling
            backend.scroll(number(msg.get('x')), number(msg.get('y')))

        elif kind == 'btn':
            name, action = msg.get('b'), msg.get('a', 'click')
            if name not in BUTTONS:
                return
            if action == 'down':
                backend.button(name, True)
                session.buttons.add(name)
            elif action == 'up':
                backend.button(name, False)
                session.buttons.discard(name)
            elif action in ('click', 'dbl'):
                for _ in range(2 if action == 'dbl' else 1):
                    backend.button(name, True)
                    await asyncio.sleep(0.01)
                    backend.button(name, False)
                    await asyncio.sleep(0.01)

        elif kind == 'key':
            name, action = msg.get('k'), msg.get('a', 'tap')
            if name not in backend.keys:
                return
            if action == 'down':
                self._press(name, session)
            elif action == 'up':
                self._release(name, session)
            elif action == 'tap':
                async with self.typing:
                    for _ in range(number(msg.get('n', 1), 500)):
                        backend.tap(name)
                        await asyncio.sleep(0.002)

        elif kind == 'combo':
            keys = msg.get('keys')
            if isinstance(keys, list) and keys:
                async with self.typing:
                    backend.combo([str(k) for k in keys[:MAX_COMBO]])

        elif kind == 'text':
            text = str(msg.get('s', ''))[:MAX_TEXT].replace('\r\n', '\n').replace('\r', '\n')
            async with self.typing:
                for ch in text:
                    backend.type_char(ch)
                    await asyncio.sleep(0.002)

        elif kind == 'lock':
            backend.lock()

    # -- keys held down from the phone

    def _press(self, name, session):
        if name in session.keys:
            return
        self.backend.key(name, True)
        task = None
        if self.backend.repeat and name not in MODIFIERS:
            # Windows only repeats keys held on a real keyboard, so we do it ourselves.
            task = asyncio.ensure_future(self._repeat(name))
        session.keys[name] = task

    async def _repeat(self, name):
        delay, interval = self.backend.repeat
        await asyncio.sleep(delay)
        while True:
            self.backend.key(name, True)
            await asyncio.sleep(interval)

    def _release(self, name, session):
        if name not in session.keys:
            return
        task = session.keys.pop(name)
        if task:
            task.cancel()
        self.backend.key(name, False)

    def release_all(self, session):
        """Lifts everything a phone was holding (for example if the connection drops)."""
        for name in list(session.keys):
            self._release(name, session)
        for name in session.buttons:
            self.backend.button(name, False)
        session.buttons.clear()
