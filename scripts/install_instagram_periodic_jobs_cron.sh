#!/usr/bin/env bash

set -euo pipefail
umask 077

BEGIN_MARKER="# BEGIN TWOCOMMS INSTAGRAM PERIODIC JOBS v1"
END_MARKER="# END TWOCOMMS INSTAGRAM PERIODIC JOBS v1"
BEGIN_PREFIX="# BEGIN TWOCOMMS INSTAGRAM PERIODIC JOBS"
JOB_MARKER="# codex:instagram-periodic-coordinator"

DJANGO_ROOT="${TWC_DJANGO_ROOT:-/home/qlknpodo/TWC/TwoComms_Site/twocomms}"
PYTHON_BIN="${TWC_PYTHON:-/home/qlknpodo/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/python}"
CRONTAB_BIN="${TWC_CRONTAB_BIN:-crontab}"
FLOCK_BIN="${TWC_FLOCK_BIN:-/usr/bin/flock}"
TIMEOUT_BIN="${TWC_TIMEOUT_BIN:-/usr/bin/timeout}"
PRODUCTION_ENV_PREFIX="DJANGO_ENV=production DJANGO_SETTINGS_MODULE=twocomms.production_settings"

usage() { echo "Usage: $0 --check|--install|--check-rollback|--rollback" >&2; exit 64; }
[ "$#" -eq 1 ] || usage
case "$1" in --check|--install|--check-rollback|--rollback) mode="$1" ;; *) usage ;; esac

contract_error() { echo "[instagram-periodic-cron] ERROR: $*" >&2; exit 65; }
config_error() { echo "[instagram-periodic-cron] ERROR: $*" >&2; exit 66; }

