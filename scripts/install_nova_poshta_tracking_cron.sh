#!/usr/bin/env bash

set -euo pipefail
umask 077

BEGIN_MARKER="# BEGIN TWOCOMMS NOVA POSHTA TRACKING"
END_MARKER="# END TWOCOMMS NOVA POSHTA TRACKING"
LEGACY_MARKER="# codex:nova-poshta-tracking"

PROJECT_ROOT="${TWC_PROJECT_ROOT:-/home/qlknpodo/TWC/TwoComms_Site}"
DJANGO_ROOT="${TWC_DJANGO_ROOT:-$PROJECT_ROOT/twocomms}"
PYTHON_BIN="${TWC_PYTHON:-/home/qlknpodo/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/python}"
CRONTAB_BIN="${TWC_CRONTAB_BIN:-crontab}"
FLOCK_BIN="${TWC_FLOCK_BIN:-/usr/bin/flock}"
TIMEOUT_BIN="${TWC_TIMEOUT_BIN:-/usr/bin/timeout}"
NICE_BIN="${TWC_NICE_BIN:-/usr/bin/nice}"
PRODUCTION_ENV_PREFIX="DJANGO_ENV=production DJANGO_SETTINGS_MODULE=twocomms.production_settings"

usage() { echo "Usage: $0 --check|--install" >&2; exit 64; }
[ "$#" -eq 1 ] || usage
case "$1" in --check|--install) mode="$1" ;; *) usage ;; esac

[ -d "$DJANGO_ROOT" ] || { echo "[nova-poshta-cron] ERROR: Django root does not exist: $DJANGO_ROOT" >&2; exit 66; }
[ -x "$PYTHON_BIN" ] || { echo "[nova-poshta-cron] ERROR: Python is not executable: $PYTHON_BIN" >&2; exit 66; }
[ -x "$FLOCK_BIN" ] || { echo "[nova-poshta-cron] ERROR: flock is required: $FLOCK_BIN" >&2; exit 66; }
[ -x "$TIMEOUT_BIN" ] || { echo "[nova-poshta-cron] ERROR: timeout is required: $TIMEOUT_BIN" >&2; exit 66; }
[ -x "$NICE_BIN" ] || { echo "[nova-poshta-cron] ERROR: nice is required: $NICE_BIN" >&2; exit 66; }

cron_line="*/5 * * * * cd $DJANGO_ROOT && $PRODUCTION_ENV_PREFIX $FLOCK_BIN -n -E 75 $DJANGO_ROOT/tmp/twocomms_heavy_background.lock $TIMEOUT_BIN --signal=TERM --kill-after=15s 240s $NICE_BIN -n 10 $PYTHON_BIN manage.py update_tracking_statuses >> $DJANGO_ROOT/logs/nova_poshta_cron.log 2>&1"
previous_cron_line="${cron_line/-n -E 75/-w 50 -E 75}"
legacy_cron_line="*/5 * * * * cd $DJANGO_ROOT && /usr/bin/flock -n $DJANGO_ROOT/tmp/nova_poshta_tracking.lock /usr/bin/nice -n 10 $PYTHON_BIN manage.py update_tracking_statuses >> $DJANGO_ROOT/logs/nova_poshta_cron.log 2>&1"
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/twocomms-np-cron.XXXXXX")"
trap 'rm -rf -- "$tmp_dir"' EXIT INT TERM
current="$tmp_dir/current"
read_error="$tmp_dir/read_error"
expected="$tmp_dir/expected"
candidate="$tmp_dir/candidate"

if ! "$CRONTAB_BIN" -l > "$current" 2> "$read_error"; then
  if grep -qi "no crontab" "$read_error"; then : > "$current"; else
    echo "[nova-poshta-cron] ERROR: unable to read crontab" >&2
    cat "$read_error" >&2
    exit 69
  fi
fi

cat > "$expected" <<EOF
$BEGIN_MARKER
$LEGACY_MARKER
$cron_line
$END_MARKER
EOF

begin_count="$(grep -Fxc "$BEGIN_MARKER" "$current" || true)"
end_count="$(grep -Fxc "$END_MARKER" "$current" || true)"
legacy_count="$(grep -Fxc "$LEGACY_MARKER" "$current" || true)"
[ "$begin_count" -eq "$end_count" ] && [ "$begin_count" -le 1 ] || { echo "[nova-poshta-cron] ERROR: malformed or duplicate managed marker block" >&2; exit 65; }
if [ "$begin_count" -eq 1 ] && [ "$legacy_count" -ne 1 ]; then
  echo "[nova-poshta-cron] ERROR: managed block must contain exactly one job marker" >&2
  exit 65
