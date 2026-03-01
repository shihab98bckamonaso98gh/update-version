import requests, time, re, random, os, json, logging, threading, pyotp, string
from datetime import datetime, timedelta
from html import escape
from collections import defaultdict
from dotenv import load_dotenv
from faker import Faker
from concurrent.futures import ThreadPoolExecutor

# Suppress all terminal output
logging.getLogger().setLevel(logging.CRITICAL)
for logger_name in ['urllib3', 'requests', 'faker', 'pyotp']:
    logging.getLogger(logger_name).setLevel(logging.CRITICAL)

load_dotenv()

# Environment validation
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GROUP_ID = os.getenv('GROUP_ID')
SUPPORT_USERNAME = os.getenv('SUPPORT_USERNAME', '@shihab98bc')
TIMEOUT_SECONDS = int(os.getenv('TIMEOUT_SECONDS', 300))

# Provider-specific credentials
STEX_EMAIL = os.getenv('STEX_EMAIL')
STEX_PASSWORD = os.getenv('STEX_PASSWORD')
MNIT_EMAIL = os.getenv('MNIT_EMAIL')
MNIT_PASSWORD = os.getenv('MNIT_PASSWORD')

if not TELEGRAM_TOKEN:
    raise EnvironmentError('TELEGRAM_TOKEN is required')

if not STEX_EMAIL or not STEX_PASSWORD:
    raise EnvironmentError('STEX_EMAIL and STEX_PASSWORD are required')

if not MNIT_EMAIL or not MNIT_PASSWORD:
    raise EnvironmentError('MNIT_EMAIL and MNIT_PASSWORD are required')

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Fetch bot username for deep links
try:
    bot_info = requests.get(f"{TG_API}/getMe").json()
    BOT_USERNAME = bot_info['result']['username']
except Exception:
    BOT_USERNAME = None  # fallback, button may not work

# Thread-safe structures
bot_sessions = {'stexsms': None, 'mnitnetwork': None}
sessions_lock = threading.RLock()
user_states = {}
states_lock = threading.RLock()
user_last_request = defaultdict(float)
user_latest_range = {}          # store per user latest manual range
user_latest_provider = {}       # store provider for that manual range
RATE_LIMIT_SECONDS = 15

# Thread pool for concurrent operations
executor = ThreadPoolExecutor(max_workers=10)

# Initialize Faker
fake = Faker('en_US')

# Pre-compile regex patterns (already optimized)
OTP_PATTERN = re.compile(
    r'(?:<#>)\s*(\d{4,8})|'
    r'(?:code|otp|pin|verification)[:\s]+(\d{4,8})|'
    r'(\d{4,8})\s+is\s+your|'
    r'([A-Z]{2,3}-\d+)|'
    r'\b(\d{4,6})\b',
    re.IGNORECASE
)

def clean_number(number):
    """Remove + and whitespace from number."""
    if number:
        return number.lstrip('+').strip()
    return number

def generate_strong_password():
    """Generate strong password 10-12 chars with special chars and today's date"""
    special_chars = "!@#$%^&*"
    chars = string.ascii_letters + string.digits + special_chars
    password_length = random.randint(10, 12)
    password = ''.join(random.choice(chars) for _ in range(password_length))
    
    # Get Bangladesh date and add at the end
    bdt_time = datetime.now() + timedelta(hours=6)
    password += str(bdt_time.day)
    
    return password

def generate_identity(gender):
    """Generate random USA identity with name, username and password"""
    if gender == 'male':
        first_name = fake.first_name_male()
        last_name = fake.last_name()
        emoji = '👨'
    else:
        first_name = fake.first_name_female()
        last_name = fake.last_name()
        emoji = '👩'
    
    full_name = f"{first_name} {last_name}"
    username = f"{first_name.lower()}{last_name.lower()}{random.randint(10,99)}"
    password = generate_strong_password()
    
    return emoji, full_name, username, password

