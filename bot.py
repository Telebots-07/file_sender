import asyncio
from pyrogram import Client, filters, errors
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, ChatMember
from pyrogram.enums import ChatMemberStatus, MessageMediaType
import time
import os
from datetime import datetime, timedelta
from collections import defaultdict, deque
from typing import Dict, Set, Optional, Deque, Tuple, List
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
admin_batch_keywords: Dict[int, str] = {}  # user_id: batch keyword (for genbatch/editbatch)
batches: Dict[str, Dict] = {}  # keyword: {"channel_id": int, "msg_ids": List[int]}
admin_list: Set[int] = {ADMIN_ID}  # Set of admin IDs (starting with the main admin)
log_channel: Optional[int] = None  # Log channel ID (set by admin)
user_search_history: Dict[int, Deque[Tuple[str, float]]] = defaultdict(lambda: deque(maxlen=5))  # user_id: [(query, timestamp)]
start_ids: Dict[int, str] = {}  # user_id: start_id
user_search_counts: Dict[int, int] = defaultdict(int)  # user_id: number of searches
search_cache: Dict[int, List[dict]] = {}  # chat_id: cached search results (temporary)
search_cache_expiry: Dict[int, float] = {}  # chat_id: cache expiry timestamp

# Constants
SEARCH_LIMIT = 50
VERIFICATION_DURATION = 3600  # 1 hour for GPLinks usage
PAGE_SIZE = 10  # Results per page
DELETE_DELAY = 600  # 10 minutes in seconds
ADMIN_PASSWORD = "12122"
RATE_LIMIT_WINDOW = 30  # Time window in seconds for rate limiting
RATE_LIMIT_MAX_MESSAGES = 20  # Max messages allowed in the window (configurable)
MIN_MESSAGE_DELAY = 1.0  # Minimum delay between messages (configurable)
CACHE_DURATION = 300  # 5 minutes for search result caching

# Dynamic rate limiting
message_timestamps: Deque[float] = deque(maxlen=RATE_LIMIT_MAX_MESSAGES)
message_queue: Queue = Queue()
is_sending = False

# Helper: Dynamic rate limiter to prevent flooding
async def rate_limit_message():
    global RATE_LIMIT_MAX_MESSAGES, MIN_MESSAGE_DELAY
    now = time.time()

    # Clean up old timestamps
    while message_timestamps and now - message_timestamps[0] > RATE_LIMIT_WINDOW:
        message_timestamps.popleft()

    # Check if we're within the rate limit
    if len(message_timestamps) >= RATE_LIMIT_MAX_MESSAGES:
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
                    shortened_url = (await response.text()).strip()
                    logger.info(f"Shortened URL: {shortened_url}")
                    return shortened_url
                else:
                    logger.warning(f"GPLinks API failed with status {response.status}")
                    return long_url
        except Exception as e:
            logger.error(f"Shorten link error: {str(e)}")
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
        search_cache.pop(chat_id, None)
        search_cache_expiry.pop(chat_id, None)

