import threading
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import login_required, current_user
from .extensions import db
from .models import AppConfig, User, Server
from .mailer import send_alert_email

settings_bp = Blueprint("settings", __name__)

def get_cfg():
    cfg = AppConfig.query.first()
    if not cfg:
        cfg = AppConfig()
        db.session.add(cfg)
        db.session.commit()
    return cfg

@settings_bp.route("/", methods=["GET", "POST"])
@login_required
def settings_home():
    cfg = get_cfg()

    if request.method == "POST":
        tab = request.form.get("tab")

        if tab == "smtp":
            cfg.smtp_host = request.form.get("smtp_host", "")
            cfg.smtp_port = int(request.form.get("smtp_port", "587"))
            cfg.smtp_user = request.form.get("smtp_user", "")
            cfg.smtp_password = request.form.get("smtp_password", "")
            cfg.smtp_tls = request.form.get("smtp_tls") == "on"
            cfg.alert_email_to = request.form.get("alert_email_to", "")
            db.session.commit()
            flash("SMTP atualizado.", "success")

        elif tab == "thresholds":
            cfg.disk_min_free_percent = float(request.form.get("disk_min_free_percent", "10"))
            cfg.cpu_max_percent = float(request.form.get("cpu_max_percent", "85"))
            cfg.mem_max_percent = float(request.form.get("mem_max_percent", "85"))
            db.session.commit()
            flash("Limites atualizados.", "success")

        elif tab == "sql":
            cfg.sql_host = request.form.get("sql_host", "")
            cfg.sql_user = request.form.get("sql_user", "")
            cfg.sql_password = request.form.get("sql_password", "")
            cfg.sql_database = request.form.get("sql_database", "")
            db.session.commit()
            flash("Configuração SQL atualizada.", "success")

        elif tab == "network":
            cfg.winrm_user = request.form.get("winrm_user", "")
            cfg.winrm_password = request.form.get("winrm_password", "")
            cfg.winrm_transport = request.form.get("winrm_transport", "ntlm")
            cfg.winrm_ssl = request.form.get("winrm_ssl") == "on"
            cfg.webhook_url = request.form.get("webhook_url", "")
            db.session.commit()
            flash("Configurações de Rede e Webhook atualizadas.", "success")
            
        elif tab == "schedule":
            cfg.scheduled_time = request.form.get("scheduled_time", "")
            days = request.form.getlist("scheduled_days")
            cfg.scheduled_days = ",".join(days)
            cfg.cleanup_time = request.form.get("cleanup_time", "")
            db.session.commit()
            flash("Agendamentos salvos com sucesso.", "success")

        elif tab == "password":
            current = request.form.get("current_password", "")
            new = request.form.get("new_password", "")
            new2 = request.form.get("new_password2", "")
            user = User.query.get(current_user.id)
            if not user.check_password(current):
                flash("Senha atual incorreta.", "danger")
            elif new != new2 or not new:
                flash("Nova senha inválida ou não confere.", "danger")
            else:
                user.set_password(new)
                db.session.commit()
                flash("Senha alterada.", "success")

        return redirect(url_for("settings.settings_home"))

    return render_template("pages/settings.html", cfg=cfg)

# ==========================================
# ROTA 1: TESTE DE E-MAIL
# ==========================================
@settings_bp.route("/test-email", methods=["POST"])
@login_required
def test_email():
    cfg = get_cfg()
    if not cfg or not cfg.smtp_host or not cfg.alert_email_to:
        return jsonify({"ok": False, "message": "Preencha e salve os dados SMTP antes de testar!"})

    status_tls = "Ativada" if cfg.smtp_tls else "Desativada"

    content = f"""
    <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 25px; font-family: 'Courier New', Courier, monospace; color: #475569; font-size: 14px; line-height: 1.8; text-align: left;">
        <div style="color: #10b981; font-weight: bold; font-size: 16px; margin-bottom: 15px; border-bottom: 1px solid #e2e8f0; padding-bottom: 10px;">
            ✔️ Conexão SMTP estabelecida com sucesso!
        </div>
        <div style="margin-bottom: 5px;"><b>Host do Servidor:</b> {cfg.smtp_host}</div>
        <div style="margin-bottom: 5px;"><b>Porta Utilizada:</b> {cfg.smtp_port}</div>
        <div style="margin-bottom: 5px;"><b>Criptografia (TLS/SSL):</b> {status_tls}</div>
        <div style="margin-bottom: 5px;"><b>Conta de Envio:</b> {cfg.smtp_user}</div>
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px dashed #cbd5e1; color: #64748b; font-size: 12px; font-family: Arial, sans-serif;">
            Se você está recebendo esta mensagem, significa que o <b>Weepulse Monitor</b> tem permissão para se comunicar com o seu provedor de e-mail.
        </div>
    </div>
    """

    success, msg = send_alert_email(
        subject="[Weepulse] Teste de Comunicação SMTP", 
        html_content=content, 
        title="TESTE DE CONEXÃO",
        intro_text="Validação de credenciais e comunicação de rede."
    )

    if success:
        return jsonify({"ok": True, "message": msg})
    else:
        return jsonify({"ok": False, "message": msg})

