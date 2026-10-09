#!/usr/bin/env bash
# =============================================================================
# AgentLaboratory multi-user server installer (idempotent).
#
#   sudo scripts/install_server.sh [--skip-deps] [--skip-pam] [--no-start]
#                                  [--no-systemd] [--source DIR]
#
# Safe to re-run: never overwrites existing user workspaces, audit logs,
# server.yaml or secrets; backs up any system file before changing it
# (<file>.agentlab-backup-<timestamp>).
# =============================================================================
set -euo pipefail

INSTALL_ROOT=/opt/agentlab
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKIP_DEPS=0; SKIP_PAM=0; NO_START=0; NO_SYSTEMD=0
STAMP="$(date +%Y%m%d%H%M%S)"

while [ $# -gt 0 ]; do
    case "$1" in
        --skip-deps) SKIP_DEPS=1 ;;
        --skip-pam) SKIP_PAM=1 ;;
        --no-start) NO_START=1 ;;
        --no-systemd) NO_SYSTEMD=1; NO_START=1 ;;   # containers/CI: install files only
        --source) SRC_DIR="$(cd "$2" && pwd)"; shift ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
    shift
done

log()  { printf '\033[1;34m[agentlab-install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[agentlab-install] WARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[agentlab-install] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

backup() {  # backup <file>   (only if it exists)
    [ -e "$1" ] && cp -a "$1" "$1.agentlab-backup-$STAMP" && log "backed up $1 -> $1.agentlab-backup-$STAMP"
    return 0
}

install_if_changed() {  # install_if_changed <src> <dst> <mode> [owner:group]
    local src="$1" dst="$2" mode="$3" owner="${4:-root:root}"
    if [ -e "$dst" ] && cmp -s "$src" "$dst"; then return 0; fi
    backup "$dst"
    install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$src" "$dst"
    log "installed $dst"
}

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
[ "$(uname -s)" = "Linux" ] || die "Linux only"
[ -f "$SRC_DIR/ai_lab_repo.py" ] && [ -d "$SRC_DIR/server" ] || die "source tree not found at $SRC_DIR"

# ----------------------------------------------------------------- 1. packages
if [ "$SKIP_DEPS" -eq 0 ]; then
    log "installing system packages"
    if command -v apt-get >/dev/null; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip git e2fsprogs \
            logrotate acl ca-certificates >/dev/null
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq texlive-latex-base texlive-latex-extra >/dev/null \
            || warn "LaTeX not installed (only needed for compile-latex: True)"
    elif command -v dnf >/dev/null; then
        dnf install -y -q python3 python3-pip git e2fsprogs logrotate acl ca-certificates
        dnf install -y -q texlive-latex || warn "LaTeX not installed (only needed for compile-latex: True)"
    else
        warn "unknown package manager: install python3 (>=3.10), venv, git, e2fsprogs, logrotate manually"
    fi
fi
PY=$(command -v python3) || die "python3 not found"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "Python >= 3.10 required"

# ------------------------------------------------------------ 2. groups/users
ensure_group() { getent group "$1" >/dev/null || { groupadd --system "$1"; log "created group $1"; }; }
ensure_group agentlab-users
ensure_group agentlab-admins
ensure_group agentlab-proxy
if ! id agentlab-proxy >/dev/null 2>&1; then
    useradd --system --gid agentlab-proxy --home-dir /nonexistent --no-create-home \
        --shell /usr/sbin/nologin agentlab-proxy
    log "created system user agentlab-proxy"
fi

# --------------------------------------------------------- 3. directory layout
log "creating directory layout under $INSTALL_ROOT"
install -d -m 0755 -o root -g root "$INSTALL_ROOT" "$INSTALL_ROOT/config" "$INSTALL_ROOT/releases"
install -d -m 0711 -o root -g root "$INSTALL_ROOT/users"          # traverse only: users cannot list others
install -d -m 0750 -o root -g agentlab-admins "$INSTALL_ROOT/system" "$INSTALL_ROOT/system/logs" \
    "$INSTALL_ROOT/system/state" "$INSTALL_ROOT/system/scripts"
install -d -m 0700 -o root -g root /etc/agentlab
# Existing user workspaces are never modified here (only their ownership is verified).
for ws in "$INSTALL_ROOT"/users/*; do
    [ -d "$ws" ] || continue
    u=$(basename "$ws")
    if id "$u" >/dev/null 2>&1 && [ "$(stat -c %U "$ws")" != "$u" ]; then
        warn "workspace $ws is not owned by $u -- fix with: chown -R $u: $ws"
    fi
    chmod go-rwx "$ws" 2>/dev/null || true
done

# ------------------------------------------------------- 4. application release
COMMIT=$(git -C "$SRC_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
DIRTY=$(git -C "$SRC_DIR" status --porcelain --untracked-files=no 2>/dev/null | head -1 || true)
if [ "$COMMIT" = "unknown" ]; then
    REL="$INSTALL_ROOT/releases/worktree-$STAMP"          # not a git checkout: always a fresh release
else
    REL="$INSTALL_ROOT/releases/${COMMIT}${DIRTY:+-dirty-$STAMP}"
fi
if [ ! -d "$REL" ]; then
    log "creating release $REL"
    mkdir -p "$REL"
    if [ "$COMMIT" != "unknown" ] && [ -z "$DIRTY" ]; then
        git -C "$SRC_DIR" archive "$COMMIT" | tar -x -C "$REL"
    else
        warn "source tree has uncommitted changes; copying working tree (use scripts/deploy.sh for reviewed releases)"
        tar -C "$SRC_DIR" --exclude=.git --exclude='venv*' --exclude=.backups --exclude=__pycache__ \
            --exclude=MATH_research_dir --exclude=state_saves -cf - . | tar -x -C "$REL"
    fi
    printf '{"commit": "%s", "dirty": %s, "deployed": "%s", "deployer": "%s"}\n' "$COMMIT" \
        "$([ -n "$DIRTY" ] && echo true || echo false)" "$(date -u +%FT%TZ)" "${SUDO_USER:-root}" > "$REL/RELEASE.json"
    chown -R root:root "$REL"
    chmod -R u=rwX,go=rX "$REL"                                       # read-only for users
fi
PREVIOUS=$(readlink -f "$INSTALL_ROOT/app" 2>/dev/null || echo "none")
ln -sfn "$REL" "$INSTALL_ROOT/app.new" && mv -Tf "$INSTALL_ROOT/app.new" "$INSTALL_ROOT/app"
log "application root -> $REL"

# -------------------------------------------------------------- 5. virtualenv
if [ ! -x "$INSTALL_ROOT/venv/bin/python" ]; then
    log "creating virtualenv $INSTALL_ROOT/venv"
    "$PY" -m venv "$INSTALL_ROOT/venv"
fi
if [ "$SKIP_DEPS" -eq 0 ]; then
    REQ_HASH=$(sha256sum "$REL/requirements.txt" | cut -c1-16)
    if [ ! -f "$INSTALL_ROOT/venv/.req-$REQ_HASH" ]; then
        log "installing Python requirements (this can take a while)"
        "$INSTALL_ROOT/venv/bin/pip" install -q --upgrade pip
        "$INSTALL_ROOT/venv/bin/pip" install -q -r "$REL/requirements.txt"
        touch "$INSTALL_ROOT/venv/.req-$REQ_HASH"
    fi
else
    "$INSTALL_ROOT/venv/bin/pip" install -q pyyaml psutil 2>/dev/null || true   # server layer minimum
fi
chown -R root:root "$INSTALL_ROOT/venv"; chmod -R go-w "$INSTALL_ROOT/venv"

# ------------------------------------------------------------ 6. configuration
if [ ! -f "$INSTALL_ROOT/config/server.yaml" ]; then
    install -m 0644 -o root -g root "$REL/config/server.yaml.example" "$INSTALL_ROOT/config/server.yaml"
    log "installed default $INSTALL_ROOT/config/server.yaml (edit to taste)"
else
    log "keeping existing $INSTALL_ROOT/config/server.yaml"
fi
if [ ! -f /etc/agentlab/secrets.env ]; then
    umask 077
    cat > /etc/agentlab/secrets.env <<'EOF'
# AgentLaboratory provider API keys -- root:root 0600. Read only by systemd for
# agentlab-llm-proxy.service. Never copy these into Git, YAML or workspaces.
# After editing:  sudo systemctl restart agentlab-llm-proxy
#OPENROUTER_API_KEY=
#OPENAI_API_KEY=
#DEEPSEEK_API_KEY=
#ANTHROPIC_API_KEY=
EOF
    log "created /etc/agentlab/secrets.env template (add keys, then restart agentlab-llm-proxy)"
else
    log "keeping existing /etc/agentlab/secrets.env (not read or modified)"
fi
chown root:root /etc/agentlab/secrets.env; chmod 0600 /etc/agentlab/secrets.env

# ---------------------------------------------------------- 7. audit log file
AUDIT="$INSTALL_ROOT/system/logs/agentlab_audit.log"
if [ ! -f "$AUDIT" ]; then install -m 0640 -o root -g agentlab-admins /dev/null "$AUDIT"; fi
# Once append-only (+a), even root cannot chown/chmod: only fix metadata when it is actually wrong.
if [ "$(stat -c %a:%U:%G "$AUDIT")" != "640:root:agentlab-admins" ]; then
    chattr -a "$AUDIT" 2>/dev/null || true
    chown root:agentlab-admins "$AUDIT"; chmod 0640 "$AUDIT"
fi
if command -v chattr >/dev/null && chattr +a "$AUDIT" 2>/dev/null; then
    log "audit log is append-only (chattr +a)"
else
    warn "filesystem does not support chattr +a; audit log relies on ownership + hash chain"
fi

# --------------------------------------------------------------- 8. CLI wrappers
install_if_changed "$REL/scripts/agentlab" /usr/local/bin/agentlab 0755
install_if_changed "$REL/scripts/agentlab-admin" /usr/local/bin/agentlab-admin 0755
install_if_changed "$REL/scripts/agentlab-pam-session" "$INSTALL_ROOT/system/scripts/agentlab-pam-session" 0750 root:root
chmod 0755 "$INSTALL_ROOT/system/scripts/agentlab-pam-session" 2>/dev/null || true
# PAM runs it as root; it only needs to be executable by root. Keep the directory admin-only.

# -------------------------------------------------- 9. logging / runtime files
install_if_changed "$REL/deploy/agentlab.logrotate" /etc/logrotate.d/agentlab 0644
install_if_changed "$REL/deploy/agentlab.tmpfiles.conf" /etc/tmpfiles.d/agentlab.conf 0644
install_if_changed "$REL/deploy/agentlab-profile.sh" /etc/profile.d/agentlab.sh 0644
if [ "$NO_SYSTEMD" -eq 0 ]; then
    systemd-tmpfiles --create /etc/tmpfiles.d/agentlab.conf
else
    install -d -m 0755 -o root -g root /run/agentlab
    install -d -m 0750 -o root -g agentlab-proxy /run/agentlab/proxy
fi

# ------------------------------------------------------------- 10. PAM session
PAM_FILE=/etc/pam.d/sshd
PAM_LINE="session optional pam_exec.so quiet $INSTALL_ROOT/system/scripts/agentlab-pam-session"
if [ "$SKIP_PAM" -eq 0 ] && [ -f "$PAM_FILE" ]; then
    if ! grep -qF "agentlab-pam-session" "$PAM_FILE"; then
        backup "$PAM_FILE"
        printf '\n# AgentLaboratory login/logout tracking (optional: never blocks SSH)\n%s\n' "$PAM_LINE" >> "$PAM_FILE"
        log "added pam_exec session hook to $PAM_FILE"
    else
        log "PAM hook already present"
    fi
elif [ "$SKIP_PAM" -eq 0 ]; then
    warn "$PAM_FILE not found; login tracking not configured"
fi

# -------------------------------------------------------------- 11. systemd
if [ "$NO_SYSTEMD" -eq 0 ]; then
    install_if_changed "$REL/systemd/agentlab-runner.service" /etc/systemd/system/agentlab-runner.service 0644
    install_if_changed "$REL/systemd/agentlab-llm-proxy.service" /etc/systemd/system/agentlab-llm-proxy.service 0644
    systemctl daemon-reload
    systemctl enable agentlab-runner.service agentlab-llm-proxy.service >/dev/null
    if [ "$NO_START" -eq 0 ]; then
        systemctl restart agentlab-llm-proxy.service
        systemctl restart agentlab-runner.service    # running research units are NOT affected
        for _ in $(seq 1 30); do [ -S /run/agentlab/agentlabd.sock ] && break; sleep 0.5; done
    fi
fi

# ------------------------------------------------- 12. record + health checks
if [ -S /run/agentlab/agentlabd.sock ]; then
    "$INSTALL_ROOT/venv/bin/python" -I - "$INSTALL_ROOT/app" "$COMMIT" "$PREVIOUS" "${SUDO_USER:-root}" <<'EOF' || true
import sys; sys.path.insert(0, sys.argv[1])
import time
from server.client import DaemonClient, DaemonUnavailable
for _ in range(60):  # the restarted daemon may not be listening yet
    try:
        DaemonClient().call("ping"); break
    except DaemonUnavailable:
        time.sleep(0.5)
DaemonClient().call("record_deployment", commit=sys.argv[2], previous=sys.argv[3], deployer=sys.argv[4],
                    branch="install", release_path=sys.argv[1], tests="install_server.sh")
EOF
fi

log "health checks"
fail=0
check() { if eval "$2"; then printf '  [ OK ] %s\n' "$1"; else printf '  [FAIL] %s\n' "$1"; fail=1; fi; }
check "users dir is 0711 root"               '[ "$(stat -c %a:%U "$INSTALL_ROOT/users")" = "711:root" ]'
check "system dir not accessible to users"   '[ "$(stat -c %a:%U:%G "$INSTALL_ROOT/system")" = "750:root:agentlab-admins" ]'
check "audit log root:agentlab-admins 0640"  '[ "$(stat -c %a:%U:%G "$AUDIT")" = "640:root:agentlab-admins" ]'
check "secrets.env root 0600"                '[ "$(stat -c %a:%U "/etc/agentlab/secrets.env")" = "600:root" ]'
check "application is root-owned read-only" '[ -z "$(find "$REL" \( -perm -o+w -o -perm -g+w -o ! -user root \) -print -quit)" ]'
check "venv python works"                    '"$INSTALL_ROOT/venv/bin/python" -c "import yaml, psutil"'
check "no API keys in server.yaml"           '! grep -qiE "(sk-|api[-_]?key: *[\"'"'"']?[A-Za-z0-9])" "$INSTALL_ROOT/config/server.yaml"'
if [ "$NO_SYSTEMD" -eq 0 ] && [ "$NO_START" -eq 0 ]; then
    check "agentlab-runner active"           'systemctl is-active --quiet agentlab-runner'
    check "agentlab-llm-proxy active"        'systemctl is-active --quiet agentlab-llm-proxy'
    check "daemon socket root:agentlab-users 0660" '[ "$(stat -c %a:%U:%G /run/agentlab/agentlabd.sock)" = "660:root:agentlab-users" ]'
fi

cat <<EOF

AgentLaboratory installed (release ${COMMIT:0:12}).

Next steps:
  1. Add provider keys:        sudoedit /etc/agentlab/secrets.env && sudo systemctl restart agentlab-llm-proxy
  2. Add lab members:          sudo $INSTALL_ROOT/app/scripts/add_lab_user.sh <username> [--admin]
  3. End-to-end self test:     sudo $INSTALL_ROOT/app/scripts/security_check.sh <user1> <user2>
  4. Members log in and run:   agentlab
EOF
exit $fail
