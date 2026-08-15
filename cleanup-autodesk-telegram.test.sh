#!/bin/bash

set -euo pipefail

source '/Users/annagorelova/work/elections/cleanup-autodesk-telegram.sh'

cleanup_test_root="$(mktemp -d "${TMPDIR:-/tmp}/cleanup-autodesk-test.XXXXXX")"
cleanup_test_file="$cleanup_test_root/immutable.f3d"

cleanup_test_teardown() {
  /usr/bin/chflags -R nouchg "$cleanup_test_root" 2>/dev/null || true
  /usr/bin/find "$cleanup_test_root" -depth -delete 2>/dev/null || true
}
trap cleanup_test_teardown EXIT

touch "$cleanup_test_file"
/usr/bin/chflags uchg "$cleanup_test_file"

if remove_tree "$cleanup_test_root" && [[ ! -e "$cleanup_test_root" ]]; then
  echo 'PASS: immutable Fusion-style files are removed'
else
  echo 'FAIL: immutable Fusion-style files survive cleanup' >&2
  exit 1
fi
