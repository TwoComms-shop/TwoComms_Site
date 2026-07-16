#!/usr/bin/env bash
# W0-3 (TD-020/TECH-042): verified MySQL backups for every TwoComms database.
#
# Production invocation (database names are not credentials):
#   /bin/bash /home/qlknpodo/TWC/TwoComms_Site/scripts/backup_mysql.sh \
#     MAIN_DATABASE DTF_DATABASE
#
# Credentials come only from TWC_MYSQL_DEFAULTS_FILE (default: $HOME/.my.cnf),
# which must be private. Archives are staged as .tmp files, validated as a
# complete batch, and only then atomically published one by one.

set -euo pipefail
umask 077

BACKUP_ROOT="${TWC_BACKUP_DIR:-$HOME/db_backups}"
DAILY_DIR="$BACKUP_ROOT/daily"
WEEKLY_DIR="$BACKUP_ROOT/weekly"
DEFAULTS_FILE="${TWC_MYSQL_DEFAULTS_FILE:-$HOME/.my.cnf}"
MIN_DUMP_BYTES="${TWC_MIN_DUMP_BYTES:-10240}"
DAILY_RETENTION_DAYS="${TWC_DAILY_RETENTION_DAYS:-14}"
WEEKLY_RETENTION_DAYS="${TWC_WEEKLY_RETENTION_DAYS:-35}"
STAMP="${TWC_BACKUP_STAMP:-$(date +%Y%m%d)}"
DOW="${TWC_BACKUP_DOW:-$(date +%u)}"

if [ "$#" -gt 0 ]; then
  DB_NAMES=("$@")
elif [ -n "${TWC_DB_NAMES:-}" ]; then
  case "$TWC_DB_NAMES" in
    ,*|*,|*,,*)
      echo "[backup_mysql] ERROR: database list contains an empty name" >&2
      exit 64
      ;;
  esac
  IFS=',' read -r -a DB_NAMES <<< "$TWC_DB_NAMES"
else
  echo "[backup_mysql] ERROR: explicit database names are required" >&2
  exit 64
fi

if [ "${#DB_NAMES[@]}" -eq 0 ]; then
  echo "[backup_mysql] ERROR: at least one database name is required" >&2
  exit 64
fi

if [ ! -r "$DEFAULTS_FILE" ]; then
  echo "[backup_mysql] ERROR: private MySQL defaults file is not readable" >&2
  exit 66
fi

file_mode() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

defaults_mode="$(file_mode "$DEFAULTS_FILE")"
case "$defaults_mode" in
  400|600) ;;
  *)
    echo "[backup_mysql] ERROR: MySQL defaults file must have mode 0600 or 0400" >&2
    exit 78
    ;;
esac

case "$MIN_DUMP_BYTES" in
  ''|*[!0-9]*)
    echo "[backup_mysql] ERROR: TWC_MIN_DUMP_BYTES must be a positive integer" >&2
    exit 64
    ;;
esac
if [ "$MIN_DUMP_BYTES" -lt 1 ]; then
  echo "[backup_mysql] ERROR: TWC_MIN_DUMP_BYTES must be a positive integer" >&2
  exit 64
fi

case "$STAMP" in
  *[!0-9]*|'')
    echo "[backup_mysql] ERROR: backup stamp must be numeric" >&2
    exit 64
    ;;
esac
case "$DOW" in
  1|2|3|4|5|6|7) ;;
  *)
    echo "[backup_mysql] ERROR: backup weekday must be in 1..7" >&2
    exit 64
    ;;
esac

for db_name in "${DB_NAMES[@]}"; do
  if [[ ! "$db_name" =~ ^[A-Za-z0-9_][A-Za-z0-9_-]*$ ]]; then
    echo "[backup_mysql] ERROR: unsafe or empty database name" >&2
    exit 64
  fi
done

mkdir -p "$DAILY_DIR" "$WEEKLY_DIR"
chmod 700 "$BACKUP_ROOT" "$DAILY_DIR" "$WEEKLY_DIR"

tmp_files=()
final_files=()
fallback_lock_dir=""

cleanup() {
  local tmp_file
  for tmp_file in "${tmp_files[@]:-}"; do
    if [ -n "$tmp_file" ]; then
      rm -f -- "$tmp_file"
    fi
  done
  if [ -n "$fallback_lock_dir" ]; then
    rmdir "$fallback_lock_dir" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

lock_file="$BACKUP_ROOT/.backup_mysql.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$lock_file"
  if ! flock -n 9; then
    echo "[backup_mysql] ERROR: backup already running" >&2
    exit 75
  fi
else
  # Portable local-development fallback. Production acceptance requires flock.
  fallback_lock_dir="${lock_file}.d"
  if ! mkdir "$fallback_lock_dir" 2>/dev/null; then
    echo "[backup_mysql] ERROR: backup already running" >&2
    exit 75
  fi
fi

file_size() {
  stat -c '%s' "$1" 2>/dev/null || stat -f '%z' "$1"
}

for db_name in "${DB_NAMES[@]}"; do
  final_file="$DAILY_DIR/${db_name}-${STAMP}.sql.gz"
  tmp_file="${final_file}.tmp.$$"
  tmp_files+=("$tmp_file")
  final_files+=("$final_file")

  if ! mysqldump \
    --defaults-extra-file="$DEFAULTS_FILE" \
    --single-transaction \
    --quick \
    --routines \
    --triggers \
    --no-tablespaces \
    "$db_name" | gzip -9 > "$tmp_file"; then
    echo "[backup_mysql] ERROR: mysqldump pipeline failed" >&2
    exit 1
  fi

  dump_size="$(file_size "$tmp_file")"
  if [ "$dump_size" -lt "$MIN_DUMP_BYTES" ]; then
    echo "[backup_mysql] ERROR: dump is suspiciously small" >&2
    exit 1
  fi
  chmod 600 "$tmp_file"
  if ! gzip -t "$tmp_file"; then
    echo "[backup_mysql] ERROR: gzip validation failed" >&2
    exit 1
  fi
done

for index in "${!tmp_files[@]}"; do
  mv -f -- "${tmp_files[$index]}" "${final_files[$index]}"
  chmod 600 "${final_files[$index]}"
done

for db_name in "${DB_NAMES[@]}"; do
  final_file="$DAILY_DIR/${db_name}-${STAMP}.sql.gz"
  if [ "$DOW" = "7" ]; then
    cp -p -- "$final_file" "$WEEKLY_DIR/"
  fi
  find "$DAILY_DIR" -type f -name "${db_name}-*.sql.gz" -mtime "+${DAILY_RETENTION_DAYS}" -delete
  find "$WEEKLY_DIR" -type f -name "${db_name}-*.sql.gz" -mtime "+${WEEKLY_RETENTION_DAYS}" -delete
done

success_tmp="$BACKUP_ROOT/.last_success.tmp.$$"
tmp_files+=("$success_tmp")
printf '%s databases=%s\n' "$(date -Is)" "${#DB_NAMES[@]}" > "$success_tmp"
chmod 600 "$success_tmp"
mv -f -- "$success_tmp" "$BACKUP_ROOT/last_success"

echo "[backup_mysql] OK: ${#DB_NAMES[@]} database archive(s) published at $(date -Is)"
