#!/bin/sh
# Removes Telecomando installed with install.sh.

data="${XDG_DATA_HOME:-$HOME/.local/share}"
dest="$data/telecomando"
link="$HOME/.local/bin/telecomando"

if [ -x "$dest/telecomando" ]; then
    "$dest/telecomando" --stop >/dev/null 2>&1 || true
    "$dest/telecomando" --autostart off >/dev/null 2>&1 || true
    echo "Removing the system permission and firewall rule (asks for your password):"
    "$dest/telecomando" --remove-setup || true
fi
if [ -L "$link" ] && [ "$(readlink "$link")" = "$dest/telecomando" ]; then
    rm -f "$link"
fi
rm -f "$data/applications/telecomando.desktop" "$data/icons/hicolor/scalable/apps/telecomando.svg"
rm -rf "$dest"

echo "Telecomando has been removed."
echo "Its access code is in ${XDG_CONFIG_HOME:-$HOME/.config}/telecomando: delete that folder too if you want."
