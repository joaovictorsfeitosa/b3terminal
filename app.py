import os, re, threading, time, calendar, requests as req_lib, csv
from io import StringIO
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
_db_url = os.environ.get("DATABASE_URL", "sqlite:////tmp/meridian.db")
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


# ── Base estática de dividendos B3 ───────────────────────────────────────────
# BRAPI free plan retorna null para dividendos — usamos base histórica confiável
# Fonte: B3 filings públicos e relatórios dos fundos | Atualizar trimestralmente
# dy = dividend yield anual como fração decimal (0.11 = 11%)
# last_div = último valor pago por cota/ação em R$
# freq = pagamentos por ano (12=mensal, 4=trimestral, 2=semestral, 1=anual)
_DIV_DB = {
    # ── FIIs (todos pagam mensalmente) ──────────────────────────────────────
    "MXRF11": {"last_div": 0.090, "dy": 0.110, "freq": 12},
    "KNRI11": {"last_div": 0.600, "dy": 0.080, "freq": 12},
    "HGLG11": {"last_div": 1.100, "dy": 0.085, "freq": 12},
    "XPML11": {"last_div": 0.780, "dy": 0.090, "freq": 12},
    "VISC11": {"last_div": 0.720, "dy": 0.088, "freq": 12},
    "BRCO11": {"last_div": 0.850, "dy": 0.082, "freq": 12},
    "BTLG11": {"last_div": 0.880, "dy": 0.095, "freq": 12},
    "GGRC11": {"last_div": 0.800, "dy": 0.100, "freq": 12},
    "BCFF11": {"last_div": 0.070, "dy": 0.120, "freq": 12},
    "CPTS11": {"last_div": 0.100, "dy": 0.130, "freq": 12},
    "KNCR11": {"last_div": 0.900, "dy": 0.105, "freq": 12},
    "KNIP11": {"last_div": 0.850, "dy": 0.115, "freq": 12},
    "RBRF11": {"last_div": 0.080, "dy": 0.115, "freq": 12},
    "RBRR11": {"last_div": 0.950, "dy": 0.108, "freq": 12},
    "HCTR11": {"last_div": 1.200, "dy": 0.120, "freq": 12},
    "VCRI11": {"last_div": 0.110, "dy": 0.125, "freq": 12},
    "RZTR11": {"last_div": 1.100, "dy": 0.118, "freq": 12},
    "TGAR11": {"last_div": 1.300, "dy": 0.122, "freq": 12},
    "XPLG11": {"last_div": 0.680, "dy": 0.088, "freq": 12},
    "LVBI11": {"last_div": 0.750, "dy": 0.092, "freq": 12},
    "VGIR11": {"last_div": 0.100, "dy": 0.128, "freq": 12},
    "HSML11": {"last_div": 0.520, "dy": 0.085, "freq": 12},
    "PVBI11": {"last_div": 0.620, "dy": 0.080, "freq": 12},
    "MALL11": {"last_div": 0.650, "dy": 0.088, "freq": 12},
    "HGBS11": {"last_div": 1.500, "dy": 0.078, "freq": 12},
    "ALZR11": {"last_div": 0.780, "dy": 0.090, "freq": 12},
    "GARE11": {"last_div": 0.090, "dy": 0.115, "freq": 12},
    "MGFF11": {"last_div": 0.060, "dy": 0.105, "freq": 12},
    "RBRP11": {"last_div": 0.400, "dy": 0.075, "freq": 12},
    "BPFF11": {"last_div": 0.550, "dy": 0.095, "freq": 12},
    "HGRU11": {"last_div": 1.050, "dy": 0.088, "freq": 12},
    "VINO11": {"last_div": 0.520, "dy": 0.082, "freq": 12},
    "TRXF11": {"last_div": 0.950, "dy": 0.105, "freq": 12},
    "VRTA11": {"last_div": 1.100, "dy": 0.112, "freq": 12},
    "URPR11": {"last_div": 0.120, "dy": 0.135, "freq": 12},
    "RECR11": {"last_div": 1.050, "dy": 0.118, "freq": 12},
    "MCCI11": {"last_div": 0.100, "dy": 0.130, "freq": 12},
    "CVBI11": {"last_div": 0.110, "dy": 0.128, "freq": 12},
    "VGHF11": {"last_div": 0.120, "dy": 0.145, "freq": 12},
    "HABT11": {"last_div": 1.150, "dy": 0.125, "freq": 12},
    "RECI11": {"last_div": 0.105, "dy": 0.122, "freq": 12},
    "PORD11": {"last_div": 0.090, "dy": 0.110, "freq": 12},
    "GTWR11": {"last_div": 0.720, "dy": 0.092, "freq": 12},
    "IRDM11": {"last_div": 1.050, "dy": 0.115, "freq": 12},
    "JSRE11": {"last_div": 0.550, "dy": 0.080, "freq": 12},
    "BCIA11": {"last_div": 0.650, "dy": 0.088, "freq": 12},
    "OUJP11": {"last_div": 0.950, "dy": 0.110, "freq": 12},
    "VSLH11": {"last_div": 0.680, "dy": 0.090, "freq": 12},
    "BLCP11": {"last_div": 0.800, "dy": 0.098, "freq": 12},
    "SNFF11": {"last_div": 0.900, "dy": 0.105, "freq": 12},
    # ── Ações ────────────────────────────────────────────────────────────────
    "TAEE4":  {"last_div": 0.900, "dy": 0.090, "freq": 4},
    "TAEE3":  {"last_div": 0.900, "dy": 0.090, "freq": 4},
    "TAEE11": {"last_div": 1.350, "dy": 0.090, "freq": 4},
    "ITUB4":  {"last_div": 0.220, "dy": 0.055, "freq": 12},
    "ITUB3":  {"last_div": 0.220, "dy": 0.055, "freq": 12},
    "BBDC4":  {"last_div": 0.180, "dy": 0.065, "freq": 12},
    "BBDC3":  {"last_div": 0.180, "dy": 0.065, "freq": 12},
    "BBAS3":  {"last_div": 0.850, "dy": 0.095, "freq": 4},
    "VALE3":  {"last_div": 2.500, "dy": 0.080, "freq": 2},
    "PETR4":  {"last_div": 0.800, "dy": 0.120, "freq": 4},
    "PETR3":  {"last_div": 0.800, "dy": 0.120, "freq": 4},
    "CXSE3":  {"last_div": 0.450, "dy": 0.055, "freq": 2},
    "KLBN4":  {"last_div": 0.300, "dy": 0.045, "freq": 2},
    "KLBN3":  {"last_div": 0.300, "dy": 0.045, "freq": 2},
    "KLBN11": {"last_div": 0.450, "dy": 0.045, "freq": 2},
    "LEVE3":  {"last_div": 0.850, "dy": 0.060, "freq": 4},
    "MRVE3":  {"last_div": 0.200, "dy": 0.040, "freq": 2},
    "POSI3":  {"last_div": 0.150, "dy": 0.035, "freq": 2},
    "ALPA4":  {"last_div": 0.500, "dy": 0.055, "freq": 2},
    "EGIE3":  {"last_div": 1.200, "dy": 0.075, "freq": 4},
    "TRPL4":  {"last_div": 1.800, "dy": 0.085, "freq": 2},
    "CPFE3":  {"last_div": 0.950, "dy": 0.065, "freq": 4},
    "CMIG4":  {"last_div": 0.400, "dy": 0.070, "freq": 4},
    "CMIG3":  {"last_div": 0.400, "dy": 0.070, "freq": 4},
    "WEGE3":  {"last_div": 0.200, "dy": 0.018, "freq": 4},
    "BBSE3":  {"last_div": 0.900, "dy": 0.075, "freq": 4},
    "CGAS5":  {"last_div": 1.500, "dy": 0.060, "freq": 4},
    "KEPL3":  {"last_div": 0.600, "dy": 0.055, "freq": 2},
    "SAPR11": {"last_div": 0.350, "dy": 0.065, "freq": 4},
    "SBSP3":  {"last_div": 1.100, "dy": 0.055, "freq": 2},
    "ENGI11": {"last_div": 0.550, "dy": 0.072, "freq": 4},
    "CMIN3":  {"last_div": 0.400, "dy": 0.080, "freq": 2},
    "PRIO3":  {"last_div": 0.200, "dy": 0.025, "freq": 2},
    "RDOR3":  {"last_div": 0.150, "dy": 0.012, "freq": 2},
    "RENT3":  {"last_div": 0.120, "dy": 0.015, "freq": 4},
    "RADL3":  {"last_div": 0.450, "dy": 0.020, "freq": 2},
    "RAIZ4":  {"last_div": 0.300, "dy": 0.050, "freq": 2},
    "HAPV3":  {"last_div": 0.100, "dy": 0.020, "freq": 2},
    "VIVT3":  {"last_div": 0.650, "dy": 0.055, "freq": 2},
    "TIMS3":  {"last_div": 0.500, "dy": 0.060, "freq": 2},
    "BEEF3":  {"last_div": 0.300, "dy": 0.045, "freq": 2},
    "JBSS3":  {"last_div": 0.500, "dy": 0.040, "freq": 2},
    "SMTO3":  {"last_div": 0.800, "dy": 0.055, "freq": 2},
    "CSNA3":  {"last_div": 0.600, "dy": 0.070, "freq": 2},
    "GGBR4":  {"last_div": 0.800, "dy": 0.085, "freq": 2},
    "USIM5":  {"last_div": 0.300, "dy": 0.060, "freq": 2},
    "SUZB3":  {"last_div": 0.700, "dy": 0.040, "freq": 2},
    "IRBR3":  {"last_div": 0.050, "dy": 0.025, "freq": 4},
    "FLRY3":  {"last_div": 0.180, "dy": 0.030, "freq": 4},
    "HAPV3":  {"last_div": 0.100, "dy": 0.020, "freq": 2},
    "BRAP4":  {"last_div": 1.200, "dy": 0.090, "freq": 2},
    "SANB11": {"last_div": 0.400, "dy": 0.065, "freq": 4},
    "COGN3":  {"last_div": 0.050, "dy": 0.020, "freq": 2},
    "MRFG3":  {"last_div": 0.200, "dy": 0.035, "freq": 2},
    "ABEV3":  {"last_div": 0.200, "dy": 0.055, "freq": 4},
    "CCRO3":  {"last_div": 0.300, "dy": 0.045, "freq": 2},
    "ECOR3":  {"last_div": 0.100, "dy": 0.020, "freq": 2},
    "EQTL3":  {"last_div": 0.400, "dy": 0.035, "freq": 2},
    "TOTS3":  {"last_div": 0.200, "dy": 0.025, "freq": 4},
    "INTB3":  {"last_div": 0.150, "dy": 0.030, "freq": 2},
}

