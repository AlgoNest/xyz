from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from datetime import datetime
import os
from dotenv import load_dotenv
import pytz

# Load environment variables
load_dotenv()

app = Flask(__name__)
CORS(app)

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
def slack_oauth_callback():
    code = request.args.get('code')
    client_id = os.getenv('SLACK_CLIENT_ID')
    client_secret = os.getenv('SLACK_CLIENT_SECRET')
    
    try:
        response = slack_client.oauth_v2_access(
            client_id=client_id,
            client_secret=client_secret,
            code=code
        )
        # Here you would typically store the access token securely
        # For demo purposes, we'll just return success
        return jsonify({"status": "success"})
    except SlackApiError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/slack/users", methods=['GET'])
def get_slack_users():
    try:
        result = slack_client.users_list()
        users = []
        for member in result["members"]:
            if not member.get("is_bot") and not member.get("deleted"):
                tz = member.get("tz")
                if tz:
                    current_time = datetime.now(pytz.timezone(tz))
                    users.append({
                        "id": member["id"],
                        "name": member.get("real_name", member["name"]),
                        "email": member.get("profile", {}).get("email"),
                        "timezone": tz,
                        "local_time": current_time.strftime("%I:%M %p"),
                        "avatar": member.get("profile", {}).get("image_72")
                    })
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

if __name__ == "__main__":
    app.run(debug=True)