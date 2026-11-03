#!/bin/sh
# Installs Telecomando for the current user, in ~/.local.
# Root is only needed for the one-time permission setup, which asks for your password.
set -e

here=$(cd "$(dirname "$0")" && pwd)
if [ -z "$HOME" ] || [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, without sudo:  ./install.sh" >&2
    exit 1
fi
if [ ! -x "$here/app/telecomando" ]; then
    echo "app/telecomando is missing: extract the whole archive, then run ./install.sh from its folder." >&2
    exit 1
fi

data="${XDG_DATA_HOME:-$HOME/.local/share}"
dest="$data/telecomando"
bin="$HOME/.local/bin"

# An older copy that is running keeps its files busy.
"$here/app/telecomando" --stop >/dev/null 2>&1 || true

echo "Installing Telecomando in $dest"
rm -rf "$dest"
mkdir -p "$dest" "$bin" "$data/applications" "$data/icons/hicolor/scalable/apps"
cp -R "$here/app/." "$dest/"
cp "$here/uninstall.sh" "$dest/uninstall.sh"
chmod 755 "$dest/uninstall.sh"
ln -sf "$dest/telecomando" "$bin/telecomando"
cp "$here/telecomando.svg" "$data/icons/hicolor/scalable/apps/telecomando.svg"

cat > "$data/applications/telecomando.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Telecomando
GenericName=Phone remote control
GenericName[it]=Telecomando dal telefono
Comment=Use your phone as the touchpad and keyboard of this PC
Comment[it]=Usa il telefono come touchpad e tastiera di questo PC
Exec="$dest/telecomando"
Icon=telecomando
Terminal=false
Categories=Utility;
Keywords=remote;mouse;keyboard;touchpad;phone;
StartupNotify=false
EOF

echo
"$dest/telecomando" --setup || echo "Permission setup skipped: Telecomando will offer it again when it starts."

echo
echo "Done. Telecomando is starting: its page with the QR code opens in your browser."
echo "From now on it starts by itself every time you log in."
if command -v setsid >/dev/null 2>&1; then
    setsid "$dest/telecomando" </dev/null >/dev/null 2>&1 &
else
    nohup "$dest/telecomando" </dev/null >/dev/null 2>&1 &
fi
