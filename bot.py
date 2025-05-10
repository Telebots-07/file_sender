import asyncio
from pyrogram import Client, filters, errors
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
import time
import os
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, Set, Optional
import random
import string
import aiohttp
import logging

# Configure logging to console
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
verified_users: Dict[int, float] = defaultdict(float)  # user_id: verification timestamp
db_channels: Set[int] = set()  # Dynamic DB channels
force_sub_channels: Set[int] = set()  # Forced subscription channels
cover_photos: Dict[str, str] = {}  # File name to cover photo file_id
message_pairs: Dict[int, tuple] = {}  # chat_id: (request_msg_id, response_msg_id)
admin_pending_action: Dict[int, str] = {}  # user_id: pending admin action
admin_list: Set[int] = {ADMIN_ID}  # Set of admin IDs (starting with the main admin)
log_channel: Optional[int] = None  # Log channel ID (set by admin)
last_message_time: float = 0  # For rate limiting

# Constants
SEARCH_LIMIT = 50
VERIFICATION_DURATION = 3600  # 1 hour for GPLinks usage
PAGE_SIZE = 10  # Results per page
DELETE_DELAY = 600  # 10 minutes in seconds
ADMIN_PASSWORD = "12122"
MESSAGE_DELAY = 0.5  # Delay in seconds between messages to prevent flooding

# Helper: Generate dynamic ID for callbacks
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

# Helper: Rate limiter to prevent flooding
async def rate_limit_message():
    global last_message_time
    now = time.time()
    time_since_last = now - last_message_time
    if time_since_last < MESSAGE_DELAY:
        await asyncio.sleep(MESSAGE_DELAY - time_since_last)
    last_message_time = time.time()

# Helper: Send log message to log channel if set
async def log_to_channel(client: Client, message: str):
    if log_channel is None:
        return
    try:
        await rate_limit_message()
        await client.send_message(log_channel, f"📋 Log: {message}")
        logger.info(f"Logged to channel {log_channel}: {message}")
    except Exception as e:
        logger.error(f"Failed to send log to channel {log_channel}: {e}")

# Helper: Check subscription status (only for private chats)
async def check_subscription(client: Client, user_id: int, chat_id: int) -> bool:
    if chat_id < 0:  # Skip subscription check in groups
        return True
    for channel_id in force_sub_channels:
        try:
            member = await client.get_chat_member(channel_id, user_id)
            if member.status not in ("member", "administrator", "creator"):
                return False
        except (errors.UserNotParticipant, errors.PeerIdInvalid):
            return False
        except Exception as e:
            await log_to_channel(client, f"Subscription check error for user {user_id}: {str(e)}")
            logger.error(f"Subscription check error: {e}")
            return False
    return True

# Helper: Delete messages after a delay
async def delete_messages_later(client: Client, chat_id: int, request_msg_id: int, response_msg_id: int):
    await asyncio.sleep(DELETE_DELAY)
    try:
        await client.delete_messages(chat_id, [request_msg_id, response_msg_id])
        await log_to_channel(client, f"Deleted messages in chat {chat_id}: {request_msg_id}, {response_msg_id}")
        logger.info(f"Deleted messages in chat {chat_id}: {request_msg_id}, {response_msg_id}")
    except Exception as e:
        await log_to_channel(client, f"Error deleting messages in chat {chat_id}: {str(e)}")
        logger.error(f"Error deleting messages in chat {chat_id}: {e}")
    finally:
        message_pairs.pop(chat_id, None)

