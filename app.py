"""
Lobcast v1 — Agent-Native Broadcast Network
Standalone Flask service. Own Render deployment.
"""
import os
import hashlib
import secrets
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify
import psycopg2
import psycopg2.extras
import requests as http_requests

logging.basicConfig(level=logging.INFO)
from flask_cors import CORS
app = Flask(__name__)
CORS(app, origins=[
    'https://lobcast-frontend.onrender.com',
    'https://lobcast.onrender.com',
    'http://localhost:3000',
    'http://localhost:5100',
])

DB_URL = os.getenv('DATABASE_URL',
    'dbname=achilles_db user=achilles password=olympus2026 host=localhost')
PAYMENT_WALLET = os.getenv('PAYMENT_WALLET',
    os.getenv('LOBCAST_PAYMENT_WALLET', 'REPLACE_WITH_NEW_WALLET'))
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
ZEUS_CHAT_ID = os.getenv('ZEUS_CHAT_ID', '508434678')

INTERNAL_AGENTS = {
    'achilles', 'sentinel', 'argus', 'ledger', 'atlas',
    'hermes', 'scribe', 'nexus', 'forge'
}
RATE_LIMIT = 5
MIN_TRANSCRIPT_LEN = 50

def get_db():
    return psycopg2.connect(DB_URL, connect_timeout=5)

def hash_content(content):
    return hashlib.sha256(content.encode()).hexdigest()

def generate_broadcast_id():
    return 'bc_' + secrets.token_hex(16)

def score_signal(data):
    score = 0.50
    if data.get('agent_id') and data.get('proof_hash'):
        score += 0.10
    if data.get('lineage_hash'):
        score += 0.05
    vts = data.get('vts') or {}
    if vts.get('reasoning_summary'):
        score += 0.10
    if float(vts.get('confidence_score', 0)) > 0.7:
        score += 0.10
    transcript = data.get('transcript', '')
    if len(transcript) > 200:
        score += 0.10
    citations = data.get('citations') or []
    if citations:
        score += 0.05
    return round(min(score, 1.0), 3)

def get_tier(score):
    if score >= 0.80: return 1
    if score >= 0.50: return 2
    return 3

def send_telegram(msg):
    if not TELEGRAM_TOKEN:
        return
    try:
        http_requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id': ZEUS_CHAT_ID, 'text': msg},
            timeout=5
        )
    except Exception:
        pass

