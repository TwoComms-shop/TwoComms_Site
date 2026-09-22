#!/usr/bin/env bash

set -euo pipefail
umask 077

BEGIN_MARKER="# BEGIN TWOCOMMS DJANGO61 DURABLE TASKS"
END_MARKER="# END TWOCOMMS DJANGO61 DURABLE TASKS"

usage() { echo "Usage: $0 --check|--install" >&2; exit 64; }
die() { echo "[django61-task-cron] ERROR: $*" >&2; exit 66; }

[ "$#" -eq 1 ] || usage
case "$1" in --check|--install) mode="$1" ;; *) usage ;; esac

# Do not silently fall back to a host/Python/SQLite layout. The caller must
# provide the CloudLinux-bound project root and its selected virtualenv.
: "${TWC_DJANGO_ROOT:?TWC_DJANGO_ROOT is required}"
: "${TWC_PYTHON:?TWC_PYTHON is required}"
DJANGO_ROOT="$TWC_DJANGO_ROOT"
PYTHON_BIN="$TWC_PYTHON"
CLOUDLINUX_PYTHON_WRAPPER="${TWC_CLOUDLINUX_PYTHON_WRAPPER:-/usr/share/l.v.e-manager/utils/python_wrapper}"
CRONTAB_BIN="${TWC_CRONTAB_BIN:-crontab}"
FLOCK_BIN="${TWC_FLOCK_BIN:-/usr/bin/flock}"
TIMEOUT_BIN="${TWC_TIMEOUT_BIN:-/usr/bin/timeout}"

[ -d "$DJANGO_ROOT" ] || die "Django root does not exist: $DJANGO_ROOT"
[ -f "$DJANGO_ROOT/manage.py" ] || die "manage.py does not exist under Django root"
[ -x "$PYTHON_BIN" ] || die "Python is not executable: $PYTHON_BIN"
[ -f "$CLOUDLINUX_PYTHON_WRAPPER" ] && [ ! -L "$CLOUDLINUX_PYTHON_WRAPPER" ] && [ -x "$CLOUDLINUX_PYTHON_WRAPPER" ] || die "CloudLinux Python wrapper is unavailable"
[ -L "$PYTHON_BIN" ] || die "selected Python is not a CloudLinux-bound symlink"
[ "$(readlink "$PYTHON_BIN")" = "$CLOUDLINUX_PYTHON_WRAPPER" ] || die "selected Python is not bound to the CloudLinux wrapper"
[ -x "$FLOCK_BIN" ] || die "flock is not executable: $FLOCK_BIN"
[ -x "$TIMEOUT_BIN" ] || die "timeout is not executable: $TIMEOUT_BIN"
command -v "$CRONTAB_BIN" >/dev/null 2>&1 || die "crontab command is unavailable: $CRONTAB_BIN"

# Resolve the settings and perform one harmless metadata query through the
# exact selected interpreter.  This prevents a plain venv from silently
# selecting SQLite and keeps wrapper/driver errors out of operator output.
if ! preflight_output="$(
  (
    cd "$DJANGO_ROOT"
    DJANGO_ENV=production \
    DJANGO_SETTINGS_MODULE=twocomms.production_settings \
    PYTHONPATH="$DJANGO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -c '
import json
import django
django.setup()
from django.db import connections
from django.db.migrations.recorder import MigrationRecorder
from task_runtime.models import DurableTask

database = connections["default"].settings_dict
if database.get("ENGINE") != "django.db.backends.mysql":
    raise SystemExit("non-mysql backend")
if database.get("CONN_MAX_AGE") != 0:
    raise SystemExit("persistent database connection")
connection = connections["default"]
with connection.cursor() as cursor:
    cursor.execute("SELECT VERSION()")
    version = str(cursor.fetchone()[0])
if "mariadb" not in version.lower():
    raise SystemExit("non-mariadb server")
applied_migrations = MigrationRecorder(connection).applied_migrations()
task_runtime_ready = (
    ("task_runtime", "0001_initial") in applied_migrations
    and DurableTask._meta.db_table in connection.introspection.table_names()
)
print(json.dumps({
    "conn_max_age": database.get("CONN_MAX_AGE"),
    "engine": database.get("ENGINE"),
    "mariadb": True,
    "task_runtime_ready": task_runtime_ready,
}, separators=(",", ":"), sort_keys=True))
'
  ) 2>/dev/null
)"; then
  die "CloudLinux production database preflight failed"
fi
case "$preflight_output" in
  *'"engine":"django.db.backends.mysql"'*) ;;
  *) die "production database preflight did not select Django MySQL" ;;
