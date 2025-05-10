import asyncio
from pyrogram import Client, filters, errors
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
import time
import os
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, Set
import random
import string
import aiohttp
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Environment variables
try:
    API_ID = int(os.getenv("API_ID"))
    API_HASH = os.getenv("API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    ADMIN_ID = int(os.getenv("ADMIN_ID"))
    GPLINK_API_KEY = os.getenv("GPLINK_API_KEY")
except (ValueError, TypeError) as e:
    logger.error("Environment variables missing or invalid")
    raise SystemExit("Please set all required environment variables")

# Initialize Pyrogram client
app = Client("file-request-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Data storage
user_requests: Dict[int, list] = defaultdict(list)
verified_users: Dict[int, float] = defaultdict(float)  # user_id: verification timestamp
db_channels: Set[int] = set()  # Dynamic DB channels
force_sub_channels: Set[int] = set()  # Forced subscription channels
cover_photos: Dict[str, str] = {}  # File name to cover photo file_id

# Constants
SEARCH_LIMIT = 50
REQUEST_LIMIT = 10  # Requests per hour
VERIFICATION_DURATION = 3600  # 1 hour
PAGE_SIZE = 10  # Results per page

# Helper: Generate dynamic ID
def generate_dynamic_id(length: int = 10) -> str:
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

# Helper: Shorten link using GPLinks
async def shorten_link(long_url: str) -> str:
    async with aiohttp.ClientSession() as session:
        api_url = f"https://api.gplinks.in/api?api={GPLINK_API_KEY}&url={long_url}&format=text"
        try:
            async with session.get(api_url, timeout=5) as response:
                if response.status == 200:
                    return (await response.text()).strip()
                logger.warning(f"GPLinks API failed: {response.status}")
        except Exception as e:
            logger.error(f"Shorten link error: {e}")
    return long_url

# Helper: Check subscription status
async def check_subscription(client: Client, user_id: int) -> bool:
    for channel_id in force_sub_channels:
        try:
            member = await client.get_chat_member(channel_id, user_id)
            if member.status not in ("member", "administrator", "creator"):
                return False
        except (errors.UserNotParticipant, errors.PeerIdInvalid):
            return False
        except Exception as e:
            logger.error(f"Subscription check error: {e}")
            return False
    return True

# Start command handler
@app.on_message(filters.private & filters.command("start"))
async def start(client: Client, message: Message):
    user_id = message.from_user.id

    # Check subscription
    if force_sub_channels and not await check_subscription(client, user_id):
        buttons = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/c/{str(ch)[4:]}")] for ch in force_sub_channels]
        buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_sub")])
        await message.reply("Please join the required channels to use this bot:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # Welcome message for new users
    if not user_requests.get(user_id):
        try:
            await client.send_message(user_id, "Welcome! Search for files by typing a keyword.\nClick below for download instructions:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("How to Download", url="https://t.me/c/2323164776/7")]]))
        except errors.FloodWait as e:
            logger.warning(f"Flood wait: {e.x} seconds")
            await asyncio.sleep(e.x)

    # Admin panel
    if user_id == ADMIN_ID:
        await message.reply("Admin Panel", reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("➕ Add DB Channel", callback_data="add_db"),
                InlineKeyboardButton("➕ Add Sub Channel", callback_data="add_sub")
            ],
            [
                InlineKeyboardButton("📊 Stats", callback_data="stats"),
                InlineKeyboardButton("📢 Broadcast", callback_data="broadcast")
            ],
            [
                InlineKeyboardButton("🖼️ Set Cover", callback_data="set_cover"),
                InlineKeyboardButton("🗑️ Remove Channel", callback_data="remove_channel")
            ]
        ]))
    else:
        await message.reply("Hi! Send a keyword to search for files.")

# Handle text queries
@app.on_message(filters.private & filters.text & ~filters.command(["start"]))
async def handle_query(client: Client, message: Message):
    user_id = message.from_user.id
    query = message.text.strip()

    # Check subscription
    if force_sub_channels and not await check_subscription(client, user_id):
        buttons = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/c/{str(ch)[4:]}")] for ch in force_sub_channels]
        buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_sub")])
        await message.reply("Please join the required channels to use this bot:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # Input validation
    if len(query) < 3:
        await message.reply("Please enter a search term with at least 3 characters.")
        return

    # Rate limiting
    now = time.time()
    user_requests[user_id] = [t for t in user_requests[user_id] if now - t < 3600]
    if len(user_requests[user_id]) >= REQUEST_LIMIT:
        await message.reply(f"⏱️ You've reached the {REQUEST_LIMIT} searches/hour limit. Try again later.")
        return

    user_requests[user_id].append(now)
    await message.reply("🔍 Searching in database channels...")

    # Search channels concurrently
    results = []
    async def search_channel(channel_id: int):
        try:
            async for msg in client.search_messages(channel_id, query, filter="document", limit=SEARCH_LIMIT):
                if msg.document:
                    results.append({
                        "file_name": msg.document.file_name,
                        "file_size": round(msg.document.file_size / (1024 * 1024), 2),
                        "file_id": msg.document.file_id,
                        "msg_id": msg.id,
                        "channel_id": channel_id
                    })
        except Exception as e:
            logger.error(f"Search error in channel {channel_id}: {e}")

    tasks = [search_channel(channel_id) for channel_id in db_channels]
    await asyncio.gather(*tasks)

    if not results:
        await message.reply("No files found.")
        return

    # Paginate results
    pages = [results[i:i + PAGE_SIZE] for i in range(0, len(results), PAGE_SIZE)]
    for page in pages:
        buttons = []
        for file in page:
            dyn_id = generate_dynamic_id()
            button_text = f"📁 {file['file_name']} ({file['file_size']}MB)"
            if file['file_name'] in cover_photos:
                button_text += " 🖼️"
            buttons.append([InlineKeyboardButton(button_text, callback_data=f"get_{file['channel_id']}_{file['msg_id']}_{dyn_id}")])
        await message.reply("📂 Select a file:", reply_markup=InlineKeyboardMarkup(buttons))

# Callback query handler
@app.on_callback_query()
async def handle_callbacks(client: Client, callback_query):
    data = callback_query.data
    user_id = callback_query.from_user.id

    if data == "check_sub":
        if await check_subscription(client, user_id):
            verified_users[user_id] = time.time()  # Mark user as verified
            await callback_query.message.edit("✅ Subscription verified! You can now search for files.")
        else:
            await callback_query.answer("Please join all required channels.", show_alert=True)

    elif data.startswith("get_"):
        _, channel_id, msg_id, _ = data.split("_", 3)

        # Check subscription
        if force_sub_channels and not await check_subscription(client, user_id):
            buttons = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/c/{str(ch)[4:]}")] for ch in force_sub_channels]
            buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_sub")])
            await callback_query.message.reply("Please join the required channels:", reply_markup=InlineKeyboardMarkup(buttons))
            return

        # Link shortening logic
        verified_time = verified_users.get(user_id, 0)
        now = time.time()
        use_shortener = now - verified_time > VERIFICATION_DURATION

        file_link = f"https://t.me/c/{str(channel_id)[4:]}/{msg_id}"
        if use_shortener:
            file_link = await shorten_link(file_link)
            await callback_query.message.reply("🔗 Link generated with shortening:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Download", url=file_link)]]))
        else:
            await callback_query.message.reply("📥 Direct download link:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Download", url=file_link)]]))

    elif data == "add_db" and user_id == ADMIN_ID:
        await callback_query.message.reply("Forward a message from the DB channel you want to add.")

    elif data == "add_sub" and user_id == ADMIN_ID:
        await callback_query.message.reply("Forward a message from the subscription channel you want to add.")

    elif data == "set_cover" and user_id == ADMIN_ID:
        await callback_query.message.reply("Send the file name (exact) followed by the image to set as cover.")

    elif data == "stats" and user_id == ADMIN_ID:
        stats = (
            f"📊 Bot Statistics:\n"
            f"Users: {len(user_requests)}\n"
            f"DB Channels: {len(db_channels)}\n"
            f"Sub Channels: {len(force_sub_channels)}\n"
            f"Covers Set: {len(cover_photos)}"
        )
        await callback_query.message.reply(stats)

    elif data == "remove_channel" and user_id == ADMIN_ID:
        buttons = [
            [InlineKeyboardButton(f"DB: {ch}", callback_data=f"rm_db_{ch}") for ch in db_channels],
            [InlineKeyboardButton(f"Sub: {ch}", callback_data=f"rm_sub_{ch}") for ch in force_sub_channels]
        ]
        await message.reply("Select channel to remove:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("rm_db_") and user_id == ADMIN_ID:
        channel_id = int(data.split("_")[2])
        db_channels.discard(channel_id)
        await callback_query.message.reply(f"✅ DB channel {channel_id} removed.")

    elif data.startswith("rm_sub_") and user_id == ADMIN_ID:
        channel_id = int(data.split("_")[2])
        force_sub_channels.discard(channel_id)
        await callback_query.message.reply(f"✅ Subscription channel {channel_id} removed.")

# Accept forwarded message from admin
@app.on_message(filters.private & filters.forwarded & filters.user(ADMIN_ID))
async def add_channel(client: Client, message: Message):
    chat = message.forward_from_chat
    if not chat:
        await message.reply("❌ Invalid forwarded message.")
        return

    if message.reply_to_message:
        if "DB channel" in message.reply_to_message.text:
            db_channels.add(chat.id)
            await message.reply(f"✅ DB channel {chat.title or chat.id} added.")
        elif "subscription channel" in message.reply_to_message.text:
            force_sub_channels.add(chat.id)
            await message.reply(f"✅ Subscription channel {chat.title or chat.id} added.")
    else:
        await message.reply("❌ Please reply to the appropriate admin command.")

# Admin sets cover image
@app.on_message(filters.private & filters.photo & filters.user(ADMIN_ID))
async def handle_cover_photo(client: Client, message: Message):
    if message.caption:
        filename = message.caption.strip()
        cover_photos[filename] = message.photo.file_id
        await message.reply(f"✅ Cover photo set for {filename}.")
    else:
        await message.reply("❌ Please provide the exact file name in the caption.")

# Run bot
if __name__ == "__main__":
    logger.info("Starting File Request Bot")
    app.run()