BRAPI_TOKEN = os.environ.get("BRAPI_TOKEN", "")
BRAPI_BASE  = "https://brapi.dev/api"

CORS(app, origins=["https://b3terminal.onrender.com","http://localhost:5000"], supports_credentials=True)
db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view    = "login_page"
login_manager.login_message = ""

with app.app_context():
    # SQLAlchemy cria todas as tabelas definidas nos models (incluindo user_data)
    db.create_all()
    print("  DB: todas as tabelas verificadas/criadas pelo SQLAlchemy.")
    # Migration: adiciona colunas novas em tabelas já existentes (não quebra se já existir)
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
                    conn.rollback()  # coluna já existe — ignorar
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
        if r.status_code in (400, 403, 422, 429):
            print(f"  BRAPI {path}: HTTP {r.status_code} (plano sem suporte ou token inválido)")
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  BRAPI {path}: {e}"); return None

def brapi_quotes(symbols):
    syms_clean = [s.upper().replace(".SA","") for s in symbols]
    # Cache key: hash long lists to avoid oversized keys
    syms_str = ",".join(syms_clean)
    ck = f"bq_{syms_str}" if len(syms_str) < 120 else f"bq_{hash(syms_str)}"
    cached = cache_get(ck, ttl=300)
    if cached: return cached
    # BRAPI free plan: batches of 5 are reliable with fundamental=true
    BATCH = 5
    all_results = []
    for i in range(0, len(syms_clean), BATCH):
        batch = syms_clean[i:i+BATCH]
        data_batch = brapi_get(f"/quote/{','.join(batch)}", {"fundamental": "true"})
        if data_batch and "results" in data_batch:
            all_results.extend(data_batch["results"])
        else:
            # fallback sem fundamental se der erro
            data_batch = brapi_get(f"/quote/{','.join(batch)}")
            if data_batch and "results" in data_batch:
                all_results.extend(data_batch["results"])
    # Build unified data structure
    data = {"results": all_results} if all_results else None
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
            "lastDividendDate":           r.get("lastDividendDate"),
            "exDividendDate":             r.get("exDividendDate"),
            "priceEarnings":              r.get("priceEarnings"),
            "priceToBook":                r.get("priceToBook"),
            "earningsPerShare":           r.get("earningsPerShare"),
            "roe":                        r.get("returnOnEquity"),
            "returnOnEquity":             r.get("returnOnEquity"),
            "debtToEquity":               r.get("debtToEquity"),
            "currentRatio":               r.get("currentRatio"),
            "revenueGrowth":              r.get("revenueGrowth"),
            "grossMargins":               r.get("grossMargins"),
            "exDividendDate_br":          None,
            "lastDividendDate_br":        None,
        })
    cache_set(ck, results)
    # Alimenta cache individual qm_{sym} — reutilizado pelo heatmap sem chamar BRAPI de novo
    for r in results:
        sym = r.get("symbol","")
        if sym:
            cache_set(f"qm_{sym}", {
                "symbol": sym,
                "price":  round(float(r.get("regularMarketPrice")         or 0), 2),
                "change": round(float(r.get("regularMarketChangePercent") or 0), 2),
                "name":   (r.get("shortName") or sym)[:24],
                "volume": int(r.get("regularMarketVolume") or 0),
            })
    return results

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

def _is_fii(symbol):
    """FIIs brasileiros terminam em 11 (HGLG11, MXRF11, XPML11...)."""
    return bool(re.match(r'^[A-Z]{4}11$', symbol.upper().replace(".SA","")))

def _build_div_result(sym, payments, freq_label, freq_months, cotas=1, declared_future=None):
    """Monta projeção e retorna dict padronizado."""
    if not payments and not declared_future:
        return None

    hist_payments = [p for p in (payments or []) if not p.get("declared")]

    month_avgs = {}
    for m in freq_months:
        vals = [p["value"] for p in hist_payments if p["month"] == m]
        if vals:
            month_avgs[m] = round(sum(vals) / len(vals), 6)

    avg_value = round(sum(p["value"] for p in hist_payments) / len(hist_payments), 6) if hist_payments else (
        (declared_future[0]["value"] if declared_future else 0)
    )
    last_val = hist_payments[-1]["value"] if hist_payments else (
        declared_future[0]["value"] if declared_future else 0
    )

    today     = date.today()
    projected = []
    seen_proj = set()

    # ── 1. Pagamentos DECLARADOS (data real, oficial) ─────────────────────────
    for dp in (declared_future or []):
        try:
            pd_ = date(dp["year"], dp["month"], dp["day"])
            k   = f"{dp['year']}-{dp['month']:02d}"
            if pd_ >= today and k not in seen_proj:
                seen_proj.add(k)
                projected.append({
                    "date_str":    pd_.strftime("%d/%m/%Y"),
                    "month_name":  pd_.strftime("%b/%Y"),
                    "value_cota":  round(dp["value"], 6),
                    "value_total": round(dp["value"] * cotas, 2),
                    "is_next":     False,
                    "confirmed":   True,
                })
        except Exception:
            pass

    # ── 2. Projeções estimadas por padrão histórico ───────────────────────────
    if avg_value > 0:
        for i in range(18):
            future = today + relativedelta(months=i)
            if future.month not in freq_months:
                continue
            k = f"{future.year}-{future.month:02d}"
            if k in seen_proj:
                continue
            proj_val = month_avgs.get(future.month, avg_value)
            if proj_val <= 0:
                continue
            hm      = [p for p in hist_payments if p["month"] == future.month]
            avg_day = int(sum(p["day"] for p in hm) / len(hm)) if hm else 15
            avg_day = min(avg_day, calendar.monthrange(future.year, future.month)[1])
            pd_     = date(future.year, future.month, avg_day)
            if pd_ >= today:
                seen_proj.add(k)
                projected.append({
                    "date_str":    pd_.strftime("%d/%m/%Y"),
                    "month_name":  pd_.strftime("%b/%Y"),
                    "value_cota":  round(proj_val, 6),
                    "value_total": round(proj_val * cotas, 2),
                    "is_next":     False,
                    "confirmed":   False,
                })

    # Ordena por data e marca próximo
    def _date_key(p):
        parts = p["date_str"].split("/")
        return (parts[2], parts[1], parts[0])
    projected.sort(key=_date_key)
    if projected:
        projected[0]["is_next"] = True
    projected = projected[:12]

    return {
        "sym":              sym,
        "freq_label":       freq_label,
        "freq_months":      freq_months,
        "avg_value":        avg_value,
        "last_value":       last_val,
        "months_paid":      sorted(set(p["month"] for p in hist_payments)),
        "history":          hist_payments[-24:],
        "projected":        projected,
        "total_pagamentos": len(hist_payments),
        "has_declared":     bool(declared_future),
    }



def _get_sim_dividend_data(sym, price):
    """
    Busca dados de dividendos para o simulador.
    Prioridade: 1) Base estática _DIV_DB  2) BRAPI  3) TwelveData
    Retorna dict com: last_div, dy, div_rate, freq_est, freq_label
    """
    fii = _is_fii(sym)
    freq_est = 12 if fii else 2
    result = {
        "last_div": 0, "dy": 0, "div_rate": 0,
        "freq_est": freq_est,
        "freq_label": "Mensal" if fii else "Semestral"
    }

    # Fonte 1: Base estática — mais confiável (BRAPI free retorna null para dividendos)
    static = _DIV_DB.get(sym)
    if static:
        result["last_div"] = static["last_div"]
        result["dy"]       = static["dy"]
        result["freq_est"] = static["freq"]
        result["freq_label"] = "Mensal" if static["freq"] == 12 else (
            "Trimestral" if static["freq"] == 4 else (
            "Semestral" if static["freq"] == 2 else "Anual"))
        print(f"  SimData {sym} [static]: last_div={static['last_div']} dy={static['dy']:.3f} freq={static['freq']}")
        return result

    # Fonte 2: BRAPI (pode retornar null no plano free, mas tentamos)
    data = brapi_get(f"/quote/{sym}", {"fundamental": "true"}) or brapi_get(f"/quote/{sym}")
    if data and "results" in data and data["results"]:
        r = data["results"][0]
        dy_raw  = r.get("dividendYield")
        ldv_raw = r.get("lastDividendValue")
        rate_raw= r.get("dividendRate")
        if dy_raw:
            dy = float(dy_raw)
            result["dy"] = dy / 100 if dy > 1 else dy
        if ldv_raw:
            result["last_div"] = float(ldv_raw)
        if rate_raw:
            result["div_rate"] = float(rate_raw)
        if any([dy_raw, ldv_raw, rate_raw]):
            print(f"  SimData {sym} [brapi]: dy={result['dy']:.4f} ldv={result['last_div']}")
            return result

    # Fonte 3: TwelveData /quote
    TD_KEY = os.environ.get("TWELVE_DATA_KEY", "")
    if TD_KEY and result["dy"] == 0 and result["last_div"] == 0:
        try:
            r2 = req_lib.get("https://api.twelvedata.com/quote",
                params={"symbol": sym, "exchange": "BVMF", "apikey": TD_KEY}, timeout=10)
            d2 = r2.json()
            if d2.get("status") != "error":
                dy2 = float(d2.get("dividend_yield") or 0)
                if dy2 > 0:
                    result["dy"] = dy2 / 100 if dy2 > 1 else dy2
                    print(f"  SimData {sym} [td]: dy={result['dy']:.4f}")
        except Exception as e:
            print(f"  TD {sym}: {e}")

    # Fallback: estima DY por preço de mercado se disponível
    if result["dy"] == 0 and result["last_div"] == 0 and price > 0:
        # FIIs brasileiros têm DY médio de ~10%; ações ~5%
        est_dy = 0.10 if fii else 0.05
        result["dy"] = est_dy
        print(f"  SimData {sym} [estimado]: dy={est_dy:.2f} (média mercado)")

    return result


