import os
from datetime import timedelta
from flask import Flask, session, request
from sqlalchemy import text

from .extensions import db, login_manager
from .models import User
from .auth import auth_bp
from .services_scan import services_bp
from .servers_monitor import servers_bp
from .requirements_check import requirements_bp
from .protheus_logs import logs_bp
from .db_monitor import dbmon_bp
from .doc_env import doc_env_bp
from .settings_ui import settings_bp
from .noc import noc_bp
from .report_ui import report_bp 
from .alerts_snapshot import alerts_bp 
from .sql_explorer import sql_explorer_bp
from config import Config

from .background_worker import start_background_worker

def _enable_sqlite_pragmas(app: Flask):
    uri = (app.config.get("SQLALCHEMY_DATABASE_URI") or "").lower()
    if not uri.startswith("sqlite"):
        return

    with app.app_context():
        try:
            db.session.execute(text("PRAGMA journal_mode=WAL;"))
            db.session.execute(text("PRAGMA synchronous=NORMAL;"))
            db.session.execute(text("PRAGMA busy_timeout=30000;")) 
            db.session.commit()
        except Exception as e:
            try: db.session.rollback()
            except Exception: pass
            print(f"[DB] Falha ao aplicar PRAGMAs SQLite: {e}")


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    app.config["SECRET_KEY"] = os.urandom(24)
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=15)
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
    app.config.setdefault("SQLALCHEMY_ENGINE_OPTIONS", {
        "connect_args": { "check_same_thread": False, "timeout": 30 },
        "pool_pre_ping": True,
    })

    db.init_app(app)
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    @app.before_request
    def gerenciar_inatividade():
        if request.endpoint in ["static", "login", "auth.login"]:
            return

        rotas_silenciosas = ["services.scan_json", "noc.get_noc_data", "servers.scan_json", "servers.history_json"]

        if request.endpoint not in rotas_silenciosas:
            session.permanent = True
            session.modified = True

    app.register_blueprint(auth_bp)
    app.register_blueprint(services_bp, url_prefix="/services")
    app.register_blueprint(servers_bp, url_prefix="/servers")
    app.register_blueprint(requirements_bp, url_prefix="/requirements")
    app.register_blueprint(logs_bp, url_prefix="/protheus-logs")
    app.register_blueprint(dbmon_bp, url_prefix="/db-monitor")
    app.register_blueprint(doc_env_bp, url_prefix="/doc-env")
    app.register_blueprint(settings_bp, url_prefix="/settings")
    app.register_blueprint(noc_bp, url_prefix="/noc")
    app.register_blueprint(report_bp, url_prefix="/report") 
    app.register_blueprint(alerts_bp, url_prefix="/alerts-snap") 
    app.register_blueprint(sql_explorer_bp)

    with app.app_context():
        db.create_all()

    _enable_sqlite_pragmas(app)
    start_background_worker(app)

    return app
