"""Dry run: logs what would happen instead of moving the real mouse (for testing)."""

import os

from .control import KEYS


class DryInput:
    platform = 'windows' if os.name == 'nt' else 'linux'
    keys = frozenset(KEYS)
    layout = 'dry run'
    repeat = None

    def __init__(self, log=None):
        self.log = log
        self.events = []

    def _record(self, *event):
        self.events.append(event)
        if self.log and event[0] not in ('move', 'scroll'):
            self.log.info('[dry run] %s', ' '.join(map(str, event)))

    def refresh(self):
        pass

    def close(self):
        pass

    def move(self, dx, dy):
        self._record('move', dx, dy)

    def scroll(self, x, y):
        self._record('scroll', x, y)

    def button(self, name, down):
        self._record('button', name, 'down' if down else 'up')

    def key(self, name, down):
        self._record('key', name, 'down' if down else 'up')

    def tap(self, name):
        self._record('tap', name)

    def combo(self, names):
        self._record('combo', '+'.join(names))

    def type_char(self, ch):
        self._record('char', repr(ch))

    def lock(self):
        self._record('lock')
