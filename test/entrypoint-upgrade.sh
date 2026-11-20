#!/bin/sh
# Starts the entrypoint on a run/ volume a 1.x guest could have left (it ran as the agent uid) and
# checks that root follows none of the planted links. Usage: test/entrypoint-upgrade.sh <image>
set -eu
IMG=${1:?usage: $0 <image>}
DOCKER=${DOCKER:-docker}
VOL=$($DOCKER volume create)
trap '$DOCKER volume rm -f "$VOL" >/dev/null' EXIT
as_root() { $DOCKER run --rm --entrypoint sh -v "$VOL:/app/run" "$IMG" -c "$1"; }

as_root '
set -eu
cd /app/run
mkdir -p claude guest/work guest/config
echo owner > claude/settings.json
echo secret > claude/.credentials.json
# A hardlink to an owner file inside the guest work dir.
ln claude/settings.json guest/work/x
# A symlink where a guest directory belongs.
ln -s /app/run/claude guest/sched
# The legacy guest dir as a symlink to the owner state.
ln -s claude claude-guest
# Links in guest/mcp that the bot would write through.
mkdir -p guest/mcp
ln -s /app/run/claude/settings.json guest/mcp/mcp-runtime-1.json
ln claude/.credentials.json guest/mcp/mcp-runtime-2.json
# Planted entries under guest/telegram: a hardlink to an owner file at <chat>/<id>, a chat entry that is a
# symlink to an owner dir, and a plain file where a chat dir belongs.
mkdir -p claude/tdir guest/telegram/100
echo towner > claude/tdir/f
ln claude/settings.json guest/telegram/100/5
ln -s /app/run/claude/tdir guest/telegram/200
echo plain > guest/telegram/300
# A FIFO at a future message id, and a chat dir (and a file in it) left writable by others.
mkfifo guest/telegram/100/6
mkdir -p guest/telegram/400
echo att > guest/telegram/400/1
chmod 777 guest/telegram/400
chmod 666 guest/telegram/400/1
chown -R 1000:1000 /app/run
# A name that starts with two dots, left readable and not owned by agent.
echo dots > ..x
chown root:root ..x
chmod 644 ..x
# A two-dot symlink to a file outside the owner tree, and an owner file hardlinked to a file outside it.
# guest/outbox is not recursed into by the owner loop, so it holds the outside files.
mkdir -p guest/outbox claude/sub
echo target > guest/outbox/tgt
chmod 644 guest/outbox/tgt
chown root:root guest/outbox/tgt
ln -s guest/outbox/tgt ..l
# Top-level state links a 1.x guest could plant to redirect an owner write into guest/outbox or the owner tree.
ln -s guest/outbox session-1
ln -s claude/settings.json guest-fired.json
echo shared > guest/outbox/outside
chmod 644 guest/outbox/outside
chown root:root guest/outbox/outside
ln guest/outbox/outside claude/sub/linked
# Symlinks to an outside file at the staging names an older entrypoint used for that copy (low PIDs).
echo planted > guest/outbox/plant
chmod 644 guest/outbox/plant
chown root:root guest/outbox/plant
for n in $(seq 1 300); do ln -s /app/run/guest/outbox/plant "claude/sub/linked.unlink.$n"; done
'
$DOCKER run --rm -v "$VOL:/app/run" "$IMG" true

