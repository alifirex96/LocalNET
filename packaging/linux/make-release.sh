#!/bin/sh
# Упаковка Linux-архивов релиза.
#
#   packaging/linux/make-release.sh <версия, напр. v0.2.0> <dist/LocalNET (onedir) или "-"> <каталог вывода>
#
# Создаёт:
#   LocalNET-<версия>-linux-x86_64.tar.gz — собранная программа (Python не нужен);
#   LocalNET-<версия>-python.tar.gz       — исходники для системного python3
#                                           (Astra Linux с ЗПС, любые архитектуры).
# В каждом архиве: install.sh (установка без root), лаунчер, иконка, README, LICENSE.
set -eu

VERSION="$1"
ONEDIR="$2"
OUT="$3"
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PKG="$ROOT/packaging/linux"

mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

add_common() {
    cp "$PKG/install.sh" "$PKG/localnet-launcher.sh" "$PKG/localnet.png" "$1/"
    cp "$ROOT/README.md" "$ROOT/LICENSE" "$1/"
    chmod +x "$1/install.sh" "$1/localnet-launcher.sh"
}

# --- вариант для системного python3 ------------------------------------------
PY="LocalNET-$VERSION-python"
mkdir -p "$WORK/$PY"
cp -R "$ROOT/localnet" "$WORK/$PY/"
find "$WORK/$PY" -name '__pycache__' -type d -prune -exec rm -rf {} +
add_common "$WORK/$PY"
cat > "$WORK/$PY/localnet.sh" <<'EOF'
#!/bin/sh
# Запуск без установки: ./localnet.sh (GUI) или ./localnet.sh --cli
DIR=$(cd "$(dirname "$0")" && pwd)
PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m localnet "$@"
EOF
chmod +x "$WORK/$PY/localnet.sh"
tar -C "$WORK" -czf "$OUT/$PY.tar.gz" "$PY"
echo "$OUT/$PY.tar.gz"

# --- собранная программа -----------------------------------------------------
if [ "$ONEDIR" != "-" ]; then
    BIN="LocalNET-$VERSION-linux-x86_64"
    mkdir -p "$WORK/$BIN"
    cp -R "$ONEDIR/LocalNET" "$ONEDIR/_internal" "$WORK/$BIN/"
    chmod +x "$WORK/$BIN/LocalNET"
    add_common "$WORK/$BIN"
    tar -C "$WORK" -czf "$OUT/$BIN.tar.gz" "$BIN"
    echo "$OUT/$BIN.tar.gz"
fi

(cd "$OUT" && sha256sum LocalNET-"$VERSION"-*.tar.gz > SHA256SUMS.txt)
echo "$OUT/SHA256SUMS.txt"
