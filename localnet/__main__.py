"""Точка входа: ``python -m localnet`` (GUI) или ``python -m localnet --cli``."""

from __future__ import annotations

import argparse
import logging
import platform
import shutil
import subprocess
import sys
import traceback

# Абсолютные импорты — чтобы файл работал и как точка входа PyInstaller.
from localnet import APP_NAME, __version__
from localnet.app import LocalNetCore, setup_logging
from localnet.config import load_config
from localnet.utils import user_config_dir

log = logging.getLogger("localnet")


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
    p.add_argument("--transport", choices=["auto", "zmq", "python"],
                   help="транспорт сообщений (python — без pyzmq, для Astra Linux с ЗПС)")
    p.add_argument("--auto-accept", action="store_true", help="принимать файлы без подтверждения")
    p.add_argument("--debug", action="store_true", help="подробный лог")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return p.parse_args(argv)


def show_fatal_error(text: str) -> None:
    """Показывает ошибку так, чтобы её увидел пользователь, запустивший программу двойным щелчком.

    Порядок: окно tkinter → zenity / kdialog / xmessage (есть почти в любой
    графической среде Linux, включая Fly в Astra Linux) → stderr.
    """
    if sys.stderr is not None:
        print(text, file=sys.stderr)
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, text)
        root.destroy()
        return
    except Exception:  # noqa: BLE001 - нет tkinter или нет дисплея
        pass
    if sys.platform.startswith("linux"):
        for cmd in (
            ["zenity", "--error", "--no-markup", "--title", APP_NAME, "--text", text],
            ["kdialog", "--title", APP_NAME, "--error", text],
            ["xmessage", "-center", f"{APP_NAME}: {text}"],
        ):
            if shutil.which(cmd[0]):
                try:
                    subprocess.run(cmd, timeout=600, check=False)
                    return
                except (OSError, subprocess.SubprocessError):
                    continue


# Код выхода «ошибка уже показана пользователю» — лаунчер не дублирует окно.
EXIT_ERROR_SHOWN = 3


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log_file = user_config_dir() / "logs" / "localnet.log"
    try:
        return _run(args)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - любая ошибка старта должна быть видна
        if isinstance(exc, KeyboardInterrupt):
            return 130
        log.critical("Аварийное завершение:\n%s", traceback.format_exc())
        show_fatal_error(f"Программа завершилась с ошибкой:\n\n{exc.__class__.__name__}: {exc}\n\n"
                         f"Подробности в журнале:\n{log_file}")
        return EXIT_ERROR_SHOWN


def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging("DEBUG" if args.debug else config.log_level)
    log.info("%s %s, Python %s, %s", APP_NAME, __version__, platform.python_version(), platform.platform())

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
    if args.transport:
        config.transport = args.transport
    if args.auto_accept:
        config.auto_accept = True
    config.validate()
    log.info("Конфиг: %s", config.path)

    if args.cli:
        from localnet.cli import CliApp
        return CliApp(LocalNetCore(config)).run()
    try:
        from localnet.gui import LocalNetGui
    except ImportError as exc:  # tkinter может отсутствовать в минимальных сборках Linux
        msg = (f"Графический интерфейс недоступен: {exc}.\n\n"
               "Установите пакет python3-tk (администратор: sudo apt install python3-tk)\n"
               "или запустите в терминале консольный режим: localnet --cli")
        log.error(msg)
        show_fatal_error(msg)
        return EXIT_ERROR_SHOWN
    return LocalNetGui(LocalNetCore(config)).run()


if __name__ == "__main__":
    sys.exit(main())
