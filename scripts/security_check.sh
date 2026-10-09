#!/usr/bin/env bash
# =============================================================================
# Live security self-test with two REAL lab accounts (run as root after install).
#   sudo scripts/security_check.sh <userA> <userB>
# Both users must be in agentlab-users and must NOT be in agentlab-admins.
# Creates workspaces for them if missing (as they would on first `agentlab`).
# =============================================================================
set -uo pipefail
A="${1:?usage: security_check.sh <userA> <userB>}"; B="${2:?}"
ROOT=${AGENTLAB_INSTALL_ROOT:-/opt/agentlab}
PY="$ROOT/venv/bin/python"
AUDIT="$ROOT/system/logs/agentlab_audit.log"
as() { local u=$1; shift; runuser -u "$u" -- "$@"; }
pass=0; fail=0
expect_fail() { if as "$1" sh -c "$3" >/dev/null 2>&1; then echo "  [FAIL] $2"; fail=$((fail+1)); else echo "  [ OK ] $2"; pass=$((pass+1)); fi; }
expect_ok()   { if as "$1" sh -c "$3" >/dev/null 2>&1; then echo "  [ OK ] $2"; pass=$((pass+1)); else echo "  [FAIL] $2"; fail=$((fail+1)); fi; }

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 2; }
for u in "$A" "$B"; do
    id -nG "$u" | grep -qw agentlab-admins && { echo "$u is an admin; pick non-admin accounts"; exit 2; }
    as "$u" "$PY" -I "$ROOT/app/server/agentlab_cli.py" workspace --create >/dev/null
done
echo "secret-$B-idea" | as "$B" sh -c "cat > $ROOT/users/$B/research_idea.txt"

echo "Workspace isolation ($A vs $B)"
expect_fail "$A" "read $B's research idea"           "cat $ROOT/users/$B/research_idea.txt"
expect_fail "$A" "list $B's workspace"               "ls $ROOT/users/$B"
expect_fail "$A" "write into $B's workspace"         "mkdir $ROOT/users/$B/20990101_000000_${B}_abcdef"
expect_fail "$A" "delete $B's workspace"             "rm -rf $ROOT/users/$B"
expect_fail "$A" "list all workspaces"               "ls $ROOT/users"
expect_ok   "$A" "access own workspace"              "touch $ROOT/users/$A/outputs/.probe && rm $ROOT/users/$A/outputs/.probe"

echo "Audit log protection"
expect_fail "$A" "read audit log"                    "cat $AUDIT"
expect_fail "$A" "append to audit log"               "echo x >> $AUDIT"
expect_fail "$A" "truncate audit log"                ": > $AUDIT"
expect_fail "$A" "delete audit log"                  "rm -f $AUDIT"
expect_fail "$A" "rename audit log"                  "mv $AUDIT $AUDIT.x"
expect_fail "$A" "create files in log directory"     "touch $ROOT/system/logs/fake.log"
expect_fail "$A" "read run registry"                 "ls $ROOT/system/state/runs"
if lsattr "$AUDIT" 2>/dev/null | cut -d' ' -f1 | grep -q a; then
    echo "  [ OK ] audit log has append-only attribute"; pass=$((pass+1))
    if (echo '{}' > "$AUDIT") 2>/dev/null; then echo "  [FAIL] root overwrite blocked by +a"; fail=$((fail+1));
    else echo "  [ OK ] even root cannot overwrite without first removing +a"; pass=$((pass+1)); fi
else
    echo "  [WARN] append-only attribute not set (filesystem without chattr support?)"
fi

echo "Secrets"
expect_fail "$A" "read /etc/agentlab/secrets.env"    "cat /etc/agentlab/secrets.env"
PROXY_PID=$(systemctl show -p MainPID --value agentlab-llm-proxy 2>/dev/null || echo 0)
if [ "${PROXY_PID:-0}" != 0 ]; then
    expect_fail "$A" "read proxy process environment" "cat /proc/$PROXY_PID/environ"
fi
expect_fail "$A" "read proxy token registry"         "cat /run/agentlab/proxy/tokens.json"
expect_fail "$A" "use proxy without a run token"     "$PY -c \"import urllib.request,sys; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8765/openai/v1/models', headers={'Authorization':'Bearer guess'}))\""
expect_ok   "$A" "no API key in user environment"    "! env | grep -qE '^(OPENAI|OPENROUTER|DEEPSEEK|ANTHROPIC|GEMINI)_API_KEY='"

echo "Application code"
expect_fail "$A" "modify production code"            "touch $ROOT/app/ai_lab_repo.py"
expect_fail "$A" "replace app symlink"               "ln -sfn /tmp $ROOT/app"

echo "Daemon authorization"
RUN_B=$(ls -1 "$ROOT/system/state/runs" 2>/dev/null | grep "_${B}_" | tail -1 | sed 's/\.json$//')
if [ -n "$RUN_B" ]; then
    expect_fail "$A" "stop $B's run via agentlab"    "$PY -I $ROOT/app/server/agentlab_cli.py stop $RUN_B | grep -q 'Stop requested'"
fi
expect_fail "$A" "use agentlab-admin"                "$PY -I $ROOT/app/server/admin_cli.py logs | grep -q timestamp"
expect_fail "$A" "forge a login event"               "$PY -I -c \"import sys; sys.path.insert(0,'$ROOT/app'); from server.client import DaemonClient; DaemonClient().call('session_event', type='open_session', user='$B', pid=1)\""

echo
echo "security_check: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
