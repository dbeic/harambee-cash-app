import os
from dotenv import load_dotenv
import psycopg2
from psycopg2 import errors
import json
import random
import time
import logging
from flask import Flask, request, render_template_string, session, redirect, url_for, jsonify, Response, stream_with_context, flash
from threading import Thread, Event
from contextlib import contextmanager
from psycopg2 import pool
from psycopg2.errors import UniqueViolation
import pytz
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from urllib.parse import urlparse
import threading
import string
from functools import wraps
import secrets
from werkzeug.security import generate_password_hash, check_password_hash
from flask import send_from_directory
from flask_wtf.csrf import CSRFProtect
from datetime import datetime, timedelta, timezone
from game_worker import run_game
from shared import get_db_connection

load_dotenv()

app = Flask(__name__)

app.secret_key = os.getenv('SECRET_KEY', 'dev-key-change-in-production')

# --- Configuration & logging ---
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)

# Required environment vars
ADMIN_DATABASE = os.getenv('ADMIN_DATABASE')
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')

if not all([ADMIN_USERNAME, ADMIN_PASSWORD]):
    raise RuntimeError("Missing required environment variables: ADMIN_USERNAME and ADMIN_PASSWORD must be set.")

# CSRF protection
csrf = CSRFProtect(app)

# Rate limiter that prefers logged-in user id, otherwise IP
def rate_limit_key():
    return (
        session.get("user_id") or  # ← Use user_id for both regular users AND admins
        request.remote_addr
    )

limiter = Limiter(
    key_func=rate_limit_key,
    default_limits=[]
)
limiter.init_app(app)

stop_event = threading.Event()

# --- Utility functions ---
def hashed_password(password: str) -> str:
    return generate_password_hash(password.strip(), method='pbkdf2:sha256')

def verify_password(stored_hash: str, password: str) -> bool:
    return check_password_hash(stored_hash, password.strip())

def get_timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

def generate_game_code():
    import string
    return ''.join(random.choices(string.ascii_uppercase + '0123456789', k=6))

# --- Database initialization ---
def init_db():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                username TEXT UNIQUE NOT NULL,
                hashed_password TEXT NOT NULL,
                wallet NUMERIC DEFAULT 0.0
            )
        """)        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_activity (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                username TEXT,
                ip_address TEXT,
                path TEXT,
                method TEXT,
                user_agent TEXT,
                referrer TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS visit_logs (
                id SERIAL PRIMARY KEY, 
                ip_address TEXT,
                user_agent TEXT,
                referrer TEXT,
                path TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS game_queue (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL UNIQUE,
                timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                hashed_password TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS results (
                id SERIAL PRIMARY KEY,
                game_code TEXT UNIQUE,
                timestamp TIMESTAMP,
                num_users INTEGER,
                total_amount NUMERIC,
                deduction NUMERIC,
                winner TEXT,
                winner_amount NUMERIC,
                outcome_message TEXT,
                status TEXT DEFAULT 'upcoming'
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('deposit', 'withdrawal', 'game_entry', 'win')),
                amount NUMERIC NOT NULL,
                timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                game_code TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                username TEXT PRIMARY KEY
            )
        """)
