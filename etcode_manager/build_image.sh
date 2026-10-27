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
    # quick launch: firefox, thunar, terminal (vscodium comes from its own
    # apt repository and is installed in provision.sh)
    firefox-esr thunar xfce4-terminal
    # passwordless sudo
    sudo
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
# 0. Host dependencies (qemu host tools + libguestfs)
# ---------------------------------------------------------------------------
install_host_dependencies() {
    local apt_packages=(qemu-utils guestfs-tools curl coreutils)
    local dnf_packages=(qemu-img libguestfs-tools curl coreutils)

    if command -v qemu-img >/dev/null 2>&1 && command -v virt-customize >/dev/null 2>&1; then
        log "host tools already installed (qemu-img, virt-customize)"
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

    log "provisioning the image (virt-customize)"
    run_sudo virt-customize --format qcow2 -a "$BUILD_DISK" \
        --install "$(IFS=,; echo "${GUEST_PACKAGES[*]}")" \
        --copy-in "$STAGING_DIR:/tmp" \
        --run "$SCRIPT_DIR/provision/provision.sh" \
        --run-command 'cloud-init clean --logs --machine-id --seed' \
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
