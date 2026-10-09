#!/usr/bin/env bash
# =============================================================================
# Controlled deployment of a reviewed, merged commit to the production server.
#
#   sudo scripts/deploy.sh <commit-sha|origin/main> [--skip-tests]
#
# 1. fetches from the canonical remote into a root-owned mirror
# 2. REFUSES any commit that is not contained in origin/<deploy_branch>
#    (i.e. not merged through a reviewed PR)
# 3. builds an immutable release directory /opt/agentlab/releases/<sha>
# 4. runs the test suite against that release
# 5. atomically switches /opt/agentlab/app -> the new release
# 6. restarts agentlabd + proxy (running research units keep running on the
#    release they started with) and records the deployment in the audit log
#
# Rollback: sudo scripts/deploy.sh <previous-sha>
# =============================================================================
set -euo pipefail
INSTALL_ROOT=/opt/agentlab
MIRROR=$INSTALL_ROOT/repo.git
BRANCH=${AGENTLAB_DEPLOY_BRANCH:-main}
TARGET="${1:?usage: deploy.sh <commit-sha|origin/main> [--skip-tests]}"
SKIP_TESTS=0; [ "${2:-}" = "--skip-tests" ] && SKIP_TESTS=1
DEPLOYER="${SUDO_USER:-root}"

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
id -nG "$DEPLOYER" | grep -qw agentlab-admins || [ "$DEPLOYER" = root ] || {
    echo "$DEPLOYER is not in agentlab-admins" >&2; exit 1; }

if [ ! -d "$MIRROR" ]; then
    REMOTE=$(git -C "$(readlink -f $INSTALL_ROOT/app)" config --get remote.origin.url 2>/dev/null || true)
    REMOTE=${AGENTLAB_GIT_REMOTE:-$REMOTE}
    [ -n "$REMOTE" ] || { echo "set AGENTLAB_GIT_REMOTE=<git url> for the first deployment" >&2; exit 1; }
    git clone --mirror "$REMOTE" "$MIRROR"
fi
git -C "$MIRROR" fetch --prune origin '+refs/heads/*:refs/heads/*'
SHA=$(git -C "$MIRROR" rev-parse --verify "${TARGET#origin/}^{commit}")

# --- review gate: only commits merged into the deploy branch -------------------
if ! git -C "$MIRROR" merge-base --is-ancestor "$SHA" "refs/heads/$BRANCH"; then
    echo "REFUSED: $SHA is not on $BRANCH. Merge it through a reviewed pull request first." >&2
    exit 1
fi

REL=$INSTALL_ROOT/releases/$SHA
if [ ! -d "$REL" ]; then
    TMP=$(mktemp -d "$INSTALL_ROOT/releases/.build-XXXX")
    git -C "$MIRROR" archive "$SHA" | tar -x -C "$TMP"
    printf '{"commit": "%s", "dirty": false, "deployed": "%s", "deployer": "%s", "branch": "%s"}\n' \
        "$SHA" "$(date -u +%FT%TZ)" "$DEPLOYER" "$BRANCH" > "$TMP/RELEASE.json"
    chown -R root:root "$TMP"; chmod -R u=rwX,go=rX "$TMP"
    mv "$TMP" "$REL"
fi

TESTS=skipped
if [ "$SKIP_TESTS" -eq 0 ]; then
    echo "running test suite for $SHA ..."
    T=$(mktemp -d); cp -a "$REL/." "$T/"
    if (cd "$T/tests" && "$INSTALL_ROOT/venv/bin/python" -m unittest -q); then TESTS=passed; else
        rm -rf "$T"; echo "REFUSED: tests failed for $SHA" >&2; exit 1; fi
    rm -rf "$T"
fi

# requirements changed? (shared venv; see README_SERVER.md "Deployment")
REQ_HASH=$(sha256sum "$REL/requirements.txt" | cut -c1-16)
if [ ! -f "$INSTALL_ROOT/venv/.req-$REQ_HASH" ]; then
    "$INSTALL_ROOT/venv/bin/pip" install -q -r "$REL/requirements.txt" && touch "$INSTALL_ROOT/venv/.req-$REQ_HASH"
fi

PREVIOUS=$(basename "$(readlink -f $INSTALL_ROOT/app)")
ln -sfn "$REL" "$INSTALL_ROOT/app.new" && mv -Tf "$INSTALL_ROOT/app.new" "$INSTALL_ROOT/app"
for f in agentlab-runner.service agentlab-llm-proxy.service; do
    cmp -s "$REL/systemd/$f" "/etc/systemd/system/$f" || {
        cp -a "/etc/systemd/system/$f" "/etc/systemd/system/$f.agentlab-backup-$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
        install -m 0644 "$REL/systemd/$f" "/etc/systemd/system/$f"; }
done
install -m 0755 "$REL/scripts/agentlab" /usr/local/bin/agentlab
install -m 0755 "$REL/scripts/agentlab-admin" /usr/local/bin/agentlab-admin
systemctl daemon-reload
systemctl restart agentlab-llm-proxy agentlab-runner
for _ in $(seq 1 30); do [ -S /run/agentlab/agentlabd.sock ] && break; sleep 0.5; done

"$INSTALL_ROOT/venv/bin/python" -I - "$INSTALL_ROOT/app" "$SHA" "$PREVIOUS" "$DEPLOYER" "$BRANCH" "$TESTS" <<'EOF'
import sys; sys.path.insert(0, sys.argv[1])
import time
from server.client import DaemonClient, DaemonUnavailable
for _ in range(60):  # the restarted daemon may not be listening yet
    try:
        DaemonClient().call("ping"); break
    except DaemonUnavailable:
        time.sleep(0.5)
print(DaemonClient().call("record_deployment", commit=sys.argv[2], previous=sys.argv[3], deployer=sys.argv[4],
                          branch=sys.argv[5], tests=sys.argv[6], release_path=sys.argv[1]))
EOF
echo "deployed $SHA (previous $PREVIOUS, tests $TESTS)"
