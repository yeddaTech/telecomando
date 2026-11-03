"""Linux: a virtual mouse and keyboard created through /dev/uinput.

The system sees them as real hardware, so this works on Wayland as well as X11,
with any desktop. Text is typed through the keyboard layout in use, which is
read with libxkbcommon.
"""

import configparser
import ctypes
import fcntl
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

from .control import KEYS, NeedsSetup

EV_SYN, EV_KEY, EV_REL = 0x00, 0x01, 0x02
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0x00, 0x01, 0x06, 0x08
REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 0x0B, 0x0C
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT = 0x40045564, 0x40045565, 0x40045566
UI_DEV_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY = 0x405C5503, 0x5501, 0x5502
BUS_VIRTUAL = 0x06
EVENT = struct.Struct('llHHi')  # struct input_event

KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_LEFTALT, KEY_RIGHTALT = 29, 42, 56, 100
KEY_U, KEY_L, KEY_SPACE = 22, 38, 57
EXCLUDED = {116, 142, 143, 205}  # power, sleep, wake up, suspend: never exposed
BUTTONS = {'left': 0x110, 'right': 0x111, 'middle': 0x112}

KEYCODES = {
    'esc': 1, 'backspace': 14, 'tab': 15, 'enter': 28, 'space': KEY_SPACE,
    'ctrl': KEY_LEFTCTRL, 'shift': KEY_LEFTSHIFT, 'alt': KEY_LEFTALT, 'altgr': KEY_RIGHTALT,
    'super': 125, 'menu': 127, 'print': 99, 'insert': 110, 'delete': 111,
    'home': 102, 'end': 107, 'pageup': 104, 'pagedown': 109,
    'up': 103, 'down': 108, 'left': 105, 'right': 106,
    'mute': 113, 'voldown': 114, 'volup': 115,
    'next': 163, 'playpause': 164, 'prev': 165,
    'brightdown': 224, 'brightup': 225,
    'f11': 87, 'f12': 88, **{'f%d' % i: 58 + i for i in range(1, 11)},
}
assert set(KEYCODES) == set(KEYS)


class VirtualDevice:
    def __init__(self, name, keys=(), axes=()):
        self.fd = os.open('/dev/uinput', os.O_WRONLY | os.O_CLOEXEC)
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for code in keys:
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
            if axes:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
                for axis in axes:
                    fcntl.ioctl(self.fd, UI_SET_RELBIT, axis)
            fcntl.ioctl(self.fd, UI_DEV_SETUP,
                        struct.pack('HHHH80sI', BUS_VIRTUAL, 0, 0, 1, name.encode(), 0))
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except OSError:
            os.close(self.fd)
            raise

    def send(self, *events):
        """Writes the events (type, code, value) followed by a SYN_REPORT."""
        data = b''.join(EVENT.pack(0, 0, *e) for e in events)
        os.write(self.fd, data + EVENT.pack(0, 0, EV_SYN, 0, 0))

    def close(self):
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        finally:
            os.close(self.fd)


# ------------------------------------------------------------ keyboard layout

def _output(command):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def _gnome_layout():
    def get(key):
        return _output(['gsettings', 'get', 'org.gnome.desktop.input-sources', key])
    xkb = r"\('xkb', '([^']+)'\)"
    sources = re.findall(xkb, get('mru-sources')) or re.findall(xkb, get('sources'))
    if sources:
        layout, _, variant = sources[0].partition('+')
        return layout, variant, ','.join(re.findall(r"'([^']+)'", get('xkb-options')))
    return None


def _kde_layout():
    path = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'kxkbrc'
    config = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        config.read(path, encoding='utf-8')
    except (OSError, configparser.Error, UnicodeDecodeError):
        return None
    if config.get('Layout', 'Use', fallback='false').lower() != 'true':
        return None
    layouts = config.get('Layout', 'LayoutList', fallback='').split(',')
    variants = config.get('Layout', 'VariantList', fallback='').split(',')
    if layouts[0]:
        return layouts[0], variants[0], config.get('Layout', 'Options', fallback='')
    return None


