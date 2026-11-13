"""Точка входа: ``python -m localnet`` (GUI) или ``python -m localnet --cli``."""

from __future__ import annotations

import argparse
import logging
import sys

# Абсолютные импорты — чтобы файл работал и как точка входа PyInstaller.
from localnet import APP_NAME, __version__
from localnet.app import LocalNetCore, setup_logging
from localnet.config import load_config


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="localnet", description=f"{APP_NAME} — LAN-мессенджер и файлообменник")
    p.add_argument("--cli", action="store_true", help="консольный режим вместо GUI")
    p.add_argument("--config", help="путь к файлу настроек JSON")
    p.add_argument("--nickname", help="ник (сохраняется в настройках)")
    p.add_argument("--msg-port", type=int, help="TCP-порт сообщений")
    p.add_argument("--file-port", type=int, help="TCP-порт файлов")
    p.add_argument("--discovery-port", type=int, help="UDP-порт обнаружения (одинаковый у всех узлов)")
    p.add_argument("--download-dir", help="папка для входящих файлов")
    p.add_argument("--secret", help="общий секрет (узлы с другим секретом не видны)")
    p.add_argument("--auto-accept", action="store_true", help="принимать файлы без подтверждения")
    p.add_argument("--debug", action="store_true", help="подробный лог")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    setup_logging("DEBUG" if args.debug else config.log_level)

    # Аргументы командной строки переопределяют конфиг (только на этот запуск,
    # кроме ника и папки загрузок, которые меняются и из GUI).
    if args.nickname:
        config.nickname = args.nickname
    if args.msg_port is not None:
        config.msg_port = args.msg_port
    if args.file_port is not None:
        config.file_port = args.file_port
    if args.discovery_port is not None:
        config.discovery_port = args.discovery_port
    if args.download_dir:
        config.download_dir = args.download_dir
    if args.secret is not None:
        config.secret = args.secret
    if args.auto_accept:
        config.auto_accept = True
    config.validate()
    logging.getLogger(__name__).info("Конфиг: %s", config.path)

    core = LocalNetCore(config)
    if args.cli:
        from localnet.cli import CliApp
        return CliApp(core).run()
    try:
        from localnet.gui import LocalNetGui
    except ImportError as exc:  # tkinter может отсутствовать в минимальных сборках Linux
        print(f"GUI недоступен ({exc}). Установите python3-tk или запустите с --cli.", file=sys.stderr)
        return 2
    return LocalNetGui(core).run()


if __name__ == "__main__":
    sys.exit(main())
