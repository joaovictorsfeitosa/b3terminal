"""
dividends_service.py
══════════════════════════════════════════════════════════════════════════
Camada de serviço do sistema de dividendos:
  • Cache Redis (com fallback automático para memória se REDIS_URL ausente)
  • Sincronização de proventos para o banco (Provento)
  • Classificador automático de frequência de pagamento
  • Agregações (totais por ano/mês/trimestre, CAGR, yield on cost, etc.)

Este módulo não importa app.py para evitar import circular. Quem chama
`sync_ticker(...)` deve passar a função de busca de dados brutos
(fetch_fn) — em produção, `brapi_dividends` de app.py.
"""

import os
import json
import time
import threading
import statistics
from datetime import date, datetime, timedelta
from collections import defaultdict

from models import db, Provento, TickerFrequencia, SyncLog

# ─────────────────────────────────────────────────────────────────────────
# Camada de cache — Redis se disponível, senão memória de processo
# ─────────────────────────────────────────────────────────────────────────

REDIS_URL = os.environ.get("REDIS_URL", "")
_redis_client = None
_redis_ok = False

if REDIS_URL:
    try:
        import redis as _redis_lib
        _redis_client = _redis_lib.from_url(
            REDIS_URL, decode_responses=True, socket_connect_timeout=3, socket_timeout=3
        )
        _redis_client.ping()
        _redis_ok = True
        print("  [dividends_service] Redis conectado.")
    except Exception as e:
        print(f"  [dividends_service] Redis indisponível ({e}) — usando cache em memória.")
        _redis_client = None
        _redis_ok = False

_mem_cache, _mem_lock = {}, threading.Lock()


def cache_get(key):
    if _redis_ok:
        try:
            raw = _redis_client.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None
    with _mem_lock:
        item = _mem_cache.get(key)
        if not item:
            return None
        data, expires_at = item
        if time.time() > expires_at:
            del _mem_cache[key]
            return None
        return data


def cache_set(key, value, ttl=3600):
    if _redis_ok:
        try:
            _redis_client.setex(key, ttl, json.dumps(value, default=str))
            return
        except Exception:
            pass
    with _mem_lock:
        _mem_cache[key] = (value, time.time() + ttl)


def cache_delete(key):
    if _redis_ok:
        try:
            _redis_client.delete(key)
        except Exception:
            pass
    with _mem_lock:
        _mem_cache.pop(key, None)


# ─────────────────────────────────────────────────────────────────────────
# Classificador automático de frequência
# ─────────────────────────────────────────────────────────────────────────

_BUCKETS = [
    (40,  "Mensal",     list(range(1, 13))),
    (75,  "Bimestral",  [1, 3, 5, 7, 9, 11]),
    (135, "Trimestral", [3, 6, 9, 12]),
    (225, "Semestral",  [6, 12]),
    (400, "Anual",      [12]),
]


def classify_frequency(payment_dates):
    """
    Recebe uma lista ordenada de `date` (pagamentos históricos, sem
    declarados-futuros) e retorna:
        {label, confidence, months, changed_recently}
    """
    dates = sorted(set(payment_dates))
    if len(dates) < 2:
        return {"label": "Irregular", "confidence": 0.0, "months": [], "changed_recently": False}

    # usa até os últimos 24 pagamentos para não deixar histórico antigo distorcer
    recent = dates[-24:]
    intervals = [(recent[i] - recent[i - 1]).days for i in range(1, len(recent))]
    median_interval = statistics.median(intervals)

    label, months = "Irregular", sorted(set(d.month for d in recent))
    for max_days, bucket_label, bucket_months in _BUCKETS:
        if median_interval <= max_days:
            label, months = bucket_label, bucket_months
            break

    # confiança: quão consistentes são os intervalos em torno da mediana
    if len(intervals) >= 2:
        deviations = [abs(i - median_interval) / max(median_interval, 1) for i in intervals]
        avg_dev = sum(deviations) / len(deviations)
        confidence = max(0.0, min(1.0, 1 - avg_dev))
    else:
        confidence = 0.5
    confidence = round(confidence, 2)

    # detecta mudança recente: classifica só os últimos 4 vs os anteriores
    changed_recently = False
    if len(dates) >= 8:
        last4 = dates[-4:]
        prior = dates[:-4]
        last4_intervals = [(last4[i] - last4[i - 1]).days for i in range(1, len(last4))]
        prior_intervals = [(prior[i] - prior[i - 1]).days for i in range(1, len(prior))]
        if last4_intervals and prior_intervals:
            m1, m2 = statistics.median(last4_intervals), statistics.median(prior_intervals)
            # mudou de "bucket" (ex.: era trimestral e virou mensal)
            def _bucket_of(days):
                for max_days, lbl, _ in _BUCKETS:
                    if days <= max_days:
                        return lbl
                return "Irregular"
            changed_recently = _bucket_of(m1) != _bucket_of(m2)

    return {
        "label": label,
        "confidence": confidence,
        "months": months,
        "changed_recently": changed_recently,
    }


