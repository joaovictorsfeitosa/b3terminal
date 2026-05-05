import os, re, threading, time, calendar
from datetime import datetime, date, timedelta
from flask import Flask, jsonify, render_template, request, redirect, url_for
from flask_cors import CORS
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from models import db, User, Ativo
import yfinance as yf
import feedparser
from dateutil.relativedelta import relativedelta

# ─── SETUP ───────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"]               = os.environ.get("SECRET_KEY", "b3terminal-2025-secret")
app.config["SQLALCHEMY_DATABASE_URI"]  = os.environ.get("DATABASE_URL", "sqlite:///b3terminal.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)

CORS(app)
db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view    = "login_page"   # redireciona para /login se não autenticado
login_manager.login_message = ""

with app.app_context():
    db.create_all()

@login_manager.user_loader
def load_user(uid):
    return User.query.get(int(uid))

# ─── CACHE ───────────────────────────────────────────────────────────────────
_cache, _lock = {}, threading.Lock()

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

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def b3_symbol(sym):
    sym = sym.upper().strip()
    if sym.startswith("^") or "=X" in sym or "-USD" in sym:
        return sym
    return sym if sym.endswith(".SA") else sym + ".SA"

def fmt_date_br(ts):
    """Converte timestamp Unix para dd/mm/aaaa"""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts).strftime("%d/%m/%Y")
    except:
        return None

def parse_ticker(ticker, sym_original):
    try:
        info  = ticker.info or {}
        price = (info.get("currentPrice")
              or info.get("regularMarketPrice")
              or info.get("navPrice"))
        prev  = (info.get("regularMarketPreviousClose")
              or info.get("previousClose"))
        open_p   = info.get("regularMarketOpen")   or info.get("open")
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
            except:
                pass

        if not price:
            return None

        change     = round(price - prev, 4)          if price and prev else None
        change_pct = round((change / prev) * 100, 4) if change and prev else None
        dy = info.get("dividendYield")
        if dy:
            dy = round(dy * 100, 4)

        # Datas em formato BR
        ex_div  = info.get("exDividendDate")
        lst_div = info.get("lastDividendDate")

        return {
            "symbol":                     sym_original.upper(),
            "shortName":                  info.get("shortName") or info.get("longName") or sym_original,
            "regularMarketPrice":         round(float(price), 2),
            "regularMarketChange":        round(change, 2)      if change is not None else None,
            "regularMarketChangePercent": change_pct,
            "regularMarketOpen":          round(float(open_p), 2)  if open_p   else None,
            "regularMarketDayLow":        round(float(day_low), 2) if day_low  else None,
            "regularMarketDayHigh":       round(float(day_high),2) if day_high else None,
            "regularMarketVolume":        int(volume)               if volume   else None,
            "fiftyTwoWeekLow":            round(float(info["fiftyTwoWeekLow"]), 2)  if info.get("fiftyTwoWeekLow")  else None,
            "fiftyTwoWeekHigh":           round(float(info["fiftyTwoWeekHigh"]),2)  if info.get("fiftyTwoWeekHigh") else None,
            "dividendYield":              dy,
            "dividendRate":               info.get("dividendRate"),
            "exDividendDate":             ex_div,
            "exDividendDate_br":          fmt_date_br(ex_div),
            "lastDividendDate":           lst_div,
            "lastDividendDate_br":        fmt_date_br(lst_div),
            "lastDividendValue":          info.get("lastDividendValue"),
            "marketCap":                  info.get("marketCap"),
        }
    except Exception as e:
        print(f"  parse_ticker({sym_original}): {e}")
        return None

