from __future__ import annotations

import asyncio
import getpass
import logging

import qrcode
from telethon.errors import SessionPasswordNeededError

from config import DATA_DIR, make_telegram_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
client = make_telegram_client()
QR_IMAGE = DATA_DIR / "login-qr.png"


def show_qr(url: str) -> None:
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make()
    qr.print_ascii(invert=True)
    qr.make_image().save(QR_IMAGE)
    try:
        QR_IMAGE.chmod(0o600)
    except OSError:
        pass
    print(f"QR image saved privately to: {QR_IMAGE}")


async def main() -> None:
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Already authorized. Telegram user_id={me.id}")
            return

        qr_login = await client.qr_login()
        while True:
            show_qr(qr_login.url)
            print(
                "\nНа телефоне открой Telegram: Настройки → Устройства → "
                "Подключить устройство, затем отсканируй QR."
            )
            try:
                await qr_login.wait(timeout=60)
                break
            except asyncio.TimeoutError:
                print("QR expired; generating a new one...\n")
                await qr_login.recreate()
            except SessionPasswordNeededError:
                password = getpass.getpass("Пароль двухэтапной аутентификации Telegram: ")
                await client.sign_in(password=password)
                break

        me = await client.get_me()
        print(f"Authorization successful. Telegram user_id={me.id}")
    finally:
        if QR_IMAGE.exists():
            QR_IMAGE.unlink()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
