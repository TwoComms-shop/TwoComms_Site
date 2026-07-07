#!/bin/bash
# Deploy script for finance subdomain
set -e

: "${SSHPASS:?Set SSHPASS in the environment before running this deploy script}"

sshpass -e ssh -o StrictHostKeyChecking=no qlknpodo@195.191.24.169 "bash -lc '
    source /home/qlknpodo/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/activate
    cd /home/qlknpodo/TWC/TwoComms_Site/twocomms

    echo \"=== Fetching latest code ===\"
    git fetch origin main
    git reset --hard origin/main

    echo \"=== Merging migrations ===\"
    python manage.py makemigrations --merge --noinput

    echo \"=== Running migrations ===\"
    python manage.py migrate finance

    echo \"=== Collecting static files ===\"
    python manage.py collectstatic --noinput

    echo \"=== Ensuring Monobank cron wrapper exists ===\"
    if [ ! -f /home/qlknpodo/mono_sync.sh ]; then
        printf \"%s\n\" \"#!/bin/bash\" \"source /home/qlknpodo/virtualenv/TWC/TwoComms_Site/twocomms/3.14/bin/activate\" \"cd /home/qlknpodo/TWC/TwoComms_Site/twocomms\" \"python manage.py finance_mono_sync >> /home/qlknpodo/mono_sync.log 2>&1\" > /home/qlknpodo/mono_sync.sh
        chmod +x /home/qlknpodo/mono_sync.sh
        echo created
    else
        echo exists
    fi

    echo \"=== Test mono sync run ===\"
    bash /home/qlknpodo/mono_sync.sh || true
    tail -5 /home/qlknpodo/mono_sync.log 2>/dev/null || echo no-log

    echo \"=== Restarting application ===\"
    touch /home/qlknpodo/TWC/TwoComms_Site/twocomms/tmp/restart.txt

    echo \"=== Deploy completed successfully ===\"
'"