# ─────────────────────────────────────────────────────────────────────────
# Sincronização: busca externa → grava em Provento → recalcula frequência
# ─────────────────────────────────────────────────────────────────────────

SYNC_STALE_HOURS = 20  # depois disso, considera o ticker desatualizado


def is_stale(ticker):
    meta = TickerFrequencia.query.get(ticker)
    if not meta or not meta.ultima_sincronizacao:
        return True
    return datetime.utcnow() - meta.ultima_sincronizacao > timedelta(hours=SYNC_STALE_HOURS)


def sync_ticker(ticker, tipo_ativo, fetch_fn):
    """
    fetch_fn(ticker) deve retornar o mesmo formato de `brapi_dividends()`:
        {"projected": [...], "freq_label":..., "avg_value":..., ...}
    ou None. Internamente usamos o campo bruto que a função expõe:
    lista de pagamentos com year/month/day/value/declared.
    """
    ticker = ticker.upper().replace(".SA", "")
    try:
        raw = fetch_fn(ticker)
    except Exception as e:
        db.session.add(SyncLog(ticker=ticker, status="erro", detalhe=str(e)))
        db.session.commit()
        return False

    payments = []
    if raw and isinstance(raw, dict):
        payments = raw.get("_raw_payments") or []

    if not payments:
        db.session.add(SyncLog(ticker=ticker, status="vazio", qtd_eventos=0))
        db.session.commit()
        return False

    tipo_provento_default = "rendimento" if tipo_ativo == "fii" else "dividendo"
    novos, atualizados = 0, 0

    for p in payments:
        try:
            dt = date(p["year"], p["month"], p.get("day", 1))
        except Exception:
            continue
        valor = float(p.get("value") or 0)
        if valor <= 0:
            continue
        declarado = bool(p.get("declared"))
        fonte = p.get("source", "twelvedata_or_brapi")

        existing = Provento.query.filter_by(
            ticker=ticker, data_pagamento=dt, tipo_provento=tipo_provento_default
        ).first()
        if existing:
            if float(existing.valor) != valor or existing.declarado_futuro != declarado:
                existing.valor = valor
                existing.declarado_futuro = declarado
                existing.fonte = fonte
                existing.atualizado_em = datetime.utcnow()
                atualizados += 1
        else:
            db.session.add(Provento(
                ticker=ticker,
                tipo_ativo=tipo_ativo,
                tipo_provento=tipo_provento_default,
                data_pagamento=dt,
                data_ex=dt,
                valor=valor,
                fonte=fonte,
                declarado_futuro=declarado,
            ))
            novos += 1

    db.session.commit()

    # recalcula classificação de frequência com o histórico atualizado
    hist_dates = [
        p.data_pagamento for p in
        Provento.query.filter_by(ticker=ticker, declarado_futuro=False).all()
    ]
    classification = classify_frequency(hist_dates)

    meta = TickerFrequencia.query.get(ticker)
    if not meta:
        meta = TickerFrequencia(ticker=ticker)
        db.session.add(meta)
    meta.frequencia = classification["label"]
    meta.confianca = classification["confidence"]
    meta.mudou_recentemente = classification["changed_recently"]
    meta.meses_pagamento = ",".join(str(m) for m in classification["months"])
    meta.ultima_sincronizacao = datetime.utcnow()
    db.session.commit()

    db.session.add(SyncLog(
        ticker=ticker, status="ok", qtd_eventos=novos + atualizados,
        detalhe=f"{novos} novos, {atualizados} atualizados"
    ))
    db.session.commit()
    cache_delete(f"divagg_{ticker}")
    return True


# ─────────────────────────────────────────────────────────────────────────
# Agregações para a tela do ativo
# ─────────────────────────────────────────────────────────────────────────

def get_frequency(ticker):
    ticker = ticker.upper().replace(".SA", "")
    meta = TickerFrequencia.query.get(ticker)
    if not meta:
        return {"label": "—", "confidence": 0.0, "months": [], "changed_recently": False}
    return {
        "label": meta.frequencia or "—",
        "confidence": meta.confianca or 0.0,
        "months": [int(m) for m in (meta.meses_pagamento or "").split(",") if m],
        "changed_recently": bool(meta.mudou_recentemente),
    }