def brapi_dividends(symbol, cotas=1):
    """
    Busca histórico de dividendos/rendimentos.
    Primário : Twelve Data (endpoint /dividends, histórico completo)
    Fallback : BRAPI (limitado a ~3 meses no plano free)
    """
    sym = symbol.upper().replace(".SA", "")
    fii = _is_fii(sym)
    ck  = f"div3_{sym}"

    # Cache: guarda apenas payments + meta para reconstruir com qualquer cotas
    raw_cached = cache_get(ck, ttl=3600)
    if raw_cached:
        return _build_div_result(sym,
                                 raw_cached["payments"],
                                 raw_cached["freq_label"],
                                 raw_cached["freq_months"],
                                 cotas,
                                 declared_future=raw_cached.get("declared_future", []))

    payments     = []
    td_frequency = None  # frequência informada pela Twelve Data
    TD_KEY = os.environ.get("TWELVE_DATA_KEY", "")

    # ── 1. Twelve Data ────────────────────────────────────────────────────────
    if TD_KEY:
        try:
            r = req_lib.get(
                "https://api.twelvedata.com/dividends",
                params={
                    "symbol":   sym,
                    "exchange": "BVMF",
                    "range":    "5y",
                    "apikey":   TD_KEY,
                },
                timeout=15,
            )
            data = r.json()
            if data.get("status") == "ok":
                for d in (data.get("dividends") or []):
                    try:
                        dt  = datetime.strptime(d["ex_dividend_date"][:10], "%Y-%m-%d")
                        val = float(d.get("amount") or 0)
                        if val <= 0:
                            continue
                        payments.append({
                            "year": dt.year, "month": dt.month, "day": dt.day,
                            "value": round(val, 6),
                            "date_str": dt.strftime("%d/%m/%Y"),
                        })
                    except Exception:
                        continue
                # Twelve Data fornece frequency: 1=anual 2=semestral 4=trimestral 12=mensal
                divs = data.get("dividends") or []
                if divs and divs[0].get("frequency"):
                    td_frequency = int(divs[0]["frequency"])
            else:
                print(f"  TwelveData dividends {sym}: {data.get('message','no data')}")
        except Exception as e:
            print(f"  TwelveData dividends error {sym}: {e}")

    # ── 2. BRAPI fallback ─────────────────────────────────────────────────────
    if not payments:
        try:
            data = brapi_get(f"/quote/{sym}",
                             {"dividends": "true", "modules": "defaultKeyStatistics"})
            if data and "results" in data and data["results"]:
                res0      = data["results"][0]
                cash_divs = (res0.get("dividendsData") or {}).get("cashDividends") or []
                for d in cash_divs:
                    try:
                        dt_str = (d.get("paymentDate") or d.get("lastDatePrior") or
                                  d.get("approvedOn")  or d.get("declaredDate") or
                                  d.get("date") or d.get("dataEx") or "")
                        if not dt_str:
                            continue
                        dt = None
                        for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y"]:
                            try:
                                dt = datetime.strptime(str(dt_str)[:10], fmt[:10])
                                break
                            except Exception:
                                pass
                        if not dt:
                            continue
                        val = float(d.get("rate") or d.get("value") or d.get("adjValue") or
                                    d.get("amount") or d.get("dividendValue") or 0)
                        if val <= 0:
                            continue
                        payments.append({
                            "year": dt.year, "month": dt.month, "day": dt.day,
                            "value": round(val, 6), "date_str": dt.strftime("%d/%m/%Y"),
                        })
                    except Exception:
                        pass

                # ── Dividendo futuro DECLARADO ────────────────────────────────
                # exDividendDate = data ex-dividendo já anunciada (unix ts)
                # dividendDate   = data de pagamento já anunciada  (unix ts)
                # Esses campos existem mesmo quando cashDividends está vazio
                try:
                    ex_ts  = res0.get("exDividendDate")
                    pay_ts = res0.get("dividendDate")
                    # Valor: usa lastDividendValue, dividendRate/frequência, ou 0
                    raw_val  = (res0.get("lastDividendValue") or
                                res0.get("dividendRate")      or
                                (res0.get("dividendsData") or {}).get("rate") or 0)
                    div_val  = float(raw_val or 0)

                    if ex_ts and div_val > 0:
                        # Prefere a data de pagamento; fallback para ex + 15 dias
                        if pay_ts:
                            pay_dt = datetime.fromtimestamp(int(pay_ts))
                        else:
                            pay_dt = datetime.fromtimestamp(int(ex_ts)) + timedelta(days=15)

                        # Adiciona se for futura ou até 60 dias no passado (pode não estar no cashDividends)
                        if (pay_dt - datetime.utcnow()).days > -60:
                            payments.append({
                                "year":     pay_dt.year,
                                "month":    pay_dt.month,
                                "day":      pay_dt.day,
                                "value":    round(div_val, 6),
                                "date_str": pay_dt.strftime("%d/%m/%Y"),
                                "declared": True,   # pagamento oficialmente anunciado
                            })
                except Exception:
                    pass

                # Tenta lastDividendValue se ainda sem nada
                if not payments:
                    ldd = res0.get("lastDividendDate")
                    ldv = float(res0.get("lastDividendValue") or 0)
                    if ldv > 0 and ldd:
                        try:
                            dt = (datetime.fromtimestamp(ldd)
                                  if isinstance(ldd, (int, float))
                                  else datetime.strptime(str(ldd)[:10], "%Y-%m-%d"))
                            payments.append({
                                "year": dt.year, "month": dt.month, "day": dt.day,
                                "value": round(ldv, 6), "date_str": dt.strftime("%d/%m/%Y"),
                            })
                        except Exception:
                            pass
        except Exception as e:
            print(f"  BRAPI dividends fallback {sym}: {e}")

    if not payments:
        return None

    # ── Separar pagamentos futuros DECLARADOS dos históricos ──────────────────
    today_dt  = datetime.utcnow()
    declared_future = [
        p for p in payments
        if p.get("declared") and
        datetime(p["year"], p["month"], p["day"]) >= today_dt
    ]
    historical = [p for p in payments if not p.get("declared")]

    # Combina: histórico primeiro, declarados depois (para não distorcer freq)
    all_payments = historical + declared_future

    # ── Deduplicar e ordenar ──────────────────────────────────────────────────
    all_payments.sort(key=lambda x: (x["year"], x["month"], x["day"]))
    seen_ym, unique = set(), []
    for p in all_payments:
        k = f"{p['year']}-{p['month']:02d}"
        if k not in seen_ym:
            seen_ym.add(k)
            unique.append(p)
    payments = unique

    # ── Detectar frequência ───────────────────────────────────────────────────
    hist_only = [p for p in payments if not p.get("declared")]
    if td_frequency:
        _freq_map = {
            1:  ("Anual",       [12]),
            2:  ("Semestral",   [6, 12]),
            4:  ("Trimestral",  [3, 6, 9, 12]),
            12: ("Mensal",      list(range(1, 13))),
        }
        freq_label, freq_months = _freq_map.get(td_frequency, ("Mensal", list(range(1, 13))))
    elif fii:
        freq_label, freq_months = "Mensal", list(range(1, 13))
    elif len(hist_only) >= 2:
        recent      = hist_only[-24:]
        months_paid = sorted(set(p["month"] for p in recent))
        n           = len(months_paid)
        if   n >= 10: freq_label, freq_months = "Mensal",      list(range(1, 13))
        elif n >= 4:  freq_label, freq_months = "Trimestral",  [3, 6, 9, 12]
        elif n >= 2:  freq_label, freq_months = "Semestral",   [6, 12]
        else:         freq_label, freq_months = "Irregular",   months_paid or [12]
    else:
        # Poucos dados históricos — usa mês do pagamento declarado se houver
        if declared_future:
            freq_months = [declared_future[0]["month"]]
        elif hist_only:
            freq_months = [hist_only[-1]["month"]]
        else:
            freq_months = [12]
        freq_label = "Irregular"

    # Salva cache sem cotas
    cache_set(ck, {
        "payments":        payments,
        "freq_label":      freq_label,
        "freq_months":     freq_months,
        "declared_future": declared_future,
    })

    return _build_div_result(sym, payments, freq_label, freq_months, cotas,
                             declared_future=declared_future)




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
    # Garante que apenas o email proprietário ou is_admin=True acessa
    if not current_user.is_authenticated or not current_user.admin:
        return redirect(url_for("index"))
    return render_template("admin.html")