# Feedback command handler
@app.on_message(filters.command("feedback"))
async def feedback_command(client: Client, message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    if len(message.command) < 2:
        await queue_message(message.reply, "Please provide your feedback. Usage: /feedback <your feedback>")
        return

    feedback = " ".join(message.command[1:])
    await log_to_channel(client, f"Feedback from user {user_id} in chat {chat_id}: {feedback}")
    await queue_message(message.reply, "Thank you for your feedback! It has been sent to the admins.")

# Help command handler
@app.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    help_text = (
        "📚 **Help Guide**\n"
        "━━━━━━━━━━━━━━\n"
        "• Use `/start` to begin.\n"
        "• Search for files by typing a keyword (e.g., 'movie').\n"
        "• If prompted, join the required channels to proceed.\n"
        "• Use `/feedback <message>` to send feedback to admins.\n"
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
            "/genbatch - Generate a new batch of files\n"
            "/editbatch - Edit an existing batch of files\n"
            "/stats - View bot statistics\n"
            "/user_stats - View user activity statistics\n"
            "/broadcast - Broadcast a message\n"
            "/remove_channel - Remove a channel\n"
            "/admin_list - View admin list\n"
            "/set_logchannel - Set a log channel\n"
            "/set_rate_limit - Adjust rate limiting settings\n"
            "/clear_logs - Clear logs in log channel\n"
            "━━━━━━━━━━━━━━\n"
            "Enter the command to proceed (password required)."
        )
        await queue_message(message.reply, admin_menu)
    else:
        await queue_message(message.reply, "Hi!\nSend me a keyword to search for files, or use /help for guidance.")

# Handle admin commands
@app.on_message(filters.private & filters.command(["add_db", "add_sub", "genbatch", "editbatch", "stats", "user_stats", "broadcast", "remove_channel", "admin_list", "set_logchannel", "set_rate_limit", "clear_logs"]))
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

    if command == "set_rate_limit":
        admin_pending_action[user_id] = "set_rate_limit"
        await queue_message(message.reply, "Please provide the new rate limit settings in the format: max_messages min_delay (e.g., 15 1.5)")
        return

    if command == "genbatch":
        admin_pending_action[user_id] = "genbatch_keyword"
        await queue_message(message.reply, "Please provide the keyword for this batch:")
        return

    if command == "editbatch":
        if not batches:
            await queue_message(message.reply, "❌ No batches exist. Create a batch using /genbatch first.")
            return
        admin_pending_action[user_id] = "editbatch_keyword"
        await queue_message(message.reply, "Please provide the keyword of the batch you want to edit:")
        return

    if command == "user_stats":
        stats_text = "📊 User Activity Statistics\n━━━━━━━━━━━━━━\n"
        for uid, count in user_search_counts.items():
            stats_text += f"User ID: {uid}, Searches: {count}\n"
        stats_text += "━━━━━━━━━━━━━━"
        await queue_message(message.reply, stats_text)
        return

    if command == "clear_logs":
        if log_channel is None:
            await queue_message(message.reply, "❌ No log channel set. Use /set_logchannel to set one.")
            return
        try:
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
@app.on_message(filters.text & ~filters.command(["start", "help", "feedback", "add_db", "add_sub", "genbatch", "editbatch", "stats", "user_stats", "broadcast", "remove_channel", "admin_list", "set_logchannel", "set_rate_limit", "clear_logs"]))
async def handle_query(client: Client, message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    query = message.text.strip().lower()  # Normalize query to lowercase

    # Prevent duplicate processing of the same message
    if chat_id in message_pairs:
        logger.warning(f"Duplicate query detected in chat {chat_id}, ignoring.")
        return

    # Log the search query, update history, and count
    await log_to_channel(client, f"User {user_id} searched for: '{query}' in chat {chat_id}")
    user_search_history[user_id].append((query, time.time()))
    user_search_counts[user_id] += 1

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
                    f"Batches: {len(batches)}\n"
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
            elif action == "set_rate_limit":
                try:
                    max_msgs, min_delay = map(float, query.split())
                    if max_msgs < 1 or min_delay < 0.5:
                        await queue_message(message.reply, "❌ Invalid values. max_messages must be >= 1, min_delay must be >= 0.5")
                        return
                    global RATE_LIMIT_MAX_MESSAGES, MIN_MESSAGE_DELAY
                    RATE_LIMIT_MAX_MESSAGES = int(max_msgs)
                    MIN_MESSAGE_DELAY = min_delay
                    message_timestamps.maxlen = RATE_LIMIT_MAX_MESSAGES
                    await queue_message(message.reply, f"✅ Rate limits updated: max_messages={RATE_LIMIT_MAX_MESSAGES}, min_delay={MIN_MESSAGE_DELAY}")
                    await log_to_channel(client, f"Admin {user_id} updated rate limits: max_messages={RATE_LIMIT_MAX_MESSAGES}, min_delay={MIN_MESSAGE_DELAY}")
                except ValueError:
                    await queue_message(message.reply, "❌ Invalid format. Please use: max_messages min_delay (e.g., 15 1.5)")
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

    # Handle genbatch/editbatch keyword input
    if chat_id > 0 and user_id in admin_list and user_id in admin_pending_action:
        if admin_pending_action[user_id] == "genbatch_keyword":
            if not query:
                await queue_message(message.reply, "❌ Please provide a valid keyword.")
                return
            admin_batch_keywords[user_id] = query.lower()
            admin_pending_action[user_id] = "genbatch_files"
            await queue_message(
                message.reply,
                "Please send the files for this batch. When you're done, type 'Done' or use the button below.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="genbatch_done")]])
            )
            return
        elif admin_pending_action[user_id] == "editbatch_keyword":
            if not query:
                await queue_message(message.reply, "❌ Please provide a valid keyword.")
                return
            keyword = query.lower()
            if keyword not in batches:
                await queue_message(message.reply, f"❌ No batch found with keyword '{keyword}'. Create a batch using /genbatch first.")
                admin_pending_action.pop(user_id, None)
                return
            admin_batch_keywords[user_id] = keyword
            admin_pending_action[user_id] = "editbatch_files"
            await queue_message(
                message.reply,
                "Please send the new files for this batch. When you're done, type 'Done' or use the button below.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="editbatch_done")]])
            )
            return
        elif admin_pending_action[user_id] in ("genbatch_files", "editbatch_files") and query.lower() == "done":
            if admin_pending_action[user_id] == "genbatch_files":
                await queue_message(message.reply, "✅ Batch creation completed.")
                await log_to_channel(client, f"Admin {user_id} completed batch creation for keyword '{admin_batch_keywords[user_id]}'")
            else:
                await queue_message(message.reply, "✅ Batch edit completed.")
                await log_to_channel(client, f"Admin {user_id} completed batch edit for keyword '{admin_batch_keywords[user_id]}'")
            admin_pending_action.pop(user_id, None)
            admin_batch_keywords.pop(user_id, None)
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

    # Check if query matches a batch
    batch_results = []
    matched_keyword = None
    for keyword in batches:
        if query in keyword or keyword in query:  # Partial match
            matched_keyword = keyword
            batch = batches[keyword]
            channel_id = batch["channel_id"]
            msg_ids = batch["msg_ids"]
            try:
                for msg_id in msg_ids:
                    msg = await client.get_messages(channel_id, msg_id)
                    if msg.media == MessageMediaType.DOCUMENT and hasattr(msg, 'document') and msg.document:
                        file_name = msg.document.file_name or "Unnamed File"
                        batch_results.append({
                            "file_name": file_name,
                            "file_size": round(msg.document.file_size / (1024 * 1024), 2),
                            "file_id": msg.document.file_id,
                            "msg_id": msg.id,
                            "channel_id": channel_id
                        })
            except Exception as e:
                await log_to_channel(client, f"Error fetching batch files for keyword '{keyword}': {str(e)}")
                continue
            break

    if batch_results:
        await log_to_channel(client, f"User {user_id} found batch match for query '{query}' with keyword '{matched_keyword}'")
        results = batch_results
    else:
        # Check if results are in cache
        now = time.time()
        if chat_id in search_cache and chat_id in search_cache_expiry and now < search_cache_expiry[chat_id]:
            results = search_cache[chat_id]
            await log_to_channel(client, f"User {user_id} used cached results for query: '{query}'")
        else:
            # Search channels concurrently
            results = []
            async def search_channel(channel_id: int):
                try:
                    if not await check_bot_privileges(client, channel_id, require_admin=False):
                        logger.warning(f"Bot lacks access to channel {channel_id}")
                        return

                    # Search for messages matching the query
                    async for msg in client.search_messages(chat_id=channel_id, query=query, limit=SEARCH_LIMIT):
                        # Check if the message is a document or has a caption matching the query
                        if msg.media == MessageMediaType.DOCUMENT and hasattr(msg, 'document') and msg.document:
                            file_name = msg.document.file_name or "Unnamed File"
                            # Also check caption for broader matching
                            caption = msg.caption.lower() if msg.caption else ""
                            if query in file_name.lower() or query in caption:
                                results.append({
                                    "file_name": file_name,
                                    "file_size": round(msg.document.file_size / (1024 * 1024), 2),
                                    "file_id": msg.document.file_id,
                                    "msg_id": msg.id,
                                    "channel_id": channel_id
                                })
                                logger.info(f"Match found in channel {channel_id}: {file_name}")
                        else:
                            # Log if a message doesn't match the criteria
                            logger.debug(f"Message {msg.id} in channel {channel_id} is not a document or doesn't match query")
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
                    await log_to_channel(client, f"No matches found for query '{query}' in chat {chat_id}")
                    return

                # Cache the results
                search_cache[chat_id] = results
                search_cache_expiry[chat_id] = now + CACHE_DURATION
            except Exception as e:
                await queue_message(searching_msg.edit, "❌ An error occurred while searching. Please try again.")
                await log_to_channel(client, f"Search error for user {user_id} in chat {chat_id}: {str(e)}")
                logger.error(f"Search error: {e}")
                message_pairs.pop(chat_id, None)
                return

    # Display results (first page)
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

    # Edit the searching message to show results
    await queue_message(
        searching_msg.edit,
        f"✅ Found {len(results)} file(s) matching your query.\n\n📂 Search Results (Page {page_num}/{len(pages)}):",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    message_pairs[chat_id] = (message.id, searching_msg.id)
    asyncio.create_task(delete_messages_later(client, chat_id, message.id, searching_msg.id))

# Handle media messages (for genbatch/editbatch)
@app.on_message(filters.private & (filters.document | filters.photo | filters.video | filters.audio))
async def handle_media(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in admin_list or user_id not in admin_pending_action:
        return

    if admin_pending_action[user_id] not in ("genbatch_files", "editbatch_files"):
        return

    # Check if a database channel exists
    if not db_channels:
        await queue_message(message.reply, "❌ No database channel found. Please add one using /add_db first.")
        admin_pending_action.pop(user_id, None)
        admin_batch_keywords.pop(user_id, None)
        return

    # Select the first available database channel
    channel_id = next(iter(db_channels))
    keyword = admin_batch_keywords[user_id]

    try:
        # If editing a batch, delete the old files
        if admin_pending_action[user_id] == "editbatch_files":
            if keyword in batches:
                old_batch = batches[keyword]
                old_channel_id = old_batch["channel_id"]
                old_msg_ids = old_batch["msg_ids"]
                try:
                    await client.delete_messages(old_channel_id, old_msg_ids)
                    await log_to_channel(client, f"Admin {user_id} deleted old files for batch '{keyword}' in channel {old_channel_id}")
                except Exception as e:
                    await log_to_channel(client, f"Error deleting old files for batch '{keyword}': {str(e)}")
                # Clear the old message IDs
                batches[keyword]["msg_ids"] = []
            else:
                await queue_message(message.reply, "❌ Batch not found. Please start over with /editbatch.")
                admin_pending_action.pop(user_id, None)
                admin_batch_keywords.pop(user_id, None)
                return

        # Upload the file to the database channel
        caption = f"Batch: {keyword}"
        if message.document:
            sent_msg = await client.send_document(channel_id, message.document.file_id, caption=caption)
        elif message.photo:
            sent_msg = await client.send_photo(channel_id, message.photo.file_id, caption=caption)
        elif message.video:
            sent_msg = await client.send_video(channel_id, message.video.file_id, caption=caption)
        elif message.audio:
            sent_msg = await client.send_audio(channel_id, message.audio.file_id, caption=caption)
        else:
            await queue_message(message.reply, "❌ Unsupported file type.")
            return

        # Store the message ID in the batch
        if keyword not in batches:
            batches[keyword] = {"channel_id": channel_id, "msg_ids": []}
        batches[keyword]["msg_ids"].append(sent_msg.id)
        await log_to_channel(client, f"Admin {user_id} added file to batch '{keyword}' in channel {channel_id}, msg_id: {sent_msg.id}")

        await queue_message(
            message.reply,
            "✅ File added to the batch. Send more files or type 'Done' to finish.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="genbatch_done" if admin_pending_action[user_id] == "genbatch_files" else "editbatch_done")]])
        )

    except Exception as e:
        await queue_message(message.reply, "❌ Error uploading file to the database channel.")
        await log_to_channel(client, f"Error uploading file for batch '{keyword}' by admin {user_id}: {str(e)}")
        admin_pending_action.pop(user_id, None)
        admin_batch_keywords.pop(user_id, None)

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

        elif data == "genbatch_done" and user_id in admin_list and admin_pending_action.get(user_id) == "genbatch_files":
            await queue_message(callback_query.message.reply, "✅ Batch creation completed.")
            await log_to_channel(client, f"Admin {user_id} completed batch creation for keyword '{admin_batch_keywords[user_id]}'")
            admin_pending_action.pop(user_id, None)
            admin_batch_keywords.pop(user_id, None)

        elif data == "editbatch_done" and user_id in admin_list and admin_pending_action.get(user_id) == "editbatch_files":
            await queue_message(callback_query.message.reply, "✅ Batch edit completed.")
            await log_to_channel(client, f"Admin {user_id} completed batch edit for keyword '{admin_batch_keywords[user_id]}'")
            admin_pending_action.pop(user_id, None)
            admin_batch_keywords.pop(user_id, None)

        elif data.startswith("get_"):
            _, channel_id, msg_id, _ = data.split("_", 3)
            channel_id = int(channel_id)
            msg_id = int(msg_id)

            # Log subscription check
            if chat_id > 0 and force_sub_channels:
                sub_status = await check_subscription(client, user_id, chat_id)
                await log_to_channel(client, f"Subscription check for user {user_id} in chat {chat_id}: {sub_status}")
                if not sub_status:
                    buttons = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/c/{str(ch)[4:]}")] for ch in force_sub_channels]
                    buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_sub")])
                    await queue_message(callback_query.message.reply, "Please join the required channels:", reply_markup=InlineKeyboardMarkup(buttons))
                    return

            # Log verification status
            verified_time = verified_users.get(user_id, 0)
            now = time.time()
            use_shortener = now - verified_time > VERIFICATION_DURATION
            await log_to_channel(client, f"Verification status for user {user_id}: use_shortener={use_shortener}, verified_time={verified_time}, now={now}")

            # Generate the file link
            # Ensure channel_id is correctly formatted (remove -100 prefix)
            channel_id_str = str(channel_id)
            if channel_id_str.startswith("-100"):
                channel_id_str = channel_id_str[4:]
            else:
                await log_to_channel(client, f"Invalid channel_id format for link generation: {channel_id}")
                await callback_query.answer("Error generating file link. Please try again.", show_alert=True)
                return

            file_link = f"https://t.me/c/{channel_id_str}/{msg_id}"
            await log_to_channel(client, f"Generated file link for user {user_id}: {file_link}")

            if use_shortener:
                file_link = await shorten_link(file_link)
                await queue_message(
                    callback_query.message.reply,
                    f"ℹ️ Type movie name: hello and get your files like this\n🔗 Link generated with shortening:\n{file_link}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬇️ Download", url=file_link)],
                        [InlineKeyboardButton("📖 How to Download", url="https://t.me/c/2323164776/7")]
                    ])
                )
                await log_to_channel(client, f"User {user_id} requested shortened download link for message {msg_id} in channel {channel_id}")
            else:
                await queue_message(
                    callback_query.message.reply,
                    f"ℹ️ Type movie name: hello and get your files like this\n📥 Direct download link:\n{file_link}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬇️ Download", url=file_link)],
                        [InlineKeyboardButton("📖 How to Download", url="https://t.me/c/2323164776/7")]
                    ])
                )
                await log_to_channel(client, f"User {user_id} requested direct download link for message {msg_id} in channel {channel_id}")

        elif data.startswith("page_"):
            page_num = int(data.split("_")[1])
            # Use cached results if available
            now = time.time()
            if chat_id not in search_cache or chat_id not in search_cache_expiry or now >= search_cache_expiry[chat_id]:
                await callback_query.answer("Search results have expired. Please search again.", show_alert=True)
                return

            results = search_cache[chat_id]
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
            await queue_message(
                callback_query.message.edit,
                f"📂 Search Results (Page {page_num}/{len(pages)}):",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

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
@app.on_message(filters.private & filters.text & filters.regex(r"^(?!/start$|/help$|/feedback$|add_db$|add_sub$|genbatch$|editbatch$|stats$|user_stats$|broadcast$|remove_channel$|admin_list$|set_logchannel$|set_rate_limit$|clear_logs$).+"))
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
