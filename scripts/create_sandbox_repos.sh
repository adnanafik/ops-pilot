#!/usr/bin/env bash
# Creates the three ops-pilot sandbox repos on GitHub and pushes the
# intentionally broken code so CI fails and ops-pilot can respond.
#
# Usage:
#   ./scripts/create_sandbox_repos.sh
#
# Requires: gh CLI authenticated, git

set -euo pipefail

GITHUB_USER=$(gh api user --jq '.login')
SANDBOX_DIR="$(cd "$(dirname "$0")/../sandbox" && pwd)"

echo "Creating sandbox repos for GitHub user: $GITHUB_USER"
echo ""

REPOS=(
  "null_pointer_auth:ops-pilot-sandbox-null-pointer:Broken null guard in SessionManager — ops-pilot demo"
  "missing_dependency_docker:ops-pilot-sandbox-missing-dep:Missing pyarrow pin causes Docker build failure — ops-pilot demo"
  "flaky_integration_test:ops-pilot-sandbox-flaky-test:Flaky integration test after Postgres migration — ops-pilot demo"
)

for entry in "${REPOS[@]}"; do
  DIR="${entry%%:*}"
  rest="${entry#*:}"
  REPO_NAME="${rest%%:*}"
  DESCRIPTION="${rest#*:}"

  echo "──────────────────────────────────────────────────"
  echo "  Repo: $REPO_NAME"
  echo "──────────────────────────────────────────────────"

  # Create repo (skip if already exists)
  if gh repo view "$GITHUB_USER/$REPO_NAME" &>/dev/null; then
    echo "  ✓ Repo already exists — skipping creation"
  else
    gh repo create "$GITHUB_USER/$REPO_NAME" \
      --public \
      --description "$DESCRIPTION" \
      --confirm 2>/dev/null || \
    gh repo create "$REPO_NAME" \
      --public \
      --description "$DESCRIPTION"
    echo "  ✓ Created $GITHUB_USER/$REPO_NAME"
  fi

  WORK_DIR=$(mktemp -d)
  trap "rm -rf $WORK_DIR" EXIT

  # Init git repo
  cp -r "$SANDBOX_DIR/$DIR/." "$WORK_DIR/"
  cd "$WORK_DIR"
  git init -q
  git checkout -b main
  git config user.email "ops-pilot@demo.local"
  git config user.name "ops-pilot"

  # Initial "good" commit — fix is already applied
  # We'll add the good version first, then a "breaking" commit
  git add -A
  git commit -q -m "chore: initial working state"

  # Push to GitHub
  git remote add origin "https://github.com/$GITHUB_USER/$REPO_NAME.git"
  git push -q -f origin main
  echo "  ✓ Pushed initial commit to main"

  # Now introduce the bug on main (so CI fails immediately on push)
  echo "  → Introducing bug commit to trigger CI failure…"
  case "$DIR" in
    null_pointer_auth)
      # Remove the null guard check (un-comment the BUG line)
      python3 - <<'PYEOF'
import re, pathlib
f = pathlib.Path("auth/session_manager.py")
content = f.read_text()
# Make the load() method return None without the guard comment context
content = content.replace(
    "        # REMOVED: if raw is None: raise ValueError(f\"Session not found for user {user_id}\")\n        if raw is None:\n            return None  # silent None — callers are not expecting this",
    "        if raw is None:\n            return None"
)
f.write_text(content)
PYEOF
      git add auth/session_manager.py
      git commit -q -m "feat: add refresh token rotation for OAuth sessions

Removed null-guard on Redis hydration in SessionManager.load() to
simplify the call path. Callers handle ValueError anyway.

BREAKING: sessions now return None instead of raising ValueError
when the session key is missing."
      ;;
    missing_dependency_docker)
      # Remove the comment about missing pyarrow to make it look intentional
      sed -i.bak '/# BUG:/d' requirements.txt && rm -f requirements.txt.bak
      git add requirements.txt
      git commit -q -m "chore: upgrade pandas to 2.2.1 and add polars for faster aggregations"
      ;;
    flaky_integration_test)
      # Remove the comment that explains the bug to make it look like normal code
      sed -i.bak '/# BUG: this resets before/d' tests/integration/test_payment_flow.py
      sed -i.bak '/# BUG: missing clear_idempotency/d' tests/integration/test_payment_flow.py && rm -f tests/integration/test_payment_flow.py.bak
      git add tests/integration/test_payment_flow.py
      git commit -q -m "refactor: extract PaymentProcessor into standalone service class

Moved idempotency key storage from in-memory dict to module-level
store to simulate Postgres persistence across requests."
      ;;
  esac

  git push -q origin main
  echo "  ✓ Pushed breaking commit — CI will now fail"
  echo "  → https://github.com/$GITHUB_USER/$REPO_NAME/actions"
  echo ""

  cd - > /dev/null
done

echo "══════════════════════════════════════════════════"
echo "  All sandbox repos created!"
echo "══════════════════════════════════════════════════"
echo ""
echo "CI is now running on GitHub. Wait ~1 minute for"
echo "the jobs to fail, then run:"
echo ""
echo "  python3 scripts/watch_and_fix.py"
echo ""
echo "Repo URLs:"
for entry in "${REPOS[@]}"; do
  rest="${entry#*:}"
  REPO_NAME="${rest%%:*}"
  echo "  https://github.com/$GITHUB_USER/$REPO_NAME"
done
