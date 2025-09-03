from flask import Flask, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import sqlite3
import os
import pytz
from functools import wraps
import secrets

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
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            timezone TEXT
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

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        if not email or not password:
            flash('Please fill in all fields')
            return redirect(url_for('login'))
        
        user = query_db('SELECT * FROM users WHERE email = ?', [email], one=True)
        
        if user and check_password_hash(user['password_hash'], password):
            session.permanent = True  # Use permanent session
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            return redirect(url_for('dashboard'))
        
        flash('Invalid email or password')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        name = request.form.get('name')
        timezone = request.form.get('timezone')
        
        if not all([email, password, name, timezone]):
            flash('Please fill in all fields')
            return redirect(url_for('register'))
        
        if query_db('SELECT id FROM users WHERE email = ?', [email], one=True):
            flash('Email already registered')
            return redirect(url_for('register'))
        
        password_hash = generate_password_hash(password)
        user_id = modify_db(
            'INSERT INTO users (email, password_hash, name, timezone) VALUES (?, ?, ?, ?)',
            [email, password_hash, name, timezone]
        )
        
        session.permanent = True  # Use permanent session
        session['user_id'] = user_id
        session['user_name'] = name
        return redirect(url_for('dashboard'))
    
    timezones = [
        "US/Pacific", "US/Mountain", "US/Central", "US/Eastern",
        "Europe/London", "Europe/Paris", "Asia/Dubai", "Asia/Singapore",
        "Australia/Sydney"
    ]
    return render_template('register.html', timezones=timezones)

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

# Initialize database when the app starts
with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(debug=True)