validate_path() {
  local label="$1"
  local value="$2"
  case "$value" in /*) ;; *) config_error "$label must be an absolute path" ;; esac
  case "$value" in *[!A-Za-z0-9_./-]*) config_error "$label contains unsafe characters" ;; esac
  [ "$value" != "/" ] || config_error "$label must not be the filesystem root"
}

validate_path "Django root" "$DJANGO_ROOT"
validate_path "Python executable" "$PYTHON_BIN"
validate_path "flock executable" "$FLOCK_BIN"
validate_path "timeout executable" "$TIMEOUT_BIN"
[ -d "$DJANGO_ROOT" ] || config_error "Django root does not exist: $DJANGO_ROOT"
[ -x "$PYTHON_BIN" ] || config_error "Python is not executable: $PYTHON_BIN"
[ -x "$FLOCK_BIN" ] || config_error "flock is required: $FLOCK_BIN"
[ -x "$TIMEOUT_BIN" ] || config_error "timeout is required: $TIMEOUT_BIN"
command -v "$CRONTAB_BIN" >/dev/null 2>&1 || config_error "crontab command is unavailable"

cron_line="* * * * * cd $DJANGO_ROOT && $PRODUCTION_ENV_PREFIX $FLOCK_BIN -n -E 75 $DJANGO_ROOT/tmp/twocomms_heavy_background.lock $TIMEOUT_BIN --signal=TERM --kill-after=15s 600s $PYTHON_BIN manage.py run_instagram_periodic_jobs --budget-seconds 540 >> $DJANGO_ROOT/logs/instagram_periodic_coordinator.log 2>&1"
rollback_order_line="*/2 * * * * cd $DJANGO_ROOT && $PRODUCTION_ENV_PREFIX $FLOCK_BIN -n -E 75 $DJANGO_ROOT/tmp/order_telegram_reconcile.lock $TIMEOUT_BIN --signal=TERM --kill-after=15s 90s $PYTHON_BIN manage.py reconcile_order_telegram_notifications --max-age-hours 168 --min-age-seconds 60 --limit 50 >> $DJANGO_ROOT/logs/order_telegram_reconcile.log 2>&1"
rollback_checkout_line="*/2 * * * * cd $DJANGO_ROOT && $PRODUCTION_ENV_PREFIX $FLOCK_BIN -n -E 75 $DJANGO_ROOT/tmp/ig_checkout_reconcile.lock $TIMEOUT_BIN --signal=TERM --kill-after=15s 90s $PYTHON_BIN manage.py reconcile_ig_checkout --limit 100 >> $DJANGO_ROOT/logs/ig_checkout_reconcile.log 2>&1"
rollback_fulfillment_line="*/2 * * * * cd $DJANGO_ROOT && $PRODUCTION_ENV_PREFIX $FLOCK_BIN -n -E 75 $DJANGO_ROOT/tmp/ig_order_fulfillment.lock $TIMEOUT_BIN --signal=TERM --kill-after=15s 90s $PYTHON_BIN manage.py reconcile_ig_order_fulfillment --limit 100 >> $DJANGO_ROOT/logs/ig_order_fulfillment.log 2>&1"
rollback_payments_line="*/4 * * * * cd $DJANGO_ROOT && $PRODUCTION_ENV_PREFIX $FLOCK_BIN -n -E 75 $DJANGO_ROOT/tmp/poll_ig_deal_payments.lock $TIMEOUT_BIN --signal=TERM --kill-after=15s 180s $PYTHON_BIN manage.py poll_ig_deal_payments --limit 50 >> $DJANGO_ROOT/logs/poll_ig_deal_payments.log 2>&1"
rollback_call_line="*/5 * * * * cd $DJANGO_ROOT && $PRODUCTION_ENV_PREFIX $FLOCK_BIN -n -E 75 $DJANGO_ROOT/tmp/run_call_ai_analyses.lock $TIMEOUT_BIN --signal=TERM --kill-after=15s 240s $PYTHON_BIN manage.py run_call_ai_analyses --limit 1 >> $DJANGO_ROOT/logs/run_call_ai_analyses.log 2>&1"

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/twocomms-instagram-periodic-cron.XXXXXX")"
trap 'rm -rf -- "$tmp_dir"' EXIT INT TERM
current="$tmp_dir/current"
read_error="$tmp_dir/read_error"
expected="$tmp_dir/expected"
candidate="$tmp_dir/candidate"

if ! "$CRONTAB_BIN" -l >"$current" 2>"$read_error"; then
  if grep -qi "no crontab" "$read_error"; then : >"$current"; else
    echo "[instagram-periodic-cron] ERROR: unable to read crontab" >&2
    cat "$read_error" >&2
    exit 69
  fi
fi

if [ "$mode" = "--rollback" ] || [ "$mode" = "--check-rollback" ]; then
cat >"$expected" <<EOF
$BEGIN_MARKER
# codex:order-telegram-reconcile
$rollback_order_line
# codex:ig-checkout-reconcile
$rollback_checkout_line
# codex:ig-order-fulfillment
$rollback_fulfillment_line
# codex:ig-deal-payments
$rollback_payments_line
# codex:call-auto-analysis
$rollback_call_line
$END_MARKER
EOF
else
cat >"$expected" <<EOF
$BEGIN_MARKER
$JOB_MARKER
$cron_line
$END_MARKER
EOF
fi

begin_count="$(grep -Fxc "$BEGIN_MARKER" "$current" || true)"
end_count="$(grep -Fxc "$END_MARKER" "$current" || true)"
[ "$begin_count" -eq "$end_count" ] && [ "$begin_count" -le 1 ] || contract_error "malformed or duplicate managed block"
unsupported_markers="$(awk -v prefix="$BEGIN_PREFIX" -v current="$BEGIN_MARKER" '
  index($0, prefix) == 1 && $0 != current { count++ }
  END { print count + 0 }
' "$current")"
[ "$unsupported_markers" -eq 0 ] || contract_error "unsupported managed block version"
if [ "$begin_count" -eq 1 ]; then
  begin_line="$(grep -Fnx "$BEGIN_MARKER" "$current" | cut -d: -f1)"
  end_line="$(grep -Fnx "$END_MARKER" "$current" | cut -d: -f1)"
  [ "$begin_line" -lt "$end_line" ] || contract_error "managed block markers are out of order"
fi

owner_kind=""
is_owner_line() {
  local line="$1"
  owner_kind=""
  case "$line" in
    \#*|"") return 1 ;;
    *"manage.py run_instagram_periodic_jobs"*) owner_kind="coordinator" ;;
    *"manage.py reconcile_order_telegram_notifications"*) owner_kind="order" ;;
    *"manage.py reconcile_ig_checkout"*) owner_kind="checkout" ;;
    *"manage.py reconcile_ig_order_fulfillment"*) owner_kind="fulfillment" ;;
    *"manage.py poll_ig_deal_payments"*) owner_kind="payments" ;;
    *"manage.py run_call_ai_analyses"*) owner_kind="call_analysis" ;;
    *"manage.py check_ig_gemini_metadata_health"*) owner_kind="gemini_metadata" ;;
    *) return 1 ;;
  esac
  return 0
}

