from flask import Flask, render_template, request, url_for, send_from_directory, make_response
from flask_socketio import SocketIO, emit, join_room
import os
import secrets
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
from collections import deque
import threading
import atexit
import re
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key')
app.config['UPLOAD_FOLDER'] = 'uploads'
socketio = SocketIO(app, cors_allowed_origins="*")

# Constants
GENERAL_ROOM = 'general'
MAX_MESSAGES = 50  # Keep only 50 messages after cleanup
CLEANUP_INTERVAL = 600  # 10 minutes in seconds
ROOM_ID_LENGTH = 16
MAX_RETRIES = 5

# Store active users and chat rooms
users = {}
chat_rooms = {}
room_messages = {}
user_counter = 0

# Initialize general room with deque
room_messages[GENERAL_ROOM] = deque(maxlen=MAX_MESSAGES * 2)  # Double size for buffer

def cleanup_old_messages():
    """Periodic cleanup of messages keeping only the latest 50"""
    while True:
        # Sleep for 10 minutes
        threading.Event().wait(CLEANUP_INTERVAL)
        
        try:
            # Clean general room messages
            if len(room_messages[GENERAL_ROOM]) > MAX_MESSAGES:
                # Keep only the latest 50 messages
                temp_messages = list(room_messages[GENERAL_ROOM])[-MAX_MESSAGES:]
                room_messages[GENERAL_ROOM].clear()
                room_messages[GENERAL_ROOM].extend(temp_messages)

            # Clean private room messages
            for room_id in list(room_messages.keys()):
                if room_id != GENERAL_ROOM and room_id in chat_rooms:
                    messages = room_messages[room_id]
                    if len(messages) > MAX_MESSAGES:
                        room_messages[room_id] = messages[-MAX_MESSAGES:]
        except Exception as e:
            print(f"Error during message cleanup: {e}")

# Start the cleanup thread when the app starts
cleanup_thread = threading.Thread(target=cleanup_old_messages, daemon=True)
cleanup_thread.start()

# Ensure upload folder exists
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

class ChatRoom:
    def __init__(self, creator_sid, room_name):
        self.id = self._generate_unique_id()
        self.creator_sid = creator_sid
        self.room_name = self._sanitize_room_name(room_name)
        self.members = set()
        self.max_members = 10
        self.created_at = datetime.now()
        self.expires_at = self.created_at + timedelta(hours=24)  # Rooms expire after 24 hours

    def _generate_unique_id(self):
        """Generate a unique room ID with retries"""
        for _ in range(MAX_RETRIES):
            new_id = secrets.token_urlsafe(ROOM_ID_LENGTH)
            if new_id not in chat_rooms:
                return new_id
        raise Exception("Failed to generate unique room ID")

    def _sanitize_room_name(self, name):
        """Sanitize room name to prevent XSS and invalid characters"""
        # Remove any non-alphanumeric characters except spaces and common punctuation
        sanitized = re.sub(r'[^a-zA-Z0-9\s\-_.,!?]', '', name)
        return sanitized.strip()[:30]  # Limit length to 30 characters

    def add_member(self, sid):
        if len(self.members) < self.max_members:
            self.members.add(sid)
            return True
        return False

    def remove_member(self, sid):
        self.members.discard(sid)

def cleanup_room(room_id):
    if room_id in chat_rooms:
        # Clear room messages
        if room_id in room_messages:
            del room_messages[room_id]
        # Delete room
        del chat_rooms[room_id]
        # Clean up any uploaded files for this room
        cleanup_room_files(room_id)

def cleanup_room_files(room_id):
    # Implement file cleanup if needed
    pass

def cleanup_expired_rooms():
    """Remove expired rooms"""
    current_time = datetime.now()
    expired_rooms = [
        room_id for room_id, room in chat_rooms.items()
        if current_time > room.expires_at
    ]
    for room_id in expired_rooms:
        cleanup_room(room_id)

@app.route('/')
def index():
    return render_template('room_selection.html')

@app.route('/general')
def general_chat():
    return render_template('chat.html', room_id=GENERAL_ROOM)

@app.route('/create-room')
def create_room():
    return render_template('create_room.html')

@app.route('/join/<room_id>')
def join_chat(room_id):
    if room_id == GENERAL_ROOM or room_id in chat_rooms:
        return render_template('chat.html', 
                             room_id=room_id, 
                             chat_rooms=chat_rooms)
    return "Invalid or expired chat room link", 404

@socketio.on('create_room')
def handle_create_room(data):
    try:
        room_name = data.get('room_name', '').strip()
        if not room_name:
            emit('room_error', {'message': 'Room name cannot be empty'})
            return

        # Check if user already has an active room
        for room in chat_rooms.values():
            if room.creator_sid == request.sid:
                emit('room_error', {'message': 'You already have an active room'})
                return

        # Create new room with retry mechanism
        try:
            new_room = ChatRoom(request.sid, room_name)
        except Exception as e:
            emit('room_error', {'message': 'Failed to create room. Please try again.'})
            return

        # Clean up expired rooms
        cleanup_expired_rooms()

        # Add new room and join it
        chat_rooms[new_room.id] = new_room
        join_room(new_room.id)
        new_room.add_member(request.sid)
        
        # Send room link back to creator
        room_link = url_for('join_chat', room_id=new_room.id, _external=True)
        emit('room_created', {
            'room_id': new_room.id,
            'room_link': room_link,
            'room_name': new_room.room_name,
            'expires_at': new_room.expires_at.isoformat()
        })

        # Send initial user count (1 for creator)
        emit('user_joined', {
            'user': 'You',
            'count': 1
        }, room=new_room.id)

    except Exception as e:
        print(f"Error creating room: {e}")
        emit('room_error', {'message': 'An error occurred while creating the room'})

