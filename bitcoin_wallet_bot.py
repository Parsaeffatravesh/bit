import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import asyncio
import aiohttp
import concurrent.futures
from mnemonic import Mnemonic
import hashlib
import time
import os
import sys
import atexit
import json
import threading
import logging
import requests
from telebot import apihelper
from bitcoinlib.wallets import HDWallet, Wallet
from bitcoinlib.keys import Key

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Telegram bot token
BOT_TOKEN = "8279506690:AAGmXaCTiGgMFugClLxfhvChoLM4FkPXfYE"

# Password for authentication
PASSWORD = "1590320"

# Lock file to prevent multiple instances
LOCK_FILE = "bot.lock"

# Files for caching checked addresses and active wallets
CHECKED_ADDRESSES_FILE = "checked_addresses.json"
ACTIVE_WALLETS_FILE = "active_wallets.json"

# Proxy settings (optional, uncomment and configure if needed)
# apihelper.proxy = {'https': 'http://user:password@proxy_address:port'}

def acquire_lock():
    if os.path.exists(LOCK_FILE):
        logger.error("Another instance of the bot is already running!")
        sys.exit(1)
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))
    atexit.register(release_lock)

def release_lock():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)

def load_checked_addresses():
    if os.path.exists(CHECKED_ADDRESSES_FILE):
        with open(CHECKED_ADDRESSES_FILE, 'r') as f:
            return set(json.load(f))
    return set()

def save_checked_addresses(addresses):
    with open(CHECKED_ADDRESSES_FILE, 'w') as f:
        json.dump(list(addresses), f)

def load_active_wallets():
    if os.path.exists(ACTIVE_WALLETS_FILE):
        with open(ACTIVE_WALLETS_FILE, 'r') as f:
            return json.load(f)
    return []

def save_active_wallet(wallet_info):
    active_wallets = load_active_wallets()
    active_wallets.append(wallet_info)
    with open(ACTIVE_WALLETS_FILE, 'w') as f:
        json.dump(active_wallets, f, indent=2)

# Acquire lock at startup
acquire_lock()

# Initialize bot with retry mechanism
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# Global variables for control (per chat_id for multi-user support)
processes = {}  # chat_id: {'running': False, 'stop_flag': False, 'status_message_id': None, 'thread': None, 'authenticated': False, 'found_count': 0, 'counter': 0}

