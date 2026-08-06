#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
BIN_DIR=${CHAT_ROOM_BIN_DIR:-"$HOME/.local/bin"}
TARGET="$BIN_DIR/chat-room"
SOURCE="$REPO_ROOT/plugins/chat-room/scripts/room.py"

mkdir -p "$BIN_DIR"

if [ -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
  echo "chat-room: refusing to replace non-symlink $TARGET" >&2
  exit 2
fi

ln -sfn "$SOURCE" "$TARGET"
echo "Installed $TARGET -> $SOURCE"
echo "Run: chat-room status"
