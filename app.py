from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
from urllib.parse import parse_qsl

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from telethon import events
from telethon.errors import FloodWaitError

from config import DATA_DIR, ROOT_DIR, int_env, make_telegram_client, required_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tg-video")

BOT_TOKEN = required_env("BOT_TOKEN")
OWNER_ID = int(required_env("OWNER_ID"))
TARGET_CHAT = int(required_env("TARGET_CHAT"))
PORT = int_env("PORT", 8080)
INIT_DATA_MAX_AGE = int_env("INIT_DATA_MAX_AGE_SECONDS", 3600)
if not 1 <= PORT <= 65535:
    raise RuntimeError("PORT должен быть в диапазоне 1..65535")
if INIT_DATA_MAX_AGE < 60:
    raise RuntimeError("INIT_DATA_MAX_AGE_SECONDS должен быть не меньше 60")

client = make_telegram_client()
app = FastAPI(title="tg-video", docs_url=None, redoc_url=None)

# SQLite is used from FastAPI worker threads and the Telethon event loop.
# Keep every access to this single connection under one lock.
db_lock = threading.RLock()
db = sqlite3.connect(DATA_DIR / "data.db", check_same_thread=False, timeout=30)
db.row_factory = sqlite3.Row
with db_lock:
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            topic_id INTEGER NOT NULL,
            min_seconds INTEGER NOT NULL DEFAULT 30
        );
        CREATE TABLE IF NOT EXISTS channels (
            channel_id INTEGER PRIMARY KEY,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS sent (
            chat_id INTEGER NOT NULL,
            msg_id INTEGER NOT NULL,
            PRIMARY KEY(chat_id, msg_id)
        );
        """
    )
    db.commit()


def verify_init_data(raw_init_data: str) -> None:
    if not raw_init_data:
        raise HTTPException(status_code=401, detail="missing init data")

    try:
        pairs = parse_qsl(raw_init_data, keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
        received_hash = data.pop("hash", "")
        if not received_hash:
            raise ValueError("missing hash")

        check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            raise HTTPException(status_code=401, detail="bad signature")

        auth_date = int(data.get("auth_date", "0"))
        now = int(time.time())
        if auth_date > now + 30 or now - auth_date > INIT_DATA_MAX_AGE:
            raise HTTPException(status_code=401, detail="expired init data")

        user = json.loads(data["user"])
        if int(user["id"]) != OWNER_ID:
            raise HTTPException(status_code=403, detail="not owner")
    except HTTPException:
        raise
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="invalid init data") from None


def auth(x_init_data: str = Header(default="", alias="X-Init-Data")) -> None:
    verify_init_data(x_init_data)


_cache: dict[str, object] = {"time": 0.0, "channels": []}


async def list_channels() -> list[tuple[int, str]]:
    if time.time() - float(_cache["time"]) > 300:
        channels = []
        async for dialog in client.iter_dialogs():
            if dialog.is_channel and not dialog.is_group:
                channels.append((dialog.id, dialog.name or str(dialog.id)))
        _cache["channels"] = channels
        _cache["time"] = time.time()
    return list(_cache["channels"])


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "telegram_connected": client.is_connected()}


@app.get("/api/state", dependencies=[Depends(auth)])
async def state():
    channels = await list_channels()
    with db_lock:
        categories = [dict(row) for row in db.execute("SELECT * FROM categories ORDER BY id")]
        assigned = {
            row["channel_id"]: row["category_id"]
            for row in db.execute("SELECT channel_id, category_id FROM channels")
        }
    return {
        "categories": categories,
        "channels": [
            {"id": channel_id, "title": title, "category_id": assigned.get(channel_id)}
            for channel_id, title in channels
        ],
    }


class Category(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    topic_id: int = Field(gt=0)
    min_seconds: int = Field(default=30, ge=0, le=86400)


@app.post("/api/categories", dependencies=[Depends(auth)])
def add_category(category: Category):
    with db_lock:
        cursor = db.execute(
            "INSERT INTO categories(name, topic_id, min_seconds) VALUES(?,?,?)",
            (category.name.strip(), category.topic_id, category.min_seconds),
        )
        db.commit()
        return {"ok": True, "id": cursor.lastrowid}


@app.put("/api/categories/{category_id}", dependencies=[Depends(auth)])
def edit_category(category_id: int, category: Category):
    with db_lock:
        cursor = db.execute(
            "UPDATE categories SET name=?, topic_id=?, min_seconds=? WHERE id=?",
            (category.name.strip(), category.topic_id, category.min_seconds, category_id),
        )
        db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="category not found")
    return {"ok": True}


@app.delete("/api/categories/{category_id}", dependencies=[Depends(auth)])
def delete_category(category_id: int):
    with db_lock:
        exists = db.execute("SELECT 1 FROM categories WHERE id=?", (category_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="category not found")
        # Explicit deletion also cleans up databases created by the first version,
        # whose channels table may not have had a foreign-key constraint.
        db.execute("DELETE FROM channels WHERE category_id=?", (category_id,))
        db.execute("DELETE FROM categories WHERE id=?", (category_id,))
        db.commit()
    return {"ok": True}


class Assignment(BaseModel):
    channel_id: int
    category_id: int | None = None


@app.post("/api/assign", dependencies=[Depends(auth)])
def assign_channel(assignment: Assignment):
    with db_lock:
        if assignment.category_id is None:
            db.execute("DELETE FROM channels WHERE channel_id=?", (assignment.channel_id,))
        else:
            exists = db.execute(
                "SELECT 1 FROM categories WHERE id=?", (assignment.category_id,)
            ).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail="category not found")
            db.execute(
                "INSERT INTO channels(channel_id, category_id) VALUES(?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET category_id=excluded.category_id",
                (assignment.channel_id, assignment.category_id),
            )
        db.commit()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(ROOT_DIR / "index.html")


@client.on(events.NewMessage(func=lambda event: event.is_channel and not event.is_group))
async def on_new_channel_message(event):
    message = event.message
    if not message.video or message.gif or message.video_note:
        return

    with db_lock:
        route = db.execute(
            "SELECT categories.topic_id, categories.min_seconds "
            "FROM channels JOIN categories ON categories.id=channels.category_id "
            "WHERE channels.channel_id=?",
            (event.chat_id,),
        ).fetchone()
        already_sent = db.execute(
            "SELECT 1 FROM sent WHERE chat_id=? AND msg_id=?",
            (event.chat_id, message.id),
        ).fetchone()

    if not route or already_sent:
        return
    if (message.file.duration or 0) < route["min_seconds"]:
        return

    # Mark as sent only after Telegram confirms the upload. A transient failure
    # must not permanently suppress a video from being forwarded later.
    for attempt in range(2):
        try:
            await client.send_file(
                TARGET_CHAT,
                message.media,
                caption=message.text or "",
                reply_to=route["topic_id"],
            )
            with db_lock:
                db.execute(
                    "INSERT OR IGNORE INTO sent(chat_id, msg_id) VALUES(?,?)",
                    (event.chat_id, message.id),
                )
                db.commit()
            log.info(
                "Forwarded video source=%s/%s topic=%s",
                event.chat_id,
                message.id,
                route["topic_id"],
            )
            return
        except FloodWaitError as exc:
            if attempt == 0:
                log.warning("Telegram FloodWait: sleeping %s seconds", exc.seconds)
                await asyncio.sleep(exc.seconds)
            else:
                log.error("FloodWait repeated for message %s/%s", event.chat_id, message.id)
        except Exception:
            log.exception("Failed to forward message %s/%s", event.chat_id, message.id)
            return


async def main() -> None:
    log.info("Connecting Telethon through local Flowseal MTProto proxy...")
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Telegram session is not authorized. Run 'python login_qr.py' once from the project directory."
        )

    me = await client.get_me()
    log.info("Telegram user session authorized (user_id=%s)", me.id)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="info", access_log=False)
    )
    try:
        await asyncio.gather(server.serve(), client.run_until_disconnected())
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
