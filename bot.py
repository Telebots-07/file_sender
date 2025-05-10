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
last_message_time: float = 0  # Global rate limiter for message sending

# Constants
SEARCH_LIMIT = 50
REQUEST_LIMIT = 10  # Requests per hour
VERIFICATION_DURATION = 3600  # 1 hour
PAGE_SIZE = 10  # Results per page
DELETE_DELAY = 600  # 10 minutes in seconds
ADMIN_PASSWORD = "12122"
MESSAGE_RATE_LIMIT = 1  # Minimum seconds between messages to avoid flood

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

# Helper: Global rate limiter for message sending
async def rate_limit_message():
    global last_message_time
    now = time.time()
    time_since_last = now - last_message_time
    if time_since_last < MESSAGE_RATE_LIMIT:
        await asyncio.sleep(MESSAGE_RATE_LIMIT - time_since_last)
    last_message_time = time.time()

# Helper: Check subscription status
async def check_subscription(client: Client, user_id: int) -> bool:
    for channel_id in force_sub_channels:
        try:
            member = await client.get_chat_member(channel_id, user_id)
            if member.status not in ("member", "administrator", "creator"):
                return False
        except (errors.UserNotParticipant, errors.PeerIdInvalid):
            return False
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in subscription check: {e.x} seconds")
            await asyncio.sleep(e.x)
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
    except errors.FloodWait as e:
        logger.warning(f"Flood wait in delete messages: {e.x} seconds")
        await asyncio.sleep(e.x)
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
        await rate_limit_message()
        try:
            await message.reply("Please join the required channels to use this bot:", reply_markup=InlineKeyboardMarkup(buttons))
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in start command: {e.x} seconds")
            await asyncio.sleep(e.x)
            await message.reply("Please join the required channels to use this bot:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # Welcome message for new users
    if not user_requests.get(user_id):
        await rate_limit_message()
        try:
            await client.send_message(user_id, "Welcome! Search for files by typing a keyword.\nClick below for download instructions:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("How to Download", url="https://t.me/c/2323164776/7")]]))
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in welcome message: {e.x} seconds")
            await asyncio.sleep(e.x)
            await client.send_message(user_id, "Welcome! Search for files by typing a keyword.\nClick below for download instructions:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("How to Download", url="https://t.me/c/2323164776/7")]]))

    # Admin menu (styled with a boxed appearance)
    if user_id == ADMIN_ID:
        await rate_limit_message()
        try:
            await message.reply(
                "👨‍💼 Admin Menu\n"
                "━━━━━━━━━━━━━━\n"
                "   Admin Options   \n"
                "━━━━━━━━━━━━━━",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Add DB Channel", callback_data="add_db")],
                    [InlineKeyboardButton("Add Subscription Channel", callback_data="add_sub")],
                    [InlineKeyboardButton("View Statistics", callback_data="stats")],
                    [InlineKeyboardButton("Broadcast Message", callback_data="broadcast")],
                    [InlineKeyboardButton("Set Cover Photo", callback_data="set_cover")],
                    [InlineKeyboardButton("Remove Channel", callback_data="remove_channel")]
                ])
            )
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in admin menu: {e.x} seconds")
            await asyncio.sleep(e.x)
            await message.reply(
                "👨‍💼 Admin Menu\n"
                "━━━━━━━━━━━━━━\n"
                "   Admin Options   \n"
                "━━━━━━━━━━━━━━",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Add DB Channel", callback_data="add_db")],
                    [InlineKeyboardButton("Add Subscription Channel", callback_data="add_sub")],
                    [InlineKeyboardButton("View Statistics", callback_data="stats")],
                    [InlineKeyboardButton("Broadcast Message", callback_data="broadcast")],
                    [InlineKeyboardButton("Set Cover Photo", callback_data="set_cover")],
                    [InlineKeyboardButton("Remove Channel", callback_data="remove_channel")]
                ])
            )
    else:
        await rate_limit_message()
        try:
            await message.reply("Hi! Send me a keyword to search for files.")
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in user start: {e.x} seconds")
            await asyncio.sleep(e.x)
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
            await rate_limit_message()
            if action == "add_db":
                await message.reply("Forward a message from the DB channel you want to add (bot must be admin).")
            elif action == "add_sub":
                await message.reply("Forward a message from the subscription channel you want to add (bot must be admin).")
            elif action == "set_cover":
                await message.reply("Send the file name (exact) followed by the image to set as cover.")
            elif action == "stats":
                stats = (
                    f"📊 Bot Statistics\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"Users: {len(user_requests)}\n"
                    f"DB Channels: {len(db_channels)}\n"
                    f"Sub Channels: {len(force_sub_channels)}\n"
                    f"Covers Set: {len(cover_photos)}\n"
                    f"━━━━━━━━━━━━━━"
                )
                await message.reply(stats)
            elif action == "remove_channel":
                buttons = [
                    [InlineKeyboardButton(f"DB: {ch}", callback_data=f"rm_db_{ch}") for ch in db_channels],
                    [InlineKeyboardButton(f"Sub: {ch}", callback_data=f"rm_sub_{ch}") for ch in force_sub_channels]
                ]
                await message.reply("Select channel to remove:", reply_markup=InlineKeyboardMarkup(buttons))
            # Handle forwarded channel actions
            elif action.startswith("add_db_forward_") or action.startswith("add_sub_forward_"):
                channel_type, _, channel_id = action.split("_")[1:4]
                channel_id = int(channel_id)
                try:
                    bot_member = await client.get_chat_member(channel_id, "me")
                    if bot_member.status not in ("administrator", "creator"):
                        await message.reply("❌ Bot must be an admin in the channel.")
                        return
                except errors.FloodWait as e:
                    logger.warning(f"Flood wait in admin check: {e.x} seconds")
                    await asyncio.sleep(e.x)
                    await message.reply("❌ Error checking bot admin status.")
                    return
                except Exception as e:
                    logger.error(f"Error checking bot admin status: {e}")
                    await message.reply("❌ Error checking bot admin status.")
                    return

                if channel_type == "db":
                    db_channels.add(channel_id)
                    await message.reply(f"✅ DB channel {channel_id} added.")
                else:  # sub
                    force_sub_channels.add(channel_id)
                    await message.reply(f"✅ Subscription channel {channel_id} added.")
            elif action.startswith("rm_db_"):
                channel_id = int(action.split("_")[2])
                db_channels.discard(channel_id)
                await message.reply(f"✅ DB channel {channel_id} removed.")
            elif action.startswith("rm_sub_"):
                channel_id = int(action.split("_")[2])
                force_sub_channels.discard(channel_id)
                await message.reply(f"✅ Subscription channel {channel_id} removed.")
        else:
            await rate_limit_message()
            await message.reply("❌ Incorrect password. Try again.")
            admin_pending_action.pop(user_id, None)
        return

    # Check subscription
    if force_sub_channels and not await check_subscription(client, user_id):
        buttons = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/c/{str(ch)[4:]}")] for ch in force_sub_channels]
        buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_sub")])
        await rate_limit_message()
        try:
            await message.reply("Please join the required channels to use this bot:", reply_markup=InlineKeyboardMarkup(buttons))
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in subscription prompt: {e.x} seconds")
            await asyncio.sleep(e.x)
            await message.reply("Please join the required channels to use this bot:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # Input validation
    if len(query) < 3:
        await rate_limit_message()
        try:
            await message.reply("Please enter a search term with at least 3 characters.")
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in input validation: {e.x} seconds")
            await asyncio.sleep(e.x)
            await message.reply("Please enter a search term with at least 3 characters.")
        return

    # Rate limiting
    now = time.time()
    user_requests[user_id] = [t for t in user_requests[user_id] if now - t < 3600]
    if len(user_requests[user_id]) >= REQUEST_LIMIT:
        await rate_limit_message()
        try:
            await message.reply(f"⏱️ You've reached the {REQUEST_LIMIT} searches/hour limit. Try again later.")
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in rate limit message: {e.x} seconds")
            await asyncio.sleep(e.x)
            await message.reply(f"⏱️ You've reached the {REQUEST_LIMIT} searches/hour limit. Try again later.")
        return

    user_requests[user_id].append(now)
    await rate_limit_message()
    try:
        await message.reply("🔍 Searching in database channels...")
    except errors.FloodWait as e:
        logger.warning(f"Flood wait in search start: {e.x} seconds")
        await asyncio.sleep(e.x)
        await message.reply("🔍 Searching in database channels...")

    # Search channels concurrently
    results = []
    async def search_channel(channel_id: int):
        try:
            bot_member = await client.get_chat_member(channel_id, "me")
            if bot_member.status not in ("administrator", "creator", "member"):
                logger.warning(f"Bot lacks access to channel {channel_id}")
                return

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
            db_channels.discard(channel_id)
        except Exception as e:
            logger.error(f"Search error in channel {channel_id}: {e}")

    tasks = [search_channel(channel_id) for channel_id in db_channels]
    await asyncio.gather(*tasks)

    if not results:
        await rate_limit_message()
        try:
            await message.reply("No files found in the database channels.")
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in no files found: {e.x} seconds")
            await asyncio.sleep(e.x)
            await message.reply("No files found in the database channels.")
        return

    # Paginate results
    pages = [results[i:i + PAGE_SIZE] for i in range(0, len(results), PAGE_SIZE)]
    for page_num, page in enumerate(pages, 1):
        buttons = []
        for file in page:
            dyn_id = generate_dynamic_id()
            button_text = f"📁 {file['file_name']} ({file['file_size']}MB)"
            if file['file_name'] in cover_photos:
                button_text += " 🖼️"
            buttons.append([InlineKeyboardButton(button_text, callback_data=f"get_{file['channel_id']}_{file['msg_id']}_{dyn_id}")])
        buttons.append([InlineKeyboardButton("How to Download", url="https://t.me/c/2323164776/7")])
        if len(pages) > 1:
            nav_buttons = []
            if page_num > 1:
                nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"page_{page_num-1}"))
            if page_num < len(pages):
                nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page_num+1}"))
            if nav_buttons:
                buttons.append(nav_buttons)
        await rate_limit_message()
        try:
            response = await message.reply(f"📂 Select a file (Page {page_num}/{len(pages)}):", reply_markup=InlineKeyboardMarkup(buttons))
            message_pairs[user_id] = (message.id, response.id)
            asyncio.create_task(delete_messages_later(client, user_id, message.id, response.id))
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in search results: {e.x} seconds")
            await asyncio.sleep(e.x)
            response = await message.reply(f"📂 Select a file (Page {page_num}/{len(pages)}):", reply_markup=InlineKeyboardMarkup(buttons))
            message_pairs[user_id] = (message.id, response.id)
            asyncio.create_task(delete_messages_later(client, user_id, message.id, response.id))

