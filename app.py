import os, re, threading, time, calendar, requests as req_lib
from datetime import datetime, date, timedelta
from flask import Flask, jsonify, render_template, request, redirect, url_for
from flask_cors import CORS
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from models import db, User, Ativo, UserData
from models import ADMIN_EMAIL
import feedparser
from dateutil.relativedelta import relativedelta

app = Flask(__name__)
import secrets as _sec
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or _sec.token_hex(32)
_db_url = os.environ.get("DATABASE_URL", "sqlite:////tmp/b3terminal.db")
# Render usa postgres:// mas SQLAlchemy precisa de postgresql://
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"]       = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Reconecta automaticamente quando a conexão SSL quebra (comum no Render free tier)
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping":   True,       # testa a conexão antes de usar
    "pool_recycle":    280,        # recicla conexões a cada 280s (antes do timeout do Render)
    "pool_size":       5,
    "max_overflow":    2,
    "connect_args":    {"sslmode": "require", "connect_timeout": 10}
    if not _db_url.startswith("sqlite") else {},
}
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)

BRAPI_TOKEN = os.environ.get("BRAPI_TOKEN", "")
BRAPI_BASE  = "https://brapi.dev/api"

CORS(app, origins=["https://b3terminal.onrender.com","http://localhost:5000"], supports_credentials=True)
db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view    = "login_page"
login_manager.login_message = ""

