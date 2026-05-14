import os
import re
import socket
import subprocess
import base64
import json
import html
import hashlib
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required
from .models import Server, HighlightKeyword, AppConfig, LogAnomaly, RadarIgnoredService
from .extensions import db
from .mailer import send_alert_email

logs_bp = Blueprint("protheus_logs", __name__)

# =============================================================================
# WinRM & PowerShell Helpers
# =============================================================================

def _is_local_target(address: str) -> bool:
    a = (address or "").strip().lower()
    if a in ("localhost", "127.0.0.1", "::1"): return True
    try:
        if a == socket.gethostname().lower(): return True
    except Exception: pass
    return False

def _run_ps_local(ps: str):
    ps = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + ps
    encoded_ps = base64.b64encode(ps.encode('utf-16le')).decode('utf-8')
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_ps], capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=120)
    return r.stdout or ""

def _run_ps_winrm(address: str, ps: str):
    import winrm
    cfg = AppConfig.query.first()
    user = cfg.winrm_user if cfg and cfg.winrm_user else ""
    pwd = cfg.winrm_password if cfg and cfg.winrm_password else ""
    transport = cfg.winrm_transport if cfg and cfg.winrm_transport else "ntlm"
    use_ssl = cfg.winrm_ssl if cfg else False
    scheme = "https" if use_ssl else "http"
    port = 5986 if use_ssl else 5985
    ps = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + ps
    encoded_ps = base64.b64encode(ps.encode('utf-16le')).decode('utf-8')
    session = winrm.Session(f"{scheme}://{address}:{port}/wsman", auth=(user, pwd), transport=transport, operation_timeout_sec=120, read_timeout_sec=130)
    r = session.run_cmd('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded_ps])
    return (r.std_out or b"").decode('utf-8', errors="ignore")

def clean_exe_path(path_name: str):
    if not path_name: return ""
    match = re.search(r'^"?([^"]+\.exe)"?', path_name, re.IGNORECASE)
    return match.group(1) if match else path_name.replace('"', '').split(' -')[0]


# =============================================================================
# CAÇADOR PROATIVO DE ANOMALIAS (RADAR 24/7 DINÂMICO)
# =============================================================================
def run_proactive_log_hunter(app):
    with app.app_context():
        servers = Server.query.all()
        cfg = AppConfig.query.first()
        
        db_keywords = HighlightKeyword.query.all()
        keywords = [k.keyword for k in db_keywords]
        
        if not keywords:
            return True
            
        kw_array_str = "@(" + ",".join([f"'{k}'" for k in keywords]) + ")"
        ignored_db = RadarIgnoredService.query.all()

        for s in servers:
            ignored_for_server = [i.service_name.lower() for i in ignored_db if i.server_address == s.address]
            ignored_str = "@(" + ",".join([f"'{ign}'" for ign in ignored_for_server]) + ")"

            # NOTA: O regex foi escapado com barras duplas (\\s e \\[) para evitar o erro do Python
            ps_script = f"""
            $ErrorActionPreference = 'SilentlyContinue'
            $keywords = {kw_array_str}
            $ignored = {ignored_str}
            $results = @()

            $svcs = Get-CimInstance Win32_Service | Select-Object Name, DisplayName, PathName
            foreach ($svc in $svcs) {{
                $name = $svc.Name; $disp = $svc.DisplayName; $path = $svc.PathName
                
                if ($ignored -contains $name.ToLower()) {{ continue }}

                $hay = "$name $disp".ToLower()
                $isProtheus = $false
                if ($hay -match "appserver" -or $hay -match "dbaccess") {{ $isProtheus = $true }}
                if ($path -match "system32") {{ $isProtheus = $false }}

                if ($isProtheus -and $path) {{
                    $exePath = $path -replace '^"|"$','' -replace ' -.*',''
                    $dir = [System.IO.Path]::GetDirectoryName($exePath)
                    $logName = if ($exePath -match '(?i)dbaccess') {{ "dbconsole.log" }} else {{ "console.log" }}
                    
                    $iniPath = [System.IO.Path]::ChangeExtension($exePath, ".ini")
                    $logPath = Join-Path $dir $logName
                    
                    if (Test-Path $iniPath) {{
                        $iniContent = Get-Content $iniPath -ErrorAction SilentlyContinue
                        $inGeneral = $false
                        foreach ($line in $iniContent) {{
                            if ($line -match '^\\s*\\[(?<sec>.*?)\\]') {{ $inGeneral = ($matches.sec -match '^GENERAL$') }}
                            elseif ($inGeneral -and $line -match '^\\s*consolefile\\s*=\\s*(?<val>.*)') {{
                                $candidate = $matches.val.Trim()
                                if (-not [System.IO.Path]::IsPathRooted($candidate)) {{ $logPath = Join-Path $dir $candidate }} 
                                else {{ $logPath = $candidate }}
                                break
                            }}
                        }}
                    }}

                    if (Test-Path $logPath) {{
                        $lines = Get-Content $logPath -Tail 2000 -ErrorAction SilentlyContinue
                        $capture = 0
                        $currentBlock = @()
                        $currentKw = ""

                        foreach ($line in $lines) {{
                            $foundKw = $false
                            foreach ($kw in $keywords) {{
                                if ($line -match $kw) {{ $foundKw = $true; $currentKw = $kw; break }}
                            }}

                            if ($foundKw) {{
                                if ($currentBlock.Count -gt 0) {{
                                    $results += [PSCustomObject]@{{ service=$name; keyword=$currentKw; trace=($currentBlock -join "`n") }}
                                }}
                                $capture = 15 
                                $currentBlock = @($line)
                            }} elseif ($capture -gt 0) {{
                                $currentBlock += $line
                                $capture--
                                if ($capture -eq 0) {{
                                    $results += [PSCustomObject]@{{ service=$name; keyword=$currentKw; trace=($currentBlock -join "`n") }}
                                    $currentBlock = @()
                                }}
                            }}
                        }}
                        if ($currentBlock.Count -gt 0) {{
                            $results += [PSCustomObject]@{{ service=$name; keyword=$currentKw; trace=($currentBlock -join "`n") }}
                        }}
                    }}
                }}
            }}
            Write-Output "===JSON-START==="
            Write-Output ($results | ConvertTo-Json -Compress)
            Write-Output "===JSON-END==="
            """

            try:
                out = _run_ps_local(ps_script) if _is_local_target(s.address) else _run_ps_winrm(s.address, ps_script)
                start_idx = out.find("===JSON-START===")
                end_idx = out.find("===JSON-END===")
                
                if start_idx != -1 and end_idx != -1:
                    clean_json = out[start_idx + 16:end_idx].strip()
                    if clean_json:
                        data = json.loads(clean_json)
                        if isinstance(data, dict): data = [data]
                        
                        for item in data:
                            svc_name = item.get("service", "Unknown")
                            kw = item.get("keyword", "ERROR")
                            trace = item.get("trace", "").strip()
                            
                            if not trace: continue
                            
                            raw_hash = f"{s.name}_{svc_name}_{kw}_{trace}"
                            hash_id = hashlib.sha256(raw_hash.encode('utf-8')).hexdigest()
                            
                            exists = LogAnomaly.query.filter_by(hash_id=hash_id).first()
                            if not exists:
                                new_anomaly = LogAnomaly(server_name=s.name, service_name=svc_name, keyword=kw, stack_trace=trace, hash_id=hash_id)
                                db.session.add(new_anomaly)
                                db.session.commit()
                                
                                color_map = {"ACCESS VIOLATION": "#b91c1c", "THREAD ERROR": "#ef4444", "MAX STRING SIZE": "#f59e0b"}
                                badge_color = color_map.get(kw, "#ef4444")
                                
                                email_html = f"""
                                <div style="font-family: Arial, sans-serif; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px; background-color: #ffffff;">
                                    <div style="background-color: {badge_color}; color: white; padding: 10px 15px; border-radius: 6px; margin-bottom: 15px; font-weight: bold; font-size: 16px;">
                                        ⚠️ Nova Anomalia: {kw}
                                    </div>
                                    <p style="color: #334155; margin-bottom: 5px;"><b>Servidor Localizado:</b> {s.name}</p>
                                    <p style="color: #334155; margin-bottom: 15px;"><b>Serviço Afetado:</b> {svc_name}</p>
                                    <p style="color: #0f172a; font-weight: bold;">Stack Trace Extraído (Linha do Erro e Pilha de Chamadas):</p>
                                    <div style="background-color: #0c0c0c; color: #38bdf8; padding: 15px; border-radius: 6px; font-family: 'Courier New', monospace; white-space: pre-wrap; font-size: 12px; overflow-x: auto;">{html.escape(trace)}</div>
                                </div>
                                """
                                try: send_alert_email(f"[Weepulse Radar] {kw} em {s.name}", email_html)
                                except: pass
                                
                                if cfg and getattr(cfg, 'webhook_url', None):
                                    try:
                                        from .webhook import send_webhook_alert
                                        team_title = f"⚠️ Protheus Radar: {kw}"
                                        ticks = chr(96) * 3
                                        safe_trace = trace[:600]
                                        team_msg = f"**Servidor:** {s.name}\n**Serviço:** {svc_name}\n\n{ticks}text\n{safe_trace}...\n{ticks}"
                                        send_webhook_alert(cfg.webhook_url, team_title, team_msg, "danger")
                                    except: pass

            except Exception as e:
                print(f"[RADAR DE LOGS] Falha no servidor {s.address}: {e}")
                
        return True

# =============================================================================
# Rotas Flask
# =============================================================================

@logs_bp.route("/", methods=["GET"])
@login_required
def logs_home():
    keywords = HighlightKeyword.query.order_by(HighlightKeyword.id.asc()).all()
    return render_template("pages/protheus_logs.html", keywords=keywords)

@logs_bp.route("/anomalies", methods=["GET"])
@login_required
def get_anomalies():
    anomalies = LogAnomaly.query.order_by(LogAnomaly.timestamp.desc()).limit(100).all()
    res = []
    for a in anomalies:
        res.append({
            "timestamp": a.timestamp.strftime("%d/%m/%Y %H:%M:%S"),
            "server_name": a.server_name,
            "service_name": a.service_name,
            "keyword": a.keyword,
            "stack_trace": a.stack_trace
        })
    
    recent = LogAnomaly.query.order_by(LogAnomaly.timestamp.desc()).limit(1000).all()
    stats = {}
    for r in recent:
        stats[r.keyword] = stats.get(r.keyword, 0) + 1
        
    stats_list = [{"keyword": k, "count": v} for k, v in stats.items()]
    stats_list.sort(key=lambda x: x["count"], reverse=True)

    return jsonify({"ok": True, "anomalies": res, "stats": stats_list})

@logs_bp.route("/scan-instances", methods=["GET"])
@login_required
def scan_instances():
    servers = Server.query.order_by(Server.id.asc()).all()
    ignored_db = RadarIgnoredService.query.all()
    ignored_set = {f"{i.server_address}|{i.service_name}".lower() for i in ignored_db}
    
    instances = []
    ps_script = """
    $ErrorActionPreference = 'SilentlyContinue'
    $svcs = Get-CimInstance Win32_Service | Select-Object Name, DisplayName, PathName
    Write-Output "===JSON-START==="
    Write-Output ($svcs | ConvertTo-Json -Depth 3 -Compress)
    Write-Output "===JSON-END==="
    """

    for s in servers:
        try:
            out = _run_ps_local(ps_script) if _is_local_target(s.address) else _run_ps_winrm(s.address, ps_script)
            start_idx = out.find("===JSON-START===")
            end_idx = out.find("===JSON-END===")
            
            if start_idx != -1 and end_idx != -1:
                clean_json = out[start_idx + 16:end_idx].strip()
                if clean_json:
                    data = json.loads(clean_json)
                    if isinstance(data, dict): data = [data]
                    
                    for svc in data:
                        name = svc.get("Name", "")
                        disp = svc.get("DisplayName", "")
                        path = svc.get("PathName", "")
                        
                        hay = f"{name} {disp}".lower()
                        is_protheus = False
                        
                        if "appserver" in hay: is_protheus = True
                        elif "dbaccess" in hay: is_protheus = True
                        elif "license" in hay and ("totvs" in hay or "virtual" in hay or "server" in hay): is_protheus = True
                        
                        if is_protheus:
                            if "windows" in hay and "totvs" not in hay:
                                is_protheus = False
                            if path and "system32" in path.lower():
                                is_protheus = False

                        if is_protheus:
                            exe_path = clean_exe_path(path)
                            if exe_path:
                                is_ignored = f"{s.address}|{name}".lower() in ignored_set
                                instances.append({
                                    "server": s.address,
                                    "server_name": s.name,
                                    "name": disp or name,
                                    "service_key": name, 
                                    "exe_path": exe_path,
                                    "is_ignored": is_ignored
                                })
        except: pass
    return jsonify({"ok": True, "instances": instances})

@logs_bp.route("/radar-toggle", methods=["POST"])
@login_required
def toggle_radar():
    server_address = request.json.get("server_address")
    service_name = request.json.get("service_name")
    
    ign = RadarIgnoredService.query.filter_by(server_address=server_address, service_name=service_name).first()
    if ign:
        db.session.delete(ign)
        ignored = False
    else:
        db.session.add(RadarIgnoredService(server_address=server_address, service_name=service_name))
        ignored = True
    db.session.commit()
    return jsonify({"ok": True, "ignored": ignored})

@logs_bp.route("/view", methods=["POST"])
@login_required
def view_log():
    server = request.json.get("server")
    exe_path = request.json.get("exe_path")
    try: linhas = int(request.json.get("lines", 1500))
    except: linhas = 1500
    
    ps_script = f"""
    $ErrorActionPreference = 'SilentlyContinue'
    $exePath = "{exe_path}"
    $iniPath = [System.IO.Path]::ChangeExtension($exePath, ".ini")
    $dir = [System.IO.Path]::GetDirectoryName($exePath)
    
    $logName = "console.log"
    if ($exePath -match '(?i)dbaccess') {{ $logName = "dbconsole.log" }}
    $logPath = Join-Path $dir $logName

    if (Test-Path $iniPath) {{
        $iniContent = Get-Content $iniPath -ErrorAction SilentlyContinue
        $inGeneral = $false
        foreach ($line in $iniContent) {{
            if ($line -match '^\\s*\\[(?<sec>.*?)\\]') {{ $inGeneral = ($matches.sec -match '^GENERAL$') }}
            elseif ($inGeneral -and $line -match '^\\s*consolefile\\s*=\\s*(?<val>.*)') {{
                $candidate = $matches.val.Trim()
                if (-not [System.IO.Path]::IsPathRooted($candidate)) {{ $logPath = Join-Path $dir $candidate }} 
                else {{ $logPath = $candidate }}
                break
            }}
        }}
    }}

    $res = [PSCustomObject]@{{ log_path = $logPath; content = "Arquivo de log não encontrado em: $logPath" }}

    if (Test-Path $logPath) {{
        $tail = Get-Content $logPath -Tail {linhas} -ErrorAction SilentlyContinue | Out-String
        if ($tail) {{ $res.content = $tail }} else {{ $res.content = "Log está vazio." }}
    }}

    Write-Output "===JSON-START==="
    Write-Output ($res | ConvertTo-Json -Compress)
    Write-Output "===JSON-END==="
    """

    try:
        out = _run_ps_local(ps_script) if _is_local_target(server) else _run_ps_winrm(server, ps_script)
        start_idx = out.find("===JSON-START===")
        end_idx = out.find("===JSON-END===")
        
        log_path = "Desconhecido"
        content = "Falha ao processar o script remoto."
        
        if start_idx != -1 and end_idx != -1:
            clean_json = out[start_idx + 16:end_idx].strip()
            data = json.loads(clean_json)
            log_path = data.get("log_path", "")
            content = data.get("content", "")
            
        keywords = HighlightKeyword.query.order_by(HighlightKeyword.id.asc()).all()
        return jsonify({
            "ok": True,
            "log_path": log_path,
            "content": content,
            "highlights": [{"keyword": k.keyword, "bg": k.bg_color, "fg": k.fg_color} for k in keywords],
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

@logs_bp.route("/keywords", methods=["POST"])
@login_required
def add_keyword():
    kw = request.json.get("keyword", "").strip().upper() 
    if not kw: return jsonify({"ok": False, "message": "Keyword vazia"}), 400
    bg = "#ef4444" if "ERROR" in kw else "#f59e0b"
    fg = "#000000"
    item = HighlightKeyword(keyword=kw, bg_color=bg, fg_color=fg)
    db.session.add(item)
    db.session.commit()
    return jsonify({"ok": True})

@logs_bp.route("/keywords/<int:kw_id>", methods=["DELETE"])
@login_required
def delete_keyword(kw_id):
    item = HighlightKeyword.query.get_or_404(kw_id)
    db.session.delete(item)
    db.session.commit()
    return jsonify({"ok": True})