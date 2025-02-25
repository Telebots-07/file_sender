import os
import requests
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, MessageHandler, filters, CallbackContext

# Load environment variables
load_dotenv()

# Secure API credentials
BOT_TOKEN = os.getenv("BOT_TOKEN")
MODIJIURL_API = os.getenv("MODIJIURL_API")
API_KEY = os.getenv("API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")

# Enable logging (for debugging on Render)
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

# File Storage Mapping (TeraBox File Links)
file_links = {
    "SAMPLE": "https://t.me/Filesreceive_bot?start=BQADAQAD1wYAAorUkUXMvLbB2RfBxRYE",
    "SAMPLE1": "https://1024terabox.com/s/1_cDqyr7PzTEys5JMLg9F_A",
}

# Function to shorten link using ModijiURL
async def shorten_link(long_url):
    try:
        response = requests.post(f"{MODIJIURL_API}/shorten", data={"api_key": API_KEY, "url": long_url})
        if response.status_code == 200:
            return response.json().get("shortenedUrl", long_url)
        else:
            logging.error(f"Error shortening link: {response.text}")
            return long_url
    except Exception as e:
        logging.error(f"Exception in shorten_link: {e}")
        return long_url

# Function to check if user is subscribed
async def is_subscribed(user_id):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember?chat_id={CHANNEL_ID}&user_id={user_id}"
        response = requests.get(url).json()
        status = response.get("result", {}).get("status", "left")
        return status in ["member", "administrator", "creator"]
    except Exception as e:
        logging.error(f"Error checking subscription: {e}")
        return False

# Function to handle messages (users type file name)
async def handle_message(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    text = update.message.text.strip().upper()

    # Subscription Check
    if not await is_subscribed(user_id):
        button = [[InlineKeyboardButton("🔔 Subscribe Now", url=f"https://t.me/{CHANNEL_ID}")]]
        reply_markup = InlineKeyboardMarkup(button)
        await update.message.reply_text("🚨 Please subscribe to access files!", reply_markup=reply_markup)
        return

    # Check if file exists
    if text in file_links:
        original_link = file_links[text]
        short_link = await shorten_link(original_link)

        # Send Inline Button
        button = [[InlineKeyboardButton("📥 Download File", url=short_link)]]
        reply_markup = InlineKeyboardMarkup(button)
        await update.message.reply_text("✅ Here is your file:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("❌ File not found! Please check the filename.")

# Main function to run the bot
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logging.info("Bot is running on Render with inline buttons...")
    app.run_polling()

if __name__ == "__main__":
    main()
