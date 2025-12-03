#newest
import json
from psycopg2.extras import Json 
from flask import current_app
import boto3
from botocore.exceptions import NoCredentialsError, ClientError
import uuid
import os
import time
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse
from dotenv import load_dotenv
from PIL import Image
import io
from flask import (
    Flask, request, redirect, url_for, session, flash, abort, 
    render_template_string, jsonify, send_file
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from functools import wraps
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SelectField, TextAreaField, DecimalField, IntegerField, FileField
from wtforms.validators import DataRequired, Length, Optional, NumberRange
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
# --- PDF & QR Code (for receipts) ---
from io import BytesIO
from reportlab.lib.pagesizes import A6
from reportlab.pdfgen import canvas
import qrcode
import requests
from flask import send_from_directory, jsonify
# ReportLab Platypus for PDF building
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter  # You can keep using 'letter' for page size
MAX_FAILED_ATTEMPTS = 5
LOCK_DURATION_MINUTES = 30

# Load environment variables from .env file
load_dotenv()

# ---------------------- Configuration ------------------

SECRET = os.environ.get("SECRET", "dev-secret-change-me")
SHIER_DATABASE = os.environ.get("SHIER_DATABASE")

if not SHIER_DATABASE:
    SHIER_DATABASE = "postgresql://postgres:postgres@localhost:5432/postgres"
    print("Warning: Using fallback database URL. Please set SHIER_DATABASE in your .env file")

if SHIER_DATABASE.startswith("postgres://"):
    SHIER_DATABASE = SHIER_DATABASE.replace("postgres://", "postgresql://", 1)

app = Flask(__name__)

app.config["PROPAGATE_EXCEPTIONS"] = True
app.config["DEBUG"] = True

app.secret_key = SECRET

# Initialize rate limiter (Flask-Limiter 2.x+)
limiter = Limiter(
    key_func=get_remote_address,  # Keep the key function
    default_limits=["200 per day", "50 per hour"]
)

# Attach limiter to Flask app
limiter.init_app(app)

# Configure image upload
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2MB max file size

# AWS S3 Configuration
AWS_ACCESS_KEY = os.environ.get('AWS_ACCESS_KEY_ID')
AWS_SECRET_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY')
S3_BUCKET = os.environ.get('S3_BUCKET_NAME')
S3_REGION = os.environ.get('S3_REGION', 'us-east-1')

# Initialize S3 client
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=S3_REGION
)

# Connection pool - initialize as None first
pool = None

def init_db_pool():
    """Initialize the database connection pool"""
    global pool
    try:
        pool = SimpleConnectionPool(1, 10, dsn=SHIER_DATABASE)
        print("Database connection pool initialized successfully")
        return True
    except Exception as e:
        print(f"Error initializing database pool: {e}")
        return False

# ---------------------- Forms ----------------------

class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=50)])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6)])

class RegistrationForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=3, max=50)])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6)])
    role = SelectField('Account Type', choices=[
        ('', 'Select your role'),
        ('customer', 'Customer'),
        ('cashier', 'Cashier')
    ], validators=[DataRequired()])
    phone = StringField('Phone Number', validators=[Optional(), Length(max=15)])

class ProductForm(FlaskForm):
    category = StringField('Category', validators=[DataRequired(), Length(max=100)])
    name = StringField('Product Name', validators=[DataRequired(), Length(max=100)])
    description = TextAreaField('Description', validators=[Optional(), Length(max=500)])
    image = FileField('Product Image')    

class OfferForm(FlaskForm):
    commodity_name = StringField('Commodity Name', validators=[DataRequired(), Length(max=100)])
    commodity_description = TextAreaField('Commodity Description', validators=[Optional(), Length(max=500)])
    price = DecimalField('Price (KES)', validators=[DataRequired(), NumberRange(min=0)], places=2)
    quantity = IntegerField('Quantity', validators=[Optional(), NumberRange(min=1)])
    payment_terms = StringField('Payment Terms', validators=[Optional(), Length(max=200)])
    delivery_terms = StringField('Delivery Terms', validators=[Optional(), Length(max=200)])

class ProfileForm(FlaskForm):
    shop_name = StringField('Shop Name', validators=[DataRequired(), Length(max=100)])
    phone = StringField('Phone Number', validators=[Optional(), Length(max=15)])
    address = TextAreaField('Address', validators=[Optional(), Length(max=300)])

class OrderForm(FlaskForm):
    quantity = IntegerField('Quantity', validators=[DataRequired(), NumberRange(min=1)])
    delivery_address = TextAreaField('Delivery Address', validators=[DataRequired(), Length(max=500)])
    terms_agreed = SelectField('I agree to pay upon delivery', choices=[('yes', 'Yes'), ('no', 'No')], validators=[DataRequired()])

class PaymentConfirmationForm(FlaskForm):
    mpesa_message = TextAreaField('M-Pesa Transaction Message', validators=[DataRequired(), Length(max=1000)])
    received_confirmation = SelectField('I have received the goods', choices=[('yes', 'Yes'), ('no', 'No')], validators=[DataRequired()])

# ---------------------- Helpers ----------------------

app_initialized = False

@app.before_request
def init_app():
    global app_initialized
    if not app_initialized:
        try:
            if init_db_pool():
                create_tables()
                print("Application initialized successfully")
                app_initialized = True
            else:
                print("Failed to initialize database connection")
        except Exception as e:
            print(f"Error initializing application: {e}")

def db_conn():
    if pool is None:
        if not init_db_pool():
            raise Exception("Database connection pool not initialized")
    return pool.getconn()

def db_put(conn):
    if conn and pool:
        pool.putconn(conn)

def query(sql, params=None, fetch="all"):
    conn = db_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params or [])
            if fetch == "one":
                row = cur.fetchone()
                return row
            elif fetch == "all":
                return cur.fetchall()
            elif fetch == "none":
                return None
    except Exception as e:
        print(f"Database query error: {e}")
        raise
    finally:
        db_put(conn)

def execute(sql, params=None):
    conn = db_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params or [])
    except Exception as e:
        print(f"Database execute error: {e}")
        raise
    finally:
        db_put(conn)

