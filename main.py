from flask import Flask, render_template_string, request, redirect, url_for, flash, session, jsonify, get_flashed_messages
import psycopg2
from psycopg2 import sql
from werkzeug.security import generate_password_hash, check_password_hash
from hashlib import pbkdf2_hmac
import binascii
import hashlib
import logging
import os
import math
import requests
from dotenv import load_dotenv
from datetime import timedelta, datetime
from psycopg2.extras import DictCursor
import boto3
from botocore.exceptions import ClientError
from werkzeug.utils import secure_filename
from geopy.distance import geodesic
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from flask import send_from_directory

# Use session ID if available, else fallback to IP address
def rate_limit_key():
    return session.get("user_id") or get_remote_address()
    
# Load environment variables from .env
load_dotenv()

app = Flask(__name__)
csrf = CSRFProtect(app)

# Initialize Limiter
limiter = Limiter(
    app=app,
    key_func=rate_limit_key,
    default_limits=["200 per day", "50 per hour"]
)

# Configuring Environment Variables
app.secret_key = os.getenv('SECRET_KEY')
CASH_DATABASE = os.getenv('CASH_DATABASE')
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME')
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
MPESA_CONSUMER_KEY = os.getenv('MPESA_CONSUMER_KEY')
MPESA_CONSUMER_SECRET = os.getenv('MPESA_CONSUMER_SECRET')
MPESA_SHORTCODE = os.getenv('MPESA_SHORTCODE')
MPESA_PASSKEY = os.getenv('MPESA_PASSKEY')

# Remove Cloudinary imports and add S3 client
s3_client = boto3.client(
    's3',
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
    region_name=os.getenv('AWS_REGION', 'us-east-1')
)
S3_BUCKET = os.getenv('AWS_S3_BUCKET_NAME')

# Validate critical environment variables
required_vars = {
    'SECRET_KEY': app.secret_key,
    'CASH_DATABASE': CASH_DATABASE,
    'ADMIN_USERNAME': ADMIN_USERNAME, 
    'ADMIN_EMAIL': ADMIN_EMAIL,
    'ADMIN_PASSWORD': ADMIN_PASSWORD,
    'MPESA_CONSUMER_KEY': MPESA_CONSUMER_KEY,
    'MPESA_CONSUMER_SECRET': MPESA_CONSUMER_SECRET,
    'MPESA_SHORTCODE': MPESA_SHORTCODE,
    'MPESA_PASSKEY': MPESA_PASSKEY
}

missing_vars = [var for var, value in required_vars.items() if not value]

if missing_vars:
    raise RuntimeError(f"Environment variables {', '.join(missing_vars)} must be set.")    

# M-Pesa Functions
def get_mpesa_access_token():
    """Get M-Pesa access token"""
    try:
        response = requests.get(
            'https://api.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials',
            auth=(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET),
            timeout=30
        )
        response.raise_for_status()
        json_response = response.json()
        return json_response.get('access_token')
    except Exception as e:
        logging.error(f"Error getting M-Pesa access token: {e}")
        return None

def get_mpesa_password():
    """Generate M-Pesa password"""
    return get_password()

def get_timestamp():
    """Get current timestamp for M-Pesa"""
    return datetime.now().strftime('%Y%m%d%H%M%S')

def lipa_na_mpesa(phone_number, amount):
    """Initiate M-Pesa payment"""
    access_token = get_mpesa_access_token()
    if not access_token:
        return {'error': 'Access token generation failed'}

    api_url = os.getenv('MPESA_LIPA_ONLINE_URL', 'https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest')
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    payload = {
        "BusinessShortCode": os.getenv('MPESA_SHORTCODE', '4487938'),
        "Password": get_mpesa_password(),
        "Timestamp": get_timestamp(),
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": phone_number,
        "PartyB": os.getenv('MPESA_PARTY_B', '4487938'),
        "PhoneNumber": phone_number,
        "CallBackURL": os.getenv('MPESA_CALLBACK_URL', 'https://ochuna-8a162e92f9ea.herokuapp.com/callback'),
        "AccountReference": os.getenv('MPESA_ACCOUNT_REFERENCE', 'Payment for ochuna portals services'),
        "TransactionDesc": os.getenv('MPESA_TRANSACTION_DESC', 'your_live_transaction_description')
    }

    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            return response.json()
        else:
            logging.error(f"Payment request failed: {response.text}")
            return {'error': 'Payment request failed'}
    except Exception as e:
        logging.error(f"Error in lipa_na_mpesa: {e}")
        return {'error': 'Payment request failed'}

def generate_token():
    """Generate M-Pesa token"""
    return get_mpesa_access_token()
    
def get_media_items():
    """Get media items from database"""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    media_items = []

    try:
        cur.execute('''
            SELECT title, content, content_type, created_at, photo_filename, created_by
            FROM media_content
            ORDER BY created_at DESC
        ''')
        media_items_db = cur.fetchall()

        for item in media_items_db:
            item_dict = dict(item)
            if item_dict['photo_filename']:
                s3_url = get_s3_url(item_dict['photo_filename'])
                item_dict['s3_url'] = s3_url
            else:
                item_dict['s3_url'] = None

            media_items.append(item_dict)

    except Exception as e:
        flash(f"Error fetching media items: {e}", 'danger')
    finally:
        cur.close()
        conn.close()

    return media_items    

# Generate M-Pesa password
def get_password():
    """Generate M-Pesa password using the shortcode, passkey, and timestamp."""
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    data_to_encode = MPESA_SHORTCODE + MPESA_PASSKEY + timestamp
    encoded_string = data_to_encode.encode("utf-8")
    password = binascii.b2a_base64(hashlib.sha256(encoded_string).digest()).decode("utf-8").strip()
    return password
    

def get_db_connection():
    """Get database connection and verify it's using the correct database"""
    try:
        conn = psycopg2.connect(CASH_DATABASE)  # This should be 8 spaces or 1 tab indent from function def
        # Verify we're connected to the right database
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            current_db = cur.fetchone()[0]
            expected_db = ADMIN_DATABASE.split('/')[-1]
            print(f"Connected to database: {current_db}")
            if current_db != expected_db:
                raise Exception(f"Connected to wrong database: {current_db}, expected: {expected_db}")
        return conn
    except psycopg2.OperationalError as e:
        print(f"Error connecting to database: {e}")
        raise

        
# Function to convert alphabetic characters to uppercase
def process_input(input_string):
    return ''.join(char.upper() if char.isalpha() else char for char in input_string)     

def get_user_status(username):
    """Get user status from database"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT status FROM users WHERE username = %s', (username,))
        status = cur.fetchone()
        return status[0] if status else None
    finally:
        cur.close()
        conn.close()

def generate_salt(length=16):
    """Generates a cryptographically secure salt."""
    return binascii.hexlify(os.urandom(length)).decode()

def hash_password(password, salt, iterations=310000):
    """Hashes a password with a given salt using PBKDF2-HMAC-SHA256."""
    if not isinstance(password, str) or not isinstance(salt, str):
        raise ValueError("Password and salt must be strings.")
    if not password:
        raise ValueError("Password cannot be empty.")
    
    # Convert hex salt back to bytes for hashing
    salt_bytes = binascii.unhexlify(salt)
    
    # Hash the password
    password_hash = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt_bytes,
        iterations
    )
    
    return binascii.hexlify(password_hash).decode()

def verify_password(password, stored_hash, salt, iterations=310000):
    """Verifies a password against a stored hash and salt."""
    try:
        # Hash the provided password with the same salt and iterations
        computed_hash = hash_password(password, salt, iterations)
        
        # Constant-time comparison to prevent timing attacks
        return len(stored_hash) == len(computed_hash) and \
               hashlib.sha256(stored_hash.encode()).hexdigest() == \
               hashlib.sha256(computed_hash.encode()).hexdigest()
    
    except (ValueError, binascii.Error):
        return False

def log_transaction(username, action, details=None):
    """Log user transaction"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('INSERT INTO user_transactions (username, action, timestamp, details) VALUES (%s, %s, CURRENT_TIMESTAMP, %s)', 
                    (username, action, details))
        conn.commit()
    except Exception as e:
        print(f"Error logging transaction: {e}")
    finally:
        cur.close()
        conn.close()
        
# Distance calculation function
def haversine(lon1, lat1, lon2, lat2):
    """Calculate distance between two points using Haversine formula"""
    # Convert latitude and longitude from degrees to radians
    lon1, lat1, lon2, lat2 = map(math.radians, [lon1, lat1, lon2, lat2])

    # Haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    # Radius of Earth in kilometers. Use 3956 for miles. Determines return value units.
    R = 6371  
    return R * c        

def upload_to_s3(file, filename):
    """Upload file to S3 and return the URL."""
    try:
        s3_client.upload_fileobj(
            file,
            S3_BUCKET,
            filename,
            ExtraArgs={'ACL': 'public-read'}  # Make file publicly accessible
        )
        # Generate the S3 URL
        s3_url = f"https://{S3_BUCKET}.s3.{os.getenv('AWS_REGION')}.amazonaws.com/{filename}"
        return s3_url
    except Exception as e:
        print(f"Error uploading to S3: {str(e)}")
        return None

def get_s3_url(filename):
    """Generate S3 URL for a filename."""
    if filename:
        return f"https://{S3_BUCKET}.s3.{os.getenv('AWS_REGION')}.amazonaws.com/{filename}"
    return None

def try_auto_unlock():
    """Attempt to auto-unlock user account"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT code, username FROM vouchers WHERE username IS NOT NULL LIMIT 1')
        voucher = cur.fetchone()

        if voucher:
            code, username = voucher

            cur.execute('UPDATE users SET status = %s WHERE username = %s', ('active', username))
            cur.execute('INSERT INTO used_vouchers (code, username) VALUES (%s, %s)', (code, username))
            cur.execute('DELETE FROM vouchers WHERE code = %s', (code,))
            cur.execute('''
                INSERT INTO user_requests (username, request_type, request_count)
                VALUES (%s, %s, 1)
                ON CONFLICT (username, request_type)
                DO UPDATE SET request_count = user_requests.request_count + 1, requested_at = CURRENT_TIMESTAMP;
            ''', (username, 'unlock_account'))
            conn.commit()

            return f'User {username} auto-unlocked successfully.'
        return 'No eligible voucher found.'

    except psycopg2.Error as e:
        return f'Database error during unlock: {e}'
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
            
def check_user_tokens(username, tokens_required=1):
    """Check if user has sufficient tokens"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('SELECT COUNT(*) FROM vouchers WHERE username = %s', (username,))
        token_count = cur.fetchone()[0]
        return token_count >= tokens_required
    finally:
        cur.close()
        conn.close()

def deduct_user_tokens(username, tokens_count=1):
    """Deduct tokens from user account"""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('DELETE FROM vouchers WHERE username = %s LIMIT %s', (username, tokens_count))
        conn.commit()
        return cur.rowcount == tokens_count
    except Exception as e:
        conn.rollback()
        logging.error(f"Error deducting tokens: {e}")
        return False
    finally:
        cur.close()
        conn.close()                                                                                       
                        
