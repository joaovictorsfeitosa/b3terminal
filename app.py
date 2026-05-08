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
app.config["SECRET_KEY"]               = os.environ.get("SECRET_KEY", "b3terminal-2025-secret")
app.config["SQLALCHEMY_DATABASE_URI"]  = os.environ.get("DATABASE_URL", "sqlite:////tmp/b3terminal.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)

BRAPI_TOKEN = os.environ.get("BRAPI_TOKEN", "")
BRAPI_BASE  = "https://brapi.dev/api"

CORS(app)
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
        data = brapi_get(f"/quote/{sym}", {"dividends":"true"})
        if not data or "results" not in data or not data["results"]: return None
        raw = data["results"][0].get("dividendsData") or {}
        cache_set(ck, raw)

    cash_divs = raw.get("cashDividends") or []
    if not cash_divs: return None

    payments = []
    for d in cash_divs:
        try:
            dt_str = (d.get("paymentDate") or d.get("lastDatePrior") or
                      d.get("approvedOn") or d.get("declaredDate") or "")
            if not dt_str: continue
            dt  = datetime.strptime(dt_str[:10], "%Y-%m-%d")
            val = float(d.get("rate") or d.get("value") or d.get("adjValue") or 0)
            if val <= 0: continue
            payments.append({"year":dt.year,"month":dt.month,"day":dt.day,
                             "value":round(val,6),"date_str":dt.strftime("%d/%m/%Y")})
        except: pass

    if not payments: return None
    payments.sort(key=lambda x:(x["year"],x["month"],x["day"]))

    recent = payments[-24:]
    months_paid = sorted(set(p["month"] for p in recent))
    n = len(months_paid)
    if n >= 10: freq_label,freq_months = "Mensal",list(range(1,13))
    elif n >= 4: freq_label,freq_months = "Trimestral",[3,6,9,12]
    elif n >= 2: freq_label,freq_months = "Semestral",[6,12]
    else:        freq_label,freq_months = "Anual",months_paid or [12]

    month_avgs = {}
    for m in freq_months:
        vals = [p["value"] for p in payments if p["month"] == m]
        if vals: month_avgs[m] = round(sum(vals)/len(vals), 6)

    avg_value = round(sum(p["value"] for p in payments)/len(payments), 6)
    last_val  = payments[-1]["value"]

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
        keys=[k for k in _cache if not sym or sym in k]
        for k in keys: del _cache[k]
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

if __name__=="__main__":
    print("="*50)
    print("  B3 Terminal — http://localhost:5000")
    print(f"  Brapi: {'OK' if BRAPI_TOKEN else 'SEM TOKEN!'}")
    print("="*50)
    app.run(debug=False,host="0.0.0.0",port=5000)
