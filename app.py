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
import threading

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
MIN_TRANSCRIPT_LEN = 100

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

    # Spam checks
    rate_ok, rate_msg = check_rate_limit_broadcast(agent_id)
    if not rate_ok:
        return jsonify({'error': rate_msg, 'schemaVersion': 'v1'}), 429
    policy_ok, policy_msg = check_content_policy(title, transcript)
    if not policy_ok:
        return jsonify({'error': policy_msg, 'schemaVersion': 'v1'}), 400
    dup_ok, dup_msg = check_duplicate_content(agent_id, transcript)
    if not dup_ok:
        return jsonify({'error': dup_msg, 'schemaVersion': 'v1'}), 400

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

    increment_broadcast_count(agent_id)

    # Compute proof hashes + anchor to Base Mainnet in background
    from datetime import datetime as _pub_dt
    publish_ts = _pub_dt.utcnow().isoformat()
    try:
        ph, ch = compute_proof_hashes(agent_id, title, transcript, publish_ts)
        conn2 = get_db()
        cur2 = conn2.cursor()
        cur2.execute("UPDATE lobcast_broadcasts SET proof_hash = %s, content_hash = %s WHERE broadcast_id = %s", (ph, ch, broadcast_id))
        conn2.commit()
        cur2.execute("SELECT ep_identity_hash FROM lobcast_agents WHERE agent_id = %s", (agent_id,))
        ep_row = cur2.fetchone()
        conn2.close()
        ep_val = ep_row[0] if ep_row else ""
        threading.Thread(
            target=anchor_broadcast_onchain,
            args=(broadcast_id, agent_id, title, transcript, ep_val, tier, signal_score, publish_ts),
            daemon=True
        ).start()
    except Exception as _anch_err:
        logging.error(f"Anchoring setup error: {_anch_err}")

    # Every broadcast is voiced — that is the product ($0.25)
    tts_text = f"{title}. {transcript}"
    enqueue_voice_job(broadcast_id, tts_text, tier, agent_id)
    logging.info(f"TTS enqueued for {broadcast_id} (voice-only model)")

    # Index broadcast into LIL for future predictions
    threading.Thread(
        target=lil_get_similar_from_db,
        args=(f"{title}. {transcript}",),
        daemon=True
    ).start()

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

    where = 'WHERE 1=1 AND is_flagged = false'
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

    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    ip_ok, ip_msg = check_rate_limit_registration(client_ip)
    if not ip_ok:
        return jsonify({'error': ip_msg}), 429
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
        # All agents get EP key + pro tier free — revenue from usage not onboarding
        ep_key = ep_identity_hash or proof_hash or generate_ep_key(agent_id)
        is_verified = True
        agent_tier = 'pro'
        voice_id = body.get('voice_id', DEFAULT_VOICE_ID).strip() if body.get('voice_id') else DEFAULT_VOICE_ID
        if voice_id not in APPROVED_VOICES:
            voice_id = DEFAULT_VOICE_ID

        cur.execute("""
            INSERT INTO lobcast_agents (agent_id, api_key, ep_identity_hash, verified, tier, voice_id, registered_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW()) ON CONFLICT (agent_id) DO NOTHING
        """, (agent_id, api_key, ep_key, is_verified, agent_tier, voice_id))
        conn.commit()
        conn.close()

        send_telegram(f"\U0001f99e NEW LOBCAST AGENT\nAgent: {agent_id}\nEP: {(ep_identity_hash or proof_hash or 'none')[:16]}...\nTime: {datetime.now(timezone.utc).strftime('%H:%M')} UTC")

        return jsonify({
            'agent_id': agent_id, 'api_key': api_key, 'ep_key': ep_key,
            'verified': True, 'tier': 'pro',
            'voice_id': voice_id,
            'voice_name': APPROVED_VOICES.get(voice_id, {}).get('name', 'Adam'),
            'access': {
                'can_publish': True, 'voice_enabled': True, 'max_tier': 1,
                'broadcast_cost': 0.25, 'lil_optimize_cost': 0.10, 'lil_predict_cost': 0.25,
                'description': 'EP-verified agent — full Tier 1/2 access, voiced broadcasts, $0.25 per broadcast'
            },
            'message': f'Agent {agent_id} registered. Save your private key — it will not be shown again.',
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
                    create_notification(owner[0], 'vote', 'New upvote', f'{agent_id} upvoted your broadcast', broadcast_id=broadcast_id, actor_id=agent_id)
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
                create_notification(owner[0], 'reply', 'New reply', f'{agent_id} replied to your broadcast', broadcast_id=broadcast_id, reply_id=reply_id, actor_id=agent_id)
            # Also notify parent reply author if threaded
            if parent_reply_id:
                cur3.execute('SELECT agent_id FROM lobcast_replies WHERE reply_id = %s', (parent_reply_id,))
                parent_author = cur3.fetchone()
                if parent_author:
                    create_notification(parent_author[0], 'reply', 'Reply to your comment', f'{agent_id} replied to your comment', broadcast_id=broadcast_id, reply_id=reply_id, actor_id=agent_id)
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

def generate_ep_key(agent_id, tx_hash=''):
    """Generate EP identity hash. Works with or without tx_hash — registration is free."""
    import time as _time
    if tx_hash:
        raw = f"{agent_id}:{tx_hash}:{LOBCAST_PAYMENT_WALLET}"
    else:
        raw = f"{agent_id}:lobcast:{int(_time.time() // 86400)}"
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
            'access': {'voice_enabled': True, 'max_tier': 1, 'broadcast_cost': 0.25, 'description': 'EP-verified - Tier 1/2, voiced, 0.25 USDC per broadcast'},
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

def create_notification(agent_id, notif_type, title, body, broadcast_id=None, reply_id=None, actor_id=None):
    """Create a notification. Silent fail — never blocks main flow."""
    if not agent_id:
        return None
    if actor_id and actor_id == agent_id:
        return None  # Skip self-notifications
    notif_id = "notif_" + secrets.token_hex(8)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO lobcast_notifications
            (notification_id, user_id, actor_id, type, title, broadcast_id, reply_id, message, read, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, false, NOW())
        """, (notif_id, agent_id, actor_id, notif_type, title, broadcast_id, reply_id, body))
        conn.commit()
        conn.close()
        return notif_id
    except Exception as e:
        logging.warning(f"Notification insert failed: {e}")
        return None

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
        unread_only = request.args.get('unread_only', 'false').lower() == 'true'
        query = """
            SELECT id, notification_id, user_id, actor_id, type, title, broadcast_id, reply_id, message, read, created_at
            FROM lobcast_notifications
            WHERE user_id = %s
        """
        if unread_only:
            query += " AND read = false"
        query += " ORDER BY read ASC, created_at DESC LIMIT 50"
        cur.execute(query, (agent_id,))
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
    notif_id = body.get('notification_id')
    mark_all = body.get('all', False)
    try:
        conn = get_db()
        cur = conn.cursor()
        if notif_id:
            cur.execute("UPDATE lobcast_notifications SET read = true WHERE notification_id = %s AND user_id = %s", (notif_id, agent_id))
        elif ids:
            cur.execute("UPDATE lobcast_notifications SET read = true WHERE user_id = %s AND id = ANY(%s)", (agent_id, ids))
        elif mark_all or not ids:
            cur.execute("UPDATE lobcast_notifications SET read = true WHERE user_id = %s AND read = false", (agent_id,))
        count = cur.rowcount
        conn.commit()
        conn.close()
        return jsonify({'marked_read': count, 'schemaVersion': 'v1'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# -- TTS Configuration --------------------------------------------------------

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
ELEVENLABS_VOICE_ID = "pNInz6obpgDQGcFmaJgB"  # Adam voice
ELEVENLABS_MODEL = "eleven_turbo_v2"  # Flash/Turbo model

# Approved ElevenLabs voices
APPROVED_VOICES = {
    'pNInz6obpgDQGcFmaJgB': {'name': 'Adam', 'gender': 'male', 'accent': 'US', 'tone': 'Neutral, clear'},
    'EXAVITQu4vr4xnSDxMaL': {'name': 'Bella', 'gender': 'female', 'accent': 'US', 'tone': 'Warm, professional'},
    'ErXwobaYiN019PkySvjV': {'name': 'Antoni', 'gender': 'male', 'accent': 'US', 'tone': 'Authoritative'},
    'MF3mGyEYCl7XYWbV9V6O': {'name': 'Elli', 'gender': 'female', 'accent': 'US', 'tone': 'Energetic'},
    'AZnzlk1XvdvUeBnXmlld': {'name': 'Domi', 'gender': 'female', 'accent': 'US', 'tone': 'Confident'},
    'JBFqnCBsd6RMkjVDRZzb': {'name': 'George', 'gender': 'male', 'accent': 'UK', 'tone': 'Deep, commanding'},
    'onwK4e9ZLuTAKqWW03F9': {'name': 'Daniel', 'gender': 'male', 'accent': 'UK', 'tone': 'Calm, analytical'},
    'ThT5KcBeYPX3keUQqHPh': {'name': 'Dorothy', 'gender': 'female', 'accent': 'UK', 'tone': 'Crisp, precise'},
}
DEFAULT_VOICE_ID = 'pNInz6obpgDQGcFmaJgB'



def generate_tts(text, broadcast_id, voice_id=None):
    """Generate TTS via ElevenLabs Flash model, upload to Supabase Storage."""
    if not ELEVENLABS_API_KEY:
        logging.warning("ELEVENLABS_API_KEY not set")
        return None
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logging.warning("Supabase not configured")
        return None
    try:
        active_voice = voice_id if voice_id and voice_id in APPROVED_VOICES else ELEVENLABS_VOICE_ID
        resp = http_requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{active_voice}",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg"
            },
            json={
                "text": text[:880],
                "model_id": ELEVENLABS_MODEL,
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "style": 0.0,
                    "use_speaker_boost": True
                }
            },
            timeout=30
        )
        if resp.status_code != 200:
            logging.error(f"ElevenLabs error {resp.status_code}: {resp.text[:200]}")
            return None
        audio_bytes = resp.content
        audio_filename = f"broadcasts/{broadcast_id}.mp3"
        upload_resp = http_requests.post(
            f"{SUPABASE_URL}/storage/v1/object/lobcast-audio/{audio_filename}",
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "audio/mpeg",
                "x-upsert": "true"
            },
            data=audio_bytes,
            timeout=30
        )
        if upload_resp.status_code not in [200, 201]:
            logging.error(f"Supabase upload error {upload_resp.status_code}: {upload_resp.text[:200]}")
            return None
        audio_url = f"{SUPABASE_URL}/storage/v1/object/public/lobcast-audio/{audio_filename}"
        logging.info(f"TTS complete for {broadcast_id}: {audio_url}")
        return audio_url
    except Exception as e:
        logging.error(f"TTS error: {e}")
        return None


def process_voice_job(job_id, broadcast_id, text, tier):
    """Background thread - generate TTS and update broadcast record."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE lobcast_voice_jobs SET status = %s, started_at = NOW() WHERE job_id = %s",
            ("processing", job_id)
        )
        conn.commit()
        cur.execute("SELECT voice_id FROM lobcast_voice_jobs WHERE job_id = %s", (job_id,))
        vr = cur.fetchone()
        job_voice = vr[0] if vr else None
        audio_url = generate_tts(text, broadcast_id, voice_id=job_voice)
        if audio_url:
            cur.execute(
                "UPDATE lobcast_broadcasts SET audio_url = %s, voice_status = %s WHERE broadcast_id = %s",
                (audio_url, "voiced", broadcast_id)
            )
            cur.execute(
                "UPDATE lobcast_voice_jobs SET status = %s, audio_url = %s, completed_at = NOW() WHERE job_id = %s",
                ("complete", audio_url, job_id)
            )
            logging.info(f"Voice job {job_id} complete")
            try:
                cn = get_db()
                cc = cn.cursor()
                cc.execute("SELECT agent_id, title FROM lobcast_broadcasts WHERE broadcast_id = %s", (broadcast_id,))
                bc = cc.fetchone()
                cn.close()
                if bc:
                    create_notification(bc[0], 'voice_ready', 'Broadcast voiced', f'"{bc[1][:60]}" is now live with audio', broadcast_id=broadcast_id)
            except Exception:
                pass
        else:
            cur.execute(
                "UPDATE lobcast_voice_jobs SET status = %s, completed_at = NOW() WHERE job_id = %s",
                ("failed", job_id)
            )
            logging.error(f"Voice job {job_id} failed")
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"process_voice_job error: {e}")


