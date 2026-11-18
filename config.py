"""Configuration helpers for tg-video.

Application secrets live in .env on the server. The MTProxy secret is read
from the separate file used by the Flowseal tg-ws-proxy deployment.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from telethon import TelegramClient, connection

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
try:
    DATA_DIR.chmod(0o700)
except OSError:
    pass

load_dotenv(ROOT_DIR / ".env")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная {name} в .env")
    return value


def int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Переменная {name} должна быть целым числом") from exc


def _normalise_mtproxy_secret(value: str) -> str:
    secret = value.strip().strip('"').strip("'")
    if secret.startswith("dd"):
        raw = secret[2:]
    else:
        raw = secret
    if not re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        raise RuntimeError("Секрет MTProxy должен содержать 32 hex-символа (с необязательным префиксом dd)")
    return "dd" + raw.lower()


def get_mtproxy_secret() -> str:
    # Environment variable takes precedence, which is useful for systemd.
    direct = os.getenv("TG_WS_PROXY_SECRET", "").strip()
    if direct:
        return _normalise_mtproxy_secret(direct)

    default_file = Path.home() / ".config/tg-ws-proxy-test/proxy.env"
    env_file = Path(os.getenv("TG_WS_PROXY_ENV_FILE", str(default_file))).expanduser()
    if not env_file.is_file():
        raise RuntimeError(
            "Не найден файл секрета tg-ws-proxy: "
            f"{env_file}. Укажите TG_WS_PROXY_ENV_FILE или настройте TG_WS_PROXY_SECRET."
        )

    values = dotenv_values(env_file)
    for key in ("TG_WS_PROXY_SECRET", "SECRET", "secret"):
        value = values.get(key)
        if value:
            return _normalise_mtproxy_secret(str(value))

    # Also accept a file containing only the hexadecimal secret.
    for line in env_file.read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if re.fullmatch(r"(?:dd)?[0-9a-fA-F]{32}", candidate):
            return _normalise_mtproxy_secret(candidate)

    raise RuntimeError(
        f"В {env_file} не найден TG_WS_PROXY_SECRET (сам секрет не выводится)."
    )


def make_telegram_client(session_name: str = "session") -> TelegramClient:
    api_id = int_env("API_ID", 0)
    if api_id <= 0:
        raise RuntimeError("API_ID должен быть положительным целым числом")
    api_hash = required_env("API_HASH")

    host = os.getenv("TG_WS_PROXY_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int_env("TG_WS_PROXY_PORT", 1443)
    if not 1 <= port <= 65535:
        raise RuntimeError("TG_WS_PROXY_PORT должен быть в диапазоне 1..65535")

    # Flowseal tg-ws-proxy speaks MTProto, not SOCKS5.
    return TelegramClient(
        str(DATA_DIR / session_name),
        api_id,
        api_hash,
        connection=connection.ConnectionTcpMTProxyRandomizedIntermediate,
        proxy=(host, port, get_mtproxy_secret()),
        connection_retries=5,
        request_retries=3,
        retry_delay=2,
        auto_reconnect=True,
    )
