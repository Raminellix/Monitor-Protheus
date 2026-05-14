import json
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required
from .extensions import db
from .models import AppConfig, AlertSnapshot, Server

alerts_bp = Blueprint("alerts_snap", __name__)

def get_sql_whoisactive(cfg):
    """Executa a sp_WhoIsActive no SQL Server e retorna os dados"""
    if not cfg or not cfg.sql_host: 
        return [{"Erro": "SQL Server não configurado."}]
    try:
        import pyodbc
        conn_str = (
            f"DRIVER={{SQL Server}};"
            f"SERVER={cfg.sql_host};"
            f"DATABASE={cfg.sql_database};"
            f"UID={cfg.sql_user};"
            f"PWD={cfg.sql_password}"
        )
        conn = pyodbc.connect(conn_str, timeout=10)
        cursor = conn.cursor()
        
        # Executa a SP
        cursor.execute("EXEC sp_WhoIsActive")
        
        # Extrai o nome das colunas
        columns = [column[0] for column in cursor.description]
        results = []
        
        for row in cursor.fetchall():
            row_dict = {}
            for i, col in enumerate(columns):
                row_dict[col] = str(row[i]) if row[i] is not None else ""
            results.append(row_dict)
            
        conn.close()
        return results
    except Exception as e:
        return [{"Erro_SQL": f"Falha ao executar sp_WhoIsActive: {str(e)} (A SP existe no banco e o usuário tem permissão GRANT EXECUTE?)"}]

def take_snapshot(app, cfg, health_results, fresh_alerts):
    """Tira a 'fotografia' do momento exato do alerta"""
    
    from .report_ui import fetch_broker_users
    
    trigger_server = ", ".join([a["server"] for a in fresh_alerts])
    trigger_reasons = "\n".join([f"{a['server']}: {', '.join(a['reasons'])}" for a in fresh_alerts])
    
    print(f"[CAIXA PRETA] Gravando snapshot. Gatilho: {trigger_server}")
    
    # Puxa o Broker
    exec_log = []
    broker_info = fetch_broker_users(exec_log)
    
    # Puxa o SQL
    sql_data = get_sql_whoisactive(cfg)
    
    snap = AlertSnapshot(
        timestamp=datetime.now(),
        trigger_server=trigger_server,
        trigger_reasons=trigger_reasons,
        servers_data=json.dumps(health_results),
        broker_data=json.dumps(broker_info),
        sql_data=json.dumps(sql_data)
    )
    db.session.add(snap)
    db.session.commit()
    print("[CAIXA PRETA] Snapshot salvo com sucesso!")

@alerts_bp.route("/", methods=["GET"])
@login_required
def alerts_view():
    servers = Server.query.all()
    return render_template("pages/alerts.html", servers=servers)

@alerts_bp.route("/data", methods=["GET"])
@login_required
def alerts_data():
    date_filter = request.args.get("date")
    server_filter = request.args.get("server")
    
    query = AlertSnapshot.query
    
    if date_filter:
        try:
            # INTELIGÊNCIA: Aceita tanto o formato americano (YYYY-MM-DD) quanto o brasileiro (DD/MM/YYYY)
            if "/" in date_filter:
                target_date = datetime.strptime(date_filter, "%d/%m/%Y").date()
            else:
                target_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
                
            query = query.filter(db.func.date(AlertSnapshot.timestamp) == target_date)
        except Exception as e:
            print(f"[ERRO FILTRO DE DATA] O formato da data ({date_filter}) não pôde ser lido: {e}")
            pass
        
    if server_filter and server_filter != "TODOS":
        query = query.filter(AlertSnapshot.trigger_server.contains(server_filter))
        
    snapshots = query.order_by(AlertSnapshot.timestamp.desc()).limit(100).all()
    
    results = []
    for s in snapshots:
        results.append({
            "id": s.id,
            "timestamp": s.timestamp.strftime("%d/%m/%Y %H:%M:%S"),
            "server": s.trigger_server,
            "reasons": s.trigger_reasons,
            "servers_data": json.loads(s.servers_data) if s.servers_data else [],
            "broker_data": json.loads(s.broker_data) if s.broker_data else {},
            "sql_data": json.loads(s.sql_data) if s.sql_data else []
        })
        
    return jsonify({"ok": True, "snapshots": results})