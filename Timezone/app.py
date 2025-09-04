from flask import Flask, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import sqlite3
import os
import pytz
from functools import wraps
import secrets
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv
from flask import jsonify
import re
from datetime import datetime, timedelta

# Load environment variables
load_dotenv()

# Deployment configuration
IS_PRODUCTION = os.getenv('RENDER', False)
BASE_URL = os.getenv('RENDER_EXTERNAL_URL', 'http://localhost:5000')

# Slack Configuration (read from env; do not hardcode secrets)
SLACK_CLIENT_ID = os.getenv('SLACK_CLIENT_ID')
SLACK_CLIENT_SECRET = os.getenv('SLACK_CLIENT_SECRET')
SLACK_REDIRECT_URI = f"{BASE_URL}/slack/callback"
SLACK_SCOPES = os.getenv('SLACK_SCOPES', 'users:read,users:read.email,identity.basic,identity.email,identity.avatar,commands')

# Debug logging of config (but not secrets)
print(f"Slack App ID: {SLACK_CLIENT_ID}")
print(f"Redirect URI: {SLACK_REDIRECT_URI}")
print(f"Scopes: {SLACK_SCOPES}")

# Fail fast in development if Slack credentials are missing
if not SLACK_CLIENT_ID or not SLACK_CLIENT_SECRET:
    raise RuntimeError('Missing SLACK_CLIENT_ID or SLACK_CLIENT_SECRET. Set them in your environment or .env file.')

# Create Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = secrets.token_hex(16)  # Generate a secure secret key
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=60)  # Session timeout

