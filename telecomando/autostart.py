"""Starts Telecomando automatically when the user logs in.

Windows: a value in HKEY_CURRENT_USER\\...\\CurrentVersion\\Run.
Linux: a desktop entry in ~/.config/autostart (works with every desktop).
"""

import os
import re
import subprocess
import sys
from pathlib import Path

NAME = 'Telecomando'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'


def supported():
    return os.name == 'nt' or sys.platform.startswith('linux')


def command(extra_args=()):
    """The command line that starts this same copy of Telecomando, without opening anything."""
    if getattr(sys, 'frozen', False):
        argv = [sys.executable]
    else:
        python = sys.executable
        if os.name == 'nt':
            pythonw = Path(python).with_name('pythonw.exe')  # no console window
            if pythonw.exists():
                python = str(pythonw)
        argv = [python, str(Path(__file__).resolve().parent / '__main__.py')]
    return argv + ['--background'] + list(extra_args)


# ------------------------------------------------------------------ Linux

def _desktop_file():
    base = os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config'
    return Path(base) / 'autostart' / 'telecomando.desktop'


def _desktop_quote(arg):
    """Quotes one argument for the Exec key of a desktop entry."""
    if re.fullmatch(r'[\w/.,:=+@-]+', arg):
        quoted = arg
    else:
        quoted = '"' + re.sub(r'([\\"`$])', r'\\\1', arg) + '"'
    return quoted.replace('\\', '\\\\').replace('%', '%%')


def _desktop_entry(argv):
    return '\n'.join([
        '[Desktop Entry]',
        'Type=Application',
        'Name=Telecomando',
        'Comment=Use your phone as the touchpad and keyboard of this PC',
        'Comment[it]=Usa il telefono come touchpad e tastiera di questo PC',
        'Exec=' + ' '.join(_desktop_quote(a) for a in argv),
        'Icon=telecomando',
        'Terminal=false',
        'X-GNOME-Autostart-enabled=true',
        '',
    ])


# ---------------------------------------------------------------- Windows

def _registry(write=False):
    import winreg
    access = winreg.KEY_SET_VALUE if write else winreg.KEY_QUERY_VALUE
    return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


# ------------------------------------------------------------------ common

def is_enabled():
    if os.name == 'nt':
        import winreg
        try:
            with _registry() as key:
                winreg.QueryValueEx(key, NAME)
            return True
        except OSError:
            return False
    try:
        text = _desktop_file().read_text(encoding='utf-8')
    except OSError:
        return False
    return not re.search(r'^Hidden\s*=\s*true', text, re.M | re.I)


def enable(extra_args=()):
    argv = command(extra_args)
    if os.name == 'nt':
        import winreg
        with _registry(write=True) as key:
            winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(argv))
        return
    path = _desktop_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_desktop_entry(argv), encoding='utf-8')


def disable():
    if os.name == 'nt':
        import winreg
        try:
            with _registry(write=True) as key:
                winreg.DeleteValue(key, NAME)
        except FileNotFoundError:
            pass
        return
    try:
        _desktop_file().unlink()
    except FileNotFoundError:
        pass
