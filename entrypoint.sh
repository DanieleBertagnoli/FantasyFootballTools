#!/bin/sh
set -e

REPO_URL="https://github.com/DanieleBertagnoli/FantasyFootballTools.git"
BRANCH="master"
REPO_DIR="/app/repo"

if [ ! -d "$REPO_DIR/.git" ]; then
    echo "Cloning the repo..."
    git clone --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
else
    echo "Updating the repo..."
    cd "$REPO_DIR"
    git fetch origin
    git reset --hard "origin/$BRANCH"
fi

pip install --no-cache-dir "flask>=3.1.3"

cd "$REPO_DIR/src"
exec flask run --host=0.0.0.0 --port=5010
