# Core Flask imports
from flask import Flask, render_template, request, jsonify, abort
from functools import wraps

# Security related imports
from flask_security import Security, SQLAlchemyUserDatastore, \
    UserMixin, RoleMixin, login_required
from flask_security.utils import hash_password, verify_password
from flask_session import Session  # Server-side session management
from flask_wtf.csrf import CSRFProtect  # CSRF protection
from flask_talisman import Talisman  # Security headers
from flask_limiter import Limiter  # Rate limiting
from flask_limiter.util import get_remote_address

# Database and migration
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

# API related
from flask_cors import CORS  # Cross-Origin Resource Sharing

# Other utilities
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from datetime import datetime, timedelta
import os
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
import pytz
import hmac
import hashlib
from werkzeug.exceptions import HTTPException 
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import pytz

# Load environment variables
load_dotenv()

app = Flask(__name__)
CORS(app)

# Slack signing secret for verifying requests
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

# Database Configuration
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'teamzone.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Security Configuration
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', os.urandom(24))
app.config['SESSION_TYPE'] = 'filesystem'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=1)
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Initialize security extensions
Talisman(app, 
    content_security_policy={
        'default-src': "'self'",
        'img-src': "'self' data: https:",
        'script-src': "'self' 'unsafe-inline' 'unsafe-eval'",
        'style-src': "'self' 'unsafe-inline' https:",
    },
    force_https=True
)

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# Initialize database
db = SQLAlchemy(app)
migrate = Migrate(app, db)

# Configure logging
if not app.debug:
    if not os.path.exists('logs'):
        os.mkdir('logs')
    file_handler = RotatingFileHandler('logs/teamzone.log', maxBytes=10240, backupCount=10)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
    ))
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)
    app.logger.info('TeamZone startup')

# Error handlers
@app.errorhandler(HTTPException)
def handle_http_error(error):
    response = {
        "error": {
            "code": error.code,
            "name": error.name,
            "description": error.description,
        }
    }
    return jsonify(response), error.code

@app.errorhandler(Exception)
def handle_exception(error):
    app.logger.error(f'Unhandled exception: {str(error)}')
    response = {
        "error": {
            "code": 500,
            "name": "Internal Server Error",
            "description": "An unexpected error occurred"
        }
    }
    return jsonify(response), 500

# Authentication decorator
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        workspace_id = request.args.get('workspace_id')
        if not workspace_id:
            return jsonify({"error": "Workspace ID is required"}), 401
        
        workspace = Workspace.query.get(workspace_id)
        if not workspace:
            return jsonify({"error": "Invalid workspace"}), 401
        
        return f(*args, **kwargs)
    return decorated

def verify_slack_request(req):
    timestamp = req.headers.get('X-Slack-Request-Timestamp')
    if not timestamp:
        return False
    # Prevent replay attacks: reject requests older than 5 minutes
    if abs(datetime.now().timestamp() - int(timestamp)) > 60 * 5:
        return False
    sig_basestring = f"v0:{timestamp}:{req.get_data(as_text=True)}"
    my_signature = 'v0=' + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    slack_signature = req.headers.get('X-Slack-Signature')
    if not slack_signature:
        return False
    return hmac.compare_digest(my_signature, slack_signature)

# Input validation
def validate_meeting_input(data):
    required_fields = ['title', 'start_time', 'duration']
    for field in required_fields:
        if field not in data:
            raise ValueError(f"Missing required field: {field}")
    
    try:
        datetime.fromisoformat(data['start_time'])
    except ValueError:
        raise ValueError("Invalid start_time format. Use ISO format")
    
    if not isinstance(data['duration'], (int, float)) or data['duration'] <= 0:
        raise ValueError("Duration must be a positive number")