# Start command handler
@app.on_message(filters.command("start"))
async def start(client: Client, message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    # Log the /start command
    await log_to_channel(client, f"User {user_id} used /start in chat {chat_id}")

    # Check subscription (only in private chats)
    if chat_id > 0 and force_sub_channels and not await check_subscription(client, user_id, chat_id):
        buttons = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/c/{str(ch)[4:]}")] for ch in force_sub_channels]
        buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_sub")])
        await rate_limit_message()
        await message.reply("Please join the required channels to use this bot:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # Welcome message for new users (only in private chats)
    if chat_id > 0:
        await rate_limit_message()
        await client.send_message(user_id, "Welcome! Search for files by typing a keyword.\nClick below for download instructions:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("How to Download", url="https://t.me/c/2323164776/7")]]))

    # Admin menu (text-based with "three lines" style, only in private chats)
    if chat_id > 0 and user_id in admin_list:
        await rate_limit_message()
        await message.reply(
            "👨‍💼 Admin Menu\n"
            "━━━━━━━━━━━━━━\n"
            "Available Commands:\n"
            "/add_db - Add a DB channel\n"
            "/add_sub - Add a subscription channel\n"
            "/stats - View bot statistics\n"
            "/broadcast - Broadcast a message\n"
            "/set_cover - Set a cover photo\n"
            "/remove_channel - Remove a channel\n"
            "/admin_list - View admin list\n"
            "/set_logchannel - Set a log channel\n"
            "━━━━━━━━━━━━━━\n"
            "Enter the command to proceed (password required)."
        )
    else:
        await rate_limit_message()
        await message.reply("Hi! Send me a keyword to search for files.")

# Handle admin commands
@app.on_message(filters.private & filters.command(["add_db", "add_sub", "stats", "broadcast", "set_cover", "remove_channel", "admin_list", "set_logchannel"]))
async def handle_admin_commands(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in admin_list:
        await rate_limit_message()
        await message.reply("🚫 This action is restricted to admins only.")
        await log_to_channel(client, f"User {user_id} attempted restricted admin command: {message.command[0]}")
        return

    command = message.command[0]
    await log_to_channel(client, f"Admin {user_id} used command: /{command}")

    if command == "admin_list":
        admin_text = "👥 Admin List\n━━━━━━━━━━━━━━\n" + "\n".join(f"Admin ID: {admin_id}" for admin_id in admin_list) + "\n━━━━━━━━━━━━━━"
        await rate_limit_message()
        await message.reply(admin_text)
        return

    if command == "set_logchannel":
        admin_pending_action[user_id] = "set_logchannel"
        await rate_limit_message()
        await message.reply("Forward a message from the channel you want to set as the log channel (bot must be admin).")
        return

    admin_pending_action[user_id] = command
    await rate_limit_message()
    await message.reply("🔒 Please enter the admin password to proceed:")

# Handle text queries (works in both private and group chats)
@app.on_message(filters.text & ~filters.command(["start", "add_db", "add_sub", "stats", "broadcast", "set_cover", "remove_channel", "admin_list", "set_logchannel"]))
async def handle_query(client: Client, message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    query = message.text.strip()

    # Log the search query
    await log_to_channel(client, f"User {user_id} searched for: '{query}' in chat {chat_id}")

    # Check if the message is a password response for admin (only in private chats)
    if chat_id > 0 and user_id in admin_list and user_id in admin_pending_action:
        if query == ADMIN_PASSWORD:
            action = admin_pending_action.pop(user_id)
            if action == "add_db":
                await rate_limit_message()
                await message.reply("Forward a message from the DB channel you want to add (bot must be admin).")
            elif action == "add_sub":
                await rate_limit_message()
                await message.reply("Forward a message from the subscription channel you want to add (bot must be admin).")
            elif action == "set_cover":
                await rate_limit_message()
                await message.reply("Send the file name (exact) followed by the image to set as cover.")
            elif action == "stats":
                stats = (
                    f"📊 Bot Statistics\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"Users: {len(verified_users)}\n"
                    f"DB Channels: {len(db_channels)}\n"
                    f"Sub Channels: {len(force_sub_channels)}\n"
                    f"Covers Set: {len(cover_photos)}\n"
                    f"━━━━━━━━━━━━━━"
                )
                await rate_limit_message()
                await message.reply(stats)
            elif action == "remove_channel":
                buttons = [
                    [InlineKeyboardButton(f"DB: {ch}", callback_data=f"rm_db_{ch}") for ch in db_channels],
                    [InlineKeyboardButton(f"Sub: {ch}", callback_data=f"rm_sub_{ch}") for ch in force_sub_channels]
                ]
                await rate_limit_message()
                await message.reply("Select channel to remove:", reply_markup=InlineKeyboardMarkup(buttons))
            elif action == "broadcast":
                await rate_limit_message()
                await message.reply("Please send the message you want to broadcast to all groups.")
            # Handle forwarded channel actions
            elif action.startswith("add_db_forward_") or action.startswith("add_sub_forward_"):
                channel_type, _, channel_id = action.split("_")[1:4]
                channel_id = int(channel_id)
                try:
                    bot_member = await client.get_chat_member(channel_id, "me")
                    if bot_member.status not in ("administrator", "creator"):
                        await rate_limit_message()
                        await message.reply("❌ Bot must be an admin in the channel.")
                        return
                except Exception as e:
                    await log_to_channel(client, f"Error checking bot admin status for channel {channel_id}: {str(e)}")
                    logger.error(f"Error checking bot admin status: {e}")
                    await rate_limit_message()
                    await message.reply("❌ Error checking bot admin status.")
                    return

                if channel_type == "db":
                    db_channels.add(channel_id)
                    await rate_limit_message()
                    await message.reply(f"✅ DB channel {channel_id} added.")
                    await log_to_channel(client, f"Admin {user_id} added DB channel {channel_id}")
                else:  # sub
                    force_sub_channels.add(channel_id)
                    await rate_limit_message()
                    await message.reply(f"✅ Subscription channel {channel_id} added.")
                    await log_to_channel(client, f"Admin {user_id} added subscription channel {channel_id}")
            elif action.startswith("rm_db_"):
                channel_id = int(action.split("_")[2])
                db_channels.discard(channel_id)
                await rate_limit_message()
                await message.reply(f"✅ DB channel {channel_id} removed.")
                await log_to_channel(client, f"Admin {user_id} removed DB channel {channel_id}")
            elif action.startswith("rm_sub_"):
                channel_id = int(action.split("_")[2])
                force_sub_channels.discard(channel_id)
                await rate_limit_message()
                await message.reply(f"✅ Subscription channel {channel_id} removed.")
                await log_to_channel(client, f"Admin {user_id} removed subscription channel {channel_id}")
        else:
            await rate_limit_message()
            await message.reply("❌ Incorrect password. Try again.")
            admin_pending_action.pop(user_id, None)
        return

    # Check subscription (only in private chats)
    if chat_id > 0 and force_sub_channels and not await check_subscription(client, user_id, chat_id):
        buttons = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/c/{str(ch)[4:]}")] for ch in force_sub_channels]
        buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_sub")])
        await rate_limit_message()
        await message.reply("Please join the required channels to use this bot:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # Input validation
    if len(query) < 3:
        await rate_limit_message()
        await message.reply("Please enter a search term with at least 3 characters.")
        return

    # Check if bot is admin in the group
    if chat_id < 0:
        try:
            bot_member = await client.get_chat_member(chat_id, "me")
            if bot_member.status not in ("administrator", "creator"):
                await rate_limit_message()
                await message.reply("❌ I need to be an admin in this group to perform searches.")
                return
        except Exception as e:
            await log_to_channel(client, f"Error checking bot admin status in group {chat_id}: {str(e)}")
            logger.error(f"Error checking bot admin status in group {chat_id}: {e}")
            await rate_limit_message()
            await message.reply("❌ Error checking my admin status in this group.")
            return

    await rate_limit_message()
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
        except errors.ChannelPrivate:
            logger.error(f"Channel {channel_id} is private or bot lacks access")
            db_channels.discard(channel_id)
        except Exception as e:
            await log_to_channel(client, f"Search error in channel {channel_id}: {str(e)}")
            logger.error(f"Search error in channel {channel_id}: {e}")

    tasks = [search_channel(channel_id) for channel_id in db_channels]
    await asyncio.gather(*tasks)

    if not results:
        await rate_limit_message()
        await message.reply("No files found in the database channels.")
        return

    # Provide feedback on search results
    await rate_limit_message()
    await message.reply(f"✅ Found {len(results)} file(s) matching your query.")

    # Paginate results with improved formatting
    pages = [results[i:i + PAGE_SIZE] for i in range(0, len(results), PAGE_SIZE)]
    for page_num, page in enumerate(pages, 1):
        buttons = []
        for idx, file in enumerate(page, start=(page_num-1)*PAGE_SIZE + 1):
            dyn_id = generate_dynamic_id()
            button_text = f"{idx}. 📁 {file['file_name']} ({file['file_size']}MB)"
            if file['file_name'] in cover_photos:
                button_text += " 🖼️"
            buttons.append([InlineKeyboardButton(button_text, callback_data=f"get_{file['channel_id']}_{file['msg_id']}_{dyn_id}")])
        buttons.append([InlineKeyboardButton("📖 How to Download", url="https://t.me/c/2323164776/7")])
        if len(pages) > 1:
            nav_buttons = []
            if page_num > 1:
                nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"page_{page_num-1}"))
            if page_num < len(pages):
                nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page_num+1}"))
            if nav_buttons:
                buttons.append(nav_buttons)
        await rate_limit_message()
        response = await message.reply(f"📂 Search Results (Page {page_num}/{len(pages)}):", reply_markup=InlineKeyboardMarkup(buttons))
        message_pairs[chat_id] = (message.id, response.id)
        asyncio.create_task(delete_messages_later(client, chat_id, message.id, response.id))

# Callback query handler
@app.on_callback_query()
async def handle_callbacks(client: Client, callback_query):
    data = callback_query.data
    user_id = callback_query.from_user.id
    chat_id = callback_query.message.chat.id

    if data == "check_sub":
        if await check_subscription(client, user_id, chat_id):
            verified_users[user_id] = time.time()
            await rate_limit_message()
            await callback_query.message.edit("✅ Subscription verified! You can now search for files.")
            await log_to_channel(client, f"User {user_id} verified subscription in chat {chat_id}")
        else:
            await callback_query.answer("Please join all required channels.", show_alert=True)

    elif data.startswith("get_"):
        _, channel_id, msg_id, _ = data.split("_", 3)

        if chat_id > 0 and force_sub_channels and not await check_subscription(client, user_id, chat_id):
            buttons = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/c/{str(ch)[4:]}")] for ch in force_sub_channels]
            buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_sub")])
            await rate_limit_message()
            await callback_query.message.reply("Please join the required channels:", reply_markup=InlineKeyboardMarkup(buttons))
            return

        verified_time = verified_users.get(user_id, 0)
        now = time.time()
        use_shortener = now - verified_time > VERIFICATION_DURATION

        file_link = f"https://t.me/c/{str(channel_id)[4:]}/{msg_id}"
        if use_shortener:
            file_link = await shorten_link(file_link)
            await rate_limit_message()
            await callback_query.message.reply(
                "ℹ️ Type movie name: hello and get your files like this\n🔗 Link generated with shortening:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬇️ Download", url=file_link)],
                    [InlineKeyboardButton("📖 How to Download", url="https://t.me/c/2323164776/7")]
                ]))
            await log_to_channel(client, f"User {user_id} requested shortened download link for message {msg_id} in channel {channel_id}")
        else:
            await rate_limit_message()
            await callback_query.message.reply(
                "ℹ️ Type movie name: hello and get your files like this\n📥 Direct download link:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬇️ Download", url=file_link)],
                    [InlineKeyboardButton("📖 How to Download", url="https://t.me/c/2323164776/7")]
                ]))
            await log_to_channel(client, f"User {user_id} requested direct download link for message {msg_id} in channel {channel_id}")

    # Admin actions with password prompt
    elif data in ["add_db", "add_sub", "set_cover", "stats", "remove_channel"] and user_id in admin_list:
        admin_pending_action[user_id] = data
        await rate_limit_message()
        await callback_query.message.reply("🔒 Please enter the admin password to proceed:")
        await log_to_channel(client, f"Admin {user_id} initiated action: {data}")

    elif data.startswith("rm_db_") and user_id in admin_list:
        admin_pending_action[user_id] = data
        await rate_limit_message()
        await callback_query.message.reply("🔒 Please enter the admin password to proceed:")
        await log_to_channel(client, f"Admin {user_id} initiated remove DB channel action: {data}")

    elif data.startswith("rm_sub_") and user_id in admin_list:
        admin_pending_action[user_id] = data
        await rate_limit_message()
        await callback_query.message.reply("🔒 Please enter the admin password to proceed:")
        await log_to_channel(client, f"Admin {user_id} initiated remove subscription channel action: {data}")

    elif data.startswith("add_db_forward_") and user_id in admin_list:
        admin_pending_action[user_id] = data
        await rate_limit_message()
        await callback_query.message.reply("🔒 Please enter the admin password to proceed:")
        await log_to_channel(client, f"Admin {user_id} initiated add DB channel action: {data}")

    elif data.startswith("add_sub_forward_") and user_id in admin_list:
        admin_pending_action[user_id] = data
        await rate_limit_message()
        await callback_query.message.reply("🔒 Please enter the admin password to proceed:")
        await log_to_channel(client, f"Admin {user_id} initiated add subscription channel action: {data}")

