#!/bin/bash
# Install Lastbook for Omarchy (or any Arch Linux) from its signed package
# repository and keep it updating with the system:
#
#   curl -fsSL https://ferdousbhai.com/lastbook/install.sh | sudo bash
#
# Every step is idempotent, so re-running is safe. It trusts the
# package-signing key (checked against the fingerprint pinned below), adds
# the [lastbook-for-omarchy] repository, installs an Omarchy hook that
# restores it after `omarchy refresh pacman` rewrites /etc/pacman.conf, and
# installs the package (with vulkan-asahi on Apple hardware, for the local
# AI on the graphics card). There is no per-user step: the first launch
# opens Lastbook's own setup, which downloads the library you choose.
set -euo pipefail

REPO=lastbook-for-omarchy
RELEASES=${LASTBOOK_RELEASES:-https://github.com/ferdousbhai/lastbook-for-omarchy/releases/latest/download}
SIGNING_KEY_FINGERPRINT=52130299581DAD685226900CA89CA1A6A1E74251
PACKAGE=lastbook-for-omarchy

# --- add_signed_repo (shared) ---
# Trust a project's package-signing key (checked against the pinned
# fingerprint), add its signed pacman repository, and keep the repository
# across `omarchy refresh pacman`, which rewrites /etc/pacman.conf from
# Omarchy's defaults and then runs the user's pre-refresh-pacman hooks.
# Works as root (`sudo bash`) or as a desktop user (sudo inside). Other
# installers carry copies, each pinned by its repository's test; a copy
# is only as safe as its last port, so carry a change to every one.
add_signed_repo() {
  local name="$1" release="$2" fingerprint="$3"
  local conf="/etc/pacman.d/$name.conf" include="Include = /etc/pacman.d/$name.conf"
  local sudo='' key user home hook_dir as_user
  (( EUID == 0 )) || sudo=sudo
  key="$(mktemp)"
  if ! curl -fsSL "$release/$name-signing-key.asc" -o "$key"; then
    rm -f "$key"
    echo "Could not download the package-signing key from $release." >&2
    return 1
  fi
  # Exactly one primary key, and it is the pinned one: a subkey with the
  # pinned fingerprint, or a second primary bundled alongside, is refused.
  if ! gpg --batch --with-colons --show-keys "$key" 2>/dev/null | awk -F: -v pin="$fingerprint" '
    $1 == "fpr" && after_pub { primary = $10 }
    { after_pub = 0 }
    $1 == "pub" { pubs++; after_pub = 1 }
    END { exit !(pubs == 1 && primary == pin) }
  '; then
    rm -f "$key"
    echo "The downloaded key does not match the pinned fingerprint $fingerprint; nothing was changed." >&2
    return 1
  fi
  $sudo pacman-key --add "$key"
  $sudo pacman-key --lsign-key "$fingerprint"
  rm -f "$key"
  printf '[%s]\nSigLevel = Required DatabaseRequired\nServer = %s\n' "$name" "$release" | $sudo tee "$conf" >/dev/null
  # ARM updates replace [omarchy] and everything below its header up to
  # the next section. Keep our Include before repository sections so it
  # cannot be mistaken for part of [omarchy], even on an existing install.
  restore_repo_include() {
    local include=$1 temporary sudo=''
    (( EUID == 0 )) || sudo=sudo
    temporary=$(mktemp)
    awk -v repo_include="$include" '
      $0 == repo_include { next }
      !added && /^[[:space:]]*\[/ && !/^[[:space:]]*\[options\]/ {
        print repo_include
        added = 1
      }
      { print }
      END { if (!added) print repo_include }
    ' /etc/pacman.conf > "$temporary"
    if ! cmp -s "$temporary" /etc/pacman.conf; then
      $sudo tee /etc/pacman.conf < "$temporary" >/dev/null
    fi
    rm -f "$temporary"
  }
  restore_repo_include "$include"
  user="${SUDO_USER:-${USER:-$(id -un)}}"
  home="$(getent passwd "$user" | cut -d: -f6)"
  if [[ -n $home && -d $home/.config/omarchy ]]; then
    # The home is the user's: write there as them, so a symlink in it cannot
    # turn root's write onto a system file.
    as_user=()
    if (( EUID == 0 )) && [[ $(id -u "$user") != 0 ]]; then
      as_user=(runuser -u "$user" --)
    fi
    hook_dir="$home/.config/omarchy/hooks/pre-refresh-pacman.d"
    "${as_user[@]}" mkdir -p "$hook_dir"
    # The user's shell reads the pipe it inherits (root's pipe cannot be
    # reopened by path once runuser drops privileges), and mv replaces the
    # hook file, never writing through a link there.
    {
      printf '%s\n' '#!/bin/bash' 'set -euo pipefail' \
        "# Restore the [$name] repository before Omarchy's ARM source rewrite."
      declare -f restore_repo_include
      printf 'restore_repo_include %q\n' "$include"
    } | "${as_user[@]}" bash -c 'umask 022; t=$(mktemp "$1.XXXXXX") && cat >"$t" && chmod 755 "$t" && mv -fT "$t" "$1"' _ "$hook_dir/$name"
  fi
  $sudo pacman -Sy
}
# --- end add_signed_repo ---

for arg in "$@"; do
  case $arg in
    -y|--yes) ;; # Never asks; accepted so a habit from other installers works.
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done
if ! command -v pacman >/dev/null; then
  echo "pacman not found: this installer is for Omarchy and other Arch Linux systems." >&2
  exit 1
fi
if [[ ! $SIGNING_KEY_FINGERPRINT =~ ^[0-9A-F]{40}$ ]]; then
  echo "This copy of install.sh has no signing key pinned; nothing was changed." >&2
  exit 1
fi

packages=("$PACKAGE")
# One repository per architecture: ARM needs a separate database.
case "$(uname -m)" in
  x86_64) ;;
  aarch64)
    REPO+=-aarch64
    # Apple GPUs on Linux use the Asahi Vulkan userspace driver; other ARM
    # machines bring their own driver, or the local AI uses the CPU.
    if grep -aqs Apple /sys/firmware/devicetree/base/model; then
      packages+=(vulkan-asahi)
    fi
    ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

echo "Adding the [$REPO] repository"
add_signed_repo "$REPO" "$RELEASES" "$SIGNING_KEY_FINGERPRINT"

echo "Installing ${packages[*]}"
if command -v omarchy-pkg-add >/dev/null; then
  # Omarchy's pacman hook aborts a direct `pacman -Syu` (system upgrades go
  # through `omarchy update`), so install the way Omarchy installs its own
  # apps; the next `omarchy update` brings everything current.
  omarchy-pkg-add "${packages[@]}"
else
  # Upgrade and install in one transaction. add_signed_repo has just synced
  # every repository's database, and installing from those without upgrading
  # is Arch's unsupported partial upgrade.
  if (( EUID == 0 )); then
    pacman -Syu --needed --noconfirm "${packages[@]}"
  else
    sudo pacman -Syu --needed --noconfirm "${packages[@]}"
  fi
fi

cat <<EOT

Done. Open Lastbook from the application menu; its first launch sets up the
library, the map and the optional local AI.
For meaning-based search, add Recall for Omarchy (optional):
  curl -fsSL https://ferdousbhai.com/recall/install.sh | sudo bash
Updates arrive with the rest of the system through: omarchy update
EOT
