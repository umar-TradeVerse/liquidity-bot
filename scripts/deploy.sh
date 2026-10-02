#!/usr/bin/env bash
# Safe deploy: run the test suite, and only if it passes, commit + push
# EVERYTHING in one commit (one clean Railway restart).
#   Usage (from the repo folder):  ./scripts/deploy.sh "what changed"
set -e
cd "$(dirname "$0")/.."

echo "== 1/3  Running tests"
if ! python3 tests/run_tests.py; then
    echo ""
    echo "TESTS FAILED — nothing was pushed. Send the output above to Claude."
    exit 1
fi

echo ""
echo "== 2/3  Files in this deploy"
git add -A
git status --short
if git diff --cached --quiet; then
    echo "Nothing to deploy."
    exit 0
fi

MSG="${1:-Update}"
echo ""
read -r -p "Push these changes as one commit (\"$MSG\")? [y/N] " ok
[ "$ok" = "y" ] || [ "$ok" = "Y" ] || { git reset -q; echo "Cancelled — nothing pushed."; exit 0; }

echo "== 3/3  Pushing"
git commit -q -m "$MSG"
git push
echo ""
echo "Done. Railway will redeploy once. Send Claude the startup log."
