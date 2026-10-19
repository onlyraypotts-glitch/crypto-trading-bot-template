#!/bin/sh
# pre-commit secret scan (gitleaks if present, else rg patterns)
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks git --pre-commit --staged --redact -v || { echo "gitleaks: possible secret staged. Commit blocked."; exit 1; }
else
  if git diff --cached -U0 | grep -E '^\+' | grep -Eq 'BEGIN [A-Z ]*PRIVATE KEY|gh[pousr]_[A-Za-z0-9]{36}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|xox[bpa]-|[0-9]{8,10}:AA[A-Za-z0-9_-]{30}'; then
    echo "Possible secret staged. Commit blocked."; exit 1; fi
fi