is_supported_legacy_line() {
  local line="$1"
  case "$line" in
    "*/2 * * * * "*"$DJANGO_ROOT"*"manage.py reconcile_order_telegram_notifications --max-age-hours 168 --min-age-seconds 60 --limit 50 "*) return 0 ;;
    "*/2 * * * * "*"$DJANGO_ROOT"*"manage.py reconcile_ig_checkout --limit 100 "*) return 0 ;;
    "*/2 * * * * "*"$DJANGO_ROOT"*"manage.py reconcile_ig_order_fulfillment --limit 100 "*) return 0 ;;
    "*/4 * * * * "*"$DJANGO_ROOT"*"manage.py poll_ig_deal_payments --limit 50 "*) return 0 ;;
    "*/5 * * * * "*"$DJANGO_ROOT"*"manage.py run_call_ai_analyses --limit 1"*) return 0 ;;
    "0 * * * * "*"$DJANGO_ROOT"*"manage.py check_ig_gemini_metadata_health "*) return 0 ;;
    *) return 1 ;;
  esac
}

# A managed block may be the repository's previous known fan-out during an
# upgrade. Unknown executable text is never silently discarded.
if [ "$begin_count" -eq 1 ]; then
  inside_managed=0
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$line" = "$BEGIN_MARKER" ]; then inside_managed=1; continue; fi
    if [ "$line" = "$END_MARKER" ]; then inside_managed=0; continue; fi
    [ "$inside_managed" -eq 1 ] || continue
    trimmed="${line#"${line%%[![:space:]]*}"}"
    case "$trimmed" in ""|\#*) continue ;; esac
    is_owner_line "$line" || contract_error "unknown command inside managed block"
    if [ "$line" != "$cron_line" ] && ! is_supported_legacy_line "$line"; then
      contract_error "unsupported owner inside managed block"
    fi
  done <"$current"
fi

job_marker_count="$(grep -Fxc "$JOB_MARKER" "$current" || true)"
[ "$job_marker_count" -le 1 ] || contract_error "duplicate coordinator markers"
if [ "$begin_count" -eq 0 ] && [ "$job_marker_count" -eq 1 ]; then
  marker_command="$(awk -v marker="$JOB_MARKER" '$0 == marker { getline; print; exit }' "$current")"
  [ "$marker_command" = "$cron_line" ] || contract_error "loose coordinator marker has unknown owner"
fi
if [ "$begin_count" -eq 1 ]; then
  managed_block="$tmp_dir/managed_block"
  awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
    $0 == begin { inside = 1 }
    inside { print }
    $0 == end { exit }
  ' "$current" >"$managed_block"
  block_job_marker_count="$(grep -Fxc "$JOB_MARKER" "$managed_block" || true)"
  [ "$job_marker_count" -eq "$block_job_marker_count" ] || contract_error "coordinator marker is outside managed block"
fi

