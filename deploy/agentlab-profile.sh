# /etc/profile.d/agentlab.sh -- login banner for AgentLaboratory lab members.
# UX only: authoritative login/logout tracking is done by PAM (pam_exec), which
# cannot be bypassed by skipping shell start-up files.
case $- in *i*) ;; *) return 0 2>/dev/null || exit 0 ;; esac
if [ -n "$AGENTLAB_BANNER_SHOWN" ]; then return 0 2>/dev/null || exit 0; fi
export AGENTLAB_BANNER_SHOWN=1
if command -v agentlab >/dev/null 2>&1 && id -nG 2>/dev/null | grep -qwE 'agentlab-users|agentlab-admins'; then
    echo ""
    echo "AgentLaboratory is available on this server. Commands:"
    echo "  agentlab            guided session (workspace, research idea, run, resume)"
    echo "  agentlab status     active users and runs"
    echo "  agentlab runs       your runs        agentlab resume <run-id>"
    echo ""
    if [ -d "/opt/agentlab/users/$(id -un)/runs" ]; then
        timeout 5 agentlab status 2>/dev/null | grep -E "^$(id -un) " | sed 's/^/  your run: /' || true
    fi
fi