esac
case "$preflight_output" in
  *'"conn_max_age":0'*) ;;
  *) die "production database preflight requires CONN_MAX_AGE=0" ;;
esac
case "$preflight_output" in
  *'"mariadb":true'*) ;;
  *) die "production database preflight did not confirm MariaDB" ;;
esac
case "$preflight_output" in
  *'"task_runtime_ready":true'*) ;;
  *) die "production database preflight requires task_runtime.0001_initial and DurableTask table" ;;
esac

shell_quote() {
  printf '%q' "$1"
}

root_q="$(shell_quote "$DJANGO_ROOT")"
python_q="$(shell_quote "$PYTHON_BIN")"
manage_q="$(shell_quote "$DJANGO_ROOT/manage.py")"
lock_q="$(shell_quote "$DJANGO_ROOT/tmp/twocomms_heavy_background.lock")"
log_q="$(shell_quote "$DJANGO_ROOT/logs/django61_durable_tasks_cron.log")"
flock_q="$(shell_quote "$FLOCK_BIN")"
timeout_q="$(shell_quote "$TIMEOUT_BIN")"

cron_line="* * * * * cd $root_q && DJANGO_ENV=production DJANGO_SETTINGS_MODULE=twocomms.production_settings exec $flock_q -n -E 75 $lock_q $timeout_q --signal=TERM --kill-after=15s 240s $python_q $manage_q run_durable_tasks --limit 25 --lease-seconds 60 --worker-id=cron-no-send >> $log_q 2>&1"

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/twocomms-django61-task-cron.XXXXXX")"
trap 'rm -rf -- "$tmp_dir"' EXIT INT TERM
current="$tmp_dir/current"
read_error="$tmp_dir/read_error"
expected="$tmp_dir/expected"
candidate="$tmp_dir/candidate"

if ! "$CRONTAB_BIN" -l >"$current" 2>"$read_error"; then
  if grep -qi "no crontab" "$read_error"; then
    : >"$current"
  else
    die "unable to read crontab"
  fi
fi

printf '%s\n%s\n%s\n' "$BEGIN_MARKER" "$cron_line" "$END_MARKER" >"$expected"
begin_count="$(grep -Fxc "$BEGIN_MARKER" "$current" || true)"
end_count="$(grep -Fxc "$END_MARKER" "$current" || true)"
[ "$begin_count" -eq "$end_count" ] && [ "$begin_count" -le 1 ] || {
  die "malformed or duplicate managed block"
}

managed_depth=0
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    "$BEGIN_MARKER") managed_depth=1 ;;
    "$END_MARKER") managed_depth=0 ;;
    *)
      is_comment=0
      trimmed_line="${line#"${line%%[![:space:]]*}"}"
      case "$trimmed_line" in
        \#*) is_comment=1 ;;
      esac
      if [ "$managed_depth" -eq 0 ] && [ "$is_comment" -eq 0 ] && [[ "$line" == *run_durable_tasks* ]]; then
        die "run_durable_tasks owner exists outside the managed block"
      fi
      ;;
  esac
done <"$current"

if [ "$mode" = "--check" ]; then
  [ "$begin_count" -eq 1 ] || {
    echo "[django61-task-cron] DRIFT: managed block is missing" >&2
    exit 1
  }
  awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" \
    '$0 == begin { inside = 1 } inside { print } $0 == end { exit }' \
    "$current" >"$candidate"
  cmp -s "$candidate" "$expected" || {
    echo "[django61-task-cron] DRIFT: managed block differs" >&2
    exit 1
  }
  echo "[django61-task-cron] OK: managed block matches"
  exit 0
fi

mkdir -p "$DJANGO_ROOT/tmp" "$DJANGO_ROOT/logs"
: >"$candidate"
inserted=0
skip_managed=0
while IFS= read -r line || [ -n "$line" ]; do
  if [ "$skip_managed" -eq 1 ]; then
    [ "$line" = "$END_MARKER" ] && skip_managed=0
    continue
  fi
  if [ "$line" = "$BEGIN_MARKER" ]; then
    cat "$expected" >>"$candidate"
    inserted=1
    skip_managed=1
    continue
  fi
  printf '%s\n' "$line" >>"$candidate"
done <"$current"

if [ "$inserted" -eq 0 ]; then cat "$expected" >>"$candidate"; fi
if cmp -s "$candidate" "$current"; then
  echo "[django61-task-cron] OK: managed block already installed"
  exit 0
fi
"$CRONTAB_BIN" "$candidate"
echo "[django61-task-cron] OK: managed block installed; unrelated entries preserved"