# Callback query handler
@app.on_callback_query()
async def handle_callbacks(client: Client, callback_query):
    data = callback_query.data
    user_id = callback_query.from_user.id

    if data == "check_sub":
        if await check_subscription(client, user_id):
            verified_users[user_id] = time.time()
            await rate_limit_message()
            try:
                await callback_query.message.edit("✅ Subscription verified! You can now search for files.")
            except errors.FloodWait as e:
                logger.warning(f"Flood wait in subscription verification: {e.x} seconds")
                await asyncio.sleep(e.x)
                await callback_query.message.edit("✅ Subscription verified! You can now search for files.")
        else:
            await callback_query.answer("Please join all required channels.", show_alert=True)

    elif data.startswith("get_"):
        _, channel_id, msg_id, _ = data.split("_", 3)

        if force_sub_channels and not await check_subscription(client, user_id):
            buttons = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/c/{str(ch)[4:]}")] for ch in force_sub_channels]
            buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_sub")])
            await rate_limit_message()
            try:
                await callback_query.message.reply("Please join the required channels:", reply_markup=InlineKeyboardMarkup(buttons))
            except errors.FloodWait as e:
                logger.warning(f"Flood wait in subscription prompt: {e.x} seconds")
                await asyncio.sleep(e.x)
                await callback_query.message.reply("Please join the required channels:", reply_markup=InlineKeyboardMarkup(buttons))
            return

        verified_time = verified_users.get(user_id, 0)
        now = time.time()
        use_shortener = now - verified_time > VERIFICATION_DURATION

        file_link = f"https://t.me/c/{str(channel_id)[4:]}/{msg_id}"
        if use_shortener:
            file_link = await shorten_link(file_link)
            await rate_limit_message()
            try:
                await callback_query.message.reply(
                    "ℹ️ type movie name: hello and get ur files like this ok\n🔗 Link generated with shortening:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Download", url=file_link)],
                        [InlineKeyboardButton("How to Download", url="https://t.me/c/2323164776/7")]
                    ]))
            except errors.FloodWait as e:
                logger.warning(f"Flood wait in shortened link: {e.x} seconds")
                await asyncio.sleep(e.x)
                await callback_query.message.reply(
                    "ℹ️ type movie name: hello and get ur files like this ok\n🔗 Link generated with shortening:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Download", url=file_link)],
                        [InlineKeyboardButton("How to Download", url="https://t.me/c/2323164776/7")]
                    ]))
        else:
            await rate_limit_message()
            try:
                await callback_query.message.reply(
                    "ℹ️ type movie name: hello and get ur files like this ok\n📥 Direct download link:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Download", url=file_link)],
                        [InlineKeyboardButton("How to Download", url="https://t.me/c/2323164776/7")]
                    ]))
            except errors.FloodWait as e:
                logger.warning(f"Flood wait in direct link: {e.x} seconds")
                await asyncio.sleep(e.x)
                await callback_query.message.reply(
                    "ℹ️ type movie name: hello and get ur files like this ok\n📥 Direct download link:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Download", url=file_link)],
                        [InlineKeyboardButton("How to Download", url="https://t.me/c/2323164776/7")]
                    ]))

    # Admin actions with password prompt
    elif data in ["add_db", "add_sub", "set_cover", "stats", "remove_channel"] and user_id == ADMIN_ID:
        admin_pending_action[user_id] = data
        await rate_limit_message()
        try:
            await callback_query.message.reply("🔒 Please enter the admin password to proceed:")
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in password prompt: {e.x} seconds")
            await asyncio.sleep(e.x)
            await callback_query.message.reply("🔒 Please enter the admin password to proceed:")

    elif data.startswith("rm_db_") and user_id == ADMIN_ID:
        admin_pending_action[user_id] = data
        await rate_limit_message()
        try:
            await callback_query.message.reply("🔒 Please enter the admin password to proceed:")
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in password prompt: {e.x} seconds")
            await asyncio.sleep(e.x)
            await callback_query.message.reply("🔒 Please enter the admin password to proceed:")

    elif data.startswith("rm_sub_") and user_id == ADMIN_ID:
        admin_pending_action[user_id] = data
        await rate_limit_message()
        try:
            await callback_query.message.reply("🔒 Please enter the admin password to proceed:")
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in password prompt: {e.x} seconds")
            await asyncio.sleep(e.x)
            await callback_query.message.reply("🔒 Please enter the admin password to proceed:")

    elif data.startswith("add_db_forward_") and user_id == ADMIN_ID:
        admin_pending_action[user_id] = data
        await rate_limit_message()
        try:
            await callback_query.message.reply("🔒 Please enter the admin password to proceed:")
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in password prompt: {e.x} seconds")
            await asyncio.sleep(e.x)
            await callback_query.message.reply("🔒 Please enter the admin password to proceed:")

    elif data.startswith("add_sub_forward_") and user_id == ADMIN_ID:
        admin_pending_action[user_id] = data
        await rate_limit_message()
        try:
            await callback_query.message.reply("🔒 Please enter the admin password to proceed:")
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in password prompt: {e.x} seconds")
            await asyncio.sleep(e.x)
            await callback_query.message.reply("🔒 Please enter the admin password to proceed:")

