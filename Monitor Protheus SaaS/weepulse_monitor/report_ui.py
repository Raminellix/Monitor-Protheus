import json
import urllib.request
import ssl
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, jsonify, current_app
from flask_login import login_required
from .extensions import db
from .models import AppConfig, Server, TableGrowthLog
from .servers_monitor import collect_server_health, _run_ps_local, _run_ps_winrm_via_tempfile, _parse_ps_json, _is_local_target
from .mailer import send_alert_email

report_bp = Blueprint("report", __name__)

def collect_table_growth_now(app):
    """Executado a cada 1 hora pelo background_worker para gravar o snapshot das tabelas"""
    with app.app_context():
        cfg = AppConfig.query.first()
        if not cfg or not cfg.sql_host or not cfg.sql_user:
            return
            
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
            
            tables = ["CT2010", "SF1010", "SF2010", "SE5010", "SA1010", "SA2010", "SB1010"]
            for t in tables:
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {t} WHERE D_E_L_E_T_ <> '*'")
                    row = cursor.fetchone()
                    if row:
                        log = TableGrowthLog(table_name=t, record_count=row[0])
                        db.session.add(log)
                except Exception as e:
                    print(f"[ERRO SQL RELATORIO] Falha ao ler {t}: {e}")
                    
            db.session.commit()
            conn.close()
        except ImportError:
            print("[ERRO SQL RELATORIO] pyodbc não instalado.")
        except Exception as e:
            print(f"[ERRO SQL RELATORIO] Falha na conexão com SQL: {e}")

def get_table_growth_data(exec_log):
    """Recupera os últimos dois registros para calcular o crescimento (hora atual vs última hora)"""
    exec_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando leitura do historico de crescimento das tabelas SQL...")
    tables = ["CT2010", "SF1010", "SF2010", "SE5010", "SA1010", "SA2010", "SB1010"]
    
    table_desc = {
        "CT2": "Contabilizações",
        "SF1": "Notas de Entrada",
        "SF2": "Notas de Saída",
        "SE5": "Movimento Financeiro",
        "SA1": "Clientes",
        "SA2": "Fornecedores",
        "SB1": "Produtos"
    }
    
    res = []
    for t in tables:
        prefix = t[:3]
        desc = table_desc.get(prefix, "Tabela do Sistema")
        
        logs = TableGrowthLog.query.filter_by(table_name=t).order_by(TableGrowthLog.timestamp.desc()).limit(2).all()
        if len(logs) > 0:
            current_count = logs[0].record_count
            prev_count = logs[1].record_count if len(logs) > 1 else current_count
            growth = current_count - prev_count
            res.append({
                "table": t,
                "description": desc,
                "current": current_count,
                "growth": growth,
                "last_update": logs[0].timestamp.strftime("%d/%m/%Y %H:%M")
            })
            exec_log.append(f"   - Tabela {t}: Atual {current_count} | Crescimento: {growth}")
        else:
            res.append({"table": t, "description": desc, "current": 0, "growth": 0, "last_update": "A aguardar a próxima hora"})
            exec_log.append(f"   - Tabela {t}: Sem dados (aguardando a proxima hora)")
    return res

