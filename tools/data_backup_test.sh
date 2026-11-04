#!/bin/bash
# Backup round trip (run on the VPS as root): take a snapshot, restore it into a scratch dir on the Mini, and diff
# it against the live data. Nothing live is written.   bash tools/data_backup_test.sh
set -e
cd "$(dirname "$0")/.."
bash tools/data_backup.sh
S="pp-restore-check-$$"
bash tools/data_restore.sh HEAD --into "/tmp/$S"
ssh -o BatchMode=yes pinpin-mini "bash -s" <<REMOTE
cd ~/pinpin-apps-data
d=0; for p in store inbox opens.json status/components; do
  [ -e "\$p" ] || continue
  diff -r "\$p" "/tmp/$S/\$p" >/dev/null || { echo "DIFF in \$p"; diff -r "\$p" "/tmp/$S/\$p" | head -5; d=1; }
done
rm -rf "/tmp/$S"
[ \$d = 0 ] && echo "BACKUP ROUND TRIP PASS: the restored snapshot equals the live data" || { echo "BACKUP ROUND TRIP FAIL"; exit 1; }
REMOTE