# Handle forwarded message from admin (strictly for admin)
@app.on_message(filters.private & filters.forwarded)
async def add_channel(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        await rate_limit_message()
        try:
            await message.reply("🚫 This action is restricted to admins only.")
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in admin restriction: {e.x} seconds")
            await asyncio.sleep(e.x)
            await message.reply("🚫 This action is restricted to admins only.")
        return

    chat = message.forward_from_chat
    if not chat:
        await rate_limit_message()
        try:
            await message.reply("❌ Invalid forwarded message.")
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in invalid forward: {e.x} seconds")
            await asyncio.sleep(e.x)
            await message.reply("❌ Invalid forwarded message.")
        return

    try:
        bot_member = await client.get_chat_member(chat.id, "me")
        if bot_member.status not in ("administrator", "creator"):
            await rate_limit_message()
            await message.reply("❌ Bot must be an admin in the channel.")
            return
    except errors.FloodWait as e:
        logger.warning(f"Flood wait in admin check: {e.x} seconds")
        await asyncio.sleep(e.x)
        await rate_limit_message()
        await message.reply("❌ Error checking bot admin status.")
        return
    except Exception as e:
        logger.error(f"Error checking bot admin status: {e}")
        await rate_limit_message()
        await message.reply("❌ Error checking bot admin status.")
        return

    await rate_limit_message()
    try:
        await message.reply("Is this a DB channel or a subscription channel?", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("DB Channel", callback_data=f"add_db_forward_{chat.id}")],
            [InlineKeyboardButton("Subscription Channel", callback_data=f"add_sub_forward_{chat.id}")]
        ]))
    except errors.FloodWait as e:
        logger.warning(f"Flood wait in channel type prompt: {e.x} seconds")
        await asyncio.sleep(e.x)
        await message.reply("Is this a DB channel or a subscription channel?", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("DB Channel", callback_data=f"add_db_forward_{chat.id}")],
            [InlineKeyboardButton("Subscription Channel", callback_data=f"add_sub_forward_{chat.id}")]
        ]))