def _parse(text, names):
    """Reads 'layout: it,us'-like lines; returns the first value of each name."""
    values = []
    for name in names:
        match = re.search(r'^\s*%s\s*[:=]\s*"?([^"\n]*)"?' % name, text, re.M | re.I)
        values.append(match.group(1).strip().split(',')[0].strip() if match else '')
    return values


def _x11_layout():
    if os.environ.get('XDG_SESSION_TYPE') != 'x11' or not shutil.which('setxkbmap'):
        return None
    layout, variant, options = _parse(_output(['setxkbmap', '-query']), ('layout', 'variant', 'options'))
    return (layout, variant, options) if layout else None


def _system_layout():
    layout, variant, options = _parse(_output(['localectl', 'status']),
                                      ('X11 Layout', 'X11 Variant', 'X11 Options'))
    if layout:
        return layout, variant, options
    for path in ('/etc/default/keyboard', '/etc/vconsole.conf'):
        try:
            text = Path(path).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        layout, variant, options = _parse(text, ('XKBLAYOUT', 'XKBVARIANT', 'XKBOPTIONS'))
        if layout:
            return layout, variant, options
    return None


def detect_layout(override=None):
    """(layout, variant, options) of the keyboard layout the desktop is using."""
    if override:
        layout, _, variant = override.partition(':')  # e.g. "de" or "de:nodeadkeys"
        return layout, variant, ''
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').upper()
    readers = [_kde_layout, _gnome_layout] if 'KDE' in desktop else [_gnome_layout, _kde_layout]
    for read in readers + [_x11_layout, _system_layout]:
        found = read()
        if found:
            return found
    return 'us', '', ''


