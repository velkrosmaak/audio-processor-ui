#!/usr/bin/env bash

# Usage:
# ./publish.sh repo-name public
# ./publish.sh repo-name private

set -e

REPO_NAME="$1"
VISIBILITY="${2:-private}"

if [ -z "$REPO_NAME" ]; then
  echo "Usage: ./publish.sh <repo-name> [public|private]"
  exit 1
fi

# Check GitHub CLI
if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI (gh) is not installed."
  echo "Install with: brew install gh"
  exit 1
fi

# Check git
if ! command -v git >/dev/null 2>&1; then
  echo "git is not installed."
  exit 1
fi

# Initialize git if needed
if [ ! -d ".git" ]; then
  echo "Initializing git repo..."
  git init
fi

# Create default .gitignore if missing
if [ ! -f ".gitignore" ]; then
cat > .gitignore <<EOF
__pycache__/
*.pyc
.env
venv/
.DS_Store
EOF
fi

# Stage files
git add .

# Commit if there are changes
if ! git diff --cached --quiet; then
  git commit -m "Initial commit"
fi

# Create and push GitHub repo
gh repo create "$REPO_NAME" \
  --"$VISIBILITY" \
  --source=. \
  --remote=origin \
  --push

echo
echo "Done."
