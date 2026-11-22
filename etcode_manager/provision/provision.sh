#!/bin/bash
#
# provision.sh — runs inside the image via `virt-customize --run`.
#
# Installs the etcode payload that build_image.sh staged at /tmp/staging and
# applies the SETTINGS.md configuration (user `agent`, passwordless sudo,
# Xfce with a Windows-like taskbar, VSCodium) and installs the etcode desktop app
# (code in /opt/etcode/etcode_desktop, dependencies in /opt/etcode/.venv, launcher,
# autostart and panel entry).
#
set -euo pipefail

USERNAME="agent"
STAGING="/tmp/staging"
export DEBIAN_FRONTEND=noninteractive

log() { printf '\n==> %s\n' "$*"; }

# ---------------------------------------------------------------------------
# User account (SETTINGS.md: username `agent`, password: none)
# ---------------------------------------------------------------------------
log "creating user ${USERNAME}"
useradd -m -s /bin/bash "$USERNAME"
passwd -l "$USERNAME"

for group in sudo audio video cdrom plugdev netdev; do
    getent group "$group" >/dev/null && usermod -aG "$group" "$USERNAME" || true
done

# passwordless sudo
install -m 0440 "$STAGING/etc/sudoers.d/agent" /etc/sudoers.d/agent
visudo -cf /etc/sudoers.d/agent

# ---------------------------------------------------------------------------
# etcode payload: skills -> /home/agent/skills, scripts -> /home/agent/scripts,
# tools -> /opt/etcode (with a python venv at /opt/etcode/.venv)
# ---------------------------------------------------------------------------
log "installing skills, scripts and tools"
cp -a "$STAGING/home/agent/skills" /home/agent/skills
cp -a "$STAGING/home/agent/scripts" /home/agent/scripts
chmod +x /home/agent/scripts/*.sh

cp -a "$STAGING/opt/etcode" /opt/etcode
log "creating python venv /opt/etcode/.venv"
python3 -m venv /opt/etcode/.venv

# ---------------------------------------------------------------------------
# etcode desktop app (PySide6 GUI). The code was copied to /opt/etcode/etcode_desktop with
# the rest of /opt/etcode; the launcher runs it with PYTHONPATH, so only its dependencies
# are installed. PySide6-Essentials has everything the app imports (QtCore, QtGui,
# QtWidgets, QtNetwork) without the large Addons package.
# ---------------------------------------------------------------------------
log "installing the etcode desktop app dependencies into /opt/etcode/.venv"
/opt/etcode/.venv/bin/python -m pip install --no-cache-dir --disable-pip-version-check \
    "PySide6-Essentials>=6.5,<7" "requests>=2.31" "pyte>=0.8.2"

log "checking the etcode desktop app (virt-customize does not show warnings from --run scripts)"
qt_dir="$(/opt/etcode/.venv/bin/python -c 'import os, PySide6; print(os.path.dirname(PySide6.__file__))')/Qt"
missing_libs="$(LD_LIBRARY_PATH="$qt_dir/lib" ldd "$qt_dir/plugins/platforms/libqxcb.so" | grep 'not found' || true)"
if [ -n "$missing_libs" ]; then
    echo "ERROR: the Qt xcb platform plugin is missing system libraries:" >&2
    echo "$missing_libs" >&2
    exit 1
fi
QT_QPA_PLATFORM=offscreen PYTHONPATH=/opt/etcode/etcode_desktop /opt/etcode/.venv/bin/python -c '
from PySide6.QtWidgets import QApplication
app = QApplication([])
import PySide6.QtNetwork, requests, etcode_desktop.__main__
'

log "installing the etcode desktop launcher, menu entry, icon and autostart"
install -m 0755 "$STAGING/usr/local/bin/etcode-desktop" /usr/local/bin/etcode-desktop
install -m 0644 "$STAGING/usr/share/applications/etcode-desktop.desktop" \
    /usr/share/applications/etcode-desktop.desktop
install -D -m 0644 "$STAGING/usr/share/icons/hicolor/scalable/apps/etcode-desktop.svg" \
    /usr/share/icons/hicolor/scalable/apps/etcode-desktop.svg
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor || true
fi
install -D -m 0644 "$STAGING/etc/xdg/autostart/etcode-desktop.desktop" \
    /etc/xdg/autostart/etcode-desktop.desktop
# No /etc/etcode here and no token: cloud-init write_files creates /etc/etcode/desktop.json per VM (seed.py).

log "installing Xfce configuration"
cp -a "$STAGING/home/agent/.config" /home/agent/.config

chown -R "$USERNAME:$USERNAME" /home/agent /opt/etcode

# ---------------------------------------------------------------------------
# Desktop: autologin (the user has no password) + graphical boot
# ---------------------------------------------------------------------------
log "configuring Swiss German keyboard layout"
install -m 0644 "$STAGING/etc/default/keyboard" /etc/default/keyboard

log "configuring display manager"
install -d -m 0755 /etc/lightdm/lightdm.conf.d
install -m 0644 \
    "$STAGING/etc/lightdm/lightdm.conf.d/50-etcode-autologin.conf" \
    /etc/lightdm/lightdm.conf.d/50-etcode-autologin.conf

log "enabling services"
systemctl enable lightdm.service ssh.service
systemctl set-default graphical.target

# ---------------------------------------------------------------------------
# Screen: never lock or blank. light-locker locks idle sessions behind the
# LightDM greeter, which cannot be unlocked because the user has no password,
# and screen blanking breaks the remote display sessions the manager uses.
# ---------------------------------------------------------------------------
log "disabling screen locking and blanking"
for pkg in light-locker xfce4-screensaver xfce4-power-manager; do
    if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
        apt-get purge -y "$pkg"
    fi
done
install -m 0644 "$STAGING/etc/xdg/autostart/etcode-no-blank.desktop" \
    /etc/xdg/autostart/etcode-no-blank.desktop

# ---------------------------------------------------------------------------
# VSCodium (quick launch app; official apt repository)
# ---------------------------------------------------------------------------
log "installing VSCodium"
wget -qO - https://gitlab.com/paulcarroty/vscodium-deb-rpm-repo/raw/master/pub.gpg \
    | gpg --dearmor > /usr/share/keyrings/vscodium-archive-keyring.gpg
cat > /etc/apt/sources.list.d/vscodium.sources <<'EOF'
Types: deb
URIs: https://download.vscodium.com/debs
Suites: vscodium
Components: main
Architectures: amd64 arm64
Signed-By: /usr/share/keyrings/vscodium-archive-keyring.gpg
EOF
apt-get update
apt-get install -y codium

# ---------------------------------------------------------------------------
# Sanity checks for the panel quick-launch entries (fail loudly: virt-customize
# does not show warnings from --run scripts)
# ---------------------------------------------------------------------------
missing=0
for desktop_file in firefox-esr thunar xfce4-terminal com.visualstudio.CodeOSS etcode-desktop; do
    if [ ! -e "/usr/share/applications/${desktop_file}.desktop" ]; then
        echo "ERROR: /usr/share/applications/${desktop_file}.desktop not found;" \
             "its quick-launch entry would not appear on the panel" >&2
        missing=1
    fi
done
[ "$missing" -eq 0 ] || exit 1

# ---------------------------------------------------------------------------
log "cleaning up"
rm -rf "$STAGING"

log "provisioning finished"