# Withdrawal requests
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                requested_amount NUMERIC NOT NULL,
                withdrawal_fee NUMERIC NOT NULL,
                net_amount NUMERIC NOT NULL,
                status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected', 'completed')),
                request_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_time TIMESTAMP NULL,
                processed_by INTEGER NULL,
                admin_notes TEXT,
                receipt_code TEXT UNIQUE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(processed_by) REFERENCES admins(id) ON DELETE SET NULL
            )
        """)
        # Deposit requests table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS deposit_requests (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                amount NUMERIC NOT NULL,
                voucher_code TEXT UNIQUE NOT NULL,
                status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'completed', 'rejected')),
                request_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_time TIMESTAMP NULL,
                processed_by INTEGER NULL,
                admin_notes TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(processed_by) REFERENCES admins(id) ON DELETE SET NULL
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_suspensions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                suspension_end TIMESTAMP NOT NULL,
                suspended_by INTEGER,
                suspended_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(suspended_by) REFERENCES admins(id) ON DELETE SET NULL
            )
        """)           
        # Withdrawal limits (tracks user withdrawal frequency)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS withdrawal_limits (
                user_id INTEGER PRIMARY KEY,
                last_withdrawal_time TIMESTAMP NULL,
                daily_attempts INTEGER DEFAULT 0,
                last_attempt_time TIMESTAMP NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        # Withdrawal fees (defines withdrawal fee brackets)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS withdrawal_fees (
                id SERIAL PRIMARY KEY,
                min_amount NUMERIC NOT NULL,
                max_amount NUMERIC NOT NULL,
                fee_amount NUMERIC DEFAULT 0,
                fee_percentage NUMERIC DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE
            )
        """)        
        # Win earnings (tracks usersâ€™ winnings from games)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS win_earnings (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                game_code TEXT NOT NULL,
                amount NUMERIC NOT NULL,
                earned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_withdrawn BOOLEAN DEFAULT FALSE,
                withdrawn_at TIMESTAMP NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("SELECT COUNT(*) FROM withdrawal_fees")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                INSERT INTO withdrawal_fees (min_amount, max_amount, fee_amount, fee_percentage, is_active) 
                VALUES 
                (100, 1000, 10, 0, TRUE),
                (1001, 5000, 25, 0, TRUE),
                (5001, 20000, 50, 0, TRUE),
                (20001, 50000, 100, 0, TRUE)
            """)        
        
        # Add performance indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_game_queue_user_id ON game_queue(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_results_status ON results(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_withdrawal_requests_user_id ON withdrawal_requests(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_deposit_requests_user_id ON deposit_requests(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_admins_username ON admins(username)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_activity_username ON user_activity(username)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_activity_timestamp ON user_activity(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_visit_logs_timestamp ON visit_logs(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_visit_logs_ip ON visit_logs(ip_address)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_timestamp ON transactions(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_type ON transactions(type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_game_code ON transactions(game_code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_results_timestamp ON results(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_results_game_code ON results(game_code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_withdrawal_requests_status ON withdrawal_requests(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_withdrawal_requests_timestamp ON withdrawal_requests(request_time)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_deposit_requests_status ON deposit_requests(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_deposit_requests_timestamp ON deposit_requests(request_time)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_deposit_requests_voucher_code ON deposit_requests(voucher_code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_suspensions_user_id ON user_suspensions(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_suspensions_end ON user_suspensions(suspension_end)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_win_earnings_user_id ON win_earnings(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_win_earnings_game_code ON win_earnings(game_code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_win_earnings_withdrawn ON win_earnings(is_withdrawn)")          
        
        cursor.execute("SELECT id FROM admins WHERE username = %s LIMIT 1", (ADMIN_USERNAME,))
        exists = cursor.fetchone()
        if not exists:
            hashed = generate_password_hash(ADMIN_PASSWORD)
            cursor.execute("INSERT INTO admins (username, hashed_password) VALUES (%s, %s)", (ADMIN_USERNAME, hashed))
            conn.commit()
            print('Admin created successfully')
            print(ADMIN_USERNAME, ADMIN_PASSWORD)


init_db()

#def login_required(role=None):
#    def decorator(f):
#        @wraps(f)
#        def decorated_function(*args, **kwargs):

#            # ADMIN ROUTES
#            if role == "admin":
#                if "admin_id" not in session:
#                    flash("Please log in as admin to access this page.", "error")
#                    return redirect(url_for("admin_login"))
#                return f(*args, **kwargs)

#            # USER ROUTES
#            if "user_id" not in session:
#                flash("Please log in to access this page.", "error")
#                return redirect(url_for("login"))

#            return f(*args, **kwargs)

#        return decorated_function
#    return decorator

# --- Wallet & transactions ---
def validate_wallet_sufficient(user_id, amount):
    """Check if user has sufficient wallet balance"""
    try:
        balance = get_wallet_balance(user_id)
        return balance >= amount
    except Exception as e:
        logging.error(f"Error validating wallet for user {user_id}: {e}")
        return False

def get_wallet_balance(user_id):
    if not user_id:
        return 0.0
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT wallet FROM users WHERE id = %s", (user_id,))
            result = cursor.fetchone()
            if result and result[0] is not None:
                return float(result[0])
            else:
                logging.warning(f"User {user_id} not found or has null wallet balance")
                return 0.0
    except Exception as e:
        logging.error(f"Error getting wallet balance for user {user_id}: {e}")
        return 0.0

