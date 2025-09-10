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
import json
from typing import List, Dict, Any, Optional

# Load environment variables
load_dotenv()

# Slack Configuration
SLACK_CLIENT_ID = os.getenv('SLACK_CLIENT_ID') 
SLACK_CLIENT_SECRET = os.getenv('SLACK_CLIENT_SECRET')
SLACK_BOT_TOKEN = os.getenv('SLACK_BOT_TOKEN') 
SLACK_REDIRECT_URI = f"{BASE_URL}/slack/callback" 
SLACK_SCOPES = os.getenv('SLACK_SCOPES')

# Fail fast if Slack credentials are missing
if not SLACK_CLIENT_ID or not SLACK_CLIENT_SECRET:
    raise RuntimeError('Missing SLACK_CLIENT_ID or SLACK_CLIENT_SECRET. Set them in your environment or .env file.')

# Create Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = secrets.token_hex(16)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=60)

# Database setup
def init_db():
    conn = sqlite3.connect('teamzone.db')
    c = conn.cursor()
    
    # Users table
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            timezone TEXT DEFAULT 'UTC',
            slack_id TEXT UNIQUE NOT NULL,
            avatar TEXT,
            workspace_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Contact messages table
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
    
    # Meetings table
    c.execute('''
        CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organizer_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            proposed_times TEXT NOT NULL,
            selected_time TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (organizer_id) REFERENCES users (id)
        )
    ''')
    
    # Meeting participants table
    c.execute('''
        CREATE TABLE IF NOT EXISTS meeting_participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            availability TEXT,
            timezone TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (meeting_id) REFERENCES meetings (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
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

# Timezone and meeting utilities
COMMON_TIMEZONES = {
    "est": "America/New_York",
    "pst": "America/Los_Angeles",
    "cst": "America/Chicago",
    "mst": "America/Denver",
    "gmt": "Europe/London",
    "cet": "Europe/Paris",
    "aest": "Australia/Sydney",
    "ist": "Asia/Kolkata",
    "jst": "Asia/Tokyo",
}

def parse_timezone_input(timezone_input: str) -> Optional[str]:
    """Parse timezone input and return valid timezone string"""
    if not timezone_input:
        return None
        
    timezone_input = timezone_input.lower().strip()
    
    # Check if it's a common timezone abbreviation
    if timezone_input in COMMON_TIMEZONES:
        return COMMON_TIMEZONES[timezone_input]
    
    # Try to find the timezone in the pytz database
    try:
        # Check if it's a valid timezone
        pytz.timezone(timezone_input)
        return timezone_input
    except pytz.UnknownTimeZoneError:
        # Try to find a timezone by city name
        matching_timezones = [
            tz for tz in pytz.all_timezones 
            if timezone_input.lower() in tz.lower()
        ]
        return matching_timezones[0] if matching_timezones else None

def generate_time_slots(start_date: datetime, days: int = 7, interval_hours: int = 1) -> List[datetime]:
    """Generate time slots for the next few days"""
    time_slots = []
    current_date = start_date.replace(hour=9, minute=0, second=0, microsecond=0)
    
    for day in range(days):
        for hour in range(9, 17):  # 9 AM to 5 PM
            time_slot = current_date.replace(hour=hour)
            time_slots.append(time_slot)
        current_date += timedelta(days=1)
    
    return time_slots

def find_best_meeting_times(user_timezones: List[str], duration_hours: int = 1) -> List[Dict[str, Any]]:
    """Find the best meeting times across multiple timezones"""
    if not user_timezones or len(user_timezones) < 2:
        return []
    
    # Get current time in UTC
    now_utc = datetime.now(pytz.utc)
    
    # Generate time suggestions for the next 7 days
    suggestions = []
    
    for day in range(0, 7):
        current_date = now_utc + timedelta(days=day)
        
        # Try times from 9 AM to 5 PM in 2-hour intervals
        for hour in [9, 11, 14, 16]:
            # Check if this time works for all timezones
            local_times = {}
            feasible = True
            
            for tz_str in user_timezones:
                try:
                    user_timezone = pytz.timezone(tz_str)
                    local_time = current_date.replace(
                        hour=hour, minute=0, second=0, microsecond=0
                    ).astimezone(user_timezone)
                    
                    # Check if this is a reasonable time (between 8 AM and 6 PM)
                    if local_time.hour < 8 or local_time.hour > 18:
                        feasible = False
                        break
                        
                    local_times[tz_str] = local_time
                except pytz.UnknownTimeZoneError:
                    feasible = False
                    break
            
            if feasible:
                # Calculate score based on how ideal the time is (closer to 10 AM or 2 PM is better)
                time_score = 10 - abs(hour - 10) if hour <= 12 else 10 - abs(hour - 14)
                
                suggestions.append({
                    "utc_time": current_date.replace(hour=hour, minute=0),
                    "local_times": local_times,
                    "score": time_score
                })
    
    # Sort by score (highest first) and return top 5
    suggestions.sort(key=lambda x: x["score"], reverse=True)
    return suggestions[:5]

def format_meeting_suggestions(suggestions: List[Dict[str, Any]], user_map: Dict[str, str]) -> str:
    """Format meeting suggestions for display"""
    if not suggestions:
        return "No suitable meeting times found in the next 7 days."
    
    result = "Here are the best meeting times:\n\n"
    
    for i, suggestion in enumerate(suggestions, 1):
        result += f"*Option {i}:* {suggestion['utc_time'].strftime('%A, %B %d at %H:%M UTC')}\n"
        
        for tz_str, local_time in suggestion['local_times'].items():
            user_name = user_map.get(tz_str, tz_str)
            result += f"• {user_name}: {local_time.strftime('%I:%M %p %Z')}\n"
        
        result += "\n"
    
    return result

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
    user_id = session.get('user_id')
    user = query_db('SELECT * FROM users WHERE id = ?', [user_id], one=True)
    
    # Get user's meetings
    meetings = query_db('''
        SELECT m.*, u.name as organizer_name 
        FROM meetings m 
        JOIN users u ON m.organizer_id = u.id 
        WHERE m.id IN (
            SELECT meeting_id FROM meeting_participants WHERE user_id = ?
        )
        ORDER BY m.created_at DESC
    ''', [user_id])
    
    # Get team members in the same workspace
    team_members = query_db('''
        SELECT id, name, timezone, avatar 
        FROM users 
        WHERE workspace_id = ? AND id != ?
        ORDER BY name
    ''', [user['workspace_id'], user_id])
    
    return render_template('dashboard.html', 
                         user=user, 
                         meetings=meetings, 
                         team_members=team_members)

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
    error = request.args.get('error')
    if error:
        print(f"Slack OAuth error: {error}")
        flash(f'Slack authorization failed: {error}')
        return redirect(url_for('index'))

    code = request.args.get('code')
    if not code:
        print("No code received from Slack")
        flash('Slack authentication failed - no code received')
        return redirect(url_for('index'))

    try:
        print(f"Starting OAuth exchange with code: {code[:5]}...")
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
        
        user_data = {
            'email': user_info['user']['email'],
            'name': user_info['user']['name'],
            'slack_id': user_info['user']['id'],
            'avatar': user_info['user'].get('image_512', ''),
            'workspace_id': workspace_id
        }

        # Check if user already exists
        existing_user = query_db('SELECT id FROM users WHERE slack_id = ?', [user_data['slack_id']], one=True)
        
        if existing_user:
            # Update existing user info
            modify_db('''
                UPDATE users 
                SET name = ?, email = ?, avatar = ?, workspace_id = ?
                WHERE slack_id = ?
            ''', [user_data['name'], user_data['email'], user_data['avatar'], user_data['workspace_id'], user_data['slack_id']])
            user_id = existing_user['id']
        else:
            # Get user's timezone from Slack
            try:
                user_profile = user_client.users_info(user=user_data['slack_id'])
                timezone = user_profile['user'].get('tz', 'UTC')
                # Convert Slack timezone format to standard format
                if timezone and timezone in pytz.all_timezones:
                    user_data['timezone'] = timezone
                else:
                    user_data['timezone'] = 'UTC'
            except:
                user_data['timezone'] = 'UTC'
            
            # Create new user
            user_id = modify_db('''
                INSERT INTO users (email, name, timezone, slack_id, avatar, workspace_id) 
                VALUES (?, ?, ?, ?, ?, ?)
            ''', [user_data['email'], user_data['name'], user_data['timezone'], 
                 user_data['slack_id'], user_data['avatar'], user_data['workspace_id']])

        # Set session
        session.permanent = True
        session['user_id'] = user_id
        session['user_name'] = user_data['name']
        session['workspace_id'] = workspace_id
        flash('Successfully connected with Slack!')
        return redirect(url_for('dashboard'))

    except SlackApiError as e:
        error_message = str(e.response['error']) if hasattr(e, 'response') and 'error' in e.response else str(e)
        print(f"Slack API Error: {error_message}")
        flash(f'Error connecting to Slack: {error_message}')
        return redirect(url_for('index'))
    except Exception as e:
        print(f"Unexpected error in Slack callback: {str(e)}")
        flash('An unexpected error occurred during Slack authentication')
        return redirect(url_for('index'))

@app.route('/api/set-timezone', methods=['POST'])
@login_required
def set_timezone():
    """API endpoint to set user timezone"""
    user_id = session.get('user_id')
    timezone = request.json.get('timezone')
    
    if not timezone:
        return jsonify({'error': 'Timezone is required'}), 400
    
    # Validate timezone
    valid_timezone = parse_timezone_input(timezone)
    if not valid_timezone:
        return jsonify({'error': 'Invalid timezone'}), 400
    
    # Update user timezone
    modify_db('UPDATE users SET timezone = ? WHERE id = ?', [valid_timezone, user_id])
    
    return jsonify({'message': 'Timezone updated successfully', 'timezone': valid_timezone})

@app.route('/api/find-meeting-times', methods=['POST'])
@login_required
def find_meeting_times():
    """API endpoint to find meeting times for selected users"""
    user_id = session.get('user_id')
    participant_ids = request.json.get('participants', [])
    duration = request.json.get('duration', 1)
    
    if not participant_ids:
        return jsonify({'error': 'At least one participant is required'}), 400
    
    # Get current user's timezone
    current_user = query_db('SELECT timezone FROM users WHERE id = ?', [user_id], one=True)
    if not current_user or not current_user['timezone']:
        return jsonify({'error': 'Please set your timezone first'}), 400
    
    # Get all participants' timezones
    timezones = [current_user['timezone']]
    user_map = {current_user['timezone']: session.get('user_name', 'You')}
    
    placeholders = ','.join(['?'] * len(participant_ids))
    participants = query_db(f'SELECT id, name, timezone FROM users WHERE id IN ({placeholders})', participant_ids)
    
    for participant in participants:
        if participant['timezone']:
            timezones.append(participant['timezone'])
            user_map[participant['timezone']] = participant['name']
    
    # Remove duplicates
    timezones = list(set(timezones))
    
    # Find best meeting times
    suggestions = find_best_meeting_times(timezones, duration)
    
    # Format response
    result = format_meeting_suggestions(suggestions, user_map)
    
    return jsonify({'suggestions': result})

@app.route('/api/create-meeting', methods=['POST'])
@login_required
def create_meeting():
    """API endpoint to create a new meeting"""
    user_id = session.get('user_id')
    title = request.json.get('title')
    description = request.json.get('description', '')
    participant_ids = request.json.get('participants', [])
    proposed_times = request.json.get('proposed_times', [])
    
    if not title or not participant_ids or not proposed_times:
        return jsonify({'error': 'Title, participants, and proposed times are required'}), 400
    
    # Create meeting
    meeting_id = modify_db('''
        INSERT INTO meetings (organizer_id, title, description, proposed_times) 
        VALUES (?, ?, ?, ?)
    ''', [user_id, title, description, json.dumps(proposed_times)])
    
    # Add participants
    for participant_id in participant_ids:
        # Get participant timezone
        participant = query_db('SELECT timezone FROM users WHERE id = ?', [participant_id], one=True)
        timezone = participant['timezone'] if participant else 'UTC'
        
        modify_db('''
            INSERT INTO meeting_participants (meeting_id, user_id, timezone) 
            VALUES (?, ?, ?)
        ''', [meeting_id, participant_id, timezone])
    
    # Add organizer as participant
    current_user = query_db('SELECT timezone FROM users WHERE id = ?', [user_id], one=True)
    modify_db('''
        INSERT INTO meeting_participants (meeting_id, user_id, timezone) 
        VALUES (?, ?, ?)
    ''', [meeting_id, user_id, current_user['timezone']])
    
    return jsonify({'message': 'Meeting created successfully', 'meeting_id': meeting_id})

@app.route('/slack/command/tz', methods=['POST'])
def handle_tz_command():
    """Handle /tz slash command from Slack"""
    if not request.form:
        return jsonify({'error': 'Invalid request'}), 400
        
    # Verify the request is from Slack
    text = request.form.get('text', '')
    user_id = request.form.get('user_id', '')
    channel_id = request.form.get('channel_id', '')
    
    # Parse the command
    if not text.startswith('find time for'):
        return jsonify({
            'response_type': 'ephemeral',
            'text': 'Please use the format: `/tz find time for @user1 @user2`'
        })
    
    # Extract mentioned users
    mentioned_users = re.findall(r'<@(\w+)>', text)
    if not mentioned_users:
        return jsonify({
            'response_type': 'ephemeral',
            'text': 'Please mention at least one user. Example: `/tz find time for @user1 @user2`'
        })
    
    # Get the command issuer's timezone
    issuer = query_db('SELECT name, timezone FROM users WHERE slack_id = ?', [user_id], one=True)
    if not issuer or not issuer['timezone']:
        return jsonify({
            'response_type': 'ephemeral',
            'text': 'You need to set your timezone first. Visit the TeamZone dashboard to set it.'
        })
    
    user_timezones = [issuer['timezone']]
    user_map = {issuer['timezone']: issuer['name']}
    
    # Get mentioned users' timezones
    for user_slack_id in mentioned_users:
        user = query_db('SELECT name, timezone FROM users WHERE slack_id = ?', [user_slack_id], one=True)
        if not user or not user['timezone']:
            return jsonify({
                'response_type': 'ephemeral',
                'text': f'User <@{user_slack_id}> has not set their timezone yet.'
            })
        user_timezones.append(user['timezone'])
        user_map[user['timezone']] = user['name']
    
    # Remove duplicates
    user_timezones = list(set(user_timezones))
    
    # Find best meeting times
    suggestions = find_best_meeting_times(user_timezones)
    
    # Format the response
    if not suggestions:
        return jsonify({
            'response_type': 'in_channel',
            'text': 'No suitable meeting times found in the next 7 days during working hours (9 AM - 5 PM).'
        })
    
    response_text = format_meeting_suggestions(suggestions, user_map)
    
    return jsonify({
        'response_type': 'in_channel',
        'text': response_text
    })

# Initialize database when the app starts
with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(debug=True)
