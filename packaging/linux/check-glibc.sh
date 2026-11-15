#!/bin/sh
# Проверяет, что собранная программа не требует glibc новее заданной.
#   packaging/linux/check-glibc.sh dist/LocalNET 2.35
# 2.35 — Ubuntu 22.04; Astra Linux 1.8 / Debian 12 — 2.36, Astra 1.7 / Debian 10 — 2.28.
set -eu
DIR="$1"
LIMIT="GLIBC_$2"
report=$(find "$DIR" -type f \( -name '*.so*' -o -name 'LocalNET' \) -exec sh -c '
    v=$(objdump -T "$1" 2>/dev/null | grep -oE "GLIBC_[0-9]+\.[0-9]+" | sort -Vu | tail -n1)
    [ -n "$v" ] && echo "$v $1"' _ {} \; | sort -V)
MAX=$(printf '%s\n' "$report" | tail -n1 | cut -d' ' -f1)
echo "Максимальная требуемая версия: $MAX (лимит $LIMIT)"
printf '%s\n' "$report" | tail -n5
if [ "$(printf '%s\n%s\n' "$MAX" "$LIMIT" | sort -V | tail -n1)" != "$LIMIT" ]; then
    echo "ОШИБКА: сборка требует $MAX — не запустится на системах со старой glibc" >&2
    exit 1
fi
