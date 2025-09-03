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

# Load environment variables
load_dotenv()

# Slack Configuration
SLACK_CLIENT_ID = os.getenv('SLACK_CLIENT_ID')
SLACK_CLIENT_SECRET = os.getenv('SLACK_CLIENT_SECRET')
SLACK_REDIRECT_URI = os.getenv('SLACK_REDIRECT_URI', 'http://localhost:5000/slack/callback')
SLACK_SCOPES = 'users:read,users:read.email,identity.basic,identity.email,identity.avatar'

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
    code = request.args.get('code')
    if not code:
        flash('Slack authentication failed')
        return redirect(url_for('index'))

    try:
        # Exchange code for token
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
        flash(f'Error authenticating with Slack: {str(e)}')
        return redirect(url_for('register'))

# Initialize database when the app starts
with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(debug=True)