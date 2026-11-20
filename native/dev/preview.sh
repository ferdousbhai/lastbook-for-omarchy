#!/usr/bin/env bash
set -Eeuo pipefail

# Lastbook's window in a nested Hyprland with a private HOME, XDG directories and D-Bus, over a small made-up library
# (dev/seed.py): nothing of the desktop it is run from is read or shown on, and no real library or Recall is used.
#
#   native/dev/preview.sh <screenshot.png> [route [theme-dir]]
#
# `route` is LASTBOOK_OPEN (ui.rs): `section:guides` (the default), `search:boil water`,
# `wiki:mdwiki_preview.zim/Water_purification`, `read:safe-water:1`, ...
# `theme-dir` holds a `colors.toml` (an Omarchy theme, such as /usr/share/omarchy/themes/catppuccin-latte) and is
# copied in as the scratch desktop's current theme; without one there is no Omarchy state and the window shows the
# fallback palette.
#
#   LASTBOOK_PREVIEW_BINARY   the window to run (default native/target/debug/lastbook)
#   LASTBOOK_PREVIEW_SETTLE   seconds to wait before the picture (default 6)
#   LASTBOOK_PREVIEW_HEADLESS 0 puts the window on the nested compositor's own output, which is a window on the
#                             desktop this runs from and as large as that desktop makes it. WebKit paints nothing on
#                             a headless output, so `wiki:` and `web:` routes need this. The default is a headless
#                             output of LASTBOOK_PREVIEW_SIZE, the same picture wherever it is run.
#   LASTBOOK_PREVIEW_SIZE     the output's size, which the window fills (default 1280x820); 560x700 is a third of a
#                             display
if (( $# < 1 || $# > 3 )); then
  echo "usage: $0 <screenshot.png> [route [theme-dir]]" >&2
  exit 2
fi
screenshot=$(realpath -m -- "$1")
route=${2:-section:guides}
theme=${3:-}

here=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo=$(realpath -e -- "$here/../..")
binary=${LASTBOOK_PREVIEW_BINARY:-$repo/native/target/debug/lastbook}
[[ -x $binary ]] || { echo "preview: build the window first (cargo build --manifest-path native/Cargo.toml)" >&2; exit 1; }
for command_name in Hyprland hyprctl grim jq dbus-daemon setsid python3; do
  command -v "$command_name" >/dev/null || { echo "preview: $command_name is required" >&2; exit 1; }
done
[[ -z $theme || -f $theme/colors.toml ]] || { echo "preview: $theme has no colors.toml" >&2; exit 1; }
parent_runtime=${XDG_RUNTIME_DIR:?preview: XDG_RUNTIME_DIR is required}
parent_display=${WAYLAND_DISPLAY:?preview: run this from a Wayland desktop}
parent_socket=$parent_runtime/$parent_display
settle=${LASTBOOK_PREVIEW_SETTLE:-6}
headless=${LASTBOOK_PREVIEW_HEADLESS:-1}
size=${LASTBOOK_PREVIEW_SIZE:-1280x820}
[[ $size =~ ^[0-9]+x[0-9]+$ ]] || { echo "preview: LASTBOOK_PREVIEW_SIZE is <width>x<height>" >&2; exit 2; }
# The two wheels Arch does not carry, from the cache bin/test uses: found before HOME is replaced.
deps=$("$repo/bin/test-deps")

root=""
runtime=""
hypr_pid=""
window_pid=""
dbus_pid=""
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  local pid
  # Only the PIDs this script started. The engine is the window's child and leaves when its input closes.
  for pid in "$window_pid" "$hypr_pid" "$dbus_pid"; do
    [[ -z $pid ]] || kill -TERM "$pid" 2>/dev/null || true
  done
  for _ in {1..30}; do
    local alive=0
    for pid in "$window_pid" "$hypr_pid" "$dbus_pid"; do
      [[ -z $pid ]] || ! kill -0 "$pid" 2>/dev/null || alive=1
    done
    (( alive == 0 )) && break
    sleep 0.1
  done
  for pid in "$window_pid" "$hypr_pid" "$dbus_pid"; do
    [[ -z $pid ]] || kill -KILL "$pid" 2>/dev/null || true
  done
  [[ -z $root || $root != /tmp/lastbook-preview.* ]] || rm -rf -- "$root"
  [[ -z $runtime || $runtime != /tmp/lbp.* ]] || rm -rf -- "$runtime"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Wayland and Hyprland IPC use AF_UNIX paths capped at 108 bytes.
root=$(mktemp -d /tmp/lastbook-preview.XXXXXX)
runtime=$(mktemp -d /tmp/lbp.XXXXXX)
chmod 700 "$root" "$runtime"
mkdir -p "$root"/{home,config,state,data,cache}

export HOME=$root/home
export XDG_CONFIG_HOME=$root/config XDG_STATE_HOME=$root/state XDG_DATA_HOME=$root/data XDG_CACHE_HOME=$root/cache
export XDG_RUNTIME_DIR=$runtime
unset GTK_THEME LASTBOOK_DATA LASTBOOK_ENGINE XDG_DOCUMENTS_DIR

if [[ -n $theme ]]; then
  current=$HOME/.local/state/omarchy/current
  mkdir -p "$current/theme"
  cp -- "$theme/colors.toml" "$current/theme/colors.toml"
  basename -- "$(realpath -- "$theme")" > "$current/theme.name"
fi

PYTHONPATH=$deps python3 -B "$here/seed.py" "$root/library" > "$root/seed.log" 2>&1 \
  || { echo "preview: could not make the library" >&2; sed -n '1,40p' "$root/seed.log" >&2; exit 1; }

# A session bus with no service directories: nothing can be dbus-activated on it.
cat > "$root/dbus.conf" <<EOF_DBUS
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:abstract=lastbook-preview-$BASHPID</listen>
  <keep_umask/>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
EOF_DBUS
mapfile -t dbus_details < <(/usr/bin/dbus-daemon --config-file="$root/dbus.conf" --fork --print-address=1 --print-pid=1)
(( ${#dbus_details[@]} == 2 )) || { echo "preview: could not start the isolated session bus" >&2; exit 1; }
export DBUS_SESSION_BUS_ADDRESS=${dbus_details[0]}
dbus_pid=${dbus_details[1]}

printf '%s\n' \
  "hl.monitor({ output = \"\", mode = \"$size@60\", position = \"auto\", scale = 1 })" \
  'hl.config({' \
  '  general = { gaps_in = 0, gaps_out = 0, border_size = 0 },' \
  '  decoration = { rounding = 0, shadow = { enabled = false }, blur = { enabled = false } },' \
  '  animations = { enabled = false },' \
  '  misc = { disable_hyprland_logo = true, disable_splash_rendering = true },' \
  '})' > "$root/hyprland.lua"
[[ $headless == 0 ]] || echo 'hl.window_rule({ match = { class = ".*" }, monitor = "lastbook" })' >> "$root/hyprland.lua"
unset HYPRLAND_INSTANCE_SIGNATURE
/usr/bin/setsid /usr/bin/env WAYLAND_DISPLAY="$parent_socket" /usr/bin/Hyprland --config "$root/hyprland.lua" > "$root/hyprland.log" 2>&1 &
hypr_pid=$!
signature=""
display=""
for _ in {1..120}; do
  kill -0 "$hypr_pid" 2>/dev/null || { echo "preview: nested Hyprland exited during startup" >&2; sed -n '1,80p' "$root/hyprland.log" >&2; exit 1; }
  for instance in "$XDG_RUNTIME_DIR"/hypr/*; do
    [[ -S $instance/.socket.sock && -f $instance/hyprland.lock ]] || continue
    candidate=$(sed -n '2p' "$instance/hyprland.lock")
    [[ -n $candidate && -S $XDG_RUNTIME_DIR/$candidate ]] || continue
    signature=$(basename "$instance")
    display=$candidate
    break 2
  done
  sleep 0.1
done
[[ -n $signature ]] || { echo "preview: nested Hyprland did not publish its sockets" >&2; exit 1; }
export HYPRLAND_INSTANCE_SIGNATURE=$signature WAYLAND_DISPLAY=$display

# The nested compositor's own output is a window on the desktop this runs from, as large as that desktop tiles it.
# Lastbook goes on a headless output instead, whose size is the monitor rule's; the window rule sends it there.
if [[ $headless == 0 ]]; then
  output=$(hyprctl monitors -j | jq -r 'first(.[]).name')
else
  output=lastbook
  hyprctl output create headless "$output" >/dev/null
fi

# No Recall (a socket that is not there).
LASTBOOK_OPEN=$route LASTBOOK_DEV=1 PYTHONPATH=$deps \
  LASTBOOK_RECALL_SOCKET=$root/no-recall.sock GDK_BACKEND=wayland \
  "$binary" --data "$root/library" > "$root/window.log" 2>&1 &
window_pid=$!
on_output() {
  local id
  id=$(hyprctl monitors -j | jq --arg name "$output" 'first(.[] | select(.name == $name) | .id)') &&
    hyprctl clients -j | jq -e --argjson id "${id:-null}" 'any(.[]; .mapped and .monitor == $id)' >/dev/null
}
for _ in {1..150}; do
  on_output && break
  kill -0 "$window_pid" 2>/dev/null || break
  sleep 0.1
done
sleep "$settle"
kill -0 "$window_pid" 2>/dev/null || { echo "preview: the window exited" >&2; sed -n '1,40p' "$root/window.log" >&2; exit 1; }
# A picture of an output the window is not on would be blank, and look like a result.
on_output || { echo "preview: the window is not on the output being captured" >&2; hyprctl clients >&2; exit 1; }
mkdir -p "$(dirname "$screenshot")"
# Hyprland's own notices (it says it was started without its wrapper) would cover the header.
hyprctl dismissnotify >/dev/null
/usr/bin/grim -o "$output" "$screenshot"
echo "preview: $screenshot"
[[ ! -s $root/window.log ]] || sed -n '1,20p' "$root/window.log" >&2
