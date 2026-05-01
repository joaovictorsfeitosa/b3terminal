from flask import Flask, jsonify, render_template, request, g
from flask_cors import CORS
import yfinance as yf
import feedparser
import re
import threading
import time
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta
import calendar
import sqlite3
import os
import hashlib
import hmac
import secrets
import json
import requests
from functools import wraps
from collections import defaultdict

app = Flask(__name__)
CORS(app)

# ─── SESSION YFINANCE (evita bloqueio do Yahoo Finance em servidores) ──────────
_yf_session = requests.Session()
_yf_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
})

# ─── CONFIGURAÇÃO ─────────────────────────────────────────────────────────────
SECRET_KEY   = os.environ.get("SECRET_KEY", secrets.token_hex(32))
ADMIN_USER   = os.environ.get("ADMIN_USER",  "admin")
ADMIN_PASS   = os.environ.get("ADMIN_PASS",  "B3Terminal@Admin2025!")
DB_PATH      = os.environ.get("DB_PATH",     "b3terminal.db")
TOKEN_TTL    = 60 * 60 * 24 * 7   # 7 dias

# ─── RATE LIMITER ─────────────────────────────────────────────────────────────
_rl_lock    = threading.Lock()
_rl_buckets = defaultdict(list)
RL_WINDOW   = 900
RL_MAX      = 10

def rate_limit_ok(ip):
    now = time.time()
    with _rl_lock:
        ts = _rl_buckets[ip]
        ts[:] = [t for t in ts if now - t < RL_WINDOW]
        if len(ts) >= RL_MAX:
            return False
        ts.append(now)
    return True

