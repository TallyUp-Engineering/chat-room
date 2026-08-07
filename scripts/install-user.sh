#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
BIN_DIR=${CHAT_ROOM_BIN_DIR:-"$HOME/.local/bin"}
TARGET="$BIN_DIR/chat-room"
SOURCE="$REPO_ROOT/plugins/chat-room/scripts/room.py"
RUNTIME_DIR=${CHAT_ROOM_RUNTIME_DIR:-"$HOME/.local/share/chat-room/runtime"}

mkdir -p "$BIN_DIR"
# Chat Room itself needs nothing beyond the standard library. The runtime exists so the
# optional transcript index has somewhere to land.
python3 -m venv "$RUNTIME_DIR"

if [ -L "$TARGET" ]; then
  rm "$TARGET"
elif [ -e "$TARGET" ] && ! grep -q "chat-room managed launcher" "$TARGET"; then
  echo "chat-room: refusing to replace existing $TARGET" >&2
  exit 2
fi
TEMPORARY="$TARGET.tmp.$$"
printf '%s\n' '#!/bin/sh' '# chat-room managed launcher' "exec \"$RUNTIME_DIR/bin/python\" \"$SOURCE\" \"\$@\"" > "$TEMPORARY"
chmod 0755 "$TEMPORARY"
mv "$TEMPORARY" "$TARGET"
echo "Installed $TARGET with local runtime $RUNTIME_DIR"
echo "Run: chat-room status"
