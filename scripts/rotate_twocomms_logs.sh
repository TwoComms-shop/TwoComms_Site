#!/usr/bin/env bash
# W3-6 (TD-016/CB-045): user-level log rotation for Hostsila/Passenger.
#
# Install on the server after deploy:
#   mkdir -p "$HOME/log_archives"
#   chmod 700 "$HOME/log_archives"
#   crontab -e
#   10 4 * * * /bin/bash /home/qlknpodo/TWC/TwoComms_Site/scripts/rotate_twocomms_logs.sh >> /home/qlknpodo/log_archives/twocomms/rotate_twocomms_logs.log 2>&1
#
# This script does not need root logrotate. It rotates only regular *.log
# files under the Django project, compresses archives, and keeps recent
# history outside the web-facing tree by default.

set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/qlknpodo/TWC/TwoComms_Site/twocomms}"
ARCHIVE_ROOT="${ARCHIVE_ROOT:-/home/qlknpodo/log_archives/twocomms}"
MAX_BYTES="${MAX_BYTES:-10485760}" # 10 MiB
KEEP_DAYS="${KEEP_DAYS:-30}"

mkdir -p "$ARCHIVE_ROOT"
chmod 700 "$ARCHIVE_ROOT"

if [ ! -d "$PROJECT_ROOT" ]; then
  echo "[rotate_twocomms_logs] ERROR: PROJECT_ROOT does not exist: $PROJECT_ROOT" >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"

rotate_file() {
  local file="$1"
  [ -f "$file" ] || return 0
  [ -s "$file" ] || return 0

  local size
  size="$(wc -c < "$file" | tr -d ' ')"
  if [ "$size" -lt "$MAX_BYTES" ]; then
    return 0
  fi

  local rel safe archive tmp
  rel="${file#$PROJECT_ROOT/}"
  safe="${rel//\//__}"
  archive="$ARCHIVE_ROOT/${safe}.${timestamp}.log.gz"
  tmp="${archive}.tmp"

  gzip -c "$file" > "$tmp"
  gzip -t "$tmp"
  mv "$tmp" "$archive"
  chmod 600 "$archive"
  : > "$file"
  chmod 600 "$file" || true
  echo "[rotate_twocomms_logs] rotated $rel ($size bytes) -> $archive"
}

while IFS= read -r -d '' file; do
  case "$file" in
    "$PROJECT_ROOT"/.venv/*|"$PROJECT_ROOT"/node_modules/*|"$PROJECT_ROOT"/.git/*)
      continue
      ;;
  esac
  rotate_file "$file"
done < <(find "$PROJECT_ROOT" -type f -name '*.log' -print0)

find "$ARCHIVE_ROOT" -type f -name '*.log.gz' -mtime +"$KEEP_DAYS" -delete
echo "[rotate_twocomms_logs] OK $(date -Is)"
