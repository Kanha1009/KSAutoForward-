import os
import asyncio
from pyrogram import Client, filters
from pymongo import MongoClient
from pyrogram.errors import FloodWait

# --- Environment Variables ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

SOURCE_CHANNEL = int(os.getenv("SOURCE_CHANNEL"))
TARGET_CHANNELS = [int(x) for x in os.getenv("TARGET_CHANNELS").split(",")]

MONGO_URL = os.getenv("MONGO_URL")  # MongoDB connection string
DB_NAME = "forwarder_db"
COLLECTION_NAME = "message_map"

# --- MongoDB Setup ---
mongo = MongoClient(MONGO_URL)
db = mongo[DB_NAME]
collection = db[COLLECTION_NAME]

# --- Pyrogram Client ---
app = Client("forwarder_bot",
             api_id=API_ID,
             api_hash=API_HASH,
             bot_token=BOT_TOKEN)

# --- Save Mapping to MongoDB ---
def save_mapping(source_id, channel_id, target_id):
    collection.update_one(
        {"source_id": source_id, "channel_id": channel_id},
        {"$set": {"target_id": target_id}},
        upsert=True
    )

# --- Get Mappings ---
def get_mappings(source_id):
    return list(collection.find({"source_id": source_id}))

# --- Forward New Messages ---
@app.on_message(filters.chat(SOURCE_CHANNEL))
async def forward_new(client, message):
    text = message.text or message.caption
    if not text:
        return

    for channel in TARGET_CHANNELS:
        try:
            sent = await client.send_message(channel, text)
            save_mapping(message.id, channel, sent.id)
            print(f"✅ New msg {message.id} copied to {channel} as {sent.id}")
        except FloodWait as e:
            print(f"⏱ FloodWait {e.x}s while sending to {channel}")
            await asyncio.sleep(e.x)
            sent = await client.send_message(channel, text)
            save_mapping(message.id, channel, sent.id)
        except Exception as e:
            print(f"❌ Error sending to {channel}: {e}")

# --- Edit Messages ---
@app.on_edited_message(filters.chat(SOURCE_CHANNEL))
async def edit_messages(client, message):
    text = message.text or message.caption
    if not text:
        return

    mappings = get_mappings(message.id)
    if not mappings:
        print(f"⚠️ No mappings found for {message.id}")
        return

    for mapping in mappings:
        try:
            await client.edit_message_text(mapping["channel_id"], mapping["target_id"], text)
            print(f"✏️ Edited msg {mapping['target_id']} in {mapping['channel_id']}")
        except FloodWait as e:
            print(f"⏱ FloodWait {e.x}s while editing {mapping['target_id']}")
            await asyncio.sleep(e.x)
            await client.edit_message_text(mapping["channel_id"], mapping["target_id"], text)
        except Exception as e:
            print(f"❌ Error editing in {mapping['channel_id']}: {e}")

# --- Sync Old Messages ---
async def sync_old_messages():
    try:
        chat = await app.get_chat(SOURCE_CHANNEL)
        print(f"Bot can access source channel: {chat.title} ({chat.id})")
    except Exception as e:
        print(f"❌ Cannot access source channel: {e}")
        return

    async for message in app.get_chat_history(SOURCE_CHANNEL, limit=500):
        text = message.text or message.caption
        if not text:
            continue

        for channel in TARGET_CHANNELS:
            try:
                # Check if mapping exists
                exists = collection.find_one({"source_id": message.id, "channel_id": channel})
                if exists:
                    continue

                # Find matching message in target channel
                async for target_msg in app.get_chat_history(channel, limit=1000):
                    if (target_msg.text or target_msg.caption) == text:
                        save_mapping(message.id, channel, target_msg.id)
                        print(f"✅ Old msg mapped: {message.id} -> {channel}:{target_msg.id}")
                        break

            except FloodWait as e:
                print(f"⏱ FloodWait {e.x}s while syncing old messages")
                await asyncio.sleep(e.x)
            except Exception as e:
                print(f"❌ Error accessing target channel {channel}: {e}")

# --- Main ---
async def main():
    try:
        await app.start()
        print("🚀 Bot started")
        # Sync old messages once at startup
        await sync_old_messages()
        # Keep the bot running
        await idle()
    finally:
        await app.stop()
        print("🛑 Bot stopped")

# --- Idle import ---
from pyrogram import idle

# --- Run ---
asyncio.run(main())