# Handle forwarded message from admin (strictly for admin)
@app.on_message(filters.private & filters.forwarded)
async def add_channel(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in admin_list:
        await rate_limit_message()
        await message.reply("🚫 This action is restricted to admins only.")
        await log_to_channel(client, f"User {user_id} attempted to forward a message for admin action")
        return

    chat = message.forward_from_chat
    if not chat:
        await rate_limit_message()
        await message.reply("❌ Invalid forwarded message.")
        return

    try:
        bot_member = await client.get_chat_member(chat.id, "me")
        if bot_member.status not in ("administrator", "creator"):
            await rate_limit_message()
            await message.reply("❌ Bot must be an admin in the channel.")
            return
    except Exception as e:
        await log_to_channel(client, f"Error checking bot admin status for channel {chat.id}: {str(e)}")
        logger.error(f"Error checking bot admin status: {e}")
        await rate_limit_message()
        await message.reply("❌ Error checking bot admin status.")
        return

    if user_id in admin_pending_action and admin_pending_action[user_id] == "set_logchannel":
        global log_channel
        log_channel = chat.id
        admin_pending_action.pop(user_id, None)
        await rate_limit_message()
        await message.reply(f"✅ Log channel set to {chat.id}.")
        await log_to_channel(client, f"Admin {user_id} set log channel to {chat.id}")
        return

    await rate_limit_message()
    await message.reply("Is this a DB channel or a subscription channel?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("DB Channel", callback_data=f"add_db_forward_{chat.id}")],
        [InlineKeyboardButton("Subscription Channel", callback_data=f"add_sub_forward_{chat.id}")]
    ]))
    await log_to_channel(client, f"Admin {user_id} forwarded a message to add channel {chat.id}")

