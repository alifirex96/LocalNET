# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спецификация LocalNET: один исполняемый файл без установки.

Сборка:
    pyinstaller --clean --noconfirm build.spec

Результат: dist/LocalNET.exe (Windows) или dist/LocalNET (Linux).
По умолчанию собирается оконное приложение без консоли в виде одного файла.

Для Linux рекомендуется сборка «папкой» (onedir): она не распаковывает библиотеки
во временный каталог при каждом запуске, поэтому работает, когда /tmp смонтирован
с noexec (частая настройка защищённых систем, в т.ч. Astra Linux), и стартует быстрее:
    LOCALNET_ONEDIR=1 pyinstaller --clean --noconfirm build.spec   → dist/LocalNET/LocalNET
 Для отладочной сборки
с консолью (нужна для режима --cli на Windows):
    set LOCALNET_CONSOLE=1   (PowerShell: $env:LOCALNET_CONSOLE=1)
    pyinstaller --clean --noconfirm build.spec
"""

import os

console = os.environ.get("LOCALNET_CONSOLE", "") == "1"
onedir = os.environ.get("LOCALNET_ONEDIR", "") == "1"

a = Analysis(
    ["localnet_launcher.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["zmq", "zmq.backend.cython"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "unittest", "pydoc", "doctest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

name = "LocalNET-console" if console else "LocalNET"

if onedir:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        debug=False,
        strip=False,
        upx=False,
        console=console,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=name)
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=name,
        debug=False,
        strip=False,
        upx=False,          # UPX часто вызывает ложные срабатывания антивирусов
        runtime_tmpdir=None,
        console=console,
        disable_windowed_traceback=False,
    )
