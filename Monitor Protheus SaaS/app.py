import os
from weepulse_monitor import create_app
from weepulse_monitor.extensions import db
from weepulse_monitor.sql_explorer import sql_explorer_bp

app = create_app()

if __name__ == "__main__":
    # Garante que o banco de dados e todas as tabelas/colunas novas sejam criados
    with app.app_context():
        db.create_all()

    # debug=True expõe o console interativo do Werkzeug na rede — nunca use em produção.
    # Para ativar temporariamente em desenvolvimento, defina a variável de ambiente:
    #   set FLASK_DEBUG=true   (Windows CMD)
    #   $env:FLASK_DEBUG="true"  (PowerShell)
    debug_mode = os.getenv("FLASK_DEBUG", "false").strip().lower() == "true"
    app.run(host="0.0.0.0", port=5001, debug=debug_mode)