# Handle broadcast message after password verification
@app.on_message(filters.private & filters.text & filters.regex(r"^(?!/start$|add_db$|add_sub$|stats$|broadcast$|set_cover$|remove_channel$|admin_list$|set_logchannel$).+"))
async def handle_broadcast_message(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in admin_list or user_id not in admin_pending_action or admin_pending_action[user_id] != "broadcast":
        return

    broadcast_message = message.text.strip()
    admin_pending_action.pop(user_id, None)

    # Send broadcast to all DB and subscription channels
    all_channels = db_channels.union(force_sub_channels)
    for channel_id in all_channels:
        try:
            await rate_limit_message()
            await client.send_message(channel_id, f"📢 Broadcast Message:\n{broadcast_message}")
            await log_to_channel(client, f"Broadcast sent to channel {channel_id}: {broadcast_message}")
            logger.info(f"Broadcast sent to channel {channel_id}")
        except Exception as e:
            await log_to_channel(client, f"Error sending broadcast to channel {channel_id}: {str(e)}")
            logger.error(f"Error sending broadcast to channel {channel_id}: {e}")

    await rate_limit_message()
    await message.reply(f"✅ Broadcast sent to {len(all_channels)} channels.")
    await log_to_channel(client, f"Admin {user_id} broadcasted message to {len(all_channels)} channels")

# Admin sets cover image (strictly for admin)
@app.on_message(filters.private & filters.photo)
async def handle_cover_photo(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in admin_list:
        await rate_limit_message()
        await message.reply("🚫 This action is restricted to admins only.")
        await log_to_channel(client, f"User {user_id} attempted to set a cover photo")
        return

    if user_id not in admin_pending_action or admin_pending_action[user_id] != "set_cover":
        return

    if message.caption:
        filename = message.caption.strip()
        cover_photos[filename] = message.photo.file_id
        await rate_limit_message()
        await message.reply(f"✅ Cover photo set for {filename}.")
        await log_to_channel(client, f"Admin {user_id} set cover photo for file {filename}")
        admin_pending_action.pop(user_id, None)
    else:
        await rate_limit_message()
        await message.reply("❌ Please provide the exact file name in the caption.")

# Run bot
if __name__ == "__main__":
    logger.info("Starting File Request Bot")
    app.run()
