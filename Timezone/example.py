from flask import Flask, render_template, request, jsonify
from werkzeug.exceptions import HTTPException

# Create Flask app
app = Flask(__name__)

# Sample data
users = [
    {"id": 1, "name": "John Doe"},
    {"id": 2, "name": "Jane Smith"}
]

# Route that renders HTML template
@app.route('/')
def home():
    return render_template('index.html', users=users)

# Route that handles JSON data
@app.route('/api/users', methods=['GET', 'POST'])
def handle_users():
    if request.method == 'GET':
        return jsonify(users)
    
    elif request.method == 'POST':
        new_user = request.json
        if not new_user or 'name' not in new_user:
            return jsonify({"error": "Name is required"}), 400
        
        users.append({
            "id": len(users) + 1,
            "name": new_user['name']
        })
        return jsonify({"message": "User added successfully"})

# Error handler for all HTTP exceptions
@app.errorhandler(HTTPException)
def handle_http_error(error):
    return jsonify({
        "error": {
            "code": error.code,
            "name": error.name,
            "description": error.description
        }
    }), error.code

if __name__ == '__main__':
    app.run(debug=True)
