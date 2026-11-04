#!/bin/bash
# VPS: `kanban` on everyone's PATH (agents run as pinpin-claude; the tool re-runs itself with sudo -n for root's ssh key).
#   sudo -n bash deploy/install-kanban-cli.sh
set -e
cat > /usr/local/bin/kanban <<'W'
#!/bin/sh
exec sudo -n --preserve-env=PINPIN_WHO python3 /root/work/pinpin-apps/tools/kanban "$@"
W
chmod 755 /usr/local/bin/kanban
echo "installed /usr/local/bin/kanban"