def update_wallet(user_id, amount):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET wallet = wallet + %s WHERE id = %s", (amount, user_id))
        conn.commit()

def log_transaction(user_id, transaction_type, amount):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO transactions (user_id, type, amount, timestamp)
                VALUES (%s, %s, %s, %s)
            """, (user_id, transaction_type, amount, get_timestamp()))
            conn.commit()
    except Exception as e:
        logging.error(f"Error in log_transaction(): {e}")

# --- Logging visitors / activity ---
def log_visit_entry(ip_address, user_agent, referrer=None, page=None, timestamp=None):
    # Ensure timestamp is always a string
    if timestamp is None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    elif isinstance(timestamp, datetime):
        ts = timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")
    else:
        ts = str(timestamp)

    # Insert visit log into the database
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO visit_logs (ip_address, user_agent, referrer, path, timestamp)
            VALUES (%s, %s, %s, %s, %s)
        """, (ip_address, user_agent, referrer, page, ts))
        conn.commit()           

@app.before_request
def log_user_activity():
    if request.path.startswith("/static"):
        return
    try:
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        ua = request.headers.get("User-Agent", "")
        ref = request.referrer
        path = request.path
        method = request.method
        username = session.get("username")
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_activity (username, ip_address, path, method, user_agent, referrer)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (username, ip, path, method, ua, ref))
            conn.commit()
    except Exception as e:
        logging.debug(f"Failed to log user activity: {e}")

@app.before_request
def log_visitor():
    if request.path.startswith("/static"):
        return
    try:
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        agent = request.headers.get('User-Agent', 'unknown')
        ref = request.referrer or 'direct'
        path = request.path
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_visit_entry(ip, agent, ref, path, ts)
    except Exception as e:
        logging.debug(f"Visitor log error: {e}")

# --- Static files route (exposed) ---
@csrf.exempt
@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


#Routes
@app.route("/")
@limiter.limit("200 per minute")
def index():
    if 'user_id' in session:
        wallet_balance = get_wallet_balance(session['user_id'])
    else:
        wallet_balance = 0.0
        
    return render_template_string(base_html, 
                                wallet_balance=wallet_balance,
                                session=session)
          
                
@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute, 20 per hour")
def login():
    if session.get('user_id'):
        return redirect(url_for('index'))

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        # Rely on CSRFProtect for CSRF verification (token injected into template).
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, username, hashed_password FROM users WHERE username = %s", (username,))
            user = cursor.fetchone()

            if user and check_password_hash(user[2], password):
                session['user_id'] = user[0]
                session['username'] = user[1]
                response = redirect(url_for("index"))
                response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
                response.headers['Pragma'] = 'no-cache'
                response.headers['Expires'] = '0'
                return response
            else:
                return render_template_string(login_html, error="Invalid username or password.", message=None)             

    return render_template_string(login_html, error=None, message=None)

@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def register():
    if request.method == "POST":
        email = request.form.get("email")
        username = request.form.get("username")
        password = request.form.get("password")

        # Basic validation
        if not all([email, username, password]):
            return render_template_string(register_html, error="All fields are required")

        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Check if username is allowed
            cursor.execute("SELECT 1 FROM allowed_users WHERE username = %s", (username,))
            if cursor.fetchone() is None:
                return render_template_string(register_html, error="Username is not allowed")

            # Hash password
            hashed_password = generate_password_hash(password)

            try:
                # Insert new user
                cursor.execute(
                    "INSERT INTO users (email, username, hashed_password) VALUES (%s, %s, %s)",
                    (email, username, hashed_password)
                )

                # Remove the username from allowed_users so it can't be reused
                cursor.execute("DELETE FROM allowed_users WHERE username = %s", (username,))

                conn.commit()
                return redirect(url_for("login"))

            except UniqueViolation:
                conn.rollback()
                return render_template_string(register_html, error="Email or username already exists")

            except Exception as e:
                conn.rollback()
                logging.error(f"Database error during registration: {e}")
                return render_template_string(register_html, error="Something went wrong. Try again later.")

    # GET request â€” show registration form
    return render_template_string(register_html)


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
    
@app.route("/privacy")
@limiter.limit("10 per hour")
def privacy():
    return render_template_string(PRIVACY_CONTENT)

@app.route("/terms")
@limiter.limit("10 per hour")
def terms():
    return render_template_string(TERMS_CONTENT)