def get_dividend_history(ticker, sym_original):
    try:
        divs = ticker.dividends
        if divs is None or len(divs) == 0:
            return None
        cutoff = datetime.now() - timedelta(days=730)
        divs   = divs[divs.index >= cutoff.strftime("%Y-%m-%d")]
        if len(divs) == 0:
            return None

        payments = []
        for dt_idx, val in divs.items():
            try:
                dt = dt_idx.to_pydatetime() if hasattr(dt_idx, "to_pydatetime") else dt_idx
                payments.append({
                    "year":  dt.year, "month": dt.month, "day": dt.day,
                    "value": round(float(val), 6),
                    "date_str": dt.strftime("%d/%m/%Y"),
                })
            except:
                continue

        if not payments:
            return None

        payments.sort(key=lambda x: (x["year"], x["month"]))
        months_paid = sorted(set(p["month"] for p in payments[-24:]))
        avg_value   = round(sum(p["value"] for p in payments[-12:]) / max(len(payments[-12:]), 1), 6)
        last_val    = payments[-1]["value"]

        n = len(months_paid)
        if n >= 10:
            freq_label, freq_months = "Mensal", list(range(1, 13))
        elif n >= 4:
            freq_label, freq_months = "Trimestral", months_paid if n >= 4 else [3,6,9,12]
        elif n >= 2:
            freq_label, freq_months = "Semestral", months_paid if n >= 2 else [6,12]
        else:
            freq_label, freq_months = "Anual", months_paid if months_paid else [12]

        today, projected = date.today(), []
        for i in range(14):
            future = today + relativedelta(months=i)
            if future.month in freq_months:
                hist_m  = [p for p in payments if p["month"] == future.month]
                avg_day = int(sum(p["day"] for p in hist_m) / len(hist_m)) if hist_m else 15
                avg_day = min(avg_day, calendar.monthrange(future.year, future.month)[1])
                pd = date(future.year, future.month, avg_day)
                if pd >= today:
                    projected.append({
                        "date_str":    pd.strftime("%d/%m/%Y"),
                        "month_name":  pd.strftime("%b/%Y"),
                        "value_cota":  round(last_val, 6),
                        "value_total": round(last_val, 2),
                        "is_next":     len(projected) == 0,
                    })

        return {
            "sym": sym_original.upper(),
            "freq_label":  freq_label,
            "freq_months": freq_months,
            "avg_value":   avg_value,
            "last_value":  last_val,
            "months_paid": months_paid,
            "history":     payments[-12:],
            "projected":   projected[:12],
        }
    except Exception as e:
        print(f"  dividend_history({sym_original}): {e}")
        return None

# ─── PÁGINAS PÚBLICAS ─────────────────────────────────────────────────────────
@app.route("/login", methods=["GET"])
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return render_template("auth.html", modo="login")

@app.route("/cadastro", methods=["GET"])
def cadastro_page():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return render_template("auth.html", modo="cadastro")

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

# ─── ROTA PRINCIPAL — exige login ────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("index.html")