as_root '
fail() { echo "entrypoint-upgrade: $1" >&2; exit 1; }
cd /app/run
[ "$(stat -c %U claude/settings.json)" = agent ] || fail "owner settings.json is owned by $(stat -c %U claude/settings.json)"
[ "$(stat -c %h claude/settings.json)" = 1 ] || fail "the hardlink to owner settings.json survived"
[ -e guest/work/x ] && fail "guest/work/x was not removed"
[ -L guest/sched ] && fail "guest/sched is still a symlink"
[ -d guest/sched ] || fail "guest/sched was not recreated as a directory"
[ -e guest/telegram/100/5 ] && fail "the hardlink under guest/telegram survived"
[ -L guest/telegram/200 ] || [ -e guest/telegram/200 ] && fail "the symlinked chat dir under guest/telegram survived"
[ -e guest/telegram/300 ] && fail "the plain file at a chat entry survived"
[ -d guest/telegram/100 ] || fail "a real chat dir under guest/telegram was removed"
[ -e guest/telegram/100/6 ] || [ -p guest/telegram/100/6 ] && fail "the FIFO under guest/telegram survived"
[ "$(stat -c %U:%a guest/telegram/400)" = agent:755 ] || fail "chat dir 400 is $(stat -c %U:%a guest/telegram/400), want agent:755"
[ "$(stat -c %U:%a guest/telegram/400/1)" = agent:644 ] || fail "guest/telegram/400/1 is $(stat -c %U:%a guest/telegram/400/1), want agent:644"
[ "$(cat guest/telegram/400/1)" = att ] || fail "an attachment under guest/telegram lost its content"
[ -z "$(find guest/telegram -perm /022)" ] || fail "guest/telegram still has entries writable by group or others"
[ "$(cat claude/tdir/f)" = towner ] || fail "the target of the symlinked chat dir changed"
[ "$(stat -c %U:%a claude/tdir/f)" = agent:600 ] || fail "claude/tdir/f is $(stat -c %U:%a claude/tdir/f), want agent:600"
[ "$(stat -c %a claude)" = 700 ] || fail "claude dir mode is $(stat -c %a claude)"
[ "$(stat -c %U:%a ..x)" = agent:600 ] || fail "run/..x is $(stat -c %U:%a ..x), want agent:600"
[ "$(stat -c %a guest/outbox/tgt)" = 644 ] || fail "the symlink run/..l made root change the mode of its target to $(stat -c %a guest/outbox/tgt)"
[ "$(stat -c %U:%G guest/outbox/tgt)" = root:root ] || fail "the symlink run/..l made root change the owner of its target"
[ -e ..l ] || [ -L ..l ] && fail "the top-level symlink run/..l survived"
[ -e session-1 ] || [ -L session-1 ] && fail "the top-level symlink run/session-1 survived"
[ -e guest-fired.json ] || [ -L guest-fired.json ] && fail "the top-level symlink run/guest-fired.json survived"
[ "$(stat -c %a guest/outbox/outside)" = 644 ] || fail "an owner-side hardlink made root change the mode of outside to $(stat -c %a guest/outbox/outside)"
[ "$(stat -c %U guest/outbox/outside)" = root ] || fail "an owner-side hardlink made root change the owner of outside"
[ "$(cat claude/sub/linked)" = shared ] || fail "the hardlinked owner file lost its content"
[ "$(stat -c %h claude/sub/linked)" = 1 ] || fail "the owner-side hardlink survived"
[ "$(cat guest/outbox/plant)" = planted ] || fail "root wrote through a symlink planted at a staging name"
[ "$(stat -c %U guest/outbox/plant)" = root ] || fail "a symlink planted at a staging name made root change the owner of its target"
[ -f claude/sub/linked ] && [ ! -L claude/sub/linked ] || fail "claude/sub/linked is no longer a regular file"
[ "$(stat -c %U:%a claude/sub/linked)" = agent:600 ] || fail "claude/sub/linked is $(stat -c %U:%a claude/sub/linked), want agent:600"
[ -e guest/mcp/mcp-runtime-1.json ] || [ -L guest/mcp/mcp-runtime-1.json ] && fail "the symlink in guest/mcp survived"
[ -e guest/mcp/mcp-runtime-2.json ] && fail "the hardlink in guest/mcp survived"
[ "$(cat claude/settings.json)" = owner ] || fail "owner settings.json changed"
[ "$(cat claude/.credentials.json)" = secret ] || fail "owner credentials changed"
[ -e claude-guest ] && fail "the symlinked claude-guest was not removed"
[ -e guest/config/.credentials.json ] && fail "owner credentials were copied into guest/config"
[ -e guest/config/settings.json ] && fail "owner settings were copied into guest/config"
echo "entrypoint-upgrade: ok"
'

# A 2.x guest hardlinks its own files (a local git clone does): a restart must keep them.
$DOCKER run --rm --user guest --entrypoint sh -v "$VOL:/app/run" "$IMG" -c '
set -eu
cd /app/run/guest/work
mkdir a && echo data > a/obj && ln a/obj b-obj
'
$DOCKER run --rm -v "$VOL:/app/run" "$IMG" true
as_root '
fail() { echo "entrypoint-upgrade: $1" >&2; exit 1; }
cd /app/run/guest/work
[ "$(cat a/obj 2>/dev/null)" = data ] || fail "the guest hardlink a/obj was removed on restart"
[ "$(cat b-obj 2>/dev/null)" = data ] || fail "the guest hardlink b-obj was removed on restart"
echo "entrypoint-upgrade: guest hardlinks ok"
'

