import asyncio
from pyrogram import Client, filters, errors
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, ChatMember
from pyrogram.enums import ChatMemberStatus, MessageMediaType
import time
import os
from datetime import datetime, timedelta
from collections import defaultdict, deque
from typing import Dict, Set, Optional, Deque, Tuple
import random
import string
import aiohttp
import logging
from asyncio import Queue

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
message_pairs: Dict[int, tuple] = {}  # chat_id: (request_msg_id, response_msg_id)
admin_pending_action: Dict[int, str] = {}  # user_id: pending admin action
admin_list: Set[int] = {ADMIN_ID}  # Set of admin IDs (starting with the main admin)
log_channel: Optional[int] = None  # Log channel ID (set by admin)
user_search_history: Dict[int, Deque[Tuple[str, float]]] = defaultdict(lambda: deque(maxlen=5))  # user_id: [(query, timestamp)]
start_ids: Dict[int, str] = {}  # user_id: start_id

# Constants
SEARCH_LIMIT = 50
VERIFICATION_DURATION = 3600  # 1 hour for GPLinks usage
PAGE_SIZE = 10  # Results per page
DELETE_DELAY = 600  # 10 minutes in seconds
ADMIN_PASSWORD = "12122"
RATE_LIMIT_WINDOW = 30  # Time window in seconds for rate limiting
RATE_LIMIT_MAX_MESSAGES = 20  # Max messages allowed in the window
MIN_MESSAGE_DELAY = 1.0  # Minimum delay between messages

# Dynamic rate limiting
message_timestamps: Deque[float] = deque(maxlen=RATE_LIMIT_MAX_MESSAGES)
message_queue: Queue = Queue()
is_sending = False

# Helper: Dynamic rate limiter to prevent flooding
async def rate_limit_message():
    global is_sending
    now = time.time()

    # Clean up old timestamps
    while message_timestamps and now - message_timestamps[0] > RATE_LIMIT_WINDOW:
        message_timestamps.popleft()

    # Check if we're within the rate limit
    if len(message_timestamps) >= RATE_LIMIT_MAX_MESSAGES:
        # Calculate how long to wait until the oldest message is outside the window
        wait_time = RATE_LIMIT_WINDOW - (now - message_timestamps[0])
        if wait_time > 0:
            logger.info(f"Rate limit hit, waiting {wait_time:.2f} seconds")
            await asyncio.sleep(wait_time)

    # Add the current timestamp
    message_timestamps.append(now)

    # Ensure a minimum delay between messages
    if message_timestamps and len(message_timestamps) > 1:
        last_time = message_timestamps[-2]
        time_since_last = now - last_time
        if time_since_last < MIN_MESSAGE_DELAY:
            await asyncio.sleep(MIN_MESSAGE_DELAY - time_since_last)

# Helper: Message sending queue to prevent flooding
async def send_message_queue(client: Client):
    global is_sending
    if is_sending:
        return
    is_sending = True
    try:
        while not message_queue.empty():
            try:
                func, args, kwargs = await message_queue.get()
                await rate_limit_message()
                await func(*args, **kwargs)
            except errors.FloodWait as e:
                logger.warning(f"FloodWait: Waiting for {e.value} seconds")
                await asyncio.sleep(e.value)
            except Exception as e:
                logger.error(f"Error sending message: {str(e)}")
            finally:
                message_queue.task_done()
    finally:
        is_sending = False

# Helper: Queue a message to be sent
async def queue_message(func, *args, **kwargs):
    await message_queue.put((func, args, kwargs))
    asyncio.create_task(send_message_queue(app))

# Helper: Generate dynamic ID for callbacks and start IDs
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

# Helper: Send log message to log channel if set
async def log_to_channel(client: Client, message: str):
    if log_channel is None:
        return
    try:
        await queue_message(client.send_message, log_channel, f"📋 Log: {message}")
        logger.info(f"Logged to channel {log_channel}: {message}")
    except Exception as e:
        logger.error(f"Failed to send log to channel {log_channel}: {e}")

