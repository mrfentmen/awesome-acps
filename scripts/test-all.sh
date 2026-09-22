#!/usr/bin/env bash
# Run every test file in the repo and report a total. Offline: no network is used.
set -u

cd "$(dirname "$0")/.." || exit 1

python="${PYTHON:-python3}"
total=0
failed_files=0

for file in tests/test_kit.py agents/*/tests/test_agent.py; do
  output="$("$python" "$file" 2>&1)"
  ran="$(printf '%s\n' "$output" | grep -oE '^Ran [0-9]+' | grep -oE '[0-9]+')"
  if printf '%s\n' "$output" | grep -q '^OK'; then
    printf '  %-46s %-5s ok\n' "$file" "${ran:-?}"
    total=$((total + ${ran:-0}))
  else
    printf '  %-46s FAILED\n' "$file"
    printf '%s\n' "$output" | tail -20
    failed_files=$((failed_files + 1))
  fi
done

echo
echo "== $total tests in $(( $(ls -d agents/*/ | wc -l | tr -d ' ') + 1 )) files"
if [ "$failed_files" -gt 0 ]; then
  echo "== $failed_files file(s) failed"
  exit 1
fi
echo "== all test files passed"
