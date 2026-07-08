#!/usr/bin/env bash
# W0-3 (TD-020/TECH-042): регулярный бэкап MySQL для TwoComms.
#
# Установка на сервере [SERVER]:
#   1. mkdir -p ~/db_backups && chmod 700 ~/db_backups   # ВНЕ web-root!
#   2. Заполнить ~/.my.cnf (chmod 600):
#        [client]
#        user=twocomms_db_user
#        password=***
#   3. crontab -e:
#        45 3 * * * /bin/bash ~/TWC/scripts/backup_mysql.sh >> ~/db_backups/backup.log 2>&1
#   4. Проверить восстановление на копии:
#        zcat ~/db_backups/daily/twocomms-YYYYMMDD.sql.gz | mysql test_restore_db
#
# Ротация: 7 ежедневных + 5 еженедельных (воскресенье) ≈ 30 дней покрытия.

set -euo pipefail

DB_NAME="${TWC_DB_NAME:-twocomms}"
BACKUP_ROOT="${TWC_BACKUP_DIR:-$HOME/db_backups}"
DAILY_DIR="$BACKUP_ROOT/daily"
WEEKLY_DIR="$BACKUP_ROOT/weekly"
STAMP="$(date +%Y%m%d)"
DOW="$(date +%u)"   # 7 = воскресенье

mkdir -p "$DAILY_DIR" "$WEEKLY_DIR"
chmod 700 "$BACKUP_ROOT" "$DAILY_DIR" "$WEEKLY_DIR"

DUMP_FILE="$DAILY_DIR/${DB_NAME}-${STAMP}.sql.gz"

# --single-transaction: консистентный снапшот InnoDB без блокировок;
# --routines --triggers: не потерять хранимые объекты.
mysqldump \
  --single-transaction \
  --quick \
  --routines \
  --triggers \
  --no-tablespaces \
  "$DB_NAME" | gzip -9 > "$DUMP_FILE.tmp"

# Атомарная замена + sanity-check: пустой/битый дамп не должен затирать прошлый
if [ "$(stat -c%s "$DUMP_FILE.tmp")" -lt 10240 ]; then
  echo "[backup_mysql] ERROR: dump suspiciously small (<10KB), keeping .tmp for inspection" >&2
  exit 1
fi
mv "$DUMP_FILE.tmp" "$DUMP_FILE"
chmod 600 "$DUMP_FILE"

# Проверка целостности gzip
gzip -t "$DUMP_FILE"

# Воскресенье → копия в weekly
if [ "$DOW" = "7" ]; then
  cp -p "$DUMP_FILE" "$WEEKLY_DIR/"
fi

# Ротация: daily 7 дней, weekly 35 дней
find "$DAILY_DIR" -name "${DB_NAME}-*.sql.gz" -mtime +7 -delete
find "$WEEKLY_DIR" -name "${DB_NAME}-*.sql.gz" -mtime +35 -delete

echo "[backup_mysql] OK: $DUMP_FILE ($(stat -c%s "$DUMP_FILE") bytes) $(date -Is)"
