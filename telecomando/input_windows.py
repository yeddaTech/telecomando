"""Windows: mouse and keyboard input through the SendInput API.

Text is sent as Unicode characters, so it comes out right whatever keyboard
layout the PC uses. Windows ignores injected input aimed at programs running as
administrator, unless Telecomando is started as administrator too.
"""

import ctypes

from .control import KEYS, MODIFIERS

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0001, 0x0800, 0x1000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
MAPVK_VK_TO_VSC = 0
SPI_GETKEYBOARDSPEED, SPI_GETKEYBOARDDELAY = 0x000A, 0x0016

# Windows types have fixed sizes: LONG is 32 bits even on 64-bit Windows.
LONG, DWORD, WORD, ULONG_PTR = ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint16, ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', LONG), ('dy', LONG), ('mouseData', DWORD), ('dwFlags', DWORD),
                ('time', DWORD), ('dwExtraInfo', ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', WORD), ('wScan', WORD), ('dwFlags', DWORD), ('time', DWORD),
                ('dwExtraInfo', ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [('uMsg', DWORD), ('wParamL', WORD), ('wParamH', WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [('type', DWORD), ('u', INPUTUNION)]


BUTTONS = {'left': (0x0002, 0x0004), 'right': (0x0008, 0x0010), 'middle': (0x0020, 0x0040)}

# Virtual-key code and whether it is an "extended" key.
VK = {
    'esc': (0x1B, False), 'backspace': (0x08, False), 'tab': (0x09, False), 'enter': (0x0D, False),
    'space': (0x20, False), 'ctrl': (0xA2, False), 'shift': (0xA0, False), 'alt': (0xA4, False),
    'altgr': (0xA5, True), 'super': (0x5B, True), 'menu': (0x5D, True), 'print': (0x2C, True),
    'insert': (0x2D, True), 'delete': (0x2E, True), 'home': (0x24, True), 'end': (0x23, True),
    'pageup': (0x21, True), 'pagedown': (0x22, True),
    'up': (0x26, True), 'down': (0x28, True), 'left': (0x25, True), 'right': (0x27, True),
    'mute': (0xAD, True), 'voldown': (0xAE, True), 'volup': (0xAF, True),
    'next': (0xB0, True), 'prev': (0xB1, True), 'playpause': (0xB3, True),
    **{'f%d' % i: (0x6F + i, False) for i in range(1, 13)},
}
# Windows has no brightness keys: the phone hides those buttons.
assert set(VK) == set(KEYS) - {'brightdown', 'brightup'}
SHIFT_STATE = ((1, VK['shift']), (2, VK['ctrl']), (4, VK['alt']))  # bits returned by VkKeyScanEx


class WindowsInput:
    platform = 'windows'
    keys = frozenset(VK)
    layout = None  # not needed: text is typed as Unicode

    def __init__(self, log=None):
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        for name, restype, argtypes in [
            ('SendInput', ctypes.c_uint, [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]),
            ('MapVirtualKeyW', ctypes.c_uint, [ctypes.c_uint, ctypes.c_uint]),
            ('VkKeyScanExW', ctypes.c_short, [ctypes.c_wchar, ctypes.c_void_p]),
            ('GetForegroundWindow', ctypes.c_void_p, []),
            ('GetWindowThreadProcessId', DWORD, [ctypes.c_void_p, ctypes.c_void_p]),
            ('GetKeyboardLayout', ctypes.c_void_p, [DWORD]),
            ('LockWorkStation', ctypes.c_int, []),
            ('SystemParametersInfoW', ctypes.c_int, [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]),
        ]:
            function = getattr(user32, name)
            function.restype, function.argtypes = restype, argtypes
        self.user32 = user32
        self.log = log
        self.held = set()  # keys and buttons down right now, released on close
        self.repeat = self._repeat_settings()

    def _repeat_settings(self):
        """Delay and interval of key repeat, as set in the Windows keyboard settings."""
        delay, speed = ctypes.c_uint(1), ctypes.c_uint(31)
        self.user32.SystemParametersInfoW(SPI_GETKEYBOARDDELAY, 0, ctypes.byref(delay), 0)
        self.user32.SystemParametersInfoW(SPI_GETKEYBOARDSPEED, 0, ctypes.byref(speed), 0)
        return (min(delay.value, 3) + 1) * 0.25, 1 / (2.5 + min(speed.value, 31) * 27.5 / 31)

    def refresh(self):
        pass

    def close(self):
        for kind, value in list(self.held):
            if kind == 'button':
                self.button(value, False)
            else:
                self._send(self._vk(*value, up=True))
        self.held.clear()

    def _send(self, *inputs):
        """Returns how many events Windows accepted."""
        if not inputs:
            return 0
        array = (INPUT * len(inputs))(*inputs)
        return self.user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))

    @staticmethod
    def _mouse(flags, dx=0, dy=0, data=0):
        return INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)))

    def _vk(self, code, extended, up=False):
        flags = (KEYEVENTF_EXTENDEDKEY if extended else 0) | (KEYEVENTF_KEYUP if up else 0)
        scan = self.user32.MapVirtualKeyW(code, MAPVK_VK_TO_VSC) & 0xFFFF
        return INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(code, scan, flags, 0, 0)))

    @staticmethod
    def _unicode(unit, up=False):
        flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
        return INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(0, unit, flags, 0, 0)))

    # -- mouse

    def move(self, dx, dy):
        if dx or dy:
            self._send(self._mouse(MOUSEEVENTF_MOVE, dx, dy))

    def scroll(self, x, y):
        inputs = []
        if y:
            inputs.append(self._mouse(MOUSEEVENTF_WHEEL, data=y))
        if x:
            inputs.append(self._mouse(MOUSEEVENTF_HWHEEL, data=x))
        self._send(*inputs)

    def button(self, name, down):
        self._send(self._mouse(BUTTONS[name][0 if down else 1]))
        (self.held.add if down else self.held.discard)(('button', name))

    # -- keyboard

    def key(self, name, down):
        self._send(self._vk(*VK[name], up=not down))
        (self.held.add if down else self.held.discard)(('key', VK[name]))

    def tap(self, name):
        self._send(self._vk(*VK[name]), self._vk(*VK[name], up=True))

    def _scan(self, ch):
        """Key and modifiers that type `ch` with the layout of the window in front."""
        if ord(ch) > 0xFFFF:
            return None
        window = self.user32.GetForegroundWindow()
        thread = self.user32.GetWindowThreadProcessId(window, None) if window else 0
        result = self.user32.VkKeyScanExW(ch, self.user32.GetKeyboardLayout(thread))
        if result == -1 or (result >> 8) & 0xF8:
            if ch.isascii() and ch.isalnum():
                return ord(ch.upper()), 0  # VK_A..VK_Z and VK_0..VK_9: shortcuts on any layout
            return None
        return result & 0xFF, (result >> 8) & 0x07

    def _resolve(self, name):
        if name in VK:
            return [VK[name]]
        if len(name) == 1:
            found = self._scan(name)
            if found:
                code, state = found
                return [vk for bit, vk in SHIFT_STATE if state & bit] + [(code, False)]
        raise KeyError(name)

    def combo(self, names):
        sequence = []
        for name in names:
            sequence += [k for k in self._resolve(name) if k not in sequence]
        self._send(*[self._vk(*k) for k in sequence], *[self._vk(*k, up=True) for k in reversed(sequence)])

    def _modifier_held(self):
        return any(('key', VK[m]) in self.held for m in MODIFIERS)

    def type_char(self, ch):
        if ch == '\n':
            self.tap('enter')
        elif ch == '\t':
            self.tap('tab')
        elif self._modifier_held():
            # With Ctrl, Alt... held down a real key is needed: Ctrl + "c" as text is not Ctrl+C.
            try:
                keys = [k for k in self._resolve(ch) if ('key', k) not in self.held]
            except KeyError:
                return
            self._send(*[self._vk(*k) for k in keys], *[self._vk(*k, up=True) for k in reversed(keys)])
        else:
            data = ch.encode('utf-16-le')
            units = [int.from_bytes(data[i:i + 2], 'little') for i in range(0, len(data), 2)]
            self._send(*[i for u in units for i in (self._unicode(u), self._unicode(u, up=True))])

    def lock(self):
        self.user32.LockWorkStation()  # Win+L cannot be simulated, this is the official way
