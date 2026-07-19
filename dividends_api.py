"""
dividends_api.py
══════════════════════════════════════════════════════════════════════════
Blueprint com os endpoints do novo sistema de dividendos (v2).
Registrado em app.py com: app.register_blueprint(dividends_bp)

Endpoints:
  GET  /api/v2/dividends/<ticker>              → perfil completo (header + agregados)
  GET  /api/v2/dividends/<ticker>/history       → série mensal/trimestral/anual
  GET  /api/v2/dividends/<ticker>/calendar      → próximos pagamentos declarados
  POST /api/v2/dividends/compare                → compara até 4 ativos
  POST /api/v2/dividends/<ticker>/sync          → força ressincronização (admin)
"""

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

from models import db
import dividends_service as ds

dividends_bp = Blueprint("dividends_v2", __name__, url_prefix="/api/v2/dividends")

# injetado por app.py em tempo de boot para evitar import circular
_fetch_dividends_fn = None
_is_fii_fn = None


def init_dividends_api(fetch_dividends_fn, is_fii_fn):
    global _fetch_dividends_fn, _is_fii_fn
    _fetch_dividends_fn = fetch_dividends_fn
    _is_fii_fn = is_fii_fn


def _ensure_synced(ticker):
    """Sincroniza sob demanda se os dados estiverem ausentes ou obsoletos."""
    ticker = ticker.upper().replace(".SA", "")
    if ds.is_stale(ticker):
        tipo_ativo = "fii" if _is_fii_fn(ticker) else "acao"
        ds.sync_ticker(ticker, tipo_ativo, _fetch_dividends_fn)


@dividends_bp.route("/<ticker>")
@login_required
def profile(ticker):
    ticker = ticker.upper().replace(".SA", "")
    _ensure_synced(ticker)
    agg = ds.aggregate(ticker)
    if not agg.get("found"):
        return jsonify({"found": False, "ticker": ticker,
                         "message": "Sem histórico de proventos disponível para este ativo."}), 200
    return jsonify(agg)


@dividends_bp.route("/<ticker>/history")
@login_required
def history(ticker):
    ticker = ticker.upper().replace(".SA", "")
    view = request.args.get("view", "monthly")  # monthly | quarterly | yearly
    _ensure_synced(ticker)
    agg = ds.aggregate(ticker)
    if not agg.get("found"):
        return jsonify({"found": False, "series": []})
    key_map = {"monthly": "monthly", "quarterly": "quarterly", "yearly": "total_por_ano"}
    series = agg.get(key_map.get(view, "monthly"), [])
    return jsonify({"found": True, "view": view, "series": series})


@dividends_bp.route("/<ticker>/calendar")
@login_required
def calendar(ticker):
    ticker = ticker.upper().replace(".SA", "")
    _ensure_synced(ticker)
    agg = ds.aggregate(ticker)
    if not agg.get("found"):
        return jsonify({"found": False, "proximos_pagamentos": []})
    return jsonify({"found": True, "proximos_pagamentos": agg.get("proximos_pagamentos", [])})


@dividends_bp.route("/compare", methods=["POST"])
@login_required
def compare():
    body = request.get_json(silent=True) or {}
    tickers = [t.upper().replace(".SA", "") for t in (body.get("tickers") or [])][:4]
    if not tickers:
        return jsonify({"error": "Informe até 4 tickers em 'tickers'."}), 400

    out = []
    for t in tickers:
        _ensure_synced(t)
        agg = ds.aggregate(t)
        out.append(agg if agg.get("found") else {"found": False, "ticker": t})
    return jsonify({"results": out})


@dividends_bp.route("/<ticker>/sync", methods=["POST"])
@login_required
def force_sync(ticker):
    if not getattr(current_user, "admin", False):
        return jsonify({"error": "Apenas administradores podem forçar sincronização."}), 403
    ticker = ticker.upper().replace(".SA", "")
    tipo_ativo = "fii" if _is_fii_fn(ticker) else "acao"
    ok = ds.sync_ticker(ticker, tipo_ativo, _fetch_dividends_fn)
    agg = ds.aggregate(ticker) if ok else {"found": False}
    return jsonify({"synced": ok, "data": agg})
