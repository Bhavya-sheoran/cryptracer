#!/usr/bin/env bash
# Hard gate on the project's non-negotiable frontend rule: JSX only.
# Fails if any TypeScript source, tsconfig, or TS tooling dependency appears.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
fail=0

ts_files=$(find frontend -type f \( -name '*.ts' -o -name '*.tsx' \) -not -path '*/node_modules/*' 2>/dev/null)
if [ -n "$ts_files" ]; then
  echo "FAIL: TypeScript source files found:"
  echo "$ts_files"
  fail=1
fi

ts_config=$(find frontend -maxdepth 2 -name 'tsconfig*.json' -not -path '*/node_modules/*' 2>/dev/null)
if [ -n "$ts_config" ]; then
  echo "FAIL: tsconfig present:"
  echo "$ts_config"
  fail=1
fi

if grep -qE '"(typescript|@typescript-eslint/[a-z-]+|@types/[a-z-]+)"' frontend/package.json 2>/dev/null; then
  echo "FAIL: TypeScript tooling declared in frontend/package.json"
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  jsx_count=$(find frontend/src -name '*.jsx' | wc -l | tr -d ' ')
  js_count=$(find frontend/src -name '*.js' | wc -l | tr -d ' ')
  echo "PASS: no TypeScript. frontend/src has ${jsx_count} .jsx and ${js_count} .js files."
fi

exit "$fail"