outside_owner_count=0
unsupported_outside_count=0
coordinator_count=0
order_count=0
checkout_count=0
fulfillment_count=0
payments_count=0
call_analysis_count=0
gemini_metadata_count=0
inside_managed=0
while IFS= read -r line || [ -n "$line" ]; do
  if [ "$inside_managed" -eq 1 ]; then
    [ "$line" = "$END_MARKER" ] && inside_managed=0
    continue
  fi
  if [ "$line" = "$BEGIN_MARKER" ]; then inside_managed=1; continue; fi
  if is_owner_line "$line"; then
    outside_owner_count=$((outside_owner_count + 1))
    case "$owner_kind" in
      coordinator) coordinator_count=$((coordinator_count + 1)) ;;
      order) order_count=$((order_count + 1)) ;;
      checkout) checkout_count=$((checkout_count + 1)) ;;
      fulfillment) fulfillment_count=$((fulfillment_count + 1)) ;;
      payments) payments_count=$((payments_count + 1)) ;;
      call_analysis) call_analysis_count=$((call_analysis_count + 1)) ;;
      gemini_metadata) gemini_metadata_count=$((gemini_metadata_count + 1)) ;;
    esac
    if [ "$line" != "$cron_line" ] && ! is_supported_legacy_line "$line"; then
      unsupported_outside_count=$((unsupported_outside_count + 1))
    fi
  fi
done <"$current"

for count in "$coordinator_count" "$order_count" "$checkout_count" "$fulfillment_count" "$payments_count" "$call_analysis_count" "$gemini_metadata_count"; do
  [ "$count" -le 1 ] || contract_error "duplicate periodic lane owner"
done
legacy_owner_count=$((order_count + checkout_count + fulfillment_count + payments_count + call_analysis_count + gemini_metadata_count))
if [ "$coordinator_count" -gt 0 ] && [ "$legacy_owner_count" -gt 0 ]; then
  contract_error "coordinator coexists with legacy periodic owners"
fi

if [ "$begin_count" -eq 1 ] && [ "$outside_owner_count" -ne 0 ]; then
  contract_error "managed block coexists with a loose periodic owner"
fi
[ "$unsupported_outside_count" -eq 0 ] || contract_error "unsupported loose periodic owner"

if [ "$mode" = "--check" ] || [ "$mode" = "--check-rollback" ]; then
  [ "$begin_count" -eq 1 ] || {
    echo "[instagram-periodic-cron] DRIFT: managed block is missing" >&2
    exit 1
  }
  awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
    $0 == begin { inside = 1 }
    inside { print }
    $0 == end { exit }
  ' "$current" >"$candidate"
  cmp -s "$candidate" "$expected" || {
    echo "[instagram-periodic-cron] DRIFT: managed block differs" >&2
    exit 1
  }
  echo "[instagram-periodic-cron] OK: managed block matches $mode"
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
  if [ "$begin_count" -eq 0 ] && is_owner_line "$line"; then
    continue
  fi
  if [ "$begin_count" -eq 0 ]; then
    case "$line" in
      "# codex:order-telegram-reconcile"|"# codex:ig-checkout-reconcile"|"# codex:ig-order-fulfillment"|"# codex:ig-deal-payments"|"# codex:call-auto-analysis"|"# codex:binotel-call-ai"|"# codex:ig-gemini-metadata-health"|"# codex:instagram-periodic-coordinator") continue ;;
    esac
  fi
  printf '%s\n' "$line" >>"$candidate"
done <"$current"

[ "$skip_managed" -eq 0 ] || contract_error "managed block did not terminate"
[ "$inserted" -eq 1 ] || cat "$expected" >>"$candidate"
if cmp -s "$candidate" "$current"; then
  echo "[instagram-periodic-cron] OK: managed block already installed"
  exit 0
fi
"$CRONTAB_BIN" "$candidate"
if [ "$mode" = "--rollback" ]; then
  echo "[instagram-periodic-cron] OK: five legacy owners restored before code rollback"
else
  echo "[instagram-periodic-cron] OK: one sequential coordinator installed; legacy fan-out and metadata schedule removed"
fi
