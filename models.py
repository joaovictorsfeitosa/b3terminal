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
