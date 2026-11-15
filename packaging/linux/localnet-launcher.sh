#!/bin/sh
# Лаунчер LocalNET. Устанавливается install.sh в ~/.local/bin/localnet.
#
# Зачем нужен: при запуске двойным щелчком (меню, ярлык на рабочем столе) нет
# терминала, и если программа не стартует (несовместимая библиотека, запрет ЗПС,
# нет python3-tk), пользователь не видит ничего. Лаунчер сохраняет вывод в журнал
# и при ошибке показывает окно с причиной.

PREFIX="@PREFIX@"
MODE="@MODE@"   # binary | python

LOG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/localnet/logs"
mkdir -p "$LOG_DIR" 2>/dev/null
ERR_LOG="$LOG_DIR/launcher.log"

if [ "$MODE" = "binary" ]; then
    set -- "$PREFIX/LocalNET" "$@"
else
    PYTHONPATH="$PREFIX${PYTHONPATH:+:$PYTHONPATH}"
    export PYTHONPATH
    set -- python3 -m localnet "$@"
fi

# Из терминала — обычный запуск: вывод виден сразу, работает --cli.
if [ -t 0 ] || [ -t 1 ]; then
    exec "$@"
fi

"$@" >"$ERR_LOG" 2>&1
rc=$?
# 3 — программа сама показала окно с ошибкой; 130/143 — прервана пользователем.
if [ "$rc" -eq 0 ] || [ "$rc" -eq 3 ] || [ "$rc" -eq 130 ] || [ "$rc" -eq 143 ]; then
    exit "$rc"
fi

hint=""
if grep -q "GLIBC_" "$ERR_LOG" 2>/dev/null; then
    hint="Сборка несовместима с версией системных библиотек. Используйте вариант LocalNET для системного Python (архив *-python.tar.gz)."
elif grep -qiE "permission denied|отказано в доступе|операция не позволена|operation not permitted|bad signature|digsig" "$ERR_LOG" 2>/dev/null; then
    hint="Запуск запрещён политикой безопасности. В Astra Linux при включённой замкнутой программной среде (ЗПС) неподписанные программы не запускаются: используйте вариант LocalNET для системного Python (архив *-python.tar.gz) или попросите администратора подписать программу."
elif grep -q "No module named" "$ERR_LOG" 2>/dev/null; then
    hint="Не хватает модуля Python (см. ниже). Для окна программы нужен пакет python3-tk."
fi

text="LocalNET не запустился (код $rc).
$hint

Последние строки журнала ($ERR_LOG):
$(tail -n 12 "$ERR_LOG" 2>/dev/null)"

if command -v zenity >/dev/null 2>&1; then
    zenity --error --no-markup --title "LocalNET" --text "$text" 2>/dev/null
elif command -v kdialog >/dev/null 2>&1; then
    kdialog --title "LocalNET" --error "$text" 2>/dev/null
elif command -v xmessage >/dev/null 2>&1; then
    xmessage -center "$text" 2>/dev/null
elif command -v notify-send >/dev/null 2>&1; then
    notify-send "LocalNET не запустился" "$hint Журнал: $ERR_LOG"
fi
exit "$rc"
