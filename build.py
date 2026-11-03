#!/usr/bin/env python3
"""Builds the downloadable archive for the system this runs on, with PyInstaller.

    python -m pip install pyinstaller
    python build.py

Windows: dist/Telecomando-<version>-windows-<arch>.zip  (Telecomando.exe, no Python needed)
Linux:   dist/Telecomando-<version>-linux-<arch>.tar.gz (with install.sh)
"""

import io
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from telecomando import __version__  # noqa: E402

ARCH = {'amd64': 'x64', 'x86_64': 'x64', 'arm64': 'arm64', 'aarch64': 'arm64'}
PACKAGING = ROOT / 'packaging'


def pyinstaller(name, *options):
    work = ROOT / 'build'
    shutil.rmtree(work, ignore_errors=True)
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--name', name,
        '--distpath', str(work / 'dist'), '--workpath', str(work / 'work'), '--specpath', str(work),
        '--paths', str(ROOT),
        '--add-data', '%s%stelecomando/web' % (ROOT / 'telecomando' / 'web', os.pathsep),
        *options, str(ROOT / 'telecomando' / '__main__.py'),
    ], check=True)
    return work / 'dist' / name


def windows(out, arch):
    app = pyinstaller('Telecomando', '--windowed', '--icon', str(PACKAGING / 'telecomando.ico'),
                      '--hidden-import', 'telecomando.input_windows')
    archive = out / ('Telecomando-%s-windows-%s.zip' % (__version__, arch))
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for path in sorted(app.rglob('*')):
            z.write(path, 'Telecomando/' + path.relative_to(app).as_posix())
        for source, name in ((PACKAGING / 'windows' / 'README.txt', 'README.txt'), (ROOT / 'LICENSE', 'LICENSE.txt')):
            text = source.read_text(encoding='utf-8').replace('\r\n', '\n').replace('\n', '\r\n')
            z.writestr('Telecomando/' + name, text.encode('utf-8'))
    return archive


def linux(out, arch):
    app = pyinstaller('telecomando', '--hidden-import', 'telecomando.input_linux')
    top = 'Telecomando-%s-linux-%s' % (__version__, arch)
    archive = out / (top + '.tar.gz')

    def clean(info):  # no local user names or odd permissions inside the archive
        info.uid = info.gid = 0
        info.uname = info.gname = 'root'
        if info.isfile():
            info.mode = 0o755 if info.mode & 0o111 else 0o644
        elif info.isdir():
            info.mode = 0o755
        return info

    with tarfile.open(archive, 'w:gz') as t:
        t.add(str(app), top + '/app', filter=clean)
        for source, name in ((PACKAGING / 'linux' / 'install.sh', 'install.sh'),
                             (PACKAGING / 'linux' / 'uninstall.sh', 'uninstall.sh'),
                             (PACKAGING / 'linux' / 'README.txt', 'README.txt'),
                             (PACKAGING / 'telecomando.svg', 'telecomando.svg'),
                             (ROOT / 'LICENSE', 'LICENSE.txt')):
            data = source.read_bytes()
            info = clean(tarfile.TarInfo(top + '/' + name))
            info.size = len(data)
            info.mode = 0o755 if name.endswith('.sh') else 0o644
            info.mtime = int(source.stat().st_mtime)
            t.addfile(info, io.BytesIO(data))
    return archive


def main():
    machine = platform.machine().lower()
    arch = ARCH.get(machine, machine)
    out = ROOT / 'dist'
    out.mkdir(exist_ok=True)
    if sys.platform == 'win32':
        archive = windows(out, arch)
    elif sys.platform.startswith('linux'):
        archive = linux(out, arch)
    else:
        sys.exit('Run this on Windows or Linux.')
    print('\nBuilt %s' % archive)


if __name__ == '__main__':
    main()
