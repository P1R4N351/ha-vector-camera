#!/usr/bin/env bash
# check.sh — run metadata validation, unit/entity tests, and mutation suite.
# Usage: check.sh [--quick] [-h|--help]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PYTHON="${PYTHON:-python3}"
QUICK=0

usage() { sed -n '2,3p' "$0"; exit "${1:-0}"; }

parse_args() {
  local i
  for ((i = 0; i < 4 && $# > 0; i++)); do
    case "$1" in
      --quick) QUICK=1; shift ;;
      -h|--help) usage 0 ;;
      *) echo "unknown argument: $1" >&2; usage 2 ;;
    esac
  done
  [[ $# -eq 0 ]] || usage 2
}

main() {
  parse_args "$@"
  command -v "$PYTHON" >/dev/null || { echo "python missing: $PYTHON" >&2; exit 3; }
  "$PYTHON" -m unittest discover -s "$HERE" -v
  if [[ $QUICK -eq 0 ]]; then
    "$PYTHON" "$HERE/mutate.py"
  fi
  echo "check.sh PASS (root=$ROOT quick=$QUICK)"
}

main "$@"
