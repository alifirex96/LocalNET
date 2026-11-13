#!/bin/sh
# Установка LocalNET для текущего пользователя — БЕЗ прав root.
#
#   ./install.sh              установить (или обновить)
#   ./install.sh --uninstall  удалить (настройки и полученные файлы остаются)
#
# Что делает:
#   * копирует программу в ~/.local/share/localnet
#   * создаёт команду ~/.local/bin/localnet
#   * добавляет ярлык в меню приложений и на рабочий стол
#   * проверяет, что программа запускается, и подсказывает, если нет
#
# Работает из распакованного архива любого варианта:
#   LocalNET-*-linux-x86_64  — собранная программа (Python не нужен)
#   LocalNET-*-python        — исходный код для системного python3 (Astra Linux с ЗПС)

set -u

SRC_DIR=$(cd "$(dirname "$0")" && pwd)
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
PREFIX="$DATA_HOME/localnet"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$DATA_HOME/applications"
DESKTOP_FILE_NAME="localnet.desktop"

desktop_dir() {
    d=""
    if command -v xdg-user-dir >/dev/null 2>&1; then
        d=$(xdg-user-dir DESKTOP 2>/dev/null)
    fi
    if [ -z "$d" ] || [ "$d" = "$HOME" ]; then
        for c in "$HOME/Desktop" "$HOME/Рабочий стол" "$HOME/Desktops/Desktop1"; do
            [ -d "$c" ] && { d="$c"; break; }
        done
    fi
    printf '%s' "$d"
}