def fetch_broker_users(exec_log):
    """Varre os serviços, descobre as URLs candidatas e o Python testa até achar o Broker correto"""
    servers = Server.query.all()
    last_url_attempted = ""
    last_ini_attempted = ""
    
    exec_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando varredura do AppServer Broker na rede...")
    
    for s in servers:
        exec_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] ---> Analisando servidor: {s.name} ({s.address})")
        
        ps_script = """
        $ErrorActionPreference = 'SilentlyContinue'
        $debugLog = @()
        $candidates = @()

        $debugLog += "Iniciando varredura WMI de servicos..."

        try {
            $svcs = Get-CimInstance Win32_Service | Where-Object { $_.State -eq 'Running' -and ($_.PathName -match 'appserver|broker' -or $_.Name -match 'protheus|totvs|appserver|broker' -or $_.DisplayName -match 'protheus|totvs|appserver|broker') }
            
            if (-not $svcs) { 
                $debugLog += "Nenhum servico em execucao encontrado com os termos esperados."
            } else {
                foreach ($svc in $svcs) {
                    $pathName = $svc.PathName
                    if (-not $pathName) { continue }
                    
                    $exePath = ""
                    if ($pathName -match '^"?([^"]+\\.exe)"?') {
                        $exePath = $matches[1]
                    } else {
                        $exePath = ($pathName -split ' -')[0] -replace '^"','' -replace '"$',''
                    }
                    
                    $iniPath = ""
                    $argsArray = $pathName -split '\\s+'
                    for ($i=0; $i -lt $argsArray.Length; $i++) {
                        if ($argsArray[$i] -match '(?i)^[-/]ini=(.*)') {
                            $iniPath = $matches[1] -replace '^"','' -replace '"$','' -replace "^'","" -replace "'$",""; break
                        } elseif ($argsArray[$i] -match '(?i)^[-/]ini$') {
                            if ($i + 1 -lt $argsArray.Length) {
                                $iniPath = $argsArray[$i+1] -replace '^"','' -replace '"$','' -replace "^'","" -replace "'$",""; break
                            }
                        }
                    }
                    
                    if ([string]::IsNullOrWhiteSpace($iniPath)) {
                        $iniPath = [System.IO.Path]::ChangeExtension($exePath, ".ini")
                    }
                    
                    if (-not [System.IO.Path]::IsPathRooted($iniPath)) {
                        $iniPath = Join-Path ([System.IO.Path]::GetDirectoryName($exePath)) $iniPath
                    }
                    
                    if (Test-Path $iniPath) {
                        $content = Get-Content $iniPath -ErrorAction SilentlyContinue
                        $port = $null
                        $isHttps = $false
                        
                        foreach ($line in $content) {
                            $line = $line.Trim()
                            if ($line.StartsWith(";")) { continue }
                            if ($line -match '(?i)^\\s*local_server_port\\s*=\\s*(\\d+)') { $port = $matches[1] }
                            if ($line -match '(?i)^\\s*ssl_certificate\\s*=') { $isHttps = $true }
                        }
                        
                        if ($port) {
                            $scheme = if ($isHttps) { "https" } else { "http" }
                            $url = "${scheme}://127.0.0.1:${port}/totvs_broker_query/userinfo"
                            $debugLog += "   >> CANDIDATO ENCONTRADO: Porta $port ($scheme). URL Base: $url | INI: $iniPath"
                            
                            $candidates += @{ url = $url; ini_used = $iniPath }
                        }
                    }
                }
            }
        } catch {
            $debugLog += "ERRO WMI: $($_.Exception.Message)"
        }

        $resObj = @{ candidates = $candidates; debug = $debugLog }
        Write-Output "===JSON-START==="
        Write-Output ($resObj | ConvertTo-Json -Depth 4 -Compress)
        Write-Output "===JSON-END==="
        """
        
        try:
            exec_log.append(f"   [PowerShell] Lendo diretorios e INIs em {s.address}...")
            
            if _is_local_target(s.address):
                out = _run_ps_local(ps_script)
            else:
                out, err, sc = _run_ps_winrm_via_tempfile(s.address, ps_script)
            
            data = _parse_ps_json(out)
            
            if data:
                if "debug" in data:
                    for line in data["debug"]:
                        exec_log.append(f"      [PS] {line}")
                
                candidates = data.get("candidates", [])
                
                if isinstance(candidates, dict):
                    candidates = [candidates]
                elif not isinstance(candidates, list):
                    candidates = []
                
                if not candidates:
                    exec_log.append(f"   [PYTHON] Nenhum candidato de Broker encontrado neste servidor.")
                
                for cand in candidates:
                    if not isinstance(cand, dict): continue
                    
                    raw_url = cand.get("url", "")
                    if not raw_url: continue
                    
                    ext_url = raw_url.replace("127.0.0.1", s.address)
                    last_url_attempted = ext_url
                    last_ini_attempted = cand.get("ini_used", "")
                    
                    exec_log.append(f"   [PYTHON] Testando candidato: {ext_url}")
                    
                    raw_data = None
                    try:
                        ctx = ssl.create_default_context()
                        ctx.check_hostname = False
                        ctx.verify_mode = ssl.CERT_NONE
                        
                        req = urllib.request.Request(ext_url)
                        with urllib.request.urlopen(req, context=ctx, timeout=4) as response:
                            resp_bytes = response.read()
                            try:
                                resp_text = resp_bytes.decode('utf-8')
                            except UnicodeDecodeError:
                                resp_text = resp_bytes.decode('latin-1', errors='replace')
                            
                            raw_data = json.loads(resp_text)
                            exec_log.append(f"   [PYTHON] SUCESSO! Download do JSON concluido.")
                    except Exception as req_e:
                        exec_log.append(f"   [PYTHON FALHA REST] Nao foi possivel baixar o JSON: {str(req_e)}")
                        continue

                    users_list = []
                    if raw_data:
                        if isinstance(raw_data, dict):
                            if "user_info" in raw_data:
                                users_list = raw_data["user_info"]
                            elif "users" in raw_data:
                                users_list = raw_data["users"]
                            else:
                                exec_log.append(f"   [PYTHON AVISO] JSON retornado não contem 'user_info' ou 'users'. Chaves retornadas: {list(raw_data.keys())}")
                        elif isinstance(raw_data, list):
                            users_list = raw_data
                    else:
                        exec_log.append(f"   [PYTHON AVISO] O JSON retornado pelo endpoint veio vazio ou nulo.")
                    
                    if isinstance(users_list, list) and len(users_list) > 0:
                        keys_ordered = list(users_list[0].keys())
                        exec_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Extracao de dados concluida: {len(users_list)} utilizadores encontrados! FIM DA BUSCA.")
                        return {"url": ext_url, "ini": last_ini_attempted, "users": users_list, "keys": keys_ordered}
                    else:
                        exec_log.append(f"   [PYTHON AVISO] Endpoint acessado com sucesso, porem a lista de usuarios esta vazia/zerada.")
                        
        except Exception as e:
            exec_log.append(f"   [ERRO GLOBAL] Falha ao processar servidor {s.name}: {str(e)}")
            
    exec_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Varredura concluida em todos os servidores. Nenhum usuario processado.")
    return {"url": last_url_attempted, "ini": last_ini_attempted, "users": [], "keys": []}

