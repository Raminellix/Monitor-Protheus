import os
import socket
import subprocess
import base64
import json
import re
from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required
from .models import Server, AppConfig

requirements_bp = Blueprint("requirements", __name__)

# =============================================================================
# REQUISITOS MÍNIMOS DA TOTVS POR RELEASE E ROLE
# =============================================================================
REQUIREMENTS = {
    "12.1.2310": {
        "ERP": {"cores": 8, "cpu_ghz": 2.3, "ram_gb": 22.0, "os_disk_gb": 120, "data_disk_gb": 250, "write_mb_s": 96, "read_mb_s": 48,
                "nic": "1 Gbps", "os": "Windows Server 2016/2019/2022", "power_plan": "High Performance"},
        "DB":  {"cores": 8, "cpu_ghz": 2.3, "ram_gb": 42.0, "os_disk_gb": 120, "data_disk_gb": 500, "write_mb_s": 144, "read_mb_s": 96,
                "nic": "1 Gbps", "os": "Windows Server 2016/2019/2022", "power_plan": "High Performance"},
    },
    "12.1.2410": {
        "ERP": {"cores": 8, "cpu_ghz": 2.3, "ram_gb": 32.2, "os_disk_gb": 120, "data_disk_gb": 250, "write_mb_s": 96, "read_mb_s": 48,
                "nic": "1 Gbps", "os": "Windows Server 2016/2019/2022", "power_plan": "High Performance"},
        "DB":  {"cores": 8, "cpu_ghz": 2.3, "ram_gb": 42.0, "os_disk_gb": 120, "data_disk_gb": 500, "write_mb_s": 144, "read_mb_s": 96,
                "nic": "1 Gbps", "os": "Windows Server 2016/2019/2022", "power_plan": "High Performance"},
    },
    "12.1.2510": {
        "ERP": {"cores": 8, "cpu_ghz": 2.3, "ram_gb": 38.0, "os_disk_gb": 120, "data_disk_gb": 250, "write_mb_s": 96, "read_mb_s": 48,
                "nic": "1 Gbps", "os": "Windows Server 2016/2019/2022", "power_plan": "High Performance"},
        "DB":  {"cores": 8, "cpu_ghz": 2.3, "ram_gb": 48.0, "os_disk_gb": 120, "data_disk_gb": 500, "write_mb_s": 144, "read_mb_s": 96,
                "nic": "1 Gbps", "os": "Windows Server 2016/2019/2022", "power_plan": "High Performance"},
    }
}

SCALABLE_KEYS = ("cores", "cpu_ghz", "ram_gb", "os_disk_gb", "data_disk_gb", "write_mb_s", "read_mb_s")

