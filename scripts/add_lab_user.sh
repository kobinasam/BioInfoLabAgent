#!/usr/bin/env bash
# Grant a Linux account access to AgentLaboratory.
#   sudo scripts/add_lab_user.sh <username> [--admin]
# The account must already exist (normal Linux user). Workspaces are created on
# first use by the user themselves (`agentlab`), never pre-populated by admins.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
user="${1:?usage: add_lab_user.sh <username> [--admin]}"
id "$user" >/dev/null 2>&1 || { echo "no such Linux user: $user" >&2; exit 1; }
usermod -aG agentlab-users "$user"
echo "added $user to agentlab-users"
if [ "${2:-}" = "--admin" ]; then
    usermod -aG agentlab-admins "$user"
    echo "added $user to agentlab-admins"
fi
echo "$user must log out and back in for the new group membership to apply."