# Helper: Check if the bot has sufficient privileges in a chat
async def check_bot_privileges(client: Client, chat_id: int, require_admin: bool = True) -> bool:
    try:
        bot_member: ChatMember = await client.get_chat_member(chat_id, "me")
        status = bot_member.status
        if not require_admin:
            return status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
        if status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await log_to_channel(client, f"Bot lacks admin privileges in chat {chat_id}: Status is {status}")
            return False
        if isinstance(bot_member, ChatMember) and hasattr(bot_member, 'privileges'):
            if bot_member.privileges and not bot_member.privileges.can_post_messages:
                await log_to_channel(client, f"Bot lacks post message privileges in chat {chat_id}")
                return False
        return True
    except errors.UserNotParticipant:
        await log_to_channel(client, f"Bot is not a participant in chat {chat_id}")
        return False
    except Exception as e:
        await log_to_channel(client, f"Error checking bot privileges in chat {chat_id}: {str(e)}")
        logger.error(f"Error checking bot privileges in chat {chat_id}: {e}")
        return False

# Helper: Check subscription status (only for private chats)
async def check_subscription(client: Client, user_id: int, chat_id: int) -> bool:
    if chat_id < 0:  # Skip subscription check in groups
        return True
    for channel_id in force_sub_channels:
        try:
            member = await client.get_chat_member(channel_id, user_id)
            if member.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
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
    try:
        await asyncio.sleep(DELETE_DELAY)
        await client.delete_messages(chat_id, [request_msg_id, response_msg_id])
        await log_to_channel(client, f"Deleted messages in chat {chat_id}: {request_msg_id}, {response_msg_id}")
        logger.info(f"Deleted messages in chat {chat_id}: {request_msg_id}, {response_msg_id}")
    except Exception as e:
        await log_to_channel(client, f"Error deleting messages in chat {chat_id}: {str(e)}")
        logger.error(f"Error deleting messages in chat {chat_id}: {e}")
    finally:
        message_pairs.pop(chat_id, None)

# Help command handler
@app.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    help_text = (
        "📚 **Help Guide**\n"
        "━━━━━━━━━━━━━━\n"
        "• Use `/start` to begin.\n"
        "• Search for files by typing a keyword (e.g., 'movie').\n"
        "• If prompted, join the required channels to proceed.\n"
        "• Admins can use commands like `/add_db`, `/stats`, etc. (see Admin Menu).\n"
        "━━━━━━━━━━━━━━\n"
        "Need more help? Contact an admin!"
    )
    await queue_message(message.reply, help_text)