@app.route("/docs")
@limiter.limit("10  per hour")
def docs():
    return render_template_string(DOCS_CONTENT)
    

@app.route("/logout")
#@login_required()
@limiter.limit("5 per hour")
def logout():
    session.pop('user_id', None)
    session.pop('username', None)
    flash("You have been logged out successfully.", "info")
    return redirect(url_for("index"))
    
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    # If already logged in as admin, go to dashboard (same pattern as user login)
    if session.get('admin_id'):
        return redirect(url_for('admin_dashboard'))

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        # Use the same get_db_connection pattern as user login
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, username, hashed_password FROM admins WHERE username = %s",
                (username,)
            )
            admin = cursor.fetchone()

            # Use the helper verify_password for consistency with user login helpers
            # Use the SAME method as user login
            if admin and check_password_hash(admin[2], password):
                session['admin_id'] = admin[0]
                session['admin_username'] = admin[1]
                session['is_admin'] = True      # <-- ADD THIS LINE

                response = redirect(url_for("admin_dashboard"))
                # Same cache headers as your user login
                response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
                response.headers['Pragma'] = 'no-cache'
                response.headers['Expires'] = '0'
                # You previously added frame options for admin — keep it
                response.headers['X-Frame-Options'] = 'SAMEORIGIN'
                return response
            else:
                # Mirror user login's render_template_string signature (error + message)
                return render_template_string(admin_login_html, error="Invalid admin credentials.", message=None)

    return render_template_string(admin_login_html, error=None, message=None)

