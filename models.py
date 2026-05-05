from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import json

db = SQLAlchemy()

class User(UserMixin, db.Model):
    __tablename__ = "users"
    id         = db.Column(db.Integer, primary_key=True)
    nome       = db.Column(db.String(80), nullable=False)
    email      = db.Column(db.String(120), unique=True, nullable=False)
    senha_hash = db.Column(db.String(256), nullable=False)
    criado_em  = db.Column(db.DateTime, default=datetime.utcnow)
    ativos     = db.relationship("Ativo", backref="user", lazy=True, cascade="all, delete-orphan")

    def set_senha(self, senha):
        self.senha_hash = generate_password_hash(senha)

    def check_senha(self, senha):
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