class BitcoinWalletGenerator:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.max_workers = 5  # Set to 5 workers for simultaneous wallet generation
        self.checked_addresses = load_checked_addresses()
        self.found_count = processes[chat_id]['found_count']
        self.emojis = ["🔄", "⏳", "⚙️", "▶️"]  # Dynamic emojis for status

    def generate_wallet(self):
        mnemo = Mnemonic("english")  # BIP-39 wordlist for English
        seed_phrase = mnemo.generate(strength=128)  # 12-word seed phrase (128 bits entropy)
        try:
            seed = mnemo.to_seed(seed_phrase, passphrase="")  # Convert to seed with optional passphrase
            # Create an HD wallet with a unique name
            wallet_name = f"hd_wallet_{int(time.time())}"
            # Initialize HDWallet with BIP-39 seed and network
            hd_wallet = HDWallet.create(wallet_name, network='bitcoin', key_type='bip32', seed=seed)
            # Derive the first address using BIP-44 path
            key = hd_wallet.get_key(path="m/44'/0'/0'/0/0")
            address = key.address()
            return seed_phrase, address
        except Exception as e:
            logger.error(f"Error generating wallet: {e}")
            return None, None

    async def has_transactions(self, session, address):
        try:
            url = f"https://api.blockcypher.com/v1/btc/main/addrs/{address}"
            async with session.get(url, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('n_tx', 0) > 0
                return False
        except Exception as e:
            logger.error(f"Transaction check error for {address}: {e}")
            return False

    async def check_btc_balance(self, session, address):
        try:
            url = f"https://api.blockcypher.com/v1/btc/main/addrs/{address}/balance"
            async with session.get(url, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    balance = float(data.get('balance', 0)) / 10**8  # Convert satoshis to BTC
                    if balance > 0:
                        return {'balance': balance, 'tokenName': 'BTC'}
                return None
        except Exception as e:
            logger.error(f"API Error for {address}: {e}")
            return None

    async def process_wallet(self, seed_phrase, address, session):
        if address in self.checked_addresses:
            return False  # Skip if address is already checked
        self.checked_addresses.add(address)
        self.save_checked_addresses()  # Save after adding
        has_tx = await self.has_transactions(session, address)
        if has_tx:
            balance_info = await self.check_btc_balance(session, address)
            if balance_info:
                wallet_info = {
                    'seed_phrase': seed_phrase,
                    'address': address,
                    'btc_balance': balance_info['balance'],
                    'transactions': await self.has_transactions(session, address),
                    'timestamp': time.strftime("%Y-%m-%d %H:%M:%S")
                }
                save_active_wallet(wallet_info)
                self.found_count += 1
                processes[self.chat_id]['found_count'] = self.found_count
                found_info = f"FOUND!\nSeed Phrase: {seed_phrase}\nAddress: {address}\nBTC Balance: {balance_info['balance']}"
                try:
                    bot.send_message(self.chat_id, found_info)
                except Exception as e:
                    logger.error(f"Failed to send message to Telegram: {e}")
                return True  # Indicate a wallet with balance was found
        return False

    async def generate_wallets_async(self):
        async with aiohttp.ClientSession() as session:
            counter = processes[self.chat_id].get('counter', 0)
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                while processes[self.chat_id]['running'] and not processes[self.chat_id]['stop_flag']:
                    futures = [executor.submit(self.generate_wallet) for _ in range(self.max_workers)]
                    tasks = []
                    for future in concurrent.futures.as_completed(futures):
                        if processes[self.chat_id]['stop_flag']:
                            break
                        seed_phrase, address = future.result()
                        if seed_phrase and address:
                            tasks.append(self.process_wallet(seed_phrase, address, session))
                    if tasks:
                        await asyncio.gather(*tasks)
                    counter += self.max_workers
                    processes[self.chat_id]['counter'] = counter
                    emoji = self.emojis[int(time.time() % len(self.emojis))]
                    self.update_status(f"Running... Checked {counter} wallets {emoji}\nActive wallets found: {self.found_count}")
                    await asyncio.sleep(0.1)  # Reduced delay for faster processing
            emoji = self.emojis[int(time.time() % len(self.emojis))]
            self.update_status(f"Process stopped. 🛑 | Active wallets found: {self.found_count}")

    def generate_wallets(self):
        try:
            asyncio.run(self.generate_wallets_async())
        except Exception as e:
            logger.error(f"Error in generate_wallets: {e}")

    def update_status(self, text):
        markup = self.get_keyboard()
        try:
            bot.edit_message_text(text, chat_id=self.chat_id, message_id=processes[self.chat_id]['status_message_id'], reply_markup=markup)
        except Exception as e:
            logger.error(f"Failed to update status: {e}")

    def get_keyboard(self):
        markup = InlineKeyboardMarkup()
        if processes[self.chat_id]['running']:
            markup.add(InlineKeyboardButton("استاپ", callback_data="stop"))
        else:
            markup.add(InlineKeyboardButton("استارت", callback_data="start"))
        return markup

@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    if chat_id not in processes:
        processes[chat_id] = {'running': False, 'stop_flag': False, 'status_message_id': None, 'thread': None, 'authenticated': False, 'found_count': 0, 'counter': 0}
    
    bot.send_message(chat_id, "لطفاً رمز عبور را وارد کنید:")
    bot.register_next_step_handler(message, check_password)

def check_password(message):
    chat_id = message.chat.id
    if message.text == PASSWORD:
        processes[chat_id]['authenticated'] = True
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("استارت", callback_data="start"))
        try:
            sent = bot.send_message(chat_id, "رمز درست است! برای شروع روی دکمه کلیک کنید. 🚀", reply_markup=markup)
            processes[chat_id]['status_message_id'] = sent.message_id
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
    else:
        bot.send_message(chat_id, "رمز اشتباه است! دوباره تلاش کنید:")
        bot.register_next_step_handler(message, check_password)

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    chat_id = call.message.chat.id
    if chat_id not in processes or not processes[chat_id]['authenticated']:
        bot.answer_callback_query(call.id, "ابتدا باید رمز عبور را وارد کنید!")
        return

    generator = BitcoinWalletGenerator(chat_id)

    if call.data == "start":
        if not processes[chat_id]['running']:
            processes[chat_id]['running'] = True
            processes[chat_id]['stop_flag'] = False
            processes[chat_id]['thread'] = threading.Thread(target=generator.generate_wallets)
            processes[chat_id]['thread'].daemon = True
            processes[chat_id]['thread'].start()
            emoji = generator.emojis[int(time.time() % len(generator.emojis))]
            generator.update_status(f"Starting... ▶️ {emoji} | Active wallets found: 0")
    elif call.data == "stop":
        if processes[chat_id]['running']:
            processes[chat_id]['stop_flag'] = True
            processes[chat_id]['running'] = False
            emoji = generator.emojis[int(time.time() % len(generator.emojis))]
            generator.update_status(f"Stopping... ⏹️ {emoji} | Active wallets found: {processes[chat_id]['found_count']}")

def run_polling():
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)  # Wait before retrying

if __name__ == "__main__":
    # Run polling in a separate thread to handle restarts
    polling_thread = threading.Thread(target=run_polling)
    polling_thread.daemon = True
    polling_thread.start()
    # Keep the main thread alive
    while True:
        time.sleep(1)