@report_bp.route("/", methods=["GET"])
@login_required
def report_view():
    return render_template("pages/report.html")

@report_bp.route("/data", methods=["GET"])
@login_required
def get_report_data():
    cfg = AppConfig.query.first()
    servers = Server.query.all()
    
    execution_log = []
    execution_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] ===================================================")
    execution_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] INICIANDO GERACAO DO RELATORIO GLOBAL")
    execution_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] ===================================================")
    
    health_data = []
    execution_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Inspecionando saude de {len(servers)} servidor(es) (CPU/MEM)...")
    for s in servers:
        h = collect_server_health(s.address, s.name, cfg)
        health_data.append(h)
        execution_log.append(f"   - {s.name} ({s.address}): CPU {h.get('cpu_percent')}% | RAM {h.get('mem_percent')}%")
        
    growth_data = get_table_growth_data(execution_log)
    broker_info = fetch_broker_users(execution_log)
    
    config_data = {
        "smtp": f"{cfg.smtp_host}:{cfg.smtp_port}" if cfg and cfg.smtp_host else "-",
        "sql": f"{cfg.sql_host} ({cfg.sql_database})" if cfg and cfg.sql_host else "-",
        "winrm": cfg.winrm_user if cfg and cfg.winrm_user else "-"
    }
    
    execution_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] ===================================================")
    execution_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] RELATORIO CONCLUIDO COM SUCESSO.")
    
    return jsonify({
        "ok": True,
        "configs": config_data,
        "health": health_data,
        "tables": growth_data,
        "broker": broker_info,
        "execution_log": execution_log
    })

@report_bp.route("/table-history", methods=["GET"])
@login_required
def table_history_data():
    start_date = request.args.get("start")
    end_date = request.args.get("end")
    table_filter = request.args.get("table")
    
    query = TableGrowthLog.query
    
    if start_date:
        try:
            query = query.filter(TableGrowthLog.timestamp >= datetime.strptime(start_date, "%Y-%m-%d"))
        except: pass
    if end_date:
        try:
            ed = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            query = query.filter(TableGrowthLog.timestamp < ed)
        except: pass
    if table_filter and table_filter != "TODAS":
        query = query.filter(TableGrowthLog.table_name == table_filter)
        
    logs = query.order_by(TableGrowthLog.table_name.asc(), TableGrowthLog.timestamp.desc()).all()
    
    table_desc = {
        "CT2": "Contabilizações", "SF1": "Notas de Entrada", "SF2": "Notas de Saída",
        "SE5": "Movimento Financeiro", "SA1": "Clientes", "SA2": "Fornecedores", "SB1": "Produtos"
    }
    
    results = []
    for i, l in enumerate(logs):
        prefix = l.table_name[:3]
        desc = table_desc.get(prefix, "Tabela do Sistema")
        
        growth = 0
        if i + 1 < len(logs) and logs[i+1].table_name == l.table_name:
            growth = l.record_count - logs[i+1].record_count
        else:
            # MAGIA AQUI: Busca no banco o registo imediatamente anterior a esta data para calcular o salto inicial
            prev_log = TableGrowthLog.query.filter(
                TableGrowthLog.table_name == l.table_name,
                TableGrowthLog.timestamp < l.timestamp
            ).order_by(TableGrowthLog.timestamp.desc()).first()
            
            if prev_log:
                growth = l.record_count - prev_log.record_count
            
        results.append({
            "timestamp": l.timestamp.strftime("%d/%m/%Y %H:%M"),
            "table": l.table_name,
            "description": desc,
            "records": l.record_count,
            "growth": growth
        })
        
    results.sort(key=lambda x: datetime.strptime(x["timestamp"], "%d/%m/%Y %H:%M"), reverse=True)
    
    return jsonify({"ok": True, "history": results})

@report_bp.route("/send-email", methods=["POST"])
@login_required
def send_email():
    html_content = request.json.get("html_content")
    if not html_content:
        return jsonify({"ok": False, "message": "Conteúdo do relatório vazio."})
        
    cfg = AppConfig.query.first()
    if not cfg or not cfg.alert_email_to:
        return jsonify({"ok": False, "message": "E-mail de destino não está configurado. Vá a 'Configurações' e defina o destinatário."})
        
    success, msg = send_alert_email("Relatório Gerencial Weepulse", html_content, title="STATUS DA INFRAESTRUTURA TOTVS PROTHEUS")
    return jsonify({"ok": success, "message": msg})