@app.route("/stream")
def stream():
    def event_stream():
        while True:
            try:
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT game_code, status, timestamp, num_users, winner, winner_amount, outcome_message
                        FROM results
                        ORDER BY timestamp DESC
                        LIMIT 1
                    """)
                    game = cursor.fetchone()

                    if game:
                        data = {
                            "game_code": game[0],
                            "status": game[1] if game[1] else "unknown",
                            "timestamp": game[2].strftime("%Y-%m-%d %H:%M:%S") if game[2] else "N/A",
                            "num_users": game[3] if isinstance(game[3], int) else 0,
                            "winner": game[4] if game[4] else "N/A",
                            "winner_amount": float(game[5]) if isinstance(game[5], (int, float)) else 0.0,
                            "outcome_message": game[6] if isinstance(game[6], str) else "",
                        }
                        yield f"data: {json.dumps(data)}\n\n"

                time.sleep(1)

            except psycopg2.Error as e:
                logging.error(f"Database error in streaming: {e}")
                break

            except GeneratorExit:
                logging.info("Client disconnected from stream.")
                break

            except Exception as e:
                logging.error(f"Unexpected error in event stream: {e}")
                break

    return Response(stream_with_context(event_stream()), content_type="text/event-stream")


@app.route("/game_data")
#@login_required()
def game_data():
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Get upcoming game
            cursor.execute("SELECT game_code, timestamp FROM results WHERE status = 'upcoming' ORDER BY timestamp DESC LIMIT 1")
            upcoming_game = cursor.fetchone()
            upcoming_game_data = {
                "game_code": upcoming_game[0] if upcoming_game else "N/A",
                "timestamp": upcoming_game[1] if upcoming_game else "N/A",
                "outcome_message": "Starting soon..."
            } if upcoming_game else None

            # Get in-progress game
            cursor.execute("""
                SELECT game_code, timestamp, num_users, total_amount, winner, winner_amount 
                FROM results 
                WHERE status = 'in progress' 
                ORDER BY timestamp DESC LIMIT 1
            """)
            in_progress_game = cursor.fetchone()
            in_progress_game_data = {
                "game_code": in_progress_game[0] if in_progress_game else "N/A",
                "timestamp": in_progress_game[1] if in_progress_game else "N/A",
                "num_users": in_progress_game[2] if in_progress_game else 0,
                "total_amount": float(in_progress_game[3]) if in_progress_game and in_progress_game[3] else 0.0,
                "winner": in_progress_game[4] if in_progress_game else "N/A",
                "winner_amount": float(in_progress_game[5]) if in_progress_game and in_progress_game[5] else 0.0,
                "status": "in progress",
                "outcome_message": "Game in progress"
            } if in_progress_game else None

            # Get completed games
            cursor.execute("""
                SELECT game_code, timestamp, num_users, total_amount, deduction, winner, winner_amount
                FROM results
                WHERE status = 'completed'
                ORDER BY timestamp DESC
                LIMIT 50
            """)
            completed_games = cursor.fetchall()

            completed_games_data = [
                {
                    "game_code": game[0],
                    "timestamp": game[1],
                    "num_users": game[2],
                    "total_amount": f"Ksh. {float(game[3]):.2f}" if game[3] else "Ksh. 0.00",
                    "deduction": f"Ksh. {float(game[4]):.2f}" if game[4] else "Ksh. 0.00",
                    "winner": game[5] if game[5] else "N/A",
                    "winner_amount": f"Ksh. {float(game[6]):.2f}" if game[6] else "Ksh. 0.00",
                    "outcome_message": f"Winner: {game[5]}" if game[5] else "No winner"
                }
                for game in completed_games
            ]

            # Check if current user is queued using your game_queue table
            current_user_queued = False
            if session.get('user_id'):
                cursor.execute("SELECT COUNT(*) FROM game_queue WHERE user_id = %s", (session['user_id'],))
                current_user_queued = cursor.fetchone()[0] > 0

        response_data = {
            "upcoming_game": upcoming_game_data or {
                "game_code": "N/A", 
                "timestamp": "N/A", 
                "outcome_message": "No upcoming games"
            },
            "in_progress_game": in_progress_game_data,
            "completed_games": completed_games_data,
            "current_user_queued": current_user_queued
        }

        return jsonify(response_data)

    except Exception as e:
        print(f"Error in game_data: {str(e)}")
        return jsonify({
            "error": "Unable to fetch game data",
            "upcoming_game": {"game_code": "N/A", "timestamp": "N/A", "outcome_message": "Error loading"},
            "completed_games": [],
            "current_user_queued": False
        }), 500

@app.route("/play", methods=["POST"])
#@login_required()
@limiter.limit("10 per minute")  # More generous limit
def play():
    user_id = session.get("user_id")
    username = session.get("username")
    
    if not user_id:
        return jsonify({"success": False, "error": "You must be logged in to play."})

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Check if user already in queue
            cursor.execute("SELECT 1 FROM game_queue WHERE user_id = %s", (user_id,))
            if cursor.fetchone():
                return jsonify({"success": False, "error": "Already enrolled in current game!"})

            # Check current balance
            cursor.execute("SELECT wallet FROM users WHERE id = %s", (user_id,))
            result = cursor.fetchone()
            current_balance = float(result[0]) if result and result[0] else 0.0
            
            if current_balance < 1.0:
                return jsonify({"success": False, "error": f"Insufficient funds! Your balance: Ksh. {current_balance:.2f}"})

            # Deduct play amount atomically
            cursor.execute("""
                UPDATE users 
                SET wallet = wallet - 1.0 
                WHERE id = %s AND wallet >= 1.0
                RETURNING wallet
            """, (user_id,))
            
            updated = cursor.fetchone()
            if not updated:
                return jsonify({"success": False, "error": "Transaction failed. Please try again."})

            # Record transaction
            cursor.execute("""
                INSERT INTO transactions (user_id, type, amount, timestamp)
                VALUES (%s, 'game_entry', %s, %s)
            """, (user_id, -1.0, get_timestamp()))

            # Add to game queue
            cursor.execute("""
                INSERT INTO game_queue (user_id, timestamp)
                VALUES (%s, %s)
            """, (user_id, get_timestamp()))

            # Get updated balance
            new_balance = float(updated[0])
            
            conn.commit()

            # Log successful play
            logging.info(f"User {username} successfully enrolled in game. New balance: Ksh. {new_balance:.2f}")

            return jsonify({
                "success": True, 
                "message": "🎉 Successfully enrolled in the next game!",
                "new_balance": new_balance
            })

    except psycopg2.IntegrityError:
        return jsonify({"success": False, "error": "Already enrolled in current game!"})
    except Exception as e:
        logging.error(f"Play error for user {username}: {str(e)}")
        return jsonify({"success": False, "error": "System error. Please try again."})

@app.route("/admin/add_allowed_user", methods=["POST"])
#@login_required(role='admin')
@limiter.limit("50 per hour")
def admin_add_allowed_user():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login", error="Unauthorized access."))

    username = request.form.get("allowed_username")

    if not username:
        return redirect(url_for("admin_dashboard", error="Username is required."))

    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO allowed_users (username) VALUES (%s) ON CONFLICT DO NOTHING",
                (username,)
            )
            conn.commit()
            return redirect(url_for("admin_dashboard", message="Allowed username added successfully."))
        except Exception as e:
            logging.error(f"Error adding allowed user: {e}")
            return redirect(url_for("admin_dashboard", error="Failed to add allowed username."))

@app.route("/admin/dashboard")
#@login_required(role='admin')
@limiter.limit("5 per hour")
def admin_dashboard():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login", error="Please log in as an admin."))

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users ORDER BY id ASC")
        users = cursor.fetchall()

        cursor.execute("SELECT * FROM user_activity ORDER BY timestamp DESC LIMIT 100")
        logs = cursor.fetchall()

    return render_template_string(
        admin_html,
        users=users,
        logs=logs,
        error=request.args.get("error"),
        message=request.args.get("message")
    )  
        
@app.route("/admin/visitor_log")
#@login_required(role='admin')
@limiter.limit("5 per hour")
def view_visits():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login", error="Unauthorized access."))

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ip_address, user_agent, referrer, path, timestamp
            FROM visit_logs
            ORDER BY timestamp DESC
            LIMIT 100
        """)
        logs = cursor.fetchall()

    return render_template_string("""
    <h2>Recent Site Visits</h2>
    <table border="1" cellpadding="5">
        <tr><th>IP Address</th><th>User Agent</th><th>Referrer</th><th>Path</th><th>Time</th></tr>
        {% for log in logs %}
            <tr>
                <td>{{ log[0] }}</td>
                <td>{{ log[1] }}</td>
                <td>{{ log[2] }}</td>
                <td>{{ log[3] }}</td>
                <td>{{ log[4] }}</td>
            </tr>
        {% endfor %}
    </table>
    <br>
    <a href="/admin/dashboard">â† Back to Admin Dashboard</a>
    """, logs=logs)