@app.route("/api/ai/status")
@login_required
def ai_status():
    """Diagnóstico rápido da configuração da IA (só para admins)."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    configured = bool(key)
    key_preview = (key[:8] + "..." + key[-4:]) if len(key) > 12 else ("configurada" if key else "NÃO configurada")
    return jsonify({
        "configured": configured,
        "key_preview": key_preview,
        "model": "claude-sonnet-4-20250514",
        "tip": "" if configured else "Adicione ANTHROPIC_API_KEY nas variáveis de ambiente do Render."
    })

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

# ── Chart period config ───────────────────────────────────────────────────────
# interval : Twelve Data interval string
# td_out   : outputsize (number of data points to request)
# ttl      : cache lifetime in seconds
CHART_PERIOD_MAP = {
    "1mo":  {"interval": "1day",   "td_out": 35,    "ttl": 600},
    "3mo":  {"interval": "1day",   "td_out": 95,    "ttl": 600},
    "6mo":  {"interval": "1day",   "td_out": 185,   "ttl": 900},
    "1y":   {"interval": "1day",   "td_out": 365,   "ttl": 1800},
    "5y":   {"interval": "1week",  "td_out": 265,   "ttl": 3600},
    "max":  {"interval": "1month", "td_out": 5000,  "ttl": 7200},
}

TWELVE_DATA_BASE = "https://api.twelvedata.com"

def twelvedata_chart(symbol, period="1mo"):
    """
    Fetch OHLCV history from Twelve Data (primary source).
    Brazilian stocks use exchange=BVMF.
    Returns list of {time, open, high, low, close, volume} dicts.
    """
    TD_KEY = os.environ.get("TWELVE_DATA_KEY", "")
    if not TD_KEY:
        return []

    sym = symbol.upper().replace(".SA", "")
    cfg = CHART_PERIOD_MAP.get(period, CHART_PERIOD_MAP["1mo"])
    ck  = f"td_{sym}_{period}"

    cached = cache_get(ck, ttl=cfg["ttl"])
    if cached:
        return cached

    try:
        r = req_lib.get(
            f"{TWELVE_DATA_BASE}/time_series",
            params={
                "symbol":     sym,
                "exchange":   "BVMF",
                "interval":   cfg["interval"],
                "outputsize": cfg["td_out"],
                "order":      "ASC",
                "apikey":     TD_KEY,
            },
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()

        # Twelve Data returns {"code":400,...} for unknown symbols
        if data.get("code") or data.get("status") == "error":
            msg = data.get("message", "unknown")
            print(f"  TwelveData {sym} ({period}): {msg}")
            return []

        values = data.get("values") or []
        if not values:
            print(f"  TwelveData {sym} ({period}): empty values")
            return []

        out, seen = [], set()
        for v in values:
            try:
                date_str = (v.get("datetime") or "")[:10]  # keep YYYY-MM-DD
                if not date_str or date_str in seen:
                    continue
                cl = float(v.get("close") or 0)
                if cl <= 0:
                    continue
                op = float(v.get("open")   or 0) or cl
                hi = float(v.get("high")   or 0) or cl
                lo = float(v.get("low")    or 0) or cl
                vol = int(float(v.get("volume") or 0))
                seen.add(date_str)
                out.append({
                    "time":   date_str,
                    "open":   round(op, 2),
                    "high":   round(hi, 2),
                    "low":    round(lo, 2),
                    "close":  round(cl, 2),
                    "volume": vol,
                })
            except (ValueError, TypeError):
                continue

        out.sort(key=lambda x: x["time"])
        if out:
            cache_set(ck, out)
            print(f"  TwelveData {sym} ({period}): {len(out)} candles OK")
        return out

    except Exception as e:
        print(f"  TwelveData error {sym} ({period}): {e}")
        return []


def brapi_chart_fallback(symbol, period="1mo"):
    """
    BRAPI fallback — free tier limited to ~3mo but better than nothing.
    Only used when Twelve Data fails or key is missing.
    """
    sym = symbol.upper().replace(".SA", "")
    brapi_range    = {"1mo":"1mo","3mo":"3mo","6mo":"6mo","1y":"1y","5y":"5y","max":"max"}.get(period,"3mo")
    brapi_interval = "1mo" if period == "max" else ("1wk" if period == "5y" else "1d")
    data = brapi_get(f"/quote/{sym}", {"range": brapi_range, "interval": brapi_interval})
    out, seen = [], set()
    if data and "results" in data and data["results"]:
        for h in (data["results"][0].get("historicalDataPrice") or []):
            ts = h.get("date")
            if not ts:
                continue
            try:
                cl = float(h.get("close") or 0)
                if cl <= 0:
                    continue
                op = float(h.get("open")  or 0) or cl
                hi = float(h.get("high")  or 0) or cl
                lo = float(h.get("low")   or 0) or cl
                dt = datetime.utcfromtimestamp(int(ts))
                time_val = dt.strftime("%Y-%m-%d")
                if time_val in seen:
                    continue
                seen.add(time_val)
                out.append({
                    "time":   time_val,
                    "open":   round(op, 2),
                    "high":   round(hi, 2),
                    "low":    round(lo, 2),
                    "close":  round(cl, 2),
                    "volume": int(h.get("volume") or 0),
                })
            except Exception:
                continue
    out.sort(key=lambda x: x["time"])
    return out


@app.route("/api/chart/<symbol>")
@login_required
def get_chart(symbol):
    sym    = symbol.upper().strip().replace(".SA", "")
    period = request.args.get("period", "1mo")
    if period not in CHART_PERIOD_MAP:
        period = "1mo"

    # 1st — Twelve Data (full history, cloud-IP friendly)
    data = twelvedata_chart(sym, period)

    # 2nd — BRAPI fallback (limited history but always available)
    if not data:
        print(f"  Falling back to BRAPI for {sym} ({period})")
        data = brapi_chart_fallback(sym, period)

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
    body        = request.get_json() or {}
    symbols     = body.get("symbols", [])
    valor_total = float(body.get("valor_total", 0))
    dist        = body.get("distribuicao", "igual")
    pcts        = body.get("pct", {})

    if not symbols or valor_total <= 0:
        return jsonify({"error": "parâmetros inválidos"}), 400

    # ── Alocação por ativo ────────────────────────────────────────────────────
    alloc = {}
    if dist == "igual":
        for s in symbols:
            alloc[s["sym"]] = valor_total / len(symbols)
    elif dist == "yield":
        ys = {}
        for s in symbols:
            c = cache_get(f"quotes_{s['sym']}", ttl=300)
            ys[s["sym"]] = (c[0].get("dividendYield") or 0.01) if (c and isinstance(c, list)) else 0.01
        ty = sum(ys.values()) or 1
        for s in symbols:
            alloc[s["sym"]] = valor_total * (ys[s["sym"]] / ty)
    else:
        tp = sum(float(pcts.get(s["sym"], 0)) for s in symbols) or 1
        for s in symbols:
            alloc[s["sym"]] = valor_total * (float(pcts.get(s["sym"], 0)) / tp)

    all_syms   = [s["sym"].upper().replace(".SA","") for s in symbols]
    quotes_map = {q["symbol"]: q for q in (brapi_quotes(all_syms) or [])}

    results = []
    for s in symbols:
        sym   = s["sym"].upper().replace(".SA", "")
        tipo  = s.get("tipo", "AÇÃO")
        inv   = alloc.get(s["sym"], 0)
        quote = quotes_map.get(sym)
        if not quote:
            continue

        price = quote.get("regularMarketPrice") or 0
        cotas = int(inv // price) if price > 0 else 0
        real  = round(cotas * price, 2)
        fii   = _is_fii(sym)

        div_h = brapi_dividends(sym, cotas)

        if div_h and div_h.get("projected"):
            projected  = [{**p, "value_total": round(p["value_cota"] * cotas, 2)}
                          for p in div_h["projected"]]
            freq_label = div_h["freq_label"]
            last_value = div_h["last_value"]
            avg_cota   = div_h["avg_value"]

            # Mensal estimado: usa último valor pago (mais recente e preciso)
            freq_months = div_h["freq_months"]
            n_freq      = len(freq_months)      # pagamentos por ano
            # Prioriza last_value (último histórico real), cai para avg_cota se inválido
            base_cota   = last_value if last_value > 0 else avg_cota
            anl         = round(base_cota * cotas * n_freq, 2)
            men         = round(anl / 12, 2)    # sempre normalizado p/ mês

        else:
            # Sem histórico completo — busca dados via helper multi-fonte
            sd = _get_sim_dividend_data(sym, price)
            last_div  = sd["last_div"]
            dy        = sd["dy"]        # já normalizado como fração decimal
            div_rate  = sd["div_rate"]
            freq_est  = sd["freq_est"]
            freq_label = sd["freq_label"]

            if last_div > 0 and cotas > 0:
                anl = round(last_div * cotas * freq_est, 2)
            elif div_rate > 0:
                anl = round(div_rate * cotas, 2)
            elif dy > 0 and real > 0:
                # dy é fração decimal: 0.1020 = 10.20% ao ano
                anl = round(real * dy, 2)
            else:
                anl = 0

            men        = round(anl / 12, 2)
            if anl == 0:
                freq_label = "—"
            last_value = last_div if last_div > 0 else (round(anl / (cotas * freq_est), 6) if cotas > 0 and anl > 0 else 0)
            avg_cota   = last_value
            projected  = []

        # n_freq: pagamentos reais por ano (12=mensal, 4=trimestral, 2=semestral, 1=anual)
        if div_h and div_h.get("projected"):
            _nfreq = len(div_h["freq_months"])
        else:
            _nfreq = freq_est if 'freq_est' in dir() or True else 4
            try: _nfreq = freq_est
            except NameError: _nfreq = 4

        results.append({
            "sym":              sym,
            "name":             quote.get("shortName") or sym,
            "tipo":             "FII" if fii else tipo,
            "price":            price,
            "cotas":            cotas,
            "investido":        real,
            "div_yield":        quote.get("dividendYield") or 0,
            "freq_label":       freq_label,
            "n_freq":           _nfreq,
            "last_value":       last_value,
            "avg_value_cota":   avg_cota,
            "mensal_estimado":  men,
            "anual_estimado":   anl,
            "projected":        projected,
        })

    tM  = round(sum(r["mensal_estimado"] for r in results), 2)
    tA  = round(sum(r["anual_estimado"]  for r in results), 2)
    tI  = round(sum(r["investido"]        for r in results), 2)
    yM  = round((tA / tI * 100) if tI > 0 else 0, 2)

    return jsonify({
        "results":       results,
        "total_mensal":  tM,
        "total_anual":   tA,
        "total_inv":     tI,
        "yield_medio":   yM,
    })


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

@app.route("/api/news/symbol/<symbol>")
@login_required
def get_news_symbol(symbol):
    """Busca notícias relacionadas a um ativo específico via RSS"""
    sym = symbol.upper().replace(".SA","")
    ck = f"news_sym_{sym}"
    cached = cache_get(ck, ttl=300)
    if cached: return jsonify(cached)

    # Mapa de nomes para busca em títulos
    sym_names = {
        "PETR4":"Petrobras","PETR3":"Petrobras","VALE3":"Vale","ITUB4":"Itaú",
        "ITUB3":"Itaú","BBDC4":"Bradesco","BBDC3":"Bradesco","BBAS3":"Banco do Brasil",
        "WEGE3":"WEG","ABEV3":"Ambev","MGLU3":"Magazine Luiza","RENT3":"Localiza",
        "PRIO3":"PRIO","CXSE3":"Caixa Seguridade","COGN3":"Cogna","EGIE3":"Engie",
        "TAEE4":"Taesa","TAEE3":"Taesa","CMIG4":"Cemig","CPFE3":"CPFL","SBSP3":"Sabesp",
        "VIVT3":"Vivo","TIMS3":"TIM","JBSS3":"JBS","MRFG3":"Marfrig",
        "BEEF3":"Minerva","SUZB3":"Suzano","CSNA3":"CSN","GGBR4":"Gerdau",
        "USIM5":"Usiminas","RDOR3":"Rede D'Or","HAPV3":"Hapvida",
    }
    search_terms = [sym.lower()]
    if sym in sym_names:
        search_terms.append(sym_names[sym].lower())

    def tr(pub):
        try:
            from email.utils import parsedate_to_datetime
            dt=parsedate_to_datetime(pub); diff=int((datetime.now(dt.tzinfo)-dt).total_seconds()/60)
            if diff<1: return "agora"
            if diff<60: return f"há {diff} min"
            if diff<1440: return f"há {diff//60}h"
            return f"há {diff//1440} dias"
        except: return ""

    results = []

    # Fonte 1: Google News RSS — busca direta pelo ticker + nome da empresa
    for term in search_terms:
        try:
            gn_url = f"https://news.google.com/rss/search?q={term}+bolsa+B3&hl=pt-BR&gl=BR&ceid=BR:pt-419"
            feed = feedparser.parse(gn_url)
            for e in (feed.entries or [])[:10]:
                title = e.get("title","")
                if not title: continue
                source = ""
                if hasattr(e, "source"): source = getattr(e.source, "title", "")
                results.append({
                    "title": title,
                    "url":   e.get("link",""),
                    "fonte": source or "Google News",
                    "time":  tr(e.get("published","")),
                    "desc":  "",
                })
        except: pass

    # Fonte 2: Feeds brasileiros — filtra por termos do ativo
    feeds = [
        {"url":"https://www.infomoney.com.br/feed/","fonte":"InfoMoney"},
        {"url":"https://exame.com/invest/feed/","fonte":"Exame Invest"},
        {"url":"https://valor.globo.com/rss/financas/feed.xml","fonte":"Valor Econômico"},
        {"url":"https://einvestidor.estadao.com.br/feed/","fonte":"E-Investidor"},
        {"url":"https://moneytimes.com.br/feed/","fonte":"Money Times"},
    ]
    for f in feeds:
        try:
            feed = feedparser.parse(f["url"])
            for e in (feed.entries or [])[:40]:
                title = e.get("title","")
                desc  = e.get("summary","") or ""
                txt   = (title + " " + desc).lower()
                if any(t in txt for t in search_terms):
                    # Avoid duplicates
                    if not any(r["title"] == title for r in results):
                        results.append({
                            "title": title,
                            "url":   e.get("link",""),
                            "fonte": f["fonte"],
                            "time":  tr(e.get("published","")),
                            "desc":  desc[:150] + "..." if len(desc)>150 else desc,
                        })
        except: pass

    # Limita e salva cache
    results = results[:12]
    cache_set(ck, results)
    return jsonify(results)


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
    if result:
        return jsonify({**result,"found":True})
    # Fallback: usa _get_sim_dividend_data para pelo menos retornar DY e último dividendo
    sd = _get_sim_dividend_data(sym, 0)
    fii = _is_fii(sym)
    if sd["last_div"] > 0 or sd["dy"] > 0:
        freq_label = sd["freq_label"]
        avg_val = sd["last_div"] if sd["last_div"] > 0 else 0
        return jsonify({
            "found": True,
            "projected": [],
            "freq_label": freq_label,
            "avg_value": avg_val,
            "last_value": sd["last_div"],
            "div_yield": sd["dy"],
            "freq_months": list(range(1,13)) if fii else [6,12],
        })
    return jsonify({"found":False,"projected":[],"freq_label":"—","avg_value":0})

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

@app.route("/api/ai/ping")
@login_required
def ai_ping():
    """Testa a conexão com a API Anthropic e retorna diagnóstico detalhado."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return jsonify({"ok": False, "erro": "ANTHROPIC_API_KEY não configurada"}), 503

    try:
        r = req_lib.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "ok"}],
            },
            timeout=30,
        )
        r.raise_for_status()
        return jsonify({
            "ok": True,
            "status": r.status_code,
            "modelo": "claude-sonnet-4-20250514",
            "key_inicio": key[:8] + "...",
        })
    except req_lib.exceptions.HTTPError as e:
        st = e.response.status_code if e.response else 0
        body = ""
        try: body = e.response.text[:500]
        except: pass
        return jsonify({"ok": False, "http_status": st, "resposta_api": body}), 200
    except Exception as e:
        return jsonify({"ok": False, "erro": f"{type(e).__name__}: {str(e)}"}), 200