def create_tables():
    try:
        # --- USERS TABLE FIRST ---
        execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('customer','admin','cashier')),
                phone TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                failed_login_attempts INTEGER DEFAULT 0,
                account_locked_until TIMESTAMP NULL
            );
            """
        )
        
        # --- SUBSCRIPTIONS TABLE ---
        execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT NOT NULL,
                service_package TEXT NOT NULL CHECK (service_package IN ('basic','standard','premium','enterprise')),
                business_type TEXT NOT NULL,
                business_details TEXT,
                amount_paid NUMERIC(12,2) NOT NULL,
                transaction_code TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','completed','cancelled')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                activated_at TIMESTAMP NULL,
                completed_at TIMESTAMP NULL
            );
            """
        )        

        # --- MEDIA ARTICLES ---
        execute(
            """
            CREATE TABLE IF NOT EXISTS media_articles (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT,
                content TEXT,
                media_type TEXT CHECK (media_type IN ('image', 'video', 'text')) DEFAULT 'text',
                media_url TEXT,
                thumbnail_url TEXT,
                s3_url TEXT,
                author TEXT,
                author_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # --- PRODUCTS ---
        execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                image_url TEXT,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # --- CASHIER PROFILES ---
        execute(
            """
            CREATE TABLE IF NOT EXISTS cashier_profiles (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                shop_name TEXT NOT NULL,
                phone TEXT,
                address TEXT,
                is_active BOOLEAN DEFAULT FALSE,
                next_available_time TIMESTAMP NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # --- OFFERS ---
        execute(
            """
            CREATE TABLE IF NOT EXISTS offers (
                id SERIAL PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                cashier_id INTEGER NOT NULL REFERENCES cashier_profiles(id) ON DELETE CASCADE,
                category_name TEXT NOT NULL, 
                commodity_name TEXT,
                commodity_description TEXT,
                price NUMERIC(12,2) NOT NULL,
                quantity INTEGER,
                payment_terms TEXT,
                delivery_terms TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP  
            );
            """
        )

        # --- SHOPPING CARTS ---
        execute(
            """
            CREATE TABLE IF NOT EXISTS shopping_carts (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # --- CART ITEMS ---
        execute(
            """
            CREATE TABLE IF NOT EXISTS cart_items (
                id SERIAL PRIMARY KEY,
                cart_id INTEGER NOT NULL REFERENCES shopping_carts(id) ON DELETE CASCADE,
                offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
                quantity INTEGER NOT NULL DEFAULT 1,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # --- ORDER SESSIONS ---
        execute(
            """
            CREATE TABLE IF NOT EXISTS order_sessions (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                session_data JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # --- ORDERS ---
        execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
                quantity INTEGER NOT NULL,
                total_price NUMERIC(12,2) NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                delivery_address TEXT NOT NULL,
                terms_agreed BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmed_at TIMESTAMP NULL,
                completed_at TIMESTAMP NULL
            );
            """
        )

        # --- PAYMENTS ---
        execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                mpesa_message TEXT NOT NULL,
                received_confirmation BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # --- TRANSACTION MESSAGES ---
        execute(
            """
            CREATE TABLE IF NOT EXISTS transaction_messages (
                id SERIAL PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                message_type TEXT NOT NULL,
                message_text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # --- USER ACTIVITY ---
        execute(
            """
            CREATE TABLE IF NOT EXISTS user_activity (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                activity_type TEXT NOT NULL,
                details TEXT,
                ip_address TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # --- ALLOWED USERS ---
        execute(
            """
            CREATE TABLE IF NOT EXISTS allowed_users (
                username TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        

        print("✅ Database tables created successfully")

        # --- DEFAULT ADMIN SETUP ---
        admin_username = os.environ.get("SHIER_USERNAME")
        admin_password = os.environ.get("SHIER_PASSWORD")

        if admin_username and admin_password:
            row = query("SELECT id FROM users WHERE username = %s", [admin_username], fetch="one")
            if not row:
                password_hash = generate_password_hash(admin_password)
                execute(
                    "INSERT INTO users (username, password_hash, role, phone) VALUES (%s, %s, %s, %s)",
                    [admin_username, password_hash, "admin", None],
                )
                print(f"🛠 Default admin created: {admin_username}")
            else:
                print("Admin account already exists")
        else:
            print("⚠️ SHIER_USERNAME or SHIER_PASSWORD not set in environment")

    except Exception as e:
        print(f"❌ Error creating tables: {e}")
        

create_tables()
        

def is_user_allowed(username):
    """Check if a username is in the allowed list"""
    row = query(
        "SELECT username FROM allowed_users WHERE username = %s", 
        [username], 
        fetch="one"
    )
    return row is not None

def get_allowed_users():
    """Get all allowed usernames"""
    rows = query(
        "SELECT username, created_at FROM allowed_users ORDER BY created_at DESC"
    )
    return [{"username": r[0], "created_at": r[1]} for r in rows]

def remove_allowed_user(username):
    """Remove a username from the allowed list"""
    try:
        execute("DELETE FROM allowed_users WHERE username = %s", [username])
        return True
    except Exception as e:
        print(f"Error removing allowed user: {e}")
        return False       

def get_user():
    uid = session.get("user_id")
    if not uid:
        return None
    row = query("SELECT id, username, role, phone FROM users WHERE id = %s", [uid], fetch="one")
    if not row:
        return None
    return {"id": row[0], "username": row[1], "role": row[2], "phone": row[3]}

def login_required(role=None):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            user = get_user()
            if not user:
                flash("Please log in first.", "warning")
                return redirect(url_for("login"))
            if role and user["role"] != role:
                abort(403)
            return fn(*args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator

def free_view_tracking(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # If user is logged in, allow access
        if get_user():
            return f(*args, **kwargs)
        
        # If user is not logged in, check free view status
        if session.get('free_view_used'):
            flash("Please log in to view more details.", "warning")
            return redirect(url_for('login'))
        
        # Mark free view as used for anonymous users
        session['free_view_used'] = True
        return f(*args, **kwargs)
    return decorated_function
    
def allowed_file(filename):
    """
    Check if uploaded file has an allowed extension.
    Supports images, videos, and text-based files.
    """
    if not filename or '.' not in filename:
        return False

    allowed_extensions = {
        # Image formats
        "jpg", "jpeg", "png", "gif", "webp",
        # Video formats
        "mp4", "mov", "avi", "mkv", "webm",
        # Text formats
        "txt", "md", "pdf", "doc", "docx"
    }

    ext = filename.rsplit('.', 1)[1].lower()
    return ext in allowed_extensions    

def resize_image(image_file, max_size=(400, 300)):
    """Resize image in memory without saving to filesystem"""
    try:
        img = Image.open(image_file)
        img.thumbnail(max_size)
        
        # Save resized image to bytes buffer
        buffer = io.BytesIO()
        img_format = 'JPEG' if img.format != 'PNG' else 'PNG'
        img.save(buffer, format=img_format, optimize=True)
        buffer.seek(0)
        
        return buffer
    except Exception as e:
        print(f"Error resizing image: {e}")
        # Return original file if resizing fails
        image_file.seek(0)
        return image_file

def upload_to_s3(file, filename, content_type=None):
    """Upload file (image, video, or text) to S3 bucket and return public URL."""
    try:
        # --- Ensure filename is unique ---
        unique_filename = f"{uuid.uuid4().hex}_{secure_filename(filename)}"

        # --- Detect content type if not provided ---
        if not content_type:
            import mimetypes
            content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

        # --- Upload file ---
        s3_client.upload_fileobj(
            file,
            S3_BUCKET,
            unique_filename,
            ExtraArgs={
                'ContentType': content_type,
                'ACL': 'public-read'  # Make it publicly accessible
            }
        )

        # --- Generate public URL ---
        s3_url = f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{unique_filename}"
        return s3_url

    except (NoCredentialsError, ClientError) as e:
        print(f"❌ Error uploading to S3: {e}")
        return None
    except Exception as e:
        print(f"⚠️ Unexpected error: {e}")
        return None

def delete_from_s3(url):
    """Delete file from S3 using its public URL."""
    try:
        if not url or not url.startswith(f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/"):
            print("⚠️ Invalid S3 URL")
            return False

        # Extract the S3 object key
        key = url.split(f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/")[-1]
        key = key.split("?")[0]  # Remove query string if present

        # Delete the object
        s3_client.delete_object(Bucket=S3_BUCKET, Key=key)
        return True

    except Exception as e:
        print(f"❌ Error deleting from S3: {e}")
        return False

def log_activity(user_id, activity_type, details=None):
    ip_address = request.remote_addr
    execute(
        "INSERT INTO user_activity (user_id, activity_type, details, ip_address) VALUES (%s, %s, %s, %s)",
        [user_id, activity_type, details, ip_address]
    )

def send_transaction_message(order_id, message_type, message_text):
    execute(
        "INSERT INTO transaction_messages (order_id, message_type, message_text) VALUES (%s, %s, %s)",
        [order_id, message_type, message_text]
    )

def extract_mpesa_details(mpesa_text: str) -> dict:
    """
    Extracts M-Pesa transaction details. Compatible with full message,
    just transaction ID, or blank input.
    Returns dict with keys: trx_id, amount, date.
    """
    details = {"trx_id": "N/A", "amount": "N/A", "date": "N/A"}

    if not mpesa_text or not mpesa_text.strip():
        return details

    text = mpesa_text.strip()

    # Transaction ID (alphanumeric 4-12 chars)
    trx_match = re.search(r"\b[A-Z0-9]{4,12}\b", text, re.I)
    if trx_match:
        details["trx_id"] = trx_match.group(0)

    # Amount (KES / numbers)
    amount_match = re.search(r"(?i)(?:KES|KSh|KSH)?\s*([0-9]+(?:\.[0-9]{1,2})?)", text)
    if amount_match:
        details["amount"] = amount_match.group(1)

    # Date (YYYY-MM-DD or DD/MM/YYYY)
    date_match = re.search(r"(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})", text)
    if date_match:
        details["date"] = date_match.group(1)

    return details
    
def get_or_create_cart(user_id):
    """Get or create a shopping cart for the user"""
    cart = query(
        "SELECT id FROM shopping_carts WHERE customer_id = %s",
        [user_id], fetch="one"
    )
    
    if not cart:
        execute(
            "INSERT INTO shopping_carts (customer_id) VALUES (%s) RETURNING id",
            [user_id]
        )
        cart = query(
            "SELECT id FROM shopping_carts WHERE customer_id = %s",
            [user_id], fetch="one"
        )
    
    return cart[0]

def get_cart_items(cart_id):
    """Get all items in a shopping cart"""
    items = query(
        """
        SELECT ci.id, ci.offer_id, ci.quantity, o.commodity_name, o.price, 
               p.name as product_name, cp.shop_name, cp.address as shop_address
        FROM cart_items ci
        JOIN offers o ON ci.offer_id = o.id
        JOIN products p ON o.product_id = p.id
        JOIN cashier_profiles cp ON o.cashier_id = cp.id
        WHERE ci.cart_id = %s
        """,
        [cart_id]
    )
    
    return [
        {
            "id": r[0], "offer_id": r[1], "quantity": r[2],
            "commodity_name": r[3], "price": float(r[4]),
            "product_name": r[5], "shop_name": r[6], "shop_address": r[7]
        }
        for r in items
    ]

def get_order_session(user_id):
    """Get order session data for a user"""
    session = query(
        "SELECT session_data FROM order_sessions WHERE customer_id = %s",
        [user_id], fetch="one"
    )
    return session[0] if session else {}
    

def save_order_session(user_id, session_data):
    """Save order session data for a user"""
    session_json = Json(session_data)  # ✅ Wrap dict properly

    existing = query(
        "SELECT id FROM order_sessions WHERE customer_id = %s",
        [user_id], fetch="one"
    )
    
    if existing:
        execute(
            "UPDATE order_sessions SET session_data = %s, updated_at = NOW() WHERE customer_id = %s",
            [session_json, user_id]
        )
    else:
        execute(
            "INSERT INTO order_sessions (customer_id, session_data) VALUES (%s, %s)",
            [user_id, session_json]
        )        

    
#_______new helper functions______

def get_active_cashier():
    """Get the currently active cashier"""
    return query(
        "SELECT id, shop_name, phone, address, is_active, next_available_time FROM cashier_profiles WHERE is_active = TRUE LIMIT 1",
        fetch="one"
    )

def set_active_cashier(cashier_id):
    """Set a cashier as active and deactivate others"""
    try:
        # First deactivate all cashiers
        execute("UPDATE cashier_profiles SET is_active = FALSE")
        # Then activate the selected one
        execute("UPDATE cashier_profiles SET is_active = TRUE WHERE id = %s", [cashier_id])
        return True
    except Exception as e:
        print(f"Error setting active cashier: {e}")
        return False

def set_cashier_availability(cashier_id, is_active, next_available_time=None):
    """Set cashier availability status"""
    try:
        if is_active:
            # Deactivate all others first
            execute("UPDATE cashier_profiles SET is_active = FALSE")
            execute(
                "UPDATE cashier_profiles SET is_active = TRUE, next_available_time = %s WHERE id = %s",
                [next_available_time, cashier_id]
            )
        else:
            execute(
                "UPDATE cashier_profiles SET is_active = FALSE, next_available_time = %s WHERE id = %s",
                [next_available_time, cashier_id]
            )
        return True
    except Exception as e:
        print(f"Error setting cashier availability: {e}")
        return False

# ---------------------- Templates ----------------------

base_html = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title or 'SALES CASHIER DIGITAL ASSISTANT' }}</title>
  <link rel="icon" href="{{ url_for('static', filename='image/cashier.ico') }}" type="image/x-icon">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    :root {
      --primary: #ff7a00;
      --primary-hover: #e86a00;
      --secondary: #6c757d;
      --success: #38b000;
      --warning: #ffb700;
      --error: #e5383b;
      --light-bg: #fff8f2;
      --card-shadow: 0 6px 15px rgba(255, 122, 0, 0.15);
      --gradient-primary: linear-gradient(135deg, #ff7a00 0%, #ff5400 100%);
      --gradient-success: linear-gradient(135deg, #38b000 0%, #007200 100%);
      --gradient-warning: linear-gradient(135deg, #ffb700 0%, #ff7a00 100%);
    }
    
    body { 
      max-width: 1200px; 
      margin: auto; 
      padding: 1rem; 
      background-color: var(--light-bg);
      font-family: 'Poppins', 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
      color: #343a40;
    }
    
    .app-header {
      display: flex; 
      justify-content: space-between; 
      align-items: center; 
      margin: 1rem 0; 
      padding: 1.5rem;
      background: white;
      border-radius: 16px;
      box-shadow: var(--card-shadow);
      background: var(--gradient-primary);
      color: white;
    }
    
    .app-title {
      color: white;
      margin: 0;
      font-weight: 800;
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 1.8rem;
      text-shadow: 1px 1px 3px rgba(0,0,0,0.2);
    }
    
    .app-title a {
      text-decoration: none;
      color: inherit;
    }
    
    .logo {
      height: 50px;
      width: auto;
      margin-right: 10px;
      border-radius: 8px;
    }
    
    .nav-menu {
      display: flex;
      gap: 1.2rem;
      align-items: center;
    }
    
    .nav-menu a {
      text-decoration: none;
      font-weight: 600;
      padding: 0.6rem 1rem;
      border-radius: 8px;
      transition: all 0.3s ease;
      color: white;
      background: rgba(1, 255, 255, 0.15);
      backdrop-filter: blur(10px);
    }
    
    .nav-menu a:hover {
      background: rgba(255, 255, 255, 0.25);
      transform: translateY(-2px);
    }
    
    .user-badge {
      background-color: rgba(255, 255, 255, 0.2);
      color: white;
      padding: 0.5rem 1rem;
      border-radius: 25px;
      font-size: 0.9rem;
      font-weight: 600;
      backdrop-filter: blur(10px);
    }
    
    .grid { 
      display: grid; 
      grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); 
      gap: 2rem; 
      margin: 2rem 0;
    }
    
    .card { 
      background: white;
      border-radius: 16px; 
      padding: 1.8rem;
      transition: transform 0.3s ease, box-shadow 0.3s ease;
      box-shadow: var(--card-shadow);
      height: 100%;
      display: flex;
      flex-direction: column;
      border: none;
      overflow: hidden;
      position: relative;
    }
    
    .card:hover {
      transform: translateY(-8px);
      box-shadow: 0 15px 30px rgba(0, 0, 0, 0.15);
    }
    
    .card::before {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 5px;
      background: var(--gradient-primary);
      border-radius: 8px 8px 0 0;
    }
    
    img.product { 
      width: 100%; 
      height: 200px; 
      object-fit: cover; 
      border-radius: 12px;
      margin-bottom: 1.5rem;
      transition: transform 0.3s ease;
      box-shadow: 0 5px 15px rgba(0,0,0,0.1);
    }
    
    .card:hover img.product {
      transform: scale(1.05);
    }
    
    .card h4 {
      margin: 0.5rem 0;
      color: #212529;
      font-size: 1.3rem;
      font-weight: 700;
    }
    
    .muted { 
      color: var(--secondary); 
      font-size: .95rem; 
      line-height: 1.6;
    }
    
    .flash-message { 
      margin: 1.5rem 0; 
    }
    
    .flash-card {
      border-left: 5px solid;
      padding: 1.2rem 1.8rem;
      border-radius: 12px;
      background: white;
      box-shadow: var(--card-shadow);
      font-weight: 500;
    }
    
    .btn-primary {
      background: var(--gradient-primary);
      border: none;
      color: white;
      font-weight: 600;
      padding: 0.8rem 1.5rem;
      border-radius: 10px;
      margin-top: auto;
      transition: all 0.3s ease;
      box-shadow: 0 4px 10px rgba(67, 97, 238, 0.3);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    
    .btn-primary:hover {
      transform: translateY(-3px);
      box-shadow: 0 8px 15px rgba(67, 97, 238, 0.4);
      color: white;
    }
    
    .btn-secondary {
      background: #f8f9fa;
      color: #495057;
      font-weight: 600;
      padding: 0.7rem 1.2rem;
      border-radius: 10px;
      transition: all 0.3s ease;
      border: 1px solid #e9ecef;
    }
    
    .btn-secondary:hover {
      background: #e9ecef;
      transform: translateY(-2px);
      box-shadow: 0 4px 10px rgba(0,0,0,0.1);
    }
    
    .btn-home {
      background: var(--gradient-success);
      color: white;
      font-weight: 600;
      padding: 0.7rem 1.5rem;
      border-radius: 10px;
      transition: all 0.3s ease;
      text-decoration: none;
      display: inline-block;
      box-shadow: 0 4px 10px rgba(56, 176, 0, 0.3);
    }
    
    .btn-home:hover {
      transform: translateY(-3px);
      box-shadow: 0 8px 15px rgba(56, 176, 0, 0.4);
      color: white;
    }
    
    .section-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 2rem;
      padding: 1.5rem;
      background: white;
      border-radius: 16px;
      box-shadow: var(--card-shadow);
    }
    
    .section-title {
      color: #212529;
      font-weight: 700;
      margin: 0;
      font-size: 1.8rem;
      display: flex;
      align-items: center;
      gap: 0.8rem;
    }
    
    form {
      background: white;
      padding: 2rem;
      border-radius: 16px;
      box-shadow: var(--card-shadow);
    }
    
    label {
      font-weight: 600;
      margin-bottom: 0.5rem;
      display: block;
      color: #343a40;
    }
    
    input, select, textarea {
      padding: 0.9rem;
      border-radius: 10px;
      border: 1px solid #ced4da;
      width: 100%;
      margin-bottom: 1.2rem;
      font-size: 1rem;
      transition: all 0.3s ease;
    }
    
    input:focus, select:focus, textarea:focus {
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 4px rgba(67, 97, 238, 0.15);
      transform: translateY(-2px);
    }
    
    .form-error {
      color: var(--error);
      font-size: 0.9rem;
      margin-top: -0.75rem;
      margin-bottom: 1rem;
      font-weight: 500;
    }
    
    table {
      width: 100%;
      background: white;
      border-radius: 12px;
      overflow: hidden;
      box-shadow: var(--card-shadow);
      border-collapse: separate;
      border-spacing: 0;
    }
    
    th {
      background: var(--gradient-primary);
      color: white;
      padding: 1rem 1.2rem;
      text-align: left;
      font-weight: 600;
      border: none;
    }
    
    td {
      padding: 1rem 1.2rem;
      border-bottom: 1px solid #e9ecef;
    }
    
    tr:last-child td {
      border-bottom: none;
    }
    
    tr:hover td {
      background-color: #f8f9fa;
    }
    
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 2rem;
      margin: 2rem 0;
    }
    
    .stat-card {
      background: white;
      border-radius: 16px;
      padding: 2rem;
      box-shadow: var(--card-shadow);
      text-align: center;
      position: relative;
      overflow: hidden;
    }
    
    .stat-card::before {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 5px;
      background: var(--gradient-primary);
    }
    
    .stat-value {
      font-size: 2.5rem;
      font-weight: 800;
      color: var(--primary);
      margin: 0.5rem 0;
    }
    
    .stat-label {
      color: var(--secondary);
      font-size: 1rem;
      font-weight: 600;
    }
    
    footer {
      margin: 4rem 0 1.5rem;
      text-align: center;
      color: var(--secondary);
      padding: 2rem 0;
      border-top: 1px solid #e9ecef;
    }
    
    .info-section {
      background: white;
      padding: 2rem;
      border-radius: 16px;
      box-shadow: var(--card-shadow);
      margin-bottom: 2rem;
    }
    
    .info-section h3 {
      color: var(--primary);
      border-bottom: 3px solid var(--primary);
      padding-bottom: 0.8rem;
      margin-top: 0;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 0.8rem;
    }
    
    .category-dropdown {
      position: relative;
      display: inline-block;
      width: 100%;
      margin-bottom: 2rem;
    }
    
    .category-dropdown-content {
      display: none;
      position: absolute;
      background-color: white;
      min-width: 250px;
      box-shadow: 0 10px 25px rgba(0,0,0,0.15);
      z-index: 100;
      border-radius: 12px;
      overflow: hidden;
      width: 100%;
    }
    
    .category-dropdown-content a {
      color: #495057;
      padding: 1rem 1.5rem;
      text-decoration: none;
      display: block;
      transition: all 0.2s ease;
      font-weight: 500;
      border-bottom: 1px solid #f1f3f5;
    }
    
    .category-dropdown-content a:hover {
      background-color: #f8f9fa;
      color: var(--primary);
      padding-left: 2rem;
    }
    
    .category-dropdown:hover .category-dropdown-content {
      display: block;
    }
    
    .current-time {
      background: var(--gradient-primary);
      color: #ffffff !important;          /* ✅  Force pure white text */
      padding: 1.2rem;
      border-radius: 12px;
      box-shadow: var(--card-shadow);
      margin-bottom: 1.5rem;
      text-align: center;
      font-weight: 700;
      font-size: 1.2rem;
    }

    .current-time h3 {
      margin: 0;
      font-weight: 700;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.8rem;
      color: #ffffff !important;          /* ✅ Ensure h3 also stays white */
      text-shadow: 0 2px 5px rgba(0, 0, 0, 0.4); /* ✅ Add soft contrast */
    }
    
    .activity-log {
      max-height: 350px;
      overflow-y: auto;
      margin-top: 2rem;
      border-radius: 12px;
      background: white;
      box-shadow: var(--card-shadow);
      padding: 1.5rem;
    }
    
    .activity-item {
      padding: 1rem;
      border-bottom: 1px solid #e9ecef;
      transition: all 0.2s ease;
    }
    
    .activity-item:last-child {
      border-bottom: none;
    }
    
    .order-status {
      display: inline-block;
      padding: 0.4rem 0.8rem;
      border-radius: 25px;
      font-size: 0.85rem;
      font-weight: 600;
    }
    
    .status-pending {
      background: var(--gradient-warning);
      color: white;
    }
    
    .status-confirmed {
      background: var(--gradient-success);
      color: white;
    }
    
    .status-completed {
      background: var(--gradient-primary);
      color: white;
    }
    
    .status-cancelled {
      background: #e9ecef;
      color: #6c757d;
    }
    
    .badge {
      background: var(--primary);
      color: white;
      border-radius: 50%;
      padding: 0.2rem 0.6rem;
      font-size: 0.8rem;
      font-weight: 600;
    }
    
    .image-placeholder {
      width: 100%;
      height: 200px;
      background: linear-gradient(45deg, #f8f9fa, #e9ecef);
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #adb5bd;
      margin-bottom: 1.5rem;
    }
    
    .image-placeholder i {
      font-size: 3rem;
    }
    
    /* Shop Status Styles */
    .shop-status {
      padding: 1.5rem;
      border-radius: 12px;
      margin: 1.5rem 0;
      text-align: center;
    }
    
    .shop-open {
      background: #d1fae5;
      border-left: 4px solid #10b981;
      color: #065f46;
    }
    
    .shop-closed {
      background: #fee2e2;
      border-left: 4px solid #ef4444;
      color: #7f1d1d;
    }
    
    .status-badge {
      display: inline-block;
      padding: 0.5rem 1rem;
      border-radius: 25px;
      font-weight: 600;
      margin-bottom: 1rem;
    }
    
    .status-open {
      background: #10b981;
      color: white;
    }
    
    .status-closed {
      background: #ef4444;
      color: white;
    }
    
    /* Legal Documents Styling */
    .legal-document {
      max-height: 400px;
      overflow-y: auto;
      padding: 1.5rem;
      background: #f8f9fa;
      border-radius: 8px;
      margin: 1rem 0;
      border-left: 4px solid var(--primary);
    }
    
    .legal-document h4 {
      color: var(--primary);
      margin-top: 1.5rem;
      margin-bottom: 0.5rem;
      font-size: 1.2rem;
    }
    
    .legal-document h5 {
      color: #495057;
      margin-top: 1rem;
      margin-bottom: 0.5rem;
      font-size: 1.1rem;
    }
    
    .legal-document p {
      margin-bottom: 0.8rem;
      line-height: 1.6;
    }
    
    .legal-document ul {
      margin-left: 1.5rem;
      margin-bottom: 1rem;
    }
    
    .legal-document li {
      margin-bottom: 0.5rem;
    }
    
    .legal-section {
      margin-bottom: 2rem;
    }
    
    .document-nav {
      display: flex;
      gap: 1rem;
      margin-bottom: 1.5rem;
      flex-wrap: wrap;
    }
    
    .document-nav a {
      text-decoration: none;
      padding: 0.7rem 1.2rem;
      border-radius: 8px;
      background: var(--primary);
      color: white;
      font-weight: 600;
      transition: all 0.3s ease;
    }
    
    .document-nav a:hover {
      background: var(--primary-hover);
      transform: translateY(-2px);
    }
    
    @media (max-width: 768px) {
      .app-header {
        flex-direction: column;
        gap: 1.2rem;
        text-align: center;
      }
      
      .nav-menu {
        flex-wrap: wrap;
        justify-content: center;
      }
      
      .grid {
        grid-template-columns: 1fr;
      }
      
     .category-dropdown-content {
        position: static;
        display: none;
        box-shadow: none;
        width: 100%;
      }
      
      .category-dropdown:hover .category-dropdown-content {
        display: block;
      }
      
      .section-header {
        flex-direction: column;
        gap: 1rem;
        text-align: center;
      }
      
      .document-nav {
        flex-direction: column;
      }
    }
  </style>
  <script>
  function updateTime() {
    const now = new Date();
    const options = { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' };
    const dateString = now.toLocaleDateString('en-US', options);
    const timeString = now.toLocaleTimeString('en-US');
    document.getElementById('current-date').textContent = dateString;
    document.getElementById('current-time').textContent = timeString;
  }
  
  function showDocument(documentId) {
    // Hide all documents
    const documents = document.querySelectorAll('.legal-document-content');
    documents.forEach(doc => doc.style.display = 'none');
    
    // Show selected document
    const selectedDoc = document.getElementById(documentId);
    if (selectedDoc) {
      selectedDoc.style.display = 'block';
    }
    
    // Update active nav link
    const navLinks = document.querySelectorAll('.document-nav a');
    navLinks.forEach(link => link.style.background = 'var(--primary)');
    event.target.style.background = 'var(--primary-hover)';
  }
  
  // Update time every second
  setInterval(updateTime, 1000);
  
  // Initialize on page load
  document.addEventListener('DOMContentLoaded', function() {
    updateTime();
    // Show terms by default
    showDocument('terms-content');
  });
  </script>
</head>
<body>
  <header class="app-header">
    <h1 class="app-title">
      {% if session.get('logo_url') %}
        <img src="{{ session.logo_url }}" alt="Logo" class="logo">
      {% else %}
        <img src="{{ url_for('static', filename='image/midway.png') }}" alt="Custom Logo" class="logo" onerror="this.style.display='none'; this.nextElementSibling.style.display='inline';">
        <i class="fas fa-store" style="display:none;"></i>
      {% endif %}
      <a href="{{ url_for('app_home') }}">Cashier Finder</a>
    </h1>
    <nav class="nav-menu">
      <a href="{{ url_for('app_home') }}" class="btn-home">
        <i class="fas fa-home"></i> Home
      </a>
      {% if user %}
        <span class="user-badge">
          <i class="fas fa-user"></i> {{ user.username }} ({{ user.role }})
        </span>
        {% if user.role == 'admin' %}
          <a href="{{ url_for('admin_catalogue') }}"><i class="fas fa-box-open"></i> Catalogue</a>
          <a href="{{ url_for('admin_stats') }}"><i class="fas fa-chart-bar"></i> Stats</a>
          <a href="{{ url_for('admin_orders') }}"><i class="fas fa-shopping-cart"></i> Orders</a>
          <a href="{{ url_for('admin_dashboard') }}"><i class="fas fa-tachometer-alt"></i> Dashboard</a>
          <a href="{{ url_for('admin_cashier_management') }}"><i class="fas fa-users"></i> Manage Cashiers</a>
        {% elif user.role == 'cashier' %}
          <a href="{{ url_for('cashier_dashboard') }}"><i class="fas fa-tags"></i> My Offers</a>
          <a href="{{ url_for('cashier_profile') }}"><i class="fas fa-store-alt"></i> My Shop</a>
          <a href="{{ url_for('cashier_orders') }}"><i class="fas fa-shopping-cart"></i> Orders</a>
        {% elif user.role == 'customer' %}
          <a href="{{ url_for('customer_orders') }}"><i class="fas fa-shopping-cart"></i> My Orders</a>
        {% endif %}

      {% if user.role == 'customer' %}
        <a href="{{ url_for('view_cart') }}"><i class="fas fa-shopping-cart"></i> Cart
          {% if session_data and session_data.items %}
            <span class="badge">{{ session_data.items|length }}</span>
          {% endif %}
        </a>
        <a href="{{ url_for('order_session') }}"><i class="fas fa-history"></i> Orders</a>
      {% endif %}

      <a href="{{ url_for('logout') }}" class="btn-secondary">
        <i class="fas fa-sign-out-alt"></i> Logout
      </a>
    {% else %}
      <a href="{{ url_for('login') }}"><i class="fas fa-sign-in-alt"></i> Login</a>
      <a href="{{ url_for('register') }}"><i class="fas fa-user-plus"></i> Register</a>
    {% endif %}
  </nav>
</header>

<!-- Flash Messages -->
{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
    <div class="flash-message" style="margin-top: 2rem;">
      {% for category, message in messages %}
        <div class="flash-card" style="border-left-color:
          {% if category == 'success' %}var(--success)
          {% elif category == 'warning' %}var(--warning)
          {% elif category == 'error' %}var(--error)
          {% else %}var(--primary){% endif %};">
          <p style="margin: 0;">
            {% if category == 'success' %}
              <i class="fas fa-check-circle"></i>
            {% elif category == 'warning' %}
              <i class="fas fa-exclamation-triangle"></i>
            {% elif category == 'error' %}
              <i class="fas fa-exclamation-circle"></i>
            {% else %}
              <i class="fas fa-info-circle"></i>
            {% endif %}
            {{ message }}
          </p>
        </div>
      {% endfor %}
    </div>
 
    <div class="current-time" style="margin:2px 0;padding:0;">
      <h3 style="margin:0;font-size:0.9rem;">
        <i class="fas fa-clock" style="font-size:0.9rem;"></i>
        <span id="current-date"></span> | <span id="current-time"></span>
      </h3>
    </div>

    <div class="section-header" style="margin:2px 0;padding:0;">
      <h2 class="section-title" style="margin:0;font-size:1rem;">
        <i class="fas fa-compass" style="font-size:1rem;"></i> Browse Products
      </h2>
    </div>
  
  <!-- Shop Status Display -->
  {% set active_cashier = get_active_cashier() %}
  {% if active_cashier %}
    <div class="shop-status shop-open">
      <span class="status-badge status-open"><i class="fas fa-store"></i> Shop Open</span>
      <p style="margin: 0; font-weight: 600;">Currently serving: {{ active_cashier[1] }}</p>
    </div>
  {% else %}
    <div class="shop-status shop-closed">
      <span class="status-badge status-closed"><i class="fas fa-store-slash"></i> Shop Closed</span>
      <p style="margin: 0;">No active cashier available at the moment. Please check back later.</p>
    </div>
  {% endif %}    
  
<!-- Category Links -->
<div class="category-links" style="margin-bottom: 1.5rem; text-align: center;">
  {% for category in categories %}
    <a href="{{ url_for('category_products', category_name=category) }}"
       style="margin: 0 0.8rem; font-weight: 600; color: var(--primary); text-decoration: none;">
       {{ category }}
    </a>
    {% if not loop.last %}|{% endif %}
  {% endfor %}
</div>

<!-- Products Grid -->
<div class="grid">
  {% for p in products %}
    <article class="card">
      {% if p.image_url %}
        <img class="product" src="{{ p.image_url }}" alt="{{ p.name }}"
             onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
        <div class="image-placeholder" style="display: none;">
          <i class="fas fa-image"></i>
        </div>
      {% else %}
        <div class="image-placeholder">
          <i class="fas fa-image"></i>
        </div>
      {% endif %}
      <h4>{{ p.name }}</h4>
      <p class="muted">
        {{ p.category }}
        {% if p.description %} · {{ p.description[:60] }}{% endif %}
      </p>
      <button class="btn-primary"
              onclick="goToCashiers('{{ url_for('product_cashiers', product_id=p.id) }}')">
        <i class="fas fa-map-marker-alt"></i> Find Wholesalers Nearby
      </button>
    </article>
  {% else %}
    {% if user and user.role == 'admin' %}
      <a href="{{ url_for('admin_catalogue') }}" class="btn-primary">Add Products</a>
    {% endif %}
  {% endfor %}
</div>
  {% endif %}
{% endwith %}

<main>
  {{ body|safe }}
</main>

<script>
function goToCashiers(targetUrl) {
  window.location.href = targetUrl;
}
</script>

<!-- Media Center Button -->
<div class="grid" style="margin-top: 2rem; text-align: center;">
  <a href="{{ url_for('media_center') }}" class="btn-primary" style="text-align:center;">
    <i class="fas fa-tv"></i> Visit Media Center
  </a>
</div>
  
  <div class="info-section">
    <h3><i class="fas fa-file-contract"></i> Legal Documents & Documentation</h3>
    <p>Welcome to Cashier Finder. Please review our important documents below.</p>
    
    <div class="document-nav">
      <a href="javascript:void(0)" onclick="showDocument('terms-content')">Terms & Conditions</a>
      <a href="javascript:void(0)" onclick="showDocument('privacy-content')">Privacy Policy</a>
      <a href="javascript:void(0)" onclick="showDocument('manual-content')">User Manual</a>
    </div>
    
    <!-- Terms and Conditions -->
    <div id="terms-content" class="legal-document legal-document-content">
      <h4>1. Acceptance of Terms</h4>
      <p>By accessing and using the Cashier Finder Platform ("the Platform"), you agree to be bound by these Terms and Conditions and all applicable laws and regulations. If you do not agree with any part of these terms, you must not use the Platform.</p>
      
      <h4>2. Definitions</h4>
      <p><strong>"Platform"</strong>: Cashier Finder web application and related services</p>
      <p><strong>"User"</strong>: Any individual or entity using the Platform</p>
      <p><strong>"Customer"</strong>: User purchasing goods</p>
      <p><strong>"Cashier"</strong>: User selling goods</p>
      
      <h4>3. User Accounts</h4>
      <h5>3.1 Registration Requirements</h5>
      <p>Users must provide accurate and complete registration information. Each user may maintain only one account unless expressly authorized.</p>
      
      <h5>3.2 Account Types</h5>
      <p><strong>Customer Accounts</strong>: For purchasing products</p>
      <p><strong>Cashier Accounts</strong>: For selling products</p>
      <p><strong>Admin Accounts</strong>: For platform management (by invitation only)</p>
      
      <h4>4. Transactions and Payments</h4>
      <p><strong>Primary Method</strong>: Cash on Delivery (COD) via M-Pesa</p>
      <p>Payment confirmation requires valid M-Pesa transaction message. Users must ensure payment details match order specifications.</p>
      
      <h4>5. Limitation of Liability</h4>
      <p>The Platform acts as a facilitator and is not liable for quality, safety, or legality of products traded, accuracy of user-provided information, delivery delays, or payment disputes between users.</p>
      
      <p><em>Last Updated: {{ now.strftime('%Y-%m-%d') }}</em></p>
    </div>
    
    <!-- Privacy Policy -->
    <div id="privacy-content" class="legal-document legal-document-content" style="display: none;">
      <h4>1. Information We Collect</h4>
      <h5>1.1 Personal Information</h5>
      <p><strong>Registration Data</strong>: Username, password, role, phone number</p>
      <p><strong>Profile Information</strong>: Shop name, address</p>
      <p><strong>Transaction Data</strong>: Order details, payment confirmations</p>
      
      <h5>1.2 Automatically Collected Information</h5>
      <p>Usage patterns, device information, IP address, access times</p>
      
      <h4>2. How We Use Information</h4>
      <p><strong>Service Provision</strong>: Facilitate transactions, provide customer support</p>
      <p><strong>Communication</strong>: Transaction notifications, platform announcements</p>
      <p><strong>Analytics</strong>: Improve platform functionality and security</p>
      
      <h4>3. Information Sharing</h4>
      <p>We share necessary information between transaction parties for order fulfillment. We may disclose information when required by court orders or legal processes.</p>
      
      <h4>4. Data Security</h4>
      <p>Password hashing using industry-standard algorithms, SSL encryption for data transmission, regular security assessments, and access controls.</p>
      
      <h4>5. User Rights</h4>
      <p>Users may access their personal information, correct inaccurate data, request data export, and request account deletion.</p>
      
      <p><em>Last Updated: {{ now.strftime('%Y-%m-%d') }}</em></p>
    </div>
    
    <!-- User Manual -->
    <div id="manual-content" class="legal-document legal-document-content" style="display: none;">
      <h4>1. Platform Overview</h4>
      <p>Cashier Finder connects buyers with sellers in a streamlined digital marketplace featuring offline payment processing.</p>
      
      <h4>2. Getting Started</h4>
      <h5>2.1 Registration Process</h5>
      <ol>
        <li>Access Platform via URL</li>
        <li>Choose Customer or Cashier role</li>
        <li>Provide username, password, and contact details</li>
        <li>Complete role-specific profile information</li>
      </ol>
      
      <h4>3. User Role Guides</h4>
      <h5>3.1 Customer Guide</h5>
      <p><strong>Browsing</strong>: Use category filters to find products</p>
      <p><strong>Order Process</strong>: Select products → Add to cart → Checkout → Delivery → Payment</p>
      
      <h5>3.2 Cashier Guide</h5>
      <p><strong>Profile Setup</strong>: Complete shop information</p>
      <p><strong>Product Management</strong>: Create product catalog and set prices</p>
      <p><strong>Order Fulfillment</strong>: Accept orders and arrange delivery</p>
      
      <h4>4. Payment Process</h4>
      <ol>
        <li>Receive and verify goods</li>
        <li>Complete M-Pesa payment to cashier</li>
        <li>Upload M-Pesa transaction message</li>
        <li>Mark order as received</li>
      </ol>
      
      <h4>5. Security Best Practices</h4>
      <ul>
        <li>Use strong, unique passwords</li>
        <li>Verify cashier credentials before ordering</li>
        <li>Keep transaction details confidential</li>
        <li>Report suspicious activity immediately</li>
      </ul>
      
      <h4>6. Support</h4>
      <p>For assistance, contact our support team via the platform's help center or email support.</p>
    </div>
  </div>

  <!-- Social Media Links Section -->
  <div class="card" style="text-align: center; margin-top: 2rem;">
    <h3><i class="fas fa-share-alt"></i> Connect With Us</h3>
    <p class="muted" style="margin-bottom: 1.5rem;">Follow us on social media for updates and support</p>
    
    <div style="display: flex; justify-content: center; gap: 1.5rem; flex-wrap: wrap;">
      <!-- WhatsApp -->
      <a href="https://wa.me/254713689808" 
         target="_blank" 
         class="btn-primary" 
         style="background: #25D366; display: flex; align-items: center; gap: 0.5rem;">
        <i class="fab fa-whatsapp"></i> WhatsApp
      </a>
      
      <!-- Facebook -->
      <a href="https://www.facebook.com" 
         target="_blank" 
         class="btn-primary" 
         style="background: #1877F2; display: flex; align-items: center; gap: 0.5rem;">
        <i class="fab fa-facebook"></i> Facebook
      </a>
      
      <!-- Twitter -->
      <a href="https://twitter.com" 
         target="_blank" 
         class="btn-primary" 
         style="background: #1DA1F2; display: flex; align-items: center; gap: 0.5rem;">
        <i class="fab fa-twitter"></i> Twitter
      </a>
    </div>
    
    <div style="margin-top: 1.5rem; padding: 1rem; background: #f8f9fa; border-radius: 8px;">
      <p class="muted" style="margin: 0;">
        <i class="fas fa-phone"></i> 
        <strong>Direct Contact:</strong> 
        <a href="tel:+254701207062" style="color: var(--primary); text-decoration: none;">
          +254 701207062
        </a>
      </p>
    </div>
  </div>

<footer>
  <p>&copy; {{ now.year }} Cashier Finder. All rights reserved. | 
     <a href="/terms">Terms</a> | 
     <a href="/privacy">Privacy</a> | 
     <a href="/docs">Help</a>
  </p>
</footer>
"""

category_products_html = """
<section>
  <div class="section-header">
    <h2 class="section-title"><i class="fas fa-tag"></i> Products in {{ category_name }}</h2>
  </div>
  <div class="grid">
    {% for p in products %}
      <article class="card">
        {% if p.image_url %}
          <img class="product" src="{{ p.image_url }}" alt="{{ p.name }}" 
               onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
          <div class="image-placeholder" style="display: none;">
            <i class="fas fa-image"></i>
          </div>
        {% else %}
          <div class="image-placeholder">
            <i class="fas fa-image"></i>
          </div>
        {% endif %}
        <h4>{{ p.name }}</h4>
        <p class="muted">{{ p.category }}{% if p.description %} · {{ p.description[:60] }}{% endif %}</p>
        <button class="btn-primary" onclick="goToCashiers('{{ url_for('product_cashiers', product_id=p.id) }}')">
          <i class="fas fa-map-marker-alt"></i> View Cashiers Nearby
        </button>
      </article>
    {% else %}
      <div class="card" style="grid-column: 1 / -1; text-align: center; padding: 3rem;">
        <i class="fas fa-box-open" style="font-size: 4rem; color: #dee2e6; margin-bottom: 1.5rem;"></i>
        <h3>No products in this category</h3>
        <p class="muted">No products found in the {{ category_name }} category.</p>
      </div>
    {% endfor %}
  </div>
</section>

<script>
function goToCashiers(targetUrl) {
  window.location.href = targetUrl;
}
</script>
"""

login_html = """
<section style="max-width: 450px; margin: 0 auto;">
  <div class="card">
    <h2 style="text-align: center; margin-bottom: 2rem; color: var(--primary);"><i class="fas fa-sign-in-alt"></i> Login</h2>
    <form method="post">
      {{ form.csrf_token }}
      <label>Username</label>
      {{ form.username(placeholder="Enter your username") }}
      {% if form.username.errors %}
        <div class="form-error">{{ form.username.errors[0] }}</div>
      {% endif %}
      
      <label>Password</label>
      {{ form.password(placeholder="Enter your password") }}
      {% if form.password.errors %}
        <div class="form-error">{{ form.password.errors[0] }}</div>
      {% endif %}
      
      <button type="submit" class="btn-primary" style="width: 100%; margin-top: 1rem;">Login</button>
    </form>
    <p class="muted" style="text-align: center; margin-top: 2rem;">
      Don't have an account? <a href="{{ url_for('register') }}" style="color: var(--primary); font-weight: 600;">Register here</a>
    </p>
  </div>
</section>
"""

register_html = """
<section style="max-width: 500px; margin: 0 auto;">
  <div class="card">
    <h2 style="text-align: center; margin-bottom: 2rem; color: var(--primary);"><i class="fas fa-user-plus"></i> Create Account</h2>
    <form method="post">
      {{ form.csrf_token }}
      <label>Username</label>
      {{ form.username(placeholder="Choose a username") }}
      {% if form.username.errors %}
        <div class="form-error">{{ form.username.errors[0] }}</div>
      {% endif %}
      
      <label>Password</label>
      {{ form.password(placeholder="Create a secure password") }}
      {% if form.password.errors %}
        <div class="form-error">{{ form.password.errors[0] }}</div>
      {% endif %}
      
      <label>Account Type</label>
      {{ form.role }}
      {% if form.role.errors %}
        <div class="form-error">{{ form.role.errors[0] }}</div>
      {% endif %}
      
      <label>Phone Number (optional)</label>
      {{ form.phone(placeholder="Enter your phone number") }}
      {% if form.phone.errors %}
        <div class="form-error">{{ form.phone.errors[0] }}</div>
      {% endif %}
      
      <button type="submit" class="btn-primary" style="width: 100%; margin-top: 1rem;">Create Account</button>
    </form>
    <div style="margin-top: 2rem; padding: 1.2rem; background: #fff3cd; border-radius: 10px; border-left: 4px solid #ffc107;">
      <i class="fas fa-exclamation-circle" style="color: #856404;"></i> 
      <span style="color: #856404; margin-left: 0.5rem;">Note: Admin registration requires special permissions.</span>
    </div>
    <p class="muted" style="text-align: center; margin-top: 2rem;">
      Already have an account? <a href="{{ url_for('login') }}" style="color: var(--primary); font-weight: 600;">Login here</a>
    </p>
  </div>
</section>
"""

catalogue_html = """
<section>
  <div class="section-header">
    <h2 class="section-title"><i class="fas fa-box-open"></i> Product Catalogue</h2>
  </div>
  
  <div class="card">
    <details open>
      <summary style="font-weight: 700; font-size: 1.2rem; cursor: pointer; color: var(--primary);">
        <i class="fas fa-plus-circle"></i> Add New Product
      </summary>
      <form method="post" enctype="multipart/form-data" style="margin-top: 2rem;">
        {{ form.csrf_token }}
        <div class="grid">
          <div>
            <label>Category</label>
            {{ form.category(placeholder="e.g., Electronics, Clothing") }}
            {% if form.category.errors %}
              <div class="form-error">{{ form.category.errors[0] }}</div>
            {% endif %}
          </div>
          <div>
            <label>Product Name</label>
            {{ form.name(placeholder="Enter product name") }}
            {% if form.name.errors %}
              <div class="form-error">{{ form.name.errors[0] }}</div>
            {% endif %}
          </div>
        <div>
            <label>Product Image</label>
            {{ form.image(accept="image/*", style="padding: 0.5rem;") }}
            {% if form.image.errors %}
                <div class="form-error">{{ form.image.errors[0] }}</div>
            {% endif %}
            <p class="muted">Max 2MB. Allowed formats: JPG, PNG, GIF</p>
        </div>
          <div>
            <label>Description</label>
            {{ form.description(placeholder="Brief product description") }}
            {% if form.description.errors %}
              <div class="form-error">{{ form.description.errors[0] }}</div>
            {% endif %}
          </div>
        </div>
        <button type="submit" class="btn-primary">Add Product</button>
      </form>
    </details>
  </div>
  
  <h3 style="margin: 2.5rem 0 1.5rem; color: var(--primary);">Current Products</h3>
  <div class="grid">
    {% for p in products %}
      <article class="card">
        {% if p.image_url %}
          <img class="product" src="{{ p.image_url }}" alt="{{ p.name }}" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
          <div class="image-placeholder" style="display: none;">
            <i class="fas fa-image"></i>
          </div>
        {% else %}
          <div class="image-placeholder">
            <i class="fas fa-image"></i>
          </div>
        {% endif %}
        <h4>{{ p.name }}</h4>
        <p class="muted">{{ p.category }}</p>
        <form method="post" action="{{ url_for('admin_delete_product', pid=p.id) }}" 
              onsubmit="return confirm('Are you sure you want to delete {{ p.name }}?');">
          <button class="btn-secondary" type="submit" style="width: 100%;">
            <i class="fas fa-trash"></i> Delete
          </button>
        </form>
      </article>
    {% else %}
      <div class="card" style="grid-column: 1 / -1; text-align: center; padding: 3rem;">
        <i class="fas fa-inbox" style="font-size: 4rem; color: #dee2e6; margin-bottom: 1.5rem;"></i>
        <h3>No products in catalogue</h3>
        <p class="muted">Add your first product using the form above.</p>
      </div>
    {% endfor %}
  </div>
</section>
"""

cashier_profile_html = """
<section>
  <div class="section-header">
    <h2 class="section-title"><i class="fas fa-store-alt"></i> My Shop Profile</h2>
  </div>
  
  <div class="card">
    <form method="post">
      {{ form.csrf_token }}
      <div class="grid">
        <div>
          <label>Shop Name</label>
          {{ form.shop_name(placeholder="Your shop name", value=prof.shop_name if prof else '') }}
          {% if form.shop_name.errors %}
            <div class="form-error">{{ form.shop_name.errors[0] }}</div>
          {% endif %}
        </div>
        <div>
          <label>Phone Number</label>
          {{ form.phone(placeholder="Phone for customers", value=prof.phone if prof else (user.phone or '')) }}
          {% if form.phone.errors %}
            <div class="form-error">{{ form.phone.errors[0] }}</div>
          {% endif %}
        </div>
      </div>
      
      <label>Address</label>
      {{ form.address(placeholder="Full shop address", value=prof.address if prof else '') }}
      {% if form.address.errors %}
        <div class="form-error">{{ form.address.errors[0] }}</div>
      {% endif %}
      
      <button type="submit" class="btn-primary">
        <i class="fas fa-save"></i> Save Profile
      </button>
    </form>
  </div>
</section>
"""

cashier_dashboard_html = """
<section>
  <div class="section-header">
    <h2 class="section-title"><i class="fas fa-tags"></i> My Offers</h2>
  </div>
  
  <div class="card">
    <details open>
      <summary style="font-weight: 700; font-size: 1.2rem; cursor: pointer; color: var(--primary);">
        <i class="fas fa-plus-circle"></i> Add/Update Offer
      </summary>
      <form method="post" action="{{ url_for('cashier_add_offer') }}" style="margin-top: 2rem;">
        {{ form.csrf_token }}
        <label>Product</label>
        <select name="product_id" required style="padding: 0.9rem;">
          <option value="" disabled selected>Select a product</option>
          {% for p in products %}
            <option value="{{ p.id }}">{{ p.name }} ({{ p.category }})</option>
          {% endfor %}
        </select>
        
        <label>Commodity Name</label>
        {{ form.commodity_name(placeholder="Name your offer") }}
        {% if form.commodity_name.errors %}
          <div class="form-error">{{ form.commodity_name.errors[0] }}</div>
        {% endif %}
        
        <div class="grid">
          <div>
            <label>Price (KES)</label>
            {{ form.price(placeholder="0.00", step="0.01", min="0") }}
            {% if form.price.errors %}
              <div class="form-error">{{ form.price.errors[0] }}</div>
            {% endif %}
          </div>
          <div>
            <label>Quantity</label>
            {{ form.quantity(placeholder="Available units", min="1") }}
            {% if form.quantity.errors %}
              <div class="form-error">{{ form.quantity.errors[0] }}</div>
            {% endif %}
          </div>
        </div>
        
        <label>Commodity Description</label>
        {{ form.commodity_description(placeholder="Describe your offer") }}
        {% if form.commodity_description.errors %}
          <div class="form-error">{{ form.commodity_description.errors[0] }}</div>
        {% endif %}
        
        <label>Payment Terms</label>
        {{ form.payment_terms(placeholder="Cash/MPesa/30-days...") }}
        {% if form.payment_terms.errors %}
          <div class="form-error">{{ form.payment_terms.errors[0] }}</div>
        {% endif %}
        
        <label>Delivery Terms</label>
        {{ form.delivery_terms(placeholder="Pickup/Within 10km...") }}
        {% if form.delivery_terms.errors %}
          <div class="form-error">{{ form.delivery_terms.errors[0] }}</div>
        {% endif %}
        
        <button type="submit" class="btn-primary">
          <i class="fas fa-save"></i> Save Offer
        </button>
      </form>
    </details>
  </div>
  
  <h3 style="margin: 2.5rem 0 1.5rem; color: var(--primary);">Current Offers</h3>
  {% if offers %}
    <div style="overflow-x: auto;">
      <table role="grid">
        <thead>
          <tr>
            <th>Category</th>
            <th>Product</th>
            <th>Commodity</th>
            <th>Price</th>
            <th>Qty</th>
            <th>Payment</th>
            <th>Delivery</th>
            <th>Updated</th>
          </tr>
        </thead>
        <tbody>
        {% for o in offers %}
          <tr>
            <td>{{ o.category_name }}</td>
            <td>{{ o.product_name }}</td>
            <td>{{ o.commodity_name }}</td>
            <td>KES {{ '%.2f'|format(o.price) }}</td>
            <td>{{ o.quantity or '-' }}</td>
            <td>{{ o.payment_terms or '-' }}</td>
            <td>{{ o.delivery_terms or '-' }}</td>
            <td>{{ o.created_at.strftime('%Y-%m-%d %H:%M') }}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  {% else %}
    <div class="card" style="text-align: center; padding: 3rem;">
      <i class="fas fa-tags" style="font-size: 4rem; color: #dee2e6; margin-bottom: 1.5rem;"></i>
      <h3>No offers yet</h3>
      <p class="muted">Create your first offer using the form above.</p>
      </div>
  {% endif %}
</section>
"""

product_cashiers_html = """
<section>
  <div class="section-header">
    <h2 class="section-title">Cashiers for {{ product.name }}</h2>
  </div>
  
  {% set active_cashier = get_active_cashier() %}
  
  {% if active_cashier %}
    <div class="shop-status shop-open" style="margin-bottom: 2rem;">
      <span class="status-badge status-open"><i class="fas fa-store"></i> Shop Open</span>
      <p style="margin: 0; font-weight: 600;">Currently serving: {{ active_cashier[1] }}</p>
    </div>
    
    {% if results %}
      <div class="grid">
        {% for w in results %}
          <article class="card">
            <h4>{{ w.shop_name }}</h4>
            <p class="muted">{{ w.address or 'Address not specified' }}</p>
            
            <div style="margin: 1.2rem 0; padding: 1.2rem; background: #f0f9ff; border-radius: 12px; border-left: 4px solid var(--primary);">
              <h5 style="margin: 0 0 0.5rem; color: var(--primary);">{{ w.commodity_name }}</h5>
              <p class="muted" style="margin: 0;">{{ w.commodity_description or 'No description' }}</p>
            </div>
            
            <div class="grid" style="grid-template-columns: 1fr 1fr; gap: 0.8rem; margin: 1.2rem 0;">
              <div>
                <strong>Price:</strong><br>
                <span style="color: var(--primary); font-weight: 700; font-size: 1.1rem;">KES {{ '%.2f'|format(w.price) }}</span>
              </div>
              <div>
                <strong>Available:</strong><br>
                {{ w.quantity or 'N/A' }}
              </div>
            </div>
            
            <div style="margin: 1.2rem 0;">
              <strong>Payment:</strong> {{ w.payment_terms or 'Not specified' }}<br>
              <strong>Delivery:</strong> {{ w.delivery_terms or 'Not specified' }}
            </div>
            
            <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 1.5rem;">
              {% if user and user.role == 'customer' %}
                <form method="post" action="{{ url_for('add_to_cart', offer_id=w.id) }}" style="display: flex; align-items: center; gap: 0.8rem;">
                  <input type="number" name="quantity" value="1" min="1" max="{{ w.quantity or 100 }}" style="width: 70px; padding: 0.5rem; border-radius: 8px;">
                  <button type="submit" class="btn-primary" style="padding: 0.6rem 1.2rem;">
                    <i class="fas fa-cart-plus"></i> Add to Cart
                  </button>
                </form>
              {% endif %}
              
              {% if w.phone %}
                <a href="tel:{{ w.phone }}" class="btn-secondary" style="padding: 0.6rem 1.2rem;">
                  <i class="fas fa-phone"></i> Call
                </a>
              {% endif %}
            </div>
          </article>
        {% endfor %}
      </div>
    {% else %}
      <div class="card" style="text-align: center; padding: 3rem;">
        <i class="fas fa-tags" style="font-size: 4rem; color: #dee2e6; margin-bottom: 1.5rem;"></i>
        <h3>No Active Offers</h3>
        <p class="muted">The active cashier doesn't have offers for this product at the moment.</p>
        <a href="{{ url_for('app_home') }}" class="btn-primary">Browse Other Products</a>
      </div>
    {% endif %}
  {% else %}
    <div class="shop-status shop-closed" style="margin-bottom: 2rem;">
      <span class="status-badge status-closed"><i class="fas fa-store-slash"></i> Shop Closed</span>
      <p style="margin: 0;">No active cashier available at the moment. Please check back later.</p>
    </div>
    
    <div class="card" style="text-align: center; padding: 3rem;">
      <i class="fas fa-store-slash" style="font-size: 4rem; color: #dee2e6; margin-bottom: 1.5rem;"></i>
      <h3>Service Unavailable</h3>
      <p class="muted">We're currently closed and not taking any orders at this time.</p>
      <p class="muted">Our business hours are Monday-Friday, 8:00 AM - 6:00 PM.</p>
      <div style="margin-top: 2rem;">
        <a href="{{ url_for('home') }}" class="btn-primary">Return to Home</a>
      </div>
    </div>
  {% endif %}
</section>
"""

order_form_html = """
<section style="max-width: 650px; margin: 0 auto; padding: 1rem;">
  <div class="card" style="padding: 2rem; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
    <h2 style="text-align: center; margin-bottom: 2rem; color: var(--primary);">
      <i class="fas fa-shopping-cart"></i> Place Order
    </h2>

    <!-- Offer Details -->
    <div class="card" style="margin-bottom: 2rem; background: #f8f9fa; padding: 1.5rem; border-radius: 12px;">
      <h4>{{ offer.commodity_name }}</h4>
      <p class="muted">{{ offer.product_name }} ({{ offer.category_name }})</p>
      <div style="display: flex; justify-content: space-between; margin-top: 0.5rem;">
        <div><strong>Price:</strong> KES {{ '%.2f'|format(offer.price) }}</div>
        <div><strong>Available:</strong> {{ offer.quantity or 'N/A' }}</div>
      </div>
      <div style="margin-top: 1rem;">
        <strong>Cashier:</strong> {{ offer.shop_name }}<br>
        <strong>Contact:</strong> {{ offer.phone }}
      </div>
    </div>

    <!-- Order Form -->
    <form method="post" novalidate>
      {{ form.csrf_token }}

      <!-- Quantity -->
      <label for="quantity">Quantity</label>
      {{ form.quantity(id="quantity", placeholder="Enter quantity", min="1", max=offer.quantity) }}
      {% if form.quantity.errors %}
        <div class="form-error" style="color: red;">{{ form.quantity.errors[0] }}</div>
      {% endif %}

      <!-- Delivery Address -->
      <label for="delivery_address">Delivery Address</label>
      {{ form.delivery_address(id="delivery_address", placeholder="Enter your complete delivery address", rows="3") }}
      {% if form.delivery_address.errors %}
        <div class="form-error" style="color: red;">{{ form.delivery_address.errors[0] }}</div>
      {% endif %}

      <!-- Payment Terms -->
      <div style="background: #fff3cd; padding: 1.5rem; border-radius: 12px; margin: 1.5rem 0; border-left: 4px solid #ffc107;">
        <h4 style="margin-top: 0; color: #856404;">Payment Terms</h4>
        <p style="margin-bottom: 0.5rem; color: #856404;">
          You agree to pay <strong>KES {{ '%.2f'|format(offer.price * (form.quantity.data or 1)) }}</strong> 
          to <strong>{{ offer.phone }}</strong> via M-Pesa after delivery.
        </p>
        <p style="margin-bottom: 0; color: #856404;">
          You will need to provide the M-Pesa transaction message as proof of payment.
        </p>
      </div>

      <!-- Terms Agreement -->
      <div style="margin-bottom: 1rem;">
        {{ form.terms_agreed }} 
        <label for="terms_agreed">I agree to pay upon delivery to the cashier's phone number</label>
        {% if form.terms_agreed.errors %}
          <div class="form-error" style="color: red;">{{ form.terms_agreed.errors[0] }}</div>
        {% endif %}
      </div>

      <!-- Submit Button -->
      <button type="submit" class="btn-primary" style="width: 100%; margin-top: 1.5rem; padding: 0.75rem; font-size: 1rem;">
        <i class="fas fa-check-circle"></i> Confirm Order
      </button>
    </form>
  </div>
</section>
"""

cashier_orders_html = """
<section>
  <div class="section-header">
    <h2 class="section-title"><i class="fas fa-shopping-cart"></i> My Orders</h2>
  </div>
  
  {% if orders %}
    <div style="overflow-x: auto;">
      <table role="grid">
        <thead>
          <tr>
            <th>Order ID</th>
            <th>Category</th>
            <th>Product</th>
            <th>Customer</th>
            <th>Quantity</th>
            <th>Total</th>
            <th>Status</th>
            <th>Order Date</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
        {% for order in orders %}
          <tr>
            <td>#{{ order.id }}</td>
            <td>{{ order.category_name}}</td>
            <td>{{ order.product_name }}<br><small class="muted">{{ order.commodity_name }}</small></td>
            <td>{{ order.customer_name }}<br><small class="muted">{{ order.customer_phone or 'No phone' }}</small></td>
            <td>{{ order.quantity }}</td>
            <td>KES {{ '%.2f'|format(order.total_price) }}</td>
            <td>
              <span class="order-status status-{{ order.status }}">
                {{ order.status|title }}
              </span>
            </td>
            <td>{{ order.created_at.strftime('%Y-%m-%d %H:%M') }}</td>
            <td>
              {% if order.status == 'pending' %}
                <form method="post" action="{{ url_for('update_order_status', order_id=order.id, status='confirmed') }}" style="display: inline;">
                  <button type="submit" class="btn-secondary" style="padding: 0.4rem 0.8rem;">
                    Confirm
                  </button>
                </form>
              {% elif order.status == 'confirmed' %}
                <form method="post" action="{{ url_for('update_order_status', order_id=order.id, status='completed') }}" style="display: inline;">
                  <button type="submit" class="btn-primary" style="padding: 0.4rem 0.8rem;">
                    Complete
                  </button>
                </form>
              {% elif order.status == 'completed' %}
                <div style="display: flex; flex-direction: column; gap: 0.4rem;">
                  <div style="background: #d1fae5; padding: 0.6rem; border-radius: 8px; text-align: center; color: #065f46;">
                    <i class="fas fa-check-circle"></i> Completed
                  </div>
                  <button class="btn-secondary" style="padding: 0.4rem;"
                          onclick="viewReceipt('{{ url_for('view_receipt', order_id=order.id) }}')">
                    <i class="fas fa-receipt"></i> View Receipt
                  </button>
                </div>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  {% else %}
    <div class="card" style="text-align: center; padding: 3rem;">
      <i class="fas fa-shopping-cart" style="font-size: 4rem; color: #dee2e6; margin-bottom: 1.5rem;"></i>
      <h3>No orders yet</h3>
      <p class="muted">You haven't received any orders yet.</p>
    </div>
  {% endif %}
</section>

<script>
function confirmDownload(url) {
  if (confirm('Download this order receipt for your business records?')) {
    window.open(url, '_blank');
  }
}

function confirmPrint(url) {
  if (confirm('Do you want to print this receipt now?')) {
    const printWindow = window.open(url, '_blank');
    printWindow.onload = () => {
      try {
        printWindow.print();
      } catch (err) {
        alert('Unable to connect to printer. Please check your Bluetooth or printer connection.');
      }
    };
  }
}

function viewReceipt(url) {
  window.open(url, '_blank', 'width=600,height=700,scrollbars=yes');
}
</script>
"""
customer_orders_html = """
<section style="padding: 2rem;">
  <div class="section-header" style="text-align: center; margin-bottom: 2rem;">
    <h2 class="section-title" style="font-size: 1.8rem; color: var(--primary);">
      <i class="fas fa-shopping-cart"></i> My Orders
    </h2>
  </div>

  {% if orders %}
    <div class="grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1.5rem;">
      {% for order in orders %}
        <article class="card" style="padding: 1.5rem; border-radius: 12px; box-shadow: 0 2px 6px rgba(0,0,0,0.05); display: flex; flex-direction: column; justify-content: space-between;">
          
          <!-- Order Info -->
          <h4>{{ order.product_name }}</h4>
          <p class="muted">{{ order.commodity_name }}</p>
          <p class="muted"><small>Category: {{ order.category_name }}</small></p>

          <div style="margin: 1rem 0;">
            <div style="display: flex; justify-content: space-between;">
              <div><strong>Quantity:</strong> {{ order.quantity }}</div>
              <div><strong>Total:</strong> KES {{ '%.2f'|format(order.total_price) }}</div>
            </div>
            <div style="margin-top: 0.5rem;">
              <strong>Status:</strong>
              <span class="order-status status-{{ order.status }}" style="text-transform: capitalize;">{{ order.status }}</span>
            </div>
            <div style="margin-top: 0.5rem;">
              <strong>Order Date:</strong> {{ order.created_at.strftime('%Y-%m-%d %H:%M') }}
            </div>
            {% if order.confirmed_at %}
              <div style="margin-top: 0.5rem;">
                <strong>Confirmed:</strong> {{ order.confirmed_at.strftime('%Y-%m-%d %H:%M') }}
              </div>
            {% endif %}
          </div>

          <!-- Action Buttons -->
          <div style="margin-top: auto;">
            {% if order.status == 'pending' %}
              <form method="post" action="{{ url_for('confirm_order', order_id=order.id) }}" style="display: inline;">
                <button type="submit" class="btn-primary" style="width: 100%; padding: 0.75rem;">
                  <i class="fas fa-check"></i> Confirm Order
                </button>
              </form>
            {% elif order.status == 'confirmed' %}
              <a href="{{ url_for('complete_order', order_id=order.id) }}" class="btn-primary" style="width: 100%; display: block; text-align: center; padding: 0.75rem;">
                <i class="fas fa-money-bill-wave"></i> Complete Payment
              </a>
            {% elif order.status == 'completed' %}
              <div style="background: #d1fae5; padding: 1rem; border-radius: 10px; text-align: center; color: #065f46; margin-bottom: 1rem;">
                <i class="fas fa-check-circle"></i> Order Completed
              </div>

              <div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">
                <button class="btn-secondary" style="flex: 1; min-width: 120px;"
                        onclick="viewReceipt('{{ url_for('view_receipt', order_id=order.id) }}')">
                  <i class="fas fa-receipt"></i> View Receipt
                </button>
              </div>
            {% endif %}
          </div>
        </article>
      {% endfor %}
    </div>
  {% else %}
    <div class="card" style="text-align: center; padding: 3rem; border-radius: 12px; background: #f8f9fa;">
      <i class="fas fa-shopping-cart" style="font-size: 4rem; color: #dee2e6; margin-bottom: 1.5rem;"></i>
      <h3>No orders yet</h3>
      <p class="muted">You haven't placed any orders yet.</p>
      <a href="{{ url_for('home') }}" class="btn-primary" style="padding: 0.75rem 1.5rem;">Browse Products</a>
    </div>
  {% endif %}
</section>

<script>
function viewReceipt(url) {
  window.open(url, '_blank', 'width=600,height=700,scrollbars=yes');
}
</script>
"""

admin_orders_html = """
<section>
  <div class="section-header">
    <h2 class="section-title"><i class="fas fa-shopping-cart"></i> All Orders</h2>
  </div>
  
  {% if orders %}
    <div style="overflow-x: auto;">
      <table role="grid">
        <thead>
          <tr>
            <th>Order ID</th>
            <th>Category</th>
            <th>Product</th>
            <th>Customer</th>
            <th>Cashier</th>
            <th>Quantity</th>
            <th>Total</th>
            <th>Status</th>
            <th>Order Date</th>
            <th>Payment Proof</th>
          </tr>
        </thead>
        <tbody>
        {% for order in orders %}
          <tr>
            <td>#{{ order.id }}</td>
            <td>{{ order.category_name }}<br><small class="muted">{{ order.commodity_name }}</small></td>            
            <td>{{ order.product_name }}<br><small class="muted">{{ order.commodity_name }}</small></td>
            <td>{{ order.customer_name }}<br><small class="muted">{{ order.customer_phone or 'No phone' }}</small></td>
            <td>{{ order.cashier_name }}<br><small class="muted">{{ order.cashier_phone or 'No phone' }}</small></td>
            <td>{{ order.quantity }}</td>
            <td>KES {{ '%.2f'|format(order.total_price) }}</td>
            <td>
              <span class="order-status status-{{ order.status }}">
                {{ order.status|title }}
              </span>
            </td>
            <td>{{ order.created_at.strftime('%Y-%m-%d %H:%M') }}</td>
            <td>
              {% if order.mpesa_message %}
                <button onclick="document.getElementById('message-{{ order.id }}').showModal()" class="btn-secondary" style="padding: 0.4rem 0.8rem;">
                  View
                </button>
                <dialog id="message-{{ order.id }}">
                  <article style="max-width: 600px;">
                    <header>
                      <button aria-label="Close" rel="prev" onclick="document.getElementById('message-{{ order.id }}').close()"></button>
                      <h3>Payment Proof for Order #{{ order.id }}</h3>
                    </header>
                    <div>
                      <p>{{ order.mpesa_message }}</p>
                      <p><small>Received: {{ order.payment_date.strftime('%Y-%m-%d %H:%M') if order.payment_date else 'N/A' }}</small></p>
                    </div>
                    <footer>
                      <button onclick="document.getElementById('message-{{ order.id }}').close()" class="btn-secondary">Close</button>
                    </footer>
                  </article>
                </dialog>
              {% else %}
                <span class="muted">None</span>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  {% else %}
    <div class="card" style="text-align: center; padding: 3rem;">
      <i class="fas fa-shopping-cart" style="font-size: 4rem; color: #dee2e6; margin-bottom: 1.5rem;"></i>
      <h3>No orders yet</h3>
      <p class="muted">No orders have been placed yet.</p>
    </div>
  {% endif %}
</section>
"""
complete_order_html = """
<section>
  <div class="section-header">
    <h2 class="section-title"><i class="fas fa-money-bill-wave"></i> Complete Payment</h2>
  </div>
  
  <div class="card">
    <h3>Order Summary</h3>
    <div style="background: #f8f9fa; padding: 1.5rem; border-radius: 8px; margin: 1rem 0;">
      <h4>{{ order.commodity_name }}</h4>
      <p class="muted">{{ order.product_name }} ({{ order.category_name }})</p>
      <div style="display: flex; justify-content: space-between; margin-top: 1rem;">
        <div><strong>Quantity:</strong> {{ order.quantity }}</div>
        <div><strong>Total Amount:</strong> KES {{ '%.2f'|format(order.total_price) }}</div>
      </div>
      <div style="margin-top: 0.5rem;">
        <strong>Cashier:</strong> {{ order.shop_name }}<br>
        <strong>Phone:</strong> {{ order.cashier_phone }}
      </div>
    </div>
    
    <form method="post">
      {{ form.csrf_token }}
      
      <label>M-Pesa Transaction Message</label>
      {{ form.mpesa_message(placeholder="Paste your M-Pesa confirmation message here...", rows="4") }}
      {% if form.mpesa_message.errors %}
        <div class="form-error">{{ form.mpesa_message.errors[0] }}</div>
      {% endif %}
      <p class="muted">Example: "Confirmed. KSh{{ '%.2f'|format(order.total_price) }} paid to {{ order.cashier_phone }} on {{ now.strftime('%d/%m/%Y') }} at {{ now.strftime('%H:%M') }}. Transaction ID: ABC123XYZ"</p>
      
      <div style="margin: 1.5rem 0;">
        {{ form.received_confirmation }} 
        <label for="received_confirmation">I confirm that I have received the goods and made payment via M-Pesa</label>
        {% if form.received_confirmation.errors %}
          <div class="form-error">{{ form.received_confirmation.errors[0] }}</div>
        {% endif %}
      </div>
      
      <button type="submit" class="btn-primary" style="width: 100%;">
        <i class="fas fa-check-circle"></i> Complete Payment
      </button>
    </form>
  </div>
</section>
"""

admin_stats_html = """
<section>
  <div class="section-header">
    <h2 class="section-title"><i class="fas fa-chart-bar"></i> Admin Dashboard</h2>
  </div>
  
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">Total Users</div>
      <div class="stat-value">{{ stats.users_total }}</div>
      <div class="muted">
        {{ stats.customers }} Customers<br>
        {{ stats.cashiers }} Cashiers<br>
        {{ stats.admins }} Admins
      </div>
    </div>
    
    <div class="stat-card">
      <div class="stat-label">Total Products</div>
      <div class="stat-value">{{ stats.products_total }}</div>
    </div>
    
    <div class="stat-card">
      <div class="stat-label">Total Offers</div>
      <div class="stat-value">{{ stats.offers_total }}</div>
    </div>
    
    <div class="stat-card">
      <div class="stat-label">Total Orders</div>
      <div class="stat-value">{{ stats.orders_total }}</div>
      <div class="muted">
        {{ stats.pending_orders }} Pending<br>
        {{ stats.confirmed_orders }} Confirmed<br>
        {{ stats.completed_orders }} Completed
      </div>
    </div>
    
    <div class="stat-card">
      <div class="stat-label">Total Revenue</div>
      <div class="stat-value">KES {{ '%.2f'|format(stats.total_revenue) }}</div>
    </div>
    
    <div class="stat-card">
      <div class="stat-label">Avg. Order Value</div>
      <div class="stat-value">KES {{ '%.2f'|format(stats.avg_order_value) }}</div>
    </div>
  </div>
  
  <h3 style="margin: 2.5rem 0 1.5rem; color: var(--primary);">Recent Orders</h3>
  {% if recent_orders %}
    <div style="overflow-x: auto;">
      <table role="grid">
        <thead>
          <tr>
            <th>Order ID</th>
            <th>Category</th>
            <th>Product</th>
            <th>Customer</th>
            <th>Cashier</th>
            <th>Amount</th>
            <th>Status</th>
            <th>Date</th>
          </tr>
        </thead>
        <tbody>
          {% for order in recent_orders %}
            <tr>
              <td>#{{ order.id }}</td>
               <td>{{ order.category_name }}</td>
              <td>{{ order.product_name }}</td>
              <td>{{ order.customer_name }}</td>
              <td>{{ order.cashier_name }}</td>
              <td>KES {{ '%.2f'|format(order.total_price) }}</td>
              <td>
                <span class="order-status status-{{ order.status }}">
                  {{ order.status|title }}
                </span>
              </td>
              <td>{{ order.created_at.strftime('%Y-%m-%d %H:%M') }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  {% else %}
    <div class="card" style="text-align: center; padding: 3rem;">
      <i class="fas fa-shopping-cart" style="font-size: 4rem; color: #dee2e6; margin-bottom: 1.5rem;"></i>
      <h3>No recent orders</h3>
      <p class="muted">No orders have been placed recently.</p>
    </div>
  {% endif %}
  
  <h3 style="margin: 2.5rem 0 1.5rem; color: var(--primary);">Transaction Messages</h3>
  {% if transaction_messages %}
    <div class="activity-log">
      {% for message in transaction_messages %}
        <div class="activity-item">
          <strong>Order #{{ message.order_id }} - {{ message.message_type|replace('_', ' ')|title }}</strong>
          <span class="muted">({{ message.created_at.strftime('%Y-%m-%d %H:%M') }})</span>
          <br>
          <span class="muted">{{ message.message_text }}</span>
        </div>
      {% endfor %}
    </div>
  {% else %}
    <div class="card" style="text-align: center; padding: 3rem;">
      <i class="fas fa-comments" style="font-size: 4rem; color: #dee2e6; margin-bottom: 1.5rem;"></i>
      <h3>No transaction messages</h3>
      <p class="muted">No transaction messages have been sent yet.</p>
    </div>
  {% endif %}
</section>
"""

cart_html = """
<section>
  <div class="section-header" style="display:flex; justify-content:space-between; align-items:center; margin-bottom:1.5rem;">
    <h2 class="section-title"><i class="fas fa-shopping-cart"></i> Shopping Cart</h2>
    <a href="{{ url_for('app_home') }}" class="btn-primary" style="padding:0.5rem 1rem; border-radius:6px;">
      <i class="fas fa-plus"></i> Continue Shopping
    </a>
  </div>
  
  {% if cart_items %}
    <div class="grid" style="gap:1.5rem;">
      {% for item in cart_items %}
        <article class="card" style="padding:1rem; border-radius:10px; box-shadow:0 2px 6px rgba(0,0,0,0.05);">
          <h4 style="margin:0 0 0.3rem 0;">{{ item.commodity_name }}</h4>
          <p class="muted" style="margin:0 0 0.3rem 0;">{{ item.product_name }} · {{ item.shop_name }}</p>
          <p class="muted" style="margin:0 0 0.6rem 0;"><small>Address: {{ item.shop_address or 'N/A' }}</small></p>
          
          <div style="display:flex; justify-content:space-between; align-items:center; margin:1.2rem 0;">
            <div>
              <strong>Price:</strong> KES {{ '%.2f'|format(item.price) }} each
            </div>
            <form method="post" action="{{ url_for('update_cart', item_id=item.id) }}" style="display:flex; align-items:center; gap:0.8rem;">
              <label style="margin:0;">Qty:</label>
              <input type="number" name="quantity" value="{{ item.quantity }}" min="1" style="width:70px; padding:0.5rem; border-radius:6px; border:1px solid #ccc;">
              <button type="submit" class="btn-secondary" style="padding:0.5rem 0.8rem; border-radius:6px;">
                <i class="fas fa-sync"></i> Update
              </button>
            </form>
          </div>
          
          <div style="border-top:1px solid #e9ecef; padding-top:1.2rem; font-weight:600;">
            <strong>Subtotal:</strong> KES {{ '%.2f'|format(item.price * item.quantity) }}
          </div>
        </article>
      {% endfor %}
    </div>
    
    <div class="card" style="margin-top:2rem; padding:1rem; border-radius:10px; box-shadow:0 2px 6px rgba(0,0,0,0.05);">
      <h3 style="margin-top:0;">Order Summary</h3>
      <div style="display:flex; justify-content:space-between; font-size:1.2rem; margin:1rem 0;">
        <strong>Total:</strong>
        <strong>KES {{ '%.2f'|format(total) }}</strong>
      </div>
      
      <form method="post" action="{{ url_for('checkout') }}" style="margin-top:1.5rem;">
        <label style="display:block; margin-bottom:0.4rem; font-weight:600;">Delivery Address</label>
        <textarea name="delivery_address" required placeholder="Enter your complete delivery address" rows="3" style="width:100%; padding:0.6rem; border:1px solid #ccc; border-radius:6px;"></textarea>
        
        <div style="background:#fff3cd; padding:1.5rem; border-radius:12px; margin:1.5rem 0; border-left:4px solid #ffc107;">
          <h4 style="margin-top:0; color:#856404;">Payment Terms</h4>
          <p style="margin-bottom:0.5rem; color:#856404;">You agree to pay upon delivery to the cashier's phone number.</p>
          <p style="margin-bottom:0; color:#856404;">You will need to provide the M-Pesa transaction message as proof of payment.</p>
        </div>
        
        <button type="submit" class="btn-primary" style="width:100%; padding:0.8rem; border-radius:8px;">
          <i class="fas fa-check-circle"></i> Proceed to Checkout
        </button>
      </form>
    </div>
  {% else %}
    <div class="card" style="text-align:center; padding:3rem; border-radius:10px; box-shadow:0 2px 6px rgba(0,0,0,0.05);">
      <i class="fas fa-shopping-cart" style="font-size:4rem; color:#dee2e6; margin-bottom:1.5rem;"></i>
      <h3>Your cart is empty</h3>
      <p class="muted">Add some items to your cart to get started.</p>
      <a href="{{ url_for('app_home') }}" class="btn-primary" style="padding:0.5rem 1rem; border-radius:6px;">Browse Products</a>
    </div>
  {% endif %}
</section>
"""

order_session_html = """
<section>
  <div class="section-header" style="margin-bottom:1.5rem;">
    <h2 class="section-title"><i class="fas fa-history"></i> Order Session</h2>
  </div>
  
  <div class="grid" style="gap:1.5rem;">
    <!-- Cart Summary -->
    <div class="card" style="padding:1rem; border-radius:10px; box-shadow:0 2px 6px rgba(0,0,0,0.05);">
      <h3><i class="fas fa-shopping-cart"></i> Cart Summary</h3>
      {% if cart_count > 0 %}
        <p>You have <strong>{{ cart_count }}</strong> item{{ 's' if cart_count > 1 else '' }} in your cart.</p>
        <a href="{{ url_for('view_cart') }}" class="btn-primary" style="width:100%; padding:0.6rem; border-radius:6px; display:block; text-align:center;">
          <i class="fas fa-shopping-cart"></i> View Cart
        </a>
      {% else %}
        <p class="muted">Your cart is empty.</p>
        <a href="{{ url_for('app_home') }}" class="btn-primary" style="width:100%; padding:0.6rem; border-radius:6px; display:block; text-align:center;">
          <i class="fas fa-plus"></i> Add Items
        </a>
      {% endif %}
    </div>
    
    <!-- Pending Actions -->
    <div class="card" style="padding:1rem; border-radius:10px; box-shadow:0 2px 6px rgba(0,0,0,0.05);">
      <h3><i class="fas fa-tasks"></i> Pending Actions</h3>
      {% if pending_orders %}
        <p>You have <strong>{{ pending_orders|length }}</strong> order{{ 's' if pending_orders|length > 1 else '' }} requiring action.</p>
        <div class="activity-log" style="max-height:250px; overflow-y:auto;">
          {% for order in pending_orders %}
            <div class="activity-item" style="border-bottom:1px solid #e9ecef; padding:0.6rem 0;">
              <strong>Order #{{ order.id }}</strong>: {{ order.quantity }} x {{ order.commodity_name }}<br>
              <span class="muted" style="color:#555;">Status: {{ order.status|title }}</span>
              <div style="margin-top:0.6rem;">
                {% if order.status == 'pending' %}
                  <form method="post" action="{{ url_for('confirm_order', order_id=order.id) }}" style="display:inline;">
                    <button type="submit" class="btn-secondary" style="padding:0.4rem 0.8rem; border-radius:6px;">Confirm</button>
                  </form>
                {% elif order.status == 'confirmed' %}
                  <a href="{{ url_for('complete_order', order_id=order.id) }}" class="btn-primary" style="padding:0.4rem 0.8rem; border-radius:6px;">Complete Payment</a>
                {% endif %}
              </div>
            </div>
          {% endfor %}
        </div>
      {% else %}
        <p class="muted">No pending actions.</p>
      {% endif %}
    </div>
  </div>
  
  <!-- Recent Activity -->
  <div class="card" style="margin-top:2rem; padding:1rem; border-radius:10px; box-shadow:0 2px 6px rgba(0,0,0,0.05);">
    <h3><i class="fas fa-clock"></i> Recent Activity</h3>
    <p>Your order session was last updated on <strong>{{ now.strftime('%Y-%m-%d %H:%M') }}</strong>.</p>
    <p>You can continue where you left off even after logging out.</p>
  </div>
</section>
"""

# --- HTML Templates ---
media_center_html = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }}</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
<style>
body {
    font-family:'Segoe UI', sans-serif;
    margin:0;
    padding:0;
    background: linear-gradient(135deg,#667eea 0%,#764ba2 100%);
    color:#333;
}
.container {
    max-width:1200px;
    margin:0 auto;
    padding:20px;
}
.header {
    text-align:center;
    margin-bottom:30px;
    color:#fff;
}
.header h2 { font-size:2.5em; margin-bottom:10px; }
.header p { font-size:1.1em; }

.flash-messages { margin-bottom:20px; }
.flash-error {
    background:#e74c3c;
    color:#fff;
    padding:15px;
    border-radius:8px;
    margin-bottom:15px;
}
.flash-success {
    background:#27ae60;
    color:#fff;
    padding:15px;
    border-radius:8px;
    margin-bottom:15px;
}
.flash-info {
    background:#3498db;
    color:#fff;
    padding:15px;
    border-radius:8px;
    margin-bottom:15px;
}

.grid {
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
    gap:25px;
}
.card {
    background:rgba(255,255,255,0.95);
    border-radius:15px;
    padding:25px;
    box-shadow:0 8px 32px rgba(0,0,0,0.1);
    transition:0.3s;
    display:flex;
    flex-direction:column;
    justify-content:space-between;
}
.card:hover {
    transform:translateY(-5px);
    box-shadow:0 12px 40px rgba(0,0,0,0.15);
}
.card h3 {
    color:#2c3e50;
    margin-bottom:10px;
}
.card p {
    color:#555;
    margin-bottom:10px;
}
.card em {
    color:#7f8c8d;
    font-size:0.9em;
    display:block;
    margin-bottom:15px;
}
.content-body {
    margin-top:15px;
    padding:15px;
    background:#f8f9fa;
    border-radius:8px;
    border-left:4px solid #667eea;
}
.content-body h4 {
    margin-top:0;
    color:#2c3e50;
    font-size:1.1em;
}
.content-text {
    white-space:pre-wrap;
    line-height:1.6;
    max-height:200px;
    overflow-y:auto;
    padding:10px;
    background:white;
    border-radius:5px;
}

/* Buttons */
.btn-primary {
    display:inline-block;
    background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
    color:white !important;
    padding:12px 25px;
    text-decoration:none;
    border-radius:8px;
    font-weight:600;
    transition:0.3s;
    border:none;
    cursor:pointer;
    text-align:center;
}
.btn-primary:hover {
    transform:translateY(-2px);
    box-shadow:0 5px 15px rgba(102,126,234,0.4);
}

/* Admin buttons */
.admin-actions {
    margin-top:15px;
    display:flex;
    flex-wrap:wrap;
    gap:10px;
}
.admin-actions a {
    font-size:0.9em;
    text-decoration:none;
    padding:8px 15px;
    border-radius:5px;
    display:inline-flex;
    align-items:center;
    gap:5px;
    color:#fff !important;
    font-weight:600;
}
.admin-actions a.edit { background:#2980b9; }
.admin-actions a.delete { background:#c0392b; }
.admin-actions a.edit:hover { background:#3498db; }
.admin-actions a.delete:hover { background:#e74c3c; }

.empty-state {
    text-align:center;
    padding:60px 20px;
    color:#fff;
}
.empty-state i {
    font-size:4em;
    margin-bottom:20px;
    opacity:0.8;
}

@media(max-width:768px){
    .grid{grid-template-columns:1fr;}
    .admin-actions { justify-content:center; }
}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h2><i class="fas fa-broadcast-tower"></i> Featured Documentaries & Articles</h2>
        <p>Informing and educating the public through responsible journalism and original productions.</p>
    </div>

    <!-- Flash messages -->
    <div class="flash-messages">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
    </div>

    {% if articles %}
    <div class="grid">
        {% for item in articles %}
        <div class="card">
            <div>
                <h3>{{ item.title }}</h3>
                <p><em>By {{ item.author }} | {{ item.date }}</em></p>
                <p>{{ item.summary }}</p>
                
                <!-- Display content if available -->
                {% if item.content %}
                <div class="content-body">
                    <h4>Full Content:</h4>
                    <div class="content-text">{{ item.content }}</div>
                </div>
                {% endif %}
            </div>

            <div>
                <!-- Always show Read/Watch if available -->
                {% if item.link %}
                <a href="{{ item.link }}" class="btn-primary" target="_blank">
                    <i class="fas fa-external-link-alt"></i> Read / Watch
                </a>
                {% endif %}

                <!-- Admin-only buttons -->
                {% if user and user.role == 'admin' %}
                <div class="admin-actions">
                    <a href="{{ url_for('media_upload', media_id=item.id) }}" class="edit">
                        <i class="fas fa-edit"></i> Edit
                    </a>
                    <a href="{{ url_for('media_delete', media_id=item.id) }}" class="delete"
                       onclick="return confirm('Delete this media?');">
                        <i class="fas fa-trash"></i> Delete
                    </a>
                </div>
                {% endif %}
            </div>
        </div>
        {% endfor %}
    </div>
    {% else %}
    <div class="empty-state">
        <i class="fas fa-newspaper"></i>
        <h3>No Media Content Available</h3>
        <p>Check back later for new documentaries and articles.</p>
    </div>
    {% endif %}
</div>
</body>
</html>
"""

media_upload_html = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }}</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
<style>
body { font-family:'Segoe UI', sans-serif; margin:0; padding:0; background:linear-gradient(135deg,#667eea 0%,#764ba2 100%); color:#333; }
.container { max-width:800px; margin:0 auto; padding:20px; }
.header { text-align:center; margin-bottom:30px; color:#fff; }
.header h2 { font-size:2.2em; }
.header p { font-size:1.1em; }
.flash-messages { margin-bottom:20px; }
.flash-error { background:#e74c3c; color:#fff; padding:15px; border-radius:8px; margin-bottom:15px; }
.flash-success { background:#27ae60; color:#fff; padding:15px; border-radius:8px; margin-bottom:15px; }
.upload-form { background:rgba(255,255,255,0.95); border-radius:15px; padding:40px; box-shadow:0 8px 32px rgba(0,0,0,0.1); border:1px solid rgba(255,255,255,0.2); }
.form-group { margin-bottom:25px; }
label { display:block; margin-bottom:8px; font-weight:600; color:#2c3e50; }
input[type="text"], textarea, input[type="file"] { width:100%; padding:12px 15px; border:2px solid #e1e8ed; border-radius:8px; font-size:16px; transition:0.3s; }
input[type="text"]:focus, textarea:focus { outline:none; border-color:#667eea; }

/* ✅ Fixed textarea scrolling and formatting */
textarea {
  resize: vertical;
  min-height: 120px;
  max-height: 600px;
  overflow-y: auto;
  font-family: inherit;
  white-space: pre-wrap;
  word-wrap: break-word;
}

.btn-submit { background:linear-gradient(135deg,#667eea 0%,#764ba2 100%); color:white; padding:15px 30px; border:none; border-radius:8px; font-size:16px; font-weight:600; cursor:pointer; transition:0.3s; width:100%; }
.btn-submit:hover { transform:translateY(-2px); box-shadow:0 5px 15px rgba(102,126,234,0.4); }
.file-info { margin-top:5px; font-size:0.9em; color:#7f8c8d; }
.preview-container { margin-top:15px; }
.preview-container video, .preview-container img, .preview-container iframe { max-width:100%; border-radius:8px; margin-top:10px; }
.size-warning { color:#e74c3c; font-weight:600; margin-top:5px; }
@media(max-width:768px){.container{padding:10px;}.upload-form{padding:25px;}.header h2{font-size:1.8em;}}
</style>
</head>
<body>
<div class="container">
<div class="header">
<h2><i class="fas fa-upload"></i> Upload / Edit Media</h2>
<p>Upload documentaries, articles, videos, or PDFs to AWS S3 for public display.</p>
</div>

<div class="flash-messages">
{% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
        {% for category, message in messages %}
            <div class="flash-{{ category }}">{{ message }}</div>
        {% endfor %}
    {% endif %}
{% endwith %}
</div>

<div class="upload-form">
<form method="POST" enctype="multipart/form-data">
<div class="form-group">
<label for="title">Title *</label>
<input type="text" id="title" name="title" value="{{ media.title if media else '' }}" required placeholder="Enter media title...">
</div>

<div class="form-group">
<label for="summary">Summary / Description *</label>
<textarea id="summary" name="summary" rows="4" required placeholder="Provide a brief description...">{{ media.summary if media else '' }}</textarea>
</div>

<div class="form-group">
<label for="content">Content / Main Text</label>
<textarea id="content" name="content" rows="6" placeholder="Paste the main content here...">{{ media.content if media else '' }}</textarea>
</div>

<div class="form-group">
<label for="author">Author *</label>
<input type="text" id="author" name="author" value="{{ media.author if media else session.get('user','Admin') }}" required placeholder="Enter author name...">
</div>

<div class="form-group">
<label for="media_file">Media File</label>
<input type="file" id="media_file" name="media_file" accept="image/*,video/*,application/pdf">
<div class="file-info">
Accepted formats: Images, Videos, PDF documents. Maximum file size: 50MB.
</div>
<div class="size-warning">Leave empty to keep existing file.</div>

{% if media and media.link %}
<div class="preview-container">
<strong>Current File:</strong> <a href="{{ media.link }}" target="_blank">{{ media.link.split('/')[-1] }}</a>
{% if media.link.endswith(('.jpg','.jpeg','.png','.gif')) %}
<img src="{{ media.link }}" alt="Current Media" style="max-height:200px;">
{% elif media.link.endswith(('.mp4','.webm','.ogg','.mov','.avi')) %}
<video controls style="max-height:200px;">
<source src="{{ media.link }}">
Your browser does not support the video tag.
</video>
{% elif media.link.endswith('.pdf') %}
<iframe src="{{ media.link }}" width="100%" height="400px"></iframe>
{% endif %}
</div>
{% endif %}
</div>

<button type="submit" class="btn-submit"><i class="fas fa-cloud-upload-alt"></i> Submit</button>
</form>
</div>
</div>
</body>
</html>
"""

# ---- HTML TEMPLATE ----
landing_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cashier Finder Business Systems | Professional SaaS Solutions</title>
    <style>
        :root {
            --primary: #2563eb;
            --primary-dark: #1d4ed8;
            --secondary: #0f172a;
            --accent: #f59e0b;
            --light: #f8fafc;
            --gray: #64748b;
            --success: #10b981;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        
        body {
            color: var(--secondary);
            line-height: 1.6;
            background-color: #f1f5f9;
        }
        
        .container {
            width: 100%;
            max-width: 1200px;
            margin: 0 auto;
            padding: 0 20px;
        }
        
        /* Header Styles */
        header {
            background-color: white;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
            position: fixed;
            width: 100%;
            top: 0;
            z-index: 1000;
        }
        
        .header-container {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 15px 0;
        }
        
        .logo {
            font-size: 1.8rem;
            font-weight: 700;
            color: var(--primary);
        }
        
        .logo span {
            color: var(--accent);
        }
        
        nav ul {
            display: flex;
            list-style: none;
        }
        
        nav ul li {
            margin-left: 30px;
        }
        
        nav ul li a {
            text-decoration: none;
            color: var(--secondary);
            font-weight: 500;
            transition: color 0.3s;
        }
        
        nav ul li a:hover {
            color: var(--primary);
        }
        
        .mobile-menu {
            display: none;
            font-size: 1.5rem;
            cursor: pointer;
        }
        
        /* Hero Section */
        .hero {
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
            color: white;
            padding: 150px 0 100px;
            text-align: center;
        }
        
        .hero h1 {
            font-size: 3rem;
            margin-bottom: 20px;
            line-height: 1.2;
        }
        
        .hero p {
            font-size: 1.2rem;
            max-width: 700px;
            margin: 0 auto 30px;
            opacity: 0.9;
        }
        
        .cta-buttons {
            display: flex;
            justify-content: center;
            gap: 15px;
            margin-top: 30px;
        }
        
        .btn {
            display: inline-block;
            padding: 12px 30px;
            border-radius: 5px;
            text-decoration: none;
            font-weight: 600;
            transition: all 0.3s;
            cursor: pointer;
            border: none;
            font-size: 1rem;
        }
        
        .btn-primary {
            background-color: var(--accent);
            color: var(--secondary);
        }
        
        .btn-primary:hover {
            background-color: #e69500;
            transform: translateY(-2px);
        }
        
        .btn-secondary {
            background-color: transparent;
            color: white;
            border: 2px solid white;
        }
        
        .btn-secondary:hover {
            background-color: white;
            color: var(--primary);
            transform: translateY(-2px);
        }
        
        /* Features Section */
        .features {
            padding: 100px 0;
            background-color: white;
        }
        
        .section-title {
            text-align: center;
            margin-bottom: 60px;
        }
        
        .section-title h2 {
            font-size: 2.5rem;
            color: var(--secondary);
            margin-bottom: 15px;
        }
        
        .section-title p {
            color: var(--gray);
            max-width: 600px;
            margin: 0 auto;
        }
        
        .features-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 30px;
        }
        
        .feature-card {
            background-color: var(--light);
            padding: 30px;
            border-radius: 10px;
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.05);
            transition: transform 0.3s;
        }
        
        .feature-card:hover {
            transform: translateY(-5px);
        }
        
        .feature-icon {
            font-size: 2.5rem;
            color: var(--primary);
            margin-bottom: 20px;
        }
        
        .feature-card h3 {
            font-size: 1.5rem;
            margin-bottom: 15px;
        }
        
        .feature-card p {
            color: var(--gray);
        }
        
        /* Pricing Section */
        .pricing {
            padding: 100px 0;
            background-color: #f8fafc;
        }
        
        .pricing-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 30px;
        }
        
        .pricing-card {
            background-color: white;
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.05);
            transition: transform 0.3s;
            position: relative;
        }
        
        .pricing-card:hover {
            transform: translateY(-5px);
        }
        
        .pricing-card.popular {
            border: 2px solid var(--primary);
        }
        
        .popular-badge {
            position: absolute;
            top: 0;
            right: 0;
            background-color: var(--primary);
            color: white;
            padding: 5px 15px;
            font-size: 0.8rem;
            font-weight: 600;
            border-bottom-left-radius: 5px;
        }
        
        .pricing-header {
            padding: 30px;
            text-align: center;
            background-color: var(--light);
        }
        
        .pricing-header h3 {
            font-size: 1.5rem;
            margin-bottom: 10px;
        }
        
        .price {
            font-size: 2.5rem;
            font-weight: 700;
            color: var(--primary);
            margin-bottom: 5px;
        }
        
        .price-period {
            color: var(--gray);
        }
        
        .pricing-features {
            padding: 30px;
        }
        
        .pricing-features ul {
            list-style: none;
        }
        
        .pricing-features ul li {
            padding: 10px 0;
            border-bottom: 1px solid #e2e8f0;
            display: flex;
            align-items: center;
        }
        
        .pricing-features ul li:last-child {
            border-bottom: none;
        }
        
        .pricing-features ul li:before {
            content: "✓";
            color: var(--success);
            font-weight: bold;
            margin-right: 10px;
        }
        
        .pricing-footer {
            padding: 0 30px 30px;
            text-align: center;
        }
        
        /* Business Models Section */
        .models {
            padding: 100px 0;
            background-color: white;
        }
        
        .models-container {
            display: flex;
            flex-wrap: wrap;
            gap: 40px;
            align-items: center;
        }
        
        .models-text {
            flex: 1;
            min-width: 300px;
        }
        
        .models-text h2 {
            font-size: 2.5rem;
            margin-bottom: 20px;
            color: var(--secondary);
        }
        
        .models-text p {
            color: var(--gray);
            margin-bottom: 30px;
        }
        
        .models-visual {
            flex: 1;
            min-width: 300px;
            background-color: var(--light);
            padding: 40px;
            border-radius: 10px;
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.05);
        }
        
        .model-tabs {
            display: flex;
            margin-bottom: 30px;
            border-bottom: 1px solid #e2e8f0;
        }
        
        .model-tab {
            padding: 10px 20px;
            cursor: pointer;
            font-weight: 500;
            border-bottom: 2px solid transparent;
        }
        
        .model-tab.active {
            border-bottom: 2px solid var(--primary);
            color: var(--primary);
        }
        
        .model-content {
            display: none;
        }
        
        .model-content.active {
            display: block;
        }
        
        .model-content h3 {
            margin-bottom: 15px;
            font-size: 1.5rem;
        }
        
        .model-content ul {
            list-style: none;
            margin-bottom: 20px;
        }
        
        .model-content ul li {
            padding: 8px 0;
            display: flex;
            align-items: center;
        }
        
        .model-content ul li:before {
            content: "•";
            color: var(--primary);
            font-weight: bold;
            margin-right: 10px;
        }
        
        /* CTA Section */
        .cta {
            padding: 100px 0;
            background: linear-gradient(135deg, var(--secondary) 0%, #1e293b 100%);
            color: white;
            text-align: center;
        }
        
        .cta h2 {
            font-size: 2.5rem;
            margin-bottom: 20px;
        }
        
        .cta p {
            max-width: 600px;
            margin: 0 auto 30px;
            opacity: 0.9;
        }
        
        /* Footer */
        footer {
            background-color: var(--secondary);
            color: white;
            padding: 70px 0 30px;
        }
        
        .footer-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 40px;
            margin-bottom: 50px;
        }
        
        .footer-col h3 {
            font-size: 1.2rem;
            margin-bottom: 20px;
            position: relative;
            padding-bottom: 10px;
        }
        
        .footer-col h3:after {
            content: '';
            position: absolute;
            left: 0;
            bottom: 0;
            width: 40px;
            height: 2px;
            background-color: var(--accent);
        }
        
        .footer-col ul {
            list-style: none;
        }
        
        .footer-col ul li {
            margin-bottom: 10px;
        }
        
        .footer-col ul li a {
            color: #cbd5e1;
            text-decoration: none;
            transition: color 0.3s;
        }
        
        .footer-col ul li a:hover {
            color: white;
        }
        
        .copyright {
            text-align: center;
            padding-top: 30px;
            border-top: 1px solid #334155;
            color: #94a3b8;
            font-size: 0.9rem;
        }
        
        /* Responsive Styles */
        @media (max-width: 768px) {
            .hero h1 {
                font-size: 2.2rem;
            }
            
            .cta-buttons {
                flex-direction: column;
                align-items: center;
            }
            
            .btn {
                width: 100%;
                max-width: 300px;
            }
            
            nav ul {
                display: none;
            }
            
            .mobile-menu {
                display: block;
            }
            
            .mobile-nav {
                position: fixed;
                top: 70px;
                left: 0;
                width: 100%;
                background-color: white;
                box-shadow: 0 5px 10px rgba(0, 0, 0, 0.1);
                padding: 20px;
                display: none;
            }
            
            .mobile-nav.active {
                display: block;
            }
            
            .mobile-nav ul {
                list-style: none;
            }
            
            .mobile-nav ul li {
                margin-bottom: 15px;
            }
            
            .mobile-nav ul li a {
                text-decoration: none;
                color: var(--secondary);
                font-weight: 500;
                display: block;
                padding: 10px 0;
            }
        }
    </style>
</head>
<body>
    <!-- Header -->
    <header>
        <div class="container header-container">
            <div class="logo">Cashier Finder<span>Systems</span></div>
            <nav>
                <ul>
                    <li><a href="#home">Home</a></li>
                    <li><a href="#features">Features</a></li>
                    <li><a href="#pricing">Pricing</a></li>
                    <li><a href="#models">Business Models</a></li>
                    <li><a href="#contact">Contact</a></li>
                </ul>
            </nav>
            <div class="mobile-menu">☰</div>
        </div>
        <div class="mobile-nav">
            <ul>
                <li><a href="#home">Home</a></li>
                <li><a href="#features">Features</a></li>
                <li><a href="#pricing">Pricing</a></li>
                <li><a href="#models">Business Models</a></li>
                <li><a href="#contact">Contact</a></li>
            </ul>
        </div>
    </header>

    <!-- Hero Section -->
    <section class="hero" id="home">
      <div class="container">
         <h1>Professional Business Systems for     Growing Organizations</h1>
        <p>
          Streamline your operations with our customizable SaaS solutions.
          Choose between cloud-hosted services or self-hosted installations tailored to your business needs.
        </p>
        <div class="cta-buttons">
          <a href="{{ url_for('app_home') }}" target="_blank" rel="noopener noreferrer" class="btn btn-primary">
              Try Our Service
          </a>
          <a href="#contact" class="btn btn-secondary">
            Request Customized Service
          </a>
        </div>
      </div>
    </section>

    <!-- Features Section -->
    <section class="features" id="features">
        <div class="container">
            <div class="section-title">
                <h2>Why Choose Cashier Finder Systems</h2>
                <p>Our solutions are designed to help businesses of all sizes optimize their operations and scale efficiently.</p>
            </div>
            <div class="features-grid">
                <div class="feature-card">
                    <div class="feature-icon">🚀</div>
                    <h3>Rapid Deployment</h3>
                    <p>Get your system up and running in days, not months. Our streamlined setup process ensures quick implementation.</p>
                </div>
                <div class="feature-card">
                    <div class="feature-icon">💼</div>
                    <h3>Flexible Business Models</h3>
                    <p>Choose between SaaS subscriptions or self-hosted licenses to match your budget and technical requirements.</p>
                </div>
                <div class="feature-card">
                    <div class="feature-icon">🛡️</div>
                    <h3>Secure & Reliable</h3>
                    <p>Built with security in mind. Our systems are hosted on robust infrastructure with regular backups and updates.</p>
                </div>
                <div class="feature-card">
                    <div class="feature-icon">📈</div>
                    <h3>Scalable Solutions</h3>
                    <p>Grow without limitations. Our systems scale with your business, accommodating increased users and data.</p>
                </div>
                <div class="feature-card">
                    <div class="feature-icon">🔧</div>
                    <h3>Customizable Features</h3>
                    <p>Tailor the system to your specific needs with custom modules, workflows, and integrations.</p>
                </div>
                <div class="feature-card">
                    <div class="feature-icon">📞</div>
                    <h3>Dedicated Support</h3>
                    <p>Get assistance when you need it with our responsive support team available during business hours.</p>
                </div>
            </div>
        </div>
    </section>

    <!-- Pricing Section -->
    <section class="pricing" id="pricing">
        <div class="container">
            <div class="section-title">
                <h2>Transparent Pricing</h2>
                <p>Choose the plan that fits your organization's needs and budget.</p>
            </div>
            <div class="pricing-grid">
                <div class="pricing-card">
                    <div class="pricing-header">
                        <h3>Starter</h3>
                        <div class="price">KSh 3,000</div>
                        <div class="price-period">per month</div>
                    </div>
                    <div class="pricing-features">
                        <ul>
                            <li>Hosted on our Fly.io infrastructure</li>
                            <li>Up to 200 users</li>
                            <li>Standard support</li>
                            <li>Basic features</li>
                            <li>Regular updates</li>
                        </ul>
                    </div>
                    <div class="pricing-footer">
                        <a href="{{ url_for('subscribe') }}" class="btn btn-primary">Get Started</a>
                    </div>
                </div>
                <div class="pricing-card popular">
                    <div class="popular-badge">Most Popular</div>
                    <div class="pricing-header">
                        <h3>Pro</h3>
                        <div class="price">KSh 5,000</div>
                        <div class="price-period">per month</div>
                    </div>
                    <div class="pricing-features">
                        <ul>
                            <li>Hosted on our Fly.io infrastructure</li>
                            <li>Up to 1,000 users</li>
                            <li>Premium support</li>
                            <li>Advanced features</li>
                            <li>Priority updates</li>
                            <li>Custom branding options</li>
                        </ul>
                    </div>
                    <div class="pricing-footer">
                        <a href="{{ url_for('subscribe') }}" class="btn btn-primary">Get Started</a>
                    </div>
                </div>
                <div class="pricing-card">
                    <div class="pricing-header">
                        <h3>Enterprise</h3>
                        <div class="price">KSh 15,000</div>
                        <div class="price-period">setup + KSh 5,000/year</div>
                    </div>
                    <div class="pricing-features">
                        <ul>
                            <li>Installed on client's server</li>
                            <li>Unlimited users</li>
                            <li>Includes setup and training</li>
                            <li>All features included</li>
                            <li>Annual maintenance included</li>
                            <li>Custom development options</li>
                        </ul>
                    </div>
                    <div class="pricing-footer">
                        <a href="#contact" class="btn btn-secondary">Request Quote</a>
                    </div>
                </div>
            </div>
        </div>
    </section>

    <!-- Business Models Section -->
    <section class="models" id="models">
        <div class="container">
            <div class="section-title">
                <h2>Flexible Business Models</h2>
                <p>We offer two main approaches to suit different organizational needs and preferences.</p>
            </div>
            <div class="models-container">
                <div class="models-text">
                    <h2>Choose Your Deployment Strategy</h2>
                    <p>Whether you prefer the convenience of cloud hosting or the control of self-hosting, we have a solution that fits your requirements.</p>
                    <p>Our flexible approach allows you to start small and scale as your business grows, with the option to transition between models if needed.</p>
                    <a href="#contact" class="btn btn-primary">Discuss Your Needs</a>
                </div>
                <div class="models-visual">
                    <div class="model-tabs">
                        <div class="model-tab active" data-tab="saas">SaaS (Cloud Hosted)</div>
                        <div class="model-tab" data-tab="self-hosted">Self-Hosted License</div>
                    </div>
                    <div class="model-content active" id="saas-content">
                        <h3>Software as a Service</h3>
                        <p>We host everything on our secure Fly.io infrastructure. You just log in and use the system online.</p>
                        <ul>
                            <li>No server maintenance required</li>
                            <li>Automatic updates and backups</li>
                            <li>Pay monthly or yearly subscription</li>
                            <li>Access from anywhere with internet</li>
                            <li>Scalable resources</li>
                        </ul>
                        <p><strong>Ideal for:</strong> Small to medium businesses, organizations without dedicated IT staff, those wanting minimal technical overhead.</p>
                    </div>
                    <div class="model-content" id="self-hosted-content">
                        <h3>Self-Hosted License</h3>
                        <p>We install the application on your own server infrastructure (VPS, company cloud, etc.).</p>
                        <ul>
                            <li>Full control over your data and infrastructure</li>
                            <li>One-time setup fee + optional annual maintenance</li>
                            <li>Customizable to your specific requirements</li>
                            <li>No recurring subscription fees</li>
                            <li>Works offline within your network</li>
                        </ul>
                        <p><strong>Ideal for:</strong> Large organizations, government entities, businesses with strict data governance requirements.</p>
                    </div>
                </div>
            </div>
        </div>
    </section>

    <!-- Footer -->
    <footer>
        <div class="container">
            <div class="footer-grid">
                <div class="footer-col">
                    <h3>Cashier Finder Systems</h3>
                    <p>Professional business solutions tailored for African businesses and organizations.</p>
                </div>
                <div class="footer-col">
                    <h3>Quick Links</h3>
                    <ul>
                        <li><a href="#home">Home</a></li>
                        <li><a href="#features">Features</a></li>
                        <li><a href="#pricing">Pricing</a></li>
                        <li><a href="#models">Business Models</a></li>
                    </ul>
                </div>
                <div class="footer-col">
                    <h3>Services</h3>
                    <ul>
                        <li><a href="#saas-content">SaaS Solutions</a></li>
                        <li><a href="#self-hosted-content">Self-Hosted Licenses</a></li>
                        <li><a href="#contact">Custom Development</a></li>
                        <li><a href="#contact">Support & Maintenance</a></li>
                    </ul>
                </div>
                <div class="footer-col">
                    <h3>Contact</h3>
                    <ul>
                        <li>James Boyid Ochuna</li>
                        <li>Whatsapp: 0701207062</a></li>
                        <li>Nairobi, Kenya</li>
                    </ul>
                </div>
            </div>
            <div class="copyright">
                <p>&copy; 2023 Cashier Finder  Business Systems. All Rights Reserved.</p>
            </div>
        </div>
    </footer>

    <script>
        // Mobile menu toggle
        document.querySelector('.mobile-menu').addEventListener('click', function() {
            document.querySelector('.mobile-nav').classList.toggle('active');
        });

        // Model tabs functionality
        const tabs = document.querySelectorAll('.model-tab');
        tabs.forEach(tab => {
            tab.addEventListener('click', function() {
                // Remove active class from all tabs and contents
                tabs.forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.model-content').forEach(content => {
                    content.classList.remove('active');
                });
                
                // Add active class to clicked tab and corresponding content
                this.classList.add('active');
                const tabId = this.getAttribute('data-tab');
                document.getElementById(tabId + '-content').classList.add('active');
            });
        });

        // Smooth scrolling for anchor links
        document.querySelectorAll('a[href^="#"]').forEach(anchor => {
            anchor.addEventListener('click', function (e) {
                e.preventDefault();
                
                const targetId = this.getAttribute('href');
                if (targetId === '#') return;
                
                const targetElement = document.querySelector(targetId);
                if (targetElement) {
                    window.scrollTo({
                        top: targetElement.offsetTop - 80,
                        behavior: 'smooth'
                    });
                    
                    // Close mobile menu if open
                    document.querySelector('.mobile-nav').classList.remove('active');
                }
            });
        });
    </script>
</body>
</html>
"""
subscribe_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Business Setup Service - Register & Subscribe</title>
    <style>
        :root {
            --primary: #2c3e50;
            --secondary: #3498db;
            --accent: #e74c3c;
            --light: #ecf0f1;
            --dark: #2c3e50;
            --success: #27ae60;
            --warning: #f39c12;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        
        body {
            background-color: #f5f7fa;
            color: var(--dark);
            line-height: 1.6;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        
        header {
            background: linear-gradient(135deg, var(--primary), var(--secondary));
            color: white;
            padding: 2rem 0;
            text-align: center;
            border-radius: 0 0 10px 10px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
        }
        
        header h1 {
            font-size: 2.5rem;
            margin-bottom: 0.5rem;
        }
        
        header p {
            font-size: 1.1rem;
            opacity: 0.9;
        }
        
        .progress-container {
            display: flex;
            justify-content: center;
            margin: 2rem 0;
        }
        
        .progress-step {
            display: flex;
            flex-direction: column;
            align-items: center;
            position: relative;
            width: 150px;
        }
        
        .progress-step:not(:last-child)::after {
            content: '';
            position: absolute;
            top: 15px;
            left: 70px;
            width: 80px;
            height: 2px;
            background-color: #ddd;
            z-index: 1;
        }
        
        .step-number {
            width: 30px;
            height: 30px;
            border-radius: 50%;
            background-color: #ddd;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-bottom: 0.5rem;
            z-index: 2;
            font-weight: bold;
        }
        
        .step-label {
            font-size: 0.9rem;
            text-align: center;
        }
        
        .progress-step.active .step-number {
            background-color: var(--secondary);
            color: white;
        }
        
        .progress-step.completed .step-number {
            background-color: var(--success);
            color: white;
        }
        
        .step-content {
            display: none;
            background: white;
            border-radius: 10px;
            padding: 2rem;
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.1);
            margin-bottom: 2rem;
        }
        
        .step-content.active {
            display: block;
            animation: fadeIn 0.5s ease;
        }
        
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        h2 {
            color: var(--primary);
            margin-bottom: 1.5rem;
            padding-bottom: 0.5rem;
            border-bottom: 2px solid var(--light);
        }
        
        .form-group {
            margin-bottom: 1.5rem;
        }
        
        label {
            display: block;
            margin-bottom: 0.5rem;
            font-weight: 600;
        }
        
        input, select, textarea {
            width: 100%;
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 5px;
            font-size: 1rem;
            transition: border 0.3s;
        }
        
        input:focus, select:focus, textarea:focus {
            border-color: var(--secondary);
            outline: none;
            box-shadow: 0 0 0 2px rgba(52, 152, 219, 0.2);
        }
        
        .btn {
            display: inline-block;
            background-color: var(--secondary);
            color: white;
            padding: 12px 24px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-size: 1rem;
            font-weight: 600;
            transition: all 0.3s;
            text-align: center;
        }
        
        .btn:hover {
            background-color: #2980b9;
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
        }
        
        .btn:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none !important;
        }
        
        .btn.loading {
            position: relative;
            color: transparent;
        }
        
        .btn.loading::after {
            content: '';
            position: absolute;
            width: 20px;
            height: 20px;
            top: 50%;
            left: 50%;
            margin-left: -10px;
            margin-top: -10px;
            border: 2px solid #ffffff;
            border-radius: 50%;
            border-top-color: transparent;
            animation: spin 1s ease-in-out infinite;
        }
        
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        .btn-outline {
            background-color: transparent;
            border: 2px solid var(--secondary);
            color: var(--secondary);
        }
        
        .btn-outline:hover {
            background-color: var(--secondary);
            color: white;
        }
        
        .btn-success {
            background-color: var(--success);
        }
        
        .btn-success:hover {
            background-color: #219653;
        }
        
        .btn-container {
            display: flex;
            justify-content: space-between;
            margin-top: 2rem;
        }
        
        .service-options {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 1.5rem;
            margin-top: 1rem;
        }
        
        .service-card {
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 1.5rem;
            transition: all 0.3s;
            cursor: pointer;
            position: relative;
        }
        
        .service-card:hover {
            border-color: var(--secondary);
            transform: translateY(-5px);
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.1);
        }
        
        .service-card.selected {
            border-color: var(--secondary);
            background-color: rgba(52, 152, 219, 0.05);
        }
        
        .service-card h3 {
            color: var(--primary);
            margin-bottom: 0.5rem;
        }
        
        .service-price {
            font-size: 1.5rem;
            font-weight: bold;
            color: var(--accent);
            margin: 1rem 0;
        }
        
        .service-features {
            list-style-type: none;
        }
        
        .service-features li {
            margin-bottom: 0.5rem;
            padding-left: 1.5rem;
            position: relative;
        }
        
        .service-features li:before {
            content: '✓';
            position: absolute;
            left: 0;
            color: var(--success);
            font-weight: bold;
        }
        
        .payment-info {
            background-color: rgba(52, 152, 219, 0.1);
            border-radius: 8px;
            padding: 1.5rem;
            margin: 1.5rem 0;
        }
        
        .mpesa-number {
            font-size: 1.8rem;
            font-weight: bold;
            color: var(--accent);
            text-align: center;
            margin: 1rem 0;
            letter-spacing: 2px;
        }
        
        .instructions {
            background-color: #fff9e6;
            border-left: 4px solid var(--warning);
            padding: 1rem;
            margin: 1rem 0;
        }
        
        .confirmation-message {
            text-align: center;
            padding: 2rem;
        }
        
        .confirmation-message h2 {
            color: var(--success);
            margin-bottom: 1rem;
        }
        
        .confirmation-icon {
            font-size: 4rem;
            color: var(--success);
            margin-bottom: 1rem;
        }
        
        footer {
            text-align: center;
            padding: 2rem 0;
            margin-top: 2rem;
            border-top: 1px solid #ddd;
            color: #7f8c8d;
        }
        
        @media (max-width: 768px) {
            .service-options {
                grid-template-columns: 1fr;
            }
            
            .btn-container {
                flex-direction: column;
                gap: 10px;
            }
            
            .btn {
                width: 100%;
            }
        }
    </style>
</head>
<body>
    <header>
        <div class="container">
            <h1>Business Setup Service</h1>
            <p>Register, subscribe, and get your customized business running in no time</p>
        </div>
    </header>
    
    <div class="container">
        <div class="progress-container">
            <div class="progress-step completed" id="step1">
                <div class="step-number">1</div>
                <div class="step-label">Registration</div>
            </div>
            <div class="progress-step active" id="step2">
                <div class="step-number">2</div>
                <div class="step-label">Service Selection</div>
            </div>
            <div class="progress-step" id="step3">
                <div class="step-number">3</div>
                <div class="step-label">Payment</div>
            </div>
            <div class="progress-step" id="step4">
                <div class="step-number">4</div>
                <div class="step-label">Confirmation</div>
            </div>
        </div>
        
        <!-- Step 1: Registration Form -->
        <div class="step-content active" id="registration-step">
            <h2>Create Your Account</h2>
            <form id="registration-form">
                <div class="form-group">
                    <label for="fullname">Full Name</label>
                    <input type="text" id="fullname" name="fullname" required>
                </div>
                
                <div class="form-group">
                    <label for="email">Email Address</label>
                    <input type="email" id="email" name="email" required>
                </div>
                
                <div class="form-group">
                    <label for="phone">Phone Number</label>
                    <input type="tel" id="phone" name="phone" required>
                </div>
                
                <div class="form-group">
                    <label for="password">Password</label>
                    <input type="password" id="password" name="password" required>
                </div>
                
                <div class="form-group">
                    <label for="confirm-password">Confirm Password</label>
                    <input type="password" id="confirm-password" name="confirm-password" required>
                </div>
                
                <div class="btn-container">
                    <div></div> <!-- Empty div for spacing -->
                    <button type="button" class="btn" id="next-to-service">Next: Select Service</button>
                </div>
            </form>
        </div>
        
        <!-- Step 2: Service Selection -->
        <div class="step-content" id="service-step">
            <h2>Choose Your Business Service Package</h2>
            <p>Select the service package that best fits your business needs. Each package includes setup, training, and ongoing support.</p>
            
            <div class="service-options">
                <div class="service-card" data-service="basic">
                    <h3>Basic Setup</h3>
                    <div class="service-price">KSh 5,000</div>
                    <ul class="service-features">
                        <li>Business registration guidance</li>
                        <li>Basic website setup</li>
                        <li>Social media integration</li>
                        <li>Email support for 30 days</li>
                    </ul>
                </div>
                
                <div class="service-card" data-service="standard">
                    <h3>Standard Package</h3>
                    <div class="service-price">KSh 15,000</div>
                    <ul class="service-features">
                        <li>All Basic features</li>
                        <li>Custom domain & hosting</li>
                        <li>E-commerce functionality</li>
                        <li>Payment gateway setup</li>
                        <li>3 months support</li>
                    </ul>
                </div>
                
                <div class="service-card" data-service="premium">
                    <h3>Premium Package</h3>
                    <div class="service-price">KSh 30,000</div>
                    <ul class="service-features">
                        <li>All Standard features</li>
                        <li>Custom design & branding</li>
                        <li>Mobile app development</li>
                        <li>SEO optimization</li>
                        <li>6 months premium support</li>
                        <li>Marketing strategy session</li>
                    </ul>
                </div>
                
                <div class="service-card" data-service="enterprise">
                    <h3>Enterprise Solution</h3>
                    <div class="service-price">KSh 50,000+</div>
                    <ul class="service-features">
                        <li>All Premium features</li>
                        <li>Multi-user management</li>
                        <li>Advanced analytics</li>
                        <li>Custom integrations</li>
                        <li>1 year dedicated support</li>
                        <li>Priority service</li>
                    </ul>
                </div>
            </div>
            
            <div class="form-group" style="margin-top: 2rem;">
                <label for="business-type">Type of Business</label>
                <select id="business-type" name="business-type">
                    <option value="">Select your business type</option>
                    <option value="retail">Retail</option>
                    <option value="service">Service Provider</option>
                    <option value="food">Food & Beverage</option>
                    <option value="tech">Technology</option>
                    <option value="consulting">Consulting</option>
                    <option value="other">Other</option>
                </select>
            </div>
            
            <div class="form-group">
                <label for="business-details">Additional Business Details (Optional)</label>
                <textarea id="business-details" name="business-details" rows="4" placeholder="Tell us more about your business needs..."></textarea>
            </div>
            
            <div class="btn-container">
                <button type="button" class="btn btn-outline" id="back-to-registration">Back</button>
                <button type="button" class="btn" id="next-to-payment">Next: Payment</button>
            </div>
        </div>
        
        <!-- Step 3: Payment -->
        <div class="step-content" id="payment-step">
            <h2>Complete Your Payment</h2>
            
            <div class="payment-info">
                <h3>Selected Service: <span id="selected-service-name">Basic Setup</span></h3>
                <p>Amount to Pay: <strong id="selected-service-price">KSh 5,000</strong></p>
            </div>
            
            <p>To complete your registration and begin your business setup, please make payment to the following M-Pesa number:</p>
            
            <div class="mpesa-number">0701207062</div>
            
            <div class="instructions">
                <h4>Payment Instructions:</h4>
                <ol>
                    <li>Go to M-Pesa on your phone</li>
                    <li>Select "Lipa Na M-Pesa"</li>
                    <li>Select "Pay Bill"</li>
                    <li>Enter Business Number: <strong> (3753684) (James Ochuna)</strong></li>
                    <li>Wait for confirmation message</li>
                </ol>
            </div>
            
            <div class="form-group">
                <label for="transaction-code">M-Pesa Transaction Code (Required)</label>
                <input type="text" id="transaction-code" name="transaction-code" placeholder="Enter the transaction code from M-Pesa" required>
                <small>This helps us verify your payment quickly</small>
            </div>
            
            <div class="btn-container">
                <button type="button" class="btn btn-outline" id="back-to-service">Back</button>
                <button type="button" class="btn btn-success" id="complete-payment">Complete Registration</button>
            </div>
        </div>
        
        <!-- Step 4: Confirmation -->
        <div class="step-content" id="confirmation-step">
            <div class="confirmation-message">
                <div class="confirmation-icon">✓</div>
                <h2>Registration Successful!</h2>
                <p>Thank you for registering with our Business Setup Service.</p>
                <p>We have received your payment confirmation and will begin setting up your business services.</p>
                <p>Our team will contact you within <strong>24 hours</strong> to discuss the next steps and schedule your setup.</p>
                <p>You will receive an email with your account details and setup timeline shortly.</p>
                
                <div style="margin-top: 2rem;">
                    <button type="button" class="btn" id="go-to-dashboard">Access Your Dashboard</button>
                </div>
            </div>
        </div>
    </div>
    
    <footer>
        <div class="container">
            <p>Need help? Contact us at pigasimucoke@gmail.com or call 0701207062</p>
            <p>&copy; 2023 Business Setup Service. All rights reserved.</p>
        </div>
    </footer>

    <script>
        // Current step tracking
        let currentStep = 1;
        
        // User data object to store form values
        const userData = {
            registration: {},
            service: {},
            payment: {}
        };
        
        // DOM elements
        const stepContents = document.querySelectorAll('.step-content');
        const progressSteps = document.querySelectorAll('.progress-step');
        
        // Service selection
        const serviceCards = document.querySelectorAll('.service-card');
        let selectedService = 'basic';
        
        // Service pricing
        const servicePrices = {
            basic: 5000,
            standard: 15000,
            premium: 30000,
            enterprise: 50000
        };
        
        // Service names
        const serviceNames = {
            basic: "Basic Setup",
            standard: "Standard Package",
            premium: "Premium Package",
            enterprise: "Enterprise Solution"
        };
        
        // Initialize event listeners
        function init() {
            // Next button from registration to service
            document.getElementById('next-to-service').addEventListener('click', function() {
                if (validateRegistration()) {
                    saveRegistrationData();
                    goToStep(2);
                }
            });
            
            // Back button from service to registration
            document.getElementById('back-to-registration').addEventListener('click', function() {
                goToStep(1);
            });
            
            // Next button from service to payment
            document.getElementById('next-to-payment').addEventListener('click', function() {
                if (validateServiceSelection()) {
                    saveServiceData();
                    updatePaymentDetails();
                    goToStep(3);
                }
            });
            
            // Back button from payment to service
            document.getElementById('back-to-service').addEventListener('click', function() {
                goToStep(2);
            });
            
            // Complete payment button
            document.getElementById('complete-payment').addEventListener('click', function() {
                if (validatePayment()) {
                    savePaymentData();
                    completeRegistration();
                }
            });
            
            // Go to dashboard button
            document.getElementById('go-to-dashboard').addEventListener('click', function() {
                alert('Dashboard functionality would be implemented here. For now, you can close this page.');
            });
            
            // Service card selection
            serviceCards.forEach(card => {
                card.addEventListener('click', function() {
                    // Remove selected class from all cards
                    serviceCards.forEach(c => c.classList.remove('selected'));
                    
                    // Add selected class to clicked card
                    this.classList.add('selected');
                    
                    // Update selected service
                    selectedService = this.getAttribute('data-service');
                });
            });
            
            // Set first service as selected by default
            document.querySelector('.service-card[data-service="basic"]').classList.add('selected');
        }
        
        // Navigate to specific step
        function goToStep(step) {
            // Hide all step contents
            stepContents.forEach(content => {
                content.classList.remove('active');
            });
            
            // Update progress steps
            progressSteps.forEach((progressStep, index) => {
                if (index + 1 < step) {
                    progressStep.classList.add('completed');
                    progressStep.classList.remove('active');
                } else if (index + 1 === step) {
                    progressStep.classList.add('active');
                    progressStep.classList.remove('completed');
                } else {
                    progressStep.classList.remove('active', 'completed');
                }
            });
            
            // Show current step content
            document.getElementById(getStepContentId(step)).classList.add('active');
            
            // Update current step
            currentStep = step;
        }
        
        // Get step content ID
        function getStepContentId(step) {
            switch(step) {
                case 1: return 'registration-step';
                case 2: return 'service-step';
                case 3: return 'payment-step';
                case 4: return 'confirmation-step';
                default: return 'registration-step';
            }
        }
        
        // Validate registration form
        function validateRegistration() {
            const fullname = document.getElementById('fullname').value;
            const email = document.getElementById('email').value;
            const phone = document.getElementById('phone').value;
            const password = document.getElementById('password').value;
            const confirmPassword = document.getElementById('confirm-password').value;
            
            if (!fullname || !email || !phone || !password || !confirmPassword) {
                alert('Please fill in all required fields.');
                return false;
            }
            
            if (password !== confirmPassword) {
                alert('Passwords do not match.');
                return false;
            }
            
            // Simple email validation
            const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
            if (!emailRegex.test(email)) {
                alert('Please enter a valid email address.');
                return false;
            }
            
            // Simple phone validation (Kenyan format)
            const phoneRegex = /^(\+?254|0)[17]\d{8}$/;
            if (!phoneRegex.test(phone)) {
                alert('Please enter a valid Kenyan phone number.');
                return false;
            }
            
            return true;
        }
        
        // Save registration data
        function saveRegistrationData() {
            userData.registration = {
                fullname: document.getElementById('fullname').value,
                email: document.getElementById('email').value,
                phone: document.getElementById('phone').value
            };
        }
        
        // Validate service selection
        function validateServiceSelection() {
            const businessType = document.getElementById('business-type').value;
            
            if (!businessType) {
                alert('Please select your business type.');
                return false;
            }
            
            return true;
        }
        
        // Save service data
        function saveServiceData() {
            userData.service = {
                selectedService: selectedService,
                businessType: document.getElementById('business-type').value,
                businessDetails: document.getElementById('business-details').value,
                amount: servicePrices[selectedService]
            };
        }
        
        // Update payment details based on selected service
        function updatePaymentDetails() {
            document.getElementById('selected-service-name').textContent = serviceNames[selectedService];
            document.getElementById('selected-service-price').textContent = `KSh ${servicePrices[selectedService].toLocaleString()}`;
        }
        
        // Validate payment
        function validatePayment() {
            const transactionCode = document.getElementById('transaction-code').value;
            
            if (!transactionCode) {
                alert('Please enter your M-Pesa transaction code.');
                return false;
            }
            
            // Simple transaction code validation (M-Pesa codes are typically 10 characters)
            if (transactionCode.length < 8) {
                alert('Please enter a valid M-Pesa transaction code.');
                return false;
            }
            
            return true;
        }
        
        // Save payment data
        function savePaymentData() {
            userData.payment = {
                transactionCode: document.getElementById('transaction-code').value,
                amount: servicePrices[selectedService]
            };
        }
        
        // Complete registration (real API call)
        function completeRegistration() {
            const completeButton = document.getElementById('complete-payment');
            const originalText = completeButton.textContent;
            
            // Show loading state
            completeButton.disabled = true;
            completeButton.classList.add('loading');
            completeButton.textContent = 'Processing...';
            
            // Send data to server
            fetch('/subscribe', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(userData)
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    // Registration completed successfully
                    goToStep(4);
                } else {
                    alert('Error: ' + data.message);
                }
            })
            .catch((error) => {
                console.error('Error:', error);
                alert('An error occurred. Please try again.');
            })
            .finally(() => {
                // Reset button state
                completeButton.disabled = false;
                completeButton.classList.remove('loading');
                completeButton.textContent = originalText;
            });
        }
        
        // Initialize the application
        document.addEventListener('DOMContentLoaded', init);
    </script>
</body>
</html>
"""

receipt_cache = {}
domain = 'https://midway-kccrug.fly.dev'

@app.context_processor
def inject_defaults():
    # Get all categories for the dropdown
    categories = []
    try:
        rows = query("SELECT DISTINCT category FROM products ORDER BY category")
        categories = [r[0] for r in rows]
    except Exception as e:
        print(f"Error loading categories: {e}")
    
    return {
        "user": get_user(), 
        "now": datetime.now(),
        "categories": categories,
        "get_active_cashier": get_active_cashier
    }
    
    

# --- MEDIA CENTER ROUTES ---

@app.route("/media_center")
@limiter.limit("10 per minute")
def media_center():
    """Public page listing uploaded documentaries and articles."""
    try:
        rows = query("""
            SELECT id, title, summary, content, author, created_at AS date, s3_url
            FROM media_articles
            ORDER BY created_at DESC
        """)
        
        articles = []
        for r in rows:
            articles.append({
                "id": r[0],
                "title": r[1] or "Untitled",
                "summary": r[2] or "No description available",
                "content": r[3],  # Include content field
                "author": r[4] or "Unknown Author",
                "date": r[5].strftime("%Y-%m-%d") if r[5] else "Unknown Date",
                "link": r[6]  # s3_url from database
            })
            
    except Exception as e:
        flash(f"Error loading media: {str(e)}", "error")
        articles = []
        print(f"Database error: {e}")

    # Mock user object for template - REPLACE WITH YOUR ACTUAL USER OBJECT
    user = session.get('user_object', None)
    
    return render_template_string(media_center_html, 
                                title="Media Center", 
                                articles=articles, 
                                user=user)


@app.route("/admin/media_upload", methods=["GET", "POST"])
@login_required(role="admin")
def media_upload():
    """Upload or edit documentaries/articles/media files."""
    media_id = request.args.get("media_id")
    media = None

    # Fetch existing media if editing
    if media_id:
        try:
            media_row = query(
                "SELECT id, title, summary, content, author, s3_url FROM media_articles WHERE id=%s",
                [media_id],
                fetch="one"
            )
            if media_row:
                media = {
                    "id": media_row[0],
                    "title": media_row[1],
                    "summary": media_row[2],
                    "content": media_row[3],
                    "author": media_row[4],
                    "link": media_row[5]
                }
        except Exception as e:
            flash(f"Error fetching media: {str(e)}", "error")

    # Handle form submission
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        summary = request.form.get("summary", "").strip()
        content = request.form.get("content", "").strip()
        author = request.form.get("author", "").strip()
        file = request.files.get("media_file")

        # Validation
        if not title or not summary or not author:
            flash("Please fill all required fields (Title, Summary, Author).", "error")
            return render_template_string(media_upload_html, title="Upload Media", media=media)

        s3_url = media["link"] if media else None

        # Handle new file upload
        if file and file.filename:
            filename = secure_filename(file.filename)
            
            # File validation
            if not allowed_file(filename):
                flash("Invalid file type. Accepted: Images, Videos, PDFs.", "error")
                return render_template_string(media_upload_html, title="Upload Media", media=media)
            
            if not check_file_size(file):
                flash("File too large. Maximum size is 50MB.", "error")
                return render_template_string(media_upload_html, title="Upload Media", media=media)

            try:
                # Resize only images
                ext = filename.rsplit('.', 1)[-1].lower()
                if ext in ['jpg', 'jpeg', 'png', 'gif']:
                    file_stream = resize_image(file)
                else:
                    file_stream = file

                # Upload to S3
                s3_url_new = upload_to_s3(file_stream, filename, content_type=file.content_type)
                if s3_url_new:
                    # Delete old file from S3 if editing
                    if media and media.get("link"):
                        delete_from_s3(media["link"])
                    s3_url = s3_url_new
                else:
                    flash("Failed to upload file to S3.", "error")
                    return render_template_string(media_upload_html, title="Upload Media", media=media)

            except Exception as e:
                flash(f"File upload error: {str(e)}", "error")
                return render_template_string(media_upload_html, title="Upload Media", media=media)

        # Insert or update media record
        try:
            if media:  # Update existing
                execute(
                    """
                    UPDATE media_articles
                    SET title=%s, summary=%s, content=%s, author=%s, s3_url=%s, updated_at=NOW()
                    WHERE id=%s
                    """, 
                    [title, summary, content, author, s3_url, media_id]
                )
                flash("Media updated successfully.", "success")
            else:  # Insert new
                execute(
                    """
                    INSERT INTO media_articles (title, summary, content, author, s3_url)
                    VALUES (%s, %s, %s, %s, %s)
                    """, 
                    [title, summary, content, author, s3_url]
                )
                flash("Media uploaded successfully.", "success")

            return redirect(url_for("media_center"))

        except Exception as e:
            flash(f"Database error: {str(e)}", "error")
            return render_template_string(media_upload_html, title="Upload Media", media=media)

    return render_template_string(media_upload_html, title="Upload Media", media=media)


@app.route("/admin/media_delete")
@login_required(role="admin")
def media_delete():
    """Delete a media article and its S3 file."""
    media_id = request.args.get("media_id")
    if not media_id:
        flash("Invalid media ID.", "error")
        return redirect(url_for("media_center"))

    try:
        # Get S3 URL before deletion
        media_row = query("SELECT s3_url FROM media_articles WHERE id=%s", [media_id], fetch="one")
        if media_row and media_row[0]:
            delete_from_s3(media_row[0])

        # Delete from database
        execute("DELETE FROM media_articles WHERE id=%s", [media_id])
        flash("Media deleted successfully.", "success")
    except Exception as e:
        flash(f"Error deleting media: {str(e)}", "error")

    return redirect(url_for("media_center"))


@app.route("/category/<category_name>")
@free_view_tracking
def category_products(category_name):
    try:
        rows = query(
            "SELECT id, category, name, image_url, description FROM products WHERE category = %s ORDER BY name",
            [category_name]
        )
        products = [
            {"id": r[0], "category": r[1], "name": r[2], "image_url": r[3], "description": r[4]}
            for r in rows
        ]
        body = render_template_string(category_products_html, products=products, category_name=category_name)
        return render_template_string(base_html, title=f"Category: {category_name}", body=body)
    except Exception as e:
        flash(f"Error loading category products: {str(e)}", "error")
        return redirect(url_for("app_home"))


@app.route("/")
@limiter.limit("20 per minute")
def home():
    """Landing page for unauthenticated users"""
    return render_template_string(landing_html)


@app.route("/app")
@limiter.limit("20 per minute")
def app_home():
    try:
        rows = query("SELECT id, category, name, image_url, description FROM products ORDER BY id DESC")
        products = [
            {"id": r[0], "category": r[1], "name": r[2], "image_url": r[3], "description": r[4]}
            for r in rows
        ]
        body = "<p>Welcome to the site.</p>"
        return render_template_string(base_html, title="Home", body=body)
    except Exception as e:
        flash(f"Error loading products: {str(e)}", "error")
        body = "<p>Error loading content.</p>"
        return render_template_string(base_html, title="Home", body=body)
    

@app.route('/subscribe', methods=['GET', 'POST'])
@limiter.limit("3 per minute")
def subscribe():
    if request.method == 'POST':
        try:
            # Get form data
            data = request.get_json()
            
            # Extract registration data
            registration = data.get('registration', {})
            service = data.get('service', {})
            payment = data.get('payment', {})
            
            # Validate required fields
            if not all([registration.get('fullname'), registration.get('email'), 
                       registration.get('phone'), service.get('selectedService'),
                       service.get('businessType'), payment.get('transactionCode')]):
                return jsonify({'success': False, 'message': 'All fields are required'}), 400
            
            # Insert into subscriptions table
            execute(
                """
                INSERT INTO subscriptions 
                (full_name, email, phone, service_package, business_type, 
                 business_details, amount_paid, transaction_code, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                """,
                [
                    registration.get('fullname'),
                    registration.get('email'),
                    registration.get('phone'),
                    service.get('selectedService'),
                    service.get('businessType'),
                    service.get('businessDetails', ''),
                    payment.get('amount'),
                    payment.get('transactionCode')
                ]
            )
            
            return jsonify({'success': True, 'message': 'Subscription created successfully!'})
            
        except Exception as e:
            print(f"Error creating subscription: {e}")
            return jsonify({'success': False, 'message': 'Error creating subscription'}), 500
    
    # GET request - return the subscription form
    return render_template_string(subscribe_html)           
        
# New route for cashier management
@app.route("/admin/cashiers")
@login_required(role="admin")
def admin_cashier_management():
    try:
        # Get all cashiers
        cashiers = query(
            """
            SELECT cp.id, cp.shop_name, cp.phone, cp.is_active, cp.next_available_time, u.username
            FROM cashier_profiles cp
            JOIN users u ON cp.user_id = u.id
            ORDER BY cp.is_active DESC, cp.shop_name
            """
        )
        
        cashiers_list = [
            {
                "id": r[0], "shop_name": r[1], "phone": r[2], 
                "is_active": r[3], "next_available_time": r[4], "username": r[5]
            }
            for r in cashiers
        ]
        
        management_html = """
        <section>
            <div class="section-header">
                <h2 class="section-title"><i class="fas fa-users"></i> Manage Cashiers</h2>
            </div>
            
            <div class="card">
                <h3>Active Cashier Status</h3>
                <p class="muted">Only one cashier can be active at a time. When a cashier is active, they will be visible to customers.</p>
                
                <div class="grid">
                    {% for cashier in cashiers %}
                        <article class="card" style="border-left: 4px solid {% if cashier.is_active %}var(--success){% else %}#6c757d{% endif %};">
                            <h4>{{ cashier.shop_name }}</h4>
                            <p class="muted">Owner: {{ cashier.username }}</p>
                            <p><strong>Phone:</strong> {{ cashier.phone or 'Not provided' }}</p>
                            
                            <div style="margin: 1rem 0;">
                                <span class="status-badge {% if cashier.is_active %}status-open{% else %}status-closed{% endif %}">
                                    {% if cashier.is_active %}
                                        <i class="fas fa-check-circle"></i> Active
                                    {% else %}
                                        <i class="fas fa-times-circle"></i> Inactive
                                    {% endif %}
                                </span>
                            </div>
                            
                            <div style="margin-top: auto;">
                                {% if not cashier.is_active %}
                                    <form method="POST" action="{{ url_for('activate_cashier', cashier_id=cashier.id) }}" style="display: inline;">
                                        <button type="submit" class="btn-primary" style="width: 100%;">
                                            <i class="fas fa-play"></i> Activate
                                        </button>
                                    </form>
                                {% else %}
                                    <form method="POST" action="{{ url_for('deactivate_cashier', cashier_id=cashier.id) }}" style="display: inline;">
                                        <button type="submit" class="btn-secondary" style="width: 100%;">
                                            <i class="fas fa-stop"></i> Deactivate
                                        </button>
                                    </form>
                                {% endif %}
                            </div>
                        </article>
                    {% endfor %}
                </div>
                
                {% if not cashiers %}
                    <div class="card" style="text-align: center; padding: 3rem;">
                        <i class="fas fa-users" style="font-size: 4rem; color: #dee2e6; margin-bottom: 1.5rem;"></i>
                        <h3>No Cashiers Registered</h3>
                        <p class="muted">No cashier accounts have been created yet.</p>
                    </div>
                {% endif %}
            </div>
        </section>
        """
        
        body = render_template_string(management_html, cashiers=cashiers_list)
        return render_template_string(base_html, title="Manage Cashiers", body=body)
        
    except Exception as e:
        flash(f"Error loading cashier management: {str(e)}", "error")
        return redirect(url_for("admin_dashboard"))        
        

@app.route("/activate_cashier/<int:cashier_id>", methods=["POST"])
@login_required(role="admin")
def activate_cashier(cashier_id):
    try:
        if set_active_cashier(cashier_id):
            flash("Cashier activated successfully. They are now visible to customers.", "success")
        else:
            flash("Error activating cashier.", "error")
    except Exception as e:
        flash(f"Error activating cashier: {str(e)}", "error")
    
    return redirect(url_for("admin_cashier_management"))


@app.route("/deactivate_cashier/<int:cashier_id>", methods=["POST"])
@login_required(role="admin")
def deactivate_cashier(cashier_id):
    try:
        if set_cashier_availability(cashier_id, False):
            flash("Cashier deactivated successfully. They are no longer visible to customers.", "success")
        else:
            flash("Error deactivating cashier.", "error")
    except Exception as e:
        flash(f"Error deactivating cashier: {str(e)}", "error")
    
    return redirect(url_for("admin_cashier_management"))        


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def login():
    form = LoginForm()

    if request.method == "POST" and form.validate():
        username = form.username.data.strip()
        password = form.password.data

        try:
            row = query(
                """
                SELECT id, password_hash, role, failed_login_attempts, account_locked_until
                FROM users WHERE username = %s
                """,
                [username],
                fetch="one"
            )

            if not row:
                flash("Invalid credentials.", "error")
                return redirect(url_for("login"))

            user_id, pw_hash, role, failed_attempts, locked_until = row

            # 🔒 Check if account is locked
            if locked_until and locked_until > datetime.now():
                lock_time = locked_until.strftime("%Y-%m-%d %H:%M")
                flash(f"Account locked until {lock_time}. Try again later.", "error")
                return redirect(url_for("login"))

            # ✅ Correct password?
            if check_password_hash(pw_hash, password):
                # Reset attempts + unlock account
                execute(
                    "UPDATE users SET failed_login_attempts = 0, account_locked_until = NULL WHERE id = %s",
                    [user_id]
                )

                session.clear()
                session["user_id"] = user_id

                log_activity(user_id, "login", f"User logged in from {request.remote_addr}")

                flash("Welcome back!", "success")
                return redirect(url_for("app_home"))

            # ❌ Wrong password
            failed_attempts += 1
            if failed_attempts >= MAX_FAILED_ATTEMPTS:
                lock_time = datetime.now() + timedelta(minutes=LOCK_DURATION_MINUTES)
                execute(
                    """
                    UPDATE users
                    SET failed_login_attempts = %s, account_locked_until = %s
                    WHERE id = %s
                    """,
                    [failed_attempts, lock_time, user_id]
                )
                flash(f"Too many failed attempts. Account locked for {LOCK_DURATION_MINUTES} minutes.", "error")
            else:
                execute(
                    "UPDATE users SET failed_login_attempts = %s WHERE id = %s",
                    [failed_attempts, user_id]
                )
                flash("Invalid credentials.", "error")

            return redirect(url_for("login"))

        except Exception as e:
            flash(f"Login error: {str(e)}", "error")
            return redirect(url_for("login"))

    body = render_template_string(login_html, form=form)
    return render_template_string(base_html, title="Login", body=body)
    

@app.route("/register", methods=["GET", "POST"])
@limiter.limit("6 per hour")
def register():
    form = RegistrationForm()
    
    if request.method == "POST" and form.validate():
        username = form.username.data.strip()
        password = form.password.data
        role = form.role.data
        phone = form.phone.data
        
        # ADD THIS: Check if user is allowed to register
        if not is_user_allowed(username):
            flash("This username is not authorized to register. Please contact administrator on WHATSAPP 0701207062 James Boyid Ochuna. Developer & Senior System Admin. . WE SELL THE USERNAME AS A WAY TO SUPPORT OUR DEVELOPMENT EFFORTS. WELCOME..", "error")
            return redirect(url_for("register"))
            
        if role not in ("customer", "cashier"):
            role = "customer"
            
        try:
            execute(
                "INSERT INTO users (username, password_hash, role, phone) VALUES (%s, %s, %s, %s)",
                [username, generate_password_hash(password), role, phone or None]
            )
            
            # Log the activity
            user_id = query("SELECT id FROM users WHERE username = %s", [username], fetch="one")[0]
            log_activity(user_id, "registration", f"New {role} account created")
            
            flash("Account created. Please log in.", "success")
            return redirect(url_for("login"))
        except Exception as e:
            flash("Username already exists or error creating account.", "error")
            return redirect(url_for("register"))
            
    body = render_template_string(register_html, form=form)
    return render_template_string(base_html, title="Register", body=body)    


@app.route("/logout")
def logout():
    user = get_user()
    if user:
        log_activity(user["id"], "logout", f"User logged out from {request.remote_addr}")
    
    session.clear()
    # Clear the free view flag specifically
    session.pop('free_view_used', None)
    flash("You have been logged out.", "success")
    return redirect(url_for("app_home"))
    

@app.route("/admin/dashboard")
@login_required(role="admin")
def admin_dashboard():
    """Admin dashboard with user management"""
    try:
        # Get platform statistics
        product_count = query("SELECT COUNT(*) FROM products", fetch="one")[0]
        user_count = query("SELECT COUNT(*) FROM users", fetch="one")[0]
        order_count = query("SELECT COUNT(*) FROM orders", fetch="one")[0]
        allowed_users = get_allowed_users()
        
        dashboard_html = """
        <section>
            <div class="section-header">
                <h2 class="section-title"><i class="fas fa-tachometer-alt"></i> Admin Dashboard</h2>
            </div>
            
            <!-- Statistics Cards -->
            <div class="stats-grid">
                <div class="stat-card">
                    <i class="fas fa-boxes" style="font-size: 2.5rem; color: var(--primary);"></i>
                    <div class="stat-value">{{ product_count }}</div>
                    <div class="stat-label">Total Products</div>
                </div>
                
                <div class="stat-card">
                    <i class="fas fa-users" style="font-size: 2.5rem; color: var(--success);"></i>
                    <div class="stat-value">{{ user_count }}</div>
                    <div class="stat-label">Total Users</div>
                </div>
                
                <div class="stat-card">
                    <i class="fas fa-shopping-cart" style="font-size: 2.5rem; color: var(--warning);"></i>
                    <div class="stat-value">{{ order_count }}</div>
                    <div class="stat-label">Total Orders</div>
                </div>
            </div>
            
            <!-- Media Upload Button -->
            <div class="upload-button-container" 
                 style="margin-top: 1.5rem; text-align: center; position:relative; z-index:10;">

              <!-- Upload Media Button -->
              <a href="{{ url_for('media_upload') }}"
                 class="btn-secondary" 
     style="display:inline-block; margin-right:10px; padding:0.75rem 1.5rem; border-radius:6px; font-weight:600; 
                        background:#007bff; color:white; text-decoration:none; box-shadow:0 2px 6px rgba(0,0,0,0.15);">
                <i class="fas fa-upload"></i> Upload New Media
              </a>

              <!-- Delete Media Button -->
              <a href="{{ url_for('media_delete') }}" 
                 class="btn-danger" 
                 style="display:inline-block; padding:0.75rem 1.5rem; border-radius:6px; font-weight:600; 
                        background:#dc3545; color:white; text-decoration:none; box-shadow:0 2px 6px rgba(0,0,0,0.15);">
                <i class="fas fa-trash-alt"></i> Delete Media
              </a>

            </div>        
            
            <!-- Allowed Users Management -->
            <div class="card" style="margin-top: 2rem;">
                <h4><i class="fas fa-user-check"></i> Manage Allowed Users</h4>
                <p class="muted">Users must be in this list to register on the platform.</p>
                
                <!-- Add User Form -->
                <form method="POST" action="{{ url_for('admin_add_allowed_user') }}" class="allowed-user-form">
                    <div class="form-group">
                        <label for="allowed_username" class="form-label">
                            <i class="fas fa-user-plus"></i> Username
                        </label>
                        <input type="text" 
                               id="allowed_username" 
                               name="allowed_username" 
                               placeholder="Enter username to allow registration"
                               class="form-input"
                               required>
                        <small class="form-help">Enter the exact username that will be allowed to register</small>
                    </div>
                    
                    <button type="submit" class="btn-success">
                        <i class="fas fa-user-plus"></i> Add to Allowed List
                    </button>
                </form>

                <!-- Allowed Users List -->
                {% if allowed_users %}
                    <div class="table-container">
                        <table class="styled-table">
                            <thead>
                                <tr>
                                    <th><i class="fas fa-user"></i> Username</th>
                                    <th><i class="fas fa-calendar"></i> Added Date</th>
                                    <th><i class="fas fa-actions"></i> Actions</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for user in allowed_users %}
                                <tr>
                                    <td class="username-cell">
                                        <i class="fas fa-user"></i>
                                        {{ user.username }}
                                    </td>
                                    <td>{{ user.created_at.strftime('%Y-%m-%d %H:%M') }}</td>
                                    <td>
                                        <form method="POST" action="{{ url_for('admin_remove_allowed_user') }}" 
                                              onsubmit="return confirm('Remove {{ user.username }} from allowed list?')"
                                              class="inline-form">
                                            <input type="hidden" name="username" value="{{ user.username }}">
                                            <button type="submit" class="btn-error btn-small">
                                                <i class="fas fa-trash"></i> Remove
                                            </button>
                                        </form>
                                    </td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                {% else %}
                    <div class="empty-state">
                        <i class="fas fa-user-slash" style="font-size: 3rem; color: #dee2e6;"></i>
                        <h4>No allowed users yet</h4>
                        <p class="muted">Add users to the allowed list using the form above.</p>
                    </div>
                {% endif %}
            </div>
            
            <!-- Quick Actions -->
            <div class="grid" style="margin-top: 2rem;">
                <div class="card">
                    <h4><i class="fas fa-box-open"></i> Product Management</h4>
                    <p class="muted">Manage your product catalogue</p>
                    <a href="{{ url_for('admin_catalogue') }}" class="btn-primary" style="display: block; text-align: center;">
                        <i class="fas fa-arrow-right"></i> Go to Catalogue
                    </a>
                </div>
                
                <div class="card">
                    <h4><i class="fas fa-shopping-cart"></i> Order Management</h4>
                    <p class="muted">View and manage customer orders</p>
                    <a href="{{ url_for('admin_orders') }}" class="btn-primary" style="display: block; text-align: center;">
                        <i class="fas fa-arrow-right"></i> Go to Orders
                    </a>
                </div>
            </div>
        </section>

        <style>
            .allowed-user-form {
                background: var(--light-bg);
                padding: 1.5rem;
                border-radius: 8px;
                margin-bottom: 2rem;
            }
            
            .form-group {
                margin-bottom: 1rem;
            }
            
            .form-label {
                display: block;
                margin-bottom: 0.5rem;
                font-weight: bold;
                color: var(--primary);
            }
            
            .form-input {
                width: 100%;
                padding: 0.75rem;
                border: 1px solid #ddd;
                border-radius: 4px;
                font-size: 1rem;
                box-sizing: border-box;
            }
            
            .form-input:focus {
                outline: none;
                border-color: var(--primary);
                box-shadow: 0 0 0 2px rgba(67, 97, 238, 0.2);
            }
            
            .form-help {
                display: block;
                margin-top: 0.25rem;
                color: #666;
                font-size: 0.85rem;
            }
            
            .table-container {
                max-height: 400px;
                overflow-y: auto;
                border: 1px solid #e0e0e0;
                border-radius: 8px;
            }
            
            .styled-table {
                width: 100%;
                border-collapse: collapse;
            }
            
            .styled-table th {
                background: var(--light-bg);
                padding: 1rem;
                text-align: left;
                font-weight: bold;
                border-bottom: 2px solid #ddd;
                position: sticky;
                top: 0;
            }
            
            .styled-table td {
                padding: 1rem;
                border-bottom: 1px solid #e0e0e0;
            }
            
            .styled-table tr:hover {
                background-color: #f8f9fa;
            }
            
            .username-cell {
                font-weight: 500;
            }
            
            .inline-form {
                display: inline;
            }
            
            .btn-small {
                padding: 0.4rem 0.8rem;
                font-size: 0.85rem;
            }
            
            .empty-state {
                text-align: center;
                padding: 3rem;
                color: #666;
            }
            
            @media (max-width: 768px) {
                .table-container {
                    font-size: 0.9rem;
                }
                
                .styled-table th,
                .styled-table td {
                    padding: 0.75rem 0.5rem;
                }
            }
        </style>
        """
        
        body = render_template_string(dashboard_html, 
                                    product_count=product_count,
                                    user_count=user_count,
                                    order_count=order_count,
                                    allowed_users=allowed_users)
        return render_template_string(base_html, title="Admin Dashboard", body=body)
        
    except Exception as e:
        flash(f"Error loading dashboard: {str(e)}", "error")
        return redirect(url_for("app_home"))


@app.route("/admin/add_allowed_user", methods=["POST"])
@login_required(role="admin")
def admin_add_allowed_user():
    """Add user to allowed list"""
    username = request.form.get("allowed_username")
    
    if not username:
        flash("Username is required", "error")
        return redirect(url_for("admin_dashboard"))
    
    try:
        # Check if user already exists
        if is_user_allowed(username):
            flash(f"User {username} is already in the allowed list", "warning")
            return redirect(url_for("admin_dashboard"))
        
        # Add user to allowed list
        execute("INSERT INTO allowed_users (username) VALUES (%s)", [username])
        flash(f"User {username} added to allowed list", "success")
        
    except Exception as e:
        flash(f"Error adding user to allowed list: {str(e)}", "error")
    
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/remove_allowed_user", methods=["POST"])
@login_required(role="admin")
def admin_remove_allowed_user():
    """Remove user from allowed list"""
    username = request.form.get("username")
    
    if not username:
        flash("Username is required", "error")
        return redirect(url_for("admin_dashboard"))
    
    try:
        if remove_allowed_user(username):
            flash(f"User {username} removed from allowed list", "success")
        else:
            flash(f"Error removing user {username} from allowed list", "error")
    except Exception as e:
        flash(f"Error removing user: {str(e)}", "error")
    
    return redirect(url_for("admin_dashboard"))                                   

@app.route("/admin/catalogue", methods=["GET", "POST"])
@login_required(role="admin")
def admin_catalogue():
    form = ProductForm()
    
    if request.method == "POST" and form.validate():
        category = form.category.data.strip()
        name = form.name.data.strip()
        description = form.description.data.strip()
        
        # FIXED: Handle image upload using form.image.data instead of request.files
        image_url = None
        if form.image.data:  # CHANGED: Use form field data
            file = form.image.data  # CHANGED: Get file from form field
            if file and allowed_file(file.filename):
                # Resize the image
                resized_image = resize_image(file)
                
                # Determine content type
                content_type = f"image/{file.filename.rsplit('.', 1)[1].lower()}"
                if content_type == 'image/jpg':
                    content_type = 'image/jpeg'
                
                # Upload to S3
                image_url = upload_to_s3(resized_image, secure_filename(file.filename), content_type)
        
        try:
            execute(
                "INSERT INTO products (category, name, image_url, description) VALUES (%s, %s, %s, %s)",
                [category, name, image_url, description or None]
            )
            
            # Log the activity
            log_activity(get_user()["id"], "product_added", f"Added product: {name}")
            
            flash("Product added.", "success")
            return redirect(url_for("admin_catalogue"))
        except Exception as e:
            flash(f"Error adding product: {str(e)}", "error")
            return redirect(url_for("admin_catalogue"))
            
    try:
        rows = query("SELECT id, category, name, image_url FROM products ORDER BY id DESC")
        products = [{"id": r[0], "category": r[1], "name": r[2], "image_url": r[3]} for r in rows]
        body = render_template_string(catalogue_html, products=products, form=form)
        return render_template_string(base_html, title="Catalogue", body=body)
    except Exception as e:
        flash(f"Error loading catalogue: {str(e)}", "error")
        body = render_template_string(catalogue_html, products=[], form=form)
        return render_template_string(base_html, title="Catalogue", body=body)


@app.route("/admin/catalogue/delete/<int:pid>", methods=["POST"])
@login_required(role="admin")
def admin_delete_product(pid):
    try:
        # Get product info for logging and image URL
        product = query("SELECT name, image_url FROM products WHERE id = %s", [pid], fetch="one")
        
        if product:
            # Delete associated image from S3
            if product[1]:  # image_url
                delete_from_s3(product[1])
            
            execute("DELETE FROM products WHERE id = %s", [pid])
            
            # Log the activity
            log_activity(get_user()["id"], "product_deleted", f"Deleted product: {product[0]}")
        
        flash("Product deleted.", "success")
    except Exception as e:
        flash(f"Error deleting product: {str(e)}", "error")
    return redirect(url_for("admin_catalogue"))


@app.route("/admin/stats")
@login_required(role="admin")
def admin_stats():
    try:
        users_total = query("SELECT COUNT(*) FROM users", fetch="one")[0]
        customers = query("SELECT COUNT(*) FROM users WHERE role = 'customer'", fetch="one")[0]
        cashiers = query("SELECT COUNT(*) FROM users WHERE role = 'cashier'", fetch="one")[0]
        admins = query("SELECT COUNT(*) FROM users WHERE role = 'admin'", fetch="one")[0]
        products_total = query("SELECT COUNT(*) FROM products", fetch="one")[0]
        offers_total = query("SELECT COUNT(*) FROM offers", fetch="one")[0]
        
        # Order statistics
        orders_total = query("SELECT COUNT(*) FROM orders", fetch="one")[0]
        pending_orders = query("SELECT COUNT(*) FROM orders WHERE status = 'pending'", fetch="one")[0]
        confirmed_orders = query("SELECT COUNT(*) FROM orders WHERE status = 'confirmed'", fetch="one")[0]
        completed_orders = query("SELECT COUNT(*) FROM orders WHERE status = 'completed'", fetch="one")[0]
        
        # Revenue statistics
        total_revenue = query("SELECT COALESCE(SUM(total_price), 0) FROM orders WHERE status = 'completed'", fetch="one")[0] or 0
        avg_order_value = query("SELECT COALESCE(AVG(total_price), 0) FROM orders WHERE status = 'completed'", fetch="one")[0] or 0
        
        # Recent orders (✅ now includes category/commodity name)
        recent_orders = query(
            """
            SELECT 
                o.id, 
                off.category_name,      -- new addition
                p.name AS product_name, 
                u.username AS customer_name, 
                cp.shop_name AS cashier_name, 
                o.total_price, 
                o.status, 
                o.created_at
            FROM orders o
            JOIN offers off ON o.offer_id = off.id
            JOIN products p ON off.product_id = p.id
            JOIN users u ON o.customer_id = u.id
            JOIN cashier_profiles cp ON off.cashier_id = cp.id
            ORDER BY o.created_at DESC 
            LIMIT 10
            """
        )
        
        recent_orders = [
            {
                "id": r[0],
                "commodity_name": r[1],  # new field for display
                "product_name": r[2],
                "customer_name": r[3],
                "cashier_name": r[4],
                "total_price": float(r[5]),
                "status": r[6],
                "created_at": r[7]
            }
            for r in recent_orders
        ]
        
        # Transaction messages
        transaction_messages = query(
            "SELECT order_id, message_type, message_text, created_at FROM transaction_messages ORDER BY created_at DESC LIMIT 20"
        )
        
        transaction_messages = [
            {
                "order_id": r[0],
                "message_type": r[1],
                "message_text": r[2],
                "created_at": r[3]
            }
            for r in transaction_messages
        ]
        
        stats = {
            "users_total": users_total,
            "customers": customers,
            "cashiers": cashiers,
            "admins": admins,
            "products_total": products_total,
            "offers_total": offers_total,
            "orders_total": orders_total,
            "pending_orders": pending_orders,
            "confirmed_orders": confirmed_orders,
            "completed_orders": completed_orders,
            "total_revenue": float(total_revenue),
            "avg_order_value": float(avg_order_value)
        }
        
        body = render_template_string(
            admin_stats_html, 
            stats=stats, 
            recent_orders=recent_orders, 
            transaction_messages=transaction_messages
        )
        return render_template_string(base_html, title="Admin Stats", body=body)
    except Exception as e:
        flash(f"Error loading statistics: {str(e)}", "error")
        return redirect(url_for("app_home"))


@app.route("/admin/orders")
@login_required(role="admin")
def admin_orders():
    try:
        orders = query(
            """
            SELECT o.id, p.category, p.name as product_name, off.commodity_name,
                   u.username as customer_name, u.phone as customer_phone,
                   cp.shop_name as cashier_name, cp.address as cashier_address, cp.phone as cashier_phone,
                   o.quantity, o.total_price, o.status, o.created_at,
                   pay.mpesa_message, pay.created_at as payment_date
            FROM orders o
            JOIN offers off ON o.offer_id = off.id
            JOIN products p ON off.product_id = p.id
            JOIN users u ON o.customer_id = u.id
            JOIN cashier_profiles cp ON off.cashier_id = cp.id
            LEFT JOIN payments pay ON o.id = pay.order_id
            ORDER BY o.created_at DESC
            """
        )
        
        orders = [
            {
                "id": r[0],
                "category": r[1],
                "product_name": r[2],
                "commodity_name": r[3],
                "customer_name": r[4],
                "customer_phone": r[5],
                "cashier_name": r[6],
                "cashier_address": r[7],  # ✅ new field
                "cashier_phone": r[8],
                "quantity": r[9],
                "total_price": float(r[10]),
                "status": r[11],
                "created_at": r[12],
                "mpesa_message": r[13],
                "payment_date": r[14]
            }
            for r in orders
        ]
        
        body = render_template_string(admin_orders_html, orders=orders)
        return render_template_string(base_html, title="Admin Orders", body=body)
    except Exception as e:
        flash(f"Error loading orders: {str(e)}", "error")
        return redirect(url_for("app_home"))


@app.route("/cashier/profile", methods=["GET", "POST"])
@login_required(role="cashier")
def cashier_profile():
    user = get_user()
    form = ProfileForm()
    prof = None
    
    try:
        row = query(
            "SELECT id, shop_name, phone, address FROM cashier_profiles WHERE user_id = %s", 
            [user["id"]], 
            fetch="one"
        )
        if row:
            prof = {
                "id": row[0],
                "shop_name": row[1],
                "phone": row[2],
                "address": row[3]
            }
    except Exception as e:
        current_app.logger.exception("Error loading cashier profile")
        flash(f"Error loading profile: {str(e)}", "error")
    
    if request.method == "POST" and form.validate():
        shop_name = form.shop_name.data.strip()
        phone = form.phone.data.strip() or None
        address = form.address.data.strip() or None
        
        try:
            if prof:
                execute(
                    """
                    UPDATE cashier_profiles 
                    SET shop_name = %s, phone = %s, address = %s 
                    WHERE user_id = %s
                    """,
                    [shop_name, phone, address, user["id"]]
                )
                flash("Profile updated.", "success")
            else:
                execute(
                    """
                    INSERT INTO cashier_profiles (user_id, shop_name, phone, address) 
                    VALUES (%s, %s, %s, %s)
                    """,
                    [user["id"], shop_name, phone, address]
                )
                flash("Profile created.", "success")
            
            log_activity(user["id"], "profile_updated", "Updated cashier profile")
            return redirect(url_for("cashier_profile"))
        
        except Exception as e:
            current_app.logger.exception("Error saving cashier profile")
            flash(f"Error saving profile: {str(e)}", "error")
            return redirect(url_for("cashier_profile"))
    
    body = render_template_string(cashier_profile_html, prof=prof, form=form)
    return render_template_string(base_html, title="My Shop", body=body)


@app.route("/cashier")
@login_required(role="cashier")
def cashier_dashboard():
    user = get_user()
    form = OfferForm()
    
    try:
        # Fetch all products
        prows = query("SELECT id, name, category FROM products ORDER BY name")
        products = [{"id": r[0], "name": r[1], "category": r[2]} for r in prows]
        
        # Fetch cashier profile
        prof = query("SELECT id FROM cashier_profiles WHERE user_id = %s", [user["id"]], fetch="one")
        offers = []
        
        if prof:
            # Fetch offers created by this cashier
            orows = query(
                """
                SELECT o.id, p.name AS product_name, p.category, o.commodity_name, o.price, o.quantity, 
                       o.payment_terms, o.delivery_terms, o.created_at
                FROM offers o
                JOIN products p ON p.id = o.product_id
                WHERE o.cashier_id = %s
                ORDER BY o.created_at DESC
                """,
                [prof[0]]
            )
            offers = [
                {
                    "id": r[0],
                    "product_name": r[1],
                    "category": r[2],
                    "commodity_name": r[3],
                    "price": float(r[4]),
                    "quantity": r[5],
                    "payment_terms": r[6],
                    "delivery_terms": r[7],
                    "created_at": r[8]
                } for r in orows
            ]
        
        body = render_template_string(cashier_dashboard_html, products=products, offers=offers, form=form)
        return render_template_string(base_html, title="My Offers", body=body)
    
    except Exception as e:
        current_app.logger.exception("Error loading cashier dashboard")
        flash(f"Error loading dashboard: {str(e)}", "error")
        return redirect(url_for("home"))


@app.route("/cashier/orders")
@login_required(role="cashier")
def cashier_orders():
    user = get_user()
    
    try:
        # Get cashier profile
        prof = query("SELECT id FROM cashier_profiles WHERE user_id = %s", [user["id"]], fetch="one")
        
        if not prof:
            flash("Please create your shop profile first.", "warning")
            return redirect(url_for("cashier_profile"))
        
        # Get orders for this cashier, including shop address
        orders = query(
            """
            SELECT o.id, p.category, p.name as product_name, off.commodity_name,
                   u.username as customer_name, u.phone as customer_phone,
                   cp.shop_name, cp.address,  -- ✅ include address
                   o.quantity, o.total_price, o.status, o.created_at
            FROM orders o
            JOIN offers off ON o.offer_id = off.id
            JOIN products p ON off.product_id = p.id
            JOIN users u ON o.customer_id = u.id
            JOIN cashier_profiles cp ON off.cashier_id = cp.id
            WHERE off.cashier_id = %s
            ORDER BY o.created_at DESC
            """,
            [prof[0]]
        )
        
        orders = [
            {
                "id": r[0], 
                "category": r[1],
                "product_name": r[2],
                "commodity_name": r[3],
                "customer_name": r[4],
                "customer_phone": r[5],
                "shop_name": r[6],
                "shop_address": r[7],  # ✅ new field
                "quantity": r[8],
                "total_price": float(r[9]),
                "status": r[10],
                "created_at": r[11]
            }
            for r in orders
        ]
        
        body = render_template_string(cashier_orders_html, orders=orders)
        return render_template_string(base_html, title="My Orders", body=body)
    except Exception as e:
        flash(f"Error loading orders: {str(e)}", "error")
        return redirect(url_for("app_home"))


@app.route("/update_order_status/<int:order_id>/<status>", methods=["POST"])
@login_required(role="cashier")
def update_order_status(order_id, status):
    user = get_user()
    
    try:
        # Verify this order belongs to this cashier
        prof = query("SELECT id FROM cashier_profiles WHERE user_id = %s", [user["id"]], fetch="one")
        
        if not prof:
            flash("Please create your shop profile first.", "warning")
            return redirect(url_for("cashier_profile"))
        
        order = query(
            """
            SELECT o.id 
            FROM orders o
            JOIN offers off ON o.offer_id = off.id
            WHERE o.id = %s AND off.cashier_id = %s
            """,
            [order_id, prof[0]], fetch="one"
        )
        
        if not order:
            flash("Order not found.", "error")
            return redirect(url_for("cashier_orders"))
        
        # Update order status
        if status == "confirmed":
            execute(
                "UPDATE orders SET status = 'confirmed', confirmed_at = NOW() WHERE id = %s",
                [order_id]
            )
            flash("Order confirmed.", "success")
            
            # Send confirmation message
            order_details = query(
                """
                SELECT u.username, off.commodity_name 
                FROM orders o
                JOIN users u ON o.customer_id = u.id
                JOIN offers off ON o.offer_id = off.id
                WHERE o.id = %s
                """,
                [order_id], fetch="one"
            )
            
            if order_details:
                message_text = f"Your order for {order_details[1]} has been confirmed by the cashier. Please prepare to make payment upon delivery."
                send_transaction_message(order_id, "order_confirmation", message_text)
                
        elif status == "completed":
            execute(
                "UPDATE orders SET status = 'completed', completed_at = NOW() WHERE id = %s",
                [order_id]
            )
            flash("Order marked as completed.", "success")
            
            # Send completion message
            order_details = query(
                """
                SELECT u.username, off.commodity_name 
                FROM orders o
                JOIN users u ON o.customer_id = u.id
                JOIN offers off ON o.offer_id = off.id
                WHERE o.id = %s
                """,
                [order_id], fetch="one"
            )
            
            if order_details:
                message_text = f"Your order for {order_details[1]} has been completed. Thank you for your business!"
                send_transaction_message(order_id, "order_completion", message_text)
                
        # Log the activity
        log_activity(user["id"], "order_updated", f"Updated order #{order_id} to {status}")
        
    except Exception as e:
        flash(f"Error updating order: {str(e)}", "error")
    
    return redirect(url_for("cashier_orders"))


@app.route("/cashier/offer", methods=["POST"])
@login_required(role="cashier")
def cashier_add_offer():
    user = get_user()
    form = OfferForm()
    
    if not form.validate():
        for field, errors in form.errors.items():
            for error in errors:
                flash(f"{getattr(form, field).label.text}: {error}", "error")
        return redirect(url_for("cashier_dashboard"))
    
    try:
        prof = query(
            "SELECT id FROM cashier_profiles WHERE user_id = %s", 
            [user["id"]], 
            fetch="one"
        )
        if not prof:
            flash("Please create your shop profile first.", "warning")
            return redirect(url_for("cashier_profile"))
        
        product_id = int(request.form.get("product_id", 0))
        commodity_name = form.commodity_name.data.strip()
        commodity_description = form.commodity_description.data.strip()
        price = form.price.data
        quantity = form.quantity.data
        payment_terms = form.payment_terms.data.strip()
        delivery_terms = form.delivery_terms.data.strip()
        
        if product_id <= 0:
            flash("Please select a product.", "error")
            return redirect(url_for("cashier_dashboard"))

        # Fetch both category and product name from products table
        product_info = query(
            "SELECT category, name FROM products WHERE id = %s",
            [product_id],
            fetch="one"
        )
        if not product_info:
            flash("Product not found.", "error")
            return redirect(url_for("cashier_dashboard"))

        category_name, product_name = product_info

        existing = query(
            "SELECT id FROM offers WHERE product_id = %s AND cashier_id = %s AND commodity_name = %s",
            [product_id, prof[0], commodity_name], 
            fetch="one"
        )
        
        if existing:
            execute(
                """
                UPDATE offers SET category_name = %s, commodity_description = %s, price = %s, quantity = %s, 
                payment_terms = %s, delivery_terms = %s, created_at = NOW()
                WHERE id = %s
                """,
                [category_name, commodity_description or None, price, quantity, payment_terms or None, delivery_terms or None, existing[0]]
            )
            flash("Offer updated.", "success")
        else:
            execute(
                """
                INSERT INTO offers (product_id, cashier_id, category_name, commodity_name, commodity_description, 
                price, quantity, payment_terms, delivery_terms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [product_id, prof[0], category_name, commodity_name, commodity_description or None,
                 price, quantity, payment_terms or None, delivery_terms or None]
            )
            flash("Offer added.", "success")
            
        # Log the activity
        log_activity(user["id"], "offer_updated", f"Updated offer for {category_name} → {product_name}: {commodity_name}")
            
    except Exception as e:
        flash(f"Error saving offer: {str(e)}", "error")
    
    return redirect(url_for("cashier_dashboard"))


@app.route("/products/<int:product_id>/cashiers")
@free_view_tracking
def product_cashiers(product_id):
    try:
        prow = query(
            "SELECT id, name, category, image_url, description FROM products WHERE id = %s", 
            [product_id], 
            fetch="one"
        )
        if not prow:
            abort(404)
            
        product = {
            "id": prow[0], "name": prow[1], "category": prow[2], 
            "image_url": prow[3], "description": prow[4]
        }

        # Get active cashier
        active_cashier = get_active_cashier()
        results = []
        
        if active_cashier:
            # Only show offers from the active cashier, now including shop address
            rows = query(
                """
                SELECT o.id, cp.shop_name, cp.phone, cp.address,
                       o.commodity_name, o.commodity_description, o.price, o.quantity, 
                       o.payment_terms, o.delivery_terms
                FROM offers o
                JOIN cashier_profiles cp ON cp.id = o.cashier_id
                WHERE o.product_id = %s AND cp.is_active = TRUE
                """,
                [product_id]
            )
            
            for r in rows:
                results.append({
                    "id": r[0],
                    "shop_name": r[1],
                    "phone": r[2],
                    "address": r[3],  # ✅ Added shop address here
                    "commodity_name": r[4],
                    "commodity_description": r[5],
                    "price": float(r[6]),
                    "quantity": r[7],
                    "payment_terms": r[8],
                    "delivery_terms": r[9]
                })

        body = render_template_string(product_cashiers_html, product=product, results=results)
        return render_template_string(base_html, title=f"Cashiers · {product['name']}", body=body)
    except Exception as e:
        flash(f"Error loading cashiers: {str(e)}", "error")
        return redirect(url_for("app_home"))


@app.route("/order/<int:offer_id>", methods=["GET", "POST"])
@login_required(role="customer")
def create_order(offer_id):
    user = get_user()
    form = OrderForm()
    
    # Get offer details with explicit column names, including shop address
    offer = query(
        """
        SELECT 
            o.id, o.product_id, o.cashier_id, o.commodity_name, 
            o.commodity_description, o.price, o.quantity, o.payment_terms, 
            o.delivery_terms, o.created_at,
            p.name as product_name, p.category,
            cp.shop_name, cp.address, cp.phone
        FROM offers o
        JOIN products p ON o.product_id = p.id
        JOIN cashier_profiles cp ON o.cashier_id = cp.id
        WHERE o.id = %s
        """,
        [offer_id], fetch="one"
    )
    
    if not offer:
        flash("Offer not found.", "error")
        return redirect(url_for("home"))
    
    # Convert the result to a dictionary with proper column names
    offer_dict = {
        "id": offer[0],
        "product_id": offer[1],
        "cashier_id": offer[2],
        "commodity_name": offer[3],
        "commodity_description": offer[4],
        "price": float(offer[5]),
        "quantity": offer[6],
        "payment_terms": offer[7],
        "delivery_terms": offer[8],
        "created_at": offer[9],
        "product_name": offer[10],
        "category": offer[11],
        "shop_name": offer[12],
        "shop_address": offer[13],  # ✅ new field
        "phone": offer[14]
    }
    
    if request.method == "POST" and form.validate():
        quantity = form.quantity.data
        delivery_address = form.delivery_address.data.strip()
        terms_agreed = form.terms_agreed.data == 'yes'
        
        if not terms_agreed:
            flash("You must agree to the payment terms to place an order.", "error")
            return redirect(url_for("create_order", offer_id=offer_id))
        
        total_price = quantity * offer_dict["price"]
        
        try:
            # Create order
            execute(
                """
                INSERT INTO orders (customer_id, offer_id, quantity, total_price, delivery_address, terms_agreed)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [user["id"], offer_id, quantity, total_price, delivery_address, terms_agreed]
            )
            
            order_id = query("SELECT LASTVAL()", fetch="one")[0]
            
            log_activity(user["id"], "order_created", f"Created order #{order_id} for {quantity} x {offer_dict['commodity_name']}")
            
            message_text = f"New order received for {quantity} x {offer_dict['commodity_name']}. Total: KES {total_price:.2f}. Delivery address: {delivery_address}"
            send_transaction_message(order_id, "new_order", message_text)
            
            flash("Order created successfully. Please confirm your order.", "success")
            return redirect(url_for("customer_orders"))
            
        except Exception as e:
            flash(f"Error creating order: {str(e)}", "error")
            return redirect(url_for("create_order", offer_id=offer_id))
    
    body = render_template_string(order_form_html, offer=offer_dict, form=form)
    return render_template_string(base_html, title="Place Order", body=body)


@app.route("/confirm_order/<int:order_id>", methods=["POST"])
@login_required(role="customer")
def confirm_order(order_id):
    user = get_user()
    
    try:
        # Verify order belongs to this user
        order = query(
            "SELECT id, status FROM orders WHERE id = %s AND customer_id = %s",
            [order_id, user["id"]],
            fetch="one"
        )
        
        if not order:
            flash("Order not found.", "error")
            return redirect(url_for("customer_orders"))
        
        if order[1] != "pending":
            flash("Order cannot be confirmed at this time.", "error")
            return redirect(url_for("customer_orders"))
        
        # Update order status to confirmed
        execute(
            "UPDATE orders SET status = 'confirmed', confirmed_at = NOW() WHERE id = %s",
            [order_id]
        )
        
        # Log activity
        log_activity(user["id"], "order_confirmed", f"Confirmed order #{order_id}")
        
        # Fetch order details including shop address
        order_details = query(
            """
            SELECT off.commodity_name, o.quantity, o.total_price, cp.phone, cp.address
            FROM orders o
            JOIN offers off ON o.offer_id = off.id
            JOIN cashier_profiles cp ON off.cashier_id = cp.id
            WHERE o.id = %s
            """,
            [order_id],
            fetch="one"
        )
        
        if order_details:
            commodity_name, quantity, total_price, cashier_phone, cashier_address = order_details
            message_text = (
                f"Order confirmed for {quantity} x {commodity_name}. "
                f"Total: KES {total_price:.2f}. "
                f"Please pay to {cashier_phone} at {cashier_address} upon delivery."
            )
            send_transaction_message(order_id, "order_confirmation", message_text)
        
        flash("Order confirmed successfully.", "success")
    
    except Exception as e:
        flash(f"Error confirming order: {str(e)}", "error")
    
    return redirect(url_for("customer_orders"))
    

@app.route("/complete_order/<int:order_id>", methods=["GET", "POST"])
@login_required(role="customer")
def complete_order(order_id):
    """
    Allows a customer to complete payment for a confirmed order.
    SECURE VERSION: Uses explicit check-and-insert/update pattern for payment processing
    """
    user = get_user()
    form = PaymentConfirmationForm()

    # Get order details (existing code remains the same)
    order = query(
        """
        SELECT 
            o.*, 
            off.category_name,
            off.commodity_name,
            p.name AS product_name,
            cp.shop_name, 
            cp.address AS shop_address, 
            cp.phone AS cashier_phone
        FROM orders o
        JOIN offers off ON o.offer_id = off.id
        JOIN products p ON off.product_id = p.id
        JOIN cashier_profiles cp ON off.cashier_id = cp.id
        WHERE o.id = %s AND o.customer_id = %s
        """,
        [order_id, user["id"]],
        fetch="one"
    )

    if not order:
        flash("❌ Order not found.", "error")
        return redirect(url_for("customer_orders"))

    # Convert tuple to dictionary (existing code remains the same)
    keys = [
        "id", "customer_id", "offer_id", "quantity", "total_price", "status",
        "delivery_address", "terms_agreed", "created_at", "confirmed_at", "completed_at",
        "category_name", "commodity_name", "product_name",
        "shop_name", "shop_address", "cashier_phone"
    ]
    order_dict = dict(zip(keys, order))
    order_dict["total_price"] = float(order_dict.get("total_price") or 0.0)

    if order_dict["status"] != "confirmed":
        flash("⚠️ This order cannot be completed at this time.", "warning")
        return redirect(url_for("customer_orders"))

    # Handle payment submission
    if request.method == "POST" and form.validate():
        mpesa_message = (form.mpesa_message.data or "").strip()
        received_confirmation = form.received_confirmation.data == 'yes'

        if not received_confirmation:
            flash("⚠️ Please confirm that you have received your goods before completing payment.", "error")
            return redirect(url_for("complete_order", order_id=order_id))

        try:
            # SECURE PAYMENT PROCESSING WITH EXPLICIT TRANSACTIONS
            # Begin transaction
            execute("BEGIN")
            
            # Check if payment already exists for this order
            existing_payment = query(
                "SELECT id, mpesa_message FROM payments WHERE order_id = %s FOR UPDATE",
                [order_id],
                fetch="one"
            )
            
            if existing_payment:
                # Update existing payment with audit trail
                execute(
                    """
                    UPDATE payments 
                    SET mpesa_message = %s, 
                        received_confirmation = %s,
                        created_at = CASE WHEN mpesa_message != %s THEN CURRENT_TIMESTAMP ELSE created_at END
                    WHERE order_id = %s
                    """,
                    [mpesa_message, received_confirmation, mpesa_message, order_id]
                )
                
                # Log payment update for audit purposes
                log_activity(
                    user["id"],
                    "payment_updated",
                    f"Updated payment for order #{order_id}. Previous M-Pesa: {existing_payment[1][:50]}..."
                )
            else:
                # Insert new payment record
                execute(
                    """
                    INSERT INTO payments (order_id, mpesa_message, received_confirmation)
                    VALUES (%s, %s, %s)
                    """,
                    [order_id, mpesa_message, received_confirmation]
                )
                
                # Log new payment for audit purposes
                log_activity(
                    user["id"],
                    "payment_created", 
                    f"Created payment for order #{order_id}"
                )

            # CRITICAL: Verify order is still in confirmed status before completing
            current_order_status = query(
                "SELECT status FROM orders WHERE id = %s FOR UPDATE",
                [order_id],
                fetch="one"
            )
            
            if not current_order_status or current_order_status[0] != "confirmed":
                execute("ROLLBACK")
                flash("❌ Order status has changed. Please refresh and try again.", "error")
                return redirect(url_for("complete_order", order_id=order_id))

            # Update order status to completed
            execute(
                "UPDATE orders SET status='completed', completed_at=NOW() WHERE id=%s",
                [order_id]
            )

            # Commit all changes
            execute("COMMIT")

            # Extract M-Pesa details for logging
            mpesa_details = extract_mpesa_details(mpesa_message)

            # Log successful order completion
            log_activity(
                user["id"],
                "order_completed",
                f"Completed order #{order_id} via M-Pesa. Transaction: {mpesa_details.get('trx_id', 'N/A')}, Amount: {mpesa_details.get('amount', 'N/A')} KES"
            )

            # Send notifications
            customer_msg = f"✅ Your payment for {order_dict['commodity_name']} is complete. Thank you for your purchase!"
            cashier_msg = f"💰 Payment received for Order #{order_id}: {order_dict['commodity_name']} ({mpesa_details.get('amount', 'N/A')} KES)."

            send_transaction_message(order_id, "order_completion_customer", customer_msg)
            send_transaction_message(order_id, "order_completion_cashier", cashier_msg)

            flash("✅ Payment completed successfully! You can now download your receipt.", "success")
            return redirect(url_for("customer_orders"))

        except Exception as e:
            # Rollback on any error
            execute("ROLLBACK")
            
            # Log the error for investigation
            log_activity(
                user["id"],
                "payment_error",
                f"Payment failed for order #{order_id}: {str(e)}"
            )
            
            flash(f"❌ Error completing order: {str(e)}", "error")
            return redirect(url_for("complete_order", order_id=order_id))

    body = render_template_string(complete_order_html, order=order_dict, form=form)
    return render_template_string(base_html, title="Complete Order", body=body)


@app.route("/init_admin")
def init_admin():
    try:
        row = query("SELECT id FROM users WHERE role = 'admin' LIMIT 1", fetch="one")
        if row:
            return "Admin already exists", 400
            
        username = "admin"
        password = "admin123"
        execute(
            "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, 'admin')",
            [username, generate_password_hash(password)]
        )
        return "Default admin created: admin / admin123 (please change)", 200
    except Exception as e:
        return f"Error creating admin: {str(e)}", 500


@app.route("/health")
def health():
    try:
        _ = query("SELECT 1", fetch="one")
        return {"ok": True, "database": "connected"}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500
        

@app.route("/add_to_cart/<int:offer_id>", methods=["POST"])
@login_required(role="customer")
def add_to_cart(offer_id):
    user = get_user()
    quantity = request.form.get("quantity", 1, type=int)

    try:
        # Get or create cart
        cart_id = get_or_create_cart(user["id"])

        # Check if item already in cart
        existing = query(
            "SELECT id, quantity FROM cart_items WHERE cart_id = %s AND offer_id = %s",
            [cart_id, offer_id],
            fetch="one"
        )

        if existing:
            # ✅ FIX: use existing[0] for id and existing[1] for current quantity
            execute(
                "UPDATE cart_items SET quantity = %s WHERE id = %s",
                [existing[1] + quantity, existing[0]]
            )
        else:
            # Add new item to cart
            execute(
                "INSERT INTO cart_items (cart_id, offer_id, quantity) VALUES (%s, %s, %s)",
                [cart_id, offer_id, quantity]
            )

        flash("Item added to cart successfully.", "success")

        # Save to session
        cart_items = get_cart_items(cart_id)
        session_data = {
            "cart_id": cart_id,
            "items": cart_items,
            "last_added": offer_id
        }
        save_order_session(user["id"], session_data)

    except Exception as e:
        flash(f"Error adding item to cart: {str(e)}", "error")

    return redirect(request.referrer or url_for("app_home"))


@app.route("/cart")
@login_required(role="customer")
def view_cart():
    user = get_user()
    try:
        cart_id = get_or_create_cart(user["id"])
        cart_items = get_cart_items(cart_id) or []

        # Validate data types before total calculation  
        total = sum(  
            (float(item.get("price", 0)) or 0) * int(item.get("quantity", 0))  
            for item in cart_items  
        )  

        body = render_template_string(cart_html, cart_items=cart_items, total=total)  
        return render_template_string(base_html, title="Shopping Cart", body=body)  

    except Exception as e:  
        current_app.logger.exception("Error loading cart")  # ← logs full traceback  
        flash("Something went wrong while loading your cart. Please try again.", "error")  
        return redirect(url_for("app_home"))


@app.route("/update_cart/<int:item_id>", methods=["POST"])
@login_required(role="customer")
def update_cart(item_id):
    user = get_user()
    quantity = request.form.get("quantity", 1, type=int)

    try:
        if quantity <= 0:
            execute("DELETE FROM cart_items WHERE id = %s", [item_id])
            flash("Item removed from cart.", "success")
        else:
            execute("UPDATE cart_items SET quantity = %s WHERE id = %s", [quantity, item_id])
            flash("Cart updated successfully.", "success")

        # Always refresh session data from DB
        cart_id = get_or_create_cart(user["id"])
        cart_items = get_cart_items(cart_id) or []
        session_data = {
            "cart_id": cart_id,
            "items": cart_items,
        }
        save_order_session(user["id"], session_data)

    except Exception as e:
        current_app.logger.exception("Error updating cart")  # ← logs full traceback
        flash("Could not update cart. Please try again.", "error")

    return redirect(url_for("view_cart"))


@app.route("/checkout", methods=["POST"])
@login_required(role="customer")
def checkout():
    """
    Handles checkout from the customer's cart.
    Creates one order per cart item and clears the cart afterward.
    """
    user = get_user()

    try:
        cart_id = get_or_create_cart(user["id"])
        cart_items = get_cart_items(cart_id)

        if not cart_items:
            flash("Your cart is empty.", "warning")
            return redirect(url_for("view_cart"))

        # --- Delivery address ---
        delivery_address = request.form.get("delivery_address", "").strip()
        if not delivery_address:
            flash("Please provide a delivery address.", "error")
            return redirect(url_for("view_cart"))

        order_ids = []

        for item in cart_items:
            # Safely extract primitive values only
            offer_id = item.get("offer_id")
            if isinstance(offer_id, dict):
                offer_id = offer_id.get("id")

            commodity_name = (
                item.get("commodity_name")
                or (item.get("offer") or {}).get("commodity_name")
                or "Unknown"
            )

            # Calculate total safely
            price = item.get("price", 0)
            quantity = item.get("quantity", 1)
            try:
                total_price = float(price) * float(quantity)
            except Exception:
                total_price = 0.0

            # --- Insert order ---
            order_row = query(
                """
                INSERT INTO orders (customer_id, offer_id, quantity, total_price, delivery_address, terms_agreed)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                [user["id"], offer_id, quantity, total_price, delivery_address, True],
                fetch="one"
            )

            if order_row:
                order_id = order_row[0]
                order_ids.append(order_id)
                log_activity(
                    user["id"],
                    "order_created",
                    f"Created order #{order_id} for {quantity} × {commodity_name}"
                )
            else:
                current_app.logger.warning("Order insert returned no ID for user %s", user["id"])

        # --- Clear the cart ---
        execute("DELETE FROM cart_items WHERE cart_id = %s", [cart_id])

        # --- Update session ---
        session_data = {
            "cart_id": cart_id,
            "items": [],
            "recent_orders": order_ids
        }
        save_order_session(user["id"], session_data)

        flash("Order placed successfully! Please confirm your orders.", "success")
        return redirect(url_for("customer_orders"))

    except Exception as e:
        current_app.logger.exception("Error during checkout for user %s", user["id"])
        flash(f"Error during checkout: {str(e)}", "error")
        return redirect(url_for("view_cart"))

@app.route("/order_session")
@login_required(role="customer")
def order_session():
    user = get_user()
    
    try:
        # Get order session
        session_data = get_order_session(user["id"])
        
        # Get pending orders that need action
        pending_orders = query(
            """
            SELECT o.id, off.category_name, p.name as product_name, off.commodity_name,   
       o.quantity, o.total_price, o.status, o.created_at  
FROM orders o  
JOIN offers off ON o.offer_id = off.id  
JOIN products p ON off.product_id = p.id  
WHERE o.customer_id = %s AND o.status IN ('pending', 'confirmed')  
ORDER BY o.created_at DESC
            """,
            [user["id"]]
        )
        
        pending_orders = [
            {
                "id": r[0], "category_name": r[1],  "product_name": r[2], "commodity_name": r[3],
                "quantity": r[4], "total_price": float(r[5]),
                "status": r[6], "created_at": r[7]
            }
            for r in pending_orders
        ]
        
        # Get cart items count
        cart_id = get_or_create_cart(user["id"])
        cart_items = get_cart_items(cart_id)
        cart_count = len(cart_items)
        
        body = render_template_string(order_session_html, 
                                    session_data=session_data,
                                    pending_orders=pending_orders,
                                    cart_count=cart_count,
                                    now=datetime.now())
        return render_template_string(base_html, title="Order Session", body=body)
        
    except Exception as e:
        flash(f"Error loading order session: {str(e)}", "error")
        return redirect(url_for("app_home"))
        

@app.route("/customer/orders")
@login_required(role="customer")
def customer_orders():
    user = get_user()
    
    try:
        orders = query(
            """
            SELECT 
                o.id, 
                off.category_name,
                p.name AS product_name, 
                off.commodity_name, 
                o.quantity, 
                o.total_price, 
                o.status, 
                o.created_at, 
                o.confirmed_at
            FROM orders o
            JOIN offers off ON o.offer_id = off.id
            JOIN products p ON off.product_id = p.id
            WHERE o.customer_id = %s
            ORDER BY o.created_at DESC
            """,
            [user["id"]]
        )
        
        orders = [
            {
                "id": r[0],
                "category_name": r[1],
                "product_name": r[2],
                "commodity_name": r[3],
                "quantity": r[4],
                "total_price": float(r[5]) if r[5] else 0.0,
                "status": r[6],
                "created_at": r[7],
                "confirmed_at": r[8]
            }
            for r in orders
        ]
        
        body = render_template_string(customer_orders_html, orders=orders)
        return render_template_string(base_html, title="My Orders", body=body)
    
    except Exception as e:
        flash(f"Error loading orders: {str(e)}", "error")
        return redirect(url_for("app_home"))


@app.route("/receipt_options/<int:order_id>")
@login_required(role="customer")
def receipt_options(order_id):
    """Presents download and Bluetooth print options to user after completing order."""
    user = get_user()
    
    # Verify order exists and belongs to user
    order = query(
        "SELECT id, status FROM orders WHERE id = %s AND customer_id = %s",
        [order_id, user["id"]],
        fetch="one"
    )
    
    if not order:
        flash("Order not found.", "error")
        return redirect(url_for("customer_orders"))

    # Only allow completed orders to generate receipts
    if order[1] != 'completed':
        flash("Receipt not available until order is completed.", "warning")
        return redirect(url_for("customer_orders"))

    html = f"""
    <section>
        <div class="section-header">
            <h2 class="section-title"><i class="fas fa-receipt"></i> Receipt for Order #{order_id}</h2>
        </div>
        <div class="card" style="text-align:center; padding:2rem;">
            <p>Your order has been completed successfully.</p>
            <div style="display: flex; flex-direction: column; gap: 1rem; align-items: center;">
                <a href="{url_for('download_receipt', order_id=order_id)}" class="btn-primary" style="width: 200px;">
                    <i class="fas fa-download"></i> Download Receipt
                </a>
                <button onclick="printBluetooth({order_id})" class="btn-success" style="width: 200px;">
                    <i class="fas fa-bluetooth"></i> Print via Bluetooth
                </button>
                <a href="{url_for('view_receipt', order_id=order_id)}" class="btn-secondary" style="width: 200px;">
                    <i class="fas fa-eye"></i> View in Browser
                </a>
            </div>
            <script>
            async function printBluetooth(order_id) {{
                if (!confirm('Do you want to send this receipt to the Bluetooth printer?')) return;
                try {{
                    const response = await fetch(`/print_receipt/${{order_id}}`, {{ method: 'POST' }});
                    const data = await response.json();
                    if (data.success) {{
                        alert('Printing initiated successfully.');
                    }} else {{
                        alert('Failed to connect to printer. Operation cancelled.');
                    }}
                }} catch (e) {{
                    alert('Connection error. Printing cancelled.');
                }}
            }}
            </script>
        </div>
    </section>
    """
    return render_template_string(base_html, title="Receipt Options", body=html)


@app.route("/download_receipt/<int:order_id>")
@login_required(role="customer")
def download_receipt(order_id):
    """Generate and download PDF receipt with professional formatting"""
    try:
        user = get_user()
        
        # Get complete order data for receipt
        order = query(
            """
            SELECT 
                o.*, 
                off.category_name,
                off.commodity_name,
                p.name AS product_name,
                cp.shop_name, 
                cp.address AS shop_address, 
                cp.phone AS cashier_phone,
                pay.mpesa_message,
                pay.received_confirmation,
                pay.created_at as payment_date
            FROM orders o
            JOIN offers off ON o.offer_id = off.id
            JOIN products p ON off.product_id = p.id
            JOIN cashier_profiles cp ON off.cashier_id = cp.id
            LEFT JOIN payments pay ON o.id = pay.order_id
            WHERE o.id = %s AND o.customer_id = %s AND o.status = 'completed'
            """,
            [order_id, user["id"]],
            fetch="one"
        )
        
        if not order:
            flash("Receipt not found or order not completed.", "error")
            return redirect(url_for("customer_orders"))
        
        # Convert to dictionary
        keys = [
            "id", "customer_id", "offer_id", "quantity", "total_price", "status",
            "delivery_address", "terms_agreed", "created_at", "confirmed_at", "completed_at",
            "category_name", "commodity_name", "product_name",
            "shop_name", "shop_address", "cashier_phone", "mpesa_message",
            "received_confirmation", "payment_date"
        ]
        order_dict = dict(zip(keys, order))
        order_dict["total_price"] = float(order_dict.get("total_price") or 0.0)
        
        # Create PDF receipt
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=72, bottomMargin=72)
        
        # Styles
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'Title',
            parent=styles['Heading1'],
            fontSize=16,
            spaceAfter=30,
            alignment=1  # Center
        )
        
        header_style = ParagraphStyle(
            'Header',
            parent=styles['Heading2'],
            fontSize=12,
            spaceAfter=12
        )
        
        normal_style = styles["Normal"]
        
        # Build story
        story = []
        
        # Title
        story.append(Paragraph("OFFICIAL RECEIPT", title_style))
        story.append(Spacer(1, 20))
        
        # Shop Information
        story.append(Paragraph(f"<b>{order_dict['shop_name']}</b>", header_style))
        story.append(Paragraph(f"Address: {order_dict['shop_address']}", normal_style))
        story.append(Paragraph(f"Phone: {order_dict['cashier_phone']}", normal_style))
        story.append(Spacer(1, 15))
        
        # Order Details
        story.append(Paragraph("Order Details", header_style))
        
        data = [
            ["Receipt No:", f"#{order_dict['id']}"],
            ["Date:", order_dict['completed_at'].strftime('%Y-%m-%d %H:%M') if order_dict.get('completed_at') else "N/A"],
            ["Product:", order_dict['product_name']],
            ["Commodity:", order_dict['commodity_name']],
            ["Category:", order_dict['category_name']],
            ["Quantity:", str(order_dict['quantity'])],
            ["Unit Price:", f"KES {order_dict['total_price'] / order_dict['quantity']:.2f}"],
            ["Total Amount:", f"<b>KES {order_dict['total_price']:.2f}</b>"],
            ["", ""],
            ["Delivery Address:", order_dict['delivery_address']],
            ["Payment Method:", "M-Pesa"],
            ["Payment Status:", "Confirmed"],
        ]
        
        if order_dict['mpesa_message']:
            data.append(["M-Pesa Message:", order_dict['mpesa_message']])
        
        table = Table(data, colWidths=[200, 200])
        table.setStyle(TableStyle([
            ('FONT', (0, 0), (-1, -1), 'Helvetica', 10),
            ('FONT', (0, 7), (1, 7), 'Helvetica-Bold', 10),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LINEBELOW', (0, 0), (-1, 0), 1, colors.black),
            ('LINEABOVE', (0, -1), (-1, -1), 1, colors.black),
        ]))
        
        story.append(table)
        story.append(Spacer(1, 20))
        
        # Footer
        story.append(Paragraph("Thank you for your purchase!", normal_style))
        story.append(Spacer(1, 10))
        story.append(Paragraph("This is an official receipt generated automatically.", 
                             styles["Italic"]))
        
        # Build PDF
        doc.build(story)
        buffer.seek(0)
        
        # Log receipt download
        log_activity(
            user["id"],
            "receipt_downloaded",
            f"Downloaded receipt for order #{order_id}"
        )
        
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"receipt_{order_id}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
            mimetype='application/pdf'
        )
        
    except Exception as e:
        flash(f"Error generating receipt: {str(e)}", "error")
        return redirect(url_for("customer_orders"))


@app.route("/print_receipt/<int:order_id>", methods=["POST"])
@login_required(role="customer")
def print_receipt(order_id):
    """Handle receipt printing request"""
    try:
        user = get_user()
        
        # Verify order belongs to user
        order = query(
            "SELECT id FROM orders WHERE id = %s AND customer_id = %s",
            [order_id, user["id"]],
            fetch="one"
        )
        
        if not order:
            return jsonify({"success": False, "message": "Order not found"}), 404
        
        # In a real implementation, this would send to a thermal printer
        # For now, we'll log the request and return success
        log_activity(
            user["id"],
            "receipt_print_request",
            f"Print request for receipt #{order_id}"
        )
        
        return jsonify({
            "success": True, 
            "message": "Print request received. This would typically send the receipt to a printer."
        })
        
    except Exception as e:
        return jsonify({"success": False, "message": f"Printing error: {str(e)}"}), 500


@app.route("/receipt/<int:order_id>")
@login_required(role="customer")
def view_receipt(order_id):
    """View receipt in browser with professional formatting"""
    try:
        user = get_user()
        
        # Get complete order data for receipt
        order = query(
            """
            SELECT 
                o.*, 
                off.category_name,
                off.commodity_name,
                p.name AS product_name,
                cp.shop_name, 
                cp.address AS shop_address, 
                cp.phone AS cashier_phone,
                pay.mpesa_message,
                pay.received_confirmation,
                pay.created_at as payment_date
            FROM orders o
            JOIN offers off ON o.offer_id = off.id
            JOIN products p ON off.product_id = p.id
            JOIN cashier_profiles cp ON off.cashier_id = cp.id
            LEFT JOIN payments pay ON o.id = pay.order_id
            WHERE o.id = %s AND o.customer_id = %s AND o.status = 'completed'
            """,
            [order_id, user["id"]],
            fetch="one"
        )
        
        if not order:
            body = "<p style='color:red;text-align:center;'>❌ Receipt not found or order not completed.</p>"
            return render_template_string(base_html, title="Receipt Not Found", body=body)

        # Convert to dictionary
        keys = [
            "id", "customer_id", "offer_id", "quantity", "total_price", "status",
            "delivery_address", "terms_agreed", "created_at", "confirmed_at", "completed_at",
            "category_name", "commodity_name", "product_name",
            "shop_name", "shop_address", "cashier_phone", "mpesa_message",
            "received_confirmation", "payment_date"
        ]
        order_dict = dict(zip(keys, order))
        order_dict["total_price"] = float(order_dict.get("total_price") or 0.0)

        quantity = float(order_dict.get("quantity", 1) or 1)
        total_price = order_dict["total_price"]
        unit_price = total_price / quantity if quantity > 0 else total_price

        # Professional receipt HTML
        receipt_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Official Receipt #{order_dict['id']}</title>
            <style>
                body {{
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    background: #f8f9fa;
                    margin: 0;
                    padding: 20px;
                    color: #333;
                }}
                .receipt-container {{
                    max-width: 800px;
                    margin: 0 auto;
                    background: white;
                    border-radius: 12px;
                    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
                    overflow: hidden;
                }}
                .receipt-header {{
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    padding: 30px;
                    text-align: center;
                }}
                .shop-name {{
                    font-size: 28px;
                    font-weight: bold;
                    margin-bottom: 10px;
                }}
                .receipt-title {{
                    font-size: 18px;
                    opacity: 0.9;
                }}
                .receipt-body {{
                    padding: 30px;
                }}
                .section {{
                    margin-bottom: 25px;
                    padding-bottom: 20px;
                    border-bottom: 1px solid #eee;
                }}
                .section-title {{
                    font-size: 16px;
                    font-weight: bold;
                    color: #667eea;
                    margin-bottom: 15px;
                    display: flex;
                    align-items: center;
                }}
                .section-title i {{
                    margin-right: 8px;
                }}
                .detail-grid {{
                    display: grid;
                    grid-template-columns: 1fr 1fr;
                    gap: 12px;
                }}
                .detail-item {{
                    display: flex;
                    justify-content: space-between;
                    padding: 8px 0;
                    border-bottom: 1px dotted #eee;
                }}
                .detail-label {{
                    font-weight: 500;
                    color: #666;
                }}
                .detail-value {{
                    font-weight: 600;
                }}
                .total-row {{
                    background: #f8f9fa;
                    padding: 15px;
                    border-radius: 8px;
                    margin-top: 10px;
                    font-size: 18px;
                    font-weight: bold;
                    color: #2c5530;
                }}
                .mpesa-message {{
                    background: #e8f5e8;
                    padding: 15px;
                    border-radius: 8px;
                    border-left: 4px solid #28a745;
                    margin-top: 15px;
                    font-family: monospace;
                    white-space: pre-wrap;
                }}
                .receipt-footer {{
                    background: #f8f9fa;
                    padding: 20px 30px;
                    text-align: center;
                    border-top: 1px solid #eee;
                    color: #666;
                    font-size: 14px;
                }}
                .action-buttons {{
                    display: flex;
                    gap: 10px;
                    justify-content: center;
                    margin-top: 20px;
                }}
                @media print {{
                    body {{ background: white; }}
                    .receipt-container {{ box-shadow: none; }}
                    .action-buttons {{ display: none; }}
                }}
            </style>
        </head>
        <body>
            <div class="receipt-container">
                <div class="receipt-header">
                    <div class="shop-name">{order_dict.get('shop_name', 'Shop Name')}</div>
                    <div class="receipt-title">OFFICIAL RECEIPT</div>
                </div>
                
                <div class="receipt-body">
                    <div class="section">
                        <div class="section-title">
                            <i class="fas fa-store"></i> Business Information
                        </div>
                        <div class="detail-grid">
                            <div><strong>Address:</strong> {order_dict.get('shop_address', 'N/A')}</div>
                            <div><strong>Phone:</strong> {order_dict.get('cashier_phone', 'N/A')}</div>
                            <div><strong>Receipt No:</strong> #{order_dict.get('id', 'N/A')}</div>
                            <div><strong>Date:</strong> {order_dict.get('completed_at').strftime('%Y-%m-%d %H:%M') if order_dict.get('completed_at') else 'N/A'}</div>
                        </div>
                    </div>

                    <div class="section">
                        <div class="section-title">
                            <i class="fas fa-shopping-cart"></i> Order Details
                        </div>
                        <div class="detail-item">
                            <span class="detail-label">Product:</span>
                            <span class="detail-value">{order_dict.get('product_name', 'N/A')}</span>
                        </div>
                        <div class="detail-item">
                            <span class="detail-label">Commodity:</span>
                            <span class="detail-value">{order_dict.get('commodity_name', 'N/A')}</span>
                        </div>
                        <div class="detail-item">
                            <span class="detail-label">Category:</span>
                            <span class="detail-value">{order_dict.get('category_name', 'N/A')}</span>
                        </div>
                        <div class="detail-item">
                            <span class="detail-label">Quantity:</span>
                            <span class="detail-value">{quantity:.0f}</span>
                        </div>
                        <div class="detail-item">
                            <span class="detail-label">Unit Price:</span>
                            <span class="detail-value">KES {unit_price:.2f}</span>
                        </div>
                        <div class="total-row">
                            <span>Total Amount:</span>
                            <span>KES {total_price:.2f}</span>
                        </div>
                    </div>

                    <div class="section">
                        <div class="section-title">
                            <i class="fas fa-credit-card"></i> Payment Information
                        </div>
                        <div class="detail-grid">
                            <div><strong>Payment Method:</strong> M-Pesa</div>
                            <div><strong>Status:</strong> <span style="color: #28a745;">Completed</span></div>
                            <div><strong>Payment Date:</strong> {order_dict.get('payment_date').strftime('%Y-%m-%d %H:%M') if order_dict.get('payment_date') else 'N/A'}</div>
                            <div><strong>Goods Received:</strong> {"Yes" if order_dict.get('received_confirmation') else "No"}</div>
                        </div>
                        {f'<div class="mpesa-message"><strong>M-Pesa Confirmation:</strong><br>{order_dict.get("mpesa_message", "N/A")}</div>' if order_dict.get('mpesa_message') else ''}
                    </div>

                    <div class="section">
                        <div class="section-title">
                            <i class="fas fa-user"></i> Customer Information
                        </div>
                        <div class="detail-grid">
                            <div><strong>Delivery Address:</strong> {order_dict.get('delivery_address', 'N/A')}</div>
                            <div><strong>Terms Agreed:</strong> {"Yes" if order_dict.get('terms_agreed') else "No"}</div>
                        </div>
                    </div>
                </div>
                
                <div class="receipt-footer">
                    <p><strong>Thank you for your business!</strong></p>
                    <p>This receipt is computer generated and does not require a signature.</p>
                    <p>Generated on {datetime.now().strftime('%Y-%m-%d at %H:%M')}</p>
                    
                    <div class="action-buttons">
                        <a href="{url_for('download_receipt', order_id=order_id)}" class="btn-primary">
                            <i class="fas fa-download"></i> Download PDF
                        </a>
                        <button onclick="window.print()" class="btn-secondary">
                            <i class="fas fa-print"></i> Print Receipt
                        </button>
                        <a href="{url_for('customer_orders')}" class="btn-outline">
                            <i class="fas fa-arrow-left"></i> Back to Orders
                        </a>
                    </div>
                </div>
            </div>
        </body>
        </html>
        """
        return render_template_string(receipt_html)

    except Exception as e:
        flash(f"Error generating receipt: {str(e)}", "error")
        return redirect(url_for("customer_orders"))

if __name__ == "__main__":
    # Create static directory if it doesn't exist
    if not os.path.exists('static'):
        os.makedirs('static')
    
    init_app()
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True);