def aggregate(ticker, years_back=5):
    """Retorna o payload completo de agregações para a tela do ativo."""
    ticker = ticker.upper().replace(".SA", "")

    cached = cache_get(f"divagg_{ticker}")
    if cached:
        return cached

    today = date.today()
    cutoff = date(today.year - years_back, 1, 1)

    rows = (Provento.query
            .filter(Provento.ticker == ticker, Provento.data_pagamento >= cutoff)
            .order_by(Provento.data_pagamento.asc())
            .all())

    historical = [r for r in rows if not r.declarado_futuro]
    declared_future = [r for r in rows if r.declarado_futuro]

    if not historical and not declared_future:
        return {"found": False}

    by_year = defaultdict(float)
    by_year_month = defaultdict(float)   # "YYYY-MM" -> total
    by_year_quarter = defaultdict(float) # "YYYY-Q1" -> total
    qtd_por_ano = defaultdict(int)

    for r in historical:
        v = float(r.valor)
        y, m = r.data_pagamento.year, r.data_pagamento.month
        q = (m - 1) // 3 + 1
        by_year[y] += v
        by_year_month[f"{y}-{m:02d}"] += v
        by_year_quarter[f"{y}-Q{q}"] += v
        qtd_por_ano[y] += 1

    last_12m_cutoff = today - timedelta(days=365)
    total_12m = sum(float(r.valor) for r in historical if r.data_pagamento >= last_12m_cutoff)
    total_ano_atual = by_year.get(today.year, 0.0)

    anos_ordenados = sorted(by_year.keys())
    total_por_ano = [{"ano": y, "total": round(by_year[y], 6), "qtd_pagamentos": qtd_por_ano[y]}
                      for y in anos_ordenados]

    # crescimento ano a ano (%)
    crescimento_aa = []
    for i in range(1, len(anos_ordenados)):
        y0, y1 = anos_ordenados[i - 1], anos_ordenados[i]
        v0, v1 = by_year[y0], by_year[y1]
        pct = ((v1 - v0) / v0 * 100) if v0 > 0 else None
        crescimento_aa.append({"de": y0, "para": y1, "crescimento_pct": round(pct, 2) if pct is not None else None})

    # CAGR sobre os anos completos disponíveis
    cagr = None
    anos_completos = [y for y in anos_ordenados if y != today.year]  # exclui ano corrente (parcial)
    if len(anos_completos) >= 2:
        v0, v1 = by_year[anos_completos[0]], by_year[anos_completos[-1]]
        n = anos_completos[-1] - anos_completos[0]
        if v0 > 0 and n > 0:
            cagr = round(((v1 / v0) ** (1 / n) - 1) * 100, 2)

    valores = [float(r.valor) for r in historical]
    media_mensal = round(sum(valores) / max(len(by_year_month), 1), 6) if by_year_month else 0
    media_anual = round(sum(by_year.values()) / max(len(by_year), 1), 6) if by_year else 0

    monthly = [{"periodo": k, "total": round(v, 6)} for k, v in sorted(by_year_month.items())]
    quarterly = [{"periodo": k, "total": round(v, 6)} for k, v in sorted(by_year_quarter.items())]

    result = {
        "found": True,
        "ticker": ticker,
        "total_ano_atual": round(total_ano_atual, 6),
        "total_12m": round(total_12m, 6),
        "total_por_ano": total_por_ano,
        "media_mensal": media_mensal,
        "media_anual": media_anual,
        "crescimento_aa": crescimento_aa,
        "cagr_pct": cagr,
        "maior_pagamento": round(max(valores), 6) if valores else 0,
        "menor_pagamento": round(min(valores), 6) if valores else 0,
        "qtd_pagamentos_total": len(historical),
        "monthly": monthly,
        "quarterly": quarterly,
        "proximos_pagamentos": [
            {"data": r.data_pagamento.isoformat(), "valor": round(float(r.valor), 6)}
            for r in declared_future
        ],
        "frequencia": get_frequency(ticker),
        "ultima_atualizacao": max(
            [r.atualizado_em for r in rows if r.atualizado_em], default=None
        ),
    }
    cache_set(f"divagg_{ticker}", result, ttl=1800)
    return result


def yield_on_cost(ticker, preco_medio, years_back=1):
    """Yield on cost = proventos últimos 12m / preço médio pago pelo investidor."""
    if not preco_medio or preco_medio <= 0:
        return None
    agg = aggregate(ticker, years_back=max(years_back, 1))
    if not agg.get("found"):
        return None
    return round((agg["total_12m"] / preco_medio) * 100, 2)