# ── Heatmap — busca sequencial no backend, cache 10 min ──────────────────────
HEATMAP_SECTORS = {
    "Bancos":    ["ITUB4","BBDC4","BBAS3","SANB11","BPAC11"],
    "Petróleo":  ["PETR4","PRIO3","RECV3","UGPA3"],
    "Mineração": ["VALE3","GGBR4","CSNA3"],
    "Energia":   ["EGIE3","TAEE11","CPFE3","CMIG4"],
    "Varejo":    ["LREN3","MGLU3","NTCO3"],
    "Saúde":     ["RDOR3","HAPV3","FLRY3"],
    "Agro":      ["JBSS3","AGRO3","MRFG3"],
    "FIIs":      ["HGLG11","MXRF11","XPML11","KNRI11","MALL11"],
}

def _td_batch_quotes(symbols):
    return {}

def _fetch_sector_quotes(syms):
    """Busca cotações para heatmap — sem fundamental para ser mais rápido e confiável."""
    missing, result = [], {}
    for sym in syms:
        hit = cache_get(f"qm_{sym}", ttl=600)
        if hit:
            result[sym] = hit
        else:
            missing.append(sym)
    if missing:
        # Batches de 10 sem fundamental — mais rápido para heatmap
        BATCH = 10
        for i in range(0, len(missing), BATCH):
            batch = missing[i:i+BATCH]
            data = brapi_get(f"/quote/{','.join(batch)}")
            if data and "results" in data:
                for r in (data["results"] or []):
                    sym = r.get("symbol","")
                    if not sym: continue
                    item = {
                        "symbol": sym,
                        "price":  round(float(r.get("regularMarketPrice")         or 0), 2),
                        "change": round(float(r.get("regularMarketChangePercent") or 0), 2),
                        "name":   (r.get("shortName") or r.get("longName") or sym)[:24],
                        "volume": int(r.get("regularMarketVolume") or 0),
                    }
                    cache_set(f"qm_{sym}", item)
                    result[sym] = item
    return result


@app.route("/api/heatmap-all")
@login_required
def heatmap_all():
    """
    Endpoint único: busca TODOS os setores sequencialmente no servidor.
    Cache 10 minutos — o frontend faz UMA só chamada e espera a resposta.
    Sem concorrência, sem rate-limit.
    """
    ck = "hm_all_v5"
    cached = cache_get(ck, ttl=600)
    if cached:
        return jsonify(cached)

    # Busca todos os símbolos de uma vez — mais eficiente
    all_syms = []
    for syms in HEATMAP_SECTORS.values():
        all_syms.extend(syms)
    all_syms = list(dict.fromkeys(all_syms))  # deduplica mantendo ordem

    # Busca em batches de 10 sem fundamental
    BATCH = 10
    all_quotes = {}
    for i in range(0, len(all_syms), BATCH):
        batch = all_syms[i:i+BATCH]
        data = brapi_get(f"/quote/{','.join(batch)}")
        if data and "results" in data:
            for r in (data["results"] or []):
                sym = r.get("symbol","")
                if not sym: continue
                all_quotes[sym] = {
                    "symbol": sym,
                    "price":  round(float(r.get("regularMarketPrice")         or 0), 2),
                    "change": round(float(r.get("regularMarketChangePercent") or 0), 2),
                    "name":   (r.get("shortName") or r.get("longName") or sym)[:24],
                    "volume": int(r.get("regularMarketVolume") or 0),
                }

    out = {}
    for name, syms in HEATMAP_SECTORS.items():
        out[name] = [all_quotes[s] for s in syms if s in all_quotes]

    total = sum(len(v) for v in out.values())
    print(f"  [Heatmap] carregado: {total}/{sum(len(v) for v in HEATMAP_SECTORS.values())} símbolos")
    if total > 0:
        cache_set(ck, out)
    return jsonify(out)


# Mantido para compatibilidade com versões antigas
@app.route("/api/heatmap-sector")
@login_required
def heatmap_sector():
    raw  = request.args.get("syms", "")
    syms = [s.strip().upper().replace(".SA","") for s in raw.split(",") if s.strip()][:6]
    if not syms:
        return jsonify([])
    qmap = _fetch_sector_quotes(syms)
    return jsonify(list(qmap.values()))


@app.route("/api/heatmap")
@login_required
def get_heatmap():
    return jsonify({"sectors": [], "updated": datetime.utcnow().strftime("%H:%M")})




# ── Score de Saúde da Carteira ────────────────────────────────────────────────
_SECTOR_MAP = {
    # Bancos & Financeiro
    "ITUB4":"Bancos","BBDC4":"Bancos","BBAS3":"Bancos","SANB11":"Bancos",
    "BPAC11":"Bancos","B3SA3":"Financeiro","CIEL3":"Financeiro","IRBR3":"Financeiro","BMGB4":"Bancos",
    # Energia
    "PETR4":"Petróleo","PETR3":"Petróleo","PRIO3":"Petróleo","RRRP3":"Petróleo",
    "RECV3":"Petróleo","UGPA3":"Petróleo","EGIE3":"Energia","ENGI11":"Energia",
    "CPFE3":"Energia","TAEE11":"Energia","CMIG4":"Energia","AURE3":"Energia","CPLE6":"Energia",
    # Mineração
    "VALE3":"Mineração","CSNA3":"Siderurgia","GGBR4":"Siderurgia","USIM5":"Siderurgia","BRAP4":"Mineração",
    # Varejo/Consumo
    "MGLU3":"Varejo","NTCO3":"Varejo","SOMA3":"Varejo","LREN3":"Varejo","ARZZ3":"Varejo","PETZ3":"Varejo",
    # Construção
    "MRVE3":"Construção","EZTC3":"Construção","CYRE3":"Construção","DIRR3":"Construção","TEND3":"Construção",
    # Saúde
    "RDOR3":"Saúde","HAPV3":"Saúde","FLRY3":"Saúde","DASA3":"Saúde",
    # Telecom
    "VIVT3":"Telecom","TIMS3":"Telecom",
    # Agro
    "AGRO3":"Agro","SLCE3":"Agro","CAML3":"Agro","JBSS3":"Agro","MRFG3":"Agro","BEEF3":"Agro","SMTO3":"Agro",
    # Tech
    "TOTVS3":"Tecnologia","POSI3":"Tecnologia","CASH3":"Tecnologia",
}

