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
message_pairs: Dict[int, tuple] = {}  # user_id: (request_msg_id, response_msg_id)
admin_pending_action: Dict[int, str] = {}  # user_id: pending admin action

# Constants
SEARCH_LIMIT = 50
REQUEST_LIMIT = 10  # Requests per hour
VERIFICATION_DURATION = 3600  # 1 hour
PAGE_SIZE = 10  # Results per page
DELETE_DELAY = 600  # 10 minutes in seconds
ADMIN_PASSWORD = "12122"

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

# Helper: Delete messages after a delay
async def delete_messages_later(client: Client, user_id: int, request_msg_id: int, response_msg_id: int):
    await asyncio.sleep(DELETE_DELAY)
    try:
        await client.delete_messages(user_id, [request_msg_id, response_msg_id])
        logger.info(f"Deleted messages for user {user_id}: {request_msg_id}, {response_msg_id}")
    except Exception as e:
        logger.error(f"Error deleting messages for user {user_id}: {e}")
    finally:
        message_pairs.pop(user_id, None)

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

    # Admin menu
    if user_id == ADMIN_ID:
        await message.reply("👨‍💼 Admin Menu", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Add DB Channel", callback_data="add_db")],
            [InlineKeyboardButton("Add Subscription Channel", callback_data="add_sub")],
            [InlineKeyboardButton("View Statistics", callback_data="stats")],
            [InlineKeyboardButton("Broadcast Message", callback_data="broadcast")],
            [InlineKeyboardButton("Set Cover Photo", callback_data="set_cover")],
            [InlineKeyboardButton("Remove Channel", callback_data="remove_channel")]
        ]))
    else:
        await message.reply("Hi! Send me a keyword to search for files.")

# Handle text queries
@app.on_message(filters.private & filters.text & ~filters.command(["start"]))
async def handle_query(client: Client, message: Message):
    user_id = message.from_user.id
    query = message.text.strip()

    # Check if the message is a password response for admin
    if user_id == ADMIN_ID and user_id in admin_pending_action:
        if query == ADMIN_PASSWORD:
            action = admin_pending_action.pop(user_id)
            if action == "add_db":
                await message.reply("Forward a message from the DB channel you want to add (bot must be admin).")
            elif action == "add_sub":
                await message.reply("Forward a message from the subscription channel you want to add (bot must be admin).")
            elif action == "set_cover":
                await message.reply("Send the file name (exact) followed by the image to set as cover.")
            elif action == "stats":
                stats = (
                    f"📊 Bot Statistics:\n"
                    f"Users: {len(user_requests)}\n"
                    f"DB Channels: {len(db_channels)}\n"
                    f"Sub Channels: {len(force_sub_channels)}\n"
                    f"Covers Set: {len(cover_photos)}"
                )
                await message.reply(stats)
            elif action == "remove_channel":
                buttons = [
                    [InlineKeyboardButton(f"DB: {ch}", callback_data=f"rm_db_{ch}") for ch in db_channels],
                    [InlineKeyboardButton(f"Sub: {ch}", callback_data=f"rm_sub_{ch}") for ch in force_sub_channels]
                ]
                await message.reply("Select channel to remove:", reply_markup=InlineKeyboardMarkup(buttons))
            # Add more admin actions as needed
        else:
            await message.reply("❌ Incorrect password. Try again.")
            admin_pending_action.pop(user_id, None)
        return

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
            # Ensure the bot has access to the channel
            bot_member = await client.get_chat_member(channel_id, "me")
            if bot_member.status not in ("administrator", "creator", "member"):
                logger.warning(f"Bot lacks access to channel {channel_id}")
                return

            # Use search_messages with proper filters
            async for msg in client.search_messages(
                chat_id=channel_id,
                query=query,
                filter=filters.document,
                limit=SEARCH_LIMIT
            ):
                if msg.document:
                    results.append({
                        "file_name": msg.document.file_name,
                        "file_size": round(msg.document.file_size / (1024 * 1024), 2),
                        "file_id": msg.document.file_id,
                        "msg_id": msg.id,
                        "channel_id": channel_id
                    })
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in channel {channel_id}: {e.x} seconds")
            await asyncio.sleep(e.x)
        except errors.ChannelPrivate:
            logger.error(f"Channel {channel_id} is private or bot lacks access")
            db_channels.discard(channel_id)  # Remove inaccessible channel
        except Exception as e:
            logger.error(f"Search error in channel {channel_id}: {e}")

    tasks = [search_channel(channel_id) for channel_id in db_channels]
    await asyncio.gather(*tasks)

    if not results:
        await message.reply("No files found in the database channels.")
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
        buttons.append([InlineKeyboardButton("How to Download", url="https://t.me/c/2323164776/7")])
        response = await message.reply("📂 Select a file:", reply_markup=InlineKeyboardMarkup(buttons))
        # Schedule deletion of request and response
        message_pairs[user_id] = (message.id, response.id)
        asyncio.create_task(delete_messages_later(client, user_id, message.id, response.id))

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
            await callback_query.message.reply(
                "ℹ️ type movie name: hello and get ur files like this ok\n🔗 Link generated with shortening:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Download", url=file_link)],
                    [InlineKeyboardButton("How to Download", url="https://t.me/c/2323164776/7")]
                ]))
        else:
            await callback_query.message.reply(
                "ℹ️ type movie name: hello and get ur files like this ok\n📥 Direct download link:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Download", url=file_link)],
                    [InlineKeyboardButton("How to Download", url="https://t.me/c/2323164776/7")]
                ]))

    # Admin actions with password prompt
    elif data in ["add_db", "add_sub", "set_cover", "stats", "remove_channel"] and user_id == ADMIN_ID:
        admin_pending_action[user_id] = data
        await callback_query.message.reply("🔒 Please enter the admin password to proceed:")

    elif data.startswith("rm_db_") and user_id == ADMIN_ID:
        admin_pending_action[user_id] = data
        await callback_query.message.reply("🔒 Please enter the admin password to proceed:")

    elif data.startswith("rm_sub_") and user_id == ADMIN_ID:
        admin_pending_action[user_id] = data
        await callback_query.message.reply("🔒 Please enter the admin password to proceed:")

    elif data.startswith("add_db_forward_") and user_id == ADMIN_ID:
        admin_pending_action[user_id] = data
        await callback_query.message.reply("🔒 Please enter the admin password to proceed:")

    elif data.startswith("add_sub_forward_") and user_id == ADMIN_ID:
        admin_pending_action[user_id] = data
        await callback_query.message.reply("🔒 Please enter the admin password to proceed:")

