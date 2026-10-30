#!/usr/bin/env bash
# Static audit for installed agent skills.
#
# Skills are instruction documents plus optional scripts that run with the
# user's own privileges on the host, so a malicious skill is a supply-chain
# hole rather than a sandboxed plugin. This script reads skills and reports
# risky patterns; it never executes anything it finds.
#
# Usage:
#   scripts/audit-skills.sh [dir ...]
# Defaults to the known skill roots when no directory is given.
# Exit code 1 when CRITICAL findings exist, 2 when only WARN.

set -uo pipefail

if [ "$#" -gt 0 ]; then
  ROOTS=("$@")
else
  ROOTS=("$HOME/.workbuddy/skills" "$HOME/.codebuddy/skills"
         "$PWD/.workbuddy/skills" "$PWD/.codebuddy/skills")
fi

CRITICAL_PATTERN='curl[^|]*\|[[:space:]]*(ba)?sh|wget[^|]*\|[[:space:]]*sh|base64[[:space:]]+-d|/dev/tcp/|(^|[^a-z])nc[[:space:]]+-[el]|launchctl[[:space:]]+(load|submit)|crontab[[:space:]]|osascript[[:space:]]|~/(\.ssh/id_|\.aws/credentials|\.gnupg)|rm[[:space:]]+-rf[[:space:]]+[~/$]|git[[:space:]]+push[^|]*--force|security[[:space:]]+find-generic-password'

WARN_PATTERN='sudo[[:space:]]|eval[[:space:]]|curl[^|]*(-d|--data|-T)[[:space:]]|nohup[[:space:]]|\.zshrc|\.bashrc|\.bash_profile|MY_SECRETS|api[_-]?key|chmod[[:space:]]+\+x'

SCRIPT_GLOB=('*.py' '*.sh' '*.bash' '*.zsh' '*.js' '*.ts' '*.rb' '*.pl' '*.command' '*.exe' '*.dmg' '*.pkg')

found_critical=0
found_warn=0
scanned=0

section() { printf '\n=== %s ===\n' "$1"; }
note() { printf '  %-8s %s\n' "$1" "$2"; }

for root in "${ROOTS[@]}"; do
  [ -d "$root" ] || continue
  # Skills live one level below a skills root, or inside a marketplace tree.
  while IFS= read -r skill; do
    scanned=$((scanned + 1))
    name=$(basename "$skill")
    section "$name  ($skill)"

    meta=""
    for candidate in "$skill/_meta.json" "$skill/metadata.json" "$skill/config.json"; do
      [ -f "$candidate" ] && meta="$candidate" && break
    done
    if [ -n "$meta" ]; then
      ver=$(grep -oE '"(version|source|namespace)"[^,}]*' "$meta" 2>/dev/null | tr -d '"' | paste -sd' ' -)
      note "meta" "${ver:-$meta}"
    else
      note "meta" "no metadata file"
    fi

    match=()
    for glob in "${SCRIPT_GLOB[@]}"; do
      [ "${#match[@]}" -gt 0 ] && match+=(-o)
      match+=(-name "$glob")
    done
    scripts=()
    while IFS= read -r file; do scripts+=("$file"); done < <(
      find "$skill" -type f \( "${match[@]}" \) 2>/dev/null
    )
    if [ "${#scripts[@]}" -gt 0 ]; then
      for file in "${scripts[@]}"; do
        note "script" "${file#$skill/}"
      done
    else
      note "script" "none"
    fi

    critical=$(grep -rnEi "$CRITICAL_PATTERN" "$skill" --include='*.md' --include='*.py' --include='*.sh' --include='*.js' --include='*.json' --include='*.yaml' --include='*.yml' 2>/dev/null | head -10)
    if [ -n "$critical" ]; then
      found_critical=1
      while IFS= read -r line; do note "CRITICAL" "${line#$skill/}"; done <<<"$critical"
    fi

    warn=$(grep -rnEi "$WARN_PATTERN" "$skill" --include='*.md' --include='*.py' --include='*.sh' --include='*.js' 2>/dev/null | head -10)
    if [ -n "$warn" ]; then
      found_warn=1
      while IFS= read -r line; do note "WARN" "${line#$skill/}"; done <<<"$warn"
    fi

    if [ -z "$critical$warn" ]; then
      note "result" "clean"
    fi
  done < <(
    {
      find "$root" -maxdepth 2 -name 'SKILL.md' -o -maxdepth 3 -name 'SKILL.md' 2>/dev/null
    } | xargs -r -n1 dirname | sort -u
  )
done

printf '\n----------------------------------------\n'
printf 'scanned skills: %d\n' "$scanned"
if [ "$found_critical" -eq 1 ]; then
  printf 'verdict: CRITICAL findings present. Review before loading.\n'
  exit 1
elif [ "$found_warn" -eq 1 ]; then
  printf 'verdict: warnings only. Read the matched lines before trusting.\n'
  exit 2
fi
printf 'verdict: clean\n'