@socketio.on('join_room')
def handle_join_room(data):
    room_id = data['room_id']
    
    if room_id == GENERAL_ROOM:
        join_room(GENERAL_ROOM)
        global user_counter
        user_counter += 1
        users[request.sid] = f"Anonymous{user_counter}"
        
        # Send existing messages from general room
        for msg in list(room_messages[GENERAL_ROOM]):  # Convert deque to list
            is_self = msg['user_id'] == request.sid
            emit('message', {
                'user': 'You' if is_self else 'Anonymous',
                'message': msg['message'],
                'type': msg['type'],
                'isSelf': is_self,
                'timestamp': msg['timestamp']
            })
        
        emit('user_joined', {
            'user': 'Anonymous',
            'count': len([u for u in users.values() if u])
        }, room=GENERAL_ROOM)
        
    elif room_id in chat_rooms:
        room = chat_rooms[room_id]
        if room.add_member(request.sid):
            join_room(room_id)
            user_counter += 1
            users[request.sid] = f"Anonymous{user_counter}"
            
            # Send existing room messages to new member
            if room_id in room_messages:
                for msg in room_messages[room_id]:
                    is_self = msg['user_id'] == request.sid
                    emit('message', {
                        'user': 'You' if is_self else 'Anonymous',
                        'message': msg['message'],
                        'type': msg['type'],
                        'isSelf': is_self,
                        'timestamp': msg['timestamp']
                    })
            
            emit('user_joined', {
                'user': 'Anonymous',
                'count': len(room.members)
            }, room=room_id)
            
            emit('join_success', {
                'room_id': room_id,
                'count': len(room.members)
            })
        else:
            emit('join_error', {'message': 'Room is full'})
    else:
        emit('join_error', {'message': 'Invalid room'})

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in users:
        user = users[request.sid]
        del users[request.sid]
        
        # Remove from all rooms except general
        for room_id, room in list(chat_rooms.items()):
            if request.sid in room.members:
                room.remove_member(request.sid)
                emit('user_left', {
                    'user': 'Anonymous',
                    'count': len(room.members)
                }, room=room_id)
                
                # Delete private room if empty
                if not room.members:
                    del chat_rooms[room_id]
                    if room_id in room_messages:
                        del room_messages[room_id]
        
        # Update general room user count
        emit('user_left', {
            'user': 'Anonymous',
            'count': len([u for u in users.values() if u])
        }, room=GENERAL_ROOM)

@socketio.on('message')
def handle_message(data):
    room_id = data.get('room_id')
    if (room_id == GENERAL_ROOM) or (room_id in chat_rooms and request.sid in chat_rooms[room_id].members):
        message_type = data.get('type', 'text')
        timestamp = datetime.now().strftime('%H:%M')
        
        # Create message object
        message_data = {
            'message': data['message'],
            'type': message_type,
            'timestamp': timestamp,
            'user_id': request.sid,
            'created_at': datetime.now()  # Add creation timestamp
        }
        
        # Store message in room history
        if room_id == GENERAL_ROOM:
            room_messages[GENERAL_ROOM].append(message_data)
        else:
            if room_id not in room_messages:
                room_messages[room_id] = []
            room_messages[room_id].append(message_data)
        
        # Send to sender
        emit('message', {
            'user': 'You',
            'message': data['message'],
            'type': message_type,
            'isSelf': True,
            'timestamp': timestamp
        })
        
        # Send to others in the room
        emit('message', {
            'user': 'Anonymous',
            'message': data['message'],
            'type': message_type,
            'isSelf': False,
            'timestamp': timestamp
        }, room=room_id, include_self=False)

@socketio.on('file_upload')
def handle_file_upload(data):
    user = users[request.sid]
    filename = secure_filename(data['filename'])
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    with open(file_path, 'wb') as f:
        f.write(data['file_data'])
    
    file_url = url_for('uploaded_file', filename=filename)
    # Send to sender with "You" as username
    emit('file_shared', {
        'user': 'You',
        'filename': filename,
        'file_url': file_url,
        'isSelf': True
    })
    # Send to everyone else with "Anonymous" as username
    emit('file_shared', {
        'user': 'Anonymous',
        'filename': filename,
        'file_url': file_url,
        'isSelf': False
    }, broadcast=True, include_self=False)

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/quit')
def quit_chat():
    response = make_response("""
        <script>
            window.opener = null;
            window.open('', '_self');
            window.close();
            setTimeout(function() {
                window.location.replace('/');
            }, 100);
        </script>
        <p>Closing chat... If the window doesn't close automatically, you can close it manually.</p>
    """)
    # Clear any session data
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

@app.after_request
def after_request(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

# Add this to properly shutdown the cleanup thread
def cleanup_on_shutdown():
    print("Shutting down cleanup thread...")
    if cleanup_thread.is_alive():
        cleanup_thread.join(timeout=1)

# Register the cleanup function
atexit.register(cleanup_on_shutdown)

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)