say()  { printf '%s\n' "$*"; }
warn() { printf 'ВНИМАНИЕ: %s\n' "$*" >&2; }
die()  { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
    rm -rf "$PREFIX" "$BIN_DIR/localnet" "$APPS_DIR/$DESKTOP_FILE_NAME"
    DESK=$(desktop_dir)
    [ -n "$DESK" ] && rm -f "$DESK/$DESKTOP_FILE_NAME"
    say "LocalNET удалён. Настройки и журналы: ${XDG_CONFIG_HOME:-$HOME/.config}/localnet"
    exit 0
fi

# --- какой вариант распакован ------------------------------------------------
if [ -f "$SRC_DIR/LocalNET" ] && [ -d "$SRC_DIR/_internal" ]; then
    MODE=binary
elif [ -f "$SRC_DIR/localnet/__main__.py" ]; then
    MODE=python
else
    die "рядом с install.sh нет ни программы LocalNET, ни папки localnet/. Распакуйте архив целиком."
fi
say "Вариант: $([ "$MODE" = binary ] && echo 'собранная программа' || echo 'системный python3')"

# --- диагностика системы -----------------------------------------------------
GLIBC=$(ldd --version 2>/dev/null | head -n1 | grep -oE '[0-9]+\.[0-9]+$')
[ -n "$GLIBC" ] && say "glibc: $GLIBC"

ZPS=no
if [ -r /etc/digsig/digsig_initramfs.conf ] && grep -qE '^[[:space:]]*DIGSIG_ELF_MODE[[:space:]]*=[[:space:]]*1' /etc/digsig/digsig_initramfs.conf; then
    ZPS=yes
fi
if [ "$ZPS" = yes ]; then
    say "Обнаружена замкнутая программная среда (ЗПС) Astra Linux."
    if [ "$MODE" = binary ]; then
        warn "при включённой ЗПС неподписанная программа не запустится."
        warn "Используйте архив LocalNET-*-python.tar.gz (работает на подписанном системном python3)"
        warn "или попросите администратора подписать файлы программы."
    fi
fi

if [ "$MODE" = python ]; then
    command -v python3 >/dev/null 2>&1 || die "не найден python3. Администратор: sudo apt install python3"
    if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
        die "нужен Python 3.8 или новее, установлен: $(python3 -V 2>&1)"
    fi
    say "Python: $(python3 -V 2>&1)"
    if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
        warn "нет модуля tkinter — окно программы не откроется (консольный режим работает)."
        warn "Администратор может установить его: sudo apt install python3-tk"
    fi
    if python3 -c 'import zmq' >/dev/null 2>&1; then
        say "pyzmq: есть (будет использован)"
    else
        say "pyzmq: нет — используется встроенный совместимый транспорт на чистом Python"
    fi
fi

# --- копирование -------------------------------------------------------------
mkdir -p "$DATA_HOME" "$BIN_DIR" "$APPS_DIR" || die "не удалось создать каталоги в $HOME"
TMP="$PREFIX.new.$$"
rm -rf "$TMP"
mkdir -p "$TMP"
if [ "$MODE" = binary ]; then
    cp -R "$SRC_DIR/LocalNET" "$SRC_DIR/_internal" "$TMP/" || die "копирование не удалось"
    chmod +x "$TMP/LocalNET"
else
    cp -R "$SRC_DIR/localnet" "$TMP/" || die "копирование не удалось"
    [ -d "$SRC_DIR/tkinterdnd2" ] && cp -R "$SRC_DIR/tkinterdnd2" "$TMP/"
    find "$TMP/localnet" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null
fi
cp "$SRC_DIR/localnet.png" "$TMP/" 2>/dev/null
rm -rf "$PREFIX"
mv "$TMP" "$PREFIX" || die "не удалось переместить в $PREFIX"

# --- команда localnet ---------------------------------------------------------
sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@MODE@|$MODE|g" "$SRC_DIR/localnet-launcher.sh" > "$BIN_DIR/localnet" \
    || die "не удалось создать $BIN_DIR/localnet"
chmod +x "$BIN_DIR/localnet"

# --- ярлыки ------------------------------------------------------------------
write_desktop() {
    cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=LocalNET
Name[ru]=LocalNET
GenericName=LAN messenger
GenericName[ru]=Мессенджер локальной сети
Comment=Chat and file transfer in the local network
Comment[ru]=Чат и передача файлов в локальной сети
Exec="$BIN_DIR/localnet"
Icon=$PREFIX/localnet.png
Terminal=false
Categories=Network;Chat;FileTransfer;
StartupNotify=true
EOF
    chmod +x "$1"
}
write_desktop "$APPS_DIR/$DESKTOP_FILE_NAME"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1
DESK=$(desktop_dir)
if [ -n "$DESK" ] && [ -d "$DESK" ]; then
    write_desktop "$DESK/$DESKTOP_FILE_NAME"
    # GNOME/Nautilus требует пометить ярлык доверенным; в Fly и KDE не нужно.
    command -v gio >/dev/null 2>&1 && gio set "$DESK/$DESKTOP_FILE_NAME" metadata::trusted true >/dev/null 2>&1
    say "Ярлык на рабочем столе: $DESK/$DESKTOP_FILE_NAME"
fi

# --- проверка запуска --------------------------------------------------------
if [ "$MODE" = binary ]; then
    CHECK=$("$PREFIX/LocalNET" --version 2>&1)
else
    CHECK=$(PYTHONPATH="$PREFIX" python3 -m localnet --version 2>&1)
fi
if [ $? -eq 0 ]; then
    say "Проверка запуска: $CHECK — OK"
else
    warn "программа установлена, но не запускается:"
    printf '%s\n' "$CHECK" | tail -n 8 >&2
    if printf '%s' "$CHECK" | grep -q GLIBC_; then
        warn "несовместимая версия glibc — используйте архив LocalNET-*-python.tar.gz"
    elif [ "$MODE" = binary ]; then
        warn "если включена ЗПС — используйте архив LocalNET-*-python.tar.gz"
    fi
    exit 2
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) say "Команда localnet доступна после повторного входа в систему (или: $BIN_DIR/localnet)";;
esac
say ""
say "Готово. Запуск: меню приложений → LocalNET, ярлык на рабочем столе или команда localnet"
say "Консольный режим: localnet --cli. Удаление: $0 --uninstall"