# ==========================================
# ROTA 2: LIMPEZA MANUAL
# ==========================================
@settings_bp.route("/run-cleanup", methods=["POST"])
@login_required
def manual_cleanup():
    servers = Server.query.all()
    if not servers:
        return jsonify({"ok": False, "message": "Nenhum servidor cadastrado para limpar."})
    
    app = current_app._get_current_object()
    
    def background_job(app_instance, server_list):
        with app_instance.app_context():
            try:
                from . import protheus_cleaner
                protheus_cleaner.run_protheus_cleanup(server_list)
            except Exception as e:
                print(f"[MANUAL CLEANUP ERRO] {e}")
                
    t = threading.Thread(target=background_job, args=(app, servers))
    t.daemon = True
    t.start()

    return jsonify({"ok": True, "message": "Ordem de faxina enviada! A varredura corre em plano de fundo e receberá o relatório por e-mail nos próximos minutos."})

# ==========================================
# ROTA 3: TESTE DE WEBHOOK (TEAMS)
# ==========================================
@settings_bp.route("/test-webhook", methods=["POST"])
@login_required
def test_webhook():
    try:
        from .webhook import send_webhook_alert
    except ImportError:
        return jsonify({"ok": False, "message": "O ficheiro webhook.py não foi encontrado!"})
        
    cfg = get_cfg()
    if not cfg or not cfg.webhook_url:
        return jsonify({"ok": False, "message": "Guarde a URL do Webhook na secção Rede antes de testar!"})

    success, msg = send_webhook_alert(
        cfg.webhook_url, 
        "✅ Teste de Comunicação Weepulse", 
        "Se está a ler isto, significa que o seu Centro de Comando está ligado a este canal e pronto para disparar alertas de queda do Protheus em tempo real!",
        "success"
    )
    
    return jsonify({"ok": success, "message": msg})

# ==========================================
# ROTA 4: TESTE DE WINRM
# ==========================================
@settings_bp.route("/test-winrm", methods=["POST"])
@login_required
def test_winrm():
    cfg = get_cfg()
    if not cfg or not cfg.winrm_user or not cfg.winrm_password:
        return jsonify({"ok": False, "message": "Salve as credenciais do WinRM antes de testar!"})

    servers = Server.query.all()
    if not servers:
        return jsonify({"ok": False, "message": "Credenciais salvas, mas você precisa de pelo menos 1 Servidor cadastrado para testar a conexão."})

    target_server = None
    
    # Inteligência para pular o localhost e testar a rede real, se houver mais máquinas
    for s in servers:
        addr = getattr(s, 'host', None) or getattr(s, 'ip', None) or getattr(s, 'hostname', None) or getattr(s, 'address', None)
        if addr and addr.lower() not in ['localhost', '127.0.0.1']:
            target_server = s
            break
            
    # Se todos forem localhost (ou a lista só tiver ele), fazemos o fallback
    if not target_server:
        target_server = servers[0]

    server_address = getattr(target_server, 'host', None) or getattr(target_server, 'ip', None) or getattr(target_server, 'hostname', None) or getattr(target_server, 'address', None)
    
    if not server_address:
         return jsonify({"ok": False, "message": "Não foi possível identificar o endereço (host/ip) do servidor no banco de dados."})

    try:
        import winrm
        protocol = 'https' if cfg.winrm_ssl else 'http'
        port = '5986' if cfg.winrm_ssl else '5985'
        endpoint = f"{protocol}://{server_address}:{port}/wsman"
        
        session = winrm.Session(
            endpoint, 
            auth=(cfg.winrm_user, cfg.winrm_password), 
            transport=cfg.winrm_transport,
            server_cert_validation='ignore'
        )
        
        r = session.run_cmd('ipconfig', ['/all'])
        
        if r.status_code == 0:
            return jsonify({"ok": True, "message": f"Conexão WinRM com {server_address} estabelecida com sucesso!"})
        else:
            return jsonify({"ok": False, "message": f"Falha na autenticação com {server_address}. Verifique usuário/senha e permissões."})
            
    except ImportError:
        return jsonify({"ok": False, "message": "A biblioteca pywinrm não está instalada. Execute: pip install pywinrm"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro de conexão com {server_address}: {str(e)}"})

# ==========================================
# ROTA 5: TESTE DE SQL
# ==========================================
@settings_bp.route("/test-sql", methods=["POST"])
@login_required
def test_sql():
    cfg = get_cfg()
    if not cfg or not cfg.sql_host or not cfg.sql_user:
        return jsonify({"ok": False, "message": "Salve os dados do Banco SQL antes de testar!"})

    try:
        import pyodbc
        
        # Utilizando o driver nativo padrão do Windows para compatibilidade universal
        conn_str = (
            f"DRIVER={{SQL Server}};"
            f"SERVER={cfg.sql_host};"
            f"DATABASE={cfg.sql_database};"
            f"UID={cfg.sql_user};"
            f"PWD={cfg.sql_password}"
        )

        conn = pyodbc.connect(conn_str, timeout=5)
        conn.close()
        return jsonify({"ok": True, "message": "Conexão com o banco estabelecida com sucesso!"})
        
    except ImportError:
         return jsonify({"ok": False, "message": "A biblioteca 'pyodbc' não está instalada. Execute no terminal: pip install pyodbc"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao conectar no banco: {str(e)}"})
