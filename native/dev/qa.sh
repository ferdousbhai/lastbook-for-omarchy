#!/usr/bin/env bash
set -Eeuo pipefail

# Runs the window, built with feature `qa`, through a QA script (omui's scripted QA: its README, "Scripted QA") over
# the made-up library of dev/seed.py, on a broadway display of its own with a private HOME and XDG directories and no
# session bus: no window opens anywhere visible, nothing of the desktop this is run from is read, and no real library
# or Recall is used. Prints each command with its answer and exits with the window's status, non-zero when a
# command failed or GLib logged a warning or a critical. bin/test runs every script in dev/qa. The window is started
# as the desktop entry starts it, `lastbook open`.
#
#   native/dev/qa.sh <script.jsonl> [shots-dir]
#
# A script's `shot` paths are relative to `shots-dir`; without one the pictures are thrown away with the run.
#
# A script with a folder of its own name beside it, `<name>.engine/engine_pipe.py`, is run over that stand-in engine in
# place of the real one: what the window shows while the engine is slow, when it fails, and while a download runs.
#
#   LASTBOOK_QA_BINARY   the window to run (default native/target/debug/lastbook, `cargo build --features qa`)
#   LASTBOOK_QA_TIMEOUT  seconds before the whole run is stopped (default 120)
(( $# == 1 || $# == 2 )) || { echo "usage: $0 <script.jsonl> [shots-dir]" >&2; exit 2; }
script=$(realpath -e -- "$1")
here=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo=$(realpath -e -- "$here/../..")
binary=${LASTBOOK_QA_BINARY:-$repo/native/target/debug/lastbook}
engine=()
[[ ! -f ${script%.jsonl}.engine/engine_pipe.py ]] || engine=(LASTBOOK_ENGINE="${script%.jsonl}.engine/engine_pipe.py")
# WebKit puts its page processes in a bubblewrap sandbox, which a container without user namespaces (CI's) cannot make:
# the window would abort at its first web page. There, and only there, the test runs them unsandboxed.
sandbox=()
bwrap --unshare-user --ro-bind / / true 2>/dev/null || sandbox=(WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1)
[[ -x $binary ]] || { echo "qa: build the window first (cargo build --features qa --manifest-path native/Cargo.toml)" >&2; exit 1; }
command -v gtk4-broadwayd >/dev/null || { echo "qa: gtk4-broadwayd is required" >&2; exit 1; }
# The two wheels Arch does not carry, from the cache bin/test uses: found before HOME is replaced.
deps=$("$repo/bin/test-deps")

root=$(mktemp -d /tmp/lastbook-qa.XXXXXX)
shots=$(realpath -m -- "${2:-$root/shots}")
mkdir -p "$shots"
broadway_pid=""
app_pid=""
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  # Only the window and the display this script started. The engine is the window's child and leaves when its input
  # closes.
  [[ -z $app_pid ]] || kill -TERM "$app_pid" 2>/dev/null || true
  [[ -z $app_pid ]] || wait "$app_pid" 2>/dev/null || true
  [[ -z $broadway_pid ]] || kill -TERM "$broadway_pid" 2>/dev/null || true
  [[ -z $broadway_pid ]] || wait "$broadway_pid" 2>/dev/null || true
  [[ $root != /tmp/lastbook-qa.* ]] || rm -rf -- "$root"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$root"/{home,config/gtk-4.0,state,data,cache,runtime}
chmod 700 "$root/runtime"
# No cross-fade between screens: a shot taken as one opens would show the last one through it.
printf '[Settings]\ngtk-enable-animations=false\n' > "$root/config/gtk-4.0/settings.ini"

PYTHONPATH=$deps python3 -B "$here/seed.py" "$root/library" > "$root/seed.log" 2>&1 \
  || { echo "qa: could not make the library" >&2; sed -n '1,40p' "$root/seed.log" >&2; exit 1; }

# A display number and input port from this script's PID, so that two runs at once do not share one. broadwayd
# listens on that port on 127.0.0.1 alone, where omui's QA sends key presses as broadway's web page would.
display=$(( 100 + $$ % 800 ))
port=$(( 8080 + display ))
XDG_RUNTIME_DIR=$root/runtime gtk4-broadwayd --address 127.0.0.1 --port "$port" ":$display" \
  > "$root/broadwayd.log" 2>&1 &
broadway_pid=$!
for _ in {1..50}; do
  [[ -S $root/runtime/broadway$((display + 1)).socket ]] && break
  kill -0 "$broadway_pid" 2>/dev/null || { cat "$root/broadwayd.log" >&2; exit 1; }
  sleep 0.1
done

# In the background, waited for: bash runs a trap only once the foreground command ends, and `timeout` puts the window
# in a process group of its own, which Ctrl-C does not reach. Killing `timeout` stops the window with it.
# WEBKIT_DISABLE_DMABUF_RENDERER: else WebKit warns that broadway has no GL, and any warning fails the run.
status=0
(
  cd "$shots"
  exec env -u WAYLAND_DISPLAY -u DISPLAY -u DBUS_SESSION_BUS_ADDRESS -u GTK_THEME -u LASTBOOK_DATA -u LASTBOOK_ENGINE \
    -u LASTBOOK_OPEN -u XDG_DOCUMENTS_DIR \
    HOME="$root/home" XDG_CONFIG_HOME="$root/config" XDG_STATE_HOME="$root/state" XDG_DATA_HOME="$root/data" \
    XDG_CACHE_HOME="$root/cache" XDG_RUNTIME_DIR="$root/runtime" \
    LASTBOOK_DEV=1 PYTHONPATH="$deps" LASTBOOK_RECALL_SOCKET="$root/no-recall.sock" "${engine[@]}" \
    WEBKIT_DISABLE_DMABUF_RENDERER=1 "${sandbox[@]}" \
    GDK_BACKEND=broadway BROADWAY_DISPLAY=":$display" OMUI_QA=1 OMUI_QA_INPUT="127.0.0.1:$port" OMUI_QA_FD=3 \
    timeout "${LASTBOOK_QA_TIMEOUT:-120}" "$binary" open --data "$root/library" < "$script" 3> "$root/answers"
) &
app_pid=$!
wait "$app_pid" || status=$?
app_pid=""
# A display that died under the window (an assertion of broadwayd's) shows in the window only as its connection lost.
kill -0 "$broadway_pid" 2>/dev/null || { echo "qa: the broadway display ended:" >&2; cat "$root/broadwayd.log" >&2; }

# Each command beside its answer; `quit`'s answer, or the failure that ended the run, comes last.
grep -Ev '^[[:space:]]*(#|$)' "$script" | awk -v answers="$root/answers" '
  { if ((getline answer < answers) > 0) { print "> " $0; print "< " answer } else { print "> " $0 " (not run)" } }
  END { while ((getline answer < answers) > 0) print "< " answer }'
(( status == 0 )) && echo "qa: $(basename -- "$script") passed" || echo "qa: $(basename -- "$script") failed with status $status"
exit "$status"
