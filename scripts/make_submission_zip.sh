#!/bin/bash
# Build a clean submission archive: no API key, no run ledgers, no backups.
# Usage: bash scripts/make_submission_zip.sh   -> dist/bridge-lab-submission.zip
set -e
cd "$(dirname "$0")/.."
mkdir -p dist
rm -f dist/bridge-lab-submission.zip
zip -r dist/bridge-lab-submission.zip . \
  -x ".env" "*/.env" "dist/*" "server/runs/*" "server/tests/*" "server/fixtures/.DS_Store" \
     "*.bak*" "*.backup-*" "*/__pycache__/*" "__pycache__/*" "*.pyc" ".DS_Store" "*/.DS_Store" ".git/*" > /dev/null
echo "Wrote dist/bridge-lab-submission.zip"
echo "Check for secrets:"
if unzip -l dist/bridge-lab-submission.zip | grep -E "(^|/)\.env$"; then echo "FOUND .env - do not submit"; exit 1; else echo "no .env inside"; fi
