#!/bin/bash
# Read-only server audit batch: TD-015, TD-016, CB-015, CB-041, CB-045, DB-007.
# Designed to run ON the server in one SSH session (server rate-limits connections).
# Usage (from repo root, requires sshpass + password in SSHPASS env):
#   sshpass -e ssh -o StrictHostKeyChecking=no qlknpodo@195.191.24.169 'bash -s' \
#     < TWOCOMMS_A_TO_B/technical/scripts/server_shell_batch.sh
# NO secrets are printed; log greps output only match counts, never lines.
set -u
P=/home/qlknpodo/TWC/TwoComms_Site/twocomms
V=/home/qlknpodo/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/activate

echo "===SECTION crontab==="
crontab -l 2>&1

echo "===SECTION passenger_processes==="
ps -eo pid,rss,etime,comm,args --sort=-rss 2>/dev/null | grep -iE 'passenger|python|lsphp' | grep -v grep | head -30
echo "--- process count by name ---"
ps -eo comm | sort | uniq -c | sort -rn | head -15

echo "===SECTION memory_limits==="
ulimit -a 2>&1
echo "--- free ---"
free -m 2>/dev/null || cat /proc/meminfo | head -5

echo "===SECTION htaccess_passenger==="
grep -iE 'passenger|lsapi' "$P/.htaccess" "$P/../.htaccess" /home/qlknpodo/public_html/.htaccess 2>/dev/null | head -20
find /home/qlknpodo -maxdepth 3 -name '.htaccess' 2>/dev/null | head -10

echo "===SECTION logs_listing==="
ls -lah "$P"/*.log 2>/dev/null
echo "--- other log dirs ---"
ls -lah "$P"/logs/ 2>/dev/null | head -20
du -sh "$P"/*.log 2>/dev/null

echo "===SECTION logrotate==="
ls /etc/logrotate.d/ 2>/dev/null | head; crontab -l 2>/dev/null | grep -i rotat
echo "--- user logrotate configs ---"
find /home/qlknpodo -maxdepth 2 -name '*logrotate*' 2>/dev/null

echo "===SECTION log_secret_scan_counts_only==="
for f in "$P"/*.log; do
  [ -f "$f" ] || continue
  n1=$(grep -ciE 'password|passwd' "$f" 2>/dev/null || echo 0)
  n2=$(grep -ciE 'secret|token=|api_key|apikey' "$f" 2>/dev/null || echo 0)
  n3=$(grep -cE '[0-9]{16}' "$f" 2>/dev/null || echo 0)
  sz=$(du -h "$f" | cut -f1)
  echo "$f size=$sz pw_hits=$n1 secret_hits=$n2 digit16_hits=$n3"
done

echo "===SECTION env_image_flags==="
grep -cE 'IMAGE_OPTIMIZATION' "$P/.env" 2>/dev/null && grep -oE 'IMAGE_OPTIMIZATION[A-Z_]*' "$P/.env" 2>/dev/null | sort -u
grep -E '^IMAGE_OPTIMIZATION[A-Z_]*=' "$P/.env" 2>/dev/null | sed 's/=.*/=<value-hidden>/'
echo "--- actual boolean values (safe, non-secret flags) ---"
grep -E '^IMAGE_OPTIMIZATION[A-Z_]*=' "$P/.env" 2>/dev/null

echo "===SECTION optimized_cache_dir==="
du -sh "$P/media/optimized_cache" 2>/dev/null; ls "$P/media/optimized_cache" 2>/dev/null | wc -l

echo "===SECTION django_checks==="
source "$V" && cd "$P" || exit 1
python manage.py makemigrations --check --dry-run 2>&1 | tail -5
echo "--- unapplied migrations ---"
python manage.py showmigrations 2>/dev/null | grep '\[ \]' | head -20
echo "--- unapplied count ---"
python manage.py showmigrations 2>/dev/null | grep -c '\[ \]'

echo "===SECTION disk==="
df -h /home 2>/dev/null | tail -1
du -sh "$P/media" "$P/staticfiles" 2>/dev/null

echo "===DONE==="
