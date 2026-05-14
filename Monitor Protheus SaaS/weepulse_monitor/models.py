from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from .extensions import db

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now) 

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

class Server(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    address = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), default="ERP")
    created_at = db.Column(db.DateTime, default=datetime.now) 
    
    metrics = db.relationship('ServerMetric', backref='server', cascade='all, delete-orphan', lazy='dynamic')

class AppConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    smtp_profile = db.Column(db.String(255), default="")
    smtp_host = db.Column(db.String(255), default="")
    smtp_port = db.Column(db.Integer, default=587)
    smtp_user = db.Column(db.String(255), default="")
    smtp_password = db.Column(db.String(255), default="")
    smtp_tls = db.Column(db.Boolean, default=True)
    alert_email_to = db.Column(db.String(255), default="")

    disk_min_free_percent = db.Column(db.Float, default=10.0)
    cpu_max_percent = db.Column(db.Float, default=85.0)
    mem_max_percent = db.Column(db.Float, default=85.0)

    sql_host = db.Column(db.String(255), default="")
    sql_user = db.Column(db.String(255), default="")
    sql_password = db.Column(db.String(255), default="")
    sql_database = db.Column(db.String(255), default="")
    
    winrm_user = db.Column(db.String(255), default="")
    winrm_password = db.Column(db.String(255), default="")
    winrm_transport = db.Column(db.String(50), default="ntlm")
    winrm_ssl = db.Column(db.Boolean, default=False)
    
    scheduled_time = db.Column(db.String(10), default="")
    scheduled_days = db.Column(db.String(50), default="")
    
    cleanup_time = db.Column(db.String(10), default="")
    webhook_url = db.Column(db.String(255), default="")

class HighlightKeyword(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    keyword = db.Column(db.String(128), nullable=False)
    bg_color = db.Column(db.String(32), default="#dc3545")
    fg_color = db.Column(db.String(32), default="#000000")
    
class ServiceMonitor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    server_address = db.Column(db.String(150), nullable=False)
    service_key = db.Column(db.String(150), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    last_alert = db.Column(db.DateTime, nullable=True)
    is_ignored = db.Column(db.Boolean, default=False)

class ServerMetric(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey('server.id', ondelete='CASCADE'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.now, index=True) 
    cpu_percent = db.Column(db.Float, default=0.0)
    mem_percent = db.Column(db.Float, default=0.0)

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.now, index=True) 
    author = db.Column(db.String(120), nullable=False)
    action = db.Column(db.String(50), nullable=False) 
    target = db.Column(db.String(255), nullable=False)
    details = db.Column(db.Text, nullable=True)

class LogAnomaly(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.now, index=True) 
    server_name = db.Column(db.String(150), nullable=False)
    service_name = db.Column(db.String(150), nullable=False)
    keyword = db.Column(db.String(100), nullable=False) 
    stack_trace = db.Column(db.Text, nullable=False)    
    hash_id = db.Column(db.String(64), unique=True, nullable=False) 

class RadarIgnoredService(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    server_address = db.Column(db.String(150), nullable=False)
    service_name = db.Column(db.String(150), nullable=False)

class TableGrowthLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.now, index=True)
    table_name = db.Column(db.String(50), nullable=False)
    record_count = db.Column(db.BigInteger, nullable=False)
    
class AlertSnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    trigger_server = db.Column(db.String(100))
    trigger_reasons = db.Column(db.Text)
    servers_data = db.Column(db.Text)  # JSON com a CPU/RAM e Top 10 de todos os servidores
    broker_data = db.Column(db.Text)   # JSON com os usuários do Broker
    sql_data = db.Column(db.Text)      # JSON com o resultado da sp_WhoIsActive
