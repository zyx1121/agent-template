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
[ "$(stat -c %a claude)" = 700 ] || fail "claude dir mode is $(stat -c %a claude)"
[ "$(stat -c %U:%a ..x)" = agent:600 ] || fail "run/..x is $(stat -c %U:%a ..x), want agent:600"
[ "$(stat -c %a guest/outbox/tgt)" = 644 ] || fail "the symlink run/..l made root change the mode of its target to $(stat -c %a guest/outbox/tgt)"
[ "$(stat -c %U:%G guest/outbox/tgt)" = root:root ] || fail "the symlink run/..l made root change the owner of its target"
[ -L ..l ] || fail "run/..l is no longer a symlink"
[ "$(stat -c %U ..l)" = agent ] || fail "the symlink run/..l is owned by $(stat -c %U ..l), want agent"
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