# Handle forwarded message from admin
@app.on_message(filters.private & filters.forwarded & filters.user(ADMIN_ID))
async def add_channel(client: Client, message: Message):
    chat = message.forward_from_chat
    if not chat:
        await message.reply("❌ Invalid forwarded message.")
        return

    # Check if the bot is an admin in the channel
    try:
        bot_member = await client.get_chat_member(chat.id, "me")
        if bot_member.status not in ("administrator", "creator"):
            await message.reply("❌ Bot must be an admin in the channel.")
            return
    except Exception as e:
        logger.error(f"Error checking bot admin status: {e}")
        await message.reply("❌ Error checking bot admin status.")
        return

    # Prompt the admin to choose the channel type
    await message.reply("Is this a DB channel or a subscription channel?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("DB Channel", callback_data=f"add_db_forward_{chat.id}")],
        [InlineKeyboardButton("Subscription Channel", callback_data=f"add_sub_forward_{chat.id}")]
    ]))

# Handle forwarded channel selection
@app.on_callback_query(filters.regex(r"add_(db|sub)_forward_"))
async def handle_forwarded_channel(client: Client, callback_query):
    data = callback_query.data
    user_id = callback_query.from_user.id
    if user_id != ADMIN_ID:
        return

    # This action requires password verification, handled in the main callback handler
    if user_id in admin_pending_action and admin_pending_action[user_id] == data:
        channel_type, _, channel_id = data.split("_")[1:4]
        channel_id = int(channel_id)

        # Check if the bot is an admin in the channel (redundant but safe)
        try:
            bot_member = await client.get_chat_member(channel_id, "me")
            if bot_member.status not in ("administrator", "creator"):
                await callback_query.message.reply("❌ Bot must be an admin in the channel.")
                return
        except Exception as e:
            logger.error(f"Error checking bot admin status: {e}")
            await callback_query.message.reply("❌ Error checking bot admin status.")
            return

        if channel_type == "db":
            db_channels.add(channel_id)
            await callback_query.message.edit(f"✅ DB channel {channel_id} added.")
        else:  # sub
            force_sub_channels.add(channel_id)
            await callback_query.message.edit(f"✅ Subscription channel {channel_id} added.")
        admin_pending_action.pop(user_id, None)

# Admin sets cover image
@app.on_message(filters.private & filters.photo & filters.user(ADMIN_ID))
async def handle_cover_photo(client: Client, message: Message):
    if user_id not in admin_pending_action or admin_pending_action[user_id] != "set_cover":
        return

    if message.caption:
        filename = message.caption.strip()
        cover_photos[filename] = message.photo.file_id
        await message.reply(f"✅ Cover photo set for {filename}.")
        admin_pending_action.pop(user_id, None)
    else:
        await message.reply("❌ Please provide the exact file name in the caption.")

# Run bot
if __name__ == "__main__":
    logger.info("Starting File Request Bot")
    app.run()