def compile_keymap(layout, variant, options):
    """Uses libxkbcommon to find which key (plus Shift/AltGr) types each character."""
    c = ctypes
    xkb = c.CDLL('libxkbcommon.so.0')

    class RuleNames(c.Structure):
        _fields_ = [(n, c.c_char_p) for n in ('rules', 'model', 'layout', 'variant', 'options')]

    for name, restype, argtypes in [
        ('xkb_context_new', c.c_void_p, [c.c_int]),
        ('xkb_context_unref', None, [c.c_void_p]),
        ('xkb_keymap_new_from_names', c.c_void_p, [c.c_void_p, c.POINTER(RuleNames), c.c_int]),
        ('xkb_keymap_unref', None, [c.c_void_p]),
        ('xkb_keymap_min_keycode', c.c_uint32, [c.c_void_p]),
        ('xkb_keymap_max_keycode', c.c_uint32, [c.c_void_p]),
        ('xkb_keymap_num_levels_for_key', c.c_uint32, [c.c_void_p, c.c_uint32, c.c_uint32]),
        ('xkb_keymap_key_get_syms_by_level', c.c_int,
         [c.c_void_p, c.c_uint32, c.c_uint32, c.c_uint32, c.POINTER(c.POINTER(c.c_uint32))]),
        ('xkb_keymap_key_get_mods_for_level', c.c_size_t,
         [c.c_void_p, c.c_uint32, c.c_uint32, c.c_uint32, c.POINTER(c.c_uint32), c.c_size_t]),
        ('xkb_keymap_mod_get_index', c.c_uint32, [c.c_void_p, c.c_char_p]),
        ('xkb_keysym_to_utf32', c.c_uint32, [c.c_uint32]),
    ]:
        function = getattr(xkb, name)
        function.restype, function.argtypes = restype, argtypes

    context = xkb.xkb_context_new(0)
    if not context:
        raise OSError('libxkbcommon: no context')
    names = RuleNames(b'evdev', b'pc105+inet', layout.encode(), variant.encode(), options.encode())
    keymap = xkb.xkb_keymap_new_from_names(context, c.byref(names), 0)
    if not keymap:
        xkb.xkb_context_unref(context)
        raise ValueError('unknown layout')
    try:
        shift = 1 << xkb.xkb_keymap_mod_get_index(keymap, b'Shift')
        altgr = 1 << xkb.xkb_keymap_mod_get_index(keymap, b'Mod5')
        syms, masks = c.POINTER(c.c_uint32)(), (c.c_uint32 * 8)()
        altgr_key, chars = None, {}

        for keycode in range(xkb.xkb_keymap_min_keycode(keymap), xkb.xkb_keymap_max_keycode(keymap) + 1):
            code = keycode - 8  # XKB keycodes are the evdev ones plus 8
            if not 0 < code < 256 or code in EXCLUDED:
                continue
            for level in range(xkb.xkb_keymap_num_levels_for_key(keymap, keycode, 0)):
                if xkb.xkb_keymap_key_get_syms_by_level(keymap, keycode, 0, level, c.byref(syms)) != 1:
                    continue
                sym = syms[0]
                if sym == 0xFE03 and (altgr_key is None or code == KEY_RIGHTALT):
                    altgr_key = code  # ISO_Level3_Shift
                if 0xFF80 <= sym <= 0xFFBD:
                    continue  # keypad: depends on Num Lock
                cp = xkb.xkb_keysym_to_utf32(sym)
                if cp < 0x20 or cp == 0x7F:
                    continue
                n = xkb.xkb_keymap_key_get_mods_for_level(keymap, keycode, 0, level, masks, 8)
                usable = [m for m in masks[:n] if not m & ~(shift | altgr)]
                if not usable:
                    continue
                mask = min(usable, key=lambda m: bin(m).count('1'))
                mods = ((KEY_LEFTSHIFT,) if mask & shift else ()) + (('ALTGR',) if mask & altgr else ())
                ch = chr(cp)
                if ch not in chars or len(mods) < len(chars[ch][1]):
                    chars[ch] = (code, mods)
    finally:
        xkb.xkb_keymap_unref(keymap)
        xkb.xkb_context_unref(context)

    altgr_key = altgr_key or KEY_RIGHTALT
    return {ch: (code, tuple(altgr_key if m == 'ALTGR' else m for m in mods))
            for ch, (code, mods) in chars.items()}


def qwerty_keymap():
    """Letters and digits on a US keyboard: the fallback, and the keys of shortcuts like Ctrl+C."""
    keymap = {' ': (KEY_SPACE, ())}
    for row, first in (('1234567890', 2), ('qwertyuiop', 16), ('asdfghjkl', 30), ('zxcvbnm', 44)):
        for i, ch in enumerate(row):
            keymap[ch] = (first + i, ())
            if ch.isalpha():
                keymap[ch.upper()] = (first + i, (KEY_LEFTSHIFT,))
    return keymap


QWERTY = qwerty_keymap()


# ------------------------------------------------------------------ backend

