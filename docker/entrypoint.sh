#!/bin/sh
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
mkdir -p /data

group_name="$(getent group "$PGID" | cut -d: -f1 || true)"
if [ -z "$group_name" ]; then
  group_name=seasonkeeper
  groupadd -g "$PGID" "$group_name"
fi

user_name="$(getent passwd "$PUID" | cut -d: -f1 || true)"
if [ -z "$user_name" ]; then
  user_name=seasonkeeper
  useradd -u "$PUID" -g "$PGID" -M -s /usr/sbin/nologin "$user_name"
fi

chown -R "$PUID:$PGID" /data
exec gosu "$user_name" "$@"