# ─── AUTH APIs ────────────────────────────────────────────────────────────────
@app.route("/api/auth/cadastro", methods=["POST"])
def api_cadastro():
    data  = request.get_json() or {}
    nome  = data.get("nome", "").strip()
    email = data.get("email", "").strip().lower()
    senha = data.get("senha", "")
    if not nome or not email or not senha:
        return jsonify({"error": "Preencha todos os campos."}), 400
    if len(senha) < 6:
        return jsonify({"error": "A senha deve ter pelo menos 6 caracteres."}), 400
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return jsonify({"error": "E-mail inválido."}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Este e-mail já está cadastrado."}), 409
    user = User(nome=nome, email=email)
    user.set_senha(senha)
    db.session.add(user)
    db.session.commit()
    login_user(user, remember=True)
    return jsonify({"ok": True, "nome": user.nome})

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data  = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    senha = data.get("senha", "")
    user  = User.query.filter_by(email=email).first()
    if not user or not user.check_senha(senha):
        return jsonify({"error": "E-mail ou senha incorretos."}), 401
    login_user(user, remember=True)
    return jsonify({"ok": True, "nome": user.nome})

@app.route("/api/auth/logout", methods=["POST"])
@login_required
def api_logout():
    logout_user()
    return jsonify({"ok": True})

@app.route("/api/auth/me")
def api_me():
    if current_user.is_authenticated:
        return jsonify({"logado": True, "nome": current_user.nome, "email": current_user.email})
    return jsonify({"logado": False})

# ─── CARTEIRA ─────────────────────────────────────────────────────────────────
@app.route("/api/carteira", methods=["GET"])
@login_required
def get_carteira():
    ativos = Ativo.query.filter_by(user_id=current_user.id).all()
    return jsonify([{"symbol": a.symbol, "tipo": a.tipo, "qty": a.qty, "pm": a.pm} for a in ativos])

@app.route("/api/carteira", methods=["POST"])
@login_required
def add_carteira():
    data   = request.get_json() or {}
    symbol = data.get("symbol", "").upper().strip()
    tipo   = data.get("tipo", "acao")
    qty    = float(data.get("qty", 0))
    pm     = float(data.get("pm", 0))
    if not symbol:
        return jsonify({"error": "symbol obrigatório"}), 400
    ativo = Ativo.query.filter_by(user_id=current_user.id, symbol=symbol).first()
    if ativo:
        ativo.qty = qty; ativo.pm = pm; ativo.tipo = tipo
    else:
        ativo = Ativo(user_id=current_user.id, symbol=symbol, tipo=tipo, qty=qty, pm=pm)
        db.session.add(ativo)
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/carteira/<symbol>", methods=["DELETE"])
@login_required
def del_carteira(symbol):
    ativo = Ativo.query.filter_by(user_id=current_user.id, symbol=symbol.upper()).first()
    if ativo:
        db.session.delete(ativo)
        db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/carteira/<symbol>", methods=["PATCH"])
@login_required
def update_carteira(symbol):
    data  = request.get_json() or {}
    ativo = Ativo.query.filter_by(user_id=current_user.id, symbol=symbol.upper()).first()
    if not ativo:
        return jsonify({"error": "não encontrado"}), 404
    if "qty" in data: ativo.qty = float(data["qty"])
    if "pm"  in data: ativo.pm  = float(data["pm"])
    db.session.commit()
    return jsonify({"ok": True})

# ─── MERCADO ──────────────────────────────────────────────────────────────────
@app.route("/api/quotes")
@login_required
def get_quotes():
    symbols_raw = request.args.get("symbols", "")
    if not symbols_raw:
        return jsonify({"error": "symbols obrigatório"}), 400
    symbols   = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
    cache_key = "quotes_" + "_".join(sorted(symbols))
    cached    = cache_get(cache_key, ttl=90)
    if cached:
        return jsonify(cached)
    results = []
    for sym in symbols:
        try:
            t = yf.Ticker(b3_symbol(sym))
            d = parse_ticker(t, sym)
            if d:
                results.append(d)
                print(f"  [{sym}] R${d['regularMarketPrice']}")
        except Exception as e:
            print(f"  [{sym}] erro: {e}")
    cache_set(cache_key, results)
    return jsonify(results)

@app.route("/api/search/<symbol>")
@login_required
def search_symbol(symbol):
    sym       = symbol.upper().strip()
    cache_key = f"search_{sym}"
    cached    = cache_get(cache_key, ttl=300)
    if cached:
        return jsonify(cached)
    try:
        t = yf.Ticker(b3_symbol(sym))
        d = parse_ticker(t, sym)
        r = {"found": True, "data": d} if d and d.get("regularMarketPrice") else {"found": False}
        cache_set(cache_key, r)
        return jsonify(r)
    except:
        return jsonify({"found": False})

@app.route("/api/index/<path:symbol>")
@login_required
def get_index(symbol):
    sym       = symbol.upper()
    cache_key = f"idx_{sym}"
    cached    = cache_get(cache_key, ttl=120)
    if cached:
        return jsonify(cached)
    try:
        t = yf.Ticker(sym)
        d = parse_ticker(t, sym)
        if d:
            cache_set(cache_key, d)
            return jsonify(d)
        return jsonify({"error": "não encontrado"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/history/<symbol>")
@login_required
def get_history(symbol):
    sym       = symbol.upper().strip()
    period    = request.args.get("period", "1mo")
    cache_key = f"hist_{sym}_{period}"
    cached    = cache_get(cache_key, ttl=300)
    if cached:
        return jsonify(cached)
    try:
        t    = yf.Ticker(b3_symbol(sym))
        hist = t.history(period=period, interval="1d")
        if hist.empty:
            return jsonify([])
        data = []
        for idx, row in hist.iterrows():
            try:
                data.append({
                    "date":  str(idx.date()),
                    "close": round(float(row["Close"]), 2),
                    "open":  round(float(row["Open"]),  2),
                    "high":  round(float(row["High"]),  2),
                    "low":   round(float(row["Low"]),   2),
                    "vol":   int(row["Volume"]),
                })
            except:
                continue
        cache_set(cache_key, data)
        return jsonify(data)
    except Exception as e:
        return jsonify([])

@app.route("/api/asset/<symbol>")
@login_required
def get_asset(symbol):
    sym       = symbol.upper().strip()
    cache_key = f"asset_{sym}"
    cached    = cache_get(cache_key, ttl=120)
    if cached:
        return jsonify(cached)
    try:
        t     = yf.Ticker(b3_symbol(sym))
        quote = parse_ticker(t, sym)
        if not quote:
            return jsonify({"error": "não encontrado"}), 404

        divs_raw = []
        try:
            divs = t.dividends
            if divs is not None and len(divs) > 0:
                cutoff = datetime.now() - timedelta(days=730)
                divs   = divs[divs.index >= cutoff.strftime("%Y-%m-%d")]
                for dt_idx, val in divs.items():
                    try:
                        dt = dt_idx.to_pydatetime() if hasattr(dt_idx, "to_pydatetime") else dt_idx
                        divs_raw.append({
                            "date":  dt.strftime("%d/%m/%Y"),
                            "value": round(float(val), 6),
                        })
                    except:
                        continue
                divs_raw.sort(key=lambda x: x["date"], reverse=True)
        except:
            pass

        div_projection = None
        try:
            div_h = get_dividend_history(t, sym)
            if div_h:
                div_projection = {
                    "freq_label":  div_h["freq_label"],
                    "avg_value":   div_h["avg_value"],
                    "last_value":  div_h["last_value"],
                    "months_paid": div_h["months_paid"],
                    "projected":   div_h["projected"][:6],
                }
        except:
            pass

        result = {**quote, "dividends": divs_raw[:12], "div_projection": div_projection}
        cache_set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/simulate", methods=["POST"])
@login_required
def simulate():
    body        = request.get_json() or {}
    symbols     = body.get("symbols", [])
    valor_total = float(body.get("valor_total", 0))
    dist        = body.get("distribuicao", "igual")
    pcts        = body.get("pct", {})

    if not symbols or valor_total <= 0:
        return jsonify({"error": "parâmetros inválidos"}), 400

    alloc = {}
    if dist == "igual":
        for s in symbols:
            alloc[s["sym"]] = valor_total / len(symbols)
    elif dist == "yield":
        ys = {}
        for s in symbols:
            c = cache_get(f"quotes_{s['sym']}", ttl=300)
            ys[s["sym"]] = (c[0].get("dividendYield") or 0.01) if c and isinstance(c, list) and c else 0.01
        ty = sum(ys.values())
        for s in symbols:
            alloc[s["sym"]] = valor_total * (ys[s["sym"]] / ty)
    else:
        tp = sum(float(pcts.get(s["sym"], 0)) for s in symbols)
        for s in symbols:
            alloc[s["sym"]] = valor_total * (float(pcts.get(s["sym"], 0)) / tp) if tp > 0 else valor_total / len(symbols)

    results = []
    for s in symbols:
        sym = s["sym"]; tipo = s["tipo"]; inv = alloc.get(sym, 0)
        ck  = f"sim_{sym}"; cached = cache_get(ck, ttl=300)
        if cached:
            quote, div_h = cached["quote"], cached["div_h"]
        else:
            try:
                t     = yf.Ticker(b3_symbol(sym))
                quote = parse_ticker(t, sym)
                div_h = get_dividend_history(t, sym)
                cache_set(ck, {"quote": quote, "div_h": div_h})
            except:
                quote = None; div_h = None
        if not quote:
            continue

        price = quote.get("regularMarketPrice") or 0
        cotas = int(inv // price) if price > 0 else 0
        real  = round(cotas * price, 2)

        if div_h and div_h.get("projected"):
            projected = [{**p, "value_total": round(p["value_cota"] * cotas, 2)} for p in div_h["projected"]]
            n_proj    = len(projected)
            men       = round(sum(p["value_total"] for p in projected) / n_proj, 2) if n_proj > 0 else 0
            anl       = round(sum(p["value_total"] for p in projected[:12]), 2)
            freq_label = div_h["freq_label"]
            last_value = div_h["last_value"]
        else:
            dy  = quote.get("dividendYield") or 0
            men = round(real * (dy / 100) / 12, 2)
            anl = round(real * (dy / 100), 2)
            freq_label = "—"; last_value = quote.get("lastDividendValue") or 0; projected = []

        results.append({
            "sym":             sym,
            "name":            quote.get("shortName") or sym,
            "tipo":            tipo,
            "price":           price,
            "cotas":           cotas,
            "investido":       real,
            "div_yield":       quote.get("dividendYield") or 0,
            "mensal_estimado": men,
            "anual_estimado":  anl,
            "freq_label":      freq_label,
            "last_value":      last_value,
            "projected":       projected,
        })

    tM = round(sum(r["mensal_estimado"] for r in results), 2)
    tA = round(sum(r["anual_estimado"]  for r in results), 2)
    tI = round(sum(r["investido"]       for r in results), 2)
    yM = round((tA / tI * 100) if tI > 0 else 0, 2)
    return jsonify({"results": results, "total_mensal": tM, "total_anual": tA, "total_inv": tI, "yield_medio": yM})

@app.route("/api/compound", methods=["POST"])
@login_required
def compound_interest():
    body          = request.get_json() or {}
    valor         = float(body.get("valor", 0))
    anos          = float(body.get("anos", 1))
    yield_anual   = float(body.get("yield_anual", 0))
    reinvestir    = bool(body.get("reinvestir", True))
    aporte_mensal = float(body.get("aporte_mensal", 0))

    if valor <= 0 or anos <= 0 or yield_anual <= 0:
        return jsonify({"error": "parâmetros inválidos"}), 400

    taxa_mensal = (1 + yield_anual / 100) ** (1 / 12) - 1
    meses       = int(anos * 12)
    saldo       = valor
    total_div   = 0.0
    total_inv   = valor
    timeline    = []

    for m in range(1, meses + 1):
        div_mes    = saldo * taxa_mensal
        total_div += div_mes
        if reinvestir:
            saldo += div_mes
        if aporte_mensal > 0:
            saldo     += aporte_mensal
            total_inv += aporte_mensal
        if m % 3 == 0 or m == meses:
            timeline.append({
                "mes":       m,
                "label":     f"{m//12}a {m%12}m" if m >= 12 else f"{m}m",
                "saldo":     round(saldo, 2),
                "dividendo": round(div_mes, 2),
            })

    return jsonify({
        "saldo_final":      round(saldo, 2),
        "total_investido":  round(total_inv, 2),
        "total_dividendos": round(total_div, 2),
        "ganho_liquido":    round(saldo - total_inv, 2),
        "yield_total_pct":  round((saldo / total_inv - 1) * 100, 2) if total_inv > 0 else 0,
        "timeline":         timeline,
    })

@app.route("/api/news")
@login_required
def get_news():
    cat       = request.args.get("categoria", "todas")
    cache_key = f"news_{cat}"
    cached    = cache_get(cache_key, ttl=300)
    if cached:
        return jsonify(cached)

    feeds = [
        {"url": "https://www.infomoney.com.br/feed/",           "fonte": "InfoMoney"},
        {"url": "https://exame.com/invest/feed/",               "fonte": "Exame Invest"},
        {"url": "https://valor.globo.com/rss/financas/feed.xml","fonte": "Valor Econômico"},
    ]
    kw = {
        "b3":       ["b3","ibovespa","ibov","ação","ações","bolsa","petrobras","bovespa"],
        "fiis":     ["fii","fundo imobiliário","fundos imobiliários","ifix"],
        "economia": ["selic","inflação","pib","juros","banco central","câmbio","dólar","ipca","copom"],
        "mundo":    ["fed","nasdaq","s&p","nyse","dow jones","china","europa","wall street"],
    }

    def det(t, d):
        txt = (t + " " + (d or "")).lower()
        for c, ks in kw.items():
            if any(k in txt for k in ks):
                return c
        return "economia"

    def tr(pub):
        try:
            from email.utils import parsedate_to_datetime
            dt   = parsedate_to_datetime(pub)
            diff = int((datetime.now(dt.tzinfo) - dt).total_seconds() / 60)
            if diff < 1:    return "agora"
            if diff < 60:   return f"há {diff} min"
            if diff < 1440: return f"há {diff//60}h"
            return f"há {diff//1440} dias"
        except:
            return ""

    all_news = []
    for f in feeds:
        try:
            feed = feedparser.parse(f["url"])
            for e in feed.entries[:15]:
                titulo = e.get("title", "").strip()
                resumo = re.sub(r"<[^>]+>", "", e.get("summary", "")).strip()[:280]
                ci     = det(titulo, resumo)
                if cat != "todas" and ci != cat:
                    continue
                all_news.append({
                    "titulo":    titulo,
                    "resumo":    resumo,
                    "fonte":     f["fonte"],
                    "categoria": ci,
                    "url":       e.get("link", "#"),
                    "tempo":     tr(e.get("published", "")),
                })
        except:
            pass

    seen, unique = set(), []
    for n in all_news:
        k = n["titulo"][:50]
        if k not in seen:
            seen.add(k)
            unique.append(n)

    cache_set(cache_key, unique[:30])
    return jsonify(unique[:30])

@app.route("/api/clear-cache", methods=["POST"])
@login_required
def clear_cache_route():
    body = request.get_json() or {}
    sym  = body.get("symbol", "").upper()
    if sym:
        with _lock:
            keys_to_del = [k for k in _cache if sym in k]
            for k in keys_to_del:
                del _cache[k]
    return jsonify({"ok": True})

if __name__ == "__main__":
    print("=" * 50)
    print("  B3 Terminal — http://localhost:5000")
    print("=" * 50)
    app.run(debug=False, host="0.0.0.0", port=5000)