# Models
class Workspace(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slack_team_id = db.Column(db.String(20), unique=True, nullable=False)
    slack_access_token = db.Column(db.String(100), nullable=False)
    name = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    users = db.relationship('User', backref='workspace', lazy=True)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slack_user_id = db.Column(db.String(20), unique=True, nullable=False)
    workspace_id = db.Column(db.Integer, db.ForeignKey('workspace.id'), nullable=False)
    name = db.Column(db.String(100))
    email = db.Column(db.String(120))
    timezone = db.Column(db.String(50))
    avatar_url = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_active = db.Column(db.DateTime)

class TeamGroup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey('workspace.id'), nullable=False)
    name = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    users = db.relationship('User', secondary='team_group_members')

class TeamGroupMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_group_id = db.Column(db.Integer, db.ForeignKey('team_group.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Meeting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey('workspace.id'), nullable=False)
    organizer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    start_time = db.Column(db.DateTime, nullable=False)
    duration = db.Column(db.Integer, nullable=False)  # Duration in minutes
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class MeetingParticipant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey('meeting.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    status = db.Column(db.String(20), default='pending')  # pending, accepted, declined
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    meeting = db.relationship('Meeting', backref=db.backref('participants', lazy=True))

# Initialize Slack client
slack_token = os.getenv('SLACK_BOT_TOKEN')
slack_client = WebClient(token=slack_token)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/slack/install", methods=['GET'])
def slack_install():
    client_id = os.getenv('SLACK_CLIENT_ID')
    scope = "channels:read,chat:write,users:read,users:read.email,team:read"
    return jsonify({
        "url": f"https://slack.com/oauth/v2/authorize?client_id={client_id}&scope={scope}"
    })

@app.route("/api/slack/oauth/callback", methods=['GET'])
@limiter.limit("10/minute")
def slack_oauth_callback():
    error = request.args.get('error')
    if error:
        app.logger.error(f"Slack OAuth error: {error}")
        return jsonify({"error": "Slack OAuth failed", "details": error}), 400

    code = request.args.get('code')
    if not code:
        return jsonify({"error": "No authorization code provided"}), 400

    client_id = os.getenv('SLACK_CLIENT_ID')
    client_secret = os.getenv('SLACK_CLIENT_SECRET')
    
    if not client_id or not client_secret:
        app.logger.error("Missing Slack credentials")
        return jsonify({"error": "Server configuration error"}), 500
    
    try:
        # Get OAuth response
        response = slack_client.oauth_v2_access(
            client_id=client_id,
            client_secret=client_secret,
            code=code
        )
        
        # Get team info
        team_client = WebClient(token=response['access_token'])
        team_info = team_client.team_info()['team']
        
        # Store or update workspace
        workspace = Workspace.query.filter_by(slack_team_id=team_info['id']).first()
        if not workspace:
            workspace = Workspace(
                slack_team_id=team_info['id'],
                slack_access_token=response['access_token'],
                name=team_info['name']
            )
            db.session.add(workspace)
        else:
            workspace.slack_access_token = response['access_token']
            workspace.name = team_info['name']
        
        # Commit changes
        db.session.commit()
        
        return jsonify({"status": "success", "team": team_info['name']})
    except SlackApiError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/slack/users", methods=['GET'])
@limiter.limit("30/minute")
@require_auth
def get_slack_users():
    try:
        workspace_id = request.args.get('workspace_id')
        workspace = Workspace.query.filter_by(id=workspace_id).first()
        
        # This check is redundant due to @require_auth but kept for clarity
        if not workspace:
            return jsonify({"error": "Workspace not found"}), 404

        team_client = WebClient(token=workspace.slack_access_token)
        result = team_client.users_list()
        users = []
        
        for member in result["members"]:
            if not member.get("is_bot") and not member.get("deleted"):
                tz = member.get("tz")
                if tz:
                    current_time = datetime.now(pytz.timezone(tz))
                    
                    # Update or create user in database
                    user = User.query.filter_by(slack_user_id=member["id"]).first()
                    if not user:
                        user = User(
                            slack_user_id=member["id"],
                            workspace_id=workspace.id,
                            name=member.get("real_name", member["name"]),
                            email=member.get("profile", {}).get("email"),
                            timezone=tz,
                            avatar_url=member.get("profile", {}).get("image_72")
                        )
                        db.session.add(user)
                    else:
                        user.name = member.get("real_name", member["name"])
                        user.email = member.get("profile", {}).get("email")
                        user.timezone = tz
                        user.avatar_url = member.get("profile", {}).get("image_72")
                        user.last_active = datetime.utcnow()
                    
                    users.append({
                        "id": member["id"],
                        "name": member.get("real_name", member["name"]),
                        "email": member.get("profile", {}).get("email"),
                        "timezone": tz,
                        "local_time": current_time.strftime("%I:%M %p"),
                        "avatar": member.get("profile", {}).get("image_72")
                    })
        
        # Commit all changes
        db.session.commit()
        return jsonify(users)
    except SlackApiError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/slack/send-message", methods=['POST'])
def send_slack_message():
    data = request.json
    try:
        response = slack_client.chat_postMessage(
            channel=data.get("channel_id"),
            text=data.get("message"),
            blocks=data.get("blocks")
        )
        return jsonify({"status": "success", "ts": response["ts"]})
    except SlackApiError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/meetings", methods=['POST'])
@limiter.limit("10/minute")
@require_auth
def create_meeting():
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400

        try:
            validate_meeting_input(data)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        workspace_id = data.get('workspace_id')
        organizer_id = data.get('organizer_id')
        
        # Validate organizer exists
        organizer = User.query.get(organizer_id)
        if not organizer:
            return jsonify({"error": "Invalid organizer"}), 400
        
        # Create new meeting
        meeting = Meeting(
            workspace_id=workspace_id,
            organizer_id=organizer_id,
            title=data.get('title'),
            description=data.get('description'),
            start_time=datetime.fromisoformat(data.get('start_time')),
            duration=data.get('duration', 30)
        )
        db.session.add(meeting)
        
        # Add participants
        for participant_id in data.get('participants', []):
            participant = MeetingParticipant(
                meeting_id=meeting.id,
                user_id=participant_id
            )
            db.session.add(participant)
        
        db.session.commit()
        
        # Send Slack notifications
        workspace = Workspace.query.get(workspace_id)
        client = WebClient(token=workspace.slack_access_token)
        
        for participant_id in data.get('participants', []):
            user = User.query.get(participant_id)
            local_time = meeting.start_time.astimezone(pytz.timezone(user.timezone))
            
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*New meeting invitation: {meeting.title}*\n"
                               f"When: {local_time.strftime('%B %d, %Y at %I:%M %p')} (your local time)\n"
                               f"Duration: {meeting.duration} minutes\n"
                               f"Description: {meeting.description}"
                    }
                }
            ]
            
            try:
                client.chat_postMessage(
                    channel=user.slack_user_id,
                    text=f"New meeting invitation: {meeting.title}",
                    blocks=blocks
                )
            except SlackApiError:
                pass  # Handle notification failure gracefully
        
        return jsonify({"status": "success", "meeting_id": meeting.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400

@app.route("/api/meetings/<int:workspace_id>", methods=['GET'])
def get_meetings(workspace_id):
    try:
        meetings = Meeting.query.filter_by(workspace_id=workspace_id).all()
        result = []
        
        for meeting in meetings:
            participants = MeetingParticipant.query.filter_by(meeting_id=meeting.id).all()
            participant_details = []
            
            for participant in participants:
                user = User.query.get(participant.user_id)
                local_time = meeting.start_time.astimezone(pytz.timezone(user.timezone))
                participant_details.append({
                    "user_id": user.id,
                    "name": user.name,
                    "timezone": user.timezone,
                    "local_time": local_time.strftime("%I:%M %p"),
                    "status": participant.status
                })
            
            result.append({
                "id": meeting.id,
                "title": meeting.title,
                "description": meeting.description,
                "start_time": meeting.start_time.isoformat(),
                "duration": meeting.duration,
                "participants": participant_details
            })
        
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/meetings/suggest-times", methods=['POST'])
def suggest_meeting_times():
    try:
        data = request.json
        participant_ids = data.get('participant_ids', [])
        duration = data.get('duration', 30)
        
        # Get all participants' timezones
        participants = User.query.filter(User.id.in_(participant_ids)).all()
        if not participants:
            return jsonify({"error": "No valid participants found"}), 400
        
        # Get current time in each timezone and find suitable slots
        now = datetime.utcnow()
        work_hours = range(9, 17)  # 9 AM to 5 PM
        suggestions = []
        
        # Look for slots in the next 5 business days
        for day_offset in range(5):
            check_date = now + timedelta(days=day_offset)
            if check_date.weekday() >= 5:  # Skip weekends
                continue
                
            # Check each hour during work hours
            for hour in work_hours:
                check_time = check_date.replace(hour=hour, minute=0, second=0, microsecond=0)
                suitable = True
                local_times = []
                
                # Check if this time works for all participants
                for participant in participants:
                    tz = pytz.timezone(participant.timezone)
                    local_time = check_time.astimezone(tz)
                    local_hour = local_time.hour
                    
                    # Check if it's during work hours in participant's timezone
                    if local_hour < 9 or local_hour > 17:
                        suitable = False
                        break
                        
                    local_times.append({
                        "user_id": participant.id,
                        "name": participant.name,
                        "local_time": local_time.strftime("%I:%M %p %Z")
                    })
                
                if suitable:
                    suggestions.append({
                        "utc_time": check_time.isoformat(),
                        "participant_times": local_times
                    })
                    
                if len(suggestions) >= 5:  # Limit to 5 suggestions
                    break
            
            if len(suggestions) >= 5:
                break
        
        return jsonify(suggestions)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/slack/command/tz", methods=['POST'])
def slack_tz_command():
    if not verify_slack_request(request):
        abort(400, description="Invalid Slack request signature")
    form_data = request.form
    user_id = form_data.get("user_id")
    text = form_data.get("text", "").strip()
    if not text:
        return jsonify({
            "response_type": "ephemeral",
            "text": "Please provide a command, e.g. `/tz find time for @username and me`"
        })
    # Parse usernames from text
    import re
    user_mentions = re.findall(r"<@([A-Z0-9]+)>", text)
    if user_id not in user_mentions:
        user_mentions.append(user_id)  # Ensure the command user is included
    
    # Fetch users from DB
    participants = User.query.filter(User.slack_user_id.in_(user_mentions)).all()
    if not participants:
        return jsonify({
            "response_type": "ephemeral",
            "text": "No valid users found in the command."
        })
    
    participant_ids = [p.id for p in participants]
    
    # Use existing logic to suggest meeting times
    # Reuse the logic from suggest_meeting_times but here inline for simplicity
    now = datetime.utcnow()
    work_hours = range(9, 17)
    suggestions = []
    for day_offset in range(5):
        check_date = now + timedelta(days=day_offset)
        if check_date.weekday() >= 5:
            continue
        for hour in work_hours:
            check_time = check_date.replace(hour=hour, minute=0, second=0, microsecond=0)
            suitable = True
            local_times = []
            for participant in participants:
                tz = pytz.timezone(participant.timezone)
                local_time = check_time.astimezone(tz)
                local_hour = local_time.hour
                if local_hour < 9 or local_hour > 17:
                    suitable = False
                    break
                local_times.append(f"{participant.name}: {local_time.strftime('%I:%M %p %Z')}")
            if suitable:
                suggestions.append(f"{check_time.strftime('%Y-%m-%d %H:%M UTC')} - " + ", ".join(local_times))
            if len(suggestions) >= 5:
                break
        if len(suggestions) >= 5:
            break
    
    if not suggestions:
        return jsonify({
            "response_type": "ephemeral",
            "text": "No suitable meeting times found in the next 5 business days."
        })
    
    response_text = "Here are some suggested meeting times:\n" + "\n".join(suggestions)
    return jsonify({
        "response_type": "in_channel",
        "text": response_text
    })

if __name__ == '__main__':
    app.run(debug=True, ssl_context='adhoc')
