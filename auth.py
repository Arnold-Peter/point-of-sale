import hashlib
import sqlite3
from database import get_db, DB
from datetime import datetime

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def verify_user(username, password):
    conn = get_db()
    user = conn.execute('''
        SELECT u.id, u.username, u.full_name, u.role,
               u.branch_id, b.name as branch_name
        FROM users u
        LEFT JOIN branches b ON u.branch_id = b.id
        WHERE u.username=? AND u.password=? AND u.active=1
    ''', (username, hash_password(password))).fetchone()
    conn.close()
    return user

def log_action(user_id, username, action):
    conn = get_db()
    conn.execute('''INSERT INTO login_logs
                    (user_id, username, action, timestamp)
                    VALUES (?,?,?,?)''',
                 (user_id, username, action,
                  datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()