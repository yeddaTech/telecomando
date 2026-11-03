"""Linux one-time setup, done as administrator.

1. Lets the person sitting at the computer create virtual input devices
   (/dev/uinput), with the same "uaccess" rule desktops use for webcams and
   game controllers.
2. If a firewall (ufw or firewalld) is active, lets the local network reach
   Telecomando's port.
"""

import asyncio
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

PRIVATE_NETWORKS = '10.0.0.0/8 172.16.0.0/12 192.168.0.0/16'

SETUP = r'''
set -e
port="$1"; nets="$2"
printf '%s\n' 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' \
    > /etc/udev/rules.d/70-telecomando.rules
echo uinput > /etc/modules-load.d/telecomando.conf
modprobe uinput 2>/dev/null || true
udevadm control --reload-rules
udevadm trigger --action=change --sysname-match=uinput 2>/dev/null || true
udevadm settle 2>/dev/null || true
echo "OK: Telecomando may now create its virtual mouse and keyboard."
if command -v ufw >/dev/null 2>&1 && LC_ALL=C ufw status 2>/dev/null | grep -q '^Status: active'; then
    for net in $nets; do
        ufw allow from "$net" to any port "$port" proto tcp comment telecomando >/dev/null
    done
    echo "OK: firewall (ufw) open on port $port for the local network."
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    zone=$(firewall-cmd --get-default-zone)
    iface=$(ip -o -4 route show default 2>/dev/null | awk '{print $5; exit}')
    if [ -n "$iface" ] && z=$(firewall-cmd --get-zone-of-interface="$iface" 2>/dev/null); then
        zone=$z
    fi
    for net in $nets; do
        firewall-cmd --permanent --zone="$zone" \
            --add-rich-rule="rule family=ipv4 source address=$net port port=$port protocol=tcp accept" >/dev/null
    done
    firewall-cmd --reload >/dev/null
    echo "OK: firewall (firewalld, zone $zone) open on port $port for the local network."
fi
'''

REMOVE = r'''
port="$1"; nets="$2"
rm -f /etc/udev/rules.d/70-telecomando.rules /etc/modules-load.d/telecomando.conf
udevadm control --reload-rules 2>/dev/null || true
if command -v ufw >/dev/null 2>&1; then
    for net in $nets; do
        ufw delete allow from "$net" to any port "$port" proto tcp >/dev/null 2>&1 || true
    done
fi
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    for zone in $(firewall-cmd --get-zones); do
        for net in $nets; do
            firewall-cmd --permanent --zone="$zone" \
                --remove-rich-rule="rule family=ipv4 source address=$net port port=$port protocol=tcp accept" \
                >/dev/null 2>&1 || true
        done
    done
    firewall-cmd --reload >/dev/null 2>&1 || true
fi
echo "OK: Telecomando's system settings have been removed."
'''


def _argv(script, port):
    return ['/bin/sh', '-c', script, 'telecomando-setup', str(port), PRIVATE_NETWORKS]


def command_hint(port):
    """The command a person can type to do the setup of this copy of Telecomando."""
    if getattr(sys, 'frozen', False):
        parts = [sys.executable]
    else:
        parts = [sys.executable, str(Path(__file__).resolve().parent / '__main__.py')]
    parts.append('--setup')
    if port != 8765:
        parts += ['--port', str(port)]
    return ' '.join(shlex.quote(p) for p in parts)


def run(remove=False, port=8765):
    """From a terminal: asks for the password (sudo, or pkexec) and applies the setup."""
    argv = _argv(REMOVE if remove else SETUP, port)
    if os.geteuid() != 0:
        if not remove:
            print('Telecomando needs administrator rights once, to:\n'
                  '  - create a virtual mouse and keyboard (access to /dev/uinput);\n'
                  '  - let the local network reach port %d, if a firewall is active.\n' % port, flush=True)
        if sys.stdin.isatty() and shutil.which('sudo'):
            argv = ['sudo'] + argv
        elif shutil.which('pkexec'):
            argv = ['pkexec'] + argv
        else:
            print('Neither sudo nor pkexec is available: run this command as root.')
            return False
    try:
        return subprocess.run(argv).returncode == 0
    except OSError as e:
        print('Setup failed: %s' % e)
        return False


async def run_graphical(port):
    """From the control panel: the desktop asks for the password in a window (pkexec)."""
    if not shutil.which('pkexec'):
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            'pkexec', *_argv(SETUP, port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await process.wait()
    except OSError:
        return False
    return process.returncode == 0
