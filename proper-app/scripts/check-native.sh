#!/usr/bin/env sh
set -eu

missing=""
for command in bun psql createdb python3; do
  if ! command -v "$command" >/dev/null 2>&1; then
    missing="$missing $command"
  fi
done

if [ -n "$missing" ]; then
  echo "Missing native tools:$missing" >&2
  exit 1
fi

bun --version
psql --version
python3 --version
