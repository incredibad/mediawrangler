#!/bin/sh
# Run as PUID:PGID (default 1000:1000) and make sure the data volume is theirs.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
DATA="${MW_DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA"
  # Only touch files that aren't already owned correctly.
  find "$DATA" \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} +
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi

exec "$@"