fi
if [ "$begin_count" -eq 1 ]; then
  begin_line="$(grep -Fnx "$BEGIN_MARKER" "$current" | cut -d: -f1)"
  end_line="$(grep -Fnx "$END_MARKER" "$current" | cut -d: -f1)"
  [ "$begin_line" -lt "$end_line" ] || {
    echo "[nova-poshta-cron] ERROR: managed block markers are out of order" >&2
    exit 65
  }
  managed_block="$tmp_dir/managed_block"
  awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '$0 == begin { inside = 1 } inside { print } $0 == end { exit }' "$current" > "$managed_block"
  block_marker_count="$(grep -Fxc "$LEGACY_MARKER" "$managed_block" || true)"
  [ "$block_marker_count" -eq 1 ] && [ "$legacy_count" -eq "$block_marker_count" ] || {
    echo "[nova-poshta-cron] ERROR: job marker is outside the managed block" >&2
    exit 65
  }
fi
if [ "$begin_count" -eq 0 ] && [ "$legacy_count" -gt 1 ]; then
  echo "[nova-poshta-cron] ERROR: duplicate legacy job markers" >&2
  exit 65
fi

outside_owner_count=0
supported_outside_owner_count=0
inside_managed=0
while IFS= read -r line || [ -n "$line" ]; do
  if [ "$inside_managed" -eq 1 ]; then
    [ "$line" = "$END_MARKER" ] && inside_managed=0
    continue
  fi
  if [ "$line" = "$BEGIN_MARKER" ]; then
    inside_managed=1
    continue
  fi
  trimmed="${line#"${line%%[![:space:]]*}"}"
  case "$trimmed" in
    ""|\#*) continue ;;
    *"manage.py update_tracking_statuses"*)
      outside_owner_count=$((outside_owner_count + 1))
      if [ "$line" = "$cron_line" ] || [ "$line" = "$previous_cron_line" ] || [ "$line" = "$legacy_cron_line" ]; then
        supported_outside_owner_count=$((supported_outside_owner_count + 1))
      fi
      ;;
  esac
done < "$current"
[ "$outside_owner_count" -le 1 ] || {
  echo "[nova-poshta-cron] ERROR: duplicate tracking owners" >&2
  exit 65
}
if [ "$begin_count" -eq 1 ] && [ "$outside_owner_count" -ne 0 ]; then
  echo "[nova-poshta-cron] ERROR: managed block coexists with a loose tracking owner" >&2
  exit 65
fi
if [ "$begin_count" -eq 0 ] && [ "$outside_owner_count" -eq 1 ] && [ "$supported_outside_owner_count" -ne 1 ]; then
  echo "[nova-poshta-cron] ERROR: unsupported loose tracking owner" >&2
  exit 65
fi

if [ "$mode" = "--check" ]; then
  [ "$begin_count" -eq 1 ] || { echo "[nova-poshta-cron] DRIFT: managed block is missing" >&2; exit 1; }
  awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '$0 == begin { inside = 1 } inside { print } $0 == end { exit }' "$current" > "$candidate"
  cmp -s "$candidate" "$expected" || { echo "[nova-poshta-cron] DRIFT: managed block differs from repository configuration" >&2; exit 1; }
  echo "[nova-poshta-cron] OK: managed block matches"
  exit 0
fi

mkdir -p "$DJANGO_ROOT/tmp" "$DJANGO_ROOT/logs"
if [ "$begin_count" -eq 0 ] && [ "$legacy_count" -eq 1 ]; then
  legacy_command="$(awk -v marker="$LEGACY_MARKER" '$0 == marker { getline; print; exit }' "$current")"
  [ "$legacy_command" = "$cron_line" ] || [ "$legacy_command" = "$previous_cron_line" ] || [ "$legacy_command" = "$legacy_cron_line" ] || {
    echo "[nova-poshta-cron] ERROR: legacy marker is followed by an unknown command" >&2
    exit 65
  }
fi

: > "$candidate"
inserted=0
skip_managed=0
skip_legacy_command=0
while IFS= read -r line || [ -n "$line" ]; do
  if [ "$skip_legacy_command" -eq 1 ]; then skip_legacy_command=0; continue; fi
  if [ "$skip_managed" -eq 1 ]; then [ "$line" = "$END_MARKER" ] && skip_managed=0; continue; fi
  if [ "$line" = "$BEGIN_MARKER" ]; then cat "$expected" >> "$candidate"; inserted=1; skip_managed=1; continue; fi
  if [ "$begin_count" -eq 0 ] && [ "$line" = "$LEGACY_MARKER" ]; then cat "$expected" >> "$candidate"; inserted=1; skip_legacy_command=1; continue; fi
  if [ "$begin_count" -eq 0 ] && { [ "$line" = "$cron_line" ] || [ "$line" = "$previous_cron_line" ] || [ "$line" = "$legacy_cron_line" ]; }; then cat "$expected" >> "$candidate"; inserted=1; continue; fi
  printf '%s\n' "$line" >> "$candidate"
done < "$current"

[ "$skip_managed" -eq 0 ] || {
  echo "[nova-poshta-cron] ERROR: managed block did not terminate" >&2
  exit 65
}
if [ "$inserted" -eq 0 ]; then cat "$expected" >> "$candidate"; fi
if cmp -s "$candidate" "$current"; then echo "[nova-poshta-cron] OK: managed block already installed"; exit 0; fi
"$CRONTAB_BIN" "$candidate"
echo "[nova-poshta-cron] OK: managed block installed; unrelated entries preserved"