class StexSMS:
    def __init__(self, provider, email, password):
        self.provider = provider
        self.email = email
        self.password = password
        
        if provider == 'mnitnetwork':
            self.base = 'https://x.mnitnetwork.com'
            self.use_headers = True
        else:
            self.base = 'https://stexsms.com'
            self.use_headers = False
        
        # Configure session with connection pooling (already optimized)
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=20,
            pool_maxsize=20,
            max_retries=2,
            pool_block=False
        )
        self.session.mount('https://', adapter)
        
        self.token = None
        self.token_time = None
        self.TOKEN_TTL = 3600
        self._lock = threading.RLock()
        self._range_cache = {'data': None, 'timestamp': 0}
    
    def _headers(self):
        h = {'Mauthtoken': self.token}
        if self.use_headers:
            h['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            h['Content-Type'] = 'application/json'
            h['Accept-Encoding'] = 'gzip, deflate'
            h['Connection'] = 'keep-alive'
        return h
    
    def ensure_auth(self):
        with self._lock:
            if self.token is None or time.time() - self.token_time > self.TOKEN_TTL:
                self.login()
    
    def login(self):
        url = f"{self.base}/mapi/v1/mauth/login"
        payload = {'email': self.email, 'password': self.password}
        headers = {'User-Agent': 'Mozilla/5.0'} if self.use_headers else None
        
        response = self.session.post(url, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        self.token = (data.get('token') or 
                     data.get('access_token') or 
                     data.get('data', {}).get('token') or 
                     self.session.cookies.get('mauthtoken'))
        
        if not self.token:
            raise RuntimeError('Failed to get token')
        
        self.token_time = time.time()
    
    def _request(self, method, url, **kwargs):
        """Unified request method with automatic retry"""
        self.ensure_auth()
        kwargs.setdefault('headers', self._headers())
        kwargs.setdefault('timeout', 25)
        
        for attempt in range(2):
            response = self.session.request(method, url, **kwargs)
            
            if response.status_code == 200:
                return response
            elif response.status_code == 401 and attempt == 0:
                with self._lock:
                    self.token = None
                    self.token_time = None
                self.ensure_auth()
                kwargs['headers'] = self._headers()
                continue
            elif response.status_code == 429:
                time.sleep(2)
                continue
            
            response.raise_for_status()
        
        return response
    
    def get_random_range(self):
        """Get a random XXX range from console info with caching"""
        now = time.time()
        
        # Cache for 5 minutes (reduces API calls)
        if self._range_cache['data'] and now - self._range_cache['timestamp'] < 300:
            return self._range_cache['data']
        
        response = self._request('GET', f"{self.base}/mapi/v1/mdashboard/console/info")
        logs = response.json().get('data', {}).get('logs', [])
        ranges = [log['number'] for log in logs if 'XXX' in log.get('number', '')]
        
        if not ranges:
            raise RuntimeError('No XXX ranges available')
        
        chosen = random.choice(ranges)
        self._range_cache = {'data': chosen, 'timestamp': now}
        return chosen
    
    def get_number_with_range(self, phone_range):
        """Get a number from a specific range."""
        response = self._request('POST', f"{self.base}/mapi/v1/mdashboard/getnum/number", 
                                json={'range': phone_range})
        raw = response.json()['data']['number']
        return clean_number(raw)
    
    def get_number(self):
        """Get a number from a random range."""
        return self.get_number_with_range(self.get_random_range())
    
    def get_numbers_info(self, search=''):
        """Fetch numbers info from API with optimized filtering"""
        params = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'page': 1,
            'search': '',
            'status': ''
        }
        
        response = self._request('GET', f"{self.base}/mapi/v1/mdashboard/getnum/info", params=params)
        numbers = response.json().get('data', {}).get('numbers', [])
        
        if search and isinstance(numbers, list):
            search_clean = clean_number(search)
            return [n for n in numbers if clean_number(n.get('number', '')) == search_clean]
        
        return numbers if isinstance(numbers, list) else []
    
    def extract_otp(self, text):
        """Extract OTP from message text."""
        if not text:
            return None
        
        match = OTP_PATTERN.search(text)
        if match:
            for group in match.groups():
                if group:
                    return group
        return None
    
    def wait_for_message(self, number, timeout=TIMEOUT_SECONDS):
        """
        Wait for a message on a specific number with aggressive adaptive polling.
        Poll intervals: start at 2s, then after 30s -> 3s, after 60s -> 4s, after 120s -> 5s.
        """
        number = clean_number(number)
        start = time.time()
        poll_interval = 2          # Start with 2 seconds for faster response
        empty_success_count = 0
        poll_count = 0
        
        while time.time() - start < timeout:
            poll_count += 1
            elapsed = int(time.time() - start)
            
            try:
                numbers = self.get_numbers_info(search=number)
                
                for n in numbers:
                    n_clean = clean_number(n.get('number', ''))
                    if n_clean != number:
                        continue
                    
                    status = n.get('status', '')
                    msg = n.get('message') or n.get('otp') or ''
                    
                    if status == 'failed':
                        return None, None
                    
                    if status == 'success':
                        if msg:
                            otp = self.extract_otp(msg)
                            return msg, otp
                        else:
                            empty_success_count += 1
                            if empty_success_count > 15:
                                return None, None
                    
                    break
                
                # Aggressive adaptive polling: increase interval gradually
                if elapsed > 30:
                    poll_interval = 3
                if elapsed > 60:
                    poll_interval = 4
                if elapsed > 120:
                    poll_interval = 5
                    
            except Exception:
                # Ignore errors and continue polling
                pass
            
            time.sleep(poll_interval)
        
        return None, None

def get_bot_instance(provider):
    """Thread-safe bot instance getter with provider-specific credentials"""
    with sessions_lock:
        if bot_sessions[provider] is None:
            if provider == 'stexsms':
                email, password = STEX_EMAIL, STEX_PASSWORD
            elif provider == 'mnitnetwork':
                email, password = MNIT_EMAIL, MNIT_PASSWORD
            else:
                raise ValueError(f"Unknown provider: {provider}")
            
            bot = StexSMS(provider=provider, email=email, password=password)
            bot.login()
            bot_sessions[provider] = bot
        return bot_sessions[provider]

def check_rate_limit(chat_id):
    """Check rate limit for user"""
    now = time.time()
    last = user_last_request[chat_id]
    if now - last < RATE_LIMIT_SECONDS:
        return False, int(RATE_LIMIT_SECONDS - (now - last))
    user_last_request[chat_id] = now
    return True, 0

def validate_range(range_str):
    """Validate range format"""
    if not range_str or len(range_str) > 20:
        return False
    if 'XXX' not in range_str:
        return False
    return bool(re.match('^[\\dX]+$', range_str))

# Keyboard generators
def main_keyboard():
    """Main menu with all core buttons"""
    return {
        'keyboard': [
            [{'text': '📞 Get Number'}, {'text': '🔄 Change Number'}],
            [{'text': '👤 Fake Name'}, {'text': '🔐 Get 2FA'}],
            [{'text': '🆘 Support'}]
        ],
        'resize_keyboard': True
    }

def gender_keyboard():
    return {
        'keyboard': [
            [{'text': '👨 Male'}, {'text': '👩 Female'}],
            [{'text': '⬅️ Back'}]
        ],
        'resize_keyboard': True
    }

def provider_keyboard():
    return {
        'keyboard': [
            [{'text': '🌐 StexSMS'}, {'text': '🌐 MNIT Network'}],
            [{'text': '⬅️ Back'}]
        ],
        'resize_keyboard': True
    }

def range_mode_keyboard():
    return {
        'keyboard': [
            [{'text': '🎲 Random Range'}, {'text': '✏️ Manual Range'}],
            [{'text': '⬅️ Back'}]
        ],
        'resize_keyboard': True
    }

def number_options_keyboard(number):
    return {
        'inline_keyboard': [
            [{'text': 'OTP Group ↗️', 'url': 'https://t.me/otpservers'}]
        ]
    }

def group_message_keyboard():
    """Inline keyboard for group messages - opens bot with main menu"""
    if not BOT_USERNAME:
        return None
    return {
        'inline_keyboard': [
            [{'text': '🚀 Get Number', 'url': f'https://t.me/{BOT_USERNAME}?start=main'}]
        ]
    }

# Message formatters
def format_inbox_message(number, provider, full_message, otp):
    t = datetime.now().strftime('%I:%M %p')
    msg = f"📩 <b>Message Received!</b>\n\n📞 <b>Number:</b> <code>+{number}</code>\n🏢 <b>Provider:</b> <code>{provider.upper()}</code>\n"
    if otp:
        msg += f"🔑 <b>OTP Code:</b> <code>{otp}</code>\n"
    msg += f"\n💬 <b>Full Message:</b>\n<blockquote>{escape(full_message)}</blockquote>\n\n🕒 <b>Time:</b> {t}"
    return msg

def format_timeout_message(number, provider):
    t = datetime.now().strftime('%I:%M %p')
    timeout_minutes = TIMEOUT_SECONDS // 60
    return f"""⏰ <b>Timeout!</b>

📞 <b>Number:</b> <code>+{number}</code>
🏢 <b>Provider:</b> <code>{provider.upper()}</code>

❌ No message received within {timeout_minutes} minutes.

🕒 <b>Time:</b> {t}"""

def format_failed_message(number, provider):
    t = datetime.now().strftime('%I:%M %p')
    return f"""❌ <b>Number Failed!</b>

📞 <b>Number:</b> <code>+{number}</code>
🏢 <b>Provider:</b> <code>{provider.upper()}</code>

This number can't receive SMS. Try again.

🕒 <b>Time:</b> {t}"""

def format_group_message(number, provider, full_message, otp):
    t = datetime.now().strftime('%I:%M %p')
    masked = f"{number[:3]}****{number[-3:]}" if len(number) > 6 else 'Unknown'
    msg = f"✅ <b>New message received!</b>\n\n📞 <b>Number:</b> <code>+{masked}</code>\n🏢 <b>Provider:</b> <code>{provider.upper()}</code>\n"
    if otp:
        msg += f"🔑 <b>OTP:</b> <code>{otp}</code>\n"
    msg += f"\n💬 <b>Message:</b>\n<blockquote>{escape(full_message)}</blockquote>\n\n🕒 <b>Time:</b> {t}"
    return msg

def format_identity_message(gender):
    """Format identity message with tap to copy - FRESH EACH TIME"""
    emoji, full_name, username, password = generate_identity(gender)
    
    msg = f"""{emoji} <b>Generated Identity:</b>

Name : <code>{full_name}</code>
Username : <code>{username}</code>
Password : <code>{password}</code>

<i>Tap on the text above to copy</i>"""
    return msg

def format_2fa_code(secret_key):
    """Generate and format 2FA code"""
    try:
        clean_secret = ''.join(secret_key.split()).upper()
        totp = pyotp.TOTP(clean_secret)
        code = totp.now()
        time_remaining = 30 - (int(time.time()) % 30)
        
        msg = f"""🔐 <b>2FA Authentication Code</b>

Your Code : <code>{code}</code>

⏱ Valid for: <b>{time_remaining} seconds</b>

📌 <b>Note:</b> This code refreshes every 30 seconds.
You can request a new code at any time."""
        return msg, True
    except Exception:
        return "❌ <b>Invalid Secret Key!</b>\n\nPlease check your format and try again.", False

def tg_send(chat_id, text, keyboard=None, parse_mode='HTML'):
    """Send Telegram message"""
    if not chat_id:
        return
    
    data = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': parse_mode
    }
    if keyboard:
        data['reply_markup'] = json.dumps(keyboard)
    
    try:
        requests.post(f"{TG_API}/sendMessage", data=data, timeout=5)
    except Exception:
        pass

