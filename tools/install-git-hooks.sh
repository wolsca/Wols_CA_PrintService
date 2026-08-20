#!/usr/bin/env bash
#
# Points git at tools/git-hooks, so the commit number is raised on every commit.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

chmod +x tools/git-hooks/* 2>/dev/null || true
git config core.hooksPath tools/git-hooks

echo "Git hooks enabled (core.hooksPath = tools/git-hooks)."
echo "Current version: $(python3 tools/bump_version.py --show)"
