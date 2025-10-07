# filename: tg_sync_bot_env.py
import os
import asyncio
from telethon import TelegramClient, events, errors
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto
from motor.motor_asyncio import AsyncIOMotorClient
import hashlib
import tempfile
import time

# ---------- CONFIG from Environment Variables ----------
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION_NAME = os.environ.get("SESSION_NAME", "sync_userbot")
MONGO_URI = os.environ.get("MONGO_URI")
DB_NAME = os.environ.get("DB_NAME", "tg_sync")
SOURCE_USERNAME = os.environ.get("SOURCE_USERNAME")  # @username OR -100ID
TARGET_USERNAME = os.environ.get("TARGET_USERNAME")  # @username OR -100ID
RATE_SLEEP = float(os.environ.get("RATE_SLEEP", 0.5))
# --------------------------------------------------------

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo[DB_NAME]
mapping_col = db["msg_map"]

# ---------- UTILS ----------
async def fingerprint_message(message):
    parts = []
    if message.message:
        parts.append(message.message)
    if message.media:
        try:
            if isinstance(message.media, MessageMediaPhoto):
                parts.append("photo:" + str(getattr(message.media.photo, 'id', 'photo')))
            elif isinstance(message.media, MessageMediaDocument):
                doc = message.media.document
                parts.append("doc:" + (doc.file_name or "") + ":" + str(doc.size or ""))
            else:
                parts.append(repr(message.media))
        except Exception:
            parts.append(repr(message.media))
    s = "|".join(parts)
    return hashlib.sha1(s.encode('utf-8')).hexdigest()

async def download_media_tmp(msg):
    if not msg.media:
        return None
    tmpdir = tempfile.gettempdir()
    try:
        path = await client.download_media(msg, file=tmpdir)
        return path
    except Exception as e:
        print("download_media error:", e)
        return None

async def send_copy_to_target(source_msg, target_entity):
    text = source_msg.message or ""
    if source_msg.media:
        media_path = await download_media_tmp(source_msg)
        if media_path:
            try:
                sent = await client.send_file(target_entity, media_path, caption=text)
                try: os.remove(media_path)
                except: pass
                return sent
            except Exception as e:
                print("send_file failed:", e)
                sent = await client.send_message(target_entity, text)
                return sent
        else:
            return await client.send_message(target_entity, text)
    else:
        return await client.send_message(target_entity, text)

# ---------- INITIAL MAPPING ----------
async def create_initial_mapping():
    print("Creating initial mapping for same-order messages...")
    src_iter = client.iter_messages(SOURCE_USERNAME, reverse=True)
    tgt_iter = client.iter_messages(TARGET_USERNAME, reverse=True)
    count = 0
    async for src_msg, tgt_msg in zip(src_iter, tgt_iter):
        if src_msg.is_service:
            continue
        existing = await mapping_col.find_one({"source_chat_id": src_msg.chat_id, "source_msg_id": src_msg.id})
        if existing:
            continue
        fp = await fingerprint_message(src_msg)
        await mapping_col.insert_one({
            "source_chat_id": src_msg.chat_id,
            "source_msg_id": src_msg.id,
            "target_chat_id": tgt_msg.chat_id,
            "target_msg_id": tgt_msg.id,
            "fingerprint": fp,
            "ts": int(time.time())
        })
        count += 1
        if count % 50 == 0:
            print(f"Mapped {count} messages...")
        await asyncio.sleep(RATE_SLEEP)
    print(f"Initial mapping done. Total messages mapped: {count}")

# ---------- EVENT HANDLERS ----------
@client.on(events.NewMessage(chats=SOURCE_USERNAME))
async def handler_newmessage(event):
    msg = event.message
    if msg.is_service:
        return
    try:
        sent = await send_copy_to_target(msg, TARGET_USERNAME)
        fp = await fingerprint_message(msg)
        await mapping_col.insert_one({
            "source_chat_id": msg.chat_id,
            "source_msg_id": msg.id,
            "target_chat_id": sent.chat_id,
            "target_msg_id": sent.id,
            "fingerprint": fp,
            "ts": int(time.time())
        })
        print(f"New message copied: {msg.id} -> {sent.id}")
    except Exception as e:
        print("Error copying new message:", e)

@client.on(events.MessageEdited(chats=SOURCE_USERNAME))
async def handler_edited(event):
    msg = event.message
    if msg.is_service:
        return
    row = await mapping_col.find_one({"source_chat_id": msg.chat_id, "source_msg_id": msg.id})
    if not row:
        print("Edited message not mapped. Sending as new.")
        try:
            sent = await send_copy_to_target(msg, TARGET_USERNAME)
            fp = await fingerprint_message(msg)
            await mapping_col.insert_one({
                "source_chat_id": msg.chat_id,
                "source_msg_id": msg.id,
                "target_chat_id": sent.chat_id,
                "target_msg_id": sent.id,
                "fingerprint": fp,
                "ts": int(time.time())
            })
        except Exception as e:
            print("Failed to copy edited-unsynced msg:", e)
        return
    new_fp = await fingerprint_message(msg)
    if new_fp == row.get("fingerprint"):
        return
    target_chat = row["target_chat_id"]
    target_msg_id = row["target_msg_id"]
    try:
        if not msg.media:
            await client.edit_message(entity=target_chat, message=target_msg_id, text=msg.message or "")
            await mapping_col.update_one({"_id": row["_id"]}, {"$set": {"fingerprint": new_fp, "ts": int(time.time())}})
            print(f"Edited target message (text): {target_msg_id}")
        else:
            try:
                await client.delete_messages(entity=target_chat, message_ids=target_msg_id)
            except: pass
            sent = await send_copy_to_target(msg, target_chat)
            await mapping_col.update_one(
                {"_id": row["_id"]},
                {"$set": {"target_chat_id": sent.chat_id, "target_msg_id": sent.id, "fingerprint": new_fp, "ts": int(time.time())}}
            )
            print(f"Replaced media message in target: new id {sent.id}")
    except errors.FloodWaitError as e:
        await asyncio.sleep(e.seconds + 1)
    except Exception as e:
        print("Error handling edited message:", e)

# ---------- MAIN ----------
async def main():
    await client.start()
    print("Client started. Getting entities...")
    await create_initial_mapping()
    print("Listening for new messages and edits...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped.")
