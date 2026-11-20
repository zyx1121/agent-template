#!/bin/sh
# Checks the entrypoint's PATH split: root must not resolve commands through /home/agent/.local/bin,
# and the bot must still get the full PATH after the drop. Usage: test/path-split.sh <image>
set -eu
IMG=${1:?usage: $0 <image>}
DOCKER=${DOCKER:-docker}
fail() { echo "path-split: $1" >&2; exit 1; }

# A sentinel chown in the agent-writable bin dir. The entrypoint runs chown as root before the drop,
# so the marker appears if root resolves it through that dir. The container is the real entrypoint,
# started after the sentinel is planted; the last command runs as the bot and reports the marker.
$DOCKER run --rm --entrypoint sh "$IMG" -c '
set -eu
MARK=/tmp/path-split-marker
mkdir -p /home/agent/.local/bin
printf "#!/bin/sh\ntouch %s\nexec /usr/bin/chown \"\$@\"\n" "$MARK" > /home/agent/.local/bin/chown
chmod 755 /home/agent/.local/bin/chown
# The sentinel must win under the image PATH, or this test proves nothing.
chown root:root /tmp
[ -e "$MARK" ] || { echo "path-split: the sentinel chown is not first on the image PATH" >&2; exit 1; }
rm -f "$MARK"
exec /usr/bin/tini -- /usr/local/bin/agent-entrypoint sh -c "
  [ \"\$(id -un)\" = agent ] || { echo \"path-split: the bot runs as \$(id -un)\" >&2; exit 1; }
  [ ! -e $MARK ] || { echo \"path-split: root ran the sentinel chown from /home/agent/.local/bin\" >&2; exit 1; }
"
'

# The bot keeps the full PATH after the drop, through the entrypoint.
$DOCKER run --rm "$IMG" sh -c 'command -v claude' >/dev/null || fail "claude is not on the bot's PATH"
echo "path-split: ok"