def handle_create_number(provider, chat_id, manual_range=None):
    """Handle number creation request"""
    try:
        allowed, remaining = check_rate_limit(chat_id)
        if not allowed:
            tg_send(chat_id, f"⏳ Please wait {remaining}s.", main_keyboard())
            return
        
        bot = get_bot_instance(provider)
        
        if manual_range:
            number = bot.get_number_with_range(manual_range)
            range_info = f"\n📋 <b>Range:</b> <code>{escape(manual_range)}</code>"
            # Store the manual range and provider for this user
            with states_lock:
                user_latest_range[chat_id] = manual_range
                user_latest_provider[chat_id] = provider
        else:
            number = bot.get_number()
            range_info = ''
        
        # Send immediate response
        timeout_minutes = TIMEOUT_SECONDS // 60
        tg_send(chat_id, f"{range_info}\n\n📞 <b>Your number:</b> <code>+{number}</code>\n\n⏳ <b>Waiting for message...</b>\n⏰ Timeout: {timeout_minutes} minutes",
                number_options_keyboard(number))
        
        # Wait for message in background
        def wait_and_send():
            try:
                msg, otp = bot.wait_for_message(number, timeout=TIMEOUT_SECONDS)
                
                if msg:
                    tg_send(chat_id, format_inbox_message(number, provider, msg, otp), main_keyboard())
                    if GROUP_ID:
                        # Send to group with inline button
                        group_keyboard = group_message_keyboard()
                        tg_send(GROUP_ID, format_group_message(number, provider, msg, otp), group_keyboard)
                else:
                    try:
                        nums = bot.get_numbers_info(search=number)
                        status = 'timeout'
                        for n in nums:
                            if clean_number(n.get('number', '')) == number:
                                status = n.get('status', 'timeout')
                                break
                        
                        if status == 'failed':
                            tg_send(chat_id, format_failed_message(number, provider), main_keyboard())
                        else:
                            tg_send(chat_id, format_timeout_message(number, provider), main_keyboard())
                    except Exception:
                        tg_send(chat_id, format_timeout_message(number, provider), main_keyboard())
            except Exception:
                tg_send(chat_id, f"❌ Error: An error occurred", main_keyboard())
        
        threading.Thread(target=wait_and_send, daemon=True, name=f"Wait-{number}").start()
        
    except Exception as e:
        tg_send(chat_id, f"❌ Error: {escape(str(e))}", main_keyboard())