@app.route("/api/portfolio-score")
@login_required
def portfolio_score():
    ativos = Ativo.query.filter_by(user_id=current_user.id).all()
    if not ativos:
        return jsonify({"error": "Carteira vazia — adicione ativos na aba Carteira."}), 400

    syms    = [a.symbol for a in ativos]
    qty_map = {a.symbol: float(a.qty or 0) for a in ativos}
    pm_map  = {a.symbol: float(a.pm  or 0) for a in ativos}

    quotes = brapi_quotes(syms) or []
    qmap   = {q["symbol"]: q for q in quotes}

    # ── Dados por ativo ────────────────────────────────────────────────────────
    total_valor = 0.0
    ativos_info = []
    for sym in syms:
        q   = qmap.get(sym, {})
        price = float(q.get("regularMarketPrice") or 0)
        if price <= 0:
            price = pm_map.get(sym, 0)
        qty   = qty_map.get(sym, 0)
        valor = price * qty
        total_valor += valor
        dy        = float(q.get("dividendYield")     or 0)
        dy_rate   = float(q.get("dividendRate")      or 0)
        last_div  = float(q.get("lastDividendValue") or 0)
        fii       = _is_fii(sym)
        # Se BRAPI não retornou DY, usa base estática
        if dy == 0:
            static = _DIV_DB.get(sym)
            if static and static["dy"] > 0:
                dy = static["dy"] * 100  # converte para % (ex: 0.11 → 11.0)
        sector    = _SECTOR_MAP.get(sym, "FII" if fii else "Outros")
        # FIIs sempre pagam rendimentos; ações: usa qualquer campo disponível
        has_div   = fii or dy > 0 or dy_rate > 0 or last_div > 0 or sym in _DIV_DB
        ativos_info.append({
            "sym": sym, "valor": valor, "dy": dy,
            "sector": sector, "fii": fii,
            "has_div": has_div,
        })

    # Se ainda não tiver valor (ativos sem PM e sem cotação), usa qty como peso
    if total_valor <= 0:
        total_valor = sum(qty_map.values()) or 1
        for a in ativos_info:
            a["valor"] = qty_map.get(a["sym"], 1)

    # ── Score 1: Diversificação (0–25) ────────────────────────────────────────
    sectors_used = set(a["sector"] for a in ativos_info)
    n_sec        = len(sectors_used)
    n_ativ       = len(ativos_info)
    if   n_sec >= 6: sc_div = 25
    elif n_sec == 5: sc_div = 22
    elif n_sec == 4: sc_div = 18
    elif n_sec == 3: sc_div = 13
    elif n_sec == 2: sc_div = 8
    else:            sc_div = 4

    # ── Score 2: Concentração (0–25) ─────────────────────────────────────────
    pcts_ativ = sorted(
        [(a["sym"], round(a["valor"] / total_valor * 100, 1)) for a in ativos_info],
        key=lambda x: -x[1]
    )
    max_sym, max_pct = pcts_ativ[0] if pcts_ativ else ("—", 100)
    if   max_pct < 15: sc_conc = 25
    elif max_pct < 25: sc_conc = 20
    elif max_pct < 35: sc_conc = 15
    elif max_pct < 50: sc_conc = 10
    elif max_pct < 70: sc_conc = 5
    else:              sc_conc = 2

    # ── Score 3: Rendimento DY médio ponderado (0–25) ────────────────────────
    dy_pond     = sum(a["dy"] * a["valor"] for a in ativos_info) / total_valor if total_valor > 0 else 0
    n_has_div   = len([a for a in ativos_info if a["has_div"]])
    pct_has_div = n_has_div / n_ativ if n_ativ else 0
    mercado_fechado = dy_pond <= 0 and pct_has_div >= 0.4

    # Quando mercado fechado BRAPI retorna dy=0 — não penaliza
    dy_display = dy_pond
    if mercado_fechado:
        dy_pond    = 5.0   # neutro
        dy_display = 0.0   # mostra real (0) no detalhe com aviso

    if   dy_pond >= 12: sc_dy = 25
    elif dy_pond >= 8:  sc_dy = 22
    elif dy_pond >= 5:  sc_dy = 17
    elif dy_pond >= 3:  sc_dy = 12
    elif dy_pond >= 1:  sc_dy = 7
    else:               sc_dy = 3

    # ── Score 4: Consistência de dividendos (0–25) ────────────────────────────
    pct_com_div = pct_has_div * 100
    if   pct_com_div >= 90: sc_cons = 25
    elif pct_com_div >= 70: sc_cons = 20
    elif pct_com_div >= 50: sc_cons = 14
    elif pct_com_div >= 25: sc_cons = 8
    else:                   sc_cons = 3

    total_score = sc_div + sc_conc + sc_dy + sc_cons

    # ── Score 5: Qualidade da carteira (bônus) ────────────────────────────────
    # FIIs mensais = consistência extra; só melhora, nunca piora
    n_fiis = len([a for a in ativos_info if a["fii"]])
    bonus  = min(5, n_fiis * 1)   # até +5 pts por FIIs
    total_score = min(100, total_score + bonus)

    # ── Classificação ─────────────────────────────────────────────────────────
    if   total_score >= 88: grade, label = "A+", "Excelente"
    elif total_score >= 75: grade, label = "A",  "Muito Boa"
    elif total_score >= 60: grade, label = "B",  "Boa"
    elif total_score >= 44: grade, label = "C",  "Regular"
    elif total_score >= 28: grade, label = "D",  "Fraca"
    else:                   grade, label = "F",  "Crítica"

    # ── Dicas personalizadas ──────────────────────────────────────────────────
    tips = []
    if sc_div < 15:
        outros = ["Energia", "Saúde", "Agro", "Varejo", "Tecnologia"]
        sugest = [s for s in outros if s not in sectors_used][:2]
        dica = f"Você tem {n_sec} setor{'es' if n_sec>1 else ''}."
        if sugest:
            dica += f" Considere adicionar {' ou '.join(sugest)} para reduzir risco."
        tips.append(dica)
    if sc_conc < 15:
        tips.append(
            f"{max_sym} ocupa {max_pct:.0f}% da carteira — acima do ideal de 25%. "
            f"Rebalancear gradualmente protege contra eventos específicos do ativo."
        )
    if sc_dy < 15 and not mercado_fechado:
        tips.append(
            f"DY médio de {dy_display:.1f}%. FIIs como HGLG11, MXRF11 e XPML11 "
            f"pagam mensalmente e costumam ter DY entre 10–14%."
        )
    if sc_cons < 15:
        pagadores = [a["sym"] for a in ativos_info if a["has_div"]]
        nao_pagam = [a["sym"] for a in ativos_info if not a["has_div"]]
        if nao_pagam:
            tips.append(
                f"{', '.join(nao_pagam[:3])} não têm histórico de dividendos confirmado. "
                f"Priorize ativos com pagamentos consistentes para fortalecer renda passiva."
            )
    if mercado_fechado:
        tips.append("⏰ Mercado fechado — DY calculado com base em histórico. Dados de yield serão atualizados na abertura.")
    if not tips:
        tips.append(
            f"Carteira equilibrada com {n_ativ} ativos em {n_sec} setores. "
            f"Continue reinvestindo os dividendos para acelerar o crescimento patrimonial."
        )

    # ── Distribuição setorial ─────────────────────────────────────────────────
    sector_dist = {}
    for a in ativos_info:
        s = a["sector"]
        sector_dist[s] = round(sector_dist.get(s, 0) + a["valor"] / total_valor * 100, 1)

    # ── Breakdown por ativo (top 5) ───────────────────────────────────────────
    top_ativos = [
        {
            "sym":    sym,
            "pct":    pct,
            "dy":     round(next((a["dy"] for a in ativos_info if a["sym"]==sym), 0), 2),
            "fii":    next((a["fii"] for a in ativos_info if a["sym"]==sym), False),
            "sector": next((a["sector"] for a in ativos_info if a["sym"]==sym), "—"),
        }
        for sym, pct in pcts_ativ[:6]
    ]

    return jsonify({
        "score":           total_score,
        "grade":           grade,
        "label":           label,
        "mercado_fechado": mercado_fechado,
        "bonus_fiis":      bonus,
        "scores": {
            "diversificacao": {
                "value":  sc_div,
                "max":    25,
                "label":  "Diversificação",
                "detail": f"{n_sec} setor{'es' if n_sec!=1 else ''} · {n_ativ} ativos",
            },
            "concentracao": {
                "value":  sc_conc,
                "max":    25,
                "label":  "Concentração",
                "detail": f"Maior posição: {max_sym} {max_pct:.0f}%",
            },
            "rendimento": {
                "value":  sc_dy,
                "max":    25,
                "label":  "Rendimento (DY)",
                "detail": ("⏰ Mercado fechado" if mercado_fechado
                           else f"DY médio: {dy_display:.1f}%"),
            },
            "consistencia": {
                "value":  sc_cons,
                "max":    25,
                "label":  "Consistência",
                "detail": f"{n_has_div}/{n_ativ} ativos c/ dividendos",
            },
        },
        "tips":         tips,
        "sector_dist":  sector_dist,
        "top_ativos":   top_ativos,
        "total_ativos": n_ativ,
        "dy_medio":     round(dy_display, 2),
        "n_fiis":       n_fiis,
    })


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