# A real legacy dir that hardlinks an owner file, next to a guest/config a 1.x guest planted links in:
# no owner file may be copied into guest/config or written through a planted link.
as_root '
set -eu
cd /app/run
rm -rf guest
mkdir -p claude-guest guest/config
ln claude/.credentials.json claude-guest/creds
echo mine > claude-guest/own.json
echo guest-settings > claude-guest/settings.json
echo guest-creds > claude-guest/.credentials.json
ln -s /app/run/claude/settings.json guest/config/settings.json
ln claude/.credentials.json guest/config/.credentials.json
chown -R 1000:1000 /app/run
'
$DOCKER run --rm -v "$VOL:/app/run" "$IMG" true
as_root '
fail() { echo "entrypoint-upgrade: $1" >&2; exit 1; }
cd /app/run
[ -e claude-guest ] && fail "claude-guest was not migrated"
[ "$(cat claude/settings.json)" = owner ] || fail "owner settings.json was written through a planted symlink"
[ "$(cat claude/.credentials.json)" = secret ] || fail "owner credentials were written through a planted hardlink"
[ "$(stat -c %h claude/.credentials.json)" = 1 ] || fail "a hardlink to owner credentials survived"
[ -e guest/config/creds ] && fail "a hardlinked owner file was copied into guest/config"
[ "$(cat guest/config/own.json)" = mine ] || fail "guest state was not migrated"
[ "$(stat -c %U guest/config/own.json)" = guest ] || fail "migrated guest state is not owned by guest"
[ -L guest/config/settings.json ] && fail "the planted symlink in guest/config survived"
[ "$(cat guest/config/settings.json)" = guest-settings ] || fail "guest settings.json was not migrated"
[ "$(stat -c %U guest/config/settings.json)" = guest ] || fail "migrated settings.json is not owned by guest"
[ "$(cat guest/config/.credentials.json)" = guest-creds ] || fail "guest credentials were not migrated"
echo "entrypoint-upgrade: migration ok"
'

# Refusals: exit 1, the diagnostic on stderr only, nothing on stdout, no argument echoed, and the seeded
# run/ volume left exactly as it was (the guard must run before the migration and chown/chmod).
snapshot() { as_root 'find /app/run -exec stat -c "%n %U:%G %a %h %s %Y %i" {} + | sort'; }
errf=$(mktemp)
trap '$DOCKER volume rm -f "$VOL" >/dev/null; rm -f "$errf"' EXIT
refuse() { # user, expected message, arguments...
  u=$1 want=$2
  shift 2
  before=$(snapshot)
  rc=0
  out=$($DOCKER run --rm --user "$u" -e SECRET_PROBE=leak-me -v "$VOL:/app/run" --entrypoint agent-entrypoint "$IMG" "$@" 2>"$errf") || rc=$?
  err=$(cat "$errf")
  [ "$rc" = 1 ] || { echo "entrypoint-upgrade: $u refusal exited $rc, want 1" >&2; exit 1; }
  [ -z "$out" ] || { echo "entrypoint-upgrade: $u refusal wrote to stdout" >&2; exit 1; }
  case $err in *"$want"*) ;; *) echo "entrypoint-upgrade: $u refusal message missing on stderr" >&2; exit 1 ;; esac
  case $out$err in *leak-me*) echo "entrypoint-upgrade: $u refusal printed the environment or an argument" >&2; exit 1 ;; esac
  [ "$(snapshot)" = "$before" ] || { echo "entrypoint-upgrade: $u refusal changed run/" >&2; exit 1; }
}

# No command: refuse to start, and never print the environment (root and non-root paths).
for u in root guest; do refuse "$u" "no command given, refusing to start."; done
echo "entrypoint-upgrade: no-command ok"

# A command made only of assignments: `env` would take them as variables and print the environment.
# The probe also sits in a rejected argument, which the message must not echo.
for u in root guest; do refuse "$u" "only assignments and no program" FOO=bar TOKEN=leak-me; done
echo "entrypoint-upgrade: assignment-command ok"

# An assignment followed by a program still starts on the root path, and the assignment reaches the program.
$DOCKER run --rm "$IMG" FOO=bar sh -c 'test "$FOO" = bar' || { echo "entrypoint-upgrade: FOO=bar before a program as root did not start or did not reach it" >&2; exit 1; }
echo "entrypoint-upgrade: assignment-with-program ok"