def run_telegram_bot():
    """Run the Telegram bot"""
    offset = 0
    
    while True:
        try:
            response = requests.get(
                f"{TG_API}/getUpdates",
                params={'offset': offset, 'timeout': 30},
                timeout=35
            )
            
            for update in response.json().get('result', []):
                offset = update['update_id'] + 1
                
                # Handle messages
                if 'message' in update:
                    text = update['message'].get('text', '').strip()
                    chat_id = update['message']['chat']['id']
                    
                    with states_lock:
                        state = user_states.get(chat_id)
                    
                    # State handling
                    if state:
                        if state.get('step') == 'awaiting_range':
                            if text == '⬅️ Back':
                                with states_lock:
                                    user_states.pop(chat_id, None)
                                tg_send(chat_id, 'Select provider:', provider_keyboard())
                                continue
                            
                            if not validate_range(text):
                                tg_send(chat_id, '❌ Invalid range!\n\nMust contain <b>XXX</b> and only digits/X.\nExample: <code>2250163333XXX</code>')
                                continue
                            
                            prov = state['provider']
                            with states_lock:
                                user_states.pop(chat_id, None)
                            tg_send(chat_id, f"🔍 Getting number from: <code>{escape(text)}</code>...")
                            handle_create_number(prov, chat_id, manual_range=text)
                            continue
                        
                        elif state.get('step') == 'choose_range_mode':
                            prov = state['provider']
                            
                            if text == '🎲 Random Range':
                                with states_lock:
                                    user_states.pop(chat_id, None)
                                handle_create_number(prov, chat_id)
                                continue
                            
                            elif text == '✏️ Manual Range':
                                with states_lock:
                                    user_states[chat_id] = {'step': 'awaiting_range', 'provider': prov}
                                
                                # Build manual range prompt with user's latest range if available
                                prompt = '✏️ <b>Enter the range:</b>\n\n📝 Example: <code>2250163333XXX</code>\n📝 Example: <code>67077267XXX</code>\n\n⚠️ Must contain <b>XXX</b>'
                                with states_lock:
                                    latest = user_latest_range.get(chat_id)
                                if latest:
                                    prompt += f'\n\n📝 <b>Latest Range:</b> <code>{escape(latest)}</code>'
                                
                                tg_send(chat_id, prompt,
                                       {'keyboard': [[{'text': '⬅️ Back'}]], 'resize_keyboard': True})
                                continue
                            
                            elif text == '⬅️ Back':
                                with states_lock:
                                    user_states.pop(chat_id, None)
                                tg_send(chat_id, 'Select provider:', provider_keyboard())
                                continue
                        
                        elif state.get('step') == 'awaiting_gender':
                            if text == '⬅️ Back':
                                with states_lock:
                                    user_states.pop(chat_id, None)
                                tg_send(chat_id, 'Welcome! Choose an option:', main_keyboard())
                                continue
                            
                            elif text in ['👨 Male', '👩 Female']:
                                gender = 'male' if 'Male' in text else 'female'
                                with states_lock:
                                    user_states.pop(chat_id, None)
                                tg_send(chat_id, format_identity_message(gender), main_keyboard())
                                continue
                        
                        elif state.get('step') == 'awaiting_2fa_secret':
                            if text == '⬅️ Back':
                                with states_lock:
                                    user_states.pop(chat_id, None)
                                tg_send(chat_id, 'Welcome! Choose an option:', main_keyboard())
                                continue
                            else:
                                with states_lock:
                                    user_states.pop(chat_id, None)
                                msg, success = format_2fa_code(text)
                                tg_send(chat_id, msg, main_keyboard())
                                continue
                    
                    # Handle /start command with or without payload
                    if text.startswith('/start'):
                        parts = text.split()
                        command = parts[0]
                        payload = parts[1] if len(parts) > 1 else None
                        
                        with states_lock:
                            user_states.pop(chat_id, None)  # Clear any ongoing state
                        
                        if payload == 'getnumber':
                            # Directly go to provider selection (same as pressing "📞 Get Number")
                            tg_send(chat_id, 'Select provider:', provider_keyboard())
                        else:
                            # Any other payload (including 'main') shows main menu
                            tg_send(chat_id, 'Welcome! Choose an option:', main_keyboard())
                        continue
                    
                    # Main menu buttons (except /start handled above)
                    if text == '⬅️ Back':
                        with states_lock:
                            user_states.pop(chat_id, None)
                        tg_send(chat_id, 'Welcome! Choose an option:', main_keyboard())
                    
                    elif text == '📞 Get Number':
                        tg_send(chat_id, 'Select provider:', provider_keyboard())
                    
                    elif text == '🔄 Change Number':
                        with states_lock:
                            latest_range = user_latest_range.get(chat_id)
                            latest_provider = user_latest_provider.get(chat_id)
                        
                        if latest_range and latest_provider:
                            tg_send(chat_id, f"🔄 Fetching new number from range: <code>{escape(latest_range)}</code>...")
                            handle_create_number(latest_provider, chat_id, manual_range=latest_range)
                        else:
                            tg_send(chat_id, 
                                   "❌ No manual range found.\n\nPlease use <b>📞 Get Number</b> with <b>✏️ Manual Range</b> first.",
                                   main_keyboard())
                        continue
                    
                    elif text == '🌐 StexSMS':
                        with states_lock:
                            user_states[chat_id] = {'step': 'choose_range_mode', 'provider': 'stexsms'}
                        tg_send(chat_id, '🔧 <b>Choose range mode:</b>', range_mode_keyboard())
                    
                    elif text == '🌐 MNIT Network':
                        with states_lock:
                            user_states[chat_id] = {'step': 'choose_range_mode', 'provider': 'mnitnetwork'}
                        tg_send(chat_id, '🔧 <b>Choose range mode:</b>', range_mode_keyboard())
                    
                    elif text == '👤 Fake Name':
                        with states_lock:
                            user_states[chat_id] = {'step': 'awaiting_gender'}
                        tg_send(chat_id, '👤 <b>Select Gender:</b>', gender_keyboard())
                    
                    elif text == '🔐 Get 2FA':
                        with states_lock:
                            user_states[chat_id] = {'step': 'awaiting_2fa_secret'}
                        instruction = """📲 <b>পেস্ট করুন আপনার 2FA Secret Key</b>

<code>ABCD EFGH IJKL MNOP QRS2 TUV7</code>
<i>(Copy the format above)</i>

📌 <b>নিবন্ধন:</b>
• শুধুমাত্র A-Z এবং 2-7 ব্যবহার করুন
• স্পেস দেওয়া যাবে বা না দেওয়াও যাবে

<i>Example: JBSW Y3DP FH5Q VKBF H3TE 2SYW</i>"""
                        tg_send(chat_id, instruction, {'keyboard': [[{'text': '⬅️ Back'}]], 'resize_keyboard': True})
                    
                    elif text == '🆘 Support':
                        support_msg = f"""🆘 <b>Support Information</b>

For any assistance, please contact:
{SUPPORT_USERNAME}

<i>We're here to help you 24/7!</i>"""
                        tg_send(chat_id, support_msg, main_keyboard())
                
                # Handle callback queries
                elif 'callback_query' in update:
                    cq = update['callback_query']
                    cq_chat = cq['message']['chat']['id']
                    
                    if cq['data'] == 'go_back':
                        tg_send(cq_chat, 'Main menu:', main_keyboard())
                    
                    try:
                        requests.post(f"{TG_API}/answerCallbackQuery", 
                                    data={'callback_query_id': cq['id'], 'text': 'OK'}, 
                                    timeout=5)
                    except Exception:
                        pass
        
        except requests.exceptions.Timeout:
            continue
        except requests.exceptions.ConnectionError:
            time.sleep(2)
        except Exception:
            time.sleep(2)

if __name__ == '__main__':
    run_telegram_bot()