with app.app_context():
    db.create_all()
    # ── Migration: adiciona colunas novas se não existirem ──────────────────
    try:
        from sqlalchemy import text
        with db.engine.connect() as conn:
            for col, definition in [
                ("is_admin",   "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("is_blocked", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ]:
                try:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {definition}"))
                    conn.commit()
                    print(f"  Migration: coluna '{col}' adicionada.")
                except Exception:
                    conn.rollback()  # coluna já existe, tudo certo
    except Exception as e:
        print(f"  Migration warning: {e}")

# ── Rate limiting simples ──────────────────────────────────────────────────
_rl_store, _rl_lock = {}, threading.Lock()
def rate_limit_check(ip, max_req=10, window=300):
    now = time.time()
    with _rl_lock:
        hits = [t for t in _rl_store.get(ip, []) if now-t < window]
        if len(hits) >= max_req: return False
        hits.append(now); _rl_store[ip] = hits
    return True

@login_manager.user_loader
def load_user(uid):
    return User.query.get(int(uid))

_cache, _lock = {}, threading.Lock()

def cache_get(key, ttl=300):
    with _lock:
        if key in _cache:
            data, ts = _cache[key]
            if time.time() - ts < ttl: return data
    return None

def cache_set(key, data):
    with _lock: _cache[key] = (data, time.time())

def brapi_get(path, params=None):
    p = dict(params or {})
    if BRAPI_TOKEN: p["token"] = BRAPI_TOKEN
    try:
        r = req_lib.get(f"{BRAPI_BASE}{path}", params=p, timeout=15)
        r.raise_for_status(); return r.json()
    except Exception as e:
        print(f"  BRAPI {path}: {e}"); return None

def brapi_quotes(symbols):
    syms   = ",".join(s.upper().replace(".SA","") for s in symbols)
    ck     = f"bq_{syms}"
    cached = cache_get(ck, ttl=300)
    if cached: return cached
    data = brapi_get(f"/quote/{syms}", {"fundamental":"true"})
    if not data or "results" not in data: return []
    results = []
    for r in data["results"]:
        dy = r.get("dividendYield")
        if dy is not None: dy = round(float(dy), 4)
        results.append({
            "symbol":                     r.get("symbol",""),
            "shortName":                  r.get("shortName") or r.get("longName") or r.get("symbol",""),
            "regularMarketPrice":         r.get("regularMarketPrice"),
            "regularMarketChange":        r.get("regularMarketChange"),
            "regularMarketChangePercent": r.get("regularMarketChangePercent"),
            "regularMarketOpen":          r.get("regularMarketOpen"),
            "regularMarketDayLow":        r.get("regularMarketDayLow"),
            "regularMarketDayHigh":       r.get("regularMarketDayHigh"),
            "regularMarketVolume":        r.get("regularMarketVolume"),
            "regularMarketPreviousClose": r.get("regularMarketPreviousClose"),
            "fiftyTwoWeekLow":            r.get("fiftyTwoWeekLow"),
            "fiftyTwoWeekHigh":           r.get("fiftyTwoWeekHigh"),
            "dividendYield":              dy,
            "dividendRate":               r.get("dividendRate"),
            "marketCap":                  r.get("marketCap"),
            "lastDividendValue":          r.get("lastDividendValue"),
            "exDividendDate_br":          None,
            "lastDividendDate_br":        None,
        })
    cache_set(ck, results); return results

def brapi_history(symbol, range_="1mo"):
    sym = symbol.upper().replace(".SA","")
    ck  = f"bh_{sym}_{range_}"; cached = cache_get(ck, ttl=600)
    if cached: return cached
    data = brapi_get(f"/quote/{sym}", {"range":range_,"interval":"1d"})
    if not data or "results" not in data or not data["results"]: return []
    hist = data["results"][0].get("historicalDataPrice",[])
    out  = []
    for h in hist:
        ts = h.get("date")
        if not ts: continue
        try:
            dt = datetime.utcfromtimestamp(ts)
            out.append({"date":dt.strftime("%Y-%m-%d"),
                        "close":round(float(h.get("close") or 0),2),
                        "open": round(float(h.get("open")  or 0),2),
                        "high": round(float(h.get("high")  or 0),2),
                        "low":  round(float(h.get("low")   or 0),2),
                        "vol":  int(h.get("volume") or 0)})
        except: pass
    cache_set(ck, out); return out

def brapi_dividends(symbol, cotas=1):
    sym = symbol.upper().replace(".SA","")
    ck  = f"bdiv_{sym}"; raw = cache_get(ck, ttl=1800)
    if raw is None:
        data = brapi_get(f"/quote/{sym}", {"dividends":"true","modules":"defaultKeyStatistics"})
        if not data or "results" not in data or not data["results"]: return None
        res0 = data["results"][0]
        raw  = res0.get("dividendsData") or {}
        # Guarda também dados diretos do quote para complementar
        raw["_quote"] = {
            "lastDividendValue": res0.get("lastDividendValue"),
            "lastDividendDate":  res0.get("lastDividendDate"),
            "dividendYield":     res0.get("dividendYield"),
            "dividendRate":      res0.get("dividendRate"),
        }
        cache_set(ck, raw)

    cash_divs = raw.get("cashDividends") or []
    quote_data = raw.get("_quote", {})

    # ── Tenta todos os campos de data/valor possíveis ─────────────────────────
    payments = []
    for d in cash_divs:
        try:
            # Campos de data — Brapi usa diferentes nomes por tipo de ativo
            dt_str = (d.get("paymentDate") or
                      d.get("lastDatePrior") or
                      d.get("approvedOn") or
                      d.get("declaredDate") or
                      d.get("date") or
                      d.get("dataEx") or "")
            if not dt_str: continue

            # Normaliza formato da data
            dt_str = str(dt_str).strip()
            dt = None
            for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y"]:
                try: dt = datetime.strptime(dt_str[:10], fmt[:len(dt_str[:10])]); break
                except: pass
            if not dt: continue

            # Campos de valor
            val = float(d.get("rate") or d.get("value") or d.get("adjValue") or
                        d.get("amount") or d.get("dividendValue") or 0)
            if val <= 0: continue

            payments.append({"year":dt.year,"month":dt.month,"day":dt.day,
                             "value":round(val,6),"date_str":dt.strftime("%d/%m/%Y")})
        except: pass

    # Se não achou dividendos no cashDividends, tenta dados diretos do quote
    if not payments and quote_data.get("lastDividendValue") and quote_data.get("lastDividendDate"):
        try:
            ldd = quote_data["lastDividendDate"]
            ldv = float(quote_data["lastDividendValue"])
            if ldv > 0:
                # lastDividendDate pode ser timestamp unix ou string
                if isinstance(ldd, (int, float)):
                    dt = datetime.fromtimestamp(ldd)
                else:
                    dt = datetime.strptime(str(ldd)[:10], "%Y-%m-%d")
                payments.append({"year":dt.year,"month":dt.month,"day":dt.day,
                                 "value":round(ldv,6),"date_str":dt.strftime("%d/%m/%Y")})
        except: pass

    if not payments: return None
    payments.sort(key=lambda x:(x["year"],x["month"],x["day"]))

    # ── Detecta frequência ────────────────────────────────────────────────────
    recent = payments[-24:]
    months_paid = sorted(set(p["month"] for p in recent))
    n = len(months_paid)
    if   n >= 10: freq_label,freq_months = "Mensal",list(range(1,13))
    elif n >= 4:  freq_label,freq_months = "Trimestral",[3,6,9,12]
    elif n >= 2:  freq_label,freq_months = "Semestral",[6,12]
    else:         freq_label,freq_months = "Anual",months_paid or [12]

    # ── Média por mês ─────────────────────────────────────────────────────────
    month_avgs = {}
    for m in freq_months:
        vals = [p["value"] for p in payments if p["month"] == m]
        if vals: month_avgs[m] = round(sum(vals)/len(vals), 6)

    avg_value = round(sum(p["value"] for p in payments)/len(payments), 6)
    last_val  = payments[-1]["value"]

    # ── Projeção ──────────────────────────────────────────────────────────────
    today = date.today(); projected = []
    for i in range(15):
        future = today + relativedelta(months=i)
        if future.month in freq_months:
            proj_val = month_avgs.get(future.month, avg_value)
            hm = [p for p in payments if p["month"] == future.month]
            ad = int(sum(p["day"] for p in hm)/len(hm)) if hm else 15
            ad = min(ad, calendar.monthrange(future.year, future.month)[1])
            pd = date(future.year, future.month, ad)
            if pd >= today:
                projected.append({"date_str": pd.strftime("%d/%m/%Y"),
                                  "month_name": pd.strftime("%b/%Y"),
                                  "value_cota": round(proj_val, 6),
                                  "value_total": round(proj_val * cotas, 2),
                                  "is_next": len(projected) == 0})

    return {"sym":sym,"freq_label":freq_label,"freq_months":freq_months,
            "avg_value":avg_value,"last_value":last_val,"months_paid":months_paid,
            "history":payments[-24:],"projected":projected[:12],
            "total_pagamentos":len(payments)}


@app.route("/login",methods=["GET"])
def login_page():
    if current_user.is_authenticated: return redirect(url_for("index"))
    return render_template("auth.html",modo="login")

@app.route("/cadastro",methods=["GET"])
def cadastro_page():
    if current_user.is_authenticated: return redirect(url_for("index"))
    return render_template("auth.html",modo="cadastro")

@app.route("/admin")
@login_required
def admin_page():
    if not current_user.admin:
        return redirect(url_for("index"))
    return render_template("admin.html")

@app.route("/api/health")
def health():
    return jsonify({"status":"ok","time":datetime.now().isoformat(),"brapi":bool(BRAPI_TOKEN)})

@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/api/auth/cadastro",methods=["POST"])
def api_cadastro():
    data=request.get_json() or {}
    nome=data.get("nome","").strip(); email=data.get("email","").strip().lower(); senha=data.get("senha","")
    if not nome or not email or not senha: return jsonify({"error":"Preencha todos os campos."}),400
    if len(senha)<6: return jsonify({"error":"A senha deve ter pelo menos 6 caracteres."}),400
    if not re.match(r"[^@]+@[^@]+\.[^@]+",email): return jsonify({"error":"E-mail inválido."}),400
    if User.query.filter_by(email=email).first(): return jsonify({"error":"Este e-mail já está cadastrado."}),409
    user=User(nome=nome,email=email); user.set_senha(senha)
    db.session.add(user); db.session.commit()
    login_user(user,remember=True)
    return jsonify({"ok":True,"nome":user.nome})

@app.route("/api/auth/login",methods=["POST"])
def api_login():
    ip = (request.headers.get("X-Forwarded-For","") or request.remote_addr or "").split(",")[0].strip()
    if not rate_limit_check(ip):
        return jsonify({"error":"Muitas tentativas. Aguarde alguns minutos."}),429
    data=request.get_json() or {}
    email=data.get("email","").strip().lower(); senha=data.get("senha","")
    user=User.query.filter_by(email=email).first()
    if not user or not user.check_senha(senha): return jsonify({"error":"E-mail ou senha incorretos."}),401
    if user.is_blocked: return jsonify({"error":"Conta bloqueada pelo administrador."}),403
    login_user(user,remember=True)
    return jsonify({"ok":True,"nome":user.nome,"is_admin":user.admin})

@app.route("/api/auth/logout",methods=["POST"])
@login_required
def api_logout():
    logout_user(); return jsonify({"ok":True})

@app.route("/api/auth/me")
def api_me():
    if current_user.is_authenticated:
        return jsonify({"logado":True,"nome":current_user.nome,"email":current_user.email,"is_admin":current_user.admin})
    return jsonify({"logado":False})

@app.route("/api/carteira",methods=["GET"])
@login_required
def get_carteira():
    ativos=Ativo.query.filter_by(user_id=current_user.id).all()
    return jsonify([{"symbol":a.symbol,"tipo":a.tipo,"qty":a.qty,"pm":a.pm} for a in ativos])

@app.route("/api/carteira",methods=["POST"])
@login_required
def add_carteira():
    data=request.get_json() or {}
    symbol=data.get("symbol","").upper().strip(); tipo=data.get("tipo","acao")
    qty=float(data.get("qty",0)); pm=float(data.get("pm",0))
    if not symbol: return jsonify({"error":"symbol obrigatório"}),400
    ativo=Ativo.query.filter_by(user_id=current_user.id,symbol=symbol).first()
    if ativo: ativo.qty=qty; ativo.pm=pm; ativo.tipo=tipo
    else:
        ativo=Ativo(user_id=current_user.id,symbol=symbol,tipo=tipo,qty=qty,pm=pm)
        db.session.add(ativo)
    db.session.commit(); return jsonify({"ok":True})

@app.route("/api/carteira/<symbol>",methods=["DELETE"])
@login_required
def del_carteira(symbol):
    ativo=Ativo.query.filter_by(user_id=current_user.id,symbol=symbol.upper()).first()
    if ativo: db.session.delete(ativo); db.session.commit()
    return jsonify({"ok":True})

@app.route("/api/carteira/<symbol>",methods=["PATCH"])
@login_required
def update_carteira(symbol):
    data=request.get_json() or {}
    ativo=Ativo.query.filter_by(user_id=current_user.id,symbol=symbol.upper()).first()
    if not ativo: return jsonify({"error":"não encontrado"}),404
    if "qty" in data: ativo.qty=float(data["qty"])
    if "pm"  in data: ativo.pm =float(data["pm"])
    db.session.commit(); return jsonify({"ok":True})

@app.route("/api/quotes")
@login_required
def get_quotes():
    symbols_raw=request.args.get("symbols","")
    if not symbols_raw: return jsonify({"error":"symbols obrigatório"}),400
    symbols=[s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
    ck="quotes_"+"_".join(sorted(symbols)); cached=cache_get(ck,ttl=300)
    if cached: return jsonify(cached)
    results=brapi_quotes(symbols); cache_set(ck,results); return jsonify(results)

@app.route("/api/search/<symbol>")
@login_required
def search_symbol(symbol):
    sym=symbol.upper().strip().replace(".SA",""); ck=f"search_{sym}"
    cached=cache_get(ck,ttl=600)
    if cached: return jsonify(cached)
    res=brapi_quotes([sym])
    if res and res[0].get("regularMarketPrice"):
        q = res[0]
        # Detecta se é FII pelo nome ou tipo
        name = (q.get("shortName") or q.get("longName") or "").lower()
        tipo = "fii" if (sym.endswith("11") and any(x in name for x in ["fii","fundo","reit","cri","cra","lci","lca"])) or \
               (sym.endswith("11") and q.get("quoteType","") in ["ETF","FUND"]) else "acao"
        r = {"found":True,"data":q,"tipo":tipo}
    else:
        r = {"found":False}
    cache_set(ck,r); return jsonify(r)

@app.route("/api/index/<path:symbol>")
@login_required
def get_index(symbol):
    sym=symbol.upper().replace(".SA",""); ck=f"idx_{sym}"
    cached=cache_get(ck,ttl=300)
    if cached: return jsonify(cached)
    res=brapi_quotes([sym])
    if not res: return jsonify({"error":"não encontrado"}),404
    cache_set(ck,res[0]); return jsonify(res[0])

@app.route("/api/history/<symbol>")
@login_required
def get_history(symbol):
    sym=symbol.upper().strip().replace(".SA",""); period=request.args.get("period","1mo")
    ck=f"hist_{sym}_{period}"; cached=cache_get(ck,ttl=600)
    if cached: return jsonify(cached)
    data=brapi_history(sym,period)
    if data: cache_set(ck,data)
    return jsonify(data or [])

# ── Chart endpoint for Análise Gráfica ────────────────────────────────────────
CHART_PERIOD_MAP = {
    "1d":  ("1d",  "30m",  True),
    "5d":  ("5d",  "1h",   True),
    "1mo": ("1mo", "1d",   False),
    "3mo": ("3mo", "1d",   False),
    "6mo": ("6mo", "1d",   False),
    "1y":  ("1y",  "1d",   False),
    "5y":  ("5y",  "1wk",  False),
    "max": ("max", "1mo",  False),
}

def brapi_chart(symbol, period="1mo"):
    sym = symbol.upper().replace(".SA","")
    range_, interval, intraday = CHART_PERIOD_MAP.get(period, ("1mo","1d",False))
    ttl = 120 if intraday else 600
    ck  = f"gc_{sym}_{period}"
    cached = cache_get(ck, ttl=ttl)
    if cached: return cached
    data = brapi_get(f"/quote/{sym}", {"range": range_, "interval": interval})
    if not data or "results" not in data or not data["results"]: return []
    hist = data["results"][0].get("historicalDataPrice", [])
    out, seen = [], set()
    for h in hist:
        ts = h.get("date")
        if not ts: continue
        try:
            cl = float(h.get("close") or 0)
            op = float(h.get("open")  or 0) or cl
            hi = float(h.get("high")  or 0) or cl
            lo = float(h.get("low")   or 0) or cl
            if cl <= 0: continue
            if intraday:
                time_val = int(ts)
            else:
                dt = datetime.utcfromtimestamp(ts)
                time_val = dt.strftime("%Y-%m-%d")
            if time_val in seen: continue
            seen.add(time_val)
            out.append({"time":time_val,"open":round(op,2),"high":round(hi,2),
                        "low":round(lo,2),"close":round(cl,2),"volume":int(h.get("volume") or 0)})
        except: pass
    # If intraday returned no data, fallback to daily
    if not out and intraday:
        data2 = brapi_get(f"/quote/{sym}", {"range": range_, "interval": "1d"})
        if data2 and "results" in data2 and data2["results"]:
            for h in data2["results"][0].get("historicalDataPrice",[]):
                ts = h.get("date")
                if not ts: continue
                try:
                    cl = float(h.get("close") or 0)
                    if cl <= 0: continue
                    dt = datetime.utcfromtimestamp(ts)
                    time_val = dt.strftime("%Y-%m-%d")
                    if time_val in seen: continue
                    seen.add(time_val)
                    out.append({"time":time_val,
                                "open":round(float(h.get("open") or 0) or cl,2),
                                "high":round(float(h.get("high") or 0) or cl,2),
                                "low":round(float(h.get("low")  or 0) or cl,2),
                                "close":cl,"volume":int(h.get("volume") or 0)})
                except: pass
    cache_set(ck, out)
    return out

@app.route("/api/chart/<symbol>")
@login_required
def get_chart(symbol):
    sym    = symbol.upper().strip().replace(".SA","")
    period = request.args.get("period","1mo")
    if period not in CHART_PERIOD_MAP:
        period = "1mo"
    data = brapi_chart(sym, period)
    return jsonify(data or [])

@app.route("/api/asset/<symbol>")
@login_required
def get_asset(symbol):
    sym=symbol.upper().strip().replace(".SA",""); ck=f"asset_{sym}"
    cached=cache_get(ck,ttl=600)
    if cached: return jsonify(cached)
    res=brapi_quotes([sym])
    if not res: return jsonify({"error":"não encontrado"}),404
    quote=res[0]; div_h=brapi_dividends(sym)
    divs_raw=div_h["history"][:12] if div_h else []
    div_proj={"freq_label":div_h["freq_label"],"avg_value":div_h["avg_value"],
              "last_value":div_h["last_value"],"months_paid":div_h["months_paid"],
              "projected":div_h["projected"][:6]} if div_h else None
    result={**quote,"dividends":divs_raw,"div_projection":div_proj}
    cache_set(ck,result); return jsonify(result)

@app.route("/api/simulate",methods=["POST"])
@login_required
def simulate():
    body=request.get_json() or {}
    symbols=body.get("symbols",[]); valor_total=float(body.get("valor_total",0))
    dist=body.get("distribuicao","igual"); pcts=body.get("pct",{})
    if not symbols or valor_total<=0: return jsonify({"error":"parâmetros inválidos"}),400
    alloc={}
    if dist=="igual":
        for s in symbols: alloc[s["sym"]]=valor_total/len(symbols)
    elif dist=="yield":
        ys={}
        for s in symbols:
            c=cache_get(f"quotes_{s['sym']}",ttl=300)
            ys[s["sym"]]=(c[0].get("dividendYield") or 0.01) if c and isinstance(c,list) and c else 0.01
        ty=sum(ys.values())
        for s in symbols: alloc[s["sym"]]=valor_total*(ys[s["sym"]]/ty)
    else:
        tp=sum(float(pcts.get(s["sym"],0)) for s in symbols)
        for s in symbols:
            alloc[s["sym"]]=valor_total*(float(pcts.get(s["sym"],0))/tp) if tp>0 else valor_total/len(symbols)
    all_syms=[s["sym"].upper().replace(".SA","") for s in symbols]
    quotes_map={q["symbol"]:q for q in brapi_quotes(all_syms)}
    results=[]
    for s in symbols:
        sym=s["sym"].upper().replace(".SA",""); tipo=s["tipo"]; inv=alloc.get(s["sym"],0)
        quote=quotes_map.get(sym)
        if not quote: continue
        price=quote.get("regularMarketPrice") or 0
        cotas=int(inv//price) if price>0 else 0; real=round(cotas*price,2)
        div_h=brapi_dividends(sym,cotas)
        if div_h and div_h.get("projected"):
            projected=[{**p,"value_total":round(p["value_cota"]*cotas,2)} for p in div_h["projected"]]
            n_proj=len(projected)
            men=round(sum(p["value_total"] for p in projected)/n_proj,2) if n_proj>0 else 0
            anl=round(sum(p["value_total"] for p in projected[:12]),2)
            freq_label=div_h["freq_label"]; last_value=div_h["last_value"]
        else:
            dy=quote.get("dividendYield") or 0; men=round(real*(dy/100)/12,2); anl=round(real*(dy/100),2)
            freq_label="—"; last_value=0; projected=[]
        results.append({"sym":sym,"name":quote.get("shortName") or sym,"tipo":tipo,
                        "price":price,"cotas":cotas,"investido":real,
                        "div_yield":quote.get("dividendYield") or 0,
                        "mensal_estimado":men,"anual_estimado":anl,
                        "freq_label":freq_label,"last_value":last_value,"projected":projected})
    tM=round(sum(r["mensal_estimado"] for r in results),2)
    tA=round(sum(r["anual_estimado"] for r in results),2)
    tI=round(sum(r["investido"] for r in results),2)
    yM=round((tA/tI*100) if tI>0 else 0,2)
    return jsonify({"results":results,"total_mensal":tM,"total_anual":tA,"total_inv":tI,"yield_medio":yM})

@app.route("/api/compound",methods=["POST"])
@login_required
def compound_interest():
    body=request.get_json() or {}
    valor=float(body.get("valor",0)); anos=float(body.get("anos",1))
    yield_anual=float(body.get("yield_anual",0)); reinvestir=bool(body.get("reinvestir",True))
    aporte_mensal=float(body.get("aporte_mensal",0))
    if valor<=0 or anos<=0 or yield_anual<=0: return jsonify({"error":"parâmetros inválidos"}),400
    taxa_mensal=(1+yield_anual/100)**(1/12)-1; meses=int(anos*12)
    saldo=valor; total_div=0.0; total_inv=valor; timeline=[]
    for m in range(1,meses+1):
        div_mes=saldo*taxa_mensal; total_div+=div_mes
        if reinvestir: saldo+=div_mes
        if aporte_mensal>0: saldo+=aporte_mensal; total_inv+=aporte_mensal
        if m%3==0 or m==meses:
            timeline.append({"mes":m,"label":f"{m//12}a {m%12}m" if m>=12 else f"{m}m",
                             "saldo":round(saldo,2),"dividendo":round(div_mes,2)})
    return jsonify({"saldo_final":round(saldo,2),"total_investido":round(total_inv,2),
                    "total_dividendos":round(total_div,2),"ganho_liquido":round(saldo-total_inv,2),
                    "yield_total_pct":round((saldo/total_inv-1)*100,2) if total_inv>0 else 0,
                    "timeline":timeline})

@app.route("/api/news")
@login_required
def get_news():
    cat=request.args.get("categoria","todas"); ck=f"news_{cat}"
    cached=cache_get(ck,ttl=300)
    if cached: return jsonify(cached)
    feeds=[{"url":"https://www.infomoney.com.br/feed/","fonte":"InfoMoney"},
           {"url":"https://exame.com/invest/feed/","fonte":"Exame Invest"},
           {"url":"https://valor.globo.com/rss/financas/feed.xml","fonte":"Valor Econômico"}]
    kw={"b3":["b3","ibovespa","ibov","ação","ações","bolsa","petrobras"],
        "fiis":["fii","fundo imobiliário","ifix"],
        "economia":["selic","inflação","pib","juros","banco central","câmbio","dólar","ipca"],
        "mundo":["fed","nasdaq","s&p","nyse","dow jones","china","europa"]}
    def det(t,d):
        txt=(t+" "+(d or "")).lower()
        for c,ks in kw.items():
            if any(k in txt for k in ks): return c
        return "economia"
    def tr(pub):
        try:
            from email.utils import parsedate_to_datetime
            dt=parsedate_to_datetime(pub); diff=int((datetime.now(dt.tzinfo)-dt).total_seconds()/60)
            if diff<1: return "agora"
            if diff<60: return f"há {diff} min"
            if diff<1440: return f"há {diff//60}h"
            return f"há {diff//1440} dias"
        except: return ""
    all_news=[]
    for f in feeds:
        try:
            feed=feedparser.parse(f["url"])
            for e in feed.entries[:15]:
                titulo=e.get("title","").strip()
                resumo=re.sub(r"<[^>]+>","",e.get("summary","")).strip()[:280]
                ci=det(titulo,resumo)
                if cat!="todas" and ci!=cat: continue
                all_news.append({"titulo":titulo,"resumo":resumo,"fonte":f["fonte"],
                                 "categoria":ci,"url":e.get("link","#"),"tempo":tr(e.get("published",""))})
        except: pass
    seen,unique=set(),[]
    for n in all_news:
        k=n["titulo"][:50]
        if k not in seen: seen.add(k); unique.append(n)
    cache_set(ck,unique[:30]); return jsonify(unique[:30])

@app.route("/api/clear-cache",methods=["POST"])
@login_required
def clear_cache_route():
    body=request.get_json() or {}; sym=body.get("symbol","").upper()
    with _lock:
        keys=[k for k in list(_cache.keys()) if not sym or sym in k]
        for k in keys: _cache.pop(k,None)
    return jsonify({"ok":True})

# ── Dados do usuário (watchlist, preferências, etc.) ─────────────────────────
@app.route("/api/dividends/<symbol>")
@login_required
def get_dividends(symbol):
    sym = symbol.upper().replace(".SA","")
    result = brapi_dividends(sym, 1)
    if not result:
        return jsonify({"found":False,"projected":[],"freq_label":"—","avg_value":0})
    return jsonify({**result,"found":True})

@app.route("/api/user/data/<key>", methods=["GET"])
@login_required
def user_data_get(key):
    import json as _json
    row = UserData.query.filter_by(user_id=current_user.id, key=key).first()
    if not row:
        return jsonify({"value": None})
    try:
        return jsonify({"value": _json.loads(row.value)})
    except:
        return jsonify({"value": None})

@app.route("/api/user/data/<key>", methods=["POST"])
@login_required
def user_data_set(key):
    import json as _json
    body  = request.get_json(silent=True) or {}
    value = body.get("value")
    if value is None:
        return jsonify({"error": "value obrigatório"}), 400
    row = UserData.query.filter_by(user_id=current_user.id, key=key).first()
    if row:
        row.value      = _json.dumps(value)
        row.updated_at = datetime.utcnow()
    else:
        row = UserData(user_id=current_user.id, key=key, value=_json.dumps(value))
        db.session.add(row)
    db.session.commit()
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════════════════════════════════
# PAINEL ADMINISTRADOR
# ═══════════════════════════════════════════════════════════════════════════════

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.admin:
            return jsonify({"error":"Acesso restrito"}), 403
        return f(*args, **kwargs)
    return decorated

@app.route("/api/admin/stats")
@login_required
@admin_required
def admin_stats():
    total_users   = User.query.filter_by(is_admin=False).count()
    blocked_users = User.query.filter_by(is_blocked=True).count()
    total_ativos  = Ativo.query.count()
    recent_users  = User.query.order_by(User.criado_em.desc()).limit(10).all()
    return jsonify({
        "total_users":    total_users,
        "blocked_users":  blocked_users,
        "total_ativos":   total_ativos,
        "recent_users": [{
            "id":         u.id,
            "nome":       u.nome,
            "email":      u.email,
            "criado_em":  u.criado_em.strftime("%d/%m/%Y %H:%M") if u.criado_em else "",
            "is_blocked": u.is_blocked,
            "num_ativos": len(u.ativos),
        } for u in recent_users]
    })

@app.route("/api/admin/users")
@login_required
@admin_required
def admin_users():
    users = User.query.order_by(User.criado_em.desc()).all()
    return jsonify([{
        "id":         u.id,
        "nome":       u.nome,
        "email":      u.email,
        "criado_em":  u.criado_em.strftime("%d/%m/%Y %H:%M") if u.criado_em else "",
        "is_blocked": u.is_blocked,
        "is_admin":   u.admin,
        "num_ativos": len(u.ativos),
    } for u in users])

@app.route("/api/admin/user/<int:user_id>/block", methods=["POST"])
@login_required
@admin_required
def admin_block_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.admin:
        return jsonify({"error": "Não é possível bloquear o administrador"}), 400
    body = request.get_json() or {}
    user.is_blocked = bool(body.get("blocked", True))
    db.session.commit()
    return jsonify({"ok": True, "blocked": user.is_blocked})

@app.route("/api/admin/user/<int:user_id>/delete", methods=["DELETE"])
@login_required
@admin_required
def admin_delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.admin:
        return jsonify({"error": "Não é possível deletar o administrador"}), 400
    db.session.delete(user)
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/admin/user/<int:user_id>/ativos")
@login_required
@admin_required
def admin_user_ativos(user_id):
    user = User.query.get_or_404(user_id)
    return jsonify([{"symbol":a.symbol,"tipo":a.tipo,"qty":a.qty,"pm":a.pm} for a in user.ativos])


# ════════════════════════════════════════════════════════════════════════════════
# ABA RI — RELAÇÕES COM INVESTIDORES
# ════════════════════════════════════════════════════════════════════════════════

RI_DB = [
  # AÇÕES — GRANDES EMPRESAS
  {"t":"PETR4","n":"Petrobras","full":"Petróleo Brasileiro S.A.","s":"Petróleo e Gás","ri":"https://ri.petrobras.com.br","d":"petrobras.com.br"},
  {"t":"PETR3","n":"Petrobras","full":"Petróleo Brasileiro S.A.","s":"Petróleo e Gás","ri":"https://ri.petrobras.com.br","d":"petrobras.com.br"},
  {"t":"VALE3","n":"Vale","full":"Vale S.A.","s":"Mineração","ri":"https://www.vale.com/pt/investors","d":"vale.com"},
  {"t":"ITUB4","n":"Itaú Unibanco","full":"Itaú Unibanco Holding S.A.","s":"Bancos","ri":"https://www.itau.com.br/relacoes-com-investidores/","d":"itau.com.br"},
  {"t":"ITUB3","n":"Itaú Unibanco","full":"Itaú Unibanco Holding S.A.","s":"Bancos","ri":"https://www.itau.com.br/relacoes-com-investidores/","d":"itau.com.br"},
  {"t":"BBDC4","n":"Bradesco","full":"Banco Bradesco S.A.","s":"Bancos","ri":"https://ri.bradesco.com.br/pt/","d":"bradesco.com.br"},
  {"t":"BBDC3","n":"Bradesco","full":"Banco Bradesco S.A.","s":"Bancos","ri":"https://ri.bradesco.com.br/pt/","d":"bradesco.com.br"},
  {"t":"ABEV3","n":"Ambev","full":"Ambev S.A.","s":"Bebidas","ri":"https://ri.ambev.com.br/pt-br/","d":"ambev.com.br"},
  {"t":"WEGE3","n":"WEG","full":"WEG S.A.","s":"Bens Industriais","ri":"https://ri.weg.net/pt/","d":"weg.net"},
  {"t":"BBAS3","n":"Banco do Brasil","full":"Banco do Brasil S.A.","s":"Bancos","ri":"https://ri.bb.com.br/pt-br/","d":"bb.com.br"},
  {"t":"RENT3","n":"Localiza","full":"Localiza Rent a Car S.A.","s":"Mobilidade","ri":"https://ri.localiza.com","d":"localiza.com"},
  {"t":"LREN3","n":"Lojas Renner","full":"Lojas Renner S.A.","s":"Varejo","ri":"https://ri.lojasrenner.com.br","d":"lojasrenner.com.br"},
  {"t":"MGLU3","n":"Magazine Luiza","full":"Magazine Luiza S.A.","s":"Varejo","ri":"https://ri.magazineluiza.com.br","d":"magazineluiza.com.br"},
  {"t":"SUZB3","n":"Suzano","full":"Suzano S.A.","s":"Papel e Celulose","ri":"https://ri.suzano.com.br","d":"suzano.com.br"},
  {"t":"GGBR4","n":"Gerdau","full":"Gerdau S.A.","s":"Siderurgia","ri":"https://ri.gerdau.com/pt/","d":"gerdau.com"},
  {"t":"GGBR3","n":"Gerdau","full":"Gerdau S.A.","s":"Siderurgia","ri":"https://ri.gerdau.com/pt/","d":"gerdau.com"},
  {"t":"EQTL3","n":"Equatorial Energia","full":"Equatorial Energia S.A.","s":"Energia Elétrica","ri":"https://ri.equatorialenergia.com.br","d":"equatorialenergia.com.br"},
  {"t":"TOTS3","n":"TOTVS","full":"TOTVS S.A.","s":"Tecnologia","ri":"https://ri.totvs.com","d":"totvs.com"},
  {"t":"PRIO3","n":"PetroRio","full":"PetroRio S.A.","s":"Petróleo e Gás","ri":"https://ri.prio3.com.br/","d":"petrorio.com.br"},
  {"t":"CSAN3","n":"Cosan","full":"Cosan S.A.","s":"Energia / Logística","ri":"https://ri.cosan.com.br","d":"cosan.com.br"},
  {"t":"BPAC11","n":"BTG Pactual","full":"Banco BTG Pactual S.A.","s":"Bancos","ri":"https://www.btgpactual.com/home/investor-relations","d":"btgpactual.com"},
  {"t":"BPAC3","n":"BTG Pactual","full":"Banco BTG Pactual S.A.","s":"Bancos","ri":"https://www.btgpactual.com/home/investor-relations","d":"btgpactual.com"},
  {"t":"RDOR3","n":"Rede D'Or","full":"Rede D'Or São Luiz S.A.","s":"Saúde","ri":"https://ri.rededorsaoluiz.com.br","d":"rededorsaoluiz.com.br"},
  {"t":"HAPV3","n":"Hapvida","full":"Hapvida Participações e Investimentos S.A.","s":"Saúde","ri":"https://ri.hapvida.com.br","d":"hapvida.com.br"},
  {"t":"RADL3","n":"Raia Drogasil","full":"Raia Drogasil S.A.","s":"Farmácias","ri":"https://ri.raiadrogasil.com.br","d":"raiadrogasil.com.br"},
  {"t":"SBSP3","n":"Sabesp","full":"Companhia de Saneamento Básico do Estado de SP","s":"Saneamento","ri":"https://ri.sabesp.com.br","d":"sabesp.com.br"},
  {"t":"ENEV3","n":"Eneva","full":"Eneva S.A.","s":"Energia Elétrica","ri":"https://ri.eneva.com.br","d":"eneva.com.br"},
  {"t":"BRFS3","n":"BRF","full":"BRF S.A.","s":"Alimentos","ri":"https://ri.brf-global.com","d":"brf-global.com"},
  {"t":"JBSS3","n":"JBS","full":"JBS S.A.","s":"Alimentos","ri":"https://ri.jbs.com.br","d":"jbs.com.br"},
  {"t":"BEEF3","n":"Minerva Foods","full":"Minerva S.A.","s":"Alimentos","ri":"https://ri.minervafoods.com","d":"minervafoods.com"},
  {"t":"SLCE3","n":"SLC Agrícola","full":"SLC Agrícola S.A.","s":"Agronegócio","ri":"https://ri.slcagricola.com.br","d":"slcagricola.com.br"},
  {"t":"AGRO3","n":"BrasilAgro","full":"BrasilAgro - Companhia Brasileira de Propriedades Agrícolas","s":"Agronegócio","ri":"https://ri.brasil-agro.com","d":"brasil-agro.com"},
  {"t":"CPFE3","n":"CPFL Energia","full":"CPFL Energia S.A.","s":"Energia Elétrica","ri":"https://ri.cpfl.com.br","d":"cpfl.com.br"},
  {"t":"ELET3","n":"Eletrobras","full":"Centrais Elétricas Brasileiras S.A.","s":"Energia Elétrica","ri":"https://ri.eletrobras.com","d":"eletrobras.com"},
  {"t":"ELET6","n":"Eletrobras","full":"Centrais Elétricas Brasileiras S.A.","s":"Energia Elétrica","ri":"https://ri.eletrobras.com","d":"eletrobras.com"},
  {"t":"CMIG4","n":"CEMIG","full":"Companhia Energética de Minas Gerais","s":"Energia Elétrica","ri":"https://ri.cemig.com.br","d":"cemig.com.br"},
  {"t":"CMIG3","n":"CEMIG","full":"Companhia Energética de Minas Gerais","s":"Energia Elétrica","ri":"https://ri.cemig.com.br","d":"cemig.com.br"},
  {"t":"CPLE6","n":"Copel","full":"Companhia Paranaense de Energia","s":"Energia Elétrica","ri":"https://ri.copel.com","d":"copel.com"},
  {"t":"EGIE3","n":"Engie Brasil","full":"Engie Brasil Energia S.A.","s":"Energia Elétrica","ri":"https://www.engieenergia.com.br/wps/portal/internet/investidores","d":"engieenergia.com.br"},
  {"t":"TAEE4","n":"Taesa","full":"Transmissora Aliança de Energia Elétrica S.A.","s":"Energia Elétrica","ri":"https://ri.taesa.com.br/pt/","d":"taesa.com.br"},
  {"t":"TAEE11","n":"Taesa","full":"Transmissora Aliança de Energia Elétrica S.A.","s":"Energia Elétrica","ri":"https://ri.taesa.com.br/pt/","d":"taesa.com.br"},
  {"t":"CXSE3","n":"Caixa Seguridade","full":"Caixa Seguridade Participações S.A.","s":"Seguros","ri":"https://www.caixaseguridade.com.br/ri/","d":"caixaseguridade.com.br"},
  {"t":"COGN3","n":"Cogna Educação","full":"Cogna Educação S.A.","s":"Educação","ri":"https://ri.cogna.com.br/pt/","d":"cogna.com.br"},
  {"t":"YDUQ3","n":"Yduqs","full":"Yduqs Participações S.A.","s":"Educação","ri":"https://ri.yduqs.com.br","d":"yduqs.com.br"},
  {"t":"SOMA3","n":"Grupo Soma","full":"Grupo de Moda S.A.","s":"Vestuário","ri":"https://ri.somagrupo.com.br/pt/","d":"somagrupo.com.br"},
  {"t":"VIVA3","n":"Vivara","full":"Vivara Participações S.A.","s":"Varejo","ri":"https://ri.vivara.com.br","d":"vivara.com.br"},
  {"t":"GMAT3","n":"Grupo Mateus","full":"Grupo Mateus S.A.","s":"Varejo Alimentar","ri":"https://ri.grupomateus.com.br","d":"grupomateus.com.br"},
  {"t":"PCAR3","n":"GPA","full":"Grupo Pão de Açúcar S.A.","s":"Varejo Alimentar","ri":"https://ri.gpabr.com","d":"gpabr.com"},
  {"t":"ASAI3","n":"Assaí","full":"Sendas Distribuidora S.A.","s":"Varejo Alimentar","ri":"https://ri.assai.com.br","d":"assai.com.br"},
  {"t":"RAIL3","n":"Rumo","full":"Rumo S.A.","s":"Logística","ri":"https://ri.rumolog.com","d":"rumolog.com"},
  {"t":"ECOR3","n":"EcoRodovias","full":"EcoRodovias Infraestrutura e Logística S.A.","s":"Concessões","ri":"https://ri.ecorodovias.com.br","d":"ecorodovias.com.br"},
  {"t":"CCRO3","n":"CCR","full":"CCR S.A.","s":"Concessões","ri":"https://ri.ccr.com.br","d":"ccr.com.br"},
  {"t":"MULT3","n":"Multiplan","full":"Multiplan Empreendimentos Imobiliários S.A.","s":"Shoppings","ri":"https://ri.multiplan.com.br","d":"multiplan.com.br"},
  {"t":"BRML3","n":"BR Malls","full":"BR Malls Participações S.A.","s":"Shoppings","ri":"https://ri.brmalls.com.br","d":"brmalls.com.br"},
  {"t":"IGTI11","n":"Iguatemi","full":"Iguatemi S.A.","s":"Shoppings","ri":"https://ri.iguatemi.com.br","d":"iguatemi.com.br"},
  {"t":"CYRE3","n":"Cyrela","full":"Cyrela Brazil Realty S.A.","s":"Construção","ri":"https://ri.cyrela.com.br/pt/","d":"cyrela.com.br"},
  {"t":"EZTC3","n":"EZTEC","full":"EZTEC Empreendimentos e Participações S.A.","s":"Construção","ri":"https://ri.eztec.com.br","d":"eztec.com.br"},
  {"t":"MRVE3","n":"MRV Engenharia","full":"MRV Engenharia e Participações S.A.","s":"Construção","ri":"https://ri.mrv.com.br","d":"mrv.com.br"},
  {"t":"EVEN3","n":"Even","full":"Even Construtora e Incorporadora S.A.","s":"Construção","ri":"https://ri.even.com.br","d":"even.com.br"},
  {"t":"DIRR3","n":"Direcional","full":"Direcional Engenharia S.A.","s":"Construção","ri":"https://ri.direcional.com.br","d":"direcional.com.br"},
  {"t":"LWSA3","n":"Locaweb","full":"Locaweb Serviços de Internet S.A.","s":"Tecnologia","ri":"https://ri.locaweb.com.br","d":"locaweb.com.br"},
  {"t":"INTB3","n":"Intelbras","full":"Intelbras S.A.","s":"Tecnologia","ri":"https://ri.intelbras.com.br","d":"intelbras.com.br"},
  {"t":"POSI3","n":"Positivo","full":"Positivo Tecnologia S.A.","s":"Tecnologia","ri":"https://ri.positivotecnologia.com.br","d":"positivotecnologia.com.br"},
  {"t":"CASH3","n":"Meliuz","full":"Méliuz S.A.","s":"Tecnologia Financeira","ri":"https://ri.meliuz.com.br","d":"meliuz.com.br"},
  {"t":"BMGB4","n":"Banco BMG","full":"Banco BMG S.A.","s":"Bancos","ri":"https://ri.bancobmg.com.br","d":"bancobmg.com.br"},
  {"t":"BIDI11","n":"Banco Inter","full":"Banco Inter S.A.","s":"Bancos","ri":"https://investors.inter.co/","d":"inter.co"},
  {"t":"SANB11","n":"Santander Brasil","full":"Banco Santander Brasil S.A.","s":"Bancos","ri":"https://www.santander.com.br/ri/","d":"santander.com.br"},
  {"t":"SANB4","n":"Santander Brasil","full":"Banco Santander Brasil S.A.","s":"Bancos","ri":"https://www.santander.com.br/ri/","d":"santander.com.br"},
  {"t":"IRBR3","n":"IRB Brasil RE","full":"IRB Brasil Resseguros S.A.","s":"Seguros","ri":"https://ri.irbre.com/pt/","d":"irbre.com"},
  {"t":"BBSE3","n":"BB Seguridade","full":"BB Seguridade Participações S.A.","s":"Seguros","ri":"https://ri.bbseguridade.com.br/pt-br/","d":"bbseguridade.com.br"},
  {"t":"PSSA3","n":"Porto Seguro","full":"Porto Seguro S.A.","s":"Seguros","ri":"https://ri.portoseguro.com.br","d":"portoseguro.com.br"},
  {"t":"QUAL3","n":"Qualicorp","full":"Qualicorp Consultoria e Corretora de Seguros S.A.","s":"Saúde","ri":"https://ri.qualicorp.com.br","d":"qualicorp.com.br"},
  {"t":"FLRY3","n":"Fleury","full":"Fleury S.A.","s":"Saúde","ri":"https://ri.fleury.com.br/pt-br/","d":"fleury.com.br"},
  {"t":"HYPE3","n":"Hypera Pharma","full":"Hypera S.A.","s":"Farmacêutico","ri":"https://ri.hypera.com.br","d":"hypera.com.br"},
  {"t":"KLBN4","n":"Klabin","full":"Klabin S.A.","s":"Papel e Celulose","ri":"https://ri.klabin.com.br","d":"klabin.com.br"},
  {"t":"KLBN11","n":"Klabin","full":"Klabin S.A.","s":"Papel e Celulose","ri":"https://ri.klabin.com.br","d":"klabin.com.br"},
  {"t":"DXCO3","n":"Dexco","full":"Dexco S.A.","s":"Materiais de Construção","ri":"https://ri.dexco.com.br","d":"dexco.com.br"},
  {"t":"USIM5","n":"Usiminas","full":"Usinas Siderúrgicas de Minas Gerais S.A.","s":"Siderurgia","ri":"https://ri.usiminas.com","d":"usiminas.com"},
  {"t":"CSNA3","n":"CSN","full":"Companhia Siderúrgica Nacional","s":"Siderurgia","ri":"https://ri.csn.com.br","d":"csn.com.br"},
  {"t":"GOAU4","n":"Metalúrgica Gerdau","full":"Metalúrgica Gerdau S.A.","s":"Siderurgia","ri":"https://ri.gerdau.com/pt/","d":"gerdau.com"},
  {"t":"EMBR3","n":"Embraer","full":"Embraer S.A.","s":"Aeroespacial","ri":"https://ri.embraer.com.br","d":"embraer.com.br"},
  {"t":"RAIZ4","n":"Raízen","full":"Raízen S.A.","s":"Energia / Açúcar","ri":"https://ri.raizen.com.br","d":"raizen.com.br"},
  {"t":"SMTO3","n":"São Martinho","full":"São Martinho S.A.","s":"Açúcar e Etanol","ri":"https://ri.saomartinho.com.br","d":"saomartinho.com.br"},
  {"t":"ENGI11","n":"Energisa","full":"Energisa S.A.","s":"Energia Elétrica","ri":"https://ri.energisa.com.br","d":"energisa.com.br"},
  {"t":"ENBR3","n":"EDP Brasil","full":"EDP Energias do Brasil S.A.","s":"Energia Elétrica","ri":"https://ri.edp.com.br","d":"edp.com.br"},
  {"t":"AURE3","n":"Auren Energia","full":"Auren Energia S.A.","s":"Energia Elétrica","ri":"https://ri.aurenenergia.com.br","d":"aurenenergia.com.br"},
  {"t":"LEVE3","n":"Mahle-Metal Leve","full":"Mahle Metal Leve S.A.","s":"Autopeças","ri":"https://www.mahle-metaleve.com.br/pt/ir/","d":"metaleve.com.br"},
  {"t":"MOVI3","n":"Movida","full":"Movida Participações S.A.","s":"Mobilidade","ri":"https://ri.movida.com.br","d":"movida.com.br"},
  {"t":"UNIP6","n":"Unipar","full":"Unipar Carbocloro S.A.","s":"Química","ri":"https://ri.unipar.com","d":"unipar.com"},
  {"t":"KEPL3","n":"Kepler Weber","full":"Kepler Weber S.A.","s":"Agronegócio","ri":"https://ri.keplerweber.com.br","d":"keplerweber.com.br"},
  {"t":"RECV3","n":"PetroRecôncavo","full":"PetroRecôncavo S.A.","s":"Petróleo e Gás","ri":"https://ri.petroreconcavo.com.br/pt-br/","d":"petroreconcavo.com.br"},
  {"t":"3R11","n":"3R Petroleum","full":"3R Petroleum Óleo e Gás S.A.","s":"Petróleo e Gás","ri":"https://ri.3rpetroleum.com.br","d":"3rpetroleum.com.br"},
  {"t":"ORVR3","n":"Orizon","full":"Orizon Valorização de Resíduos S.A.","s":"Saneamento","ri":"https://ri.orizon.com.br/pt/","d":"orizon.com.br"},
  {"t":"SIMH3","n":"Simpar","full":"Simpar S.A.","s":"Logística","ri":"https://ri.simpar.com.br","d":"simpar.com.br"},
  # FIIs
  {"t":"MXRF11","n":"Maxi Renda FII","full":"XP Malls Fundo de Investimento Imobiliário","s":"FII - Recebíveis","ri":"https://mxrf11.com.br/","d":"btgpactual.com"},
  {"t":"HGLG11","n":"CSHG Logística","full":"CSHG Logística Fundo de Investimento Imobiliário","s":"FII - Logística","ri":"https://www.cshg.com.br/produtos/fundos-imobiliarios/hglg11","d":"cshg.com.br"},
  {"t":"KNRI11","n":"Kinea Renda Imobiliária","full":"Kinea Renda Imobiliária Fundo de Investimento Imobiliário","s":"FII - Híbrido","ri":"https://www.kinea.com.br/investimentos/fundos-imobiliarios/knri11/","d":"kinea.com.br"},
  {"t":"XPML11","n":"XP Malls","full":"XP Malls Fundo de Investimento Imobiliário","s":"FII - Shoppings","ri":"https://www.xpinvestimentos.com.br/fundos/fundos-imobiliarios/xp-malls-fundo-de-investimento-imobiliario/","d":"xpinvestimentos.com.br"},
  {"t":"BCFF11","n":"BTG Pactual Fundo de Fundos","full":"BTG Pactual Fundo de Fundos Imobiliários","s":"FII - Fundos","ri":"https://www.btgpactual.com/investment-banking/asset-management/fundos/bcff11","d":"btgpactual.com"},
  {"t":"VISC11","n":"Vinci Shopping Centers","full":"Vinci Shopping Centers FII","s":"FII - Shoppings","ri":"https://vincifunds.com.br/fundo/visc11","d":"vincifunds.com.br"},
  {"t":"BTLG11","n":"BTG Pactual Logística","full":"BTG Pactual Logística FII","s":"FII - Logística","ri":"https://www.btgpactual.com/investment-banking/asset-management/fundos/btlg11","d":"btgpactual.com"},
  {"t":"XPLG11","n":"XP Log","full":"XP Log Fundo de Investimento Imobiliário","s":"FII - Logística","ri":"https://www.xpinvestimentos.com.br/fundos/fundos-imobiliarios/xp-log/","d":"xpinvestimentos.com.br"},
  {"t":"RBRR11","n":"RBR Rendimento High Grade","full":"RBR Rendimento High Grade FII","s":"FII - Recebíveis","ri":"https://rbrasset.com.br/fundos/rbrr11/","d":"rbrasset.com.br"},
  {"t":"HFOF11","n":"Hedge Top FOFII","full":"Hedge Top FOFII 3 FII","s":"FII - Fundos","ri":"https://hedgeinvestimentos.com.br/fundos/hfof11/","d":"hedgeinvestimentos.com.br"},
  {"t":"IRDM11","n":"Iridium Recebíveis","full":"Iridium Recebíveis Imobiliários FII","s":"FII - Recebíveis","ri":"https://iridiumgestao.com.br/irdm11/","d":"iridiumgestao.com.br"},
  {"t":"RBRF11","n":"RBR Alpha","full":"RBR Alpha Multiestratégia Real Estate FII","s":"FII - Híbrido","ri":"https://rbrasset.com.br/fundos/rbrf11/","d":"rbrasset.com.br"},
  {"t":"GTWR11","n":"GR Louveira","full":"GR Louveira Fundo de Investimento Imobiliário","s":"FII - Logística","ri":"https://www.granadeiro.com.br/gtwr11","d":"granadeiro.com.br"},
  {"t":"HSML11","n":"HSI Malls","full":"HSI Malls Fundo de Investimento Imobiliário","s":"FII - Shoppings","ri":"https://www.hsi.com.br/hsml11","d":"hsi.com.br"},
]

RI_INDEX = {}
for item in RI_DB:
    RI_INDEX[item["t"].upper()] = item
    for word in item["n"].lower().split():
        if len(word) > 2:
            RI_INDEX.setdefault(f"_n_{word}", [])
            if isinstance(RI_INDEX[f"_n_{word}"], list):
                RI_INDEX[f"_n_{word}"].append(item["t"])

@app.route("/api/ai/chat", methods=["POST"])
@login_required
def ai_chat():
    """João — AI Financial Assistant powered by Claude."""
    ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    if not ANTHROPIC_KEY:
        return jsonify({"error": "IA não configurada. Adicione ANTHROPIC_API_KEY nas variáveis de ambiente."}), 503

    body = request.get_json() or {}
    messages = body.get("messages", [])
    if not messages:
        return jsonify({"error": "messages obrigatório"}), 400

    # Validate and sanitize messages
    clean_msgs = []
    for m in messages[-20:]:  # max 20 turns of history
        role = m.get("role", "")
        content = str(m.get("content", "")).strip()
        if role in ("user", "assistant") and content:
            clean_msgs.append({"role": role, "content": content[:2000]})

    if not clean_msgs:
        return jsonify({"error": "Nenhuma mensagem válida."}), 400

    system_prompt = """Você é João, um assistente especializado no mercado financeiro brasileiro (B3).

Seu perfil:
- Nome: João
- Especialidade: Mercado de capitais brasileiro — ações, FIIs, ETFs, BDRs, índices da B3
- Tom: Objetivo, claro, educativo e amigável. Profissional mas acessível.
- Idioma: Sempre português brasileiro

Você domina:
- Análise fundamentalista: P/L, P/VP, EV/EBITDA, ROE, ROIC, Margem EBITDA, Dívida Líquida/EBITDA
- Análise técnica: médias móveis, suporte/resistência, candlestick, volume
- FIIs: tipos (tijolo, papel, híbrido, FOF), P/VP, DY, vacância, gestão
- ETFs brasileiros: BOVA11, IVVB11, SMAL11, DIVO11, HASH11 e outros
- BDRs: como funcionam, tributação, principais ativos
- Estratégias: diversificação, rebalanceamento, preço médio, juros compostos
- Conceitos: dividendos, proventos, JCP, bonificação, subscrição
- Tributação: IR sobre ações, FIIs, ETFs — isenções e obrigações

Ao responder:
- Seja direto e objetivo. Evite respostas longas desnecessárias.
- Use exemplos práticos com números reais quando útil.
- Para perguntas sobre preços específicos, informe que não tem acesso a cotações em tempo real.
- Nunca faça recomendações definitivas de compra/venda sem embasamento — sempre mencione riscos.
- Quando não souber algo, seja honesto.
- Use marcações simples: **negrito** para termos importantes, linhas separadas para listas.
"""

    try:
        r = req_lib.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "system": system_prompt,
                "messages": clean_msgs,
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        reply = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                reply += block.get("text", "")
        if not reply:
            return jsonify({"error": "Resposta vazia da IA."}), 502
        return jsonify({"reply": reply.strip()})
    except req_lib.exceptions.Timeout:
        return jsonify({"error": "Tempo de resposta excedido. Tente novamente."}), 504
    except req_lib.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else 500
        print(f"  AI chat HTTP error {status}: {e}")
        if status == 401:
            return jsonify({"error": "Chave da API inválida. Verifique ANTHROPIC_API_KEY."}), 503
        return jsonify({"error": f"Erro na API de IA ({status})."}), 502
    except Exception as e:
        print(f"  AI chat error: {e}")
        return jsonify({"error": "Erro ao processar resposta da IA."}), 500


@app.route("/api/ri/search")
@login_required
def ri_search():
    q = request.args.get("q","").strip().upper()
    if len(q) < 2:
        return jsonify([])
    results = []
    seen = set()
    # Busca por ticker exato primeiro
    for k, v in RI_INDEX.items():
        if not k.startswith("_") and q in k and v["t"] not in seen:
            results.append(v); seen.add(v["t"])
    # Busca por nome
    q_lower = q.lower()
    for item in RI_DB:
        if item["t"] not in seen and (q_lower in item["n"].lower() or q_lower in item["full"].lower() or q_lower in item["s"].lower()):
            results.append(item); seen.add(item["t"])
    return jsonify(results[:15])

@app.route("/api/ri/all")
@login_required
def ri_all():
    return jsonify(RI_DB[:50])

@app.after_request
def sec_headers(r):
    r.headers.update({
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options":        "SAMEORIGIN",
        "X-XSS-Protection":       "1; mode=block",
        "Referrer-Policy":        "strict-origin-when-cross-origin",
    })
    return r

if __name__=="__main__":
    print("="*50)
    print("  B3 Terminal — http://localhost:5000")
    print(f"  Brapi: {'OK' if BRAPI_TOKEN else 'SEM TOKEN!'}")
    print("="*50)
    app.run(debug=False,host="0.0.0.0",port=5000)