# ── Consultor IA (chat com Anthropic) ────────────────────────────────────────
@app.route("/api/ai/chat", methods=["POST"])
@login_required
def ai_chat():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return jsonify({"error": "ANTHROPIC_API_KEY não configurada. Configure nas variáveis de ambiente."}), 503

    data = request.json or {}
    messages = data.get("messages", [])
    portfolio_ctx = data.get("portfolio_context", "")

    if not messages:
        return jsonify({"error": "Nenhuma mensagem enviada."}), 400

    system_prompt = f"""Você é o MERIDIAN AI, um consultor financeiro especializado no mercado brasileiro de investimentos (B3, FIIs, ações, renda fixa).

Você auxilia investidores com:
- Análise de carteira e diversificação
- Sugestões de alocação por perfil de risco
- Explicação de indicadores financeiros (DY, P/L, ROE, ROIC, etc.)
- Estratégias de renda passiva com dividendos
- Planejamento de independência financeira
- Comparação entre ativos
- Simulação de cenários
- Educação financeira

Contexto da carteira do usuário:
{portfolio_ctx if portfolio_ctx else "Carteira não carregada."}

Regras:
- Responda sempre em português brasileiro
- Use formatação com **negrito** e listas quando útil
- Seja objetivo, direto e profissional
- Use dados reais do mercado brasileiro quando possível
- Não forneça recomendações definitivas de compra/venda — sempre oriente a buscar assessoria profissional para decisões definitivas
- Use emojis moderadamente para melhorar legibilidade
- Formate valores em R$ quando relevante"""

    try:
        r = req_lib.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1500,
                "system": system_prompt,
                "messages": messages[-20:],  # últimas 20 mensagens
            },
            timeout=45,
        )
        r.raise_for_status()
        resp = r.json()
        content = resp.get("content", [{}])[0].get("text", "")
        return jsonify({"response": content})
    except req_lib.exceptions.HTTPError as e:
        st = e.response.status_code if e.response else 0
        if st == 401:
            return jsonify({"error": "API key inválida. Verifique ANTHROPIC_API_KEY."}), 503
        if st == 529 or st == 503:
            return jsonify({"error": "API sobrecarregada. Tente novamente em instantes."}), 503
        return jsonify({"error": f"Erro HTTP {st} na API Anthropic."}), 503
    except Exception as e:
        return jsonify({"error": f"Erro ao conectar à IA: {str(e)}"}), 503


# ── Simulador de Independência Financeira ────────────────────────────────────
@app.route("/api/independencia", methods=["POST"])
@login_required
def simulador_independencia():
    data = request.json or {}
    patrimonio_atual = float(data.get("patrimonio_atual", 0))
    aporte_mensal    = float(data.get("aporte_mensal", 0))
    rentabilidade    = float(data.get("rentabilidade", 0.1)) / 100
    idade_atual      = int(data.get("idade_atual", 30))
    idade_aposentadoria = int(data.get("idade_aposentadoria", 60))
    objetivo         = float(data.get("objetivo", 0))

    cenarios = {
        "conservador": max(rentabilidade * 0.6, 0.004),
        "realista":    rentabilidade,
        "otimista":    min(rentabilidade * 1.4, 0.025),
    }

    resultados = {}
    for nome, taxa_anual in cenarios.items():
        taxa_mensal = (1 + taxa_anual) ** (1/12) - 1
        anos = idade_aposentadoria - idade_atual
        meses = anos * 12
        if meses <= 0:
            resultados[nome] = {"erro": "Idade inválida"}
            continue

        patrimonio = patrimonio_atual
        evolucao = []
        for m in range(meses + 1):
            if m > 0:
                patrimonio = patrimonio * (1 + taxa_mensal) + aporte_mensal
            if m % 12 == 0:
                ano = idade_atual + m // 12
                renda_mensal = patrimonio * taxa_mensal
                evolucao.append({
                    "ano": ano,
                    "patrimonio": round(patrimonio, 2),
                    "renda_mensal": round(renda_mensal, 2),
                })

        # Calcular quando atinge o objetivo
        anos_para_objetivo = None
        if objetivo > 0:
            p = patrimonio_atual
            for m2 in range(meses * 2):
                p = p * (1 + taxa_mensal) + aporte_mensal
                if p >= objetivo:
                    anos_para_objetivo = round(m2 / 12, 1)
                    break

        patrimonio_final = evolucao[-1]["patrimonio"] if evolucao else 0
        renda_mensal_final = round(patrimonio_final * taxa_mensal, 2)
        total_aportado = patrimonio_atual + aporte_mensal * meses
        rendimentos = round(patrimonio_final - total_aportado, 2)

        resultados[nome] = {
            "patrimonio_final": round(patrimonio_final, 2),
            "renda_mensal": renda_mensal_final,
            "total_aportado": round(total_aportado, 2),
            "rendimentos": rendimentos,
            "anos_para_objetivo": anos_para_objetivo,
            "evolucao": evolucao,
            "taxa_anual_pct": round(taxa_anual * 100, 2),
        }

    return jsonify(resultados)


# ── MERIDIAN SCORE PRO — score individual de ativo ───────────────────────────
@app.route("/api/score-pro/<symbol>")
@login_required
def score_pro_ativo(symbol):
    sym = symbol.upper().replace(".SA", "")
    # Tenta com modules; se falhar (plano free), cai para chamada simples
    data = brapi_get(f"/quote/{sym}", {"fundamental": "true", "modules": "summaryProfile,defaultKeyStatistics,financialData"})
    if not data or "results" not in data or not data["results"]:
        data = brapi_get(f"/quote/{sym}")  # fallback sem modules

    if not data or "results" not in data or not data["results"]:
        return jsonify({"error": f"Ativo {sym} não encontrado."}), 404

    r = data["results"][0]
    fii = _is_fii(sym)

    # Extraindo métricas
    dy           = float(r.get("dividendYield") or 0)
    price        = float(r.get("regularMarketPrice") or 0)
    pl           = float(r.get("priceEarnings") or 0)
    pb           = float(r.get("priceToBook") or 0)
    roe          = float(r.get("returnOnEquity") or 0) * 100 if r.get("returnOnEquity") else 0
    market_cap   = float(r.get("marketCap") or 0)
    div_rate     = float(r.get("dividendRate") or 0)
    last_div     = float(r.get("lastDividendValue") or 0)
    week52_low   = float(r.get("fiftyTwoWeekLow") or 0)
    week52_high  = float(r.get("fiftyTwoWeekHigh") or 0)

    # ── Score Dividendos (0-25) ───────────────────────────────────────────────
    sc_div = 0
    if dy >= 10: sc_div = 25
    elif dy >= 7: sc_div = 20
    elif dy >= 5: sc_div = 15
    elif dy >= 3: sc_div = 10
    elif dy >= 1: sc_div = 6
    elif last_div > 0: sc_div = 4
    sc_div_label = f"DY de {dy:.1f}%" if dy > 0 else "Sem DY disponível"

    # ── Score Valuation (0-25) ────────────────────────────────────────────────
    sc_val = 12  # base
    if pl > 0:
        if pl < 8: sc_val = 25
        elif pl < 12: sc_val = 22
        elif pl < 18: sc_val = 18
        elif pl < 25: sc_val = 14
        elif pl < 40: sc_val = 8
        else: sc_val = 4
    elif fii and pb > 0:
        if pb < 0.85: sc_val = 25
        elif pb < 1.0: sc_val = 20
        elif pb < 1.1: sc_val = 15
        elif pb < 1.3: sc_val = 10
        else: sc_val = 5
    sc_val_label = f"P/L: {pl:.1f}" if pl > 0 else ("P/VP: {:.2f}".format(pb) if pb > 0 else "Valuation indisponível")

    # ── Score Crescimento (0-25) ──────────────────────────────────────────────
    sc_cres = 12  # base neutro
    if roe >= 20: sc_cres = 25
    elif roe >= 15: sc_cres = 22
    elif roe >= 10: sc_cres = 17
    elif roe >= 5: sc_cres = 12
    elif roe > 0: sc_cres = 7
    sc_cres_label = f"ROE: {roe:.1f}%" if roe > 0 else "ROE indisponível"

    # ── Score Segurança Financeira (0-25) ─────────────────────────────────────
    sc_seg = 12  # base
    dist_52 = 0
    if week52_high > week52_low and price > 0:
        dist_52 = (price - week52_low) / (week52_high - week52_low) * 100 if week52_high != week52_low else 50
        if dist_52 < 20: sc_seg = 22    # perto da mínima = barato
        elif dist_52 < 40: sc_seg = 18
        elif dist_52 < 60: sc_seg = 14
        elif dist_52 < 80: sc_seg = 10
        else: sc_seg = 6                # perto da máxima
    sc_seg_label = f"{dist_52:.0f}% da faixa 52 semanas" if dist_52 > 0 else "Posição 52 semanas"

    total = min(100, sc_div + sc_val + sc_cres + sc_seg)

    if total >= 90: grade, cat = "A+", "Excelente"
    elif total >= 80: grade, cat = "A",  "Muito Bom"
    elif total >= 70: grade, cat = "B+", "Bom"
    elif total >= 60: grade, cat = "B",  "Regular"
    else: grade, cat = "C", "Fraco"

    sector = _SECTOR_MAP.get(sym, "FII" if fii else "Outros")
    name   = r.get("shortName") or r.get("longName") or sym

    return jsonify({
        "symbol": sym, "name": name, "sector": sector,
        "price": price, "fii": fii,
        "score_total": total, "grade": grade, "categoria": cat,
        "scores": {
            "dividendos":  {"value": sc_div,  "max": 25, "label": "Dividendos",          "detail": sc_div_label},
            "valuation":   {"value": sc_val,  "max": 25, "label": "Valuation",           "detail": sc_val_label},
            "crescimento": {"value": sc_cres, "max": 25, "label": "Crescimento/ROE",     "detail": sc_cres_label},
            "seguranca":   {"value": sc_seg,  "max": 25, "label": "Segurança Financeira","detail": sc_seg_label},
        },
        "metricas": {
            "dy": dy, "pl": pl, "pb": pb, "roe": roe,
            "market_cap": market_cap, "last_div": last_div,
            "week52_low": week52_low, "week52_high": week52_high,
        }
    })


