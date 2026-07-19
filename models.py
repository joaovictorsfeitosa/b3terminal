from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import json

db = SQLAlchemy()

ADMIN_EMAIL = "joaovictor.sf100@gmail.com"

class User(UserMixin, db.Model):
    __tablename__ = "users"
    id         = db.Column(db.Integer, primary_key=True)
    nome       = db.Column(db.String(80), nullable=False)
    email      = db.Column(db.String(120), unique=True, nullable=False)
    senha_hash = db.Column(db.String(256), nullable=False)
    criado_em  = db.Column(db.DateTime, default=datetime.utcnow)
    is_admin   = db.Column(db.Boolean, default=False, nullable=False)
    is_blocked = db.Column(db.Boolean, default=False, nullable=False)
    ativos     = db.relationship("Ativo", backref="user", lazy=True, cascade="all, delete-orphan")

    @property
    def admin(self):
        return self.is_admin or self.email.lower() == ADMIN_EMAIL.lower()

    def set_senha(self, senha):
        self.senha_hash = generate_password_hash(senha)

    def check_senha(self, senha):
        if self.is_blocked:
            return False
        return check_password_hash(self.senha_hash, senha)

class Ativo(db.Model):
    __tablename__ = "ativos"
    id       = db.Column(db.Integer, primary_key=True)
    user_id  = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    symbol   = db.Column(db.String(12), nullable=False)
    tipo     = db.Column(db.String(10), default="acao")   # acao | fii
    qty      = db.Column(db.Float, default=0)
    pm       = db.Column(db.Float, default=0)
    adicionado_em = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint("user_id", "symbol"),)

class UserData(db.Model):
    __tablename__ = "user_data"
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    key        = db.Column(db.String(64), nullable=False)
    value      = db.Column(db.Text, nullable=False, default="{}")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint("user_id", "key"),)


# ══════════════════════════════════════════════════════════════════════════
# Sistema de Dividendos — histórico, classificação de frequência e sync log
# ══════════════════════════════════════════════════════════════════════════

class Provento(db.Model):
    """Um evento de provento (dividendo, JCP, rendimento de FII) de um ativo."""
    __tablename__ = "proventos"

    id              = db.Column(db.Integer, primary_key=True)
    ticker          = db.Column(db.String(12), nullable=False, index=True)
    nome_ativo      = db.Column(db.String(120))
    tipo_ativo      = db.Column(db.String(10))     # acao | fii
    tipo_provento   = db.Column(db.String(20), nullable=False, default="dividendo")
    # dividendo | jcp | rendimento | bonificacao

    data_anuncio    = db.Column(db.Date)
    data_com        = db.Column(db.Date)
    data_ex         = db.Column(db.Date, index=True)
    data_pagamento  = db.Column(db.Date, nullable=False, index=True)

    valor           = db.Column(db.Numeric(14, 6), nullable=False)
    moeda           = db.Column(db.String(3), default="BRL")
    fonte           = db.Column(db.String(30))       # twelvedata | brapi | static_db
    declarado_futuro= db.Column(db.Boolean, default=False)  # anunciado mas ainda não pago

    atualizado_em   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("ticker", "data_pagamento", "tipo_provento", name="uq_provento_evento"),
        db.Index("ix_provento_ticker_data", "ticker", "data_pagamento"),
    )


class EventoCorporativo(db.Model):
    """Desdobramentos / grupamentos usados para ajustar histórico."""
    __tablename__ = "eventos_corporativos"

    id        = db.Column(db.Integer, primary_key=True)
    ticker    = db.Column(db.String(12), nullable=False, index=True)
    tipo      = db.Column(db.String(20))   # desdobramento | grupamento | mudanca_ticker
    data      = db.Column(db.Date, nullable=False)
    fator     = db.Column(db.Float)        # ex.: 2.0 = desdobrou 1:2
    detalhe   = db.Column(db.String(200))
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)


class TickerFrequencia(db.Model):
    """Cache da classificação de frequência de pagamento de cada ativo."""
    __tablename__ = "ticker_frequencia"

    ticker               = db.Column(db.String(12), primary_key=True)
    frequencia           = db.Column(db.String(12))   # Mensal|Bimestral|Trimestral|Semestral|Anual|Irregular
    confianca            = db.Column(db.Float, default=0.0)   # 0..1
    mudou_recentemente   = db.Column(db.Boolean, default=False)
    meses_pagamento      = db.Column(db.String(60))   # "3,6,9,12"
    ultima_sincronizacao = db.Column(db.DateTime)


class SyncLog(db.Model):
    """Log de auditoria das sincronizações de proventos."""
    __tablename__ = "sync_logs"

    id         = db.Column(db.Integer, primary_key=True)
    ticker     = db.Column(db.String(12), index=True)
    status     = db.Column(db.String(10))   # ok | erro | vazio
    fonte      = db.Column(db.String(30))
    qtd_eventos= db.Column(db.Integer, default=0)
    detalhe    = db.Column(db.Text)
    criado_em  = db.Column(db.DateTime, default=datetime.utcnow, index=True)
