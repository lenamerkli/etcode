#!/usr/bin/env bash
#
# build_image.sh — build the immutable etcode Debian template image.
#
# Produces: out/etcode-base-v1-amd64.qcow2
#
# The image is based on the official Debian generic cloud image and is
# provisioned according to PLAN.md (build flow, cleanup) and SETTINGS.md
# (user `agent`, passwordless sudo, Xfce desktop with a Windows-like
# taskbar). Per-VM overlays are created later by etcode-manager itself,
# not by this script.
#
# Supported build hosts: Fedora/RHEL (dnf) and Debian/Ubuntu (apt-get).
# Run it as a user with passwordless sudo, or as root.
#
set -euo pipefail

# The virt-* tools live in /usr/sbin on Debian-family hosts, and a non-root
# shell (or sudo's secure_path) may not include the sbin directories.
case ":$PATH:" in
    *:/usr/sbin:*) ;;
    *) PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH" ;;
esac
export PATH

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DEBIAN_RELEASE="trixie"                       # Debian 13
UPSTREAM_NAME="debian-13-generic-amd64.qcow2" # generic variant: max. device compatibility
UPSTREAM_URL="${UPSTREAM_URL:-https://cloud.debian.org/images/cloud/${DEBIAN_RELEASE}/latest/${UPSTREAM_NAME}}"
SHA512SUMS_URL="${SHA512SUMS_URL:-https://cloud.debian.org/images/cloud/${DEBIAN_RELEASE}/latest/SHA512SUMS}"

# Optional: pin the upstream image for released builds. If set, the download
# must match this checksum instead of the moving `latest` SHA512SUMS file.
UPSTREAM_SHA512="${UPSTREAM_SHA512:-}"

IMAGE_VERSION="${IMAGE_VERSION:-v1}"   # bump for new template releases (v2, v3, ...)
IMAGE_NAME="etcode-base-${IMAGE_VERSION}-amd64"

# Virtual disk size of the template. The upstream image is only 3 GiB, which is
# too small for the desktop stack. Per PLAN.md the per-VM overlays are enlarged
# to 20G at instance creation and grown by cloud-init on first boot.
TEMPLATE_DISK_SIZE="10G"
ROOT_PARTITION="/dev/sda1"    # root partition of the generic cloud image

# The etcode desktop app (PySide6 GUI) that is installed into the image and autostarted. Its packaging/
# directory holds the launcher, the .desktop files and the icon.
DESKTOP_DIR="${DESKTOP_DIR:-$SCRIPT_DIR/../etcode_desktop}"

WORK_DIR="$SCRIPT_DIR/build"
OUT_DIR="$SCRIPT_DIR/out"
UPSTREAM_CACHE="$WORK_DIR/$UPSTREAM_NAME"
SHA512SUMS_CACHE="$WORK_DIR/SHA512SUMS"
BUILD_DISK="$WORK_DIR/build.qcow2"
STAGING_DIR="$WORK_DIR/staging"
OUTPUT_IMAGE="$OUT_DIR/${IMAGE_NAME}.qcow2"