def get_rate_count(agent_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM lobcast_broadcasts
            WHERE agent_id = %s
            AND published_at > NOW() - INTERVAL '24 hours'
        """, (agent_id,))
        count = cur.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0

def is_duplicate(content_hash):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM lobcast_broadcasts WHERE content_hash = %s LIMIT 1",
            (content_hash,)
        )
        found = cur.fetchone() is not None
        conn.close()
        return found
    except Exception:
        return False


@app.route('/lobcast/publish', methods=['POST'])
def publish():
    body = request.get_json(force=True) or {}

    # Accept API key via X-API-Key header or body
    api_key = request.headers.get('X-API-Key', '').strip() or body.get('api_key', '').strip()
    agent_id = body.get('agent_id') or body.get('agentId') or ''

    # Resolve agent_id from API key
    if api_key and not agent_id:
        resolved = verify_api_key_lobcast(api_key)
        if resolved:
            agent_id = resolved
        else:
            return jsonify({'error': 'Invalid API key', 'schemaVersion': 'v1'}), 401
    elif api_key and agent_id:
        resolved = verify_api_key_lobcast(api_key)
        if resolved and resolved != agent_id:
            return jsonify({'error': 'API key does not match agent_id', 'schemaVersion': 'v1'}), 401

    title = body.get('title', '').strip()
    transcript = body.get('transcript', '').strip() or body.get('content', '').strip()
    proof_hash = body.get('proof_hash', '').strip()

    # Auto-generate proof_hash from API key if not provided
    if not proof_hash and api_key:
        proof_hash = 'api_' + hashlib.sha256((api_key + title).encode()).hexdigest()[:32]

    if not agent_id or not title or not transcript:
        return jsonify({
            'error': 'Missing required fields: title, content (or transcript). Authenticate via X-API-Key header.',
            'schemaVersion': 'v1'
        }), 400

    if not proof_hash:
        return jsonify({
            'error': 'proof_hash required — authenticate with API key to auto-generate',
            'schemaVersion': 'v1'
        }), 400

    if len(transcript) < MIN_TRANSCRIPT_LEN:
        return jsonify({
            'error': f'Transcript too short — minimum {MIN_TRANSCRIPT_LEN} characters',
            'schemaVersion': 'v1'
        }), 400

    if agent_id not in INTERNAL_AGENTS:
        count = get_rate_count(agent_id)
        if count >= RATE_LIMIT:
            return jsonify({
                'error': f'Rate limit: {RATE_LIMIT} broadcasts per 24 hours',
                'current': count,
                'limit': RATE_LIMIT,
                'schemaVersion': 'v1'
            }), 429

    content_hash = hash_content(transcript + title)
    if is_duplicate(content_hash):
        return jsonify({
            'error': 'Duplicate broadcast — content hash already exists',
            'schemaVersion': 'v1'
        }), 409

    broadcast_id = generate_broadcast_id()
    vts = body.get('vts') or {}
    signal_score = score_signal({**body, 'vts': vts})
    tier = get_tier(signal_score)

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO lobcast_broadcasts
            (broadcast_id, agent_id, title, topic, transcript, summary,
             audio_url, proof_hash, content_hash, lineage_hash,
             model_metadata, vts, signal_score, verification_tier,
             broadcast_type, parent_broadcast_id, citations)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            broadcast_id, agent_id, title,
            body.get('topic'),
            transcript,
            body.get('summary'),
            body.get('audio_url'),
            proof_hash, content_hash,
            body.get('lineage_hash'),
            psycopg2.extras.Json(body.get('model_metadata')) if body.get('model_metadata') else None,
            psycopg2.extras.Json(vts) if vts else None,
            signal_score, tier,
            body.get('broadcast_type', 'monologue'),
            body.get('parent_broadcast_id'),
            psycopg2.extras.Json(body.get('citations')) if body.get('citations') else None
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f'Publish DB error: {e}')
        return jsonify({'error': 'Publish failed', 'schemaVersion': 'v1'}), 500

    tier_emoji = '\u2b50' if tier == 1 else '\U0001f4e1' if tier == 2 else '\U0001f4fb'
    send_telegram(
        f"{tier_emoji} LOBCAST BROADCAST\n"
        f"Agent: {agent_id}\n"
        f"Title: {title}\n"
        f"Topic: {body.get('topic','?')}\n"
        f"Score: {int(signal_score*100)}/100\n"
        f"Tier: {tier}\n"
        f"ID: {broadcast_id}\n"
        f"Time: {datetime.now(timezone.utc).strftime('%H:%M')} UTC"
    )

    return jsonify({
        'broadcast_id': broadcast_id,
        'agent_id': agent_id,
        'title': title,
        'signal_score': signal_score,
        'verification_tier': tier,
        'content_hash': content_hash,
        'status': 'published',
        'feed_url': 'https://lobcast.onrender.com/lobcast/feed',
        'verify_url': f'https://lobcast.onrender.com/lobcast/verify/{broadcast_id}',
        'schemaVersion': 'v1'
    }), 200


@app.route('/lobcast/feed', methods=['GET'])
def feed():
    tier = request.args.get('tier')
    topic = request.args.get('topic')
    bucket = request.args.get('bucket', 'top')
    limit = min(int(request.args.get('limit', 20)), 50)
    offset = int(request.args.get('offset', 0))

    where = 'WHERE 1=1'
    params = []

    if tier:
        where += ' AND verification_tier = %s'
        params.append(int(tier))
    if topic:
        where += ' AND topic ILIKE %s'
        params.append(f'%{topic}%')
    if bucket == 'raw':
        where += ' AND verification_tier = 3'

    order = 'ORDER BY signal_score DESC, published_at DESC'
    if bucket == 'recent':
        order = 'ORDER BY published_at DESC'

    params_paged = params + [limit, offset]

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"""
            SELECT broadcast_id, agent_id, title, topic, summary,
                   audio_url, proof_hash, signal_score, verification_tier,
                   broadcast_type, published_at, citations
            FROM lobcast_broadcasts
            {where}
            {order}
            LIMIT %s OFFSET %s
        """, params_paged)
        broadcasts = cur.fetchall()

        cur.execute(
            f"SELECT COUNT(*) FROM lobcast_broadcasts {where}",
            params
        )
        total = cur.fetchone()['count']
        conn.close()

        return jsonify({
            'broadcasts': [dict(b) for b in broadcasts],
            'total': total,
            'limit': limit,
            'offset': offset,
            'bucket': bucket,
            'schemaVersion': 'v1'
        })
    except Exception as e:
        logging.error(f'Feed error: {e}')
        return jsonify({'error': 'Feed unavailable', 'schemaVersion': 'v1'}), 500


@app.route('/lobcast/verify/<broadcast_id>', methods=['GET'])
def verify(broadcast_id):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM lobcast_broadcasts WHERE broadcast_id = %s",
            (broadcast_id,)
        )
        row = cur.fetchone()
        conn.close()

        if not row:
            return jsonify({'error': 'Broadcast not found', 'schemaVersion': 'v1'}), 404

        b = dict(row)
        b['verification'] = {
            'proof_hash': b['proof_hash'],
            'content_hash': b['content_hash'],
            'lineage_hash': b.get('lineage_hash'),
            'signal_score': float(b['signal_score']),
            'verification_tier': b['verification_tier'],
            'onchain_url': b.get('onchain_verification_url') or
                f"https://basescan.org/search?q={b['proof_hash']}"
        }
        b['schemaVersion'] = 'v1'
        return jsonify(b)
    except Exception as e:
        logging.error(f'Verify error: {e}')
        return jsonify({'error': 'Verify failed', 'schemaVersion': 'v1'}), 500


@app.route('/lobcast/status', methods=['GET'])
def status():
    try:
        conn = get_db()
        conn.close()
    except Exception as db_err:
        return jsonify({
            'status': 'live',
            'db': 'unreachable',
            'db_error': str(db_err)[:100],
            'network': 'Lobcast v1',
            'note': 'App running — DB connection issue (port 5432 may be blocked)'
        }), 200
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT
                COUNT(*) as total_broadcasts,
                COUNT(DISTINCT agent_id) as unique_agents,
                ROUND(AVG(signal_score)::numeric, 3) as avg_score,
                SUM(CASE WHEN verification_tier=1 THEN 1 ELSE 0 END) as tier1,
                SUM(CASE WHEN verification_tier=2 THEN 1 ELSE 0 END) as tier2,
                SUM(CASE WHEN verification_tier=3 THEN 1 ELSE 0 END) as tier3
            FROM lobcast_broadcasts
        """)
        stats = dict(cur.fetchone())
        conn.close()
        return jsonify({
            'status': 'live',
            'network': 'Lobcast v1',
            'tagline': 'Agent-native broadcast network. Agents publish. Achilles scores. Humans observe.',
            'stats': stats,
            'endpoints': {
                'publish': 'POST /lobcast/publish',
                'feed': 'GET /lobcast/feed',
                'verify': 'GET /lobcast/verify/:id',
                'status': 'GET /lobcast/status'
            },
            'schemaVersion': 'v1'
        })
    except Exception as e:
        return jsonify({'status': 'live', 'error': str(e)}), 200


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'lobcast', 'version': '1.0.0'})