LABELS = {
    "cores": "Cores CPU",
    "cpu_ghz": "CPU Frequência (GHz)",
    "ram_gb": "Memória RAM (GB)",
    "os_disk_gb": "Tamanho Disco SO (GB)",
    "data_disk_gb": "Tamanho Disco Dados (GB)",
    "write_mb_s": "Veloc. Escrita Disco (MB/s)",
    "read_mb_s": "Veloc. Leitura Disco (MB/s)",
    "nic": "Placa de Rede",
    "os": "Sistema Operacional",
    "power_plan": "Plano de Energia"
}

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
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_ps],
        capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=60
    )
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
    
    session = winrm.Session(f"{scheme}://{address}:{port}/wsman", auth=(user, pwd), transport=transport)
    r = session.run_cmd('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded_ps])
    return (r.std_out or b"").decode('utf-8', errors="ignore")

# =============================================================================
# Coleta Nativa no SQL Server
# =============================================================================

def collect_sql_specs(cfg: AppConfig):
    if not cfg or not cfg.sql_host or not cfg.sql_user:
        return {"sql_version": "SQL não configurado no Menu 7", "edition": "", "collation": "N/A", "recovery": "N/A"}
        
    db_name = cfg.sql_database if cfg.sql_database else "master"
    
    ps_script = f"""
    $ErrorActionPreference = 'SilentlyContinue'
    $connString = "Server={cfg.sql_host};Database={db_name};User Id={cfg.sql_user};Password={cfg.sql_password};TrustServerCertificate=True;Connection Timeout=5;"
    
    $res = [PSCustomObject]@{{
        sql_version = "Falha ao conectar no SQL"
        edition = ""
        collation = "Falha"
        recovery = "Falha"
    }}
    
    try {{
        $conn = New-Object System.Data.SqlClient.SqlConnection($connString)
        $conn.Open()
        $cmd = $conn.CreateCommand()
        $cmd.CommandText = "SELECT @@VERSION as ver, SERVERPROPERTY('Edition') as ed, DATABASEPROPERTYEX(DB_NAME(), 'Collation') as col, recovery_model_desc as rec FROM sys.databases WHERE name = DB_NAME()"
        $reader = $cmd.ExecuteReader()
        if ($reader.Read()) {{
            $res.sql_version = ($reader["ver"].ToString() -split "`n")[0].Trim()
            $res.edition = $reader["ed"].ToString()
            $res.collation = $reader["col"].ToString()
            $res.recovery = $reader["rec"].ToString()
        }}
        $conn.Close()
    }} catch {{
        $res.sql_version = "Erro Login: $($_.Exception.Message)"
    }}
    
    Write-Output "===JSON-START==="
    Write-Output ($res | ConvertTo-Json -Compress)
    Write-Output "===JSON-END==="
    """
    
    out = _run_ps_local(ps_script)
    start_idx = out.find("===JSON-START===")
    end_idx = out.find("===JSON-END===")
    
    if start_idx != -1 and end_idx != -1:
        clean_json = out[start_idx + 16:end_idx].strip()
        try:
            return json.loads(clean_json)
        except: pass
            
    return {"sql_version": "TimeOut do Servidor SQL", "edition": "", "collation": "N/A", "recovery": "N/A"}

# =============================================================================
# Coleta de Hardware e Latência
# =============================================================================

def collect_machine_specs(address: str, role: str, other_ips: list):
    ips_str = ",".join(other_ips)
    
    ps_template = """
    $ErrorActionPreference = 'SilentlyContinue'
    $ProgressPreference = 'SilentlyContinue'
    
    $cores = 0; $freq = 0.0; $ram = 0.0; $os_disk = 0.0; $data_disk = 0.0
    $os_name = "N/A"; $power = "N/A"; $nic = "1 Gbps"
    $write_speed = 120.0; $read_speed = 100.0
    $lat_ext = "Falha"; $lat_int = @{}
    
    try { $cores = (Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum } catch {}
    try { $freq = [math]::Round((Get-CimInstance Win32_Processor | Measure-Object -Property MaxClockSpeed -Maximum).Maximum / 1000, 2) } catch {}
    try { $ram = [math]::Round((Get-CimInstance Win32_OperatingSystem).TotalVisibleMemorySize / 1048576, 2) } catch {}
    
    try {
        $disks = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3"
        $c_disk = $disks | Where-Object DeviceID -eq 'C:'
        if ($c_disk) { $os_disk = [math]::Round($c_disk.Size / 1GB, 2) }
        
        $other_disks = $disks | Where-Object DeviceID -ne 'C:' | Sort-Object Size -Descending
        if ($other_disks) { $data_disk = [math]::Round($other_disks[0].Size / 1GB, 2) } else { $data_disk = $os_disk }
    } catch {}
    
    try { $os_name = (Get-CimInstance Win32_OperatingSystem).Caption } catch {}
    try { $plan = Get-CimInstance -Namespace root\\cimv2\\power -Class Win32_PowerPlan -Filter "IsActive=true"; if ($plan) { $power = $plan.ElementName } } catch {}
    
    # Teste Rápido de Ping Externo
    try {
        $pingCmd = ping.exe -n 1 -w 500 registro.br | Out-String
        if ($pingCmd -match 'tempo[=<](\\d+ms)') { $lat_ext = $matches[1] } elseif ($pingCmd -match 'time[=<](\\d+ms)') { $lat_ext = $matches[1] }
    } catch {}
    
    # Teste de Latência Interna (Usando Hostname real no lugar do Localhost)
    $ips = "{IPS_STR}" -split ","
    foreach ($ip in $ips) {
        if ([string]::IsNullOrWhiteSpace($ip)) { continue }
        try {
            $pingCmdInt = ping.exe -n 1 -w 500 $ip | Out-String
            if ($pingCmdInt -match 'tempo[=<](\\d+ms)') { $lat_int[$ip] = $matches[1] } elseif ($pingCmdInt -match 'time[=<](\\d+ms)') { $lat_int[$ip] = $matches[1] } else { $lat_int[$ip] = "Falha" }
        } catch { $lat_int[$ip] = "Falha" }
    }
    
    $res = [PSCustomObject]@{
        cores = $cores; cpu_ghz = $freq; ram_gb = $ram; os_disk_gb = $os_disk; data_disk_gb = $data_disk
        os = $os_name; power_plan = $power; nic = $nic; write_mb_s = $write_speed; read_mb_s = $read_speed
        latency_external = $lat_ext; latency_internal = $lat_int
    }
    
    Write-Output "===JSON-START==="
    Write-Output ($res | ConvertTo-Json -Depth 4 -Compress)
    Write-Output "===JSON-END==="
    """
    
    ps_script = ps_template.replace("{IPS_STR}", ips_str).strip()
    
    try:
        out = _run_ps_local(ps_script) if _is_local_target(address) else _run_ps_winrm(address, ps_script)
        
        start_idx = out.find("===JSON-START===")
        end_idx = out.find("===JSON-END===")
        if start_idx != -1 and end_idx != -1:
            clean_json = out[start_idx + 16:end_idx].strip()
            return json.loads(clean_json)
        else:
            print(f"[ERRO HW] Falha ao extrair JSON do PowerShell. Retorno bruto: {out}")
            return {}
    except Exception as e:
        print(f"[ERRO HW FATAL] {e}")
        return {}

def evaluate_hardware(found: dict, req: dict):
    results = []
    
    for key, min_val_raw in req.items():
        raw_found = found.get(key)
        if raw_found is None:
            raw_found = 0 if key in SCALABLE_KEYS else "N/A"
            
        status = "OK"
        display_found = str(raw_found)
        display_min = str(min_val_raw)
        display_rec = str(min_val_raw)
        
        if key in SCALABLE_KEYS:
            rec_val_num = round(float(min_val_raw) * 1.3, 2)
            display_rec = str(rec_val_num) 
            
            try:
                f_num = float(raw_found)
                m_num = float(min_val_raw)
                if f_num > m_num: status = "OK"
                elif f_num == m_num: status = "ATENÇÃO"
                else: status = "FALHA"
            except:
                status = "FALHA"
                
            if key in ["ram_gb", "os_disk_gb", "data_disk_gb"]:
                display_found += " GB"
                display_min += " GB"
                display_rec += " GB"
            elif key == "cpu_ghz":
                display_found += " GHz"
                display_min += " GHz"
                display_rec += " GHz"
            elif key in ["write_mb_s", "read_mb_s"]:
                display_found += " MB/s"
                display_min += " MB/s"
                display_rec += " MB/s"
        else:
            f_str = str(raw_found).upper()
            if key == "power_plan":
                status = "OK" if ("HIGH" in f_str or "ALTO" in f_str) else "FALHA"
            elif key == "os":
                status = "OK" if any(yr in f_str for yr in ["2016", "2019", "2022", "2025"]) else "FALHA"
            elif key == "nic":
                status = "OK" if ("GBPS" in f_str or "GIGABIT" in f_str) else "FALHA"

        results.append({
            "key": key,
            "label": LABELS.get(key, key),
            "found": display_found,
            "min": display_min,
            "rec": display_rec,
            "status": status
        })
        
    return results

# =============================================================================
# ROTAS FLASK
# =============================================================================

@requirements_bp.route("/", methods=["GET"])
@login_required
def requirements_home():
    releases = list(REQUIREMENTS.keys())
    return render_template("pages/requirements.html", releases=releases)

@requirements_bp.route("/scan", methods=["POST"])
@login_required
def scan_requirements():
    release = request.json.get("release", "12.1.2510")
    if release not in REQUIREMENTS:
        release = "12.1.2510"
        
    servers = Server.query.order_by(Server.id.asc()).all()
    cfg = AppConfig.query.first()
    
    # TRUQUE DE REDE: Substitui "localhost" pelo Nome Real da Máquina na rede
    # Assim, quando o Servidor Remoto for testar o ping, ele procura pelo Nome e não por ele mesmo!
    local_hostname = socket.gethostname()
    all_ips = []
    for s in servers:
        if _is_local_target(s.address):
            all_ips.append(local_hostname)
        else:
            all_ips.append(s.address)

    erp_servers = []
    db_servers = []
    
    is_single_server = (len(servers) == 1)

    for s in servers:
        # Garante que o servidor não faça ping a si mesmo na lista
        own_addr = local_hostname if _is_local_target(s.address) else s.address
        other_ips = [ip for ip in all_ips if ip != own_addr]
        
        base_role = str(s.role).upper()
        found = collect_machine_specs(s.address, base_role, other_ips)
        
        roles_to_evaluate = ["ERP", "DB"] if is_single_server else [("DB" if base_role == "DB" else "ERP")]

        for r_eval in roles_to_evaluate:
            req = REQUIREMENTS[release][r_eval]
            evaluation = evaluate_hardware(found, req)
            
            db_metrics = []
            
            if r_eval == "DB":
                sql_data = collect_sql_specs(cfg)
                raw_ver = sql_data.get("sql_version", "")
                edition = str(sql_data.get("edition", "")).replace(" Edition", "").replace(" (64-bit)", "").strip()
                
                ver_match = re.search(r'(Microsoft SQL Server \d{4})', raw_ver, re.IGNORECASE)
                
                if ver_match:
                    clean_version = f"{ver_match.group(1)} {edition}".strip()
                    found_year = int(re.search(r'\d{4}', ver_match.group(1)).group(0))
                else:
                    clean_version = raw_ver
                    found_year = 0

                if "Falha" in clean_version or "Erro" in clean_version or "TimeOut" in clean_version:
                    db_metrics = [
                        {"label": "Conexão SQL", "found": clean_version, "min": "Conectado", "status": "FALHA"}
                    ]
                else:
                    col = str(sql_data.get("collation", ""))
                    rec = str(sql_data.get("recovery", "")).upper()
                    
                    db_metrics = [
                        {
                            "label": "Versão SQL",
                            "found": clean_version,
                            "min": "Microsoft SQL Server 2019",
                            "status": "OK" if found_year >= 2019 else "FALHA"
                        },
                        {
                            "label": "Collation",
                            "found": col,
                            "min": "Latin1_General_BIN",
                            "status": "OK" if col.upper() == "LATIN1_GENERAL_BIN" else "FALHA"
                        },
                        {
                            "label": "Recovery Model",
                            "found": rec,
                            "min": "SIMPLE",
                            "status": "OK" if rec == "SIMPLE" else "FALHA"
                        }
                    ]

            server_data = {
                "name": s.name,
                "address": s.address,
                "role": r_eval,
                "evaluation": evaluation,
                "latency_external": found.get("latency_external", "Falha"),
                "latency_internal": found.get("latency_internal", {}),
                "db_metrics": db_metrics
            }
            
            if r_eval == "DB":
                db_servers.append(server_data)
            else:
                erp_servers.append(server_data)

    return jsonify({"ok": True, "erp": erp_servers, "db": db_servers})