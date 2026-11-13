# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спецификация LocalNET: один исполняемый файл без установки.

Сборка:
    pyinstaller --clean --noconfirm build.spec

Результат: dist/LocalNET.exe (Windows) или dist/LocalNET (Linux).
По умолчанию собирается оконное приложение без консоли. Для отладочной сборки
с консолью (нужна для режима --cli на Windows):
    set LOCALNET_CONSOLE=1   (PowerShell: $env:LOCALNET_CONSOLE=1)
    pyinstaller --clean --noconfirm build.spec
"""

import os

console = os.environ.get("LOCALNET_CONSOLE", "") == "1"

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

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="LocalNET-console" if console else "LocalNET",
    debug=False,
    strip=False,
    upx=False,          # UPX часто вызывает ложные срабатывания антивирусов
    runtime_tmpdir=None,
    console=console,
    disable_windowed_traceback=False,
)