# Database setup
def init_db():
    conn = sqlite3.connect('teamzone.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            timezone TEXT,
            slack_id TEXT UNIQUE NOT NULL,
            avatar TEXT,
            workspace_id TEXT NOT NULL
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS contact_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            subject TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

# Database helper functions
def get_db():
    conn = sqlite3.connect('teamzone.db')
    conn.row_factory = sqlite3.Row
    return conn

def query_db(query, args=(), one=False):
    conn = get_db()
    cur = conn.execute(query, args)
    rv = cur.fetchall()
    conn.close()
    return (rv[0] if rv else None) if one else rv

def modify_db(query, args=()):
    conn = get_db()
    cur = conn.execute(query, args)
    conn.commit()
    lastrowid = cur.lastrowid
    conn.close()
    return lastrowid

# Authentication decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Helper functions for finding meeting times
def find_suitable_meeting_times(user_timezones):
    """Find suitable meeting times across different timezones"""
    # Consider 9 AM to 5 PM as working hours
    meeting_times = []
    base_time = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    
    # Check next 5 business days
    for day in range(5):
        current_date = base_time + timedelta(days=day)
        if current_date.weekday() >= 5:  # Skip weekends
            continue
            
        for hour in range(9, 17):  # 9 AM to 5 PM
            time_to_check = current_date.replace(hour=hour)
            is_suitable = True
            
            # Check if this time works for all users
            for tz in user_timezones:
                local_time = time_to_check.astimezone(pytz.timezone(tz))
                local_hour = local_time.hour
                if local_hour < 9 or local_hour >= 17:
                    is_suitable = False
                    break
            
            if is_suitable:
                meeting_times.append(time_to_check)
                
    return meeting_times[:5]  # Return top 5 suitable times

def format_meeting_times(times, user_timezones):
    """Format meeting times for all users"""
    formatted_times = []
    for time in times:
        time_slots = []
        for tz in user_timezones:
            local_time = time.astimezone(pytz.timezone(tz))
            time_slots.append(f"{local_time.strftime('%I:%M %p %Z')}")
        formatted_times.append(time_slots)
    return formatted_times

# Routes
@app.route('/')
def index():
    return render_template('index.html', user=session.get('user_name'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    users = query_db('SELECT name, timezone FROM users')
    user_times = []
    for user in users:
        if user['timezone']:
            local_time = datetime.now(pytz.timezone(user['timezone']))
            user_times.append({
                'name': user['name'],
                'time': local_time.strftime('%I:%M %p'),
                'timezone': user['timezone']
            })
    return render_template('dashboard.html', user_times=user_times, user=session.get('user_name'))

@app.route('/contact', methods=['POST'])
def contact():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        subject = request.form.get('subject')
        message = request.form.get('message')
        
        if not all([name, email, subject, message]):
            flash('Please fill in all fields')
            return redirect(url_for('index', _anchor='contact'))
        
        try:
            modify_db(
                'INSERT INTO contact_messages (name, email, subject, message) VALUES (?, ?, ?, ?)',
                [name, email, subject, message]
            )
            flash('Message sent successfully! We will get back to you soon.', 'success')
        except Exception as e:
            flash('Error sending message. Please try again.', 'error')
            
        return redirect(url_for('index', _anchor='contact'))

@app.route('/slack/login')
def slack_login():
    """Initiate Slack OAuth flow"""
    return redirect(f'https://slack.com/oauth/v2/authorize?'
                   f'client_id={SLACK_CLIENT_ID}&'
                   f'scope={SLACK_SCOPES}&'
                   f'redirect_uri={SLACK_REDIRECT_URI}')

@app.route('/slack/callback')
def slack_callback():
    """Handle Slack OAuth callback"""
    # Check for OAuth error first
    error = request.args.get('error')
    if error:
        print(f"Slack OAuth error: {error}")  # Debug log
        flash(f'Slack authorization failed: {error}')
        return redirect(url_for('index'))

    # Check for auth code
    code = request.args.get('code')
    if not code:
        print("No code received from Slack")  # Debug log
        flash('Slack authentication failed - no code received')
        return redirect(url_for('index'))

    try:
        print(f"Starting OAuth exchange with code: {code[:5]}...")  # Debug log (truncated)
        client = WebClient()
        auth_response = client.oauth_v2_access(
            client_id=SLACK_CLIENT_ID,
            client_secret=SLACK_CLIENT_SECRET,
            code=code,
            redirect_uri=SLACK_REDIRECT_URI
        )

        # Get user info from Slack
        user_client = WebClient(token=auth_response['access_token'])
        user_info = user_client.users_identity()
        workspace_id = auth_response['team']['id']
        
        user = {
            'email': user_info['user']['email'],
            'name': user_info['user']['name'],
            'slack_id': user_info['user']['id'],
            'avatar': user_info['user'].get('image_512', ''),
            'workspace_id': workspace_id
        }

        # Check if user already exists
        existing_user = query_db('SELECT id FROM users WHERE slack_id = ?', [user['slack_id']], one=True)
        
        if existing_user:
            # Update existing user info
            modify_db('''
                UPDATE users 
                SET name = ?, email = ?, avatar = ?
                WHERE slack_id = ?
            ''', [user['name'], user['email'], user['avatar'], user['slack_id']])
            user_id = existing_user['id']
        else:
            # Get user's timezone from Slack
            user_profile = user_client.users_info(user=user['slack_id'])
            timezone = user_profile['user'].get('tz', 'UTC')
            
            # Create new user
            user_id = modify_db('''
                INSERT INTO users (email, name, timezone, slack_id, avatar, workspace_id) 
                VALUES (?, ?, ?, ?, ?, ?)
            ''', [user['email'], user['name'], timezone, user['slack_id'], user['avatar'], user['workspace_id']])

        # Set session
        session.permanent = True
        session['user_id'] = user_id
        session['user_name'] = user['name']
        session['workspace_id'] = workspace_id
        flash('Successfully connected with Slack!')
        return redirect(url_for('dashboard'))

    except SlackApiError as e:
        error_message = str(e.response['error']) if hasattr(e, 'response') and 'error' in e.response else str(e)
        print(f"Slack API Error: {error_message}")  # Debug log
        flash(f'Error connecting to Slack: {error_message}')
        return redirect(url_for('index'))
    except Exception as e:
        print(f"Unexpected error in Slack callback: {str(e)}")  # Debug log
        flash('An unexpected error occurred during Slack authentication')
        return redirect(url_for('index'))

@app.route('/slack/command/tz', methods=['POST'])
def handle_tz_command():
    """Handle /tz slash command"""
    if not request.form:
        return jsonify({'error': 'Invalid request'}), 400
        
    # Verify the request is from Slack (you should implement proper verification)
    text = request.form.get('text', '')
    user_id = request.form.get('user_id', '')
    
    # Parse the command
    # Expected format: "find time for @user1 and me"
    if not text.startswith('find time for'):
        return jsonify({
            'response_type': 'ephemeral',
            'text': 'Please use the format: /tz find time for @user1 and me'
        })
    
    # Extract mentioned users
    mentioned_users = re.findall(r'@(\w+)', text)
    user_timezones = []
    user_names = []
    
    # Get the command issuer's timezone
    issuer = query_db('SELECT name, timezone FROM users WHERE slack_id = ?', [user_id], one=True)
    if not issuer:
        return jsonify({
            'response_type': 'ephemeral',
            'text': 'You need to connect your Slack account first'
        })
    
    user_timezones.append(issuer['timezone'])
    user_names.append(issuer['name'])
    
    # Get mentioned users' timezones
    for username in mentioned_users:
        user = query_db('SELECT name, timezone FROM users WHERE name LIKE ?', [f'%{username}%'], one=True)
        if not user:
            return jsonify({
                'response_type': 'ephemeral',
                'text': f'User @{username} not found in the system'
            })
        user_timezones.append(user['timezone'])
        user_names.append(user['name'])
    
    # Find suitable meeting times
    suitable_times = find_suitable_meeting_times(user_timezones)
    if not suitable_times:
        return jsonify({
            'response_type': 'in_channel',
            'text': 'No suitable meeting times found in the next 5 business days during working hours (9 AM - 5 PM)'
        })
    
    # Format the response
    formatted_times = format_meeting_times(suitable_times, user_timezones)
    response_text = f"Suitable meeting times for {', '.join(user_names)}:\n\n"
    
    for i, time_slots in enumerate(formatted_times):
        response_text += f"Option {i+1}:\n"
        for j, slot in enumerate(time_slots):
            response_text += f"- {user_names[j]}: {slot}\n"
        response_text += "\n"
    
    return jsonify({
        'response_type': 'in_channel',
        'text': response_text
    })

# Initialize database when the app starts
with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(debug=True)