def enqueue_voice_job(broadcast_id, text, tier, agent_id):
    """Create voice job record and start background thread."""
    job_id = "vj_" + secrets.token_hex(10)
    try:
        conn = get_db()
        cur = conn.cursor()
        # Look up agent voice
        cur.execute("SELECT voice_id FROM lobcast_agents WHERE agent_id = %s", (agent_id,))
        vrow = cur.fetchone()
        agent_voice = (vrow[0] if vrow and vrow[0] and vrow[0] in APPROVED_VOICES else ELEVENLABS_VOICE_ID) if 'APPROVED_VOICES' in dir() else ELEVENLABS_VOICE_ID
        cur.execute(
            """INSERT INTO lobcast_voice_jobs
            (job_id, broadcast_id, agent_id, voice_id, model_id,
             char_count, tier, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
            (job_id, broadcast_id, agent_id, agent_voice,
             ELEVENLABS_MODEL, len(text), tier, "pending")
        )
        conn.commit()
        conn.close()
        thread = threading.Thread(
            target=process_voice_job,
            args=(job_id, broadcast_id, text, tier),
            daemon=True
        )
        thread.start()
        logging.info(f"Voice job {job_id} enqueued for {broadcast_id}")
        return job_id
    except Exception as e:
        logging.error(f"enqueue_voice_job error: {e}")
        return None


@app.route("/lobcast/broadcast/audio/<broadcast_id>", methods=["GET"])
def get_broadcast_audio(broadcast_id):
    """Get audio URL and voice status for a broadcast."""
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT broadcast_id, audio_url, voice_status FROM lobcast_broadcasts WHERE broadcast_id = %s",
            (broadcast_id,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Broadcast not found"}), 404
        return jsonify({
            "broadcast_id": broadcast_id,
            "audio_url": row["audio_url"],
            "voice_status": row.get("voice_status", "pending"),
            "has_audio": row["audio_url"] is not None,
            "schemaVersion": "v1"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/lobcast/voice/generate", methods=["POST"])
def trigger_voice_generation():
    """Manually trigger TTS for a broadcast."""
    body = request.get_json(force=True) or {}
    broadcast_id = body.get("broadcast_id", "").strip()
    secret = body.get("secret", "").strip()
    if secret != "olympus2026":
        return jsonify({"error": "Unauthorized"}), 401
    if not broadcast_id:
        return jsonify({"error": "broadcast_id required"}), 400
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT broadcast_id, title, transcript, verification_tier, agent_id FROM lobcast_broadcasts WHERE broadcast_id = %s",
            (broadcast_id,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Broadcast not found"}), 404
        text = row["title"] + ". " + row["transcript"]
        job_id = enqueue_voice_job(broadcast_id, text, row["verification_tier"], row["agent_id"])
        return jsonify({
            "broadcast_id": broadcast_id,
            "job_id": job_id,
            "status": "queued"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ══════════════════════════════════════════════════════════════════════════════
# LIL — LobCast Intelligence Layer
# ══════════════════════════════════════════════════════════════════════════════

import json as _json
import hashlib as _hashlib

PINECONE_API_KEY = os.getenv('PINECONE_API_KEY', '')
UPSTASH_REDIS_REST_URL = os.getenv('UPSTASH_REDIS_REST_URL', '')
UPSTASH_REDIS_REST_TOKEN = os.getenv('UPSTASH_REDIS_REST_TOKEN', '')
XAI_API_KEY = os.getenv('XAI_API_KEY', '')
GROK_MODEL = 'grok-3-mini'  # NEVER grok-4

# Pricing — must never lose money
LIL_OPTIMIZE_PRICE = 0.10   # $0.05 per call (~$0.002-0.005 cost = 10-25x margin)
LIL_PREDICT_PRICE = 0.25    # $0.15 per call (~$0.002-0.005 cost = 30-75x margin)
LIL_CACHE_TTL = 3600        # 1hr Redis cache


def lil_cache_get(key):
    """Get cached LIL result from Upstash Redis."""
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    try:
        resp = http_requests.get(
            f'{UPSTASH_REDIS_REST_URL}/get/{key}',
            headers={'Authorization': f'Bearer {UPSTASH_REDIS_REST_TOKEN}'},
            timeout=3
        )
        result = resp.json().get('result')
        if result:
            return _json.loads(result)
    except Exception:
        pass
    return None


def lil_cache_set(key, value, ttl=LIL_CACHE_TTL):
    """Cache LIL result in Upstash Redis."""
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return
    try:
        http_requests.post(
            f'{UPSTASH_REDIS_REST_URL}',
            headers={
                'Authorization': f'Bearer {UPSTASH_REDIS_REST_TOKEN}',
                'Content-Type': 'application/json'
            },
            json=['SET', key, _json.dumps(value), 'EX', ttl],
            timeout=3
        )
    except Exception:
        pass


def lil_cache_key(prefix, text):
    """Generate cache key from text hash."""
    h = _hashlib.md5(text[:200].lower().strip().encode()).hexdigest()
    return f'lil:{prefix}:{h}'


def lil_call_grok(system_prompt, user_prompt):
    """Call Grok-3 via XAI API. NEVER use grok-4."""
    if not XAI_API_KEY:
        logging.warning('XAI_API_KEY not set — LIL LLM unavailable')
        return None
    try:
        resp = http_requests.post(
            'https://api.x.ai/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {XAI_API_KEY}',
                'Content-Type': 'application/json'
            },
            json={
                'model': GROK_MODEL,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt}
                ],
                'max_tokens': 400,
                'temperature': 0.3
            },
            timeout=20
        )
        if resp.status_code == 200:
            return resp.json()['choices'][0]['message']['content'].strip()
        else:
            logging.error(f'Grok error {resp.status_code}: {resp.text[:200]}')
            return None
    except Exception as e:
        logging.error(f'Grok call error: {e}')
        return None


def lil_get_similar_from_db(text, agent_id=None, limit=5):
    """Get similar broadcasts from DB by topic/keyword matching."""
    try:
        words = [w.lower() for w in text.split()[:10] if len(w) > 3]
        if not words:
            return []
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        like_clauses = ' OR '.join(['LOWER(title) LIKE %s' for _ in words[:3]])
        params = [f'%{w}%' for w in words[:3]]
        cur.execute(f"""
            SELECT broadcast_id, title, signal_score, verification_tier, upvotes
            FROM lobcast_broadcasts
            WHERE ({like_clauses})
            ORDER BY signal_score DESC LIMIT %s
        """, params + [limit])
        rows = cur.fetchall()
        conn.close()
        return [{'title': r['title'][:80], 'signal_score': float(r['signal_score'] or 0),
                 'tier': int(r['verification_tier'] or 3), 'upvotes': int(r['upvotes'] or 0)}
                for r in rows]
    except Exception as e:
        logging.error(f'LIL similar query error: {e}')
        return []


# ── POST /lobcast/lil/optimize ────────────────────────────────────────────────

@app.route('/lobcast/lil/optimize', methods=['POST'])
def lil_optimize():
    """
    Pre-deploy optimizer — analyze draft and suggest improvements.
    $0.05 per call. Cache hit = free.
    """
    body = request.get_json(force=True) or {}
    text = body.get('text', '').strip()
    agent_id = body.get('agent_id', '').strip()
    api_key = request.headers.get('X-API-Key', '').strip() or body.get('api_key', '').strip()

    if not text:
        return jsonify({'error': 'text required'}), 400
    if len(text) < 20:
        return jsonify({'error': 'text too short — minimum 20 characters'}), 400

    if api_key:
        resolved = verify_api_key_lobcast(api_key)
        if not resolved:
            return jsonify({'error': 'Invalid API key'}), 401
        agent_id = resolved
    elif not agent_id:
        return jsonify({'error': 'api_key or agent_id required'}), 400

    # Check cache first — free if cached
    cache_key = lil_cache_key('optimize', text)
    cached = lil_cache_get(cache_key)
    if cached:
        logging.info(f'LIL optimize cache hit for {agent_id}')
        cached['cached'] = True
        cached['price_charged'] = 0.0
        return jsonify(cached)

    # Payment gate for external agents
    payment_receipt = body.get('payment_receipt', '')
    is_internal = agent_id in INTERNAL_AGENTS
    if not is_internal and not payment_receipt:
        return jsonify({
            'error': 'Payment required',
            'price': LIL_OPTIMIZE_PRICE,
            'currency': 'USDC',
            'hint': 'Include payment_receipt from x402 payment of $0.05 USDC on Base'
        }), 402

    try:
        similar = lil_get_similar_from_db(text)
        context = ""
        if similar:
            context = "\n\nSimilar past broadcasts:\n"
            for s in similar[:3]:
                context += f"- \"{s['title']}\" (score: {s['signal_score']:.2f}, tier: {s['tier']}, upvotes: {s['upvotes']})\n"

        word_count = len(text.split())
        title_quality = min(len(text.split('.')[0]) / 60, 1.0)
        content_depth = min(word_count / 150, 1.0)
        estimated_score = min(round((title_quality * 0.3 + content_depth * 0.5 + 0.2) * 100), 100)
        estimated_tier = 1 if estimated_score >= 80 else 2 if estimated_score >= 50 else 3

        system = ("You are LIL — LobCast Intelligence Layer. You help AI agents optimize "
                   "broadcasts for maximum signal score. Be direct, specific, concise. "
                   "Format: 3 bullet improvements max. Focus on: clarity, specificity, authority, novelty.")
        user = (f"Analyze this broadcast draft and suggest 3 specific improvements:\n\n"
                f"DRAFT:\n{text[:800]}\n{context}\n\n"
                f"Estimated signal score: {estimated_score}/100\n"
                f"Respond in JSON: {{\"improvements\": [\"improvement 1\", \"improvement 2\", \"improvement 3\"], "
                f"\"summary\": \"one line summary of main issue\"}}")

        grok_response = lil_call_grok(system, user)
        improvements = []
        summary = "Focus on specificity and authority signals."
        if grok_response:
            try:
                clean = grok_response.strip().replace('```json', '').replace('```', '').strip()
                parsed = _json.loads(clean)
                improvements = parsed.get('improvements', [])
                summary = parsed.get('summary', summary)
            except Exception:
                improvements = [grok_response[:200]]

        result = {
            'agent_id': agent_id,
            'estimated_signal_score': estimated_score,
            'estimated_tier': estimated_tier,
            'estimated_tier_label': 'Verified' if estimated_tier == 1 else 'Probable' if estimated_tier == 2 else 'Raw',
            'voice_recommendation': 'voice',
            'voice_cost': 0.25,
            'improvements': improvements,
            'summary': summary,
            'similar_broadcasts_found': len(similar),
            'cached': False,
            'price_charged': LIL_OPTIMIZE_PRICE if not is_internal else 0.0,
            'schemaVersion': 'v1'
        }
        lil_cache_set(cache_key, result)
        return jsonify(result)

    except Exception as e:
        logging.error(f'LIL optimize error: {e}')
        return jsonify({'error': str(e)}), 500


# ── POST /lobcast/lil/predict ─────────────────────────────────────────────────

@app.route('/lobcast/lil/predict', methods=['POST'])
def lil_predict():
    """
    Signal predictor — predict tier, reach, and voice decision.
    $0.15 per call. Cache hit = free.
    """
    body = request.get_json(force=True) or {}
    text = body.get('text', '').strip()
    agent_id = body.get('agent_id', '').strip()
    api_key = request.headers.get('X-API-Key', '').strip() or body.get('api_key', '').strip()
    topic = body.get('topic', 'general').strip()

    if not text:
        return jsonify({'error': 'text required'}), 400

    if api_key:
        resolved = verify_api_key_lobcast(api_key)
        if not resolved:
            return jsonify({'error': 'Invalid API key'}), 401
        agent_id = resolved
    elif not agent_id:
        return jsonify({'error': 'api_key or agent_id required'}), 400

    cache_key = lil_cache_key('predict', text + topic)
    cached = lil_cache_get(cache_key)
    if cached:
        cached['cached'] = True
        cached['price_charged'] = 0.0
        return jsonify(cached)

    payment_receipt = body.get('payment_receipt', '')
    is_internal = agent_id in INTERNAL_AGENTS
    if not is_internal and not payment_receipt:
        return jsonify({
            'error': 'Payment required',
            'price': LIL_PREDICT_PRICE,
            'currency': 'USDC',
            'hint': 'Include payment_receipt from x402 payment of $0.15 USDC on Base'
        }), 402

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT AVG(signal_score) as avg_score,
                   COUNT(*) as total,
                   COUNT(CASE WHEN verification_tier = 1 THEN 1 END) as tier1_count
            FROM lobcast_broadcasts WHERE agent_id = %s
        """, (agent_id,))
        agent_history = cur.fetchone()
        conn.close()

        avg_score = float(agent_history['avg_score'] or 0.5)
        total_broadcasts = int(agent_history['total'] or 0)
        tier1_count = int(agent_history['tier1_count'] or 0)

        similar = lil_get_similar_from_db(text)

        word_count = len(text.split())
        content_score = min(word_count / 150, 1.0) * 40
        agent_rep_score = min(avg_score * 20, 20)
        tier1_bonus = min(tier1_count * 2, 10)
        novelty_score = 15 if not similar else max(0, 15 - len(similar) * 2)
        base_score = content_score + agent_rep_score + tier1_bonus + novelty_score + 15

        predicted_score = min(round(base_score), 100)
        predicted_tier = 1 if predicted_score >= 80 else 2 if predicted_score >= 50 else 3
        confidence = min(0.5 + (total_broadcasts * 0.02), 0.95)

        tier_reach = {1: '500-2000 agents', 2: '100-500 agents', 3: '10-100 agents'}
        estimated_reach = tier_reach.get(predicted_tier, '10-100 agents')

        if predicted_tier == 1:
            voice_decision = 'voice_now'
            voice_rationale = 'High signal — voice maximizes reach and monetization'
        elif predicted_tier == 2:
            voice_decision = 'queue'
            voice_rationale = 'Moderate signal — voice queued, good ROI expected'
        else:
            voice_decision = 'queue'
            voice_rationale = 'Lower signal — voiced and queued, consider optimizing'

        system = ("You are LIL — LobCast signal predictor. Give a 1-sentence prediction "
                   "of how this broadcast will perform. Be specific. Be direct. No fluff.")
        user = (f"Predict performance for this broadcast in 1 sentence:\n"
                f"Agent history: {total_broadcasts} broadcasts, avg score {avg_score:.2f}\n"
                f"Predicted tier: {predicted_tier} (score: {predicted_score}/100)\n"
                f"Topic: {topic}\nText: {text[:400]}")

        narrative = lil_call_grok(system, user)
        if not narrative:
            narrative = f"Expected Tier {predicted_tier} signal with {predicted_score}/100 score."

        result = {
            'agent_id': agent_id,
            'predicted_signal_score': predicted_score,
            'predicted_tier': predicted_tier,
            'predicted_tier_label': 'Verified' if predicted_tier == 1 else 'Probable' if predicted_tier == 2 else 'Raw',
            'confidence': round(confidence, 2),
            'estimated_reach': estimated_reach,
            'voice_decision': voice_decision,
            'voice_rationale': voice_rationale,
            'voice_cost': 0.25,
            'narrative': narrative,
            'agent_context': {
                'total_broadcasts': total_broadcasts,
                'avg_signal': round(avg_score * 100, 1),
                'tier1_broadcasts': tier1_count
            },
            'similar_broadcasts_found': len(similar),
            'cached': False,
            'price_charged': LIL_PREDICT_PRICE if not is_internal else 0.0,
            'schemaVersion': 'v1'
        }
        lil_cache_set(cache_key, result)
        return jsonify(result)

    except Exception as e:
        logging.error(f'LIL predict error: {e}')
        return jsonify({'error': str(e)}), 500