@app.route("/admin/logout")
#@login_required(role='admin')
@limiter.limit("3 per hour")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))
    
@app.route('/robots.txt')
def robots_txt():
    return (
        "User-agent: *\nDisallow:\n",
        200,
        {'Content-Type': 'text/plain'}
    )

admin_login_html = """  
<!DOCTYPE html>  
<html lang="en">  
<head>  
    <meta charset="UTF-8">  
    <meta name="viewport" content="width=device-width, initial-scale=1.0">  
    <title>Admin Login - HARAMBEE CASH!</title>  
    <style>  
        body { font-family: Arial, sans-serif; margin:0; padding:0; display:flex; justify-content:center; align-items:center; min-height:100vh; background:linear-gradient(to right,#43cea2,#185a9d); color:white; }
        .container { width:90%; max-width:400px; padding:20px; background:rgba(0,0,0,0.8); border-radius:15px; text-align:center; box-sizing:border-box; }
        h1 { font-size:1.8rem; margin-bottom:15px; color:#ffcc00; }
        .error { color:#ffcccb; font-weight:bold; margin-bottom:10px; }
        form { display:flex; flex-direction:column; gap:15px; }
        label { font-size:1rem; text-align:left; color:#ffcccb; }
        input, button { padding:10px; font-size:1rem; border-radius:5px; width:100%; box-sizing:border-box; }
        input { border:1px solid #ccc; background:rgba(255,255,255,0.1); color:white; }
        button { background-color:#4CAF50; color:white; cursor:pointer; border:none; transition:background-color 0.3s ease; font-weight:bold; }
        button:hover { background-color:#45a049; }
    </style>  
</head>  
<body>  
    <div class="container">  
        <h1>Admin Login</h1>  
        {% if error %} <p class="error">{{ error }}</p> {% endif %}  
        <form method="POST" action="/admin/login" id="adminLoginForm" autocomplete="on">  
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <label for="adminUsername">Username:</label>  
            <input type="text" id="adminUsername" name="username" required autocomplete="username" placeholder="Admin username">  
            <label for="adminPassword">Password:</label>  
            <input type="password" id="adminPassword" name="password" required autocomplete="current-password" placeholder="Admin password">  
            <button type="submit">Login</button>  
        </form>  
    </div>  
    <script>  
        document.getElementById('adminLoginForm').addEventListener('submit', function() { console.log('Admin login submitted'); });  
    </script>  
</body>  
</html>  
"""