# ─── BANCO DE DADOS ────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    with app.app_context():
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT    NOT NULL UNIQUE COLLATE NOCASE,
                pass_hash  TEXT    NOT NULL,
                pass_salt  TEXT    NOT NULL,
                created_at TEXT    NOT NULL DEFAULT (datetime('now')),
                is_admin   INTEGER NOT NULL DEFAULT 0,
                is_blocked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash  TEXT    NOT NULL UNIQUE,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                expires_at  TEXT    NOT NULL,
                last_seen   TEXT    NOT NULL DEFAULT (datetime('now')),
                device_info TEXT    NOT NULL DEFAULT '{}',
                is_revoked  INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS devices (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                fingerprint TEXT    NOT NULL,
                browser     TEXT,
                os          TEXT,
                ip          TEXT,
                first_seen  TEXT    NOT NULL DEFAULT (datetime('now')),
                last_seen   TEXT    NOT NULL DEFAULT (datetime('now')),
                is_blocked  INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, fingerprint)
            );
            CREATE TABLE IF NOT EXISTS user_data (
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                key        TEXT    NOT NULL,
                value      TEXT    NOT NULL DEFAULT '{}',
                updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, key)
            );
            CREATE TABLE IF NOT EXISTS login_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
                username   TEXT,
                ip         TEXT,
                success    INTEGER NOT NULL,
                reason     TEXT,
                created_at TEXT    NOT NULL DEFAULT (datetime('now'))
            );
        """)
        db.commit()
        row = db.execute("SELECT id FROM users WHERE username=?", (ADMIN_USER,)).fetchone()
        if not row:
            salt = secrets.token_hex(32)
            ph   = _hash_password(ADMIN_PASS, salt)
            db.execute("INSERT INTO users(username,pass_hash,pass_salt,is_admin) VALUES(?,?,?,1)",
                       (ADMIN_USER, ph, salt))
            db.commit()
            print(f"  Admin criado: {ADMIN_USER}")
        db.close()

# ─── SENHA & TOKENS ────────────────────────────────────────────────────────────
def _hash_password(password, salt):
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310000)
    return dk.hex()

def _verify_password(password, salt, stored_hash):
    return hmac.compare_digest(_hash_password(password, salt), stored_hash)

def _create_token(user_id):
    raw = secrets.token_urlsafe(48)
    h   = hashlib.sha256(raw.encode()).hexdigest()
    return raw, h

def _hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()

# ─── AUTH DECORATOR ────────────────────────────────────────────────────────────
def require_auth(admin=False):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            raw = request.headers.get("Authorization", "").strip()
            if raw.startswith("Bearer "):
                raw = raw[7:]
            if not raw:
                return jsonify({"error": "Não autenticado"}), 401
            db   = get_db()
            h    = _hash_token(raw)
            sess = db.execute("""
                SELECT s.*, u.id as uid, u.username, u.is_admin, u.is_blocked
                FROM sessions s JOIN users u ON s.user_id=u.id
                WHERE s.token_hash=? AND s.is_revoked=0
                  AND datetime('now') < s.expires_at
            """, (h,)).fetchone()
            if not sess:
                return jsonify({"error": "Token inválido ou expirado"}), 401
            if sess["is_blocked"]:
                return jsonify({"error": "Conta bloqueada"}), 403
            if admin and not sess["is_admin"]:
                return jsonify({"error": "Acesso restrito"}), 403
            db.execute("UPDATE sessions SET last_seen=datetime('now') WHERE token_hash=?", (h,))
            db.commit()
            g.user = dict(sess)
            return f(*args, **kwargs)
        return wrapper
    return decorator

# ─── HELPERS DEVICE ───────────────────────────────────────────────────────────
def _parse_ua(ua):
    browser = "Desconhecido"
    os_name = "Desconhecido"
    if "Chrome" in ua and "Edg" not in ua and "OPR" not in ua:
        browser = "Chrome"
    elif "Firefox" in ua:
        browser = "Firefox"
    elif "Safari" in ua and "Chrome" not in ua:
        browser = "Safari"
    elif "Edg" in ua:
        browser = "Edge"
    elif "OPR" in ua or "Opera" in ua:
        browser = "Opera"
    if "Windows" in ua:
        os_name = "Windows"
    elif "Macintosh" in ua or "Mac OS" in ua:
        os_name = "macOS"
    elif "iPhone" in ua or "iPad" in ua:
        os_name = "iOS"
    elif "Android" in ua:
        os_name = "Android"
    elif "Linux" in ua:
        os_name = "Linux"
    return {"browser": browser, "os": os_name}

def _get_ip():
    return (request.headers.get("X-Forwarded-For") or
            request.headers.get("X-Real-IP") or
            request.remote_addr or "?")

def _register_device(db, user_id, fp, ua, ip):
    if not fp:
        return
    parsed  = _parse_ua(ua)
    now_str = datetime.utcnow().isoformat()
    db.execute("""
        INSERT INTO devices(user_id,fingerprint,browser,os,ip,first_seen,last_seen)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(user_id,fingerprint) DO UPDATE SET
          last_seen=excluded.last_seen, browser=excluded.browser,
          os=excluded.os, ip=excluded.ip
    """, (user_id, fp, parsed["browser"], parsed["os"], ip, now_str, now_str))

# ─── CACHE ────────────────────────────────────────────────────────────────────
_cache = {}
_lock  = threading.Lock()

def cache_get(key, ttl=90):
    with _lock:
        if key in _cache:
            data, ts = _cache[key]
            if time.time() - ts < ttl:
                return data
    return None

def cache_set(key, data):
    with _lock:
        _cache[key] = (data, time.time())

# ─── HELPERS YFINANCE ─────────────────────────────────────────────────────────
def b3_symbol(sym):
    sym = sym.upper().strip()
    if sym.startswith("^") or "=X" in sym or "-USD" in sym:
        return sym
    return sym if sym.endswith(".SA") else sym + ".SA"

def parse_ticker(ticker, sym_original, _retry=0):
    try:
        info  = ticker.info or {}
        price = (info.get("currentPrice") or info.get("regularMarketPrice") or info.get("navPrice"))
        prev  = (info.get("regularMarketPreviousClose") or info.get("previousClose"))
        open_p   = info.get("regularMarketOpen") or info.get("open")
        day_low  = info.get("regularMarketDayLow")  or info.get("dayLow")
        day_high = info.get("regularMarketDayHigh") or info.get("dayHigh")
        volume   = info.get("regularMarketVolume")  or info.get("volume")
        if not price:
            try:
                hist = ticker.history(period="5d")
                if not hist.empty:
                    price    = float(hist["Close"].iloc[-1])
                    day_low  = float(hist["Low"].iloc[-1])
                    day_high = float(hist["High"].iloc[-1])
                    volume   = int(hist["Volume"].iloc[-1])
                    if len(hist) >= 2 and not prev:
                        prev = float(hist["Close"].iloc[-2])
            except: pass
        if not price:
            return None
        change     = round(price - prev, 4) if price and prev else None
        change_pct = round((change / prev) * 100, 4) if change and prev else None
        dy = info.get("dividendYield")
        if dy: dy = round(dy * 100, 4)
        return {
            "symbol":                     sym_original.upper(),
            "shortName":                  info.get("shortName") or info.get("longName") or sym_original,
            "regularMarketPrice":         round(float(price), 2),
            "regularMarketChange":        round(change, 2)     if change     is not None else None,
            "regularMarketChangePercent": change_pct,
            "regularMarketOpen":          round(float(open_p), 2)  if open_p   else None,
            "regularMarketDayLow":        round(float(day_low), 2) if day_low  else None,
            "regularMarketDayHigh":       round(float(day_high),2) if day_high else None,
            "regularMarketVolume":        int(volume)               if volume   else None,
            "fiftyTwoWeekLow":            round(float(info.get("fiftyTwoWeekLow")), 2)  if info.get("fiftyTwoWeekLow")  else None,
            "fiftyTwoWeekHigh":           round(float(info.get("fiftyTwoWeekHigh")),2)  if info.get("fiftyTwoWeekHigh") else None,
            "dividendYield":              dy,
            "dividendRate":               info.get("dividendRate"),
            "exDividendDate":             info.get("exDividendDate"),
            "lastDividendValue":          info.get("lastDividendValue"),
            "lastDividendDate":           info.get("lastDividendDate"),
            "marketCap":                  info.get("marketCap"),
        }
    except Exception as e:
        msg = str(e)
        print(f"  parse_ticker({sym_original}): {e}")
        if "Too Many Requests" in msg and _retry < 2:
            time.sleep(2 + _retry * 3)
            return parse_ticker(ticker, sym_original, _retry + 1)
        return None

def get_dividend_history(ticker, sym_original, cotas=1):
    try:
        divs = ticker.dividends
        if divs is None or len(divs) == 0:
            return None
        cutoff = datetime.now() - timedelta(days=730)
        divs   = divs[divs.index >= cutoff.strftime("%Y-%m-%d")]
        if len(divs) == 0:
            return None
        payments = []
        for dt, val in divs.items():
            if hasattr(dt, 'to_pydatetime'):
                dt = dt.to_pydatetime()
            payments.append({"year": dt.year, "month": dt.month, "day": dt.day,
                             "value": round(float(val), 6), "date_str": dt.strftime("%d/%m/%Y")})
        if not payments:
            return None
        payments.sort(key=lambda x: (x["year"], x["month"]))
        months_paid = sorted(set(p["month"] for p in payments[-24:]))
        avg_value   = round(sum(p["value"] for p in payments[-12:]) / max(len(payments[-12:]), 1), 6)
        last_val    = payments[-1]["value"]
        n_months = len(months_paid)
        if n_months >= 10:
            freq_label = "Mensal"; freq_months = list(range(1, 13))
        elif n_months >= 4:
            freq_label = "Trimestral"; freq_months = months_paid if len(months_paid) >= 4 else [3,6,9,12]
        elif n_months >= 2:
            freq_label = "Semestral"; freq_months = months_paid if len(months_paid) >= 2 else [6,12]
        else:
            freq_label = "Anual"; freq_months = months_paid if months_paid else [12]
        today = date.today()
        projected = []
        for i in range(13):
            future = today + relativedelta(months=i)
            if future.month in freq_months:
                hfm = [p for p in payments if p["month"] == future.month]
                avg_day = int(sum(p["day"] for p in hfm) / len(hfm)) if hfm else 15
                avg_day = min(avg_day, calendar.monthrange(future.year, future.month)[1])
                pay_date = date(future.year, future.month, avg_day)
                if pay_date >= today:
                    projected.append({"date_str": pay_date.strftime("%d/%m/%Y"),
                                      "month_name": pay_date.strftime("%b/%Y"),
                                      "value_cota": round(last_val, 6),
                                      "value_total": round(last_val * cotas, 2),
                                      "is_next": len(projected) == 0})
        return {"sym": sym_original.upper(), "freq_label": freq_label, "freq_months": freq_months,
                "avg_value": avg_value, "last_value": last_val, "months_paid": months_paid,
                "history": payments[-12:], "projected": projected[:12]}
    except Exception as e:
        print(f"  dividend_history({sym_original}): {e}")
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# AUTENTICAÇÃO
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    ip   = _get_ip()
    if not rate_limit_ok(ip):
        return jsonify({"error": "Muitas tentativas. Aguarde 15 minutos."}), 429
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    if not username or not password:
        return jsonify({"error": "Usuário e senha obrigatórios"}), 400
    if len(username) < 3 or len(username) > 30:
        return jsonify({"error": "Usuário: 3–30 caracteres"}), 400
    if not re.match(r"^[a-zA-Z0-9_\-\.]+$", username):
        return jsonify({"error": "Usuário: apenas letras, números, _ - ."}), 400
    if len(password) < 6:
        return jsonify({"error": "Senha mínima: 6 caracteres"}), 400
    db  = get_db()
    row = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if row:
        return jsonify({"error": "Usuário já existe"}), 409
    salt = secrets.token_hex(32)
    ph   = _hash_password(password, salt)
    db.execute("INSERT INTO users(username,pass_hash,pass_salt) VALUES(?,?,?)", (username, ph, salt))
    db.commit()
    return jsonify({"ok": True, "message": "Conta criada com sucesso"}), 201

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    ip   = _get_ip()
    if not rate_limit_ok(ip):
        return jsonify({"error": "Muitas tentativas. Aguarde 15 minutos."}), 429
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    fp       = (body.get("fp")       or "").strip()
    ua       = request.headers.get("User-Agent", "")
    if not username or not password:
        return jsonify({"error": "Usuário e senha obrigatórios"}), 400
    db  = get_db()
    usr = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not usr or not _verify_password(password, usr["pass_salt"], usr["pass_hash"]):
        db.execute("INSERT INTO login_log(username,ip,success,reason) VALUES(?,?,0,'bad_credentials')", (username, ip))
        db.commit()
        return jsonify({"error": "Usuário ou senha incorretos"}), 401
    if usr["is_blocked"]:
        db.execute("INSERT INTO login_log(user_id,username,ip,success,reason) VALUES(?,?,?,0,'blocked')", (usr["id"], username, ip))
        db.commit()
        return jsonify({"error": "Conta bloqueada pelo administrador"}), 403
    _register_device(db, usr["id"], fp, ua, ip)
    raw, h   = _create_token(usr["id"])
    exp_str  = (datetime.utcnow() + timedelta(seconds=TOKEN_TTL)).isoformat()
    dev_info = json.dumps({"browser": _parse_ua(ua)["browser"], "os": _parse_ua(ua)["os"], "ip": ip, "fp": fp})
    db.execute("INSERT INTO sessions(user_id,token_hash,expires_at,device_info) VALUES(?,?,?,?)",
               (usr["id"], h, exp_str, dev_info))
    db.execute("INSERT INTO login_log(user_id,username,ip,success) VALUES(?,?,?,1)", (usr["id"], username, ip))
    db.commit()
    return jsonify({"ok": True, "token": raw, "username": usr["username"],
                    "is_admin": bool(usr["is_admin"]), "expires": exp_str})

@app.route("/api/auth/logout", methods=["POST"])
@require_auth()
def auth_logout():
    raw = request.headers.get("Authorization", "").strip()
    if raw.startswith("Bearer "): raw = raw[7:]
    db  = get_db()
    db.execute("UPDATE sessions SET is_revoked=1 WHERE token_hash=?", (_hash_token(raw),))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/auth/me")
@require_auth()
def auth_me():
    u = g.user
    return jsonify({"username": u["username"], "is_admin": bool(u["is_admin"])})

@app.route("/api/auth/change-password", methods=["POST"])
@require_auth()
def auth_change_password():
    body     = request.get_json(silent=True) or {}
    old_pass = (body.get("old_password") or "").strip()
    new_pass = (body.get("new_password") or "").strip()
    if not old_pass or not new_pass:
        return jsonify({"error": "Campos obrigatórios"}), 400
    if len(new_pass) < 6:
        return jsonify({"error": "Senha mínima: 6 caracteres"}), 400
    db  = get_db()
    uid = g.user["uid"]
    usr = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not _verify_password(old_pass, usr["pass_salt"], usr["pass_hash"]):
        return jsonify({"error": "Senha atual incorreta"}), 401
    salt = secrets.token_hex(32)
    ph   = _hash_password(new_pass, salt)
    db.execute("UPDATE users SET pass_hash=?,pass_salt=? WHERE id=?", (ph, salt, uid))
    db.execute("UPDATE sessions SET is_revoked=1 WHERE user_id=?", (uid,))
    db.commit()
    return jsonify({"ok": True, "message": "Senha alterada. Faça login novamente."})

# ─── DADOS SINCRONIZADOS ──────────────────────────────────────────────────────
@app.route("/api/user/data/<key>", methods=["GET"])
@require_auth()
def user_data_get(key):
    db  = get_db()
    uid = g.user["uid"]
    row = db.execute("SELECT value FROM user_data WHERE user_id=? AND key=?", (uid, key)).fetchone()
    return jsonify({"value": json.loads(row["value"]) if row else None})

@app.route("/api/user/data/<key>", methods=["POST"])
@require_auth()
def user_data_set(key):
    body  = request.get_json(silent=True)
    value = body.get("value") if body else None
    if value is None:
        return jsonify({"error": "value obrigatório"}), 400
    db  = get_db()
    uid = g.user["uid"]
    db.execute("""
        INSERT INTO user_data(user_id,key,value,updated_at) VALUES(?,?,?,datetime('now'))
        ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
    """, (uid, key, json.dumps(value)))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/user/devices")
@require_auth()
def user_devices():
    db   = get_db()
    uid  = g.user["uid"]
    rows = db.execute("""
        SELECT id,fingerprint,browser,os,ip,first_seen,last_seen,is_blocked
        FROM devices WHERE user_id=? ORDER BY last_seen DESC
    """, (uid,)).fetchall()
    return jsonify([dict(r) for r in rows])

# ─── PAINEL ADMIN ─────────────────────────────────────────────────────────────
@app.route("/api/admin/stats")
@require_auth(admin=True)
def admin_stats():
    db            = get_db()
    total_users   = db.execute("SELECT COUNT(*) FROM users WHERE is_admin=0").fetchone()[0]
    total_devices = db.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    active_sess   = db.execute("SELECT COUNT(*) FROM sessions WHERE is_revoked=0 AND datetime('now') < expires_at").fetchone()[0]
    logins_today  = db.execute("SELECT COUNT(*) FROM login_log WHERE success=1 AND date(created_at)=date('now')").fetchone()[0]
    recent_logins = db.execute("SELECT username,ip,success,reason,created_at FROM login_log ORDER BY created_at DESC LIMIT 20").fetchall()
    return jsonify({
        "total_users":     total_users,
        "total_devices":   total_devices,
        "active_sessions": active_sess,
        "logins_today":    logins_today,
        "recent_logins":   [dict(r) for r in recent_logins],
    })

@app.route("/api/admin/users")
@require_auth(admin=True)
def admin_users():
    db   = get_db()
    rows = db.execute("""
        SELECT u.id, u.username, u.created_at, u.is_blocked, u.is_admin,
               COUNT(DISTINCT d.id) as device_count,
               MAX(d.last_seen) as last_active
        FROM users u LEFT JOIN devices d ON d.user_id=u.id
        GROUP BY u.id ORDER BY u.created_at DESC
    """).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/devices")
@require_auth(admin=True)
def admin_devices():
    db   = get_db()
    rows = db.execute("""
        SELECT d.id, d.fingerprint, d.browser, d.os, d.ip,
               d.first_seen, d.last_seen, d.is_blocked, u.username, u.id as user_id
        FROM devices d JOIN users u ON d.user_id=u.id
        ORDER BY d.last_seen DESC LIMIT 200
    """).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/device/<int:dev_id>/block", methods=["POST"])
@require_auth(admin=True)
def admin_block_device(dev_id):
    body    = request.get_json(silent=True) or {}
    blocked = int(bool(body.get("blocked", True)))
    db      = get_db()
    db.execute("UPDATE devices SET is_blocked=? WHERE id=?", (blocked, dev_id))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/admin/user/<int:user_id>/block", methods=["POST"])
@require_auth(admin=True)
def admin_block_user(user_id):
    body    = request.get_json(silent=True) or {}
    blocked = int(bool(body.get("blocked", True)))
    db      = get_db()
    db.execute("UPDATE users SET is_blocked=? WHERE id=?", (blocked, user_id))
    if blocked:
        db.execute("UPDATE sessions SET is_revoked=1 WHERE user_id=?", (user_id,))
    db.commit()
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════════════════════════════════
# ROTAS ORIGINAIS B3
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/quotes")
def get_quotes():
    symbols_raw = request.args.get("symbols", "")
    if not symbols_raw:
        return jsonify({"error": "symbols obrigatório"}), 400
    symbols   = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
    cache_key = "quotes_" + "_".join(sorted(symbols))
    cached    = cache_get(cache_key, ttl=300)
    if cached:
        return jsonify(cached)
    results = []
    for sym in symbols:
        try:
            t    = yf.Ticker(b3_symbol(sym), session=_yf_session)
            data = parse_ticker(t, sym)
            if data: results.append(data)
        except Exception as e:
            print(f"  [{sym}] erro: {e}")
    cache_set(cache_key, results)
    return jsonify(results)

@app.route("/api/search/<symbol>")
def search_symbol(symbol):
    sym       = symbol.upper().strip()
    cache_key = f"search_{sym}"
    cached    = cache_get(cache_key, ttl=300)
    if cached: return jsonify(cached)
    try:
        t      = yf.Ticker(b3_symbol(sym), session=_yf_session)
        data   = parse_ticker(t, sym)
        result = {"found": True, "data": data} if data and data.get("regularMarketPrice") else {"found": False}
        cache_set(cache_key, result)
        return jsonify(result)
    except:
        return jsonify({"found": False})

@app.route("/api/index/<path:symbol>")
def get_index(symbol):
    sym       = symbol.upper()
    cache_key = f"idx_{sym}"
    cached    = cache_get(cache_key, ttl=300)
    if cached: return jsonify(cached)
    try:
        t    = yf.Ticker(sym, session=_yf_session)
        data = parse_ticker(t, sym)
        if data:
            cache_set(cache_key, data)
            return jsonify(data)
        return jsonify({"error": "não encontrado"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/simulate", methods=["POST"])
def simulate():
    body = request.get_json()
    if not body: return jsonify({"error": "body vazio"}), 400
    symbols     = body.get("symbols", [])
    valor_total = float(body.get("valor_total", 0))
    dist        = body.get("distribuicao", "igual")
    pcts        = body.get("pct", {})
    if not symbols or valor_total <= 0:
        return jsonify({"error": "symbols e valor_total obrigatórios"}), 400
    alloc = {}
    if dist == "igual":
        per = valor_total / len(symbols)
        for s in symbols: alloc[s["sym"]] = per
    elif dist == "yield":
        yields = {}
        for s in symbols:
            c = cache_get(f"quotes_{s['sym']}", ttl=300)
            yields[s["sym"]] = (c[0].get("dividendYield") or 0.01) if c and isinstance(c, list) and c else 0.01
        total_y = sum(yields.values())
        for s in symbols: alloc[s["sym"]] = valor_total * (yields[s["sym"]] / total_y)
    else:
        total_pct = sum(float(pcts.get(s["sym"], 0)) for s in symbols)
        for s in symbols:
            p = float(pcts.get(s["sym"], 0))
            alloc[s["sym"]] = valor_total * (p / total_pct) if total_pct > 0 else valor_total / len(symbols)
    results = []
    for s in symbols:
        sym = s["sym"]; tipo = s["tipo"]; inv = alloc.get(sym, 0)
        ck  = f"sim_{sym}"; cached = cache_get(ck, ttl=300)
        if cached:
            quote = cached["quote"]; div_h = cached["div_h"]
        else:
            try:
                t = yf.Ticker(b3_symbol(sym), session=_yf_session)
                quote = parse_ticker(t, sym); div_h = get_dividend_history(t, sym)
                cache_set(ck, {"quote": quote, "div_h": div_h})
            except:
                quote = None; div_h = None
        if not quote: continue
        price = quote.get("regularMarketPrice") or 0
        cotas = int(inv // price) if price > 0 else 0
        real  = cotas * price
        if div_h:
            projected = [{**p, "value_total": round(p["value_cota"] * cotas, 2)} for p in div_h.get("projected", [])]
            mensal_estimado = round(sum(p["value_total"] for p in projected) / max(len(projected), 1), 2)
            anual_estimado  = round(sum(p["value_total"] for p in projected[:12]), 2)
            freq_label = div_h["freq_label"]; last_value = div_h["last_value"]
            history = div_h["history"]; months_paid = div_h["months_paid"]
        else:
            dy = quote.get("dividendYield") or 0
            mensal_estimado = round(real * (dy / 100) / 12, 2)
            anual_estimado  = round(real * (dy / 100), 2)
            freq_label = "—"; last_value = quote.get("lastDividendValue") or 0
            history = []; months_paid = []; projected = []
        results.append({"sym": sym, "name": quote.get("shortName") or sym, "tipo": tipo,
                        "price": price, "cotas": cotas, "investido": round(real, 2),
                        "div_yield": quote.get("dividendYield") or 0,
                        "mensal_estimado": mensal_estimado, "anual_estimado": anual_estimado,
                        "freq_label": freq_label, "last_value": last_value,
                        "months_paid": months_paid, "history": history, "projected": projected})
    total_mensal = round(sum(r["mensal_estimado"] for r in results), 2)
    total_anual  = round(sum(r["anual_estimado"]  for r in results), 2)
    total_inv    = round(sum(r["investido"]        for r in results), 2)
    yield_medio  = round((total_anual / total_inv * 100) if total_inv > 0 else 0, 2)
    return jsonify({"results": results, "total_mensal": total_mensal,
                    "total_anual": total_anual, "total_inv": total_inv, "yield_medio": yield_medio})

@app.route("/api/news")
def get_news():
    cat = request.args.get("categoria", "todas")
    ck  = f"news_{cat}"; cached = cache_get(ck, ttl=300)
    if cached: return jsonify(cached)
    feeds = [
        {"url": "https://www.infomoney.com.br/feed/",           "fonte": "InfoMoney"},
        {"url": "https://exame.com/invest/feed/",                "fonte": "Exame Invest"},
        {"url": "https://valor.globo.com/rss/financas/feed.xml", "fonte": "Valor Econômico"},
        {"url": "https://br.investing.com/rss/news.rss",         "fonte": "Investing.com"},
    ]
    keywords = {
        "b3":       ["b3","ibovespa","ibov","ação","ações","bolsa","petrobras","bovespa"],
        "fiis":     ["fii","fundo imobiliário","fundos imobiliários","ifix","tijolo"],
        "economia": ["selic","inflação","pib","juros","banco central","câmbio","dólar","ipca","copom"],
        "mundo":    ["fed","nasdaq","s&p","nyse","dow jones","china","europa","wall street"],
    }
    def detectar(titulo, desc):
        txt = (titulo + " " + (desc or "")).lower()
        for c, kws in keywords.items():
            if any(k in txt for k in kws): return c
        return "economia"
    def tempo_rel(pub):
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(pub)
            diff = int((datetime.now(dt.tzinfo) - dt).total_seconds() / 60)
            if diff < 1: return "agora"
            if diff < 60: return f"há {diff} min"
            if diff < 1440: return f"há {diff // 60}h"
            return f"há {diff // 1440} dias"
        except: return ""
    all_news = []
    for f in feeds:
        try:
            feed = feedparser.parse(f["url"])
            for entry in feed.entries[:15]:
                titulo   = entry.get("title", "").strip()
                resumo   = re.sub(r"<[^>]+>", "", entry.get("summary", "")).strip()[:280]
                cat_item = detectar(titulo, resumo)
                if cat != "todas" and cat_item != cat: continue
                all_news.append({"titulo": titulo, "resumo": resumo, "fonte": f["fonte"],
                                 "categoria": cat_item, "url": entry.get("link", "#"),
                                 "tempo": tempo_rel(entry.get("published", ""))})
        except Exception as e:
            print(f"  Feed {f['fonte']}: {e}")
    seen, unique = set(), []
    for n in all_news:
        k = n["titulo"][:50]
        if k not in seen: seen.add(k); unique.append(n)
    cache_set(ck, unique[:30])
    return jsonify(unique[:30])

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

@app.route("/api/clear-cache", methods=["POST"])
def clear_cache_route():
    body = request.get_json() or {}
    sym  = body.get("symbol", "").upper()
    if sym:
        with _lock:
            for k in [k for k in _cache if sym in k]: del _cache[k]
    return jsonify({"ok": True})

@app.route("/api/history/<symbol>")
def get_history(symbol):
    sym = symbol.upper().strip(); period = request.args.get("period", "1mo")
    ck  = f"hist_{sym}_{period}"; cached = cache_get(ck, ttl=300)
    if cached: return jsonify(cached)
    try:
        t    = yf.Ticker(b3_symbol(sym), session=_yf_session)
        hist = t.history(period=period, interval="1d")
        if hist.empty: return jsonify({"error": "sem dados"}), 404
        data = [{"date": str(idx.date()), "close": round(float(row["Close"]), 2),
                 "open": round(float(row["Open"]), 2), "high": round(float(row["High"]), 2),
                 "low": round(float(row["Low"]), 2), "vol": int(row["Volume"])}
                for idx, row in hist.iterrows()]
        cache_set(ck, data); return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/asset/<symbol>")
def get_asset(symbol):
    sym = symbol.upper().strip(); ck = f"asset_{sym}"
    cached = cache_get(ck, ttl=120)
    if cached: return jsonify(cached)
    try:
        t     = yf.Ticker(b3_symbol(sym), session=_yf_session)
        quote = parse_ticker(t, sym)
        if not quote: return jsonify({"error": "não encontrado"}), 404
        divs_raw = []
        try:
            divs = t.dividends
            if divs is not None and len(divs) > 0:
                cutoff = datetime.now() - timedelta(days=730)
                divs   = divs[divs.index >= cutoff.strftime("%Y-%m-%d")]
                for dt, val in divs.items():
                    if hasattr(dt, 'to_pydatetime'): dt = dt.to_pydatetime()
                    divs_raw.append({"date": dt.strftime("%d/%m/%Y"), "value": round(float(val), 6)})
                divs_raw.sort(key=lambda x: x["date"], reverse=True)
        except: pass
        div_projection = None
        try:
            div_h = get_dividend_history(t, sym)
            if div_h:
                div_projection = {"freq_label": div_h["freq_label"], "avg_value": div_h["avg_value"],
                                  "last_value": div_h["last_value"], "months_paid": div_h["months_paid"],
                                  "projected": div_h["projected"][:6]}
        except: pass
        result = {**quote, "dividends": divs_raw[:12], "div_projection": div_projection}
        cache_set(ck, result); return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/compound", methods=["POST"])
def compound_interest():
    body          = request.get_json() or {}
    valor         = float(body.get("valor",         0))
    anos          = float(body.get("anos",           1))
    yield_anual   = float(body.get("yield_anual",    0))
    reinvestir    = bool (body.get("reinvestir",     True))
    aporte_mensal = float(body.get("aporte_mensal",  0))
    if valor <= 0 or anos <= 0 or yield_anual <= 0:
        return jsonify({"error": "parâmetros inválidos"}), 400
    taxa_mensal = (1 + yield_anual / 100) ** (1/12) - 1
    meses = int(anos * 12); timeline = []
    saldo = valor; total_div = 0.0; total_inv = valor
    for m in range(1, meses + 1):
        div_mes   = saldo * taxa_mensal; total_div += div_mes
        if reinvestir: saldo += div_mes
        if aporte_mensal > 0: saldo += aporte_mensal; total_inv += aporte_mensal
        if m % 3 == 0 or m == meses:
            timeline.append({"mes": m, "label": f"{m//12}a {m%12}m" if m >= 12 else f"{m}m",
                             "saldo": round(saldo, 2), "dividendo": round(div_mes, 2)})
    return jsonify({"saldo_final": round(saldo, 2), "total_investido": round(total_inv, 2),
                    "total_dividendos": round(total_div, 2), "ganho_liquido": round(saldo - total_inv, 2),
                    "yield_total_pct": round((saldo / total_inv - 1) * 100, 2) if total_inv > 0 else 0,
                    "timeline": timeline})

# ─── STARTUP ──────────────────────────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    print("=" * 50)
    print("  B3 Terminal — http://localhost:5000")
    print(f"  Admin: {ADMIN_USER}")
    print("=" * 50)
    app.run(debug=False, host="0.0.0.0", port=5000)