# Packages installed into the guest.
#   - base per PLAN.md (cloud-init stack, ssh, git, python)
#   - Xfce desktop + taskbar plugins per SETTINGS.md
#   - quick-launch apps: firefox, thunar, xfce4-terminal, vscodium (see below)
GUEST_PACKAGES=(
    # PLAN.md: base
    cloud-init cloud-guest-utils openssh-server ca-certificates git
    python3 python3-venv
    # SETTINGS.md: desktop
    xorg lightdm lightdm-gtk-greeter xfce4 xfce4-whiskermenu-plugin
    # keeps the display on (used by provision/etcode-no-blank.desktop)
    x11-xserver-utils
    # Window listing, input and screenshots for the guest desktop tool
    wmctrl xdotool x11-utils imagemagick
    # quick launch: firefox, thunar, terminal (vscodium comes from its own
    # apt repository and is installed in provision.sh)
    firefox-esr thunar xfce4-terminal
    # passwordless sudo
    sudo
    # Qt (PySide6 wheels) for the etcode desktop app: the xcb platform plugin and OpenGL/EGL need these.
    # Verified with `ldd` on libqxcb.so of PySide6 6.12 on Debian 13; provision.sh checks again in the image.
    libegl1 libgl1 libxkbcommon0 libxkbcommon-x11-0 libfontconfig1 libdbus-1-3
    libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0
    libxcb-render-util0 libxcb-shape0 libxcb-util1 libxcb-xkb1 libxcb-xfixes0
    libxcb-sync1 libxcb-xinerama0
    # needed inside the guest to set up the VSCodium apt repository
    wget gnupg
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { printf '\n==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

run_sudo() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        die "this script needs root privileges (install sudo or run as root)"
    fi
}

# ---------------------------------------------------------------------------
# 0. Host dependencies (qemu host tools + guestfs-tools)
# ---------------------------------------------------------------------------
# Binaries this script invokes; verified before and after package install.
REQUIRED_TOOLS=(qemu-img virt-resize virt-customize)

missing_tools() {
    local tool missing=""
    for tool in "${REQUIRED_TOOLS[@]}"; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            missing+="$tool "
        fi
    done
    printf '%s' "${missing% }"
}

install_host_dependencies() {
    local apt_packages=(qemu-utils guestfs-tools curl coreutils)
    # NOTE: on modern Fedora `libguestfs-tools` is only a virtual provide of the
    # tiny `virt-win-reg` package and contains none of the C tools. The tools
    # (virt-resize, virt-customize, ...) ship in `guestfs-tools`, which also
    # Provides `libguestfs-tools-c`. Never install `libguestfs-tools` here.
    local dnf_packages=(qemu-img guestfs-tools curl coreutils)

    local missing
    missing="$(missing_tools)"
    if [ -z "$missing" ]; then
        log "host tools already installed (${REQUIRED_TOOLS[*]})"
        return
    fi

    if command -v dnf >/dev/null 2>&1; then
        log "installing host tools with dnf: ${dnf_packages[*]}"
        run_sudo dnf install -y "${dnf_packages[@]}"
    elif command -v apt-get >/dev/null 2>&1; then
        log "installing host tools with apt-get: ${apt_packages[*]}"
        run_sudo apt-get update
        run_sudo apt-get install -y "${apt_packages[@]}"
    else
        die "unsupported build host: need dnf (Fedora) or apt-get (Debian)"
    fi

    # Fail fast here, not mid-build: catches package names that resolve to
    # something which does not actually ship the expected tools.
    missing="$(missing_tools)"
    if [ -n "$missing" ]; then
        die "host tools still missing after install: $missing (expected from the 'qemu-img' and 'guestfs-tools' packages)"
    fi
}

# ---------------------------------------------------------------------------
# 1. Download and verify the upstream Debian cloud image
# ---------------------------------------------------------------------------
fetch_upstream() {
    mkdir -p "$WORK_DIR"

    if [ -n "$UPSTREAM_SHA512" ]; then
        # pinned build: reuse the cache if it matches the pin
        if [ -f "$UPSTREAM_CACHE" ] \
            && echo "$UPSTREAM_SHA512  $UPSTREAM_CACHE" | sha512sum -c --quiet -; then
            log "upstream image cached and matches pinned checksum"
            return
        fi
        log "downloading $UPSTREAM_URL"
        curl -fL --retry 3 -o "$UPSTREAM_CACHE" "$UPSTREAM_URL"
        echo "$UPSTREAM_SHA512  $UPSTREAM_CACHE" | sha512sum -c --quiet - \
            || die "upstream image does not match UPSTREAM_SHA512"
        return
    fi

    log "downloading SHA512SUMS"
    curl -fL --retry 3 -o "$SHA512SUMS_CACHE" "$SHA512SUMS_URL"

    if [ -f "$UPSTREAM_CACHE" ] \
        && (cd "$WORK_DIR" && grep " ${UPSTREAM_NAME}\$" SHA512SUMS | sha512sum -c --quiet -); then
        log "upstream image cached and checksum verified"
        return
    fi

    log "downloading $UPSTREAM_URL"
    curl -fL --retry 3 -o "$UPSTREAM_CACHE" "$UPSTREAM_URL"
    (cd "$WORK_DIR" && grep " ${UPSTREAM_NAME}\$" SHA512SUMS | sha512sum -c --quiet -) \
        || die "upstream image failed checksum verification"
    log "upstream image checksum verified"
}

# ---------------------------------------------------------------------------
# 2. Stage the etcode payload (copied into the image by provision.sh)
# ---------------------------------------------------------------------------
stage_payload() {
    log "staging etcode payload"
    rm -rf "$STAGING_DIR"
    mkdir -p \
        "$STAGING_DIR/home/agent/.config/xfce4/xfconf/xfce-perchannel-xml" \
        "$STAGING_DIR/opt" \
        "$STAGING_DIR/etc/sudoers.d" \
        "$STAGING_DIR/etc/lightdm/lightdm.conf.d"

    # skills and scripts -> /home/agent/{skills,scripts}
    cp -a "$SCRIPT_DIR/data/skills" "$STAGING_DIR/home/agent/skills"
    cp -a "$SCRIPT_DIR/data/scripts" "$STAGING_DIR/home/agent/scripts"

    # etcode tools + assets -> /opt/etcode
    cp -a "$SCRIPT_DIR/data/opt" "$STAGING_DIR/opt/etcode"

    # Xfce panel layout -> /home/agent/.config/... (per SETTINGS.md)
    cp -a "$SCRIPT_DIR/provision/xfce4-panel.xml" \
        "$STAGING_DIR/home/agent/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml"

    # launcher item .desktop files referenced by the panel config
    cp -a "$SCRIPT_DIR/provision/panel" \
        "$STAGING_DIR/home/agent/.config/xfce4/panel"

    # passwordless sudo for agent
    cp -a "$SCRIPT_DIR/provision/sudoers-agent" "$STAGING_DIR/etc/sudoers.d/agent"

    # display manager: autologin as agent (user has no password)
    cp -a "$SCRIPT_DIR/provision/50-etcode-autologin.conf" \
        "$STAGING_DIR/etc/lightdm/lightdm.conf.d/50-etcode-autologin.conf"

    # keep the display on (no blanking/locking in agent VMs)
    mkdir -p "$STAGING_DIR/etc/xdg/autostart"
    cp -a "$SCRIPT_DIR/provision/etcode-no-blank.desktop" \
        "$STAGING_DIR/etc/xdg/autostart/etcode-no-blank.desktop"

    stage_desktop_app
}

# The etcode desktop app: the code (without tests and caches) -> /opt/etcode/etcode_desktop, the launcher ->
# /usr/local/bin, menu entry + icon -> /usr/share, autostart entry -> /etc/xdg/autostart (the same mechanism as
# etcode-no-blank.desktop), and a quick-launch item for the panel (plugin 11 in xfce4-panel.xml). Its Python
# dependencies are installed into the venv by provision.sh. No token goes into the image: the manager delivers it
# per VM through cloud-init (seed.py).
stage_desktop_app() {
    local packaging="$DESKTOP_DIR/packaging"
    if [ ! -f "$DESKTOP_DIR/pyproject.toml" ] || [ ! -d "$DESKTOP_DIR/etcode_desktop" ]; then
        die "etcode_desktop not found in $DESKTOP_DIR (set DESKTOP_DIR)"
    fi
    [ -d "$packaging" ] || die "$packaging not found"

    mkdir -p \
        "$STAGING_DIR/opt/etcode/etcode_desktop" \
        "$STAGING_DIR/usr/local/bin" \
        "$STAGING_DIR/etc/xdg/autostart" \
        "$STAGING_DIR/usr/share/applications" \
        "$STAGING_DIR/usr/share/icons/hicolor/scalable/apps" \
        "$STAGING_DIR/home/agent/.config/xfce4/panel/launcher-11"
    (cd "$DESKTOP_DIR" && tar --exclude='__pycache__' --exclude='*.pyc' -cf - \
        etcode_desktop pyproject.toml requirements.txt README.md) \
        | tar -C "$STAGING_DIR/opt/etcode/etcode_desktop" -xf -
    install -m 0755 "$packaging/etcode-desktop" "$STAGING_DIR/usr/local/bin/etcode-desktop"
    install -m 0644 "$packaging/etcode-desktop.desktop" \
        "$STAGING_DIR/usr/share/applications/etcode-desktop.desktop"
    install -m 0644 "$packaging/etcode-desktop.svg" \
        "$STAGING_DIR/usr/share/icons/hicolor/scalable/apps/etcode-desktop.svg"
    install -m 0644 "$packaging/etcode-desktop-autostart.desktop" \
        "$STAGING_DIR/etc/xdg/autostart/etcode-desktop.desktop"
    install -m 0644 "$packaging/etcode-desktop.desktop" \
        "$STAGING_DIR/home/agent/.config/xfce4/panel/launcher-11/17929499125.desktop"
}

# ---------------------------------------------------------------------------
# 3. Build the image
# ---------------------------------------------------------------------------
build_image() {
    log "creating working disk ($TEMPLATE_DISK_SIZE) from the upstream image"
    rm -f "$BUILD_DISK"
    qemu-img create -f qcow2 "$BUILD_DISK" "$TEMPLATE_DISK_SIZE"
    run_sudo virt-resize --format qcow2 --output-format qcow2 \
        --expand "$ROOT_PARTITION" "$UPSTREAM_CACHE" "$BUILD_DISK"

    # /etc/machine-id must exist but be EMPTY in the template. Inside the
    # libguestfs appliance `cloud-init clean --machine-id` deletes the file;
    # since the root fs is still read-only early in boot, systemd then cannot
    # create a machine ID ("System cannot boot: Missing /etc/machine-id and
    # /etc is mounted read-only"). Without one, systemd-networkd fails to
    # start its DHCPv4 client (ENOENT), the NIC never gets an address and
    # systemd-networkd-wait-online blocks the boot for its 2 min timeout.
    # An empty file makes systemd generate a fresh, unique ID on first boot
    # and commit it. /var/lib/dbus/machine-id must not carry the build-time
    # ID either, so it becomes a symlink to /etc/machine-id.
    log "provisioning the image (virt-customize)"
    run_sudo virt-customize --format qcow2 -a "$BUILD_DISK" \
        --install "$(IFS=,; echo "${GUEST_PACKAGES[*]}")" \
        --copy-in "$STAGING_DIR:/tmp" \
        --run "$SCRIPT_DIR/provision/provision.sh" \
        --run-command 'cloud-init clean --logs --machine-id --seed' \
        --run-command 'rm -f /etc/machine-id /var/lib/dbus/machine-id; : > /etc/machine-id; chmod 0444 /etc/machine-id; ln -s /etc/machine-id /var/lib/dbus/machine-id' \
        --run-command 'rm -f /etc/ssh/ssh_host_*; apt-get clean'

    log "writing final template $OUTPUT_IMAGE"
    mkdir -p "$OUT_DIR"
    rm -f "$OUTPUT_IMAGE"
    qemu-img convert -f qcow2 -O qcow2 "$BUILD_DISK" "$OUTPUT_IMAGE"

    # nothing here should stay root-owned
    if [ "$(id -u)" -eq 0 ]; then
        chown "$(stat -c %u:%g "$SCRIPT_DIR")" "$OUTPUT_IMAGE" 2>/dev/null || true
    fi

    log "done"
    echo
    echo "Template image : $OUTPUT_IMAGE"
    echo "Size           : $(du -h "$OUTPUT_IMAGE" | cut -f1)"
    echo "SHA512         : $(sha512sum "$OUTPUT_IMAGE" | cut -d' ' -f1)"
    echo
    echo "Boot it with disk.qcow2 overlays (see PLAN.md); the template itself"
    echo "must never be modified."
}

# ---------------------------------------------------------------------------
install_host_dependencies
fetch_upstream
stage_payload
build_image
