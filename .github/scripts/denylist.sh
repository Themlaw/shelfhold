#!/usr/bin/env bash
# Echoue si une regex de DENYLIST_NAMES (une par ligne) matche le nom ou le contenu
# d'un fichier suivi par git. N'affiche jamais la regex ni le texte trouve.
set -euo pipefail

if [ -z "${DENYLIST_NAMES:-}" ]; then
  echo "::warning::DENYLIST_NAMES est vide ou absent (PR de fork ?) : controle saute, a relancer avant fusion."
  exit 0
fi

status=0
n=0
while IFS= read -r pattern || [ -n "$pattern" ]; do
  [ -z "$pattern" ] && continue
  n=$((n + 1))
  hits=$(git ls-files | grep -i -E -e "$pattern" || true)
  hits+=$'\n'$(git grep -I -i -l -E -e "$pattern" || true)
  hits=$(printf '%s\n' "$hits" | sed '/^$/d' | sort -u)
  if [ -n "$hits" ]; then
    status=1
    echo "::error::la regle numero $n de la denylist matche :"
    printf '%s\n' "$hits" | sed 's/^/  - /'
  fi
done <<< "$DENYLIST_NAMES"

echo "denylist : $n regle(s) evaluee(s)"
exit $status