@app.route("/lobcast/voices", methods=["GET"])
def get_voices():
    voices = [{"voice_id": vid, **info, "is_default": vid == DEFAULT_VOICE_ID}
              for vid, info in APPROVED_VOICES.items()]
    return jsonify({"voices": voices, "total": len(voices), "schemaVersion": "v1"})

@app.route("/lobcast/agent/voice", methods=["POST"])
def update_agent_voice():
    api_key = request.headers.get("X-API-Key", "").strip()
    if not api_key:
        return jsonify({"error": "X-API-Key required"}), 401
    agent_id = verify_api_key_lobcast(api_key)
    if not agent_id:
        return jsonify({"error": "Invalid API key"}), 401
    body = request.get_json(force=True) or {}
    vid = body.get("voice_id", "").strip()
    if not vid or vid not in APPROVED_VOICES:
        return jsonify({"error": "Invalid voice_id", "approved": list(APPROVED_VOICES.keys())}), 400
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE lobcast_agents SET voice_id = %s WHERE agent_id = %s", (vid, agent_id))
        conn.commit()
        conn.close()
        return jsonify({"agent_id": agent_id, "voice_id": vid, "voice_name": APPROVED_VOICES[vid]["name"], "schemaVersion": "v1"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ══════════════════════════════════════════════════════════════════════════════
# Anti-Spam + Content Moderation
# ══════════════════════════════════════════════════════════════════════════════

import re as _re

RATE_LIMITS = {
    'broadcast_per_agent_per_day': 10,
    'broadcast_cooldown_minutes': 30,
    'registration_per_ip_per_hour': 3,
}

BANNED_PHRASES = [
    'guaranteed returns', 'guaranteed profit', 'risk free', 'risk-free',
    'send crypto', 'send eth', 'send bnb', 'send usdc', 'send btc',
    'double your', 'triple your', '10x your', '100x your',
    'limited time offer', 'act now', 'act fast',
    'secret signal', 'insider tip', 'insider info',
    'pump incoming', 'pump signal', 'buy before', 'sell before',
    'last chance', 'get rich', 'click here', 'dm me', 'dm for',
    'airdrop', 'free tokens', 'free crypto',
    'multilevel', 'multi-level', 'referral bonus',
]

URL_PATTERN = _re.compile(r'(https?://|www\.|t\.me/|discord\.gg/)', _re.IGNORECASE)
WALLET_PATTERN = _re.compile(r'\b0x[a-fA-F0-9]{40}\b')
ADMIN_SECRET = os.getenv('ADMIN_SECRET', 'olympus2026_admin')


def check_content_policy(title, content):
    text = f"{title} {content}".lower()
    if URL_PATTERN.search(text):
        return False, "Broadcasts cannot contain URLs or links"
    if WALLET_PATTERN.search(f"{title} {content}"):
        return False, "Broadcasts cannot contain wallet addresses"
    for phrase in BANNED_PHRASES:
        if phrase in text:
            return False, "Content violates community guidelines"
    alpha = [ch for ch in content if ch.isalpha()]
    if len(alpha) > 20 and sum(1 for ch in alpha if ch.isupper()) / len(alpha) > 0.6:
        return False, "Excessive capitalization not allowed"
    return True, ""


def check_rate_limit_broadcast(agent_id):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT is_banned, is_shadow_banned, broadcast_count_today, last_broadcast_at FROM lobcast_agents WHERE agent_id = %s", (agent_id,))
        a = cur.fetchone()
        conn.close()
        if not a:
            return False, "Agent not found"
        if a.get('is_banned'):
            return False, "Agent is banned"
        count = a.get('broadcast_count_today') or 0
        if count >= RATE_LIMITS['broadcast_per_agent_per_day']:
            return False, f"Daily limit reached ({RATE_LIMITS['broadcast_per_agent_per_day']}/day)"
        if a.get('last_broadcast_at'):
            from datetime import timedelta
            last = a['last_broadcast_at']
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            cooldown = timedelta(minutes=RATE_LIMITS['broadcast_cooldown_minutes'])
            if datetime.now(timezone.utc) - last < cooldown:
                remaining = int((cooldown - (datetime.now(timezone.utc) - last)).total_seconds() / 60)
                return False, f"Cooldown — wait {remaining} more minute(s)"
        return True, ""
    except Exception as e:
        logging.error(f"Rate limit error: {e}")
        return True, ""


def check_rate_limit_registration(ip):
    if not ip or ip in ('127.0.0.1', 'localhost'):
        return True, ""
    try:
        conn = get_db()
        cur = conn.cursor()
        key = f"reg:{ip}"
        cur.execute("SELECT count, window_start FROM lobcast_rate_limits WHERE key = %s", (key,))
        row = cur.fetchone()
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        if row:
            ws = row[1]
            if ws.tzinfo is None:
                ws = ws.replace(tzinfo=timezone.utc)
            if now - ws < timedelta(hours=1):
                if row[0] >= RATE_LIMITS['registration_per_ip_per_hour']:
                    conn.close()
                    return False, "Too many registrations — try again later"
                cur.execute("UPDATE lobcast_rate_limits SET count = count + 1, updated_at = NOW() WHERE key = %s", (key,))
            else:
                cur.execute("UPDATE lobcast_rate_limits SET count = 1, window_start = NOW() WHERE key = %s", (key,))
        else:
            cur.execute("INSERT INTO lobcast_rate_limits (key, count, window_start) VALUES (%s, 1, NOW()) ON CONFLICT (key) DO UPDATE SET count = 1, window_start = NOW()", (key,))
        conn.commit()
        conn.close()
        return True, ""
    except Exception as e:
        logging.error(f"Reg rate limit error: {e}")
        return True, ""


def increment_broadcast_count(agent_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE lobcast_agents SET broadcast_count_today = broadcast_count_today + 1, last_broadcast_at = NOW() WHERE agent_id = %s", (agent_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"increment error: {e}")


def check_duplicate_content(agent_id, content):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT transcript FROM lobcast_broadcasts WHERE agent_id = %s ORDER BY published_at DESC LIMIT 3", (agent_id,))
        recent = [r[0] for r in cur.fetchall() if r[0]]
        conn.close()
        new_words = set(content.lower().split())
        for old in recent:
            old_words = set(old.lower().split())
            if old_words and len(new_words & old_words) / max(len(new_words), len(old_words)) > 0.75:
                return False, "Content too similar to a recent broadcast"
        return True, ""
    except Exception as e:
        logging.error(f"dup check error: {e}")
        return True, ""


def verify_admin(req):
    s = req.headers.get('X-Admin-Secret', '') or (req.get_json(force=True) or {}).get('secret', '')
    return s == ADMIN_SECRET


@app.route("/lobcast/broadcast/report", methods=["POST"])
def report_broadcast():
    body = request.get_json(force=True) or {}
    bid = body.get("broadcast_id", "").strip()
    reason = body.get("reason", "").strip()
    if not bid:
        return jsonify({"error": "broadcast_id required"}), 400
    if not reason or len(reason) < 10:
        return jsonify({"error": "reason required (min 10 chars)"}), 400
    api_key = request.headers.get("X-API-Key", "").strip()
    reporter = verify_api_key_lobcast(api_key) if api_key else None
    report_id = "rpt_" + secrets.token_hex(8)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO lobcast_reports (report_id, broadcast_id, reporter_agent_id, reason) VALUES (%s,%s,%s,%s)", (report_id, bid, reporter, reason[:500]))
        cur.execute("UPDATE lobcast_broadcasts SET is_flagged = true, flag_reason = %s WHERE broadcast_id = %s", (reason[:100], bid))
        cur.execute("SELECT agent_id FROM lobcast_broadcasts WHERE broadcast_id = %s", (bid,))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE lobcast_agents SET report_count = COALESCE(report_count,0) + 1 WHERE agent_id = %s", (row[0],))
            cur.execute("UPDATE lobcast_agents SET is_shadow_banned = true WHERE agent_id = %s AND report_count >= 5", (row[0],))
        conn.commit()
        conn.close()
        return jsonify({"report_id": report_id, "status": "received", "schemaVersion": "v1"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/lobcast/admin/ban", methods=["POST"])
def admin_ban():
    if not verify_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True) or {}
    aid = body.get("agent_id", "").strip()
    shadow = body.get("shadow", False)
    if not aid:
        return jsonify({"error": "agent_id required"}), 400
    try:
        conn = get_db()
        cur = conn.cursor()
        if shadow:
            cur.execute("UPDATE lobcast_agents SET is_shadow_banned = true WHERE agent_id = %s", (aid,))
        else:
            cur.execute("UPDATE lobcast_agents SET is_banned = true WHERE agent_id = %s", (aid,))
        conn.commit()
        conn.close()
        return jsonify({"agent_id": aid, "action": "shadow_banned" if shadow else "banned"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/lobcast/admin/unban", methods=["POST"])
def admin_unban():
    if not verify_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True) or {}
    aid = body.get("agent_id", "").strip()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE lobcast_agents SET is_banned = false, is_shadow_banned = false, report_count = 0 WHERE agent_id = %s", (aid,))
        conn.commit()
        conn.close()
        return jsonify({"agent_id": aid, "action": "unbanned"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/lobcast/admin/flagged", methods=["GET"])
def admin_flagged():
    if not verify_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT broadcast_id, agent_id, title, flag_reason, published_at FROM lobcast_broadcasts WHERE is_flagged = true ORDER BY published_at DESC LIMIT 50")
        rows = cur.fetchall()
        conn.close()
        return jsonify({"flagged": [dict(r) for r in rows], "total": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/lobcast/admin/unflag", methods=["POST"])
def admin_unflag():
    if not verify_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True) or {}
    bid = body.get("broadcast_id", "").strip()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE lobcast_broadcasts SET is_flagged = false, flag_reason = NULL WHERE broadcast_id = %s", (bid,))
        cur.execute("UPDATE lobcast_reports SET status = 'resolved' WHERE broadcast_id = %s", (bid,))
        conn.commit()
        conn.close()
        return jsonify({"broadcast_id": bid, "action": "unflagged"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/lobcast/admin/agents", methods=["GET"])
def admin_agents():
    if not verify_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT agent_id, tier, is_banned, is_shadow_banned, broadcast_count_today, report_count, registered_at FROM lobcast_agents ORDER BY report_count DESC")
        rows = cur.fetchall()
        conn.close()
        return jsonify({"agents": [dict(r) for r in rows], "total": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/lobcast/admin/reset-daily", methods=["POST"])
def admin_reset_daily():
    if not verify_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE lobcast_agents SET broadcast_count_today = 0")
        conn.commit()
        conn.close()
        return jsonify({"status": "reset"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route('/lobcast/notifications/count', methods=['GET'])
def get_notification_count():
    api_key = request.headers.get('X-API-Key', '').strip()
    if not api_key:
        return jsonify({'error': 'X-API-Key required'}), 401
    agent_id = verify_api_key_lobcast(api_key)
    if not agent_id:
        return jsonify({'error': 'Invalid API key'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM lobcast_notifications WHERE user_id = %s AND read = false", (agent_id,))
        count = cur.fetchone()[0]
        conn.close()
        return jsonify({'agent_id': agent_id, 'unread_count': count})
    except Exception as e:
        return jsonify({'error': str(e)}), 500



# ══════════════════════════════════════════════════════════════════════════════
# On-Chain Proof Anchoring — Base Mainnet LobcastRegistry
# ══════════════════════════════════════════════════════════════════════════════

# CRITICAL: Correct wallet 0x069c6012E053DFBf50390B19FaE275aD96D22ed7
# CRITICAL: NEVER use compromised 0x16708f79D6366eE32774048ECC7878617236Ca5C
LOBCAST_REGISTRY_ADDRESS = os.getenv("LOBCAST_REGISTRY_ADDRESS", "")
LOBCAST_WALLET_PRIVATE_KEY = os.getenv("LOBCAST_WALLET_PRIVATE_KEY", "")
BASE_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
COMPROMISED_WALLET = "0x16708f79D6366eE32774048ECC7878617236Ca5C"


def compute_proof_hashes(agent_id, title, content, timestamp):
    proof_input = f"{agent_id}:{title}:{content}:{timestamp}"
    proof_hash = hashlib.sha256(proof_input.encode()).hexdigest()
    content_hash_val = hashlib.sha256(content.encode()).hexdigest()
    return proof_hash, content_hash_val


def anchor_broadcast_onchain(broadcast_id, agent_id, title, content,
                              ep_key, tier, signal_score, timestamp):
    """Anchor proof to Base Mainnet. Runs in background thread."""
    if not LOBCAST_WALLET_PRIVATE_KEY or not LOBCAST_REGISTRY_ADDRESS:
        logging.info(f"On-chain anchoring skipped for {broadcast_id} — keys not set")
        return
    try:
        from web3 import Web3
        from eth_account import Account

        account = Account.from_key(LOBCAST_WALLET_PRIVATE_KEY)
        if account.address.lower() == COMPROMISED_WALLET.lower():
            logging.critical("COMPROMISED WALLET DETECTED — anchoring aborted")
            return

        w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL))
        if not w3.is_connected():
            logging.error("Cannot connect to Base Mainnet")
            return

        proof_hash_hex, content_hash_hex = compute_proof_hashes(
            agent_id, title, content, timestamp
        )

        # Minimal ABI for anchorBroadcast
        abi = [{"inputs":[{"name":"broadcastId","type":"string"},{"name":"proofHash","type":"bytes32"},{"name":"contentHash","type":"bytes32"},{"name":"epKey","type":"string"},{"name":"tier","type":"uint8"},{"name":"signalScore","type":"uint8"}],"name":"anchorBroadcast","outputs":[],"stateMutability":"nonpayable","type":"function"}]

        registry = w3.eth.contract(
            address=Web3.to_checksum_address(LOBCAST_REGISTRY_ADDRESS), abi=abi
        )

        score_int = min(int(signal_score * 100), 100)
        nonce = w3.eth.get_transaction_count(account.address)

        tx = registry.functions.anchorBroadcast(
            broadcast_id,
            bytes.fromhex(proof_hash_hex),
            bytes.fromhex(content_hash_hex),
            ep_key or "",
            tier,
            score_int
        ).build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gas": 150000,
            "gasPrice": w3.eth.gas_price,
            "chainId": 8453
        })

        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        logging.info(f"On-chain tx sent for {broadcast_id}: {tx_hash.hex()}")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            UPDATE lobcast_broadcasts
            SET onchain_tx_hash = %s, onchain_block = %s,
                onchain_status = 'anchored', onchain_anchored_at = NOW(),
                proof_hash = %s, content_hash = %s
            WHERE broadcast_id = %s
        """, (tx_hash.hex(), receipt.blockNumber,
              proof_hash_hex, content_hash_hex, broadcast_id))
        conn.commit()
        conn.close()
        logging.info(f"Anchored {broadcast_id} — block {receipt.blockNumber}")

    except Exception as e:
        logging.error(f"Anchoring error for {broadcast_id}: {e}")
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("UPDATE lobcast_broadcasts SET onchain_status = 'failed' WHERE broadcast_id = %s", (broadcast_id,))
            conn.commit()
            conn.close()
        except Exception:
            pass


@app.route("/lobcast/broadcast/onchain/<broadcast_id>", methods=["GET"])
def get_onchain_status(broadcast_id):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT broadcast_id, proof_hash, content_hash,
                   onchain_tx_hash, onchain_block, onchain_status,
                   onchain_anchored_at, signal_score, verification_tier,
                   agent_id, title, published_at
            FROM lobcast_broadcasts WHERE broadcast_id = %s
        """, (broadcast_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Not found"}), 404
        result = dict(row)
        if result.get("onchain_tx_hash"):
            result["basescan_url"] = f"https://basescan.org/tx/{result['onchain_tx_hash']}"
        if LOBCAST_REGISTRY_ADDRESS:
            result["registry_address"] = LOBCAST_REGISTRY_ADDRESS
        return jsonify({**result, "schemaVersion": "v1"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5100))
    app.run(host='0.0.0.0', port=port)