def init_db():
    """Initialize database tables"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()                
        # Create the table users
        cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(50) UNIQUE NOT NULL,
            email VARCHAR(100) UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            role VARCHAR(20) NOT NULL DEFAULT 'user',
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        ''')
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mpesa_payments (
                id SERIAL PRIMARY KEY,
                transaction_id TEXT UNIQUE NOT NULL,
                phone TEXT NOT NULL,
                sender_name TEXT,
                amount NUMERIC(10,2),
                message TEXT,
                status TEXT DEFAULT 'unused',
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)        
        
        # Create the user_interactions table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_interactions (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) NOT NULL,
                role VARCHAR(50),
                status VARCHAR(50),
                interaction_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')        
        
        # Table creation for user_requests
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_requests (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) NOT NULL,
                request_type VARCHAR(50) NOT NULL,  -- E.g., 'login', 'password_reset', etc.
                request_count INT DEFAULT 1,        -- Tracks the number of requests by the user
                requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- Timestamp of the latest request
                CONSTRAINT fk_username FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE,
                CONSTRAINT unique_request UNIQUE (username, request_type)  -- Add this unique constraint
            );
        ''')                             
        # Create the service_providers table
        cur.execute('''
        CREATE TABLE IF NOT EXISTS service_providers (
            id SERIAL PRIMARY KEY,                     -- Auto-incrementing primary key
            service_type VARCHAR(100) NOT NULL,        -- Service type (e.g., doctor, electrician)
            phone_number VARCHAR(15) NOT NULL,         -- Phone number (should be unique per service type)
            password VARCHAR(255) NOT NULL,            -- Hashed password
            salt VARCHAR(255) NOT NULL,                -- Salt used for hashing the password
            longitude DECIMAL(9,6) NOT NULL,           -- Longitude of the provider's location
            latitude DECIMAL(9,6) NOT NULL,            -- Latitude of the provider's location
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- Timestamp of when the record was created
            UNIQUE (phone_number, service_type)        -- Ensure unique phone number per service type
        );
        ''')               
        
        # Create the table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_transactions (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                details TEXT
            );
        ''')                 

        cur.execute('''
        CREATE TABLE IF NOT EXISTS roles (
            id SERIAL PRIMARY KEY,
            role_name TEXT UNIQUE NOT NULL
        );
        ''')

        cur.execute('''
        CREATE TABLE IF NOT EXISTS user_roles (
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            role_id INTEGER REFERENCES roles(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, role_id)
        );
        ''')

        cur.execute('''
        CREATE TABLE IF NOT EXISTS calls (
            id SERIAL PRIMARY KEY,
            phone_number VARCHAR(15) NOT NULL,
            service_type VARCHAR(50) NOT NULL,
            latitude DECIMAL(9, 6) NOT NULL,
            longitude DECIMAL(9, 6) NOT NULL,
            call_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        ''')
        
        cur.execute('''
        CREATE TABLE IF NOT EXISTS vouchers (
            code TEXT PRIMARY KEY,
            username TEXT,
            created_at TIMESTAMP NOT NULL,
            created_by TEXT NOT NULL,
            valid_until TIMESTAMP NOT NULL
        );
        ''')

        cur.execute('''
        CREATE TABLE IF NOT EXISTS used_vouchers (
            id SERIAL PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            username TEXT NOT NULL,
            used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        ''')

        cur.execute('''
        CREATE TABLE IF NOT EXISTS login_attempts (
            id SERIAL PRIMARY KEY,
            username TEXT REFERENCES users(username),
            attempt_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            successful BOOLEAN
        );
        ''')
        
        cur.execute('''
        CREATE TABLE IF NOT EXISTS media_content (
            id SERIAL PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            content TEXT,
            content_type VARCHAR(50),
            photo_filename VARCHAR(255),
            created_by VARCHAR(100),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        ''')        

        # Insert default roles
        cur.execute('''
        INSERT INTO roles (role_name) VALUES ('admin'), ('user'), ('moderator')
        ON CONFLICT(role_name) DO NOTHING;
        ''')

        conn.commit()
        cur.close()
        conn.close()
        print("Database initialized successfully")
    except psycopg2.Error as e:
        print(f"Error initializing database: {e}")

def insert_initial_admin():
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Check if the admin already exists
        cur.execute("SELECT id FROM users WHERE username = %s", (ADMIN_USERNAME,))
        admin_exists = cur.fetchone()

        if not admin_exists:
            # Generate a secure password and hash it
            password = ADMIN_PASSWORD
            salt = generate_salt()
            hashed_password = hash_password(password, salt)

            # Insert the initial admin user - THIS IS CORRECT
            cur.execute('''
                INSERT INTO users (username, email, password_hash, salt, role, status)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (ADMIN_USERNAME, ADMIN_EMAIL, hashed_password, salt, 'admin', 'active'))
            admin_id = cur.fetchone()[0]

            # Assign the admin role to the initial admin
            cur.execute('''
                INSERT INTO user_roles (user_id, role_id)
                SELECT %s, id FROM roles WHERE role_name = %s
            ''', (admin_id, 'admin'))

            conn.commit()
            print("Initial admin added successfully.")
            print()
        else:
            print("Initial admin already exists.")          

    except psycopg2.Error as e:
        print(f"Error inserting initial admin: {e}")

    finally:
        cur.close()
        conn.close()

# Initialize database and admin
init_db()                
insert_initial_admin()

# Routes
@app.route('/')
def index():
    return render_template_string(index_html)

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if not username or not password:
            flash('Username and password are required.', 'danger')
            log_transaction(username, 'failed_login', 'Missing username or password.')
            return redirect(url_for('login'))

        try:
            conn = get_db_connection()
            cur = conn.cursor()

            # Log login attempt
            log_transaction(username, 'login_attempt', f'User {username} attempted to login.')

            # Fetch user details - handle both password and password_hash scenarios
            try:
                cur.execute('SELECT id, password_hash, salt, role, status FROM users WHERE username = %s', (username,))
                user_info = cur.fetchone()
            except psycopg2.Error:
                # If password_hash doesn't exist, try password
                cur.execute('SELECT id, password, salt, role, status FROM users WHERE username = %s', (username,))
                user_info = cur.fetchone()

            if user_info:
                user_id, password_hash, salt, role, status = user_info

                if status == 'locked':
                    # Attempt auto-unlock before rejecting the login
                    unlock_status = try_auto_unlock()
                    log_transaction(username, 'auto_unlock_attempt', unlock_status)

                    # Fetch status again to check if unlock succeeded
                    cur.execute('SELECT status FROM users WHERE username = %s', (username,))
                    updated_status = cur.fetchone()[0]
                    
                    if updated_status == 'active':
                        flash('Your account was locked but has been automatically unlocked. Please try logging in again.', 'info')
                        return redirect(url_for('login'))
                    
                    else:
                        flash('Your account is locked. Please enter a valid token to unlock it.', 'danger')
                        return redirect(url_for('unlock_account'))

                # ONLY THIS LINE CHANGED - from hash_password() to verify_password()
                elif verify_password(password, password_hash, salt):
                    # Successful login - ALL ORIGINAL LOGIC PRESERVED
                    session['username'] = username
                    session['role'] = role
                    session['is_admin'] = (role == 'admin')

                    # Check request count for the token
                    cur.execute('SELECT COUNT(*) FROM user_requests WHERE username = %s AND request_type = %s', (username, 'login'))
                    request_count = cur.fetchone()[0]

                    if request_count < 10:
                        # Increment the request count and log the request
                        cur.execute('''
                            INSERT INTO user_requests (username, request_type, requested_at)
                            VALUES (%s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (username, request_type)
                            DO UPDATE SET requested_at = CURRENT_TIMESTAMP
                        ''', (username, 'login'))
                        conn.commit()

                        # Log successful login
                        log_transaction(username, 'login', f'User {username} logged in successfully.')
                        flash('Login successful!', 'success')
                        return redirect(url_for('dashboard'))
                    else:
                        # Archive user details and lock the account
                        cur.execute('INSERT INTO user_interactions (username, role, status) VALUES (%s, %s, %s)', 
                                    (username, role, status))
                        cur.execute('UPDATE users SET status = %s WHERE username = %s', ('locked', username))
                        conn.commit()

                        # Log account locking due to token usage limit
                        log_transaction(username, 'account_locked', f'User {username} account locked due to token usage limit.')
                        flash('Token usage limit reached. Your account is locked for further analysis.', 'warning')
                        return redirect(url_for('index'))
                else:
                    # Log invalid password attempt
                    log_transaction(username, 'failed_login', f'User {username} entered an invalid password.')
                    flash('Invalid password. Please try again.', 'danger')
            else:
                # Log invalid username attempt
                log_transaction(username, 'failed_login', f'User {username} entered an invalid username.')
                flash('Invalid username. Please try again.', 'danger')

        except psycopg2.Error as e:
            # Log any database error
            log_transaction(username, 'login_error', f"Login error: {e}")
            flash(f"Login error: {e}", 'danger')
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()

    return render_template_string(login_html)
    
# Route for unlocking account
@app.route('/unlock_account', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def unlock_account():
    if request.method == 'POST':
        username = request.form.get('username')
        token = request.form.get('token')

        # Check if both username and token are provided
        if not token or not username:
            flash('Both username and token are required.', 'danger')
            return redirect(url_for('unlock_account'))

        try:
            conn = get_db_connection()
            cur = conn.cursor()

            # Check if the token exists in the vouchers table
            cur.execute('SELECT code FROM vouchers WHERE code = %s', (token,))
            token_record = cur.fetchone()

            if token_record:
                # Unlock the user account if the token is valid
                cur.execute('UPDATE users SET status = %s WHERE username = %s', ('active', username))

                # Insert the token into the used_vouchers table
                cur.execute('INSERT INTO used_vouchers (code, username) VALUES (%s, %s)', (token, username))

                # Delete the token from the vouchers table
                cur.execute('DELETE FROM vouchers WHERE code = %s', (token,))

                # Log the unlock action in the user_requests table
                cur.execute('''
                    INSERT INTO user_requests (username, request_type, request_count)
                    VALUES (%s, %s, 1)
                    ON CONFLICT (username, request_type)
                    DO UPDATE SET request_count = user_requests.request_count + 1, requested_at = CURRENT_TIMESTAMP;
                ''', (username, 'unlock_account'))

                conn.commit()

                flash('Your account has been unlocked successfully!', 'success')
                return redirect(url_for('login'))

            else:
                flash('Invalid or already used token. Please try again.', 'danger')

        except psycopg2.Error as e:
            flash(f"Error unlocking account: {e}", 'danger')

        finally:
            # Ensure the cursor and connection are closed
            if cur:
                cur.close()
            if conn:
                conn.close()

    return render_template_string(unlock_account_html)
    
@app.route('/auto_unlock', methods=['POST'])
@limiter.limit("10 per minute")
def save_payment():
    phone = request.form.get('phone')
    transaction_id = request.form.get('transaction_id')
    amount = request.form.get('amount')
    sender_name = request.form.get('sender_name')
    raw_message = request.form.get('message')

    if not phone or not transaction_id or not amount:
        return "Missing required fields", 400

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Save payment to mpesa_payments
        cur.execute('''
            INSERT INTO mpesa_payments (phone, transaction_id, amount, sender_name, raw_message)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (transaction_id) DO NOTHING
        ''', (phone, transaction_id, amount, sender_name, raw_message))

        # Save transaction_id to vouchers (if not already there)
        cur.execute('SELECT 1 FROM vouchers WHERE code = %s', (transaction_id,))
        if not cur.fetchone():
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            valid_until = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            created_by = session.get('username', 'manual')

            cur.execute('''
                INSERT INTO vouchers (code, created_at, created_by, valid_until)
                VALUES (%s, %s, %s, %s)
            ''', (transaction_id, created_at, created_by, valid_until))

        conn.commit()
        cur.close()
        conn.close()

        return "Payment saved and voucher created", 200

    except Exception as e:
        return f"Error: {str(e)}", 500  

# Register a new user with a voucher code or M-Pesa payment
@app.route('/register', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        voucher = request.form.get('voucher')

        if not username or not email or not password or not voucher:
            flash('All fields are required.', 'danger')
            return redirect(url_for('register'))

        try:
            conn = get_db_connection()
            cur = conn.cursor()

            # Check if user is locked
            cur.execute('SELECT status FROM users WHERE username = %s', (username,))
            user_status = cur.fetchone()

            if user_status and user_status[0] == 'locked':
                # Validate the voucher for unlocking the account
                cur.execute('SELECT * FROM vouchers WHERE code = %s', (voucher,))
                voucher_info = cur.fetchone()

                if voucher_info:
                    # Unlock the account by updating the status
                    cur.execute('DELETE FROM vouchers WHERE code = %s', (voucher,))
                    cur.execute('UPDATE users SET status = %s WHERE username = %s', ('active', username))
                    conn.commit()

                    # Log the transaction for account unlocking
                    log_transaction(username, 'unlock', f'User {username} unlocked their account using a voucher.')

                    # Update the request_count in user_requests or insert a new entry if it doesn't exist
                    cur.execute('SELECT request_count FROM user_requests WHERE username = %s AND request_type = %s', 
                                (username, 'unlock'))
                    request_record = cur.fetchone()

                    if request_record:
                        cur.execute('''
                            UPDATE user_requests 
                            SET request_count = request_count + 1, requested_at = CURRENT_TIMESTAMP
                            WHERE username = %s AND request_type = %s
                        ''', (username, 'unlock'))
                    else:
                        cur.execute('''
                            INSERT INTO user_requests (username, request_type, request_count, requested_at)
                            VALUES (%s, %s, 1, CURRENT_TIMESTAMP)
                        ''', (username, 'unlock'))
                    conn.commit()

                    flash('Your account has been unlocked. Please log in.', 'success')
                    return redirect(url_for('login'))
                else:
                    # Invalid voucher, redirect to M-Pesa payment page
                    flash('Invalid or already used voucher code. Please complete payment.', 'danger')
                    return redirect(url_for('mpesa_payment'))
            else:
                # Normal registration process for new users
                cur.execute('SELECT * FROM vouchers WHERE code = %s', (voucher,))
                voucher_info = cur.fetchone()

                if voucher_info:
                    # Voucher is valid, proceed with registration
                    cur.execute('DELETE FROM vouchers WHERE code = %s', (voucher,))
                    cur.execute('INSERT INTO used_vouchers (code, username) VALUES (%s, %s)', (voucher, username))
                    conn.commit()

                    # Define the salt and hashed password
                    salt = generate_salt()
                    hashed_password = hash_password(password, salt)

                    try:
                        cur.execute('''
                        INSERT INTO users (username, email, password_hash, salt, role, status)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ''', (username, email, hashed_password, salt, 'user', 'active'))
                        
                        # Log the transaction for registration
                        log_transaction(username, 'register', f'User {username} registered.')

                        # Insert into user_requests to start tracking request count for the new user
                        cur.execute('''
                            INSERT INTO user_requests (username, request_type, request_count, requested_at)
                            VALUES (%s, %s, 1, CURRENT_TIMESTAMP)
                        ''', (username, 'register'))
                        conn.commit()

                        flash('Registration successful! Please log in.', 'success')
                        return redirect(url_for('login'))
                    except psycopg2.IntegrityError:
                        flash('This username or email is already taken. Please choose another one.', 'danger')
                else:
                    # Invalid voucher, redirect to M-Pesa payment page
                    flash('The voucher code you entered is invalid or has already been used. Please complete payment.', 'danger')
                    return redirect(url_for('mpesa_payment'))

        except psycopg2.Error as e:
            flash(f"Registration error: {e}", 'danger')
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()

    return render_template_string(register_html)
    
# Route to render the payment page
@app.route('/mpesa_payment')
def mpesa_payment():
    return render_template_string(mpesa_payment_html)

# Route to initiate M-Pesa payment
@app.route('/pay', methods=['GET', 'POST'])
def pay():
    if request.method == 'POST':
        phone_number = request.form.get('phone_number')
        amount = 20 
        
        try:
            response = lipa_na_mpesa(phone_number, amount)

            if response and response.get('ResponseCode') == '0':
                flash('Payment initiated. Please check your phone to complete the transaction.', 'success')
            else:
                flash('Payment initiation failed. Please try again.', 'danger')
                
        except KeyError as ke:
            logging.error(f"Missing key in response: {str(ke)}")
            flash('Error initiating payment. Please check your credentials or contact support.', 'danger')
        
        except Exception as e:
            logging.error(f"Error during M-Pesa payment initiation: {str(e)}")
            flash('Something went wrong while initiating payment. Please try again or contact support.', 'danger')

        return redirect(url_for('mpesa_payment'))

    return render_template_string(mpesa_payment_html)

# M-Pesa callback route
@app.route('/callback', methods=['POST'])
def mpesa_callback():
    try:
        mpesa_data = request.json
        if not mpesa_data:
            logging.error("No JSON data received in M-Pesa callback.")
            return jsonify({"ResultCode": 1, "ResultDesc": "Invalid data"}), 400

        result_code = mpesa_data.get('Body', {}).get('stkCallback', {}).get('ResultCode', None)
        if result_code is None:
            logging.error("ResultCode missing in M-Pesa callback.")
            return jsonify({"ResultCode": 1, "ResultDesc": "Invalid callback format"}), 400

        if result_code == 0:
            transaction_id = mpesa_data['Body']['stkCallback'].get('CheckoutRequestID')
            metadata_items = mpesa_data['Body']['stkCallback'].get('CallbackMetadata', {}).get('Item', [])

            if len(metadata_items) >= 5:
                amount = metadata_items[0].get('Value')
                phone_number = metadata_items[4].get('Value')

                if amount and phone_number:
                    # Convert M-Pesa format (254XXX) to username format (0XXX)
                    if phone_number.startswith('254'):
                        username = '0' + phone_number[3:]
                    else:
                        username = phone_number
                    
                    conn = get_db_connection()
                    cur = conn.cursor()
                    
                    # Calculate tokens based on amount (2 KES per token)
                    tokens_to_create = int(int(amount) / 2)
                    
                    for i in range(tokens_to_create):
                        token_code = f"MPESA_{transaction_id}_{i}"
                        created_at = datetime.now()
                        valid_until = created_at + timedelta(days=30)  # Same 30-day validity
                        
                        cur.execute('''
                            INSERT INTO vouchers (code, username, created_at, created_by, valid_until)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (code) DO NOTHING
                        ''', (token_code, username, created_at, 'mpesa_system', valid_until))
                    
                    conn.commit()
                    logging.info(f"M-Pesa: Assigned {tokens_to_create} tokens to user {username}")
                    
                    cur.close()
                    conn.close()
                    
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200

    except Exception as e:
        logging.error(f"Error processing M-Pesa callback: {str(e)}")
        return jsonify({"ResultCode": 1, "ResultDesc": "Error processing request"}), 500
    
@app.route('/admin_reset_password', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def admin_reset_password():
    if 'role' not in session or session['role'] != 'admin':
        flash('Access denied. Only admins can reset passwords.', 'danger')
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        new_password = request.form.get('new_password')

        if not username or not new_password:
            flash('Username and new password are required.', 'danger')
            return redirect(url_for('admin_reset_password'))

        try:
            conn = get_db_connection()
            cur = conn.cursor()

            # Check if the user exists
            cur.execute('SELECT id, username FROM users WHERE username = %s', (username,))
            user_info = cur.fetchone()

            if user_info:
                # User exists, generate salt and hash the new password
                salt = generate_salt()
                hashed_password = hash_password(new_password, salt)

                # Update the password in the database
                cur.execute('UPDATE users SET password_hash = %s, salt = %s WHERE username = %s', 
                            (hashed_password, salt, username))
                conn.commit()

                # Log the password reset
                log_transaction(session['username'], 'admin_password_reset', f'Admin reset password for user {username}.')

                flash(f'Password reset successfully for user {username}.', 'success')
                flash(f'New password for {username} is: {new_password}', 'info')
            else:
                flash('User not found.', 'danger')

        except psycopg2.Error as e:
            flash(f"Error resetting password: {e}", 'danger')
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()

    return render_template_string(admin_reset_password_html)
    
@app.route('/check-auth')
def check_auth():
    """Check if user is authenticated for frontend JavaScript"""
    if 'username' in session:
        return jsonify({
            'authenticated': True,
            'user': {
                'name': session.get('username'),
                'id': session.get('user_id', ''),
                'phone': session.get('username')  # Using username as phone
            }
        })
    return jsonify({'authenticated': False})    
    
@app.route('/dashboard')
def dashboard():
    # Fetch is_admin and account_status from session
    is_admin = session.get('is_admin', False)
    account_status = session.get('account_status', 'active')
    
    # Fetch media items
    media_items = get_media_items()
    
    # CALCULATE TOKEN BALANCE
    token_balance = 0.0
    if 'username' in session:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute('SELECT COUNT(*) FROM vouchers WHERE username = %s', (session['username'],))
            token_count = cur.fetchone()[0]
            token_balance = float(token_count)  # Convert to float for display
        except Exception as e:
            print(f"Error fetching token balance: {e}")
        finally:
            cur.close()
            conn.close()

    # Render dashboard with the appropriate context
    return render_template_string(dashboard_html, 
                                  is_admin=is_admin, 
                                  account_status=account_status, 
                                  media_items=media_items,
                                  token_balance=token_balance)
                                  
                                  
@app.route('/upload_media', methods=['GET', 'POST'])
def upload_media():
    if 'username' not in session or session.get('role') != 'admin':  # ADD ADMIN CHECK
        flash('Access denied. Only admins can upload media.', 'danger')
        return redirect(url_for('dashboard'))  
    
    if request.method == 'POST':
        title = request.form.get('title')
        content = request.form.get('content')
        media_file = request.files.get('media_file')
        
        if not title or not media_file:
            flash('Title and media file are required.', 'danger')
            return redirect(url_for('upload_media'))
        
        try:
            # Secure filename and upload to S3
            filename = secure_filename(media_file.filename)
            s3_url = upload_to_s3(media_file, filename)
            
            if s3_url:
                # Save to database
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute('''
                    INSERT INTO media_content (title, content, content_type, photo_filename, created_by)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (title, content, media_file.content_type, filename, session['username']))
                conn.commit()
                cur.close()
                conn.close()
                
                flash('Media uploaded successfully!', 'success')
                return redirect(url_for('dashboard'))
            else:
                flash('Failed to upload media to storage.', 'danger')
                
        except Exception as e:
            flash(f'Error uploading media: {str(e)}', 'danger')
    
    return render_template_string(upload_media_html)
                                                                                                                                       
@app.route('/add_token', methods=['GET', 'POST'])
def add_token():
    if 'username' not in session or session.get('role') != 'admin':
        flash("Unauthorized access. Admins only.", 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        username = request.form.get('username')  # User's phone number
        amount = request.form.get('amount')      # Payment amount in KES
        payment_method = request.form.get('payment_method')  # Cash, Bank, etc.

        if not username or not amount:
            flash("Username and amount are required.", 'danger')
            return redirect(url_for('add_token'))

        try:
            conn = get_db_connection()
            cur = conn.cursor()

            # Check if user exists
            cur.execute('SELECT id FROM users WHERE username = %s', (username,))
            user_exists = cur.fetchone()

            if not user_exists:
                flash("User does not exist. Please ask user to register first.", 'danger')
                return redirect(url_for('add_token'))

            # Calculate tokens based on amount (2 KES per token)
            tokens_to_create = int(int(amount) / 2)
            
            if tokens_to_create < 1:
                flash("Minimum amount is KES 2 for 1 token.", 'danger')
                return redirect(url_for('add_token'))

            # Generate unique transaction ID for manual payment
            transaction_id = f"MANUAL_{datetime.now().strftime('%Y%m%d%H%M%S')}_{username}"
            
            # Create tokens (same structure as M-Pesa tokens)
            for i in range(tokens_to_create):
                token_code = f"{transaction_id}_{i}"
                created_at = datetime.now()
                valid_until = created_at + timedelta(days=30)  # Same 30-day validity
                
                cur.execute('''
                    INSERT INTO vouchers (code, username, created_at, created_by, valid_until)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (code) DO NOTHING
                ''', (token_code, username, created_at, session['username'], valid_until))

            # Log the manual payment transaction
            cur.execute('''
                INSERT INTO mpesa_payments (phone, transaction_id, amount, sender_name, status)
                VALUES (%s, %s, %s, %s, %s)
            ''', (username, transaction_id, amount, f"Manual by {session['username']}", 'manual_payment'))

            conn.commit()

            # Log admin action
            log_transaction(session['username'], 'manual_token_add', 
                          f'Added {tokens_to_create} tokens to {username} for KES {amount} via {payment_method}')

            flash(f"Successfully added {tokens_to_create} tokens to user {username} for KES {amount}.", 'success')
            return redirect(url_for('add_token'))

        except ValueError:
            flash("Invalid amount. Please enter a valid number.", 'danger')
        except psycopg2.Error as e:
            flash(f"Error adding tokens: {e}", 'danger')
        finally:
            cur.close()
            conn.close()

    return render_template_string(add_token_html)

@app.route('/change_password', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def change_password():
    if 'username' not in session:
        flash("You must be logged in to change your password.", 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        if not current_password or not new_password or not confirm_password:
            flash("All fields are required.", 'danger')
            return redirect(url_for('change_password'))
            
        if new_password != confirm_password:
            flash("New passwords do not match.", 'danger')
            return redirect(url_for('change_password'))

        try:
            conn = get_db_connection()
            cur = conn.cursor()

            # Fetch the user's current password hash and salt
            cur.execute('SELECT password_hash, salt FROM users WHERE username = %s', (session['username'],))
            user = cur.fetchone()

            if user:
                stored_password_hash, salt = user

                # Verify the current password
                current_password_hash = hash_password(current_password, salt)
                if current_password_hash != stored_password_hash:
                    flash('Current password is incorrect.', 'danger')
                    return redirect(url_for('change_password'))

                # Generate a new salt and hash the new password
                new_salt = generate_salt()
                new_password_hash = hash_password(new_password, new_salt)

                # Update the user's password hash and salt in the database
                cur.execute('''
                    UPDATE users
                    SET password_hash = %s, salt = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE username = %s
                ''', (new_password_hash, new_salt, session['username']))

                # Check if a request record already exists for this user and request type
                cur.execute('SELECT request_count FROM user_requests WHERE username = %s AND request_type = %s', 
                            (session['username'], 'change_password'))
                request_record = cur.fetchone()

                if request_record:
                    # Record exists, so update the request_count and requested_at
                    cur.execute('''
                        UPDATE user_requests
                        SET request_count = request_count + 1, requested_at = %s
                        WHERE username = %s AND request_type = %s
                    ''', (datetime.now(), session['username'], 'change_password'))
                else:
                    # Record does not exist, so insert a new row
                    cur.execute('''
                        INSERT INTO user_requests (username, request_type, request_count, requested_at)
                        VALUES (%s, %s, %s, %s)
                    ''', (session['username'], 'change_password', 1, datetime.now()))

                conn.commit()

                # Log the password change
                log_transaction(session['username'], 'change_password', 'Password changed successfully.')

                flash("Password changed successfully!", 'success')
                return redirect(url_for('dashboard'))

            else:
                flash('User not found.', 'danger')

        except psycopg2.Error as e:
            flash(f"Change password error: {e}", 'danger')
        finally:
            cur.close()
            conn.close()

    return render_template_string(change_password_html)
    
@app.route('/logout')
def logout():
    # Retrieve the username from the session if it's stored there
    username = session.get('username', 'Unknown User')

    # Clear the session
    session.clear()

    # Log the logout action
    log_transaction(username, 'logout', f'User {username} logged out successfully.')

    # Flash a success message
    flash("You have been logged out successfully.", 'success')

    # Redirect to the login page
    return redirect(url_for('login'))

@app.route('/find_nearest_service_provider', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def find_nearest_service_provider():
    if 'username' not in session:
        flash("You must be logged in to search for a service provider.", 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        service_type = request.form.get('service_type')
        user_longitude = request.form.get('longitude')
        user_latitude = request.form.get('latitude')

        # Validate inputs
        if not service_type or not user_longitude or not user_latitude:
            flash('All fields are required, including location access.', 'danger')
            return redirect(url_for('find_nearest_service_provider'))

        conn = get_db_connection()
        cur = conn.cursor()

        try:
            # Check user token balance before allowing search
            cur.execute('SELECT COUNT(*) FROM vouchers WHERE username = %s', (session['username'],))
            token_count = cur.fetchone()[0]

            if token_count < 1:
                flash('Insufficient tokens. Please purchase more tokens to search for service providers.', 'danger')
                return redirect(url_for('mpesa_payment'))

            # Fetch all service providers of the requested type
            cur.execute('''
                SELECT phone_number, longitude, latitude 
                FROM service_providers
                WHERE service_type = %s;
            ''', (service_type,))

            providers = cur.fetchall()

            if not providers:
                flash(f'No {service_type} providers found.', 'warning')
                log_transaction(session['username'], 'search_provider', f"Searched nearest {service_type}. No providers found.")
                return redirect(url_for('find_nearest_service_provider'))

            # Convert the user's input coordinates to floats
            user_longitude = float(user_longitude)
            user_latitude = float(user_latitude)

            # Find the nearest provider using the haversine function
            nearest_provider = None
            nearest_distance = float('inf')
            all_providers = []

            for provider in providers:
                phone_number, provider_longitude, provider_latitude = provider

                # Calculate the distance using the haversine function
                distance = haversine(user_longitude, user_latitude, provider_longitude, provider_latitude)
                all_providers.append({
                    'phone': phone_number,
                    'distance': distance,
                    'longitude': provider_longitude,
                    'latitude': provider_latitude
                })

                if distance < nearest_distance:
                    nearest_distance = distance
                    nearest_provider = phone_number

            if nearest_provider:
                # Deduct 1 token for the successful search (changed from 2 to 1)
                cur.execute('DELETE FROM vouchers WHERE username = %s LIMIT 1', (session['username'],))
                
                # Check if tokens were actually deducted
                tokens_deducted = cur.rowcount
                
                if tokens_deducted == 1:
                    flash(f'The nearest {service_type} is {nearest_distance:.2f} km away. Contact: {nearest_provider}. 1 token (KES 2) deducted.', 'success')
                else:
                    flash(f'The nearest {service_type} is {nearest_distance:.2f} km away. Contact: {nearest_provider}. Token deduction failed - please contact support.', 'warning')

                # Store detailed results in session for display
                session['last_search_result'] = {
                    'service_type': service_type,
                    'nearest_provider': nearest_provider,
                    'distance': round(nearest_distance, 1),
                    'token_deducted': tokens_deducted,
                    'total_providers_found': len(providers)
                }

                # Log successful search transaction
                log_transaction(session['username'], 'search_provider',
                              f"Searched nearest {service_type}. Contact: {nearest_provider}, Distance: {nearest_distance:.2f} km. Found {len(providers)} providers.")
            else:
                flash(f'No {service_type} providers found nearby.', 'warning')
                # Log failed search transaction
                log_transaction(session['username'], 'search_provider', f"Searched nearest {service_type}. No providers found.")

            # Increment or insert user request count for 'find_nearest_service_provider'
            cur.execute('''
                SELECT request_count FROM user_requests WHERE username = %s AND request_type = %s
            ''', (session['username'], 'find_nearest_service_provider'))
            request_record = cur.fetchone()

            if request_record:
                cur.execute('''
                    UPDATE user_requests
                    SET request_count = request_count + 1, requested_at = CURRENT_TIMESTAMP
                    WHERE username = %s AND request_type = %s
                ''', (session['username'], 'find_nearest_service_provider'))
            else:
                cur.execute('''
                    INSERT INTO user_requests (username, request_type, request_count, requested_at)
                    VALUES (%s, %s, 1, CURRENT_TIMESTAMP)
                ''', (session['username'], 'find_nearest_service_provider'))

            conn.commit()

        except Exception as e:
            flash(f"Error finding the nearest provider: {e}", 'danger')
            log_transaction(session['username'], 'search_error', f"Error during {service_type} search: {e}")
        finally:
            cur.close()
            conn.close()

    # Check if we have a previous search result to display
    search_result = session.pop('last_search_result', None)
    
    return render_template_string(find_nearest_service_provider_html, search_result=search_result)

@app.route('/submit_service_provider', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def submit_service_provider():
    if 'username' not in session:
        flash("You must be logged in to submit your details as a new or updated service provider.", 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        service_type = request.form.get('service_type')
        phone_number = request.form.get('phone_number')
        password = request.form.get('password')
        longitude = request.form.get('longitude')
        latitude = request.form.get('latitude')
        location_accuracy = request.form.get('location_accuracy')

        # Validate inputs
        if not all([service_type, phone_number, password, longitude, latitude]):
            flash('All fields are required, including location access.', 'danger')
            return redirect(url_for('submit_service_provider'))

        try:
            # Additional input validation
            longitude = float(longitude)
            latitude = float(latitude)
            accuracy = float(location_accuracy) if location_accuracy else 0
        except ValueError:
            flash('Invalid location values.', 'danger')
            return redirect(url_for('submit_service_provider'))

        conn = get_db_connection()
        cur = conn.cursor()

        try:
            # Check user token balance
            cur.execute('SELECT COUNT(*) FROM vouchers WHERE username = %s', (session['username'],))
            token_count = cur.fetchone()[0]

            if token_count < 1:
                flash('Insufficient tokens. Please purchase more tokens to submit service provider details.', 'danger')
                return redirect(url_for('mpesa_payment'))

            # Check if phone number is already registered for this service type
            cur.execute('SELECT id, password FROM service_providers WHERE phone_number = %s AND service_type = %s', 
                        (phone_number, service_type))
            provider = cur.fetchone()

            # Generate a salt and hash the password
            salt = generate_salt()
            hashed_password = hash_password(password, salt)

            if provider:
                # Update existing provider details
                cur.execute('''
                    UPDATE service_providers 
                    SET longitude = %s, latitude = %s, password = %s, salt = %s
                    WHERE id = %s
                ''', (longitude, latitude, hashed_password, salt, provider[0]))
                action_message = 'Service provider information updated successfully.'
                log_action = 'update_provider'
            else:
                # Insert new provider details
                cur.execute('''
                    INSERT INTO service_providers (service_type, phone_number, password, salt, longitude, latitude)
                    VALUES (%s, %s, %s, %s, %s, %s)
                ''', (service_type, phone_number, hashed_password, salt, longitude, latitude))
                action_message = 'Service provider information submitted successfully.'
                log_action = 'new_provider'

            # Deduct 1 token for the service (changed from 2 to 1)
            cur.execute('DELETE FROM vouchers WHERE username = %s LIMIT 1', (session['username'],))
            
            # Check if tokens were actually deducted
            tokens_deducted = cur.rowcount
            
            conn.commit()

            if tokens_deducted == 1:
                flash(f'{action_message} 1 token (KES 2) deducted.', 'success')
            else:
                flash(f'{action_message} Token deduction failed - please contact support.', 'warning')

            # Store registration details for display
            session['last_registration'] = {
                'service_type': service_type,
                'phone_number': phone_number,
                'location_accuracy': accuracy,
                'tokens_deducted': tokens_deducted,
                'action': 'updated' if provider else 'registered'
            }

            # Log transaction
            log_transaction(session['username'], log_action, 
                          f"{log_action} {service_type} with phone {phone_number}. Location accuracy: {accuracy:.0f}m")

            # Increment or insert user request count for 'submit_service_provider'
            cur.execute('''
                SELECT request_count FROM user_requests WHERE username = %s AND request_type = %s
            ''', (session['username'], 'submit_service_provider'))
            request_record = cur.fetchone()

            if request_record:
                cur.execute('''
                    UPDATE user_requests
                    SET request_count = request_count + 1, requested_at = CURRENT_TIMESTAMP
                    WHERE username = %s AND request_type = %s
                ''', (session['username'], 'submit_service_provider'))
            else:
                cur.execute('''
                    INSERT INTO user_requests (username, request_type, request_count, requested_at)
                    VALUES (%s, %s, 1, CURRENT_TIMESTAMP)
                ''', (session['username'], 'submit_service_provider'))

            conn.commit()

        except psycopg2.IntegrityError as e:
            conn.rollback()
            flash('This phone number is already registered for this service type.', 'danger')
        except psycopg2.Error as e:
            conn.rollback()
            flash(f"An error occurred while submitting: {e}", 'danger')
        finally:
            cur.close()
            conn.close()

    # Check if we have a previous registration result to display
    registration_result = session.pop('last_registration', None)
    
    return render_template_string(submit_service_provider_html, registration_result=registration_result)
    
@app.route("/offline")
def offline():
    return """
    <html><head><title>Offline</title></head>
    <body style="text-align:center;padding:40px;font-family:sans-serif;">
        <h1>You're Offline</h1>
        <p>It looks like you don't have an internet connection.</p>
        <p>Try again when you're back online.</p>
    </body></html>
    """    
@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json')

@app.route('/service-worker.js')
def sw():
    return send_from_directory('static', 'service-worker.js')
    
@app.route('/terms')
def terms():
    return render_template_string(terms_html)

@app.route('/privacy')
def privacy():
    return render_template_string(privacy_html)

@app.route('/docs')
def docs():
    return render_template_string(docs_html)
    
@app.route('/admin/vouchers')
def admin_vouchers():
    if 'username' not in session or session.get('role') != 'admin':
        flash('Access denied. Admins only.', 'danger')
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Get search parameters
    username_filter = request.args.get('username', '')
    show_used = request.args.get('show_used', 'false') == 'true'
    show_expired = request.args.get('show_expired', 'false') == 'true'
    
    # Build query
    if show_used:
        query = '''
            SELECT v.code, v.username, v.created_at, v.created_by, v.valid_until,
                   uv.used_at, uv.username as used_by
            FROM vouchers v
            LEFT JOIN used_vouchers uv ON v.code = uv.code
            WHERE 1=1
        '''
    else:
        query = '''
            SELECT code, username, created_at, created_by, valid_until,
                   NULL as used_at, NULL as used_by
            FROM vouchers 
            WHERE 1=1
        '''
    
    params = []
    
    if username_filter:
        query += ' AND v.username = %s' if show_used else ' AND username = %s'
        params.append(username_filter)
    
    if not show_used:
        query += ' AND NOT EXISTS (SELECT 1 FROM used_vouchers uv WHERE uv.code = vouchers.code)'
    
    if not show_expired:
        query += ' AND valid_until > NOW()'
    
    query += ' ORDER BY created_at DESC'
    
    cur.execute(query, params)
    vouchers = cur.fetchall()
    
    # Get statistics
    cur.execute('''
        SELECT 
            COUNT(*) as total_vouchers,
            COUNT(CASE WHEN valid_until > NOW() THEN 1 END) as valid_vouchers,
            COUNT(CASE WHEN valid_until <= NOW() THEN 1 END) as expired_vouchers,
            (SELECT COUNT(*) FROM used_vouchers) as used_vouchers
        FROM vouchers
    ''')
    stats = cur.fetchone()
    
    cur.close()
    conn.close()
    
    # FIX: Add this line to provide 'now' to the template
    from datetime import datetime
    now = datetime.now()
    
    return render_template_string(admin_vouchers_html, 
                                vouchers=vouchers, 
                                username_filter=username_filter,
                                show_used=show_used,
                                show_expired=show_expired,
                                stats=stats,
                                now=now)  # This fixes the 'now is undefined' error

@app.route('/admin/vouchers/export')
def export_vouchers():
    if 'username' not in session or session.get('role') != 'admin':
        return "Unauthorized", 403
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    username_filter = request.args.get('username', '')
    
    query = '''
        SELECT username, code, created_at, created_by, valid_until
        FROM vouchers 
        WHERE 1=1
    '''
    params = []
    
    if username_filter:
        query += ' AND username = %s'
        params.append(username_filter)
    
    query += ' ORDER BY username, created_at'
    
    cur.execute(query, params)
    vouchers = cur.fetchall()
    
    cur.close()
    conn.close()
    
    # Create CSV
    
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Phone Number', 'Voucher Code', 'Created At', 'Created By', 'Valid Until'])
    
    for voucher in vouchers:
        writer.writerow(voucher)
    
    from flask import make_response
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = "attachment; filename=vouchers.csv"
    response.headers["Content-type"] = "text/csv"
    return response

@app.route('/admin/vouchers/delete_expired', methods=['POST'])
def delete_expired_vouchers():
    if 'username' not in session or session.get('role') != 'admin':
        flash('Access denied. Admins only.', 'danger')
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('DELETE FROM vouchers WHERE valid_until <= NOW()')
    deleted_count = cur.rowcount
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash(f'Deleted {deleted_count} expired vouchers', 'success')
    return redirect(url_for('admin_vouchers'))    
    

index_html = """<!DOCTYPE html>
<html lang="en">  
<head>
  <meta charset="UTF-8" />  
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>  
  <title>Mashamba na Nyumba Portals</title>    
  <link rel="icon" href="{{ url_for('static', filename='favicon.ico') }}" type="image/x-icon" />  
  <link rel="manifest" href="{{ url_for('static', filename='manifest.json') }}" />  
  <meta name="theme-color" content="#1e88e5" />    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" />  <style>  
    body {  
        font-family: Arial, sans-serif;  
        margin: 0;  
        padding: 0;  
        background-color: #f4f6f9;  
        color: #333333;  
        text-align: center;  
    }  
    .container {  
        min-height: 100vh;  
        display: flex;  
        flex-direction: column;  
    }  
    .header {  
        padding: 20px;  
        background-color: #1e88e5;  
        color: #ffffff;  
    }  
    .header img {  
        max-width: 100px;  
        height: auto;  
        display: block;  
        margin: 0 auto 10px;  
    }  
    .header h1 {  
        margin: 0;  
        font-size: 2.5em;  
    }  
    .telephone {  
        margin-top: 10px;  
        font-size: 1.2em;  
        color: #e3f2fd;  
        display: flex;  
        justify-content: center;  
        align-items: center;  
        gap: 8px;  
    }  
    .telephone i {  
        font-size: 1.3em;  
    }
    .content {  
        padding: 30px 20px;  
        flex: 1;  
    }  
    .content p {  
        font-size: 1.3em;  
        color: #555555;  
        margin-bottom: 20px;  
    }
    .account-heading {
    color: #000000; /* black */
    font-size: 1.8em;
    font-weight: bold;
    text-shadow: 2px 2px 4px rgba(0,0,0,0.15); /* soft shadow for depth */
    letter-spacing: 1px;
    margin-bottom: 10px;
    }
    .content a {  
        display: inline-block;  
        padding: 12px 24px;  
        margin: 10px;  
        color: #ffffff;  
        background-color: #43a047;  
        text-decoration: none;  
        border-radius: 6px;  
        font-size: 1.1em;  
        transition: background-color 0.3s ease;  
    }  
    .content a:hover {  
        background-color: #2e7d32;  
    }
    .harambee-banner {
      background: linear-gradient(to right, #006400, #008080);
      border-radius: 16px;
      margin: 40px auto;
      max-width: 800px;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
      padding: 60px 20px;
      }

    .harambee-logo {
      max-width: 120px;
      height: auto;
      border-radius: 12px;
      box-shadow: 0 0 10px rgba(255, 255, 255, 0.2);
      }    
#installBtn {
    display: block;
    margin: 20px auto;
    padding: 10px 20px; /* reduce left/right padding */
    width: auto; /* remove width override */
    background-color: #ff9800;
    color: white;
    font-weight: bold;
    font-size: 1em;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    box-shadow: 0 3px 6px rgba(0,0,0,0.15);
    transition: background-color 0.3s ease;
}
#installBtn:hover {
    background-color: #f57c00;
} 
    .footer {  
        background-color: #0d47a1;  
        color: #ffffff;  
        padding: 15px;  
        font-size: 0.95em;  
    }  
    .flash-messages {  
        list-style-type: none;  
        padding: 0;  
        margin: 20px auto;  
        max-width: 600px;  
    }  
    .flash-messages li {  
        background-color: #fff3cd;  
        color: #856404;  
        border: 1px solid #ffeeba;  
        border-radius: 5px;  
        padding: 10px;  
        margin: 10px 0;  
    }  
    .socials {  
        margin: 15px 0;  
    }  
    .socials a {  
        margin: 0 15px;  
        text-decoration: none;  
        display: inline-flex;  
        align-items: center;  
        justify-content: center;  
        width: 45px;  
        height: 45px;  
        border-radius: 50%;  
        background-color: white;  
        transition: transform 0.3s ease;  
    }  
    .socials a:hover {  
        transform: scale(1.1);  
    }  
    .socials img {  
        width: 24px;  
        height: 24px;  
        vertical-align: middle;  
    }  
    .phone-icon {  
        display: inline-block;  
        width: 24px;  
        height: 24px;  
        background-color: #25D366;  
        mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M6.62 10.79c1.44 2.83 3.76 5.14 6.59 6.59l2.2-2.2c.27-.27.67-.36 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1-9.39 0-17-7.61-17-17 0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z'/%3E%3C/svg%3E") no-repeat center;  
        -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M6.62 10.79c1.44 2.83 3.76 5.14 6.59 6.59l2.2-2.2c.27-.27.67-.36 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1-9.39 0-17-7.61-17-17 0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z'/%3E%3C/svg%3E") no-repeat center;  
    }
    #timestamp-display {  
        margin-top: 15px;  
        font-size: 1em;  
        color: #555;  
        padding: 10px;  
        background-color: #e3f2fd;  
        border-radius: 5px;  
        display: inline-block;  
    }  
    .install-explainer {  
        margin-top: 15px;  
        font-size: 0.9em;  
        color: #666;  
        max-width: 500px;  
        margin-left: auto;  
        margin-right: auto;  
    }  