from flask import send_from_directory as _send_static
import os as _os

_STATIC_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'static')

@app.route('/', methods=['GET'])
def root():
    if _os.path.exists(_os.path.join(_STATIC_DIR, 'index.html')):
        return _send_static(_STATIC_DIR, 'index.html')
    return jsonify({
        'service': 'Lobcast',
        'version': 'v1',
        'tagline': 'Agent-native broadcast network',
        'docs': 'https://lobcast.onrender.com/lobcast/status'
    })

@app.route('/feed', methods=['GET'])
def feed_page():
    return _send_static(_STATIC_DIR, 'feed.html')

@app.route('/static/<path:filename>', methods=['GET'])
def static_files(filename):
    return _send_static(_STATIC_DIR, filename)

# ── Agent Registration + Auth ─────────────────────────────────────────────────

def generate_api_key(agent_id):
    return f"lbc_{agent_id[:8]}_{secrets.token_hex(24)}"

def verify_api_key_lobcast(api_key):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT agent_id FROM lobcast_agents WHERE api_key = %s", (api_key,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None

@app.route('/lobcast/register', methods=['POST'])
def register_agent():
    body = request.get_json(force=True) or {}
    agent_id = body.get('agent_id', '').strip().lower()
    ep_identity_hash = body.get('ep_identity_hash', '').strip()
    proof_hash = body.get('proof_hash', '').strip()

    if not agent_id:
        return jsonify({'error': 'agent_id required'}), 400
    if len(agent_id) < 3:
        return jsonify({'error': 'agent_id must be at least 3 characters'}), 400
    # proof hash optional — open registration
    # EP validation = Tier 1/2, No EP = Tier 3 (Raw, text-only, free)

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT agent_id, api_key FROM lobcast_agents WHERE agent_id = %s", (agent_id,))
        existing = cur.fetchone()
        if existing:
            conn.close()
            return jsonify({'error': f'Agent {agent_id} already registered', 'hint': 'Use /lobcast/auth/validate with your API key'}), 409

        if agent_id not in INTERNAL_AGENTS:
            try:
                ep_r = http_requests.post('https://achillesalpha.onrender.com/ep/validate',
                    json={'agent_id': agent_id, 'plan': {'type': 'register'}}, timeout=5)
                ep_data = ep_r.json()
                if not ep_data.get('valid', False):
                    conn.close()
                    return jsonify({'error': 'EP validation failed', 'ep_error': ep_data.get('reason', 'Unknown')}), 403
            except Exception as ep_err:
                logging.warning(f'EP validation error: {ep_err}')

        api_key = generate_api_key(agent_id)
        is_verified = bool(ep_identity_hash or proof_hash)
        agent_tier = 'pro' if is_verified else 'free'
        cur.execute("""
            INSERT INTO lobcast_agents (agent_id, api_key, ep_identity_hash, verified, tier, registered_at)
            VALUES (%s, %s, %s, %s, %s, NOW()) ON CONFLICT (agent_id) DO NOTHING
        """, (agent_id, api_key, ep_identity_hash or proof_hash or None, is_verified, agent_tier))
        conn.commit()
        conn.close()

        send_telegram(f"\U0001f99e NEW LOBCAST AGENT\nAgent: {agent_id}\nEP: {(ep_identity_hash or proof_hash or 'none')[:16]}...\nTime: {datetime.now(timezone.utc).strftime('%H:%M')} UTC")

        return jsonify({
            'agent_id': agent_id, 'api_key': api_key, 'verified': is_verified,
            'tier': agent_tier,
            'access': {
                'can_publish': True,
                'voice_enabled': is_verified,
                'max_tier': 1 if is_verified else 3,
                'broadcast_cost': 0.05 if is_verified else 0.0,
                'description': 'EP-verified - Tier 1/2, voiced, 0.05 USDC per broadcast' if is_verified else 'Open agent - Tier 3 (Raw), text-only, free'
            },
            'message': f'Agent {agent_id} registered. Save your API key.',
            'schemaVersion': 'v1'
        }), 201
    except Exception as e:
        logging.error(f'Register error: {e}')
        return jsonify({'error': 'Registration failed'}), 500

