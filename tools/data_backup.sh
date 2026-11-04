#!/bin/bash
# Hourly snapshot of the apps' DATA (what people set: shopping list, notes, timers, every app store and its previous
# version, app inboxes, opened pages, status checks) from the Mini, in two places:
#   the Mini     ~/pinpin-apps-data-snapshots/data-<UTC>.tgz, the last 48 kept
#   the VPS      /root/backups/pinpin-apps-data, a root-only git repo: one commit per change, full history
# Never copied: the token, secrets.json, auth.json, logs, caches (icons, thumbnails).
# Root cron on the VPS:   17 * * * * bash /root/work/pinpin-apps/tools/data_backup.sh >> /tmp/pinpin-data-backup.log 2>&1
set -o pipefail
REPO=${PINPIN_DATA_BACKUP:-/root/backups/pinpin-apps-data}
M="ssh -o BatchMode=yes -o ConnectTimeout=10 pinpin-mini"
umask 077
mkdir -p "$REPO"; chmod 700 "$(dirname "$REPO")" "$REPO"
[ -d "$REPO/.git" ] || git -C "$REPO" init -q -b main
stamp=$(date -u +%Y%m%dT%H%M%SZ)
report() {  # one line on /status → Checks (the token is read on the Mini, it never leaves it)
  $M "curl -s -m5 -X POST -H Content-Type:application/json -H \"Authorization: Bearer \$(cat ~/pinpin-apps-data/token)\" \
      -d '{\"component\":\"apps-data-backup\",\"text\":\"$2\",\"ok\":$1,\"link\":\"VPS /root/backups/pinpin-apps-data\"}' \
      127.0.0.1:57830/api/status/component >/dev/null" 2>/dev/null
}
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
# on the Mini: one consistent tarball (written atomically there too), and the same bytes come here
$M "bash -s" > "$tmp/data.tgz" <<REMOTE || { echo "$stamp backup: Mini unreachable"; report false "snapshot failed: Mini unreachable"; exit 1; }
set -e; cd ~/pinpin-apps-data
mkdir -p ~/pinpin-apps-data-snapshots; chmod 700 ~/pinpin-apps-data-snapshots
items=""; for p in store inbox opens.json status/components; do [ -e "\$p" ] && items="\$items \$p"; done
[ -n "\$items" ] || { mkdir -p store; items=store; }
export COPYFILE_DISABLE=1  # no macOS xattr headers in the archive
tar --no-xattrs -czf ~/pinpin-apps-data-snapshots/.data-$stamp.tgz --exclude='*.tmp*' --exclude='*restore-tmp' \$items
mv ~/pinpin-apps-data-snapshots/.data-$stamp.tgz ~/pinpin-apps-data-snapshots/data-$stamp.tgz
ls -1t ~/pinpin-apps-data-snapshots/data-*.tgz | tail -n +49 | while read f; do mv "\$f" /tmp/; done
cat ~/pinpin-apps-data-snapshots/data-$stamp.tgz
REMOTE
tar tzf "$tmp/data.tgz" >/dev/null || { echo "$stamp backup: damaged tarball, not committed"; report false "snapshot failed: damaged archive"; exit 1; }
mkdir "$tmp/x" && tar xzf "$tmp/data.tgz" -C "$tmp/x"
rsync -a --delete --exclude .git "$tmp/x/" "$REPO/"
cd "$REPO" && git add -A
n=$(git rev-list --count HEAD 2>/dev/null || echo 0)
if git diff --cached --quiet; then echo "$stamp backup: unchanged"; report true "hourly snapshot OK, unchanged ($n versions kept)"; exit 0; fi
git -c user.name="Mr. PinPin" -c user.email=miguelemosreverte@gmail.com commit -q -m "apps data snapshot $stamp"
echo "$stamp backup: committed $(git rev-parse --short HEAD) ($(git show --stat --format= HEAD | tail -1))"
report true "hourly snapshot OK, new version $(git rev-parse --short HEAD) ($((n + 1)) versions kept)"