</style>

<script>  
  let deferredPrompt;  
  
  window.addEventListener('beforeinstallprompt', (e) => {  
    e.preventDefault();  
    deferredPrompt = e;  
  
    // Check location permission before showing install button  
    navigator.permissions.query({name: 'geolocation'}).then((permissionStatus) => {  
      if (permissionStatus.state === 'granted' || permissionStatus.state === 'prompt') {  
        document.getElementById('installBtn').style.display = 'block';  
      } else {  
        deferredPrompt = null; // Cancel installation  
      }  
    }).catch(() => {  
      deferredPrompt = null; // Cancel on error or unsupported  
    });  
  });  
  
  document.addEventListener('DOMContentLoaded', function () {  
    const installBtn = document.getElementById('installBtn');  
  
    installBtn.addEventListener('click', async () => {  
      try {  
        const permission = await navigator.permissions.query({name: 'geolocation'});  
        if (permission.state === 'denied') {  
          alert('Location permission denied. Installation is cancelled.');  
          deferredPrompt = null;  
          installBtn.style.display = 'none';  
          return;  
        }  
  
        navigator.geolocation.getCurrentPosition(  
          async () => {  
            if (deferredPrompt) {  
              deferredPrompt.prompt();  
              const { outcome } = await deferredPrompt.userChoice;  
              if (outcome === 'accepted') {  
                console.log('User accepted install');  
              } else {  
                console.log('User dismissed install');  
              }  
              deferredPrompt = null;  
            }  
          },  
          (error) => {  
            alert('Location access required to install. Installation cancelled.');  
            deferredPrompt = null;  
            installBtn.style.display = 'none';  
          }  
        );  
      } catch (err) {  
        alert('Unable to verify location permission. Installation cancelled.');  
        deferredPrompt = null;  
        installBtn.style.display = 'none';  
      }  
    });  
  });  
