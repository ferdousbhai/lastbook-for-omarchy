#!/usr/bin/env bash
# Run install.sh for real, with pacman, pacman-key, curl, gpg and uname
# stubbed, inside a user and mount namespace where /etc/pacman.conf,
# /etc/pacman.d and /root are scratch copies: nothing on this machine
# changes. Covers both architectures, plain Arch, re-running and options.
# First, outside the namespace, checks the add_signed_repo hash; only the
# installer runs are skipped where unprivileged namespaces are off.
set -euo pipefail
cd "$(dirname "$0")/.."
root="$PWD"

work="$(mktemp -d)"
trap 'rm -rf "${work:?}"' EXIT
fail=0
check() { # check <description> <command...>
  local what=$1
  shift
  if "$@"; then echo "ok $what"; else echo "FAIL $what"; fail=1; fi
}
not() { ! "$@"; }

if [[ ${LASTBOOK_INSTALL_TEST_INNER:-} != 1 ]]; then
  hash_of() { sed -n '/^# --- add_signed_repo (shared) ---$/,/^# --- end add_signed_repo ---$/p' "$1" | sed '1d;$d' | sha256sum | cut -d' ' -f1; }
  check "add_signed_repo hash in install.sh" [ "$(hash_of install.sh)" = "$(cat tests/add_signed_repo.sha256)" ]
  if ((fail)); then
    echo "add_signed_repo drifted from its pinned hash: review the change and update tests/add_signed_repo.sha256" >&2
    exit 1
  fi
  if ! unshare -rm true 2>/dev/null; then
    echo "skip install_test: unprivileged user namespaces are unavailable" >&2
    exit ${CI:+1}   # skipped on a computer that cannot run them; in CI a skip is a failure
  fi
  rm -rf "${work:?}"
  exec env LASTBOOK_INSTALL_TEST_INNER=1 unshare -rm "$root/tests/install_test.sh"
fi

# The scratch copies are mounted over /etc/pacman.conf and /etc/pacman.d, so both must be there to mount over (CI,
# which is not Arch, makes them empty first).
if [[ ! -f /etc/pacman.conf || ! -d /etc/pacman.d ]]; then
  echo "install_test: /etc/pacman.conf and /etc/pacman.d must exist (they are never written)" >&2
  exit 1