# Admin sets cover image (strictly for admin)
@app.on_message(filters.private & filters.photo)
async def handle_cover_photo(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        await rate_limit_message()
        try:
            await message.reply("🚫 This action is restricted to admins only.")
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in admin restriction: {e.x} seconds")
            await asyncio.sleep(e.x)
            await message.reply("🚫 This action is restricted to admins only.")
        return

    if user_id not in admin_pending_action or admin_pending_action[user_id] != "set_cover":
        return

    if message.caption:
        filename = message.caption.strip()
        cover_photos[filename] = message.photo.file_id
        await rate_limit_message()
        try:
            await message.reply(f"✅ Cover photo set for {filename}.")
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in cover photo set: {e.x} seconds")
            await asyncio.sleep(e.x)
            await message.reply(f"✅ Cover photo set for {filename}.")
        admin_pending_action.pop(user_id, None)
    else:
        await rate_limit_message()
        try:
            await message.reply("❌ Please provide the exact file name in the caption.")
        except errors.FloodWait as e:
            logger.warning(f"Flood wait in cover photo error: {e.x} seconds")
            await asyncio.sleep(e.x)
            await message.reply("❌ Please provide the exact file name in the caption.")

# Run bot
if __name__ == "__main__":
    logger.info("Starting File Request Bot")
    app.run()