</script>

</head>  
<body>    <div class="header">  
    <img src="{{ url_for('static', filename='mashamba.png') }}" alt="Mashamba na Nyumba Logo">  
    <button id="installBtn" style="display: none;">Install App</button>
    <p id="locationNotice" style="background-color: black; color: white; font-size: 14px; padding: 10px; border-radius: 5px; margin-top: 10px;">
      This app uses geolocation. Your location will be used to determine the distance between you and the other client.
    </p>           
                                 
    {% with messages = get_flashed_messages(with_categories=true) %}  
    {% if messages %}  
    <ul class="flash-messages">  
        {% for category, message in messages %}  
        <li class="{{ category }}">{{ message }}</li>  
        {% endfor %}  
    </ul>  
    {% endif %}  
    {% endwith %}  
      
    <!-- Mashamba na Nyumba-->
    <div class="soko-banner bg-gradient-to-r from-green-600 via-emerald-500 to-lime-500 text-white p-6 rounded-xl shadow-lg mb-6">
      <h1 class="text-4xl font-bold mb-2">Welcome to Mashamba na Nyumba Portal</h1>
      <p class="text-lg mb-4">Your trusted proximity-based service marketplace</p>

      <div class="grid grid-cols-1 md:grid-cols-3 gap-4 text-sm md:text-base">
        <div class="bg-white bg-opacity-10 p-4 rounded-lg border border-white border-opacity-20">
          <h2 class="font-semibold text-lg mb-1">👨‍🔧 Service Providers</h2>
          <p>Register, get listed, and connect with nearby clients using token units.</p>
        </div>

        <div class="bg-white bg-opacity-10 p-4 rounded-lg border border-white border-opacity-20">
          <h2 class="font-semibold text-lg mb-1">🧑‍💼 Clients</h2>
          <p>Search nearby providers, filter by category or location, and unlock contact details.</p>
        </div>
    </div>        

        <div class="bg-white bg-opacity-10 p-4 rounded-lg border border-white border-opacity-20">
          <h2 class="font-semibold text-lg mb-1">🛡️ Admin Panel</h2>
          <p>Secure portal for managing users, tokens, and platform settings.</p>
        </div>
      </div>
                                 
    <div class="content">  
        <p>ACCOUNT MANAGEMENT</p>  
          
        <a href="{{ url_for('register') }}">Register</a>  
        <a href="{{ url_for('login') }}">Login</a>  
    </div>      
    <div class="footer">    
        <p>  
            <a href="/terms" style="color:white">Terms & Conditions</a> |   
            <a href="/privacy" style="color:white">Privacy Policy</a> |   
            <a href="/docs" style="color:white">Documentation</a>  
        </p>    
        <div class="socials">    
            <a href="https://m.facebook.com/jamesboyid.ochuna" target="_blank" title="Facebook">    
                <img src="https://upload.wikimedia.org/wikipedia/commons/5/51/Facebook_f_logo_%282019%29.svg" alt="Facebook">    
            </a>    
            <a href="https://wa.me/254701207062" target="_blank" title="WhatsApp">    
                <img src="https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg" alt="WhatsApp">    
            </a>    
            <a href="tel:+254701207062" title="Call Us">    
                <span class="phone-icon"></span>  
            </a>    
        </div>  
        <p>&copy; Pigasimu 2025. All rights reserved.</p>    
    </div>    
</div>

    <div class="harambee-banner text-center py-5">
       <img src="/static/piclog.png" alt="Harambee Cash Logo" class="harambee-logo mb-4" />
      <h2 class="display-5 fw-bold text-white">Visit Our Partner Platform</h2>
      <p class="lead text-light mb-4">Experience the digita fast, secure, honest mobile harambee fund raiser and auto random disburser on <strong>Harambee Cash App</strong>.</p>
      <a href="https://harambeecash.pigasimu.co.ke" class="btn btn-light btn-lg px-4 rounded-pill fw-bold" target="_blank">
        Go to Harambee Cash
      </a>
    </div>
    
  <script>  
    if ('serviceWorker' in navigator) {  
      navigator.serviceWorker.register('{{ url_for("static", filename="service-worker.js") }}')  
        .then(reg => console.log('Service Worker registered:', reg))  
        .catch(err => console.log('Service Worker registration failed:', err));  
    }  
  
    let deferredPrompt;  
    const installBtn = document.getElementById("installBtn");  
  
    window.addEventListener("beforeinstallprompt", (e) => {  
      e.preventDefault();  
      deferredPrompt = e;  
      installBtn.style.display = "inline-block";  
    });  
  
    installBtn.addEventListener("click", async () => {  
      if (deferredPrompt) {  
        deferredPrompt.prompt();  
        const { outcome } = await deferredPrompt.userChoice;  
        if (outcome === "accepted") {  
          installBtn.style.display = "none";  
        }  
        deferredPrompt = null;  
      }  
    });  
  </script>  </body>  