# Start command handler
@app.on_message(filters.command("start"))
async def start(client: Client, message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    # Generate a unique start ID for each /start command (for internal use only)
    start_id = generate_dynamic_id()
    start_ids[user_id] = start_id
    await log_to_channel(client, f"User {user_id} used /start in chat {chat_id} with Start ID: {start_id}")

    # Check subscription (only in private chats)
    if chat_id > 0 and force_sub_channels and not await check_subscription(client, user_id, chat_id):
        buttons = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/c/{str(ch)[4:]}")] for ch in force_sub_channels]
        buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_sub")])
        await queue_message(message.reply, "Please join the required channels to use this bot:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # Welcome message for new users (only in private chats)
    if chat_id > 0:
        buttons = [
            [InlineKeyboardButton("📖 How to Download", url="https://t.me/c/2323164776/7")],
            [InlineKeyboardButton("🕒 Recent Searches", callback_data="view_history")]
        ]
        await queue_message(client.send_message, user_id, "Welcome!\nSearch for files by typing a keyword, or use /help for guidance.", reply_markup=InlineKeyboardMarkup(buttons))

    # Admin menu (text-based with "three lines" style, only in private chats)
    if chat_id > 0 and user_id in admin_list:
        admin_menu = (
            "👨‍💼 Admin Menu\n"
            "━━━━━━━━━━━━━━\n"
            "Available Commands:\n"
            "/add_db - Add a DB channel\n"
            "/add_sub - Add a subscription channel\n"
            "/stats - View bot statistics\n"
            "/broadcast - Broadcast a message\n"
            "/remove_channel - Remove a channel\n"
            "/admin_list - View admin list\n"
            "/set_logchannel - Set a log channel\n"
            "/clear_logs - Clear logs in log channel\n"
            "━━━━━━━━━━━━━━\n"
            "Enter the command to proceed (password required)."
        )
        await queue_message(message.reply, admin_menu)
    else:
        await queue_message(message.reply, "Hi!\nSend me a keyword to search for files, or use /help for guidance.")

# Handle admin commands
@app.on_message(filters.private & filters.command(["add_db", "add_sub", "stats", "broadcast", "remove_channel", "admin_list", "set_logchannel", "clear_logs"]))
async def handle_admin_commands(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in admin_list:
        await queue_message(message.reply, "🚫 This action is restricted to admins only.")
        await log_to_channel(client, f"User {user_id} attempted restricted admin command: {message.command[0]}")
        return

    command = message.command[0]
    await log_to_channel(client, f"Admin {user_id} used command: /{command}")

    if command == "admin_list":
        admin_text = "👥 Admin List\n━━━━━━━━━━━━━━\n" + "\n".join(f"Admin ID: {admin_id}" for admin_id in admin_list) + "\n━━━━━━━━━━━━━━"
        await queue_message(message.reply, admin_text)
        return

    if command == "set_logchannel":
        admin_pending_action[user_id] = "set_logchannel"
        await queue_message(message.reply, "Forward a message from the channel you want to set as the log channel (bot must be admin).")
        return

    if command == "clear_logs":
        if log_channel is None:
            await queue_message(message.reply, "❌ No log channel set. Use /set_logchannel to set one.")
            return
        try:
            # Delete all messages in the log channel (up to 100 at a time)
            async for msg in client.get_chat_history(log_channel, limit=100):
                await client.delete_messages(log_channel, msg.id)
            await queue_message(message.reply, "✅ Logs cleared in the log channel.")
            await log_to_channel(client, f"Admin {user_id} cleared logs in log channel {log_channel}")
        except Exception as e:
            await queue_message(message.reply, "❌ Error clearing logs.")
            await log_to_channel(client, f"Error clearing logs in log channel {log_channel}: {str(e)}")
        return

    admin_pending_action[user_id] = command
    await queue_message(message.reply, "🔒 Please enter the admin password to proceed:")

# Handle text queries (works in both private and group chats)
@app.on_message(filters.text & ~filters.command(["start", "help", "add_db", "add_sub", "stats", "broadcast", "remove_channel", "admin_list", "set_logchannel", "clear_logs"]))
async def handle_query(client: Client, message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    query = message.text.strip()

    # Prevent duplicate processing of the same message
    if chat_id in message_pairs:
        logger.warning(f"Duplicate query detected in chat {chat_id}, ignoring.")
        return

    # Log the search query and add to history
    await log_to_channel(client, f"User {user_id} searched for: '{query}' in chat {chat_id}")
    user_search_history[user_id].append((query, time.time()))

    # Check if the message is a password response for admin (only in private chats)
    if chat_id > 0 and user_id in admin_list and user_id in admin_pending_action:
        if query == ADMIN_PASSWORD:
            action = admin_pending_action.pop(user_id)
            if action == "add_db":
                await queue_message(message.reply, "Forward a message from the DB channel you want to add (bot must be admin).")
            elif action == "add_sub":
                await queue_message(message.reply, "Forward a message from the subscription channel you want to add (bot must be admin).")
            elif action == "stats":
                stats = (
                    f"📊 Bot Statistics\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"Users: {len(verified_users)}\n"
                    f"DB Channels: {len(db_channels)}\n"
                    f"Sub Channels: {len(force_sub_channels)}\n"
                    f"━━━━━━━━━━━━━━"
                )
                await queue_message(message.reply, stats)
            elif action == "remove_channel":
                buttons = [
                    [InlineKeyboardButton(f"DB: {ch}", callback_data=f"rm_db_{ch}") for ch in db_channels],
                    [InlineKeyboardButton(f"Sub: {ch}", callback_data=f"rm_sub_{ch}") for ch in force_sub_channels]
                ]
                await queue_message(message.reply, "Select channel to remove:", reply_markup=InlineKeyboardMarkup(buttons))
            elif action == "broadcast":
                await queue_message(message.reply, "Please send the message you want to broadcast to all groups.")
            elif action.startswith("add_db_forward_") or action.startswith("add_sub_forward_"):
                channel_type, _, channel_id = action.split("_")[1:4]
                channel_id = int(channel_id)
                if not await check_bot_privileges(client, channel_id):
                    await queue_message(message.reply, "❌ Bot must be an admin in the channel with sufficient privileges.")
                    return

                if channel_type == "db":
                    db_channels.add(channel_id)
                    await queue_message(message.reply, f"✅ DB channel {channel_id} added.")
                    await log_to_channel(client, f"Admin {user_id} added DB channel {channel_id}")
                else:  # sub
                    force_sub_channels.add(channel_id)
                    await queue_message(message.reply, f"✅ Subscription channel {channel_id} added.")
                    await log_to_channel(client, f"Admin {user_id} added subscription channel {channel_id}")
            elif action.startswith("rm_db_"):
                channel_id = int(action.split("_")[2])
                db_channels.discard(channel_id)
                await queue_message(message.reply, f"✅ DB channel {channel_id} removed.")
                await log_to_channel(client, f"Admin {user_id} removed DB channel {channel_id}")
            elif action.startswith("rm_sub_"):
                channel_id = int(action.split("_")[2])
                force_sub_channels.discard(channel_id)
                await queue_message(message.reply, f"✅ Subscription channel {channel_id} removed.")
                await log_to_channel(client, f"Admin {user_id} removed subscription channel {channel_id}")
        else:
            await queue_message(message.reply, "❌ Incorrect password. Try again.")
            admin_pending_action.pop(user_id, None)
        return

    # Check subscription (only in private chats)
    if chat_id > 0 and force_sub_channels and not await check_subscription(client, user_id, chat_id):
        buttons = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/c/{str(ch)[4:]}")] for ch in force_sub_channels]
        buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_sub")])
        await queue_message(message.reply, "Please join the required channels to use this bot:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # Input validation
    if len(query) < 3:
        await queue_message(message.reply, "Please enter a search term with at least 3 characters.")
        return

    # Check if bot has sufficient privileges in the group
    if chat_id < 0:
        if not await check_bot_privileges(client, chat_id):
            await queue_message(message.reply, "❌ I need to be an admin in this group with sufficient privileges to perform searches.")
            return

    searching_msg = await message.reply("🔍 Searching in database channels...")
    message_pairs[chat_id] = (message.id, searching_msg.id)

    # Search channels concurrently
    results = []
    async def search_channel(channel_id: int):
        try:
            if not await check_bot_privileges(client, channel_id, require_admin=False):
                logger.warning(f"Bot lacks access to channel {channel_id}")
                return

            async for msg in client.search_messages(
                chat_id=channel_id,
                query=query,
                limit=SEARCH_LIMIT
            ):
                if msg.media == MessageMediaType.DOCUMENT and hasattr(msg, 'document') and msg.document:
                    results.append({
                        "file_name": msg.document.file_name or "Unnamed File",
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

    try:
        tasks = [search_channel(channel_id) for channel_id in db_channels]
        await asyncio.gather(*tasks)

        if not results:
            await queue_message(searching_msg.edit, "No files found in the database channels.")
            return

        # Update searching message with results count
        await queue_message(searching_msg.edit, f"✅ Found {len(results)} file(s) matching your query.")

        # Paginate results, but only send the first page initially
        pages = [results[i:i + PAGE_SIZE] for i in range(0, len(results), PAGE_SIZE)]
        page_num = 1
        page = pages[page_num - 1]  # First page
        buttons = []
        for idx, file in enumerate(page, start=(page_num-1)*PAGE_SIZE + 1):
            dyn_id = generate_dynamic_id()
            button_text = f"{idx}. 📁 {file['file_name']} ({file['file_size']}MB)"
            buttons.append([InlineKeyboardButton(button_text, callback_data=f"get_{file['channel_id']}_{file['msg_id']}_{dyn_id}")])
        buttons.append([InlineKeyboardButton("📖 How to Download", url="https://t.me/c/2323164776/7")])
        if len(pages) > 1:
            nav_buttons = []
            if page_num < len(pages):
                nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page_num+1}"))
            buttons.append(nav_buttons)
        response = await message.reply(f"📂 Search Results (Page {page_num}/{len(pages)}):", reply_markup=InlineKeyboardMarkup(buttons))
        message_pairs[chat_id] = (message.id, response.id)
        asyncio.create_task(delete_messages_later(client, chat_id, message.id, response.id))
    except Exception as e:
        await queue_message(searching_msg.edit, "❌ An error occurred while searching. Please try again.")
        await log_to_channel(client, f"Error in search for user {user_id}: {str(e)}")
        logger.error(f"Error in search: {e}")
        message_pairs.pop(chat_id, None)

# Callback query handler
@app.on_callback_query()
async def handle_callbacks(client: Client, callback_query):
    data = callback_query.data
    user_id = callback_query.from_user.id
    chat_id = callback_query.message.chat.id

    try:
        if data == "check_sub":
            if await check_subscription(client, user_id, chat_id):
                verified_users[user_id] = time.time()
                await queue_message(callback_query.message.edit, "✅ Subscription verified! You can now search for files.")
                await log_to_channel(client, f"User {user_id} verified subscription in chat {chat_id}")
            else:
                await callback_query.answer("Please join all required channels.", show_alert=True)

        elif data == "view_history":
            history = user_search_history.get(user_id, [])
            if not history:
                await queue_message(callback_query.message.reply, "You have no recent searches.")
                return
            history_text = "🕒 Recent Searches\n━━━━━━━━━━━━━━\n"
            for idx, (query, timestamp) in enumerate(history, 1):
                time_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
                history_text += f"{idx}. '{query}' at {time_str}\n"
            history_text += "━━━━━━━━━━━━━━"
            await queue_message(callback_query.message.reply, history_text)

        elif data.startswith("get_"):
            _, channel_id, msg_id, _ = data.split("_", 3)

            if chat_id > 0 and force_sub_channels and not await check_subscription(client, user_id, chat_id):
                buttons = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/c/{str(ch)[4:]}")] for ch in force_sub_channels]
                buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_sub")])
                await queue_message(callback_query.message.reply, "Please join the required channels:", reply_markup=InlineKeyboardMarkup(buttons))
                return

            verified_time = verified_users.get(user_id, 0)
            now = time.time()
            use_shortener = now - verified_time > VERIFICATION_DURATION

            file_link = f"https://t.me/c/{str(channel_id)[4:]}/{msg_id}"
            if use_shortener:
                file_link = await shorten_link(file_link)
                await queue_message(
                    callback_query.message.reply,
                    "ℹ️ Type movie name: hello and get your files like this\n🔗 Link generated with shortening:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬇️ Download", url=file_link)],
                        [InlineKeyboardButton("📖 How to Download", url="https://t.me/c/2323164776/7")]
                    ])
                )
                await log_to_channel(client, f"User {user_id} requested shortened download link for message {msg_id} in channel {channel_id}")
            else:
                await queue_message(
                    callback_query.message.reply,
                    "ℹ️ Type movie name: hello and get your files like this\n📥 Direct download link:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬇️ Download", url=file_link)],
                        [InlineKeyboardButton("📖 How to Download", url="https://t.me/c/2323164776/7")]
                    ])
                )
                await log_to_channel(client, f"User {user_id} requested direct download link for message {msg_id} in channel {channel_id}")

        elif data.startswith("page_"):
            page_num = int(data.split("_")[1])
            # Recompute results (simplified; in a real app, you'd cache the results)
            query = user_search_history[user_id][-1][0] if user_search_history[user_id] else ""
            results = []
            async def search_channel(channel_id: int):
                try:
                    if not await check_bot_privileges(client, channel_id, require_admin=False):
                        logger.warning(f"Bot lacks access to channel {channel_id}")
                        return
                    async for msg in client.search_messages(chat_id=channel_id, query=query, limit=SEARCH_LIMIT):
                        if msg.media == MessageMediaType.DOCUMENT and hasattr(msg, 'document') and msg.document:
                            results.append({
                                "file_name": msg.document.file_name or "Unnamed File",
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

            pages = [results[i:i + PAGE_SIZE] for i in range(0, len(results), PAGE_SIZE)]
            if page_num < 1 or page_num > len(pages):
                await callback_query.answer("Invalid page number.", show_alert=True)
                return

            page = pages[page_num - 1]
            buttons = []
            for idx, file in enumerate(page, start=(page_num-1)*PAGE_SIZE + 1):
                dyn_id = generate_dynamic_id()
                button_text = f"{idx}. 📁 {file['file_name']} ({file['file_size']}MB)"
                buttons.append([InlineKeyboardButton(button_text, callback_data=f"get_{file['channel_id']}_{file['msg_id']}_{dyn_id}")])
            buttons.append([InlineKeyboardButton("📖 How to Download", url="https://t.me/c/2323164776/7")])
            nav_buttons = []
            if page_num > 1:
                nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"page_{page_num-1}"))
            if page_num < len(pages):
                nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page_num+1}"))
            if nav_buttons:
                buttons.append(nav_buttons)
            await queue_message(callback_query.message.edit, f"📂 Search Results (Page {page_num}/{len(pages)}):", reply_markup=InlineKeyboardMarkup(buttons))

        # Admin actions with password prompt
        elif data in ["add_db", "add_sub", "stats", "remove_channel"] and user_id in admin_list:
            admin_pending_action[user_id] = data
            await queue_message(callback_query.message.reply, "🔒 Please enter the admin password to proceed:")
            await log_to_channel(client, f"Admin {user_id} initiated action: {data}")

        elif data.startswith("rm_db_") and user_id in admin_list:
            admin_pending_action[user_id] = data
            await queue_message(callback_query.message.reply, "🔒 Please enter the admin password to proceed:")
            await log_to_channel(client, f"Admin {user_id} initiated remove DB channel action: {data}")

        elif data.startswith("rm_sub_") and user_id in admin_list:
            admin_pending_action[user_id] = data
            await queue_message(callback_query.message.reply, "🔒 Please enter the admin password to proceed:")
            await log_to_channel(client, f"Admin {user_id} initiated remove subscription channel action: {data}")

        elif data.startswith("add_db_forward_") and user_id in admin_list:
            admin_pending_action[user_id] = data
            await queue_message(callback_query.message.reply, "🔒 Please enter the admin password to proceed:")
            await log_to_channel(client, f"Admin {user_id} initiated add DB channel action: {data}")

        elif data.startswith("add_sub_forward_") and user_id in admin_list:
            admin_pending_action[user_id] = data
            await queue_message(callback_query.message.reply, "🔒 Please enter the admin password to proceed:")
            await log_to_channel(client, f"Admin {user_id} initiated add subscription channel action: {data}")

    except Exception as e:
        await log_to_channel(client, f"Error in callback for user {user_id}: {str(e)}")
        logger.error(f"Error in callback: {e}")
        await callback_query.answer("An error occurred. Please try again.", show_alert=True)