# ── MERIDIAN SCORE PRO — ranking de ativos ───────────────────────────────────
@app.route("/api/score-pro/ranking/<tipo>")
@login_required
def score_pro_ranking(tipo):
    """tipo: acoes | fiis | etfs | geral"""
    if tipo == "acoes":
        symbols = ["WEGE3","ITUB4","BBAS3","PETR4","VALE3","LREN3","EGIE3","TAEE11",
                   "BBDC4","PRIO3","RDOR3","FLRY3","ODPV3","VIVT3","CMIG4","CPLE6",
                   "SAPR11","ENBR3","BBSE3","CXSE3"]
    elif tipo == "fiis":
        symbols = ["HGLG11","MXRF11","XPML11","KNRI11","MALL11","BCFF11","HSML11",
                   "VISC11","BRCO11","RBRR11","TVRI11","BTLG11","HCTR11","MCCI11","IRDM11"]
    elif tipo == "etfs":
        symbols = ["BOVA11","IVVB11","SMAL11","DIVO11","HASH11","GOLD11","SPXI11","NTNB11"]
    else:  # geral
        symbols = ["WEGE3","ITUB4","BBAS3","EGIE3","TAEE11","HGLG11","MXRF11","XPML11",
                   "PETR4","VALE3","BBSE3","FLRY3","VIVT3","CMIG4","SAPR11","ODPV3"]

    quotes = brapi_quotes(symbols) or []
    qmap   = {q["symbol"]: q for q in quotes}

    ranking = []
    for sym in symbols:
        q = qmap.get(sym, {})
        if not q:
            continue
        fii      = _is_fii(sym)
        dy       = float(q.get("dividendYield") or 0)
        pl       = float(q.get("priceEarnings") or 0)
        pb       = float(q.get("priceToBook") or 0)
        roe      = float(q.get("returnOnEquity") or 0) * 100 if q.get("returnOnEquity") else 0
        price    = float(q.get("regularMarketPrice") or 0)
        chg      = float(q.get("regularMarketChangePercent") or 0)
        w52l     = float(q.get("fiftyTwoWeekLow") or 0)
        w52h     = float(q.get("fiftyTwoWeekHigh") or 0)
        last_div = float(q.get("lastDividendValue") or 0)

        # Score rápido
        sc_div = 0
        if dy >= 10: sc_div = 25
        elif dy >= 7: sc_div = 20
        elif dy >= 5: sc_div = 15
        elif dy >= 3: sc_div = 10
        elif dy >= 1: sc_div = 6
        elif last_div > 0: sc_div = 4

        sc_val = 12
        if pl > 0:
            if pl < 8: sc_val = 25
            elif pl < 12: sc_val = 22
            elif pl < 18: sc_val = 18
            elif pl < 25: sc_val = 14
            elif pl < 40: sc_val = 8
            else: sc_val = 4
        elif fii and pb > 0:
            if pb < 0.85: sc_val = 25
            elif pb < 1.0: sc_val = 20
            elif pb < 1.1: sc_val = 15
            elif pb < 1.3: sc_val = 10
            else: sc_val = 5

        sc_cres = 12
        if roe >= 20: sc_cres = 25
        elif roe >= 15: sc_cres = 22
        elif roe >= 10: sc_cres = 17
        elif roe >= 5: sc_cres = 12
        elif roe > 0: sc_cres = 7

        sc_seg = 12
        if w52h > w52l and price > 0 and w52h != w52l:
            dist = (price - w52l) / (w52h - w52l) * 100
            if dist < 20: sc_seg = 22
            elif dist < 40: sc_seg = 18
            elif dist < 60: sc_seg = 14
            elif dist < 80: sc_seg = 10
            else: sc_seg = 6

        total = min(100, sc_div + sc_val + sc_cres + sc_seg)
        if total >= 90: grade, cat = "A+", "Excelente"
        elif total >= 80: grade, cat = "A",  "Muito Bom"
        elif total >= 70: grade, cat = "B+", "Bom"
        elif total >= 60: grade, cat = "B",  "Regular"
        else: grade, cat = "C", "Fraco"

        sector = _SECTOR_MAP.get(sym, "FII" if fii else "ETF" if sym in ["BOVA11","IVVB11","SMAL11","DIVO11","HASH11","GOLD11","SPXI11","NTNB11"] else "Outros")
        name   = (q.get("shortName") or sym)[:22]

        ranking.append({
            "symbol": sym, "name": name, "sector": sector,
            "price": price, "change": round(chg, 2), "fii": fii,
            "score": total, "grade": grade, "categoria": cat,
            "dy": round(dy, 2), "pl": round(pl, 2), "roe": round(roe, 2),
        })

    ranking.sort(key=lambda x: -x["score"])
    return jsonify(ranking)


# ── Radar de Oportunidades ────────────────────────────────────────────────────
@app.route("/api/radar")
@login_required
def radar_oportunidades():
    ck = "radar_data"; cached = cache_get(ck, ttl=600)
    if cached: return jsonify(cached)

    # Ativos monitorados para oportunidades
    acoes_radar = ["WEGE3","ITUB4","BBAS3","PETR4","VALE3","EGIE3","TAEE11",
                   "BBDC4","PRIO3","RDOR3","FLRY3","VIVT3","CMIG4","BBSE3",
                   "LREN3","MGLU3","HAPV3","ODPV3","CXSE3","SAPR11"]
    fiis_radar  = ["HGLG11","MXRF11","XPML11","KNRI11","MALL11","HSML11",
                   "VISC11","BRCO11","BTLG11","IRDM11","TVRI11","MCCI11"]

    all_syms = acoes_radar + fiis_radar
    quotes   = brapi_quotes(all_syms) or []
    qmap     = {q["symbol"]: q for q in quotes}

    # Enriquecer com fundamental individualmente para garantir priceEarnings/priceToBook/dy
    for sym in all_syms:
        ck_re = f"radar_enrich_{sym}"
        cached_re = cache_get(ck_re, ttl=3600)
        if cached_re:
            if sym in qmap:
                for k, v in cached_re.items():
                    if v is not None and not qmap[sym].get(k):
                        qmap[sym][k] = v
            continue
        raw = brapi_get(f"/quote/{sym}", {"fundamental": "true"})
        if raw and "results" in raw and raw["results"]:
            r = raw["results"][0]
            enriched = {
                "dividendYield":  r.get("dividendYield"),
                "priceEarnings":  r.get("priceEarnings"),
                "priceToBook":    r.get("priceToBook"),
                "fiftyTwoWeekLow":  r.get("fiftyTwoWeekLow"),
                "fiftyTwoWeekHigh": r.get("fiftyTwoWeekHigh"),
                "returnOnEquity": r.get("returnOnEquity"),
            }
            cache_set(ck_re, enriched)
            if sym not in qmap:
                qmap[sym] = r
            else:
                for k, v in enriched.items():
                    if v is not None:
                        qmap[sym][k] = v

    descontadas_acoes, descontadas_fiis = [], []
    maiores_altas, maiores_quedas       = [], []
    alto_dy                              = []

    for sym in all_syms:
        q = qmap.get(sym, {})
        if not q: continue
        fii   = _is_fii(sym)
        price = float(q.get("regularMarketPrice") or 0)
        chg   = float(q.get("regularMarketChangePercent") or 0)
        dy    = float(q.get("dividendYield") or 0)
        pl    = float(q.get("priceEarnings") or 0)
        pb    = float(q.get("priceToBook") or 0)
        w52l  = float(q.get("fiftyTwoWeekLow") or 0)
        w52h  = float(q.get("fiftyTwoWeekHigh") or 0)
        name  = (q.get("shortName") or sym)[:22]
        sector = _SECTOR_MAP.get(sym, "FII" if fii else "Outros")

        item = {"symbol": sym, "name": name, "price": price,
                "change": round(chg, 2), "dy": round(dy, 2),
                "sector": sector, "fii": fii,
                "pl": round(pl, 2), "pb": round(pb, 2)}

        # Descontadas: perto do mínimo de 52 semanas
        if price > 0 and w52h > w52l and w52h != w52l:
            dist = (price - w52l) / (w52h - w52l) * 100
            item["dist_min_52"] = round(dist, 1)
            if dist < 25:
                if fii: descontadas_fiis.append(item)
                else:   descontadas_acoes.append(item)

        # Alto DY
        if dy >= 0.04: alto_dy.append(item)  # dy é fração decimal: 0.04 = 4%

        # Altas e quedas do dia (exclui ativos sem variação real)
        if price > 0 and chg != 0:
            if chg > 0: maiores_altas.append(item)
            if chg < 0: maiores_quedas.append(item)

    maiores_altas.sort(key=lambda x: -x["change"])
    maiores_quedas.sort(key=lambda x: x["change"])
    descontadas_acoes.sort(key=lambda x: x.get("dist_min_52", 100))
    descontadas_fiis.sort(key=lambda x: x.get("dist_min_52", 100))
    alto_dy.sort(key=lambda x: -x["dy"])

    result = {
        "descontadas_acoes": descontadas_acoes[:6],
        "descontadas_fiis":  descontadas_fiis[:6],
        "maiores_altas":     maiores_altas[:6],
        "maiores_quedas":    maiores_quedas[:6],
        "alto_dy":           alto_dy[:8],
        "atualizado_em":     datetime.utcnow().strftime("%H:%M UTC"),
    }
    cache_set(ck, result)
    return jsonify(result)


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
    print("  MERIDIAN — http://localhost:5000")
    print(f"  Brapi: {'OK' if BRAPI_TOKEN else 'SEM TOKEN!'}")
    print("="*50)
    app.run(debug=False,host="0.0.0.0",port=5000)

# ── Debug: testa token BRAPI e retorna campos disponíveis ─────────────────────
@app.route("/api/debug/brapi/<symbol>")
@login_required
def debug_brapi(symbol):
    if not current_user.admin:
        return jsonify({"error": "admin only"}), 403
    sym = symbol.upper().replace(".SA","")
    result = {}
    # Teste 1: básico sem fundamental
    d1 = brapi_get(f"/quote/{sym}")
    r1 = (d1.get("results") or [{}])[0] if d1 else {}
    result["basic"] = {
        "dividendYield": r1.get("dividendYield"),
        "dividendRate": r1.get("dividendRate"),
        "lastDividendValue": r1.get("lastDividendValue"),
        "regularMarketPrice": r1.get("regularMarketPrice"),
    }
    # Teste 2: com fundamental
    d2 = brapi_get(f"/quote/{sym}", {"fundamental": "true"})
    r2 = (d2.get("results") or [{}])[0] if d2 else {}
    result["fundamental"] = {
        "ok": d2 is not None,
        "dividendYield": r2.get("dividendYield"),
        "dividendRate": r2.get("dividendRate"),
        "lastDividendValue": r2.get("lastDividendValue"),
        "priceEarnings": r2.get("priceEarnings"),
        "priceToBook": r2.get("priceToBook"),
    }
    # Teste 3: com dividends
    d3 = brapi_get(f"/quote/{sym}", {"dividends": "true"})
    r3 = (d3.get("results") or [{}])[0] if d3 else {}
    cash = (r3.get("dividendsData") or {}).get("cashDividends") or []
    result["dividends"] = {
        "ok": d3 is not None,
        "count": len(cash),
        "first": cash[0] if cash else None,
    }
    result["token_configured"] = bool(BRAPI_TOKEN)
    return jsonify(result)