fi
# Stubs: pacman logs its arguments, curl
# "downloads" an empty key, gpg reports the pinned fingerprint for it.
mkdir -p "$work/bin"
cat >"$work/bin/pacman" <<'STUB'
#!/bin/sh
echo "pacman $*" >>"$PACMAN_LOG"
# STUB_NOT_INSTALLED: the install "succeeds" but the package is not there afterwards (pacman -Q fails).
[ "$1" = -Q ] && [ -n "${STUB_NOT_INSTALLED:-}" ] && exit 1
exit 0
STUB
cat >"$work/bin/omarchy-pkg-add" <<'STUB'
#!/bin/sh
echo "omarchy-pkg-add $*" >>"$PACMAN_LOG"
STUB
cat >"$work/bin/pacman-key" <<'STUB'
#!/bin/sh
exit 0
STUB
cat >"$work/bin/curl" <<'STUB'
#!/bin/sh
while [ $# -gt 0 ]; do [ "$1" = -o ] && : >"$2"; shift; done
STUB
cat >"$work/bin/uname" <<'STUB'
#!/bin/sh
echo "${STUB_ARCH:-x86_64}"
STUB
fingerprint=$(sed -n 's/^SIGNING_KEY_FINGERPRINT=//p' install.sh)
printf '#!/bin/sh\necho "pub:u:4096:1:A89CA1A6A1E74251:1:::u:::scESC:"\necho "fpr:::::::::%s:"\n' "$fingerprint" >"$work/bin/gpg"
chmod 755 "$work/bin/"*
export PATH="$work/bin:$PATH" PACMAN_LOG="$work/pacman.log" SUDO_USER=root

# A fresh scratch /etc and /root for each scenario.
setup() {
  local s="$work/scene"
  umount -q /etc/pacman.conf /etc/pacman.d /root 2>/dev/null || true
  rm -rf "${s:?}" "$PACMAN_LOG"
  mkdir -p "$s/pacman.d" "$s/root/.config/omarchy/hooks/pre-refresh-pacman.d"
  printf '[options]\nArchitecture = auto\n\n[core]\nInclude = /etc/pacman.d/mirrorlist\n' >"$s/pacman.conf"
  mount --bind "$s/pacman.conf" /etc/pacman.conf
  mount --bind "$s/pacman.d" /etc/pacman.d
  mount --bind "$s/root" /root
}
hooks=/root/.config/omarchy/hooks/pre-refresh-pacman.d
# What the last install asked for, whichever way it installed (omarchy-pkg-add on Omarchy).
installed() { grep -E '^(pacman -Syu|omarchy-pkg-add) ' "$PACMAN_LOG" | tail -1 | sed -E 's/^(pacman -Syu --needed --noconfirm|omarchy-pkg-add) //'; }
count_include() { grep -cxF "Include = /etc/pacman.d/$1.conf" /etc/pacman.conf || true; }

setup
# Nobody is at a terminal here, so Recall is never offered: the installer says how to add it, unless it is there already.
export LASTBOOK_RECALL_COMMAND=no-such-recall-command
bash install.sh >"$work/out" 2>&1 || { cat "$work/out" >&2; exit 1; }
check "install.sh installs lastbook-for-omarchy" [ "$(installed)" = "lastbook-for-omarchy" ]
check "the repository config is written" grep -qx 'Server = https://github.com/ferdousbhai/lastbook-for-omarchy/releases/latest/download' /etc/pacman.d/lastbook-for-omarchy.conf
check "the repository requires signatures" grep -qx 'SigLevel = Required DatabaseRequired' /etc/pacman.d/lastbook-for-omarchy.conf
check "the Include line is added" [ "$(count_include lastbook-for-omarchy)" = 1 ]
check "the Omarchy hook is installed" [ -x "$hooks/lastbook-for-omarchy" ]
check "it says how to add Recall, without installing it" grep -q 'ferdousbhai.com/recall/install.sh' "$work/out"
check "it never installs Recall itself" not grep -q recall "$PACMAN_LOG"
check "the installer says it is done" grep -q '^Done' "$work/out"
# The skill belongs to the desktop user. Here the installer runs as root itself, so it writes none and says how.
check "as root it says how to install the agent skill" grep -qx 'For AI agents, as your desktop user: lastbook skill install' "$work/out"
check "as root it writes no skill" [ ! -e /root/.agents ]
check "it names the command line" grep -q 'lastbook --help' "$work/out"
check "updates come through omarchy update" grep -q 'omarchy update' "$work/out"
LASTBOOK_RECALL_COMMAND=true bash install.sh >"$work/have-recall.out" 2>&1
check "with Recall already installed it does not suggest it" not grep -q 'recall/install.sh' "$work/have-recall.out"
bash install.sh --yes >/dev/null 2>&1
check "re-running (with --yes) keeps one Include line" [ "$(count_include lastbook-for-omarchy)" = 1 ]
check "on Omarchy it never runs pacman -Syu (Omarchy's update guard aborts it)" not grep -q '^pacman -Syu' "$PACMAN_LOG"

setup
status=0
bash install.sh --bogus >/dev/null 2>&1 || status=$?
check "an unknown option exits 2" [ "$status" = 2 ]
check "an unknown option changes nothing" [ ! -e /etc/pacman.d/lastbook-for-omarchy.conf ]

# ARM's update helper removes the old [omarchy] block before appending
# a restricted replacement. A trailing Include must survive that rewrite.
setup
printf '\n[omarchy]\nServer = old\nInclude = /etc/pacman.d/lastbook-for-omarchy.conf\n' >> /etc/pacman.conf
bash install.sh >/dev/null 2>&1
awk '/^[[:space:]]*\[/ { omit = ($0 ~ /^[[:space:]]*\[omarchy\][[:space:]]*(#.*)?$/) } !omit { print }' /etc/pacman.conf > "$work/arm.conf"
check "repository Include survives ARM source replacement" grep -qxF 'Include = /etc/pacman.d/lastbook-for-omarchy.conf' "$work/arm.conf"
check "moving an existing Include keeps one copy" [ "$(count_include lastbook-for-omarchy)" = 1 ]
printf '[options]\n[omarchy]\nServer = old\n' > /etc/pacman.conf
bash "$hooks/lastbook-for-omarchy"
awk '/^[[:space:]]*\[/ { omit = ($0 ~ /^[[:space:]]*\[omarchy\][[:space:]]*(#.*)?$/) } !omit { print }' /etc/pacman.conf > "$work/arm.conf"
check "refresh hook Include survives ARM source replacement" grep -qxF 'Include = /etc/pacman.d/lastbook-for-omarchy.conf' "$work/arm.conf"

# aarch64: its own repository.
setup
STUB_ARCH=aarch64 bash install.sh >/dev/null 2>&1
check "aarch64 uses the [lastbook-for-omarchy-aarch64] repository" [ "$(count_include lastbook-for-omarchy-aarch64)" = 1 ]
check "aarch64 repository config is written" [ -e /etc/pacman.d/lastbook-for-omarchy-aarch64.conf ]
check "aarch64 leaves the x86 repository out" [ "$(count_include lastbook-for-omarchy)" = 0 ]
check "aarch64 installs lastbook-for-omarchy" [ "$(installed)" = "lastbook-for-omarchy" ]

setup
status=0
STUB_ARCH=riscv64 bash install.sh >/dev/null 2>&1 || status=$?
check "an unsupported architecture fails" [ "$status" = 1 ]
check "an unsupported architecture changes nothing" [ ! -e /etc/pacman.d/lastbook-for-omarchy.conf ]

setup
status=0
STUB_NOT_INSTALLED=1 bash install.sh >"$work/out" 2>&1 || status=$?
check "a package that did not install fails the installer" [ "$status" = 1 ]
check "and says so" grep -q 'was not installed' "$work/out"

# Plain Arch: no Omarchy commands anywhere on PATH, so one pacman -Syu transaction.
setup
mkdir -p "$work/plain"
cp "$work/bin/pacman" "$work/bin/pacman-key" "$work/bin/curl" "$work/bin/gpg" "$work/bin/uname" "$work/plain/"
for tool in bash sh sed grep mktemp rm tee id getent cut install chown chmod cat env dirname basename tr sort head tail awk cmp mkdir mv; do
  ln -sf "$(command -v "$tool")" "$work/plain/$tool"
done
PATH="$work/plain" bash install.sh >"$work/plain.out" 2>&1 || true
check "without Omarchy it installs in one pacman -Syu" grep -qx 'pacman -Syu --needed --noconfirm lastbook-for-omarchy' "$PACMAN_LOG"

umount -q /etc/pacman.conf /etc/pacman.d /root 2>/dev/null || true
if (( fail )); then
  echo "FAIL install_test" >&2
  exit 1
fi
echo "ok install_test"