# Handle forwarded message from admin (strictly for admin)
@app.on_message(filters.private & filters.forwarded)
async def add_channel(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in admin_list:
        await queue_message(message.reply, "🚫 This action is restricted to admins only.")
        await log_to_channel(client, f"User {user_id} attempted to forward a message for admin action")
        return

    chat = message.forward_from_chat
    if not chat:
        await queue_message(message.reply, "❌ Invalid forwarded message.")
        return

    if not await check_bot_privileges(client, chat.id):
        await queue_message(message.reply, "❌ Bot must be an admin in the channel with sufficient privileges.")
        return

    if user_id in admin_pending_action and admin_pending_action[user_id] == "set_logchannel":
        global log_channel
        log_channel = chat.id
        admin_pending_action.pop(user_id, None)
        await queue_message(message.reply, f"✅ Log channel set to {chat.id}.")
        await log_to_channel(client, f"Admin {user_id} set log channel to {chat.id}")
        return

    await queue_message(
        message.reply,
        "Is this a DB channel or a subscription channel?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("DB Channel", callback_data=f"add_db_forward_{chat.id}")],
            [InlineKeyboardButton("Subscription Channel", callback_data=f"add_sub_forward_{chat.id}")]
        ])
    )
    await log_to_channel(client, f"Admin {user_id} forwarded a message to add channel {chat.id}")

# Handle broadcast message after password verification
@app.on_message(filters.private & filters.text & filters.regex(r"^(?!/start$|/help$|add_db$|add_sub$|stats$|broadcast$|remove_channel$|admin_list$|set_logchannel$|clear_logs$).+"))
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
            await queue_message(client.send_message, channel_id, f"📢 Broadcast Message:\n{broadcast_message}")
            await log_to_channel(client, f"Broadcast sent to channel {channel_id}: {broadcast_message}")
            logger.info(f"Broadcast sent to channel {channel_id}")
        except Exception as e:
            await log_to_channel(client, f"Error sending broadcast to channel {channel_id}: {str(e)}")
            logger.error(f"Error sending broadcast to channel {channel_id}: {e}")

    await queue_message(message.reply, f"✅ Broadcast sent to {len(all_channels)} channels.")
    await log_to_channel(client, f"Admin {user_id} broadcasted message to {len(all_channels)} channels")

# Run bot
if __name__ == "__main__":
    logger.info("Starting File Request Bot")
    app.run()