</html>  
"""

upload_media_html = """
<!DOCTYPE html>
<html>
<head>
    <title>Upload Media</title>
    <style>
        body { font-family: Arial; max-width: 500px; margin: 50px auto; padding: 20px; }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; }
        input, textarea { width: 100%; padding: 8px; }
        button { background: #007bff; color: white; padding: 10px 20px; border: none; cursor: pointer; }
    </style>
</head>
<body>
    <h1>Upload Media</h1>
    <form method="POST" enctype="multipart/form-data">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <div class="form-group">
            <label>Title:</label>
            <input type="text" name="title" required>
        </div>
        <div class="form-group">
            <label>Description:</label>
            <textarea name="content"></textarea>
        </div>
        <div class="form-group">
            <label>Media File:</label>
            <input type="file" name="media_file" accept="image/*,video/*" required>
        </div>
        <button type="submit">Upload</button>
    </form>
    <a href="{{ url_for('dashboard') }}">Back to Dashboard</a>
</body>
</html>
"""

mpesa_payment_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pay with M-Pesa</title>
    
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.1.3/css/bootstrap.min.css">
    <style>
        body {
            background-color: #f4f7fa;
        }
        .container {
            max-width: 600px;
            margin: 0 auto;
            padding-top: 50px;
        }
        .card {
            border: none;
            border-radius: 10px;
            box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
            padding: 20px;
            background-color: #ffffff;
        }
        h2 {
            color: #007bff;
            font-weight: bold;
            text-align: center;
            margin-bottom: 30px;
        }
        label {
            font-weight: bold;
            color: #343a40;
        }
        .form-control {
            border-radius: 10px;
            font-size: 1.25rem;
            padding: 12px;
        }
        .btn {
            background-color: #28a745;
            color: #fff;
            border-radius: 25px;
            font-size: 18px;
            padding: 10px;
            width: 100%;
        }
        .btn:hover {
            background-color: #218838;
        }
        /* Style for outstanding error messages */
        .alert-danger {
            background-color: #f8d7da; /* Bright background */
            color: #dc3545; /* Red text */
            font-size: 1.25rem;
            font-weight: bold;
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 20px;
            text-align: center;
        }
        .alert-info {
            background-color: #d1ecf1;
            color: #0c5460;
            border-color: #bee5eb;
            border-radius: 10px;
        }
        .note-box {
            background-color: #ffefba;
            border: 2px solid #f8c146;
            border-radius: 10px;
            padding: 20px;
            margin-bottom: 20px;
            font-size: 1.1rem;
            text-align: center;
            font-weight: bold;
            color: #333;
        }
        .note-box h3 {
            color: #e67e22;
        }
        .mpesa-details {
            font-size: 1.2rem;
            color: #c0392b;
        }
        /* Adding space between buttons */
        .mb-5 {
            margin-bottom: 50px; /* Larger gap for the last button */
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h2>Pay with M-Pesa</h2>           

            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    <div class="alert alert-info">
                        {% for category, message in messages %}
                            <div class="alert alert-{{ category }}">{{ message }}</div>
                        {% endfor %}
                    </div>
                {% endif %}
            {% endwith %}
            
            <form method="POST" action="/pay">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">            
                <div class="mb-4">
                    <label for="phone_number" class="form-label">Phone Number</label>
                    <input type="tel" class="form-control" id="phone_number" name="phone_number" placeholder="e.g., 2547XXXXXXXX" required>
                </div>
                
                <!-- Sandwiched Buttons -->
                <div class="mb-4">
                    <button type="submit" class="btn btn-success">Pay KES 2</button>
                </div>

                <div class="note-box">
                    <h3>IMPORTANT NOTE</h3>
                    <p>IF THE ABOVE MPESA PAYMENT FOR TOKEN PURCHASE FAILS: Please pay directly to M-Pesa Buy Goods TILL NO: <span class="mpesa-details">4487938</span></p>
                    <p><strong>JAMES BOYID OCHUNA</strong></p>
                    <p>and wait for your token to be sent to your telephone number shortly.</p>
                </div>

                <!-- Home Button -->
                <div class="mb-5">
                    <a href="/" class="btn btn-info">Home</a>
                </div>
            </form>
        </div>
    </div>
</body>
</html>
"""

register_html = """<!DOCTYPE html>
<html lang="en">
<head>
  <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-5190046541953794"
     crossorigin="anonymous"></script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Soko Jirani Portals</title>
    
    <style>
        body {
            font-family: Arial, sans-serif;
            background-color: #4caf50; /* Green background color */
            color: #ffffff;
            text-align: center;
            margin: 0;
            padding: 0;
        }
        form {
            background-color: #388e3c;
            padding: 20px;
            border-radius: 5px;
            display: inline-block;
        }
        label {
            display: block;
            margin: 10px 0 5px;
        }
        input {
            padding: 8px;
            margin-bottom: 10px;
            border-radius: 4px;
            border: none;
            width: 100%;
        }
        input[type="submit"] {
            background-color: #1976d2;
            color: #ffffff;
            cursor: pointer;
            width: 100%;
        }
        input[type="submit"]:hover {
            background-color: #1565c0;
        }
        a {
            display: block;
            margin-top: 20px;
            color: #ffffff;
            text-decoration: none;
            background-color: #1976d2;
            padding: 10px;
            border-radius: 5px;
        }
        a:hover {
            background-color: #1565c0;
        }
        ul {
            list-style: none;
            padding: 0;
        }
        li {
            margin: 5px 0;
            color: #ffcc00;
        }
    </style>
</head>
<body>
    <h1>Register</h1>    
    
    <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">  
        <label for="username">Phone Number :</label>
        <input type="text" name="username" required><br>
        <label for="email">Email:</label>
        <input type="email" name="email" required><br>
        <label for="password">Password:</label>
        <input type="password" name="password" required><br>
        <label for="voucher">Voucher:</label>
        <input type="text" name="voucher" required><br>
        <input type="submit" value="Register">
    </form>
    <a href="{{ url_for('index') }}">Back to Home</a>
    <a href="https://pigasimu.co.ke" class="button neutral">Back to Pigasimu Home</a>    
    {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
    <ul>
        {% for category, message in messages %}
        <li class="{{ category }}">{{ message }}</li>
        {% endfor %}
    </ul>
    {% endif %}
    {% endwith %}
</body>
</html>
"""

dashboard_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Mashamba na Nyumba Portal">
   
    <title>Dashboard</title>
    <style>
        body {
            font-family: 'Helvetica Neue', Arial, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #f0f2f5;
        }
        .header {
            background-color: #003366;
            color: #ffffff;
            padding: 15px;
            text-align: center;
            box-shadow: 0 2px 5px rgba(0, 0, 0, 0.1);
        }
        .header img {
            max-width: 90px;
            height: auto;
            display: block;
            margin: 0 auto 10px;
        }
        .container {
            width: 90%;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        .dashboard {
            background-color: #ffffff;
            padding: 25px;
            border-radius: 10px;
            box-shadow: 0 3px 8px rgba(0, 0, 0, 0.05);
        }
        .dashboard h2 {
            color: #1a1a1a;
            font-size: 1.8em;
            margin-bottom: 10px;
        }
        .dashboard p {
            font-size: 16px;
            color: #555555;
        }
        .button {
            display: inline-block;
            padding: 12px 18px;
            margin: 8px;
            color: #ffffff;
            text-decoration: none;
            border-radius: 5px;
            text-align: center;
            font-weight: bold;
            font-size: 15px;
            transition: background-color 0.3s, transform 0.3s;
            cursor: pointer;
        }
        .button.primary { background-color: #00509e; }
        .button.secondary { background-color: #28a745; }
        .button.success { background-color: #28a745; }
        .button.warning { background-color: #fd7e14; }
        .button.neutral { background-color: #6c757d; }
        .button.admin { background-color: #6f42c1; }
        .button:hover {
            filter: brightness(90%);
            transform: translateY(-2px);
        }
        .button:active {
            filter: brightness(80%);
            transform: translateY(0);
        }
        .hidden { display: none; }
        .button-group {
            margin: 10px 0;
            padding: 10px;
            background-color: #e9ecef;
            border: 1px solid #d6d8db;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.05);
        }
        @media (max-width: 768px) {
            .button {
                display: block;
                width: 100%;
                box-sizing: border-box;
            }
            .header img {
                max-width: 80px;
            }
            .dashboard h2 {
                font-size: 1.5em;
            }
        }
    </style>
</head>
<body>
    <div class="header">
        <img src="{{ url_for('static', filename='jirani.png') }}" alt="Ochuna Logo">
        <h1>MASHAMBA NA NYUMBA</h1>    
        
        <div class="telephone">Tel: +254 701207062</div>
    </div>
    <div class="container">
        <div class="dashboard">
            <h2>You are viewing Live! Soko Jirani Portal</h2>
            <p>Select an option below:</p>

            <button class="button primary" onclick="toggleButtons('random-shop-group')">GO TO MASHAMBA NA NYUMBA</button>
            <!-- Add this in the dashboard template -->
            <div class="token-balance" style="background: #e3f2fd; padding: 10px; border-radius: 5px; margin: 10px 0;">
                <strong>Token Balance: {{ token_balance | default(0.0) }}</strong>
                <p style="margin: 5px 0 0 0; font-size: 0.9em;">Each search/submission costs 1 token (KES 2)</p>
            </div>                 
            <div id="random-shop-group" class="button-group hidden">
                <a href="{{ url_for('submit_service_provider') }}" class="button success">Post Your Service To Mashamba Na Nyumba</a>
                <a href="{{ url_for('find_nearest_service_provider') }}" class="button secondary">Search For Any Shamba or Nyumba</a>
                <a href="{{ url_for('index') }}" class="button neutral">Home</a>
                <a href="{{ url_for('change_password') }}" class="button warning">Change Password</a>
            </div>      
            {% if is_admin %}
                <div class="button-group">
                    <a href="{{ url_for('admin_reset_password') }}" class="button admin">Admin Reset Password</a>
                    <a href="{{ url_for('add_token') }}" class="button admin">Admin Add Token</a>
                    <a href="{{ url_for('upload_media') }}" class="button admin">Upload Media</a>  <!-- ← ADDED HERE -->
                    <!-- ADD THIS NEW BUTTON -->
                    <a href="{{ url_for('admin_vouchers') }}" class="button admin">Manage Vouchers</a>                                                     
                </div>
            {% endif %}
    <script>
        function toggleButtons(groupId) {
            var group = document.getElementById(groupId);
            group.classList.toggle('hidden');
        }
    </script>
</body>
</html>
"""

admin_vouchers_html = """
<!DOCTYPE html>
<html>
<head>
    <title>Voucher Management</title>
    <style>
        body { 
            font-family: Arial, sans-serif; 
            margin: 20px;
            background-color: #f4f6f9;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            padding: 20px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        h1 {
            color: #333;
            border-bottom: 2px solid #007bff;
            padding-bottom: 10px;
        }
        .stats-container {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin: 20px 0;
        }
        .stat-card {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 8px;
            border-left: 4px solid #007bff;
        }
        .stat-number {
            font-size: 24px;
            font-weight: bold;
            color: #007bff;
        }
        .search-form {
            background: #e9ecef;
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
        }
        .form-group {
            margin-bottom: 15px;
        }
        label {
            display: block;
            margin-bottom: 5px;
            font-weight: bold;
        }
        input[type="text"], select {
            width: 100%;
            padding: 8px;
            border: 1px solid #ddd;
            border-radius: 4px;
        }
        .btn {
            padding: 10px 15px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            text-decoration: none;
            display: inline-block;
            margin-right: 10px;
        }
        .btn-primary { background: #007bff; color: white; }
        .btn-success { background: #28a745; color: white; }
        .btn-danger { background: #dc3545; color: white; }
        .btn-warning { background: #ffc107; color: black; }
        .btn-group {
            margin: 20px 0;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
        }
        th, td {
            border: 1px solid #ddd;
            padding: 12px;
            text-align: left;
        }
        th {
            background-color: #f2f2f2;
            font-weight: bold;
        }
        tr:nth-child(even) {
            background-color: #f9f9f9;
        }
        .status-valid { color: #28a745; font-weight: bold; }
        .status-expired { color: #dc3545; font-weight: bold; }
        .status-used { color: #6c757d; font-weight: bold; }
        .checkbox-group {
            display: flex;
            gap: 15px;
            margin: 10px 0;
        }
        .checkbox-group label {
            display: flex;
            align-items: center;
            gap: 5px;
            font-weight: normal;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>📋 Voucher Management</h1>
        
        <!-- Statistics -->
        <div class="stats-container">
            <div class="stat-card">
                <div class="stat-number">{{ stats[0] }}</div>
                <div>Total Vouchers</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{{ stats[1] }}</div>
                <div>Valid Vouchers</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{{ stats[2] }}</div>
                <div>Expired Vouchers</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{{ stats[3] }}</div>
                <div>Used Vouchers</div>
            </div>
        </div>

        <!-- Search Form -->
        <div class="search-form">
            <form method="GET">
                <div class="form-group">
                    <label for="username">Filter by Phone Number:</label>
                    <input type="text" name="username" value="{{ username_filter }}" 
                           placeholder="e.g., 0712345678">
                </div>
                
                <div class="checkbox-group">
                    <label>
                        <input type="checkbox" name="show_used" value="true" 
                               {% if show_used %}checked{% endif %}>
                        Show Used Vouchers
                    </label>
                    <label>
                        <input type="checkbox" name="show_expired" value="true" 
                               {% if show_expired %}checked{% endif %}>
                        Show Expired Vouchers
                    </label>
                </div>
                
                <button type="submit" class="btn btn-primary">Search</button>
                <a href="{{ url_for('admin_vouchers') }}" class="btn btn-warning">Clear Filters</a>
            </form>
        </div>

        <!-- Action Buttons -->
        <div class="btn-group">
            <a href="{{ url_for('export_vouchers') }}?username={{ username_filter }}" 
               class="btn btn-success">📥 Export to CSV</a>
            <form action="{{ url_for('delete_expired_vouchers') }}" method="POST" style="display: inline;">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <button type="submit" class="btn btn-danger" 
                        onclick="return confirm('Delete all expired vouchers?')">
                    🗑️ Delete Expired Vouchers
                </button>
            </form>
            <a href="{{ url_for('dashboard') }}" class="btn btn-primary">← Back to Dashboard</a>
        </div>

        <!-- Vouchers Table -->
        <table>
            <thead>
                <tr>
                    <th>Voucher Code</th>
                    <th>Phone Number</th>
                    <th>Created At</th>
                    <th>Created By</th>
                    <th>Valid Until</th>
                    <th>Status</th>
                    <th>Used By</th>
                    <th>Used At</th>
                </tr>
            </thead>
            <tbody>
                {% for voucher in vouchers %}
                <tr>
                    <td><code>{{ voucher[0] }}</code></td>
                    <td>{{ voucher[1] }}</td>
                    <td>{{ voucher[2] }}</td>
                    <td>{{ voucher[3] }}</td>
                    <td>{{ voucher[4] }}</td>
                    <td>
                        {% if voucher[5] %} <!-- Used -->
                            <span class="status-used">Used</span>
                        {% elif voucher[4] and voucher[4] < now %}
                            <span class="status-expired">Expired</span>
                        {% else %}
                            <span class="status-valid">Valid</span>
                        {% endif %}
                    </td>
                    <td>{{ voucher[6] if voucher[6] else '-' }}</td>
                    <td>{{ voucher[5] if voucher[5] else '-' }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>

        {% if not vouchers %}
        <div style="text-align: center; padding: 40px; color: #6c757d;">
            No vouchers found matching your criteria.
        </div>
        {% endif %}
    </div>

    <script>
        // Auto-format phone number input
        document.querySelector('input[name="username"]').addEventListener('input', function(e) {
            let value = e.target.value.replace(/\D/g, '');
            if (value.startsWith('0')) {
                value = value.substring(0, 10);
            }
            e.target.value = value;
        });

        // Confirm before deleting expired vouchers
        document.querySelector('form[action*="delete_expired"]').addEventListener('submit', function(e) {
            if (!confirm('Are you sure you want to delete all expired vouchers? This action cannot be undone.')) {
                e.preventDefault();
            }
        });
    </script>
</body>
</html>
"""
    
terms_html = """
<!DOCTYPE html>
<html lang="en">
<head>
  <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-5190046541953794"
     crossorigin="anonymous"></script>
    <meta charset="UTF-8">
    <title>Mashamba na Nyumba - Terms and Conditions</title>
     
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {
            font-family: 'Segoe UI', sans-serif;
            margin: 0;
            padding: 20px;
            background: linear-gradient(to right, #ffffff, #e0f7fa);
            color: #333;
        }
        h1, h2 {
            color: #00695c;
        }
        section {
            margin-bottom: 30px;
        }
        ul {
            margin-top: 10px;
        }
        footer {
            margin-top: 50px;
            font-size: 0.9em;
            text-align: center;
            color: #777;
        }
    </style>
</head>
<body>
    <h1>Terms and Conditions</h1>    
    <p><strong>Effective Date:</strong> 1st Jan 2025</p>

    <section>
        <h2>1. Acceptance of Terms</h2>
        <p>By accessing or using Mashamba na nyumba, you agree to be bound by these Terms and Conditions. If you do not agree, please do not use our platform.</p>
    </section>

    <section>
        <h2>2. Purpose of the Platform</h2>
        <p>Mashamba na nyumba is a community marketplace that connects service seekers with nearby service providers based on their location and service type.</p>
    </section>

    <section>
        <h2>3. User Responsibilities</h2>
        <ul>
            <li>Provide accurate and complete information when creating or editing your profile and while updating your service provider details</li>
            <li>Do not impersonate others or misrepresent your identity or services.</li>
            <li>Respect other users and refrain from abusive or illegal behavior.</li>
        </ul>
    </section>

    <section>
        <h2>4. Service Listings</h2>
        <ul>
            <li>All service listings must be truthful and lawful.</li>
            <li>Mashamba na nyumba reserves the right to remove or suspend any listing that violates these terms or appears fraudulent.</li>
        </ul>
    </section>

    <section>
        <h2>5. Token System</h2>
        <ul>
            <li>Access to contact details of service providers and the listing for the service provider details may require tokens.</li>
            <li>Tokens are virtual access rights and are not redeemable for cash.</li>
            <li>Any abuse of the token system may lead to account suspension.</li>
        </ul>
    </section>

    <section>
        <h2>6. Privacy and Data</h2>
        <p>We collect user data (such as location, phone number, Email and hashed_password) strictly for service matching and user verification. Your data will not be shared with third parties without your consent.</p>
    </section>

    <section>
        <h2>7. Account Termination</h2>
        <p>We reserve the right to suspend or delete user accounts that violate our terms, spread false information, or abuse the platform.</p>
    </section>

    <section>
        <h2>8. Limitation of Liability</h2>
        <p>Mashamba na nyumba acts as a listing and matching platform and is not liable for the conduct or quality of services offered by listed providers. Users engage with each other at their own discretion and risk.</p>
    </section>

    <section>
        <h2>9. Intellectual Property</h2>
        <p>The Mashamba na nyumba name, logo, and system code are protected and may not be copied or reused without written permission.</p>
    </section>

    <section>
        <h2>10. Modifications to Terms</h2>
        <p>We may revise these Terms and Conditions at any time. Users will be notified of any major updates via the platform.</p>
    </section>

    <section>
        <h2>11. Contact Us</h2>
        <p>If you have any questions or concerns about these Terms, contact us at:</p>
        <ul>
            <li>Phone/WhatsApp: 0701207062</li>
            <li>Website: www.pigasimu.co.ke</li>
        </ul>
    </section>
</body>
</html>
"""  
 
docs_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Mashamba na nyumba - Documentation</title>
        
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: 'Segoe UI', sans-serif;
            background-color: #f5fdf7;
            color: #2e2e2e;
            line-height: 1.7;
            padding: 20px;
        }
        h1, h2, h3 {
            color: #388e3c;
        }
        code {
            background: #e0f2f1;
            padding: 2px 5px;
            border-radius: 4px;
        }
        section {
            margin-bottom: 40px;
        }
        ul {
            margin-top: 10px;
        }
        a {
            color: #2e7d32;
            text-decoration: none;
        }
        a:hover {
            text-decoration: underline;
        }
        footer {
            margin-top: 50px;
            font-size: 0.9em;
            color: #888;
            text-align: center;
        }
    </style>
</head>
<body>
    <h1>Mashamba na nyumba Platform Documentation</h1>

    <section>
        <h2>1. Overview</h2>
        <p>Mashamba na nyumba is a proximity-based service marketplace where users can register as either service providers or clients. The platform enables clients to discover, contact, and engage verified service providers based on location and category.</p>
    </section>

    <section>
        <h2>2. User Roles</h2>
        <ul>
            <li><strong>Service Provider:</strong> Registers, selects service categories and redeems token units and gets listed in search results.</li>
            <li><strong>Client:</strong> Searches for nearby providers and redeems tokens to view contact details.</li>
            <li><strong>Admin:</strong> Manages the platform, users, categories, and token issues.</li>
        </ul>
    </section>

    <section>
        <h2>3. Token System</h2>
        <ul>
            <li>Clients must purchase token units to access provider contact information or submit service contacts.</li>
            <li>Each contact view or listing deducts one unit of tokens (KES 2).</li>
            <li>Admin can adjust token values, distribute tokens, or reset balances.</li>
        </ul>
    </section>

    <section>
        <h2>4. Registration & Login</h2>
        <ul>
            <li>Users register via a simple form with username, contact, category, and (hidden location values).</li>
            <li>All login sessions are secured with session-based cookies and rate limiting.</li>
        </ul>
    </section>

    <section>
        <h2>5. Searching & Filtering</h2>
        <ul>
            <li>Clients can filter service providers based on distance, category, and location.</li>
            <li>Search results are sorted by category, proximity and distance.</li>
        </ul>
    </section>

    <section>
        <h2>6. Admin Panel</h2>
        <p>Accessible at <code>/admin/login</code>. Key functions:</p>
        <ul>
            <li>Login with secure admin credentials.</li>
            <li>View, search, and manage registered users.</li>
            <li>Set and reset token balances.</li>
            <li>Manage allowed categories, regions, and user bans.</li>
            <li>Monitor access logs, errors, and platform usage.</li>
        </ul>
    </section>

    <section>
        <h2>7. API Endpoints</h2>
        <p>These routes are secured and accessed via browser or client app:</p>
        <ul>
            <li><code>/</code> - Homepage / Landing page</li>
            <li><code>/register</code> - User registration form</li>
            <li><code>/login</code> - Login form</li>
            <li><code>/logout</code> - Session reset and logout</li>
            <li><code>/search</code> - Dynamic filter form for clients</li>
            <li><code>/redeem</code> - Token redemption and contact reveal</li>
            <li><code>/admin/login</code> - Admin portal access</li>
            <li><code>/privacy</code> - Privacy policy page</li>
            <li><code>/terms</code> - Terms and Conditions</li>
            <li><code>/docs</code> - This documentation</li>
        </ul>
    </section>

    <section>
        <h2>8. Security Features</h2>
        <ul>
            <li>CSRF protection on all forms.</li>
            <li>Rate limiting on login and sensitive routes.</li>
            <li>Session clearing when switching between roles.</li>
            <li>Audit logging for key admin actions.</li>
        </ul>
    </section>

    <section>
        <h2>9. Deployment & Hosting</h2>
        <ul>
            <li>Designed for deployment on Render, Railway, or Fly.io.</li>
            <li>Uses PostgreSQL with schema separation.</li>
            <li>Sessions and cookies configured for secure environments.</li>
        </ul>
    </section>

    <section>
        <h2>10. Support</h2>
        <p>If you have questions, issues, or wish to report a bug, contact our support:</p>
        <ul>
            <li>WhatsApp: 0701207062</li>
        </ul>
    </section>
</body>
</html>
"""

privacy_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Mashamba na nyumba - Privacy Policy</title>    
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {
            font-family: 'Segoe UI', sans-serif;
            margin: 0;
            padding: 20px;
            background: linear-gradient(to right, #ffffff, #f1f8e9);
            color: #333;
        }
        h1, h2 {
            color: #558b2f;
        }
        section {
            margin-bottom: 30px;
        }
        ul {
            margin-top: 10px;
        }
        footer {
            margin-top: 50px;
            font-size: 0.9em;
            text-align: center;
            color: #777;
        }
    </style>
</head>
<body>
    <h1>Privacy Policy</h1>

    <p><strong>Effective Date:</strong> 1st Jan 2025</p>

    <section>
        <h2>1. Introduction</h2>
        <p>At Mashamba na nyumba, we value your privacy. This policy outlines how we collect, use, and protect your personal information while using our platform.</p>
    </section>

    <section>
        <h2>2. Information We Collect</h2>
        <ul>
            <li><strong>Personal Data:</strong> phone number, email, hashed_password, location, and service categories.</li>
            <li><strong>Device and Usage Data:</strong> IP address, browser type, device information, and pages visited.</li>
        </ul>
    </section>

    <section>
        <h2>3. How We Use Your Information</h2>
        <ul>
            <li>To match service seekers with nearby service providers.</li>
            <li>To manage user profiles and update contact information.</li>
            <li>To improve our services and user experience.</li>
            <li>To send important account or service-related communications.</li>
        </ul>
    </section>

    <section>
        <h2>4. Sharing of Information</h2>
        <p>We do not sell or rent your personal information. Your information is only shared:</p>
        <ul>
            <li>With other users, when they redeem tokens to view your contact.</li>
            <li>With trusted service providers strictly for operating our platform (e.g., SMS or email delivery).</li>
            <li>When required by law or to protect our legal rights.</li>
        </ul>
    </section>

    <section>
        <h2>5. Data Security</h2>
        <p>We implement strong security measures to protect your data from unauthorized access, loss, or alteration. However, no method of data transmission is 100% secure.</p>
    </section>

    <section>
        <h2>6. Your Rights</h2>
        <ul>
            <li>You can request to access, correct, or delete your personal data.</li>
            <li>You may deactivate your account at any time by contacting our support team.</li>
            <li>You can opt out of promotional messages by following the unsubscribe link or contacting us directly.</li>
        </ul>
    </section>

    <section>
        <h2>7. Children's Privacy</h2>
        <p>Mashamba na nyumba does not knowingly collect personal data from users under the age of 18. If we become aware of such data, we will delete it promptly.</p>
    </section>

    <section>
        <h2>8. Updates to This Policy</h2>
        <p>We may update this Privacy Policy to reflect changes to our practices. When we do, we will notify users via the platform or email.</p>
    </section>

    <section id="business-data-policy">
        <h2>Collection of Publicly Available Business Information</h2>
        <p>
            As part of our Mashamba na nyumba business directory strategy, we may collect and display 
            publicly available business contact information, such as <strong>business names, telephone numbers, 
            and service descriptions</strong>, that are openly displayed on signboards, banners, shops, 
            or roadside advertisements.
        </p>

        <h3>Purpose</h3>
        <p>
            This data is collected to improve the accessibility and reach of local businesses 
            to users of the Mashamba na nyumba app and website. It supplements — not replaces — the 
            already available methods of user-submitted listings.
        </p>

        <h3>Lawful Basis</h3>
        <p>
            This activity is carried out under the principles of <strong>legitimate interest</strong> and 
            <strong>public interest</strong> as defined under the <em>Kenya Data Protection Act, 2019</em>. 
            Only business-related information that is <strong>already public</strong> is collected.
        </p>

        <h3>Use Limitations</h3>
        <ul>
            <li>We do <strong>not</strong> use this data for unsolicited marketing or spam.</li>
            <li>We do <strong>not</strong> sell or share this data with third parties.</li>
            <li>We do <strong>not</strong> collect private or confidential information without explicit consent.</li>
        </ul>

        <h3>Corrections and Removal Requests</h3>
        <p>
            Business owners have the right to request corrections or removal of their information 
            from our directory. To do so, kindly contact us at:
            <br />
            📧 <a href="mailto:jamesochuna37@gnail.com">jamesochuna37@gnail.com</a><br />
            📞 <a href="tel:+254701207062">0701 207 062</a>
        </p>

        <h3>Your Rights</h3>
        <p>
            All users have the right to access, correct, or request deletion of personal or business 
            data listed on our platform. We will process such requests within 7 working days.
        </p>
    </section>    

    <section>
        <h2>9. Contact Us</h2>
        <p>If you have any questions or concerns about this Privacy Policy, please contact:</p>
        <ul>
            <li>Phone/WhatsApp: 0701207062</li>
            <li>Website: <a href="https://pigasimu.co.ke">https://pigasimu.co.ke</a></li>
        </ul>
    </section>
</body>
</html>
"""   



login_html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Soko Jirani Portals - Login</title>
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(to bottom right, #388e3c, #4caf50);
            color: #fff;
            margin: 0;
            padding: 0;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
        }
                
        .container {
            background: linear-gradient(145deg, #f0f4ff, #dce7f7);
            padding: 30px 25px;
            border-radius: 12px;
            box-shadow: 0 8px 25px rgba(0, 0, 0, 0.2);
            max-width: 350px;
            width: 90%;
            text-align: center;
            color: #333;
        }

        h1 {
            margin-bottom: 20px;
            font-size: 28px;
            color: #1a237e;
            font-weight: 600;
        }        

        label {
            display: block;
            margin: 15px 0 5px;
            text-align: left;
        }

        input[type="text"], input[type="password"] {
            width: 100%;
            padding: 10px;
            border-radius: 6px;
            border: none;
            margin-bottom: 15px;
        }

        input[type="submit"] {
            background-color: #1976d2;
            color: white;
            padding: 10px;
            border: none;
            border-radius: 6px;
            width: 100%;
            cursor: pointer;
            font-weight: bold;
            font-size: 16px;
        }

        input[type="submit"]:hover {
            background-color: #125aaa;
        }

        .link-button {
            display: block;
            margin: 15px 0;
            text-decoration: none;
            color: white;
            background-color: #1976d2;
            padding: 10px;
            border-radius: 6px;
            font-weight: bold;
        }

        .link-button:hover {
            background-color: #125aaa;
        }

        ul {
            list-style-type: none;
            padding: 0;
            margin-top: 15px;
        }

        li {
            margin-bottom: 8px;
            color: #ffeb3b;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Login</h1>
        <form method="post">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <label for="username">Phone Number:</label>
            <input type="text" name="username" required>
            <label for="password">Password:</label>
            <input type="password" name="password" required>
            <input type="submit" value="Login">
        </form>
        <a href="{{ url_for('index') }}" class="link-button">Back to Home</a>

        {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
        <ul>
            {% for category, message in messages %}
            <li class="{{ category }}">{{ message }}</li>
            {% endfor %}
        </ul>
        {% endif %}
        {% endwith %}
    </div>
</body>
</html>
"""

unlock_account_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Unlock Account</title>      
    
    <style>
        body {
            font-family: Arial, sans-serif;
            background-color: #f4f6f7;
            color: #333333;
            text-align: center;
            margin: 0;
            padding: 0;
        }
        h1 {
            background-color: #4caf50;
            color: white;
            padding: 20px;
            margin-bottom: 40px;
            border-bottom: 5px solid #388e3c;
        }
        .form-container {
            background-color: #ffffff;
            padding: 30px;
            border-radius: 10px;
            box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
            display: inline-block;
            width: 100%;
            max-width: 400px;
        }
        label {
            font-size: 1.2em;
            color: #333333;
            margin-bottom: 10px;
            display: block;
            text-align: left;
        }
        input[type="text"] {
            padding: 10px;
            margin-bottom: 20px;
            border-radius: 5px;
            border: 1px solid #cccccc;
            width: calc(100% - 22px);
            font-size: 1em;
        }
        input[type="submit"] {
            background-color: #1976d2;
            color: white;
            padding: 12px;
            font-size: 1.2em;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            width: 100%;
        }
        input[type="submit"]:hover {
            background-color: #1565c0;
        }
        .flash-message {
            background-color: #ffcccc;
            color: #cc0000;
            padding: 10px;
            margin-bottom: 20px;
            border: 1px solid #ff9999;
            border-radius: 5px;
        }
        .flash-success {
            background-color: #ccffcc;
            color: #006600;
            padding: 10px;
            margin-bottom: 20px;
            border: 1px solid #99ff99;
            border-radius: 5px;
        }
    </style>
</head>
<body>
    <h1>Unlock Your Account</h1>       
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="flash-message {{ 'flash-' + category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}
    <div class="form-container">
        <form method="post">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <label for="username">Enter Your Username:</label>
            <input type="text" name="username" required><br>
            <label for="token">Enter Purchased Token:</label>
            <input type="text" name="token" required><br>
            <input type="submit" value="Submit Token">
        </form>
    </div>
</body>
</html>
"""

admin_reset_password_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Password Reset</title>
    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
</head>
<body>
    <div class="container mt-5">
        <h2 class="mb-4">Admin Password Reset</h2>
        <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">        
            <!-- Changed label -->
            <div class="form-group">
                <label for="username">Phone Number (or Username):</label>
                <input type="text" name="username" id="username" class="form-control" required>
            </div>
            <div class="form-group">
                <label for="new_password">New Password:</label>
                <input type="password" name="new_password" id="new_password" class="form-control" required>
            </div>
            <button type="submit" class="btn btn-primary">Reset Password</button>
        </form>
    </div>
</body>
</html>
"""

change_password_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Change Password Page">       
    
    <title>Change Password</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #f4f4f4;
        }
        .container {
            width: 100%;
            max-width: 500px;
            margin: 50px auto;
            padding: 20px;
            background-color: #ffffff;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
        }
        h1 {
            text-align: center;
            color: #333333;
        }
        form {
            margin-top: 20px;
        }
        label {
            display: block;
            margin-bottom: 10px;
            font-weight: bold;
        }
        input[type="password"], input[type="submit"] {
            width: 100%;
            padding: 12px;
            margin-bottom: 20px;
            border-radius: 5px;
            border: 1px solid #ccc;
            font-size: 16px;
        }
        input[type="submit"] {
            background-color: #28a745;
            color: white;
            cursor: pointer;
            font-weight: bold;
        }
        input[type="submit"]:hover {
            background-color: #218838;
        }
        .message {
            text-align: center;
            margin-top: 20px;
        }
        .alert {
            padding: 15px;
            background-color: #f44336;
            color: white;
            border-radius: 5px;
            text-align: center;
            margin-bottom: 15px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Change Password</h1>                        
        <!-- Flash message for feedback -->            
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        <form action="{{ url_for('change_password') }}" method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <label for="current_password">Current Password</label>
            <input type="password" name="current_password" id="current_password" required placeholder="Enter current password">

            <label for="new_password">New Password</label>
            <input type="password" name="new_password" id="new_password" required placeholder="Enter new password">

            <label for="confirm_password">Confirm New Password</label>
            <input type="password" name="confirm_password" id="confirm_password" required placeholder="Confirm new password">

            <input type="submit" value="Change Password">
        </form>
        
        <div class="back-button">
            <a href="{{ url_for('dashboard') }}">Back to Dashboard</a>
        </div>
    </div>        

        <div class="message">
            <p>Ensure your new password is at least 8 characters long.</p>
        </div>
    </div>
</body>
</html>
"""

add_token_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Add Tokens - Admin</title>
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #f4f6f9;
            color: #343a40;
        }
        .container {
            width: 80%;
            max-width: 600px;
            margin: 30px auto;
            padding: 20px;
            background-color: #ffffff;
            box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
            border-radius: 10px;
        }
        h1 {
            text-align: center;
            font-size: 2.5em;
            margin-bottom: 20px;
            color: #007bff;
        }
        .info-box {
            background: #e7f3ff;
            border: 1px solid #b3d9ff;
            border-radius: 5px;
            padding: 15px;
            margin-bottom: 20px;
        }
        form {
            background-color: #fff;
            padding: 20px;
            border-radius: 10px;
            box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
        }
        label {
            font-size: 1.1em;
            margin-bottom: 5px;
            display: block;
            color: #343a40;
            font-weight: bold;
        }
        input[type="text"],
        input[type="number"],
        select {
            width: 100%;
            padding: 10px;
            margin-bottom: 20px;
            border: 1px solid #ced4da;
            border-radius: 5px;
            font-size: 1em;
            box-sizing: border-box;
        }
        input[type="submit"] {
            background-color: #007bff;
            color: white;
            border: none;
            padding: 12px;
            cursor: pointer;
            font-size: 1.1em;
            width: 100%;
            border-radius: 5px;
        }
        input[type="submit"]:hover {
            background-color: #0056b3;
        }
        .token-info {
            background: #d4edda;
            border: 1px solid #c3e6cb;
            border-radius: 5px;
            padding: 10px;
            margin: 10px 0;
        }
        .flashes {
            margin-bottom: 20px;
            padding: 10px;
            border-radius: 5px;
        }
        .flashes .success {
            background-color: #d4edda;
            color: #155724;
            padding: 10px;
            border-radius: 5px;
        }
        .flashes .danger {
            background-color: #f8d7da;
            color: #721c24;
            padding: 10px;
            border-radius: 5px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Manual Token Addition</h1>

        <div class="info-box">
            <strong>M-Pesa Alternative System</strong>
            <p>Use this when M-Pesa is down. Tokens work exactly like M-Pesa purchases.</p>
            <div class="token-info">
                <strong>Pricing:</strong> KES 2 = 1 token<br>
                <strong>Service Cost:</strong> 1 token per search/submission<br>
                <strong>Validity:</strong> 30 days
            </div>
        </div>

        <!-- Display Flash Messages -->
        {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
        <div class="flashes">
            {% for category, message in messages %}
            <div class="{{ category }}">{{ message }}</div>
            {% endfor %}
        </div>
        {% endif %}
        {% endwith %}

        <form action="{{ url_for('add_token') }}" method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            
            <label for="username">User Phone Number:</label>
            <input type="text" id="username" name="username" placeholder="e.g., 0712345678" required>
            
            <label for="amount">Payment Amount (KES):</label>
            <input type="number" id="amount" name="amount" min="2" step="2" placeholder="e.g., 20 for 10 tokens" required>
            
            <label for="payment_method">Payment Method:</label>
            <select id="payment_method" name="payment_method" required>
                <option value="">Select payment method</option>
                <option value="cash">Cash</option>
                <option value="bank_transfer">Bank Transfer</option>
                <option value="other">Other</option>
            </select>
            
            <input type="submit" value="Add Tokens">
        </form>
        
        <div style="margin-top: 20px; text-align: center;">
            <a href="{{ url_for('dashboard') }}">Back to Dashboard</a>
        </div>
    </div>
</body>
</html>
"""

submit_service_provider_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Submit Service Provider Information">
    <title>Submit Service Provider</title>      
    <style>
        body {
            font-family: Arial, sans-serif;
            background-color: #f0f0f0;
            margin: 0;
            padding: 0;
        }
        .container {
            max-width: 500px;
            margin: 50px auto;
            padding: 20px;
            background-color: #ffffff;
            border-radius: 10px;
            box-shadow: 0 0 10px rgba(0, 0, 0, 0.1);
        }
        h1 {
            text-align: center;
            color: #333333;
        }
        form {
            margin-top: 20px;
        }
        label {
            font-weight: bold;
            display: block;
            margin-bottom: 10px;
        }
        select, input[type="tel"], input[type="password"], input[type="submit"], button {
            width: 100%;
            padding: 12px;
            margin-bottom: 20px;
            border-radius: 5px;
            border: 1px solid #ccc;
            font-size: 16px;
        }
        input[type="submit"], button {
            background-color: #007bff;
            color: white;
            cursor: pointer;
            font-weight: bold;
        }
        input[type="submit"]:hover, button:hover {
            background-color: #0056b3;
        }
        button:disabled {
            background-color: #6c757d;
            cursor: not-allowed;
        }
        .alert {
            padding: 15px;
            background-color: #f44336;
            color: white;
            border-radius: 5px;
            text-align: center;
            margin-bottom: 15px;
        }
        .success {
            background-color: #28a745;
        }
        .warning {
            background-color: #ffc107;
            color: #000;
        }
        .location-status {
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 15px;
            text-align: center;
            font-weight: bold;
        }
        .location-active {
            background-color: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .location-inactive {
            background-color: #fff3cd;
            color: #856404;
            border: 1px solid #ffeaa7;
        }
        .location-error {
            background-color: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        .back-to-dashboard {
            display: block;
            text-align: center;
            margin-top: 20px;
            font-size: 16px;
        }
        .back-to-dashboard a {
            text-decoration: none;
            color: #007bff;
        }
        .back-to-dashboard a:hover {
            color: #0056b3;
        }
        .coordinates-display {
            background-color: #f8f9fa;
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 15px;
            font-family: monospace;
            text-align: center;
        }
        .user-info {
            background-color: #e7f3ff;
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 15px;
            text-align: center;
            border-left: 4px solid #007bff;
        }
        .auto-fill-notice {
            background-color: #d1ecf1;
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 15px;
            text-align: center;
            border-left: 4px solid #17a2b8;
        }
        .registration-success {
            background-color: #e8f5e8;
            border: 2px solid #4caf50;
            border-radius: 10px;
            padding: 20px;
            margin: 20px 0;
            text-align: center;
        }
        .location-details {
            background-color: #fff3cd;
            border: 1px solid #ffc107;
            border-radius: 5px;
            padding: 15px;
            margin: 10px 0;
        }
        .token-info {
            background-color: #e7f3ff;
            border: 1px solid #007bff;
            border-radius: 5px;
            padding: 10px;
            margin: 10px 0;
            text-align: center;
        }
        .service-area-info {
            background-color: #f0f8ff;
            border: 1px solid #4682b4;
            border-radius: 5px;
            padding: 15px;
            margin: 15px 0;
        }
        .coordinates-preview {
            font-family: monospace;
            background-color: #f8f9fa;
            padding: 8px;
            border-radius: 4px;
            margin: 5px 0;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Submit Service Provider Information</h1>        
        <!-- Mashamba na Nyumba -->              
        <!-- Flash messages for feedback -->
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        <!-- User Info Display -->
        <div id="userInfo" class="user-info" style="display: none;">
            Welcome back! <span id="userName">User</span>
        </div>

        <!-- Auto-fill Notice -->
        <div id="autoFillNotice" class="auto-fill-notice" style="display: none;">
            Your information has been auto-filled
        </div>

        <!-- Registration Success Display -->
        <div id="registrationSuccess" class="registration-success" style="display: none;">
            <h3>✅ Registration Successful!</h3>
            <div class="location-details">
                <strong>Service Type:</strong> <span id="successServiceType">-</span><br>
                <strong>Phone Number:</strong> <span id="successPhoneNumber">-</span><br>
                <strong>Location Registered:</strong> <span id="successLocation">Your current location</span>
            </div>
            <div class="service-area-info">
                <h4>📍 Service Area Registered</h4>
                <p>Your service will be available to clients within your area.</p>
                <div class="coordinates-preview">
                    Location accuracy: <span id="locationAccuracy">-</span> meters
                </div>
            </div>
            <div class="token-info">
                <strong>2 tokens deducted from your account</strong><br>
                <small>Your service is now visible to nearby clients</small>
            </div>
        </div>

        <!-- Location Status Display -->
        <div id="locationStatus" class="location-status location-inactive">
            📍 Location: Waiting for permission...
        </div>

        <!-- Coordinates Display - HIDDEN AS REQUESTED -->
        <div id="coordinatesDisplay" class="coordinates-display" style="display: none;">
            Location verified successfully
        </div>

        <!-- Location Accuracy Info -->
        <div id="locationAccuracyInfo" class="service-area-info" style="display: none;">
            <h4>📍 Your Service Coverage Area</h4>
            <p>Based on your current location accuracy of <span id="accuracyValue">-</span> meters, 
               clients within this range will be able to find your service.</p>
            <p><strong>Better location accuracy = Better client matching</strong></p>
        </div>

        <form action="{{ url_for('submit_service_provider') }}" method="POST" id="submitServiceProviderForm">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">        
            
            <label for="service_type">Select Service Type</label>
            <select name="service_type" id="service_type" required>
                <option value="" disabled selected>Select a service</option>
                <option value="Shamba">Shamba</option>
                <option value="Nyumba">Nyumba</option>     
            </select>           

            <label for="phone_number">Phone Number</label>
            <input type="tel" name="phone_number" id="phone_number" pattern="\\d{10}" inputmode="tel" required placeholder="Enter your 10-digit phone number">

            <label for="password">Password</label>
            <input type="password" name="password" id="password" minlength="8" required placeholder="Enter your password">

            <!-- Automatically filled longitude and latitude -->
            <input type="hidden" name="longitude" id="longitude">
            <input type="hidden" name="latitude" id="latitude">

            <!-- Auto-filled user credentials -->
            <input type="hidden" name="user_id" id="user_id">
            <input type="hidden" name="auto_auth" id="auto_auth" value="true">

            <!-- Location Accuracy -->
            <input type="hidden" name="location_accuracy" id="location_accuracy">

            <!-- Enable Location Button -->
            <button type="button" id="enableLocationBtn">📍 Enable Location Services</button>

            <input type="submit" id="submitBtn" value="Register as Service Provider" disabled>

            <div class="token-info">
                <strong>Cost: 2 tokens</strong><br>
                <small>Tokens will be deducted upon successful registration</small>
            </div>
        </form>

        <div class="message">
            <p><strong>Location is required:</strong> We need your current location to register you as a service provider in the correct area.</p>
            <p><strong>Service Area:</strong> Clients will find you based on your registered location and service type.</p>
        </div>

        <!-- Back to Dashboard link -->
        <div class="back-to-dashboard">
            <a href="{{ url_for('dashboard') }}">Back to Dashboard</a>
        </div>
    </div>

    <script>
        let hasLocation = false;
        let currentCoordinates = null;
        let isUserLoggedIn = false;
        let userData = null;

        document.addEventListener('DOMContentLoaded', function() {
            const locationStatus = document.getElementById('locationStatus');
            const coordinatesDisplay = document.getElementById('coordinatesDisplay');
            const enableLocationBtn = document.getElementById('enableLocationBtn');
            const submitBtn = document.getElementById('submitBtn');
            const longitudeInput = document.getElementById('longitude');
            const latitudeInput = document.getElementById('latitude');
            const userInfo = document.getElementById('userInfo');
            const userName = document.getElementById('userName');
            const autoFillNotice = document.getElementById('autoFillNotice');
            const userIdInput = document.getElementById('user_id');
            const phoneInput = document.getElementById('phone_number');
            const passwordInput = document.getElementById('password');
            const locationAccuracyInput = document.getElementById('location_accuracy');
            const locationAccuracyInfo = document.getElementById('locationAccuracyInfo');
            const accuracyValue = document.getElementById('accuracyValue');
            const registrationSuccess = document.getElementById('registrationSuccess');
            const successServiceType = document.getElementById('successServiceType');
            const successPhoneNumber = document.getElementById('successPhoneNumber');
            const locationAccuracyDisplay = document.getElementById('locationAccuracy');

            // Check user authentication and auto-fill credentials
            checkUserAuthentication();

            // Process any successful registration messages
            processSuccessMessages();

            // Check if geolocation is supported
            if (!navigator.geolocation) {
                locationStatus.textContent = '📍 Location: Not supported by your browser';
                locationStatus.className = 'location-status location-error';
                enableLocationBtn.disabled = true;
                enableLocationBtn.textContent = 'Location Not Supported';
                return;
            }

            // Enable Location Button Click Handler
            enableLocationBtn.addEventListener('click', function() {
                requestLocationAccess();
            });

            function checkUserAuthentication() {
                // Check for existing session or stored credentials
                const userSession = localStorage.getItem('user_session');
                const authToken = localStorage.getItem('auth_token');
                const userCredentials = localStorage.getItem('user_credentials');
                
                if (userSession || authToken) {
                    try {
                        userData = userSession ? JSON.parse(userSession) : null;
                        isUserLoggedIn = true;
                        
                        // Update UI for logged-in user
                        userInfo.style.display = 'block';
                        if (userData && userData.name) {
                            userName.textContent = userData.name;
                        }
                        
                        // Auto-fill user ID if available
                        if (userData && userData.id) {
                            userIdInput.value = userData.id;
                        }
                        
                        console.log('User auto-authenticated');
                    } catch (e) {
                        console.log('No valid user session found');
                    }
                }
                
                // Auto-fill credentials if available
                if (userCredentials) {
                    try {
                        const credentials = JSON.parse(userCredentials);
                        if (credentials.phone) {
                            phoneInput.value = credentials.phone;
                        }
                        // Note: Passwords should not be stored in localStorage for security reasons
                        // In a real implementation, use secure tokens instead
                        
                        autoFillNotice.style.display = 'block';
                    } catch (e) {
                        console.log('No stored credentials found');
                    }
                }
                
                // Additional check for server-side session
                fetch('/check-auth', {
                    method: 'GET',
                    credentials: 'same-origin'
                })
                .then(response => response.json())
                .then(data => {
                    if (data.authenticated && data.user) {
                        isUserLoggedIn = true;
                        userData = data.user;
                        userInfo.style.display = 'block';
                        userName.textContent = userData.name || 'User';
                        if (userData.id) {
                            userIdInput.value = userData.id;
                        }
                        if (userData.phone) {
                            phoneInput.value = userData.phone;
                            autoFillNotice.style.display = 'block';
                        }
                    }
                })
                .catch(error => {
                    console.log('Auth check failed, proceeding without auto-login');
                });
            }

            function processSuccessMessages() {
                const flashMessages = document.querySelectorAll('.alert');
                flashMessages.forEach(message => {
                    const messageText = message.textContent;
                    
                    // Check if this is a successful registration message
                    if (message.classList.contains('success') && 
                        (messageText.includes('successfully') || messageText.includes('registered') || messageText.includes('updated'))) {
                        
                        // Extract information from the form
                        const serviceType = document.getElementById('service_type').value;
                        const phoneNumber = document.getElementById('phone_number').value;
                        
                        // Display success section
                        successServiceType.textContent = serviceType || 'Service';
                        successPhoneNumber.textContent = phoneNumber || 'Your number';
                        
                        // Show the success section
                        registrationSuccess.style.display = 'block';
                        
                        // Hide the form
                        document.getElementById('submitServiceProviderForm').style.display = 'none';
                        
                        // Scroll to success message
                        registrationSuccess.scrollIntoView({ behavior: 'smooth' });
                        
                        // Hide the original flash message after processing
                        setTimeout(() => {
                            message.style.display = 'none';
                        }, 1000);
                    }
                });
            }

            function requestLocationAccess() {
                locationStatus.textContent = '📍 Location: Requesting permission...';
                locationStatus.className = 'location-status location-inactive';
                enableLocationBtn.disabled = true;
                enableLocationBtn.textContent = '🔄 Detecting Location...';
                
                // Request high accuracy location
                navigator.geolocation.getCurrentPosition(
                    // Success callback
                    function(position) {
                        const lat = position.coords.latitude;
                        const lng = position.coords.longitude;
                        const accuracy = position.coords.accuracy;
                        
                        // Store coordinates and accuracy
                        longitudeInput.value = lng;
                        latitudeInput.value = lat;
                        locationAccuracyInput.value = accuracy;
                        currentCoordinates = { lat, lng, accuracy };
                        
                        // Update UI
                        const accuracyLevel = accuracy < 50 ? 'High' : accuracy < 200 ? 'Medium' : 'Low';
                        locationStatus.textContent = `📍 Location: Active (${accuracyLevel} accuracy)`;
                        locationStatus.className = 'location-status location-active';
                        
                        // Update accuracy display
                        accuracyValue.textContent = Math.round(accuracy);
                        locationAccuracyDisplay.textContent = Math.round(accuracy) + ' meters';
                        
                        // Show location info and coordinates display
                        coordinatesDisplay.style.display = 'block';
                        locationAccuracyInfo.style.display = 'block';
                        
                        enableLocationBtn.textContent = '🔄 Update Location';
                        enableLocationBtn.disabled = false;
                        submitBtn.disabled = false;
                        hasLocation = true;
                        
                        // Start watching position for updates
                        startWatchingPosition();
                    },
                    // Error callback
                    function(error) {
                        handleLocationError(error);
                    },
                    // Options - high accuracy for better service matching
                    {
                        enableHighAccuracy: true,
                        timeout: 15000,
                        maximumAge: 60000
                    }
                );
            }

            function startWatchingPosition() {
                if ('geolocation' in navigator) {
                    navigator.geolocation.watchPosition(
                        function(position) {
                            const lat = position.coords.latitude;
                            const lng = position.coords.longitude;
                            const accuracy = position.coords.accuracy;
                            
                            // Update coordinates if they changed significantly or accuracy improved
                            if (currentCoordinates && 
                                (Math.abs(currentCoordinates.lat - lat) > 0.0001 || 
                                 Math.abs(currentCoordinates.lng - lng) > 0.0001 ||
                                 Math.abs(currentCoordinates.accuracy - accuracy) > 10)) {
                                
                                longitudeInput.value = lng;
                                latitudeInput.value = lat;
                                locationAccuracyInput.value = accuracy;
                                currentCoordinates = { lat, lng, accuracy };
                                
                                // Update accuracy display
                                accuracyValue.textContent = Math.round(accuracy);
                                locationAccuracyDisplay.textContent = Math.round(accuracy) + ' meters';
                                
                                const accuracyLevel = accuracy < 50 ? 'High' : accuracy < 200 ? 'Medium' : 'Low';
                                locationStatus.textContent = `📍 Location: Active (${accuracyLevel} accuracy)`;
                            }
                        },
                        function(error) {
                            console.log('Location watch error:', error);
                        },
                        {
                            enableHighAccuracy: true,
                            maximumAge: 30000
                        }
                    );
                }
            }

            function handleLocationError(error) {
                let errorMessage = '📍 Location: ';
                let userMessage = '';
                
                switch(error.code) {
                    case error.PERMISSION_DENIED:
                        errorMessage += 'Permission denied.';
                        userMessage = 'Please allow location access in your browser settings to register as a service provider.';
                        break;
                    case error.POSITION_UNAVAILABLE:
                        errorMessage += 'Location information unavailable.';
                        userMessage = 'Please check your device location settings and try again.';
                        break;
                    case error.TIMEOUT:
                        errorMessage += 'Location request timed out.';
                        userMessage = 'Location detection took too long. Please try again.';
                        break;
                    default:
                        errorMessage += 'An unknown error occurred.';
                        userMessage = 'Please try enabling location again.';
                        break;
                }
                
                locationStatus.textContent = errorMessage;
                locationStatus.className = 'location-status location-error';
                enableLocationBtn.textContent = '📍 Retry Location Access';
                enableLocationBtn.disabled = false;
                submitBtn.disabled = true;
                hasLocation = false;
                
                if (userMessage) {
                    alert(userMessage);
                }
            }

            // Prevent form submission if location is not available
            document.getElementById('submitServiceProviderForm').addEventListener('submit', function(event) {
                if (!hasLocation || !longitudeInput.value || !latitudeInput.value) {
                    event.preventDefault();
                    alert('Please enable location services before submitting.');
                    enableLocationBtn.scrollIntoView({ behavior: 'smooth' });
                } else {
                    // Show loading state
                    submitBtn.disabled = true;
                    submitBtn.value = 'Registering... Please wait';
                }
            });

            // Try to get location automatically on page load (with user permission)
            setTimeout(() => {
                if (!hasLocation) {
                    navigator.geolocation.getCurrentPosition(
                        function(position) {
                            // If we get location automatically, update UI
                            const lat = position.coords.latitude;
                            const lng = position.coords.longitude;
                            const accuracy = position.coords.accuracy;
                            
                            longitudeInput.value = lng;
                            latitudeInput.value = lat;
                            locationAccuracyInput.value = accuracy;
                            currentCoordinates = { lat, lng, accuracy };
                            
                            const accuracyLevel = accuracy < 50 ? 'High' : accuracy < 200 ? 'Medium' : 'Low';
                            locationStatus.textContent = `📍 Location: Auto-detected (${accuracyLevel} accuracy)`;
                            locationStatus.className = 'location-status location-active';
                            
                            // Update accuracy display
                            accuracyValue.textContent = Math.round(accuracy);
                            locationAccuracyDisplay.textContent = Math.round(accuracy) + ' meters';
                            
                            coordinatesDisplay.style.display = 'block';
                            locationAccuracyInfo.style.display = 'block';
                            
                            enableLocationBtn.textContent = '🔄 Update Location';
                            submitBtn.disabled = false;
                            hasLocation = true;
                        },
                        function(error) {
                            // Silent fail - user will manually enable
                            console.log('Auto-location failed, waiting for manual activation');
                        },
                        {
                            enableHighAccuracy: false,
                            timeout: 8000,
                            maximumAge: 300000
                        }
                    );
                }
            }, 1000);
        });
    </script>
</body>
</html>
"""

find_nearest_service_provider_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Find Nearest Service Provider">
    <title>Find Nearest Service Provider</title>          
    <style>
        body {
            font-family: Arial, sans-serif;
            background-color: #f0f0f0;
            margin: 0;
            padding: 0;
        }
        .container {
            max-width: 500px;
            margin: 50px auto;
            padding: 20px;
            background-color: #ffffff;
            border-radius: 10px;
            box-shadow: 0 0 10px rgba(0, 0, 0, 0.1);
        }
        h1 {
            text-align: center;
            color: #333333;
        }
        form {
            margin-top: 20px;
        }
        label {
            font-weight: bold;
            display: block;
            margin-bottom: 10px;
        }
        select, input[type="submit"], button {
            width: 100%;
            padding: 12px;
            margin-bottom: 20px;
            border-radius: 5px;
            border: 1px solid #ccc;
            font-size: 16px;
        }
        input[type="submit"], button {
            background-color: #007bff;
            color: white;
            cursor: pointer;
            font-weight: bold;
        }
        input[type="submit"]:hover, button:hover {
            background-color: #0056b3;
        }
        button:disabled, input[type="submit"]:disabled {
            background-color: #6c757d;
            cursor: not-allowed;
        }
        .alert {
            padding: 15px;
            background-color: #f44336;
            color: white;
            border-radius: 5px;
            text-align: center;
            margin-bottom: 15px;
        }
        .success {
            background-color: #28a745;
        }
        .warning {
            background-color: #ffc107;
            color: #000;
        }
        .location-status {
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 15px;
            text-align: center;
            font-weight: bold;
        }
        .location-active {
            background-color: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .location-inactive {
            background-color: #fff3cd;
            color: #856404;
            border: 1px solid #ffeaa7;
        }
        .location-error {
            background-color: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        .coordinates-display {
            background-color: #f8f9fa;
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 15px;
            font-family: monospace;
            text-align: center;
        }
        .message {
            text-align: center;
            margin-bottom: 20px;
        }
        .back-button {
            text-align: center;
            margin-top: 20px;
        }
        .back-button a {
            display: inline-block;
            padding: 10px 20px;
            background-color: #007bff;
            color: white;
            text-decoration: none;
            border-radius: 5px;
        }
        .back-button a:hover {
            background-color: #0056b3;
        }
        .accuracy-info {
            font-size: 0.9em;
            color: #6c757d;
            text-align: center;
            margin-top: -15px;
            margin-bottom: 15px;
        }
        .user-info {
            background-color: #e7f3ff;
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 15px;
            text-align: center;
            border-left: 4px solid #007bff;
        }
        .distance-results {
            background-color: #e8f5e8;
            border: 2px solid #4caf50;
            border-radius: 10px;
            padding: 20px;
            margin: 20px 0;
            text-align: center;
        }
        .distance-value {
            font-size: 2em;
            font-weight: bold;
            color: #2e7d32;
            margin: 10px 0;
        }
        .provider-info {
            background-color: #fff3cd;
            border: 1px solid #ffc107;
            border-radius: 5px;
            padding: 15px;
            margin: 10px 0;
        }
        .token-info {
            background-color: #e7f3ff;
            border: 1px solid #007bff;
            border-radius: 5px;
            padding: 10px;
            margin: 10px 0;
            text-align: center;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Find Nearest Service Provider</h1>
        <!-- Flash messages for feedback -->
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        <!-- User Info Display for Logged-in Users -->
        <div id="userInfo" class="user-info" style="display: none;">
            Welcome back! <span id="userName">User</span>
        </div>

        <!-- Location Status Display -->
        <div id="locationStatus" class="location-status location-inactive">
            📍 Location: Click button to enable
        </div>

        <!-- Coordinates Display - HIDDEN AS REQUESTED -->
        <div id="coordinatesDisplay" class="coordinates-display" style="display: none;">
            Location detected successfully
        </div>

        <!-- Distance Results Display -->
        <div id="distanceResults" class="distance-results" style="display: none;">
            <h3>📍 Nearest Provider Found</h3>
            <div class="distance-value" id="distanceValue">0.00 km</div>
            <div class="provider-info">
                <strong>Service Type:</strong> <span id="resultServiceType">-</span><br>
                <strong>Phone Number:</strong> <span id="resultPhoneNumber">-</span><br>
                <strong>Distance:</strong> <span id="resultDistance">-</span> away
            </div>
            <div class="token-info">
                <strong>2 tokens deducted from your account</strong>
            </div>
        </div>

        <form action="{{ url_for('find_nearest_service_provider') }}" method="POST" id="findServiceProviderForm">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            
            <label for="service_type">Select Service Type</label>
            <select name="service_type" id="service_type" required>
                <option value="" disabled selected>Select a service</option>
                <option value="Shamba">Shamba</option>
                <option value="Nyumba">Nyumba</option>     
            </select>

            <!-- Automatically filled longitude and latitude -->
            <input type="hidden" name="longitude" id="longitude" required>
            <input type="hidden" name="latitude" id="latitude" required>

            <!-- Auto-filled credentials for logged-in users -->
            <input type="hidden" name="user_id" id="user_id">
            <input type="hidden" name="auto_auth" id="auto_auth" value="true">

            <!-- Enable Location Button -->
            <button type="button" id="enableLocationBtn">📍 Enable Location to Search</button>

            <input type="submit" id="submitBtn" value="Find Nearest Service Provider" disabled>

            <div class="accuracy-info">
                Better location accuracy = better search results
            </div>
        </form>

        <div class="message">
            <p><strong>How it works:</strong> We use your current location to find the closest service provider in your area.</p>
            <p><strong>Cost:</strong> 2 tokens per search</p>
        </div>

        <div class="back-button">
            <a href="{{ url_for('dashboard') }}">Back to Dashboard</a>
        </div>
    </div>

    <script>
        let hasLocation = false;
        let currentCoordinates = null;
        let isUserLoggedIn = false;
        let userData = null;

        document.addEventListener('DOMContentLoaded', function() {
            const locationStatus = document.getElementById('locationStatus');
            const coordinatesDisplay = document.getElementById('coordinatesDisplay');
            const enableLocationBtn = document.getElementById('enableLocationBtn');
            const submitBtn = document.getElementById('submitBtn');
            const longitudeInput = document.getElementById('longitude');
            const latitudeInput = document.getElementById('latitude');
            const userInfo = document.getElementById('userInfo');
            const userName = document.getElementById('userName');
            const userIdInput = document.getElementById('user_id');
            const distanceResults = document.getElementById('distanceResults');
            const distanceValue = document.getElementById('distanceValue');
            const resultServiceType = document.getElementById('resultServiceType');
            const resultPhoneNumber = document.getElementById('resultPhoneNumber');
            const resultDistance = document.getElementById('resultDistance');

            // Check user authentication status
            checkUserAuthentication();

            // Check if geolocation is supported
            if (!navigator.geolocation) {
                locationStatus.textContent = '📍 Location: Not supported by your browser';
                locationStatus.className = 'location-status location-error';
                enableLocationBtn.disabled = true;
                enableLocationBtn.textContent = 'Location Not Supported';
                return;
            }

            // Enable Location Button Click Handler
            enableLocationBtn.addEventListener('click', function() {
                requestLocationAccess();
            });

            function checkUserAuthentication() {
                // Check for existing session or stored credentials
                const userSession = localStorage.getItem('user_session');
                const authToken = localStorage.getItem('auth_token');
                
                if (userSession || authToken) {
                    try {
                        userData = userSession ? JSON.parse(userSession) : null;
                        isUserLoggedIn = true;
                        
                        // Update UI for logged-in user
                        userInfo.style.display = 'block';
                        if (userData && userData.name) {
                            userName.textContent = userData.name;
                        }
                        
                        // Auto-fill user ID if available
                        if (userData && userData.id) {
                            userIdInput.value = userData.id;
                        }
                        
                        console.log('User auto-authenticated');
                    } catch (e) {
                        console.log('No valid user session found');
                    }
                }
                
                // Additional check for server-side session
                fetch('/check-auth', {
                    method: 'GET',
                    credentials: 'same-origin'
                })
                .then(response => response.json())
                .then(data => {
                    if (data.authenticated && data.user) {
                        isUserLoggedIn = true;
                        userData = data.user;
                        userInfo.style.display = 'block';
                        userName.textContent = userData.name || 'User';
                        if (userData.id) {
                            userIdInput.value = userData.id;
                        }
                    }
                })
                .catch(error => {
                    console.log('Auth check failed, proceeding without auto-login');
                });
            }

            function requestLocationAccess() {
                locationStatus.textContent = '📍 Location: Detecting your location...';
                locationStatus.className = 'location-status location-inactive';
                enableLocationBtn.disabled = true;
                enableLocationBtn.textContent = '🔄 Detecting...';
                
                // Request high accuracy location for better search results
                navigator.geolocation.getCurrentPosition(
                    // Success callback
                    function(position) {
                        const lat = position.coords.latitude;
                        const lng = position.coords.longitude;
                        const accuracy = position.coords.accuracy;
                        
                        // Store coordinates
                        longitudeInput.value = lng;
                        latitudeInput.value = lat;
                        currentCoordinates = { lat, lng, accuracy };
                        
                        // Update UI - COORDINATES HIDDEN AS REQUESTED
                        const accuracyText = accuracy < 50 ? 'High' : accuracy < 200 ? 'Medium' : 'Low';
                        locationStatus.textContent = `📍 Location: Ready (${accuracyText} accuracy)`;
                        locationStatus.className = 'location-status location-active';
                        
                        // Coordinates text removed from display
                        coordinatesDisplay.style.display = 'block';
                        
                        enableLocationBtn.textContent = '🔄 Update My Location';
                        enableLocationBtn.disabled = false;
                        submitBtn.disabled = false;
                        hasLocation = true;
                        
                        // Auto-focus the submit button for better UX
                        submitBtn.focus();
                    },
                    // Error callback
                    function(error) {
                        handleLocationError(error);
                    },
                    // Options - high accuracy for better search
                    {
                        enableHighAccuracy: true,
                        timeout: 15000,
                        maximumAge: 30000
                    }
                );
            }

            function handleLocationError(error) {
                let errorMessage = '📍 Location: ';
                let userMessage = '';
                
                switch(error.code) {
                    case error.PERMISSION_DENIED:
                        errorMessage += 'Permission denied.';
                        userMessage = 'Please allow location access in your browser settings to find nearby providers.';
                        break;
                    case error.POSITION_UNAVAILABLE:
                        errorMessage += 'Location unavailable.';
                        userMessage = 'Please check your device location settings and try again.';
                        break;
                    case error.TIMEOUT:
                        errorMessage += 'Request timeout.';
                        userMessage = 'Location detection took too long. Please try again.';
                        break;
                    default:
                        errorMessage += 'Detection failed.';
                        userMessage = 'Please try enabling location again.';
                        break;
                }
                
                locationStatus.textContent = errorMessage;
                locationStatus.className = 'location-status location-error';
                enableLocationBtn.textContent = '📍 Try Again';
                enableLocationBtn.disabled = false;
                submitBtn.disabled = true;
                hasLocation = false;
                
                if (userMessage) {
                    alert(userMessage);
                }
            }

            // Process flash messages to display distance results
            function processFlashMessages() {
                const flashMessages = document.querySelectorAll('.alert');
                flashMessages.forEach(message => {
                    const messageText = message.textContent;
                    
                    // Check if this is a successful search result
                    if (message.classList.contains('success') && messageText.includes('km away')) {
                        // Extract information from the flash message
                        const distanceMatch = messageText.match(/(\d+\.?\d*)\s*km/);
                        const phoneMatch = messageText.match(/Contact: (\d+)/);
                        const serviceMatch = messageText.match(/nearest (\w+)/);
                        
                        if (distanceMatch && phoneMatch) {
                            const distance = distanceMatch[1];
                            const phone = phoneMatch[1];
                            const serviceType = serviceMatch ? serviceMatch[1] : 'Service';
                            
                            // Display the distance results
                            distanceValue.textContent = `${distance} km`;
                            resultServiceType.textContent = serviceType;
                            resultPhoneNumber.textContent = phone;
                            resultDistance.textContent = `${distance} km`;
                            
                            // Show the results section
                            distanceResults.style.display = 'block';
                            
                            // Scroll to results
                            distanceResults.scrollIntoView({ behavior: 'smooth' });
                            
                            // Hide the original flash message
                            message.style.display = 'none';
                        }
                    }
                });
            }

            // Run message processing after page load
            setTimeout(processFlashMessages, 100);

            // Prevent form submission if location is not available
            document.getElementById('findServiceProviderForm').addEventListener('submit', function(event) {
                if (!hasLocation || !longitudeInput.value || !latitudeInput.value) {
                    event.preventDefault();
                    alert('Please enable location services to find nearby providers.');
                    enableLocationBtn.scrollIntoView({ behavior: 'smooth' });
                } else {
                    // Show loading state
                    submitBtn.disabled = true;
                    submitBtn.value = 'Searching... Please wait';
                }
            });

            // Try to get location automatically on page load
            setTimeout(() => {
                if (!hasLocation) {
                    navigator.geolocation.getCurrentPosition(
                        function(position) {
                            // Auto-success
                            const lat = position.coords.latitude;
                            const lng = position.coords.longitude;
                            const accuracy = position.coords.accuracy;
                            
                            longitudeInput.value = lng;
                            latitudeInput.value = lat;
                            currentCoordinates = { lat, lng, accuracy };
                            
                            locationStatus.textContent = `📍 Location: Auto-detected`;
                            locationStatus.className = 'location-status location-active';
                            
                            // Coordinates display updated without showing actual coordinates
                            coordinatesDisplay.style.display = 'block';
                            
                            enableLocationBtn.textContent = '🔄 Update Location';
                            submitBtn.disabled = false;
                            hasLocation = true;
                        },
                        function(error) {
                            // Silent fail - user will manually enable
                            console.log('Auto-location failed for search');
                        },
                        {
                            enableHighAccuracy: false,
                            timeout: 8000,
                            maximumAge: 60000
                        }
                    );
                }
            }, 500);
        });
    </script>
</body>
</html>
"""

search_user_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Search User</title>    
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #f4f6f9;
            color: #343a40;
        }
        .container {
            width: 80%;
            max-width: 800px;
            margin: 30px auto;
            padding: 20px;
            background-color: #ffffff;
            box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
            border-radius: 10px;
        }
        h1 {
            text-align: center;
            font-size: 2.5em;
            margin-bottom: 20px;
            color: #007bff;
        }
        form {
            background-color: #fff;
            padding: 20px;
            border-radius: 10px;
            box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
        }
        label {
            font-size: 1.1em;
            margin-bottom: 5px;
            display: block;
            color: #343a40;
        }
        input[type="text"],
        input[type="submit"] {
            width: 100%;
            padding: 10px;
            margin-bottom: 20px;
            border: 1px solid #ced4da;
            border-radius: 5px;
            font-size: 1em;
            box-sizing: border-box;
        }
        input[type="submit"] {
            background-color: #007bff;
            color: white;
            border: none;
            padding: 10px;
            cursor: pointer;
            font-size: 1.1em;
        }
        input[type="submit"]:hover {
            background-color: #0056b3;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Search User</h1>        
        <form action="{{ url_for('search_user') }}" method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">        
            <label for="username">Username:</label>
            <input type="text" id="username" name="username" placeholder="Enter Username" required>
            <input type="submit" value="Search">
        </form>
    </div>
</body>
</html>
"""

view_user_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>User Details</title>    
    <style>
        table {
            width: 100%;
            border-collapse: collapse;
        }
        table, th, td {
            border: 1px solid black;
        }
        th, td {
            padding: 10px;
            text-align: left;
        }
        th {
            background-color: #f2f2f2;
        }
    </style>
</head>
<body>
    <h1>User Details for {{ user_data[0] }}</h1>    
    <table>
        <tr>
            <th>Username</th>
            <td>{{ user_data[0] }}</td>
        </tr>
        <tr>
            <th>Email</th>
            <td>{{ user_data[1] }}</td>
        </tr>
        <tr>
            <th>Role</th>
            <td>{{ user_data[2] }}</td>
        </tr>
        <tr>
            <th>Status</th>
            <td>{{ user_data[3] }}</td>
        </tr>
        <tr>
            <th>Created At</th>
            <td>{{ user_data[4] }}</td>
        </tr>
        <tr>
            <th>Last Interaction</th>
            <td>{{ user_data[5] }}</td>
        </tr>
        <tr>
            <th>Total Requests</th>
            <td>{{ user_data[6] }}</td>
        </tr>
        <tr>
            <th>Total Transactions</th>
            <td>{{ user_data[7] }}</td>
        </tr>
        <tr>
            <th>Used Voucher Code</th>
            <td>{{ user_data[8] }}</td>
        </tr>
        <tr>
            <th>Last Login Attempt</th>
            <td>{{ user_data[9] }}</td>
        </tr>
        <tr>
            <th>Successful Logins</th>
            <td>{{ user_data[10] }}</td>
        </tr>
    </table>

</body>
</html>
"""        
        

#Function to generate HTML error message
def generate_html_message(title, message):
    """Generate an HTML page for error messages."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            background-color: #f0f0f0;
            text-align: center;
            padding: 20px;
            color: #333;
        }}
        h1 {{
            color: #4285f4;
        }}
        .message {{
            border: 2px solid #4285f4;
            padding: 10px;
            max-width: 400px;
            margin: auto;
            background-color: #fff;
        }}
    </style>
</head>
<body>
    <div class="message">
        <h1>{title}</h1>
        <p>{message}</p>
    </div>
</body>
</html>
"""
    
if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