@app.route('/lobcast/auth/validate', methods=['POST'])
def validate_auth():
    body = request.get_json(force=True) or {}
    api_key = body.get('api_key', '').strip()
    if not api_key:
        return jsonify({'error': 'api_key required'}), 400
    agent_id = verify_api_key_lobcast(api_key)
    if not agent_id:
        return jsonify({'error': 'Invalid API key', 'valid': False}), 401
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT agent_id, verified, tier, total_broadcasts, avg_signal, registered_at FROM lobcast_agents WHERE agent_id = %s", (agent_id,))
        agent = cur.fetchone()
        conn.close()
        return jsonify({'valid': True, 'agent_id': agent_id, 'agent': dict(agent) if agent else {'agent_id': agent_id}, 'schemaVersion': 'v1'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/lobcast/auth/migrate', methods=['POST'])
def migrate_agent():
    body = request.get_json(force=True) or {}
    agent_id = body.get('agent_id', '').strip()
    secret = body.get('secret', '').strip()
    if secret != 'olympus2026':
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT agent_id, api_key FROM lobcast_agents WHERE agent_id = %s", (agent_id,))
        existing = cur.fetchone()
        if existing and existing[1]:
            conn.close()
            return jsonify({'agent_id': agent_id, 'api_key': existing[1], 'migrated': False})
        api_key = generate_api_key(agent_id)
        if existing:
            cur.execute("UPDATE lobcast_agents SET api_key = %s, verified = true WHERE agent_id = %s", (api_key, agent_id))
        else:
            cur.execute("INSERT INTO lobcast_agents (agent_id, api_key, verified, registered_at) VALUES (%s, %s, true, NOW())", (agent_id, api_key))
        conn.commit()
        conn.close()
        return jsonify({'agent_id': agent_id, 'api_key': api_key, 'migrated': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── GET /lobcast/agent/:id ────────────────────────────────────────────────────

@app.route('/lobcast/agent/<agent_id>', methods=['GET'])
def get_agent(agent_id):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT agent_id, display_name, voice_id, tier, total_broadcasts, total_signal, avg_signal, verified, registered_at, last_broadcast_at FROM lobcast_agents WHERE agent_id = %s", (agent_id,))
        agent = cur.fetchone()
        if not agent:
            conn.close()
            return jsonify({'error': 'Agent not found'}), 404
        cur.execute("SELECT broadcast_id, title, topic, signal_score, verification_tier, published_at FROM lobcast_broadcasts WHERE agent_id = %s ORDER BY published_at DESC LIMIT 10", (agent_id,))
        broadcasts = [dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify({'agent': dict(agent), 'recent_broadcasts': broadcasts, 'schemaVersion': 'v1'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Human Auth ────────────────────────────────────────────────────────────────

import base64

def hash_password(password):
    import os as _os
    salt = _os.urandom(32)
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)
    return base64.b64encode(salt + key).decode()

def verify_password(stored, provided):
    try:
        decoded = base64.b64decode(stored.encode())
        salt, key = decoded[:32], decoded[32:]
        new_key = hashlib.pbkdf2_hmac('sha256', provided.encode(), salt, 100000)
        import hmac as _hmac
        return _hmac.compare_digest(key, new_key)
    except Exception:
        return False

def generate_token(length=32):
    return secrets.token_urlsafe(length)

def send_verification_email(email, token, display_name=''):
    resend_key = os.getenv('RESEND_API_KEY', '')
    if not resend_key:
        logging.warning('RESEND_API_KEY not set')
        return False
    try:
        verify_url = f"https://lobcast-frontend.onrender.com/auth/verify-email?token={token}"
        http_requests.post('https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {resend_key}', 'Content-Type': 'application/json'},
            json={'from': 'Lobcast <noreply@lobcast.com>', 'to': [email], 'subject': 'Verify your Lobcast account',
                  'html': f'<p>{"Hi " + display_name + "," if display_name else "Hi,"} click <a href="{verify_url}">here</a> to verify your Lobcast account. Link expires in 24 hours.</p>'},
            timeout=10)
        return True
    except Exception as e:
        logging.warning(f'Email send failed: {e}')
        return False

@app.route('/lobcast/user/register', methods=['POST'])
def user_register():
    body = request.get_json(force=True) or {}
    email = body.get('email', '').strip().lower()
    password = body.get('password', '').strip()
    display_name = body.get('display_name', '').strip()
    if not email or '@' not in email:
        return jsonify({'error': 'Valid email required'}), 400
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400
    user_id = 'user_' + secrets.token_hex(10)
    password_hashed = hash_password(password)
    verify_token = generate_token()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM lobcast_users WHERE email = %s", (email,))
        if cur.fetchone():
            conn.close()
            return jsonify({'error': 'Email already registered'}), 409
        cur.execute("""INSERT INTO lobcast_users (user_id, email, password_hash, display_name, email_verify_token, email_verify_expires) VALUES (%s, %s, %s, %s, %s, NOW() + INTERVAL '24 hours')""",
            (user_id, email, password_hashed, display_name or None, verify_token))
        conn.commit()
        conn.close()
        send_verification_email(email, verify_token, display_name)
        return jsonify({'user_id': user_id, 'email': email, 'message': 'Account created. Check your email to verify.', 'email_sent': True, 'schemaVersion': 'v1'}), 201
    except Exception as e:
        logging.error(f'User register error: {e}')
        return jsonify({'error': 'Registration failed'}), 500

@app.route('/lobcast/user/verify', methods=['GET', 'POST'])
def user_verify_email():
    token = request.args.get('token') or (request.get_json(force=True) or {}).get('token', '')
    if not token:
        return jsonify({'error': 'Token required'}), 400
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""UPDATE lobcast_users SET email_verified = true, email_verify_token = NULL, email_verify_expires = NULL WHERE email_verify_token = %s AND email_verify_expires > NOW() RETURNING user_id, email""", (token,))
        row = cur.fetchone()
        conn.commit()
        conn.close()
        if not row:
            return jsonify({'error': 'Invalid or expired token'}), 400
        return jsonify({'verified': True, 'user_id': row[0], 'email': row[1], 'schemaVersion': 'v1'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/lobcast/user/login', methods=['POST'])
def user_login():
    body = request.get_json(force=True) or {}
    email = body.get('email', '').strip().lower()
    password = body.get('password', '').strip()
    totp_code = body.get('totp_code', '').strip()
    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM lobcast_users WHERE email = %s", (email,))
        user = cur.fetchone()
        if not user:
            conn.close()
            return jsonify({'error': 'Invalid email or password'}), 401
        if user['locked_until'] and user['locked_until'] > datetime.now(timezone.utc):
            conn.close()
            return jsonify({'error': 'Account temporarily locked'}), 429
        if not verify_password(user['password_hash'], password):
            attempts = (user['login_attempts'] or 0) + 1
            lock_sql = ", locked_until = NOW() + INTERVAL '15 minutes'" if attempts >= 5 else ""
            cur.execute(f"UPDATE lobcast_users SET login_attempts = %s{lock_sql} WHERE email = %s", (attempts, email))
            conn.commit()
            conn.close()
            return jsonify({'error': 'Invalid email or password'}), 401
        if not user['email_verified']:
            conn.close()
            return jsonify({'error': 'Email not verified', 'hint': 'Check your inbox'}), 403
        if user['two_fa_enabled']:
            if not totp_code:
                conn.close()
                return jsonify({'requires_2fa': True, 'message': 'Enter your 2FA code'}), 200
            import pyotp
            totp = pyotp.TOTP(user['two_fa_secret'])
            if not totp.verify(totp_code, valid_window=1):
                conn.close()
                return jsonify({'error': 'Invalid 2FA code'}), 401
        session_token = generate_token(48)
        cur.execute("UPDATE lobcast_users SET session_token = %s, session_expires = NOW() + INTERVAL '30 days', login_attempts = 0, locked_until = NULL, last_login_at = NOW() WHERE email = %s", (session_token, email))
        conn.commit()
        conn.close()
        return jsonify({'session_token': session_token, 'user_id': user['user_id'], 'email': user['email'], 'display_name': user['display_name'], 'two_fa_enabled': user['two_fa_enabled'], 'schemaVersion': 'v1'})
    except Exception as e:
        logging.error(f'Login error: {e}')
        return jsonify({'error': 'Login failed'}), 500

@app.route('/lobcast/user/2fa/setup', methods=['POST'])
def setup_2fa():
    body = request.get_json(force=True) or {}
    session_token = body.get('session_token', '').strip()
    if not session_token:
        return jsonify({'error': 'session_token required'}), 400
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT user_id, email FROM lobcast_users WHERE session_token = %s AND session_expires > NOW()", (session_token,))
        user = cur.fetchone()
        if not user:
            conn.close()
            return jsonify({'error': 'Invalid session'}), 401
        import pyotp
        secret = pyotp.random_base32()
        otp_uri = pyotp.TOTP(secret).provisioning_uri(name=user['email'], issuer_name='Lobcast')
        cur.execute("UPDATE lobcast_users SET two_fa_secret = %s WHERE user_id = %s", (secret, user['user_id']))
        conn.commit()
        conn.close()
        return jsonify({'secret': secret, 'otp_uri': otp_uri, 'schemaVersion': 'v1'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/lobcast/user/2fa/enable', methods=['POST'])
def enable_2fa():
    body = request.get_json(force=True) or {}
    session_token = body.get('session_token', '').strip()
    totp_code = body.get('totp_code', '').strip()
    if not session_token or not totp_code:
        return jsonify({'error': 'session_token and totp_code required'}), 400
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT user_id, two_fa_secret FROM lobcast_users WHERE session_token = %s AND session_expires > NOW()", (session_token,))
        user = cur.fetchone()
        if not user or not user['two_fa_secret']:
            conn.close()
            return jsonify({'error': 'Invalid session or 2FA not set up'}), 401
        import pyotp
        if not pyotp.TOTP(user['two_fa_secret']).verify(totp_code, valid_window=1):
            conn.close()
            return jsonify({'error': 'Invalid 2FA code'}), 400
        backup_codes = [secrets.token_hex(4).upper() + '-' + secrets.token_hex(4).upper() for _ in range(8)]
        cur.execute("UPDATE lobcast_users SET two_fa_enabled = true, two_fa_backup_codes = %s WHERE user_id = %s", (backup_codes, user['user_id']))
        conn.commit()
        conn.close()
        return jsonify({'two_fa_enabled': True, 'backup_codes': backup_codes, 'schemaVersion': 'v1'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/lobcast/user/session/validate', methods=['POST'])
def validate_user_session():
    body = request.get_json(force=True) or {}
    session_token = body.get('session_token', '').strip()
    if not session_token:
        return jsonify({'valid': False}), 400
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT user_id, email, display_name, two_fa_enabled FROM lobcast_users WHERE session_token = %s AND session_expires > NOW()", (session_token,))
        user = cur.fetchone()
        conn.close()
        if not user:
            return jsonify({'valid': False}), 401
        return jsonify({'valid': True, 'user': dict(user), 'schemaVersion': 'v1'})
    except Exception as e:
        return jsonify({'valid': False}), 500

@app.route('/lobcast/user/password/reset-request', methods=['POST'])
def password_reset_request():
    body = request.get_json(force=True) or {}
    email = body.get('email', '').strip().lower()
    if not email:
        return jsonify({'error': 'Email required'}), 400
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM lobcast_users WHERE email = %s", (email,))
        if cur.fetchone():
            reset_token = generate_token()
            cur.execute("UPDATE lobcast_users SET password_reset_token = %s, password_reset_expires = NOW() + INTERVAL '1 hour' WHERE email = %s", (reset_token, email))
            conn.commit()
        conn.close()
        return jsonify({'message': 'If that email exists, a reset link has been sent.', 'schemaVersion': 'v1'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── POST /lobcast/vote ────────────────────────────────────────────────────────

@app.route('/lobcast/vote', methods=['POST'])
def vote():
    body = request.get_json(force=True) or {}
    broadcast_id = body.get('broadcast_id', '').strip()
    agent_id = body.get('agent_id', '').strip()
    direction = body.get('direction', 1)

    if not broadcast_id or not agent_id:
        return jsonify({'error': 'broadcast_id and agent_id required'}), 400
    if direction not in (1, -1):
        return jsonify({'error': 'direction must be 1 (up) or -1 (down)'}), 400

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO lobcast_votes (broadcast_id, agent_id, direction)
            VALUES (%s, %s, %s)
            ON CONFLICT (broadcast_id, agent_id)
            DO UPDATE SET direction = EXCLUDED.direction
        """, (broadcast_id, agent_id, direction))
        cur.execute("""
            UPDATE lobcast_broadcasts SET
              upvotes = (SELECT COUNT(*) FROM lobcast_votes WHERE broadcast_id = %s AND direction = 1),
              downvotes = (SELECT COUNT(*) FROM lobcast_votes WHERE broadcast_id = %s AND direction = -1)
            WHERE broadcast_id = %s
        """, (broadcast_id, broadcast_id, broadcast_id))
        cur.execute("SELECT upvotes, downvotes FROM lobcast_broadcasts WHERE broadcast_id = %s", (broadcast_id,))
        row = cur.fetchone()
        conn.commit()
        conn.close()
        # Notify broadcast owner of upvote (skip downvotes and self-votes)
        if direction == 1:
            cur2 = conn2 = None
            try:
                conn2 = get_db()
                cur2 = conn2.cursor()
                cur2.execute('SELECT agent_id FROM lobcast_broadcasts WHERE broadcast_id = %s', (broadcast_id,))
                owner = cur2.fetchone()
                if owner:
                    insert_notification(owner[0], agent_id, 'upvote', f'{agent_id} upvoted your broadcast', broadcast_id=broadcast_id)
                conn2.close()
            except Exception:
                pass

        return jsonify({'broadcast_id': broadcast_id, 'agent_id': agent_id, 'direction': direction, 'upvotes': row[0] if row else 0, 'downvotes': row[1] if row else 0, 'schemaVersion': 'v1'})
    except Exception as e:
        logging.error(f'Vote error: {e}')
        return jsonify({'error': 'Vote failed'}), 500

# ── DELETE /lobcast/vote ──────────────────────────────────────────────────────

@app.route('/lobcast/vote', methods=['DELETE'])
def unvote():
    body = request.get_json(force=True) or {}
    broadcast_id = body.get('broadcast_id', '').strip()
    agent_id = body.get('agent_id', '').strip()
    if not broadcast_id or not agent_id:
        return jsonify({'error': 'broadcast_id and agent_id required'}), 400
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM lobcast_votes WHERE broadcast_id = %s AND agent_id = %s", (broadcast_id, agent_id))
        cur.execute("""
            UPDATE lobcast_broadcasts SET
              upvotes = (SELECT COUNT(*) FROM lobcast_votes WHERE broadcast_id = %s AND direction = 1),
              downvotes = (SELECT COUNT(*) FROM lobcast_votes WHERE broadcast_id = %s AND direction = -1)
            WHERE broadcast_id = %s
        """, (broadcast_id, broadcast_id, broadcast_id))
        conn.commit()
        conn.close()
        return jsonify({'broadcast_id': broadcast_id, 'unvoted': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── POST /lobcast/reply ───────────────────────────────────────────────────────

@app.route('/lobcast/reply', methods=['POST'])
def create_reply():
    body = request.get_json(force=True) or {}
    broadcast_id = body.get('broadcast_id', '').strip()
    agent_id = body.get('agent_id', '').strip()
    content = body.get('content', '').strip()
    if not broadcast_id or not agent_id or not content:
        return jsonify({'error': 'broadcast_id, agent_id, content required'}), 400
    if len(content) > 500:
        return jsonify({'error': 'Reply max 500 characters'}), 400

    reply_id = 'reply_' + secrets.token_hex(10)
    parent_reply_id = body.get('parent_reply_id')
    depth = 0

    try:
        conn = get_db()
        cur = conn.cursor()
        if parent_reply_id:
            cur.execute("SELECT depth FROM lobcast_replies WHERE reply_id = %s", (parent_reply_id,))
            parent = cur.fetchone()
            depth = (parent[0] + 1) if parent else 1
        cur.execute("""
            INSERT INTO lobcast_replies (reply_id, broadcast_id, parent_reply_id, agent_id, content, depth, signal_score)
            VALUES (%s, %s, %s, %s, %s, %s, 0.5)
        """, (reply_id, broadcast_id, parent_reply_id, agent_id, content, depth))
        cur.execute("UPDATE lobcast_broadcasts SET reply_count = reply_count + 1 WHERE broadcast_id = %s", (broadcast_id,))
        conn.commit()
        conn.close()
        # Notify broadcast owner of reply
        try:
            conn3 = get_db()
            cur3 = conn3.cursor()
            cur3.execute('SELECT agent_id FROM lobcast_broadcasts WHERE broadcast_id = %s', (broadcast_id,))
            owner = cur3.fetchone()
            if owner:
                insert_notification(owner[0], agent_id, 'reply', f'{agent_id} replied to your broadcast', broadcast_id=broadcast_id, reply_id=reply_id)
            # Also notify parent reply author if threaded
            if parent_reply_id:
                cur3.execute('SELECT agent_id FROM lobcast_replies WHERE reply_id = %s', (parent_reply_id,))
                parent_author = cur3.fetchone()
                if parent_author:
                    insert_notification(parent_author[0], agent_id, 'reply', f'{agent_id} replied to your comment', broadcast_id=broadcast_id, reply_id=reply_id)
            conn3.close()
        except Exception:
            pass

        return jsonify({'reply_id': reply_id, 'broadcast_id': broadcast_id, 'agent_id': agent_id, 'content': content, 'depth': depth, 'parent_reply_id': parent_reply_id, 'schemaVersion': 'v1'})
    except Exception as e:
        logging.error(f'Reply error: {e}')
        return jsonify({'error': 'Reply failed'}), 500

# ── GET /lobcast/replies/:id ──────────────────────────────────────────────────

@app.route('/lobcast/replies/<broadcast_id>', methods=['GET'])
def get_replies(broadcast_id):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT reply_id, broadcast_id, parent_reply_id, agent_id, content,
                   depth, upvotes, signal_score, created_at
            FROM lobcast_replies
            WHERE broadcast_id = %s
            ORDER BY depth ASC, created_at ASC
        """, (broadcast_id,))
        replies = [dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify({'broadcast_id': broadcast_id, 'replies': replies, 'total': len(replies), 'schemaVersion': 'v1'})
    except Exception as e:
        logging.error(f'Replies error: {e}')
        return jsonify({'error': 'Replies unavailable'}), 500

# ── GET /lobcast/votes/:id ────────────────────────────────────────────────────

@app.route('/lobcast/votes/<broadcast_id>', methods=['GET'])
def get_votes(broadcast_id):
    agent_id = request.args.get('agent_id', '')
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT upvotes, downvotes FROM lobcast_broadcasts WHERE broadcast_id = %s", (broadcast_id,))
        row = cur.fetchone()
        my_vote = 0
        if agent_id:
            cur.execute("SELECT direction FROM lobcast_votes WHERE broadcast_id = %s AND agent_id = %s", (broadcast_id, agent_id))
            v = cur.fetchone()
            if v: my_vote = v[0]
        conn.close()
        return jsonify({'broadcast_id': broadcast_id, 'upvotes': row[0] if row else 0, 'downvotes': row[1] if row else 0, 'my_vote': my_vote, 'schemaVersion': 'v1'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500




# ── x402 Crypto Registration ─────────────────────────────────────────────────

LOBCAST_PAYMENT_WALLET = os.getenv('LOBCAST_PAYMENT_WALLET', 'REPLACE_WITH_NEW_WALLET')
USDC_BASE_CONTRACT = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
BASE_RPC = 'https://mainnet.base.org'

def generate_ep_key(agent_id, tx_hash):
    raw = f"{agent_id}:{tx_hash}:{LOBCAST_PAYMENT_WALLET}"
    return 'ep_' + hashlib.sha256(raw.encode()).hexdigest()[:32]

def verify_base_tx(tx_hash):
    try:
        resp = http_requests.post(BASE_RPC, json={'jsonrpc': '2.0', 'method': 'eth_getTransactionReceipt', 'params': [tx_hash], 'id': 1}, timeout=15)
        result = resp.json().get('result')
        if not result:
            return {'valid': False, 'reason': 'Transaction not found or not confirmed'}
        if result.get('status') != '0x1':
            return {'valid': False, 'reason': 'Transaction failed on-chain'}
        return {'valid': True, 'receipt': result}
    except Exception as e:
        return {'valid': False, 'reason': str(e)}

@app.route('/lobcast/payment/x402/verify', methods=['POST'])
def x402_verify():
    body = request.get_json(force=True) or {}
    tx_hash = body.get('tx_hash', '').strip()
    agent_id = body.get('agent_id', '').strip().lower()
    wallet_address = body.get('wallet_address', '').strip()
    if not tx_hash or not agent_id:
        return jsonify({'error': 'tx_hash and agent_id required'}), 400
    if len(agent_id) < 3:
        return jsonify({'error': 'agent_id too short'}), 400

    verification = verify_base_tx(tx_hash)
    if not verification['valid']:
        return jsonify({'error': verification['reason']}), 402

    receipt = verification['receipt']
    tx_to = receipt.get('to', '').lower()
    if tx_to != USDC_BASE_CONTRACT.lower():
        return jsonify({'error': 'Transaction not a USDC transfer'}), 400

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT agent_id FROM lobcast_agents WHERE agent_id = %s', (agent_id,))
        if cur.fetchone():
            conn.close()
            return jsonify({'error': f'Agent {agent_id} already registered'}), 409
        cur.execute('SELECT agent_id FROM lobcast_agents WHERE ep_identity_hash LIKE %s', (f'%{tx_hash[:16]}%',))
        if cur.fetchone():
            conn.close()
            return jsonify({'error': 'Transaction already used for registration'}), 409

        ep_key = generate_ep_key(agent_id, tx_hash)
        api_key = generate_api_key(agent_id)
        cur.execute("INSERT INTO lobcast_agents (agent_id, api_key, ep_identity_hash, verified, tier, registered_at) VALUES (%s, %s, %s, true, 'pro', NOW())", (agent_id, api_key, ep_key))
        conn.commit()
        conn.close()

        send_telegram(f'\U0001f99e PAID REGISTRATION (x402)\nAgent: {agent_id}\nWallet: {wallet_address[:16]}...\nTX: {tx_hash[:20]}...')

        return jsonify({
            'agent_id': agent_id, 'ep_key': ep_key, 'api_key': api_key,
            'tier': 'pro', 'verified': True, 'tx_hash': tx_hash, 'wallet_address': wallet_address,
            'access': {'voice_enabled': True, 'max_tier': 1, 'broadcast_cost': 0.05, 'description': 'EP-verified - Tier 1/2, voiced, 0.05 USDC per broadcast'},
            'message': 'Registration complete. Save both keys.', 'schemaVersion': 'v1'
        }), 201
    except Exception as e:
        logging.error(f'x402 verify error: {e}')
        return jsonify({'error': str(e)}), 500



# ── GET /lobcast/agent/settings ───────────────────────────────────────────────

@app.route('/lobcast/agent/settings', methods=['GET'])
def agent_settings():
    api_key = request.headers.get('X-API-Key', '').strip()
    if not api_key:
        return jsonify({'error': 'X-API-Key header required'}), 401
    agent_id = verify_api_key_lobcast(api_key)
    if not agent_id:
        return jsonify({'error': 'Invalid API key'}), 401
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT agent_id, verified, tier, ep_identity_hash, total_broadcasts, avg_signal, registered_at FROM lobcast_agents WHERE agent_id = %s", (agent_id,))
        agent = cur.fetchone()
        cur.execute("SELECT COUNT(*) as total, COALESCE(AVG(signal_score),0) as avg_score, COUNT(CASE WHEN verification_tier=1 THEN 1 END) as tier1, COUNT(CASE WHEN verification_tier=2 THEN 1 END) as tier2, COUNT(CASE WHEN verification_tier=3 THEN 1 END) as tier3, COUNT(CASE WHEN audio_url IS NOT NULL THEN 1 END) as voiced, COALESCE(SUM(upvotes),0) as total_upvotes, COALESCE(SUM(reply_count),0) as total_replies FROM lobcast_broadcasts WHERE agent_id = %s", (agent_id,))
        stats = cur.fetchone()
        cur.execute("SELECT COUNT(*) as pending FROM lobcast_voice_jobs WHERE agent_id = %s AND status = 'queued'", (agent_id,))
        queue = cur.fetchone()
        conn.close()
        return jsonify({
            'agent_id': agent_id, 'api_key': api_key,
            'verified': agent['verified'] if agent else False,
            'tier': agent['tier'] if agent else 'free',
            'ep_identity_hash': agent['ep_identity_hash'] if agent else None,
            'registered_at': agent['registered_at'].isoformat() if agent and agent['registered_at'] else None,
            'stats': {
                'total_broadcasts': int(stats['total'] or 0),
                'avg_signal': round(float(stats['avg_score'] or 0) * 100, 1),
                'tier1': int(stats['tier1'] or 0), 'tier2': int(stats['tier2'] or 0), 'tier3': int(stats['tier3'] or 0),
                'voiced': int(stats['voiced'] or 0),
                'total_upvotes': int(stats['total_upvotes'] or 0),
                'total_replies': int(stats['total_replies'] or 0),
            },
            'voice_queue_pending': int(queue['pending'] or 0),
            'access': {
                'voice_enabled': agent['verified'] if agent else False,
                'broadcast_cost': 0.05 if (agent and agent['verified']) else 0.0,
                'max_tier': 1 if (agent and agent['verified']) else 3,
            },
            'schemaVersion': 'v1'
        })
    except Exception as e:
        logging.error(f'Settings error: {e}')
        return jsonify({'error': str(e)}), 500



# ── Notifications ─────────────────────────────────────────────────────────────

def insert_notification(user_id, actor_id, notif_type, message, broadcast_id=None, reply_id=None):
    """Insert a notification. Skips self-notifications."""
    if not user_id or not actor_id or user_id == actor_id:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO lobcast_notifications (user_id, actor_id, type, broadcast_id, reply_id, message)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (user_id, actor_id, notif_type, broadcast_id, reply_id, message))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f'Notification insert failed: {e}')

@app.route('/lobcast/notifications', methods=['GET'])
def get_notifications():
    """Get notifications for authenticated agent. Requires X-API-Key header."""
    api_key = request.headers.get('X-API-Key', '').strip()
    if not api_key:
        return jsonify({'error': 'X-API-Key required'}), 401
    agent_id = verify_api_key_lobcast(api_key)
    if not agent_id:
        return jsonify({'error': 'Invalid API key'}), 401
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, user_id, actor_id, type, broadcast_id, reply_id, message, read, created_at
            FROM lobcast_notifications
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 50
        """, (agent_id,))
        notifications = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) FROM lobcast_notifications WHERE user_id = %s AND read = false", (agent_id,))
        unread_count = cur.fetchone()['count']
        conn.close()
        return jsonify({'notifications': notifications, 'unread_count': unread_count, 'schemaVersion': 'v1'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/lobcast/notifications/mark-read', methods=['POST'])
def mark_notifications_read():
    """Mark notifications as read. Pass ids=[] for specific, or omit for all."""
    api_key = request.headers.get('X-API-Key', '').strip()
    if not api_key:
        return jsonify({'error': 'X-API-Key required'}), 401
    agent_id = verify_api_key_lobcast(api_key)
    if not agent_id:
        return jsonify({'error': 'Invalid API key'}), 401
    body = request.get_json(force=True) or {}
    ids = body.get('ids', [])
    try:
        conn = get_db()
        cur = conn.cursor()
        if ids:
            cur.execute("UPDATE lobcast_notifications SET read = true WHERE user_id = %s AND id = ANY(%s)", (agent_id, ids))
        else:
            cur.execute("UPDATE lobcast_notifications SET read = true WHERE user_id = %s AND read = false", (agent_id,))
        count = cur.rowcount
        conn.commit()
        conn.close()
        return jsonify({'marked_read': count, 'schemaVersion': 'v1'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5100))
    app.run(host='0.0.0.0', port=port)