class LinuxInput:
    platform = 'linux'
    keys = frozenset(KEYCODES)
    repeat = None  # the desktop repeats held keys by itself

    def __init__(self, layout=None, log=None):
        if not os.access('/dev/uinput', os.W_OK):
            raise NeedsSetup()
        try:
            self.mouse = VirtualDevice(
                'Telecomando mouse', BUTTONS.values(),
                (REL_X, REL_Y, REL_WHEEL, REL_HWHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES))
        except PermissionError:
            raise NeedsSetup()
        try:
            self.keyboard = VirtualDevice(
                'Telecomando keyboard', [c for c in range(1, 256) if c not in EXCLUDED])
        except OSError:
            self.mouse.close()
            raise
        self.log = log
        self.override = layout
        self.chars, self.layout, self._current = {}, '', None
        self.wheel_rest = {REL_WHEEL: 0, REL_HWHEEL: 0}
        desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').upper()
        modules = ' '.join(os.environ.get(v, '') for v in ('GTK_IM_MODULE', 'QT_IM_MODULE', 'XMODIFIERS'))
        # Ctrl+Shift+U followed by the code point types any character in GNOME/IBus.
        self.unicode_entry = 'GNOME' in desktop or 'ibus' in modules.lower()
        self.refresh()

    def refresh(self):
        """Reads the keyboard layout again, in case the user changed it."""
        current = detect_layout(self.override)
        if current == self._current:
            return
        self._current = current
        layout, variant, _ = current
        self.layout = layout + (' (%s)' % variant if variant else '')
        try:
            self.chars = compile_keymap(*current)
        except (OSError, AttributeError, ValueError) as e:
            if self.log:
                self.log.warning('Cannot read the keyboard layout "%s" (%s): typing letters and digits only.',
                                 self.layout, e)
            self.chars = dict(QWERTY)

    def close(self):
        self.mouse.close()
        self.keyboard.close()

    # -- mouse

    def move(self, dx, dy):
        events = [(EV_REL, axis, v) for axis, v in ((REL_X, dx), (REL_Y, dy)) if v]
        if events:
            self.mouse.send(*events)

    def scroll(self, x, y):
        events = []
        for hi_res, classic, v in ((REL_WHEEL_HI_RES, REL_WHEEL, y), (REL_HWHEEL_HI_RES, REL_HWHEEL, x)):
            if not v:
                continue
            events.append((EV_REL, hi_res, v))
            self.wheel_rest[classic] += v
            notches = int(self.wheel_rest[classic] / 120)
            if notches:
                self.wheel_rest[classic] -= notches * 120
                events.append((EV_REL, classic, notches))
        if events:
            self.mouse.send(*events)

    def button(self, name, down):
        self.mouse.send((EV_KEY, BUTTONS[name], int(down)))

    # -- keyboard

    def _key(self, code, down):
        self.keyboard.send((EV_KEY, code, int(down)))

    def _stroke(self, code, mods=()):
        for m in mods:
            self._key(m, True)
        self._key(code, True)
        self._key(code, False)
        for m in reversed(mods):
            self._key(m, False)

    def key(self, name, down):
        self._key(KEYCODES[name], down)

    def tap(self, name):
        self._stroke(KEYCODES[name])

    def _resolve(self, name):
        if name in KEYCODES:
            return KEYCODES[name], ()
        if len(name) == 1:
            found = self.chars.get(name) or self.chars.get(name.lower())
            if found:
                return found
            return QWERTY[name.lower()]  # e.g. Ctrl+C with a Cyrillic layout
        raise KeyError(name)

    def combo(self, names):
        sequence = []
        for name in names:
            code, mods = self._resolve(name)
            sequence += [c for c in (*mods, code) if c not in sequence]
        for code in sequence:
            self._key(code, True)
        for code in reversed(sequence):
            self._key(code, False)

    def type_char(self, ch):
        if ch == '\n':
            self.tap('enter')
        elif ch == '\t':
            self.tap('tab')
        elif ch in self.chars:
            self._stroke(*self.chars[ch])
        elif self.unicode_entry:
            # Not on the keyboard (an emoji, for example): Ctrl+Shift+U, hex code, space.
            digits = [self.chars.get(d) for d in '%x' % ord(ch)]
            if None not in digits:
                self._stroke(KEY_U, (KEY_LEFTCTRL, KEY_LEFTSHIFT))
                for digit in digits:
                    self._stroke(*digit)
                self._stroke(KEY_SPACE)

    def lock(self):
        session = os.environ.get('XDG_SESSION_ID') or _output(
            ['loginctl', 'show-user', str(os.getuid()), '-p', 'Display', '--value']).strip()
        if session:
            try:
                if subprocess.run(['loginctl', 'lock-session', session],
                                  capture_output=True, timeout=3).returncode == 0:
                    return
            except (OSError, subprocess.SubprocessError):
                pass
        self._stroke(KEY_L, (125,))  # Super+L, the usual shortcut
