import os
import re
import asyncio
import logging
import threading
import time
import random
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import urllib.parse

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
RENDER_APP_URL = os.getenv('RENDER_APP_URL', '')

# Store user data
user_files = {}
user_processing = {}
user_progress_messages = {}

# Keep-alive configuration
KEEP_ALIVE_INTERVAL = 10 * 60
last_activity = time.time()

def create_progress_bar(current, total, bar_length=20):
    """Create a visual progress bar"""
    progress = current / total
    filled_length = int(bar_length * progress)
    bar = '█' * filled_length + '░' * (bar_length - filled_length)
    percentage = int(progress * 100)
    return f"{bar} {percentage}%"

# Simple HTTP server for health checks
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global last_activity
        last_activity = time.time()
        
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        elif self.path == '/wake':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'AWAKE')
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        return

def run_health_server():
    """Run a simple HTTP server for health checks"""
    port = int(os.getenv('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    logger.info(f"Health server running on port {port}")
    server.serve_forever()

def keep_alive_ping():
    """Ping the app itself to keep it awake"""
    if RENDER_APP_URL:
        try:
            response = requests.get(f"{RENDER_APP_URL}/wake", timeout=10)
            logger.info(f"Keep-alive ping sent: {response.status_code}")
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")

def keep_alive_worker():
    """Background thread to keep the app alive"""
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        keep_alive_ping()

def update_activity():
    """Update the last activity timestamp"""
    global last_activity
    last_activity = time.time()

def setup_selenium():
    """Setup reliable headless Chrome for Selenium"""
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--remote-debugging-port=9222")
    
    # Set download behavior
    chrome_options.add_experimental_option("prefs", {
        "download.default_directory": "/tmp",
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True
    })
    
    # For Render environment
    chrome_options.binary_location = "/usr/bin/chromium"
    
    try:
        driver = webdriver.Chrome(options=chrome_options)
        return driver
    except Exception as e:
        logger.error(f"Failed to setup Selenium: {e}")
        return None

def parse_file_links(content):
    """Parse the text file to extract file names and URLs"""
    lines = content.split('\n')
    file_links = []
    
    for i, line in enumerate(lines):
        line = line.strip()
        # Look for file names with extensions
        if re.search(r'\.(pdf|jpg|jpeg|png|doc|docx|txt)$', line, re.IGNORECASE):
            # Check if next line is a URL
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if next_line.startswith('http'):
                    file_links.append({
                        'name': line,
                        'url': next_line,
                        'type': 'pdf' if '.pdf' in line.lower() else 'image' if any(ext in line.lower() for ext in ['.jpg', '.jpeg', '.png']) else 'document'
                    })
    
    return file_links

def download_file_with_selenium(url, filename):
    """Download file using Selenium with proper waiting"""
    try:
        driver = setup_selenium()
        if not driver:
            return None
            
        logger.info(f"Downloading: {filename} from {url}")
        driver.get(url)
        
        # Wait for page to load
        time.sleep(5)
        
        # Check if we're redirected to a download or viewing page
        current_url = driver.current_url
        
        # For direct file links, we can use requests
        if any(ext in current_url for ext in ['.pdf', '.jpg', '.jpeg', '.png']):
            # It's a direct file link, use requests
            driver.quit()
            return download_file_direct(current_url, filename)
        else:
            # Try to find download buttons or links
            download_buttons = driver.find_elements(By.XPATH, "//a[contains(text(), 'Download')] | //button[contains(text(), 'Download')] | //a[contains(@href, 'download')]")
            
            if download_buttons:
                download_buttons[0].click()
                time.sleep(5)
                
                # Check for downloaded file in /tmp
                downloaded_files = os.listdir('/tmp')
                matching_files = [f for f in downloaded_files if filename.lower() in f.lower()]
                
                if matching_files:
                    file_path = os.path.join('/tmp', matching_files[0])
                    with open(file_path, 'rb') as f:
                        file_data = f.read()
                    
                    # Clean up
                    os.remove(file_path)
                    driver.quit()
                    return file_data
            else:
                # If no download button, try to get the file directly
                driver.quit()
                return download_file_direct(url, filename)
                
        driver.quit()
        return None
        
    except Exception as e:
        logger.error(f"Error downloading {filename}: {e}")
        try:
            driver.quit()
        except:
            pass
        return None

def download_file_direct(url, filename):
    """Download file directly using requests"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=30, stream=True)
        response.raise_for_status()
        
        return response.content
        
    except Exception as e:
        logger.error(f"Direct download failed for {filename}: {e}")
        return None

async def process_files_sequentially(update, context, user_id):
    """Process files one by one with progress tracking"""
    try:
        if user_id not in user_files:
            return
        
        content = user_files[user_id]
        file_links = parse_file_links(content)
        
        total_files = len(file_links)
        if total_files == 0:
            await update.message.reply_text("❌ No downloadable files found in the text file!")
            return
        
        # Send initial progress message
        progress_msg = await update.message.reply_text(
            f"📥 **Starting File Download**\n"
            f"📊 Progress: {create_progress_bar(0, total_files)}\n"
            f"⏳ Status: Initializing...\n"
            f"✅ Successful: 0\n"
            f"❌ Failed: 0"
        )
        user_progress_messages[user_id] = progress_msg.message_id
        
        success_count = 0
        failed_count = 0
        
        # Process each file one by one
        for current, file_info in enumerate(file_links, 1):
            # Check if processing was stopped
            if user_id not in user_processing or not user_processing[user_id]:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=progress_msg.message_id,
                    text="❌ Download stopped by user."
                )
                return
            
            # Update activity to prevent sleep
            update_activity()
            
            # Update progress
            progress_text = (
                f"📥 **Downloading File {current}/{total_files}**\n"
                f"📊 Progress: {create_progress_bar(current, total_files)}\n"
                f"⏳ Status: Downloading {file_info['name']}...\n"
                f"✅ Successful: {success_count}\n"
                f"❌ Failed: {failed_count}"
            )
            
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=progress_msg.message_id,
                text=progress_text
            )
            
            # Download the file
            file_data = download_file_with_selenium(file_info['url'], file_info['name'])
            
            if file_data:
                try:
                    # Send file based on type
                    if file_info['type'] == 'pdf':
                        await update.message.reply_document(
                            document=file_data,
                            filename=file_info['name'],
                            caption=f"📄 {file_info['name']}"
                        )
                    elif file_info['type'] == 'image':
                        await update.message.reply_photo(
                            photo=file_data,
                            caption=f"🖼️ {file_info['name']}"
                        )
                    else:
                        await update.message.reply_document(
                            document=file_data,
                            filename=file_info['name'],
                            caption=f"📎 {file_info['name']}"
                        )
                    
                    success_count += 1
                    logger.info(f"✅ Successfully downloaded: {file_info['name']}")
                    
                except Exception as e:
                    logger.error(f"Failed to send {file_info['name']}: {e}")
                    failed_count += 1
            else:
                failed_count += 1
                logger.warning(f"❌ Failed to download: {file_info['name']}")
            
            # Random delay between 2-8 seconds
            delay = random.uniform(2, 8)
            time.sleep(delay)
        
        # Final summary
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=progress_msg.message_id,
            text=f"✅ **Download Complete!**\n"
                 f"📊 Total: {total_files} files\n"
                 f"✅ Successful: {success_count}\n"
                 f"❌ Failed: {failed_count}"
        )
        
        # Cleanup
        if user_id in user_files:
            del user_files[user_id]
        if user_id in user_processing:
            del user_processing[user_id]
        if user_id in user_progress_messages:
            del user_progress_messages[user_id]
            
    except Exception as e:
        logger.error(f"Error in file processing: {e}")
        await update.message.reply_text(f"❌ Processing error: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    update_activity()
    await update.message.reply_text(
        "🤖 **PDF & Image Downloader Bot**\n\n"
        "Send me a text file with file links and I'll download them for you!\n\n"
        "**Supported Formats:**\n"
        "• PDF documents (.pdf)\n"
        "• Images (.jpg, .jpeg, .png)\n"
        "• Documents (.doc, .docx, .txt)\n\n"
        "**File Format Example:**\n"
        "```\n"
        "1. filename.pdf\n"
        "https://example.com/file.pdf\n"
        "2. image.jpg\n"
        "https://example.com/image.jpg\n"
        "```\n\n"
        "**Commands:**\n"
        "/start - Show this message\n"
        "/download - Start downloading files\n"
        "/stop - Stop current download\n"
        "/wake - Keep the bot awake"
    )

async def handle_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start downloading files"""
    update_activity()
    user_id = update.effective_user.id
    
    if user_id not in user_files or not user_files[user_id]:
        await update.message.reply_text("❌ Please send a text file first, then use /download")
        return
    
    # Check if already processing
    if user_id in user_processing and user_processing[user_id]:
        await update.message.reply_text("⚠️ Download is already running. Use /stop to cancel.")
        return
    
    # Start processing
    user_processing[user_id] = True
    
    # Run processing in background
    asyncio.create_task(process_files_sequentially(update, context, user_id))

async def handle_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop current download"""
    update_activity()
    user_id = update.effective_user.id
    
    if user_id in user_processing and user_processing[user_id]:
        user_processing[user_id] = False
        await update.message.reply_text("🛑 Download stopped.")
        
        # Cleanup progress message
        if user_id in user_progress_messages:
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=user_progress_messages[user_id]
                )
            except:
                pass
            del user_progress_messages[user_id]
    else:
        await update.message.reply_text("❌ No active download to stop.")

async def handle_wake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual wake command"""
    update_activity()
    keep_alive_ping()
    await update.message.reply_text("🔔 Bot is awake and active!")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document upload"""
    update_activity()
    try:
        document = update.message.document
        user_id = update.effective_user.id
        
        # Check if it's a text file
        if not document.mime_type == 'text/plain' and not document.file_name.endswith('.txt'):
            await update.message.reply_text("❌ Please send a text file (.txt)")
            return
        
        # Stop any existing processing
        if user_id in user_processing and user_processing[user_id]:
            user_processing[user_id] = False
            await update.message.reply_text("🛑 Stopped previous download for new file.")
        
        # Download the file
        file = await context.bot.get_file(document.file_id)
        file_content = await file.download_as_bytearray()
        content = file_content.decode('utf-8')
        
        # Parse and count files
        file_links = parse_file_links(content)
        total_files = len(file_links)
        
        if total_files == 0:
            await update.message.reply_text("❌ No downloadable files found in the text file!")
            return
        
        # Store file content
        user_files[user_id] = content
        
        # Show file summary
        file_types = {}
        for file_info in file_links:
            file_type = file_info['type']
            file_types[file_type] = file_types.get(file_type, 0) + 1
        
        type_summary = "\n".join([f"• {count} {typ.upper()} files" for typ, count in file_types.items()])
        
        await update.message.reply_text(
            f"📁 **File Received: {document.file_name}**\n"
            f"📊 Found {total_files} downloadable files:\n"
            f"{type_summary}\n\n"
            f"**Commands:**\n"
            f"/download - Start downloading\n"
            f"/stop - Stop download\n"
            f"/wake - Keep bot awake\n\n"
            f"💡 **Features:**\n"
            f"• One-by-one file download\n"
            f"• Visual progress bar\n"
            f"• Automatic file type detection\n"
            f"• Keep-alive to prevent sleeping"
        )
        
    except Exception as e:
        logger.error(f"Error handling document: {e}")
        await update.message.reply_text(f"❌ Error reading file: {str(e)}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    error = context.error
    if "terminated by other getUpdates request" in str(error):
        logger.warning("Another bot instance is running. This is normal during deployment.")
        return
    logger.error(f"Update {update} caused error {error}")

def main():
    """Start the bot"""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set!")
        return
    
    # Start health server in a separate thread
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    
    # Start keep-alive worker if RENDER_APP_URL is set
    if RENDER_APP_URL:
        keep_alive_thread = threading.Thread(target=keep_alive_worker, daemon=True)
        keep_alive_thread.start()
        logger.info("Keep-alive worker started")
    else:
        logger.warning("RENDER_APP_URL not set - keep-alive disabled")
    
    # Create temporary directory for downloads
    os.makedirs('/tmp', exist_ok=True)
    
    # Create and configure bot application
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("download", handle_download))
    application.add_handler(CommandHandler("stop", handle_stop))
    application.add_handler(CommandHandler("wake", handle_wake))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_error_handler(error_handler)
    
    logger.info("PDF Downloader Bot is starting...")
    
    # Start the bot
    try:
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            close_loop=False
        )
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        time.sleep(10)
        logger.info("Retrying to start bot...")
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            close_loop=False
        )

if __name__ == '__main__':
    main()
