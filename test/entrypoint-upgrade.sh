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
chown -R 1000:1000 /app/run
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
[ -e claude-guest ] && fail "the symlinked claude-guest was not removed"
[ -e guest/config/.credentials.json ] && fail "owner credentials were copied into guest/config"
[ -e guest/config/settings.json ] && fail "owner settings were copied into guest/config"
echo "entrypoint-upgrade: ok"
'

# A real legacy dir that hardlinks an owner file: the file must not be copied into guest/config.
as_root '
set -eu
cd /app/run
rm -rf guest
mkdir claude-guest
ln claude/.credentials.json claude-guest/creds
echo mine > claude-guest/own.json
chown -R 1000:1000 /app/run
'
$DOCKER run --rm -v "$VOL:/app/run" "$IMG" true
as_root '
fail() { echo "entrypoint-upgrade: $1" >&2; exit 1; }
cd /app/run
[ -e claude-guest ] && fail "claude-guest was not migrated"
[ -e guest/config/creds ] && fail "a hardlinked owner file was copied into guest/config"
[ "$(cat guest/config/own.json)" = mine ] || fail "guest state was not migrated"
[ "$(stat -c %U guest/config/own.json)" = guest ] || fail "migrated guest state is not owned by guest"
echo "entrypoint-upgrade: migration ok"
'
