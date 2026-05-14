import os
import re
import socket
import subprocess
import base64
import json
import html
import tempfile
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from flask import Blueprint, render_template, request, jsonify, has_request_context
from flask_login import login_required, current_user

from .extensions import db
from .models import Server, ServiceMonitor, AppConfig, AuditLog
from .mailer import send_alert_email

services_bp = Blueprint("services", __name__)

DEBUG_METRICS = True

# =============================================================================
# GRAVADOR DE AUDITORIA (O ESPIÃO)
# =============================================================================
def _record_audit(action: str, target: str, details: str, author: str = None):
    try:
        if not author:
            if has_request_context() and current_user.is_authenticated:
                author = current_user.username
            else:
                author = "Weepulse Engine"
                
        log = AuditLog(author=author, action=action, target=target, details=details)
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        print(f"[AUDIT LOG ERROR] Falha ao gravar log: {e}")

# =============================================================================
# Helpers Genéricos
# =============================================================================
def _get_ci(d: dict, key: str, default=None):
    if not isinstance(d, dict): return default
    key_lower = key.lower()
    for k, v in d.items():
        if k.lower() == key_lower:
            return v
    return default

def _is_local_target(address: str) -> bool:
    a = (address or "").strip().lower()
    if a in ("localhost", "127.0.0.1", "::1"): 
        return True
    try:
        if a == socket.gethostname().lower(): 
            return True
    except Exception: 
        pass
    return False

def clean_exe_path(path_name: str):
    if not path_name: return ""
    match = re.search(r'^"?([^"]+\.exe)"?', path_name, re.IGNORECASE)
    return match.group(1) if match else path_name.replace('"', '').split(' -')[0]

# =============================================================================
# Classificação Protheus
# =============================================================================
_PATTERNS = {
    "APPSERVER": [r"\bappserver\b", r"\bprotheus\s*appserver\b", r"\btotvs\s*appserver\b"],
    "DBACCESS": [r"\bdbaccess\b", r"\bprotheus\s*dbaccess\b", r"\btotvs\s*dbaccess\b"],
    "LICENSE": [r"\blicen[sc]e\s*server\b", r"\btotvs\s*license\b", r"\bprotheus\s*license\b", r"\blicen[sc]e\s*virtual\b"],
}

def classify_protheus_service(service_name: str, display_name: str, exe_path: str):
    exe_lower = (exe_path or "").lower()
    if "svchost.exe" in exe_lower or "system32" in exe_lower: 
        return None
        
    hay = f"{service_name} {display_name}".lower()
    for typ, pats in _PATTERNS.items():
        for p in pats:
            if re.search(p, hay, flags=re.IGNORECASE): 
                return typ
                
    if "appserver.exe" in exe_lower:
        if "license" in exe_lower or "licence" in exe_lower: 
            return "LICENSE"
        return "APPSERVER"
        
    if "dbaccess.exe" in exe_lower or "dbaccess64.exe" in exe_lower: 
        return "DBACCESS"
    if "licenseserver.exe" in exe_lower: 
        return "LICENSE"
        
    return None

def _format_uptime(seconds: Optional[int]) -> str:
    if not seconds or seconds <= 0: return "-"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if days > 0: 
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

# =============================================================================
# PowerShell / WinRM Base
# =============================================================================
def _parse_ps_json(txt: str):
    txt = (txt or "").strip()
    if not txt: return []
    start_idx = txt.find("===JSON-START===")
    end_idx = txt.find("===JSON-END===")
    if start_idx != -1 and end_idx != -1:
        txt = txt[start_idx + 16:end_idx].strip()
    if not txt: return []
    try:
        data = json.loads(txt)
        return [data] if isinstance(data, dict) else data
    except Exception:
        return []

def _run_ps_local(ps: str, timeout: int = 120) -> Tuple[int, str, str]:
    ps_full = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n" + ps
    fd, path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8-sig') as f:
            f.write(ps_full)
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=timeout
        )
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    finally:
        try: os.remove(path)
        except: pass

def _winrm_connect(address: str):
    import winrm 
    cfg = AppConfig.query.first()
    user = cfg.winrm_user if cfg and cfg.winrm_user else ""
    pwd = cfg.winrm_password if cfg and cfg.winrm_password else ""
    transport = cfg.winrm_transport if cfg and cfg.winrm_transport else "ntlm"
    use_ssl = cfg.winrm_ssl if cfg else False
    scheme = "https" if use_ssl else "http"
    port = 5986 if use_ssl else 5985
    return winrm.Session(f"{scheme}://{address}:{port}/wsman", auth=(user, pwd), transport=transport)
    
def _run_ps_winrm(address: str, ps: str) -> Tuple[int, str, str]:
    s = _winrm_connect(address)
    ps_b64 = base64.b64encode(ps.encode('utf-8')).decode('utf-8')
    
    r_temp = s.run_ps("$p = [System.IO.Path]::GetTempFileName(); Write-Output $p")
    if r_temp.status_code != 0: 
        return r_temp.status_code, "", r_temp.std_err.decode('utf-8')
        
    remote_path = r_temp.std_out.decode('utf-8').strip()
    
    chunk_size = 2000
    for i in range(0, len(ps_b64), chunk_size):
        chunk = ps_b64[i:i+chunk_size]
        append_cmd = f"[System.IO.File]::AppendAllText('{remote_path}', '{chunk}')"
        r_app = s.run_ps(append_cmd)
        if r_app.status_code != 0: 
            return r_app.status_code, "", f"Erro: {r_app.std_err.decode('utf-8')}"
            
    exec_cmd = f"""
    $ErrorActionPreference = 'Stop'
    try {{
        $b64 = [System.IO.File]::ReadAllText('{remote_path}')
        $bytes = [System.Convert]::FromBase64String($b64)
        $decoded = [System.Text.Encoding]::UTF8.GetString($bytes)
        $runPath = '{remote_path}.ps1'
        [System.IO.File]::WriteAllText($runPath, $decoded)
        & $runPath
        Remove-Item '{remote_path}' -Force -ErrorAction SilentlyContinue
        Remove-Item $runPath -Force -ErrorAction SilentlyContinue
    }} catch {{ Write-Output "Erro Remoto Interno: $($_.Exception.Message)" }}
    """
    r_exec = s.run_ps(exec_cmd)
    return int(r_exec.status_code), (r_exec.std_out or b"").decode('utf-8', errors="ignore"), (r_exec.std_err or b"").decode('utf-8', errors="ignore")

# =============================================================================
# DETALHES AVANÇADOS (TCP / RPO / BROKER)
# =============================================================================
def _fetch_service_log(server_address: str, exe_path: str) -> str:
    ps_script = rf"""
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
            if ($line -match '^\s*\[(?<sec>.*?)\]') {{ $inGeneral = ($matches.sec -match '^GENERAL$') }}
            elseif ($inGeneral -and $line -match '^\s*consolefile\s*=\s*(?<val>.*)') {{
                $candidate = $matches.val.Trim()
                if (-not [System.IO.Path]::IsPathRooted($candidate)) {{ $logPath = Join-Path $dir $candidate }} 
                else {{ $logPath = $candidate }}
                break
            }}
        }}
    }}
    if (Test-Path $logPath) {{
        $tail = Get-Content $logPath -Tail 50 -ErrorAction SilentlyContinue | Out-String
        if ($tail) {{ Write-Output $tail }} else {{ Write-Output "Log está vazio." }}
    }} else {{ 
        Write-Output "Arquivo de log não encontrado." 
    }}
    """
    try:
        if _is_local_target(server_address): 
            _, out, _ = _run_ps_local(ps_script)
        else: 
            _, out, _ = _run_ps_winrm(server_address, ps_script)
        return out.strip() or "Nenhuma informação retornada do Log."
    except Exception as e: 
        return f"Falha ao extrair log: {str(e)}"

def _get_service_details(server_address: str, svcs_info: List[dict]) -> dict:
    if not svcs_info: return {}
    input_json = json.dumps(svcs_info)
    input_b64 = base64.b64encode(input_json.encode('utf-8')).decode('utf-8')
    
    ps = f"""
    $ErrorActionPreference = 'Stop'
    try {{
        $jsonStr = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{input_b64}'))
        $svcs = $jsonStr | ConvertFrom-Json
        $results = @{{}}
        
        foreach ($s in $svcs) {{
            $svcKey = $s.service_key
            $exePath = $s.exe_path
            $pathName = $s.path_name
            
            if ($exePath -match '(?i)dbaccess') {{
                $dbMonIni = Join-Path ([System.IO.Path]::GetDirectoryName($exePath)) "dbmonitor.ini"
                $pt = ""; $pst = ""
                if (Test-Path $dbMonIni) {{
                    $dbContent = Get-Content $dbMonIni -ErrorAction SilentlyContinue
                    foreach ($line in $dbContent) {{
                        if ($line -match '(?i)^\s*port\s*=\s*(\d+)') {{ $pt = $matches[1]; break }}
                    }}
                }}
                if ($pt -ne "") {{
                    try {{
                        $tcp = New-Object System.Net.Sockets.TcpClient
                        $async = $tcp.BeginConnect("127.0.0.1", [int]$pt, $null, $null)
                        if ($async.AsyncWaitHandle.WaitOne(500, $false) -and $tcp.Connected) {{ $pst = "Respondendo" }} 
                        else {{ $pst = "Travado/Timeout" }}
                        $tcp.Close()
                    }} catch {{ $pst = "Erro/Timeout" }}
                }}
                $msg = if ($pt -ne "") {{ $dbMonIni }} else {{ "Sem porta" }}
                $results[$svcKey] = @{{ port = $pt; port_status = $pst; rpo_name = ""; rpo_date = ""; rpo_custom_name = ""; rpo_custom_date = ""; debug_ini = $msg }}
                continue
            }}
            
            $iniPath = ""
            $argsArray = $pathName -split '\s+'
            for ($i=0; $i -lt $argsArray.Length; $i++) {{
                if ($argsArray[$i] -match '(?i)^[-/]ini=(.*)') {{ 
                    $iniPath = $matches[1] -replace '^["'']|["'']$', ''; break 
                }} 
                elseif ($argsArray[$i] -match '(?i)^[-/]ini$') {{
                    if ($i + 1 -lt $argsArray.Length) {{ 
                        $iniPath = $argsArray[$i+1] -replace '^["'']|["'']$', ''; break 
                    }}
                }}
            }}
            
            if ([string]::IsNullOrWhiteSpace($iniPath)) {{ 
                $iniPath = [System.IO.Path]::ChangeExtension($exePath, ".ini") 
            }} 
            elseif (-not [System.IO.Path]::IsPathRooted($iniPath)) {{ 
                $iniPath = Join-Path ([System.IO.Path]::GetDirectoryName($exePath)) $iniPath 
            }}
            
            $debugMsg = $iniPath
            $port = ""; $port_status = ""; $rpo_name = ""; $rpo_date = ""; $rpo_custom_name = ""; $rpo_custom_date = ""
            
            if (-not [string]::IsNullOrWhiteSpace($iniPath) -and (Test-Path $iniPath)) {{
                $content = Get-Content $iniPath -ErrorAction SilentlyContinue
                $iniData = @{{}}; $orderedEnvs = @(); $currentSection = ""
                
                foreach ($line in $content) {{
                    $line = $line.Trim()
                    if ($line.StartsWith(";")) {{ continue }}
                    $commentIdx = $line.IndexOf(";")
                    if ($commentIdx -ge 0) {{ $line = $line.Substring(0, $commentIdx).Trim() }}
                    
                    if ($line -match '^\s*\[(.*)\]\s*$') {{
                        $currentSection = $matches[1].Trim().ToLower()
                        if (-not $iniData.Contains($currentSection)) {{ 
                            $iniData[$currentSection] = @{{}}; 
                            $orderedEnvs += $currentSection 
                        }}
                    }} elseif ($line -match '^([^=]+)=(.*)$' -and $currentSection -ne "") {{
                        $k = $matches[1].Trim().ToLower()
                        $v = $matches[2].Trim() -replace '^["'']|["'']$', ''
                        $iniData[$currentSection][$k] = $v
                    }}
                }}
                
                if ($iniData.Contains("tcp") -and $iniData["tcp"].Contains("port")) {{ 
                    $port = $iniData["tcp"]["port"] 
                }}
                
                if ([string]::IsNullOrWhiteSpace($port)) {{
                    foreach ($sec in $iniData.Keys) {{
                        if ($iniData[$sec].Contains("local_server_port")) {{ 
                            $port = $iniData[$sec]["local_server_port"]; break 
                        }}
                    }}
                }}
                
                $envName = ""
                if ($iniData.Contains("general") -and $iniData["general"].Contains("app_environment")) {{ 
                    $envName = $iniData["general"]["app_environment"].ToLower() 
                }} 
                else {{
                    foreach ($sec in $orderedEnvs) {{
                        if ($iniData[$sec].Contains("sourcepath") -or $iniData[$sec].Contains("rpocustom")) {{ 
                            $envName = $sec; break 
                        }}
                    }}
                }}
                if ($envName -ne "") {{ $debugMsg += " | Env: $envName" }}
                
                if ($envName -ne "" -and $iniData.Contains($envName)) {{
                    if ($iniData[$envName].Contains("rpocustom")) {{
                        $rpoCustom = $iniData[$envName]["rpocustom"]
                        if (-not [System.IO.Path]::IsPathRooted($rpoCustom)) {{ 
                            $rpoCustom = Join-Path ([System.IO.Path]::GetDirectoryName($exePath)) $rpoCustom 
                        }}
                        $debugMsg += " | RpoC: " + $rpoCustom
                        if (Test-Path $rpoCustom) {{
                            $fCustom = Get-Item $rpoCustom -ErrorAction SilentlyContinue
                            if ($fCustom) {{ 
                                $rpo_custom_name = $fCustom.Name; 
                                $rpo_custom_date = $fCustom.LastWriteTime.ToString("dd/MM/yyyy HH:mm") 
                            }}
                        }} else {{ 
                            $rpo_custom_name = "[ERRO]"; 
                            $rpo_custom_date = "-" 
                        }}
                    }}
                    
                    if ($iniData[$envName].Contains("sourcepath")) {{
                        $sourcePath = $iniData[$envName]["sourcepath"]
                        if (-not [System.IO.Path]::IsPathRooted($sourcePath)) {{ 
                            $sourcePath = Join-Path ([System.IO.Path]::GetDirectoryName($exePath)) $sourcePath 
                        }}
                        $debugMsg += " | Src: " + $sourcePath
                        
                        if (Test-Path $sourcePath) {{
                            $rpoFiles = Get-ChildItem -Path $sourcePath -Filter "*.rpo" -File -ErrorAction SilentlyContinue
                            if ($rpoFiles) {{
                                $foundRpo = $rpoFiles | Where-Object {{ $_.Name -match '(?i)^ttt[mp]120\.rpo$' }} | Select-Object -First 1
                                if (-not $foundRpo) {{ 
                                    $foundRpo = $rpoFiles | Sort-Object Length -Descending | Select-Object -First 1 
                                }}
                                if ($foundRpo) {{ 
                                    $rpo_name = $foundRpo.Name; 
                                    $rpo_date = $foundRpo.LastWriteTime.ToString("dd/MM/yyyy HH:mm") 
                                }}
                            }} else {{ 
                                $rpo_name = "[ERRO]"; $rpo_date = "-" 
                            }}
                        }} else {{ 
                            $rpo_name = "[ERRO]"; $rpo_date = "-" 
                        }}
                    }}
                }}
                
                if ($port -ne "") {{
                    try {{
                        $tcp = New-Object System.Net.Sockets.TcpClient
                        $async = $tcp.BeginConnect("127.0.0.1", [int]$port, $null, $null)
                        if ($async.AsyncWaitHandle.WaitOne(500, $false) -and $tcp.Connected) {{ 
                            $port_status = "Respondendo" 
                        }} 
                        else {{ 
                            $port_status = "Travado/Timeout" 
                        }}
                        $tcp.Close()
                    }} catch {{ 
                        $port_status = "Erro/Timeout" 
                    }}
                }}
            }} else {{ 
                $debugMsg = "NAO_ENCONTRADO: " + $iniPath 
            }}
            
            $results[$svcKey] = @{{ port = $port; port_status = $port_status; rpo_name = $rpo_name; rpo_date = $rpo_date; rpo_custom_name = $rpo_custom_name; rpo_custom_date = $rpo_custom_date; debug_ini = $debugMsg }}
        }}
        
        Write-Output "===JSON-START==="
        Write-Output ($results | ConvertTo-Json -Depth 3 -Compress)
        Write-Output "===JSON-END==="
    }} catch {{
        Write-Output "===JSON-START==="
        Write-Output "{{\`"ERROR_CRITICAL\`": \`"$($_.Exception.Message)\`"}}"
        Write-Output "===JSON-END==="
    }}
    """
    try:
        if _is_local_target(server_address): 
            code, out, err = _run_ps_local(ps)
        else: 
            code, out, err = _run_ps_winrm(server_address, ps)
            
        start_idx = out.find("===JSON-START===")
        end_idx = out.find("===JSON-END===")
        if start_idx != -1 and end_idx != -1:
            clean_json = out[start_idx + 16:end_idx].strip()
            if clean_json:
                data = json.loads(clean_json)
                if isinstance(data, dict): 
                    return data
    except Exception: 
        pass
    return {}

# =============================================================================
# Scan: Win32_Service
# =============================================================================
def _scan_win32_service_local() -> List[dict]:
    ps = r"""
    $ErrorActionPreference = 'SilentlyContinue'
    $svcs = Get-CimInstance Win32_Service | Select-Object Name, DisplayName, State, ProcessId, PathName
    Write-Output "===JSON-START==="
    Write-Output ($svcs | ConvertTo-Json -Depth 4 -Compress)
    Write-Output "===JSON-END==="
    """
    code, out, err = _run_ps_local(ps, timeout=120)
    res = []
    for it in _parse_ps_json(out):
        res.append({
            "name": _get_ci(it, "Name", ""),
            "display_name": _get_ci(it, "DisplayName") or _get_ci(it, "Name", ""),
            "state": str(_get_ci(it, "State", "")).upper(),
            "process_id": int(_get_ci(it, "ProcessId", 0) or 0),
            "path_name": _get_ci(it, "PathName", ""),
            "exe_path": clean_exe_path(_get_ci(it, "PathName", ""))
        })
    return res

def _scan_win32_service_remote(address: str) -> List[dict]:
    ps = r"""
    $ErrorActionPreference = 'SilentlyContinue'
    $svcs = Get-CimInstance Win32_Service | Select-Object Name, DisplayName, State, ProcessId, PathName
    Write-Output "===JSON-START==="
    Write-Output ($svcs | ConvertTo-Json -Depth 4 -Compress)
    Write-Output "===JSON-END==="
    """
    code, out, err = _run_ps_winrm(address, ps)
    res = []
    for it in _parse_ps_json(out):
        res.append({
            "name": _get_ci(it, "Name", ""),
            "display_name": _get_ci(it, "DisplayName") or _get_ci(it, "Name", ""),
            "state": str(_get_ci(it, "State", "")).upper(),
            "process_id": int(_get_ci(it, "ProcessId", 0) or 0),
            "path_name": _get_ci(it, "PathName", ""),
            "exe_path": clean_exe_path(_get_ci(it, "PathName", ""))
        })
    return res

# =============================================================================
# Proc metrics
# =============================================================================
def _chunked(iterable: List[int], size: int) -> List[List[int]]:
    return [iterable[i:i + size] for i in range(0, len(iterable), size)]

def _parse_metrics_text(txt: str) -> List[dict]:
    res = []
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("===METRIC===|"):
            parts = line.split("|")
            if len(parts) >= 5:
                try:
                    item = {
                        "Pid": int(parts[1]),
                        "UptimeSeconds": int(parts[2]),
                        "CpuPercent": float(parts[3]),
                        "MemMb": float(parts[4])
                    }
                    if len(parts) >= 6: 
                        item["Threads"] = int(parts[5])
                    else: 
                        item["Threads"] = 0
                    res.append(item)
                except: 
                    pass
    return res

def _build_proc_metrics_ps(pid_list_str: str) -> str:
    return """
    try {
        $ErrorActionPreference = 'SilentlyContinue'; $ProgressPreference = 'SilentlyContinue'
        $pids = @({PIDS}); $cores = $env:NUMBER_OF_PROCESSORS; if (-not $cores -or $cores -eq 0) { $cores = 1 }
        $snap = @{}
        foreach ($p in $pids) { try { $proc = Get-Process -Id $p; if ($proc.TotalProcessorTime) { $snap[$p] = $proc.TotalProcessorTime.TotalSeconds } } catch {} }
        Start-Sleep -Milliseconds 600
        foreach ($targetId in $pids) {
            $mem = 0.0; $upt = 0; $cpu = 0.0; $threads = 0
            try {
                $proc = Get-Process -Id $targetId
                if ($proc) {
                    if ($proc.WorkingSet64) { $mem = [math]::Round(($proc.WorkingSet64 / 1048576), 2) }
                    if ($proc.StartTime) { $upt = [math]::Floor(((Get-Date) - $proc.StartTime).TotalSeconds) }
                    if ($proc.Threads) { $threads = $proc.Threads.Count }
                    $old = $snap[$targetId]
                    if ($old -ne $null -and $proc.TotalProcessorTime) {
                        $diff = $proc.TotalProcessorTime.TotalSeconds - $old
                        $cpu = [math]::Round((($diff / 0.6) / $cores * 100), 2)
                        if ($cpu -lt 0) { $cpu = 0 }
                    }
                }
            } catch {}
            Write-Output "===METRIC===|$targetId|$upt|$([string]$cpu.ToString().Replace(',','.'))|$([string]$mem.ToString().Replace(',','.'))|$threads"
        }
    } catch {}
    """.replace("{PIDS}", pid_list_str).strip()

def _proc_metrics_from_pids_local(pids: List[int]) -> Dict[int, dict]:
    res = {}
    for block in _chunked(sorted({int(p) for p in pids if int(p) > 0}), 25):
        _, out, _ = _run_ps_local(_build_proc_metrics_ps(",".join(str(p) for p in block)), timeout=120)
        for it in _parse_metrics_text(out):
            res[it["Pid"]] = {
                "uptime_seconds": it["UptimeSeconds"], 
                "cpu_percent": it["CpuPercent"], 
                "mem_mb": it["MemMb"], 
                "threads": it.get("Threads", 0)
            }
    return res

def _proc_metrics_from_pids_remote(address: str, pids: List[int]) -> Dict[int, dict]:
    res = {}
    for block in _chunked(sorted({int(p) for p in pids if int(p) > 0}), 25):
        _, out, _ = _run_ps_winrm(address, _build_proc_metrics_ps(",".join(str(p) for p in block)))
        for it in _parse_metrics_text(out):
            res[it["Pid"]] = {
                "uptime_seconds": it["UptimeSeconds"], 
                "cpu_percent": it["CpuPercent"], 
                "mem_mb": it["MemMb"], 
                "threads": it.get("Threads", 0)
            }
    return res

# =============================================================================
# Ações: start/stop/restart
# =============================================================================
def _action_ps(service_key: str, action: str) -> str:
    if action not in ("start", "stop", "restart"):
        raise ValueError("ação inválida")
        
    return f"""
    $ErrorActionPreference = 'SilentlyContinue'
    $svcName = '{service_key}'
    $act = '{action}'

    function Kill-Svc {{
        $svc = Get-CimInstance Win32_Service -Filter "Name='$svcName'"
        if ($svc -and $svc.ProcessId -gt 0) {{ 
            Stop-Process -Id $svc.ProcessId -Force 
        }}
    }}

    if ($act -eq 'stop' -or $act -eq 'restart') {{
        $svcObj = Get-Service -Name $svcName
        if ($svcObj -and $svcObj.Status -ne 'Stopped') {{
            Invoke-CimMethod -Query "SELECT * FROM Win32_Service WHERE Name='$svcName'" -MethodName StopService | Out-Null
            $sw = [Diagnostics.Stopwatch]::StartNew()
            while ($sw.Elapsed.TotalSeconds -lt 20) {{
                $svcObj.Refresh()
                if ($svcObj.Status -eq 'Stopped') {{ break }}
                Start-Sleep -Seconds 1
            }}
            $svcObj.Refresh()
            if ($svcObj.Status -ne 'Stopped') {{
                Kill-Svc
                Start-Sleep -Seconds 2
            }}
        }}
    }}

    if ($act -eq 'start' -or $act -eq 'restart') {{
        $svcObj = Get-Service -Name $svcName
        if ($svcObj -and $svcObj.Status -match 'Pending') {{
            Kill-Svc
            Start-Sleep -Seconds 2
        }}
        $wmi = Get-CimInstance Win32_Service -Filter "Name='$svcName'"
        if ($wmi -and $wmi.State -eq 'Stopped' -and $wmi.ProcessId -gt 0) {{
            Kill-Svc
            Start-Sleep -Seconds 2
        }}
        $ErrorActionPreference = 'Stop'
        Start-Service -Name $svcName
    }}
    """

def _service_action_local(service_key: str, action: str):
    code, out, err = _run_ps_local(_action_ps(service_key, action), timeout=60)
    if code != 0: 
        raise RuntimeError((err or out).strip() or "Falha ao executar ação (local).")

def _service_action_remote(address: str, service_key: str, action: str):
    code, out, err = _run_ps_winrm(address, _action_ps(service_key, action))
    if code != 0: 
        raise RuntimeError((err or out).strip() or "Falha ao executar ação (remoto).")

# =============================================================================
# Scan principal com Gatilho de Auto-Healing Refinado + WEBHOOK TEAMS
# =============================================================================
def scan_protheus_services(server_address: str, server_name: str) -> List[dict]:
    server_address = (server_address or "").strip()
    server_name = (server_name or "").strip()

    is_local = _is_local_target(server_address)
    all_svcs = _scan_win32_service_local() if is_local else _scan_win32_service_remote(server_address)

    monitors = ServiceMonitor.query.filter_by(server_address=server_address).all()
    monitor_map = {m.service_key: m for m in monitors}

    filtered: List[dict] = []
    protheus_svcs_info = []
    cfg = AppConfig.query.first()
    
    for svc in all_svcs:
        name = svc.get("name") or ""
        display = svc.get("display_name") or name
        exe_path = svc.get("exe_path", "")
        path_name = svc.get("path_name", "")
        
        typ = classify_protheus_service(name, display, exe_path)
        if not typ: 
            continue

        protheus_svcs_info.append({"service_key": name, "exe_path": exe_path, "path_name": path_name})

        state = (svc.get("state") or "").upper()
        status = "RUNNING" if "RUN" in state else ("STOPPED" if "STOP" in state else state)
        
        m_obj = monitor_map.get(name)
        is_monitored = m_obj.is_active if m_obj else False
        is_ignored = getattr(m_obj, 'is_ignored', False) if m_obj else False

        if status == "STOPPED" and is_monitored:
            now = datetime.now()
            can_alert = not m_obj.last_alert or (now - m_obj.last_alert) > timedelta(minutes=1)
            
            log_tail = _fetch_service_log(server_address, exe_path) if can_alert else ""

            start_success = False
            start_err = ""
            try:
                if is_local: 
                    _service_action_local(name, "start")
                else: 
                    _service_action_remote(server_address, name, "start")
                status = "RESTARTING (Auto-Healing)"
                start_success = True
            except Exception as healing_err:
                status = "STOPPED (Falha no Auto-Healing)"
                start_err = str(healing_err)

            if can_alert:
                status_color = "#10b981" if start_success else "#ef4444"
                status_text = "SUCESSO: Serviço religado com sucesso pelo Weepulse!" if start_success else f"FALHA: O Windows retornou erro: {start_err}"
                
                _record_audit("AUTO_HEALING", f"{display} ({server_address})", f"Status: {'Sucesso' if start_success else 'Falha'} - {start_err if not start_success else 'Serviço recuperado'}", author="Weepulse Engine")

                if cfg and getattr(cfg, 'webhook_url', None):
                    try:
                        from .webhook import send_webhook_alert
                        send_webhook_alert(
                            cfg.webhook_url, 
                            f"⚠️ Queda de Serviço: {display}", 
                            f"**Servidor:** {server_name}\n\n**Ação Automática:** {status_text}", 
                            "success" if start_success else "danger"
                        )
                    except Exception: 
                        pass

                email_html = f"""
                <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 20px; font-family: Arial, sans-serif; text-align: left;">
                    <div style="color: #ef4444; font-weight: bold; font-size: 16px; margin-bottom: 15px; border-bottom: 1px solid #e2e8f0; padding-bottom: 10px;">
                        ⚠️ ALERTA CRÍTICO: Queda de Serviço Detectada
                    </div>
                    <div style="margin-bottom: 5px; color: #333;"><b>Servidor:</b> {server_name} ({server_address})</div>
                    <div style="margin-bottom: 5px; color: #333;"><b>Serviço Afetado:</b> {display}</div>
                    <div style="margin-bottom: 20px; color: {status_color}; font-weight: bold;"><b>Ação Automática:</b> {status_text}</div>
                    
                    <div style="color: #1e293b; font-weight: bold; margin-bottom: 5px;">Últimas linhas capturadas do Log antes da queda:</div>
                    <div style="background-color: #0c0c0c; color: #22c55e; padding: 15px; border-radius: 6px; font-family: 'Courier New', monospace; font-size: 12px; white-space: pre-wrap; overflow-x: auto; max-height: 400px;">{html.escape(log_tail)}</div>
                </div>
                """
                try:
                    send_alert_email(f"[Weepulse] Queda de Serviço: {display}", email_html)
                    m_obj.last_alert = now
                    db.session.commit()
                except Exception: 
                    pass

        filtered.append({
            "server_address": server_address, 
            "server_name": server_name,
            "service_key": name, 
            "service_name": display, 
            "type": typ,
            "exe_path": exe_path, 
            "path_name": path_name,
            "status": status, 
            "process_id": int(svc.get("process_id") or 0),
            "is_monitored": is_monitored, 
            "is_ignored": is_ignored
        })

    running_pids = [x["process_id"] for x in filtered if "RUNNING" in x["status"] and x["process_id"] > 0]
    proc_map = {}
    if running_pids:
        proc_map = _proc_metrics_from_pids_local(running_pids) if is_local else _proc_metrics_from_pids_remote(server_address, running_pids)

    extra_details = _get_service_details(server_address, protheus_svcs_info) if protheus_svcs_info else {}

    results: List[dict] = []
    for x in filtered:
        pid = x["process_id"]
        m = proc_map.get(pid) if ("RUNNING" in x["status"] and pid > 0) else None
        det = extra_details.get(x["service_key"], {})
        
        results.append({
            "server_address": x["server_address"], 
            "server_name": x["server_name"],
            "service_key": x["service_key"], 
            "service_name": x["service_name"],
            "type": x["type"], 
            "status": x["status"],
            "uptime": _format_uptime(int(m.get("uptime_seconds", 0))) if m else "-",
            "cpu_percent": float(m.get("cpu_percent", 0.0)) if m else 0.0,
            "mem_mb": float(m.get("mem_mb", 0.0)) if m else 0.0,
            "threads": int(m.get("threads", 0)) if m else 0,
            "is_monitored": x["is_monitored"], 
            "is_ignored": x["is_ignored"],
            "port": det.get("port", ""), 
            "port_status": det.get("port_status", ""),
            "rpo_name": det.get("rpo_name", ""), 
            "rpo_date": det.get("rpo_date", ""),
            "rpo_custom_name": det.get("rpo_custom_name", ""), 
            "rpo_custom_date": det.get("rpo_custom_date", ""),
            "debug_ini": det.get("debug_ini", "")
        })

    order = {"APPSERVER": 1, "DBACCESS": 2, "LICENSE": 3}
    results.sort(key=lambda i: (order.get(i["type"], 9), i["service_name"].lower()))
    return results

# =============================================================================
# MOTOR GLOBAL INTELIGENTE (INICIAR, PARAR, REINICIAR)
# =============================================================================
def run_global_sequence(command="restart", is_scheduled=False):
    import time
    logs = []
    
    action_title = {"start": "LIGAR", "stop": "DESLIGAR", "restart": "REINICIAR"}[command]
    logs.append(f"<b>[{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] A INICIAR PROCEDIMENTO GLOBAL: {action_title} TODO O SISTEMA</b>")
    
    author_name = "Weepulse Engine (Agendado)" if is_scheduled else None
    action_name = f"SCHEDULED_{command.upper()}" if is_scheduled else f"MANUAL_{command.upper()}_ALL"
    _record_audit(action_name, "Toda a Infraestrutura", f"Início do procedimento sequencial de {action_title.lower()}.", author=author_name)
    
    try:
        active_monitors = ServiceMonitor.query.filter_by(is_active=True).all()
        saved_monitors = [(m.server_address, m.service_key) for m in active_monitors]
        
        ignored_keys = set()
        try:
            ignored_monitors = [m for m in ServiceMonitor.query.all() if getattr(m, 'is_ignored', False)]
            ignored_keys = {f"{m.server_address}|{m.service_key}" for m in ignored_monitors}
        except Exception: 
            pass

        for m in active_monitors: 
            m.is_active = False
        db.session.commit()
        logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Auto-Healing suspenso preventivamente para {len(saved_monitors)} serviços.")

        logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] A mapear serviços e verificar regras de proteção e estado atual...")
        servers = Server.query.all()
        protheus_svcs = {"APPSERVER": [], "DBACCESS": [], "LICENSE": []}
        
        for s in servers:
            is_local = _is_local_target(s.address)
            try:
                svcs = _scan_win32_service_local() if is_local else _scan_win32_service_remote(s.address)
                for svc in svcs:
                    typ = classify_protheus_service(svc.get("name"), svc.get("display_name"), svc.get("exe_path"))
                    if typ in protheus_svcs:
                        svc_key = svc.get("name")
                        disp_name = svc.get("display_name")
                        
                        if f"{s.address}|{svc_key}" in ignored_keys:
                            logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] 🔒 <b>[TRAVADO]</b> Serviço {disp_name} protegido por regra e será ignorado.")
                            continue
                            
                        state = svc.get("state", "").upper()
                        is_running = "RUN" in state

                        if command == "start" and is_running:
                            logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] ⏩ [PULOU] O serviço <b>{disp_name}</b> já está ligado.")
                            continue
                        elif command == "stop" and not is_running:
                            logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] ⏩ [PULOU] O serviço <b>{disp_name}</b> já está desligado.")
                            continue

                        protheus_svcs[typ].append({
                            "server": s.address, 
                            "key": svc_key, 
                            "display": disp_name, 
                            "is_local": is_local
                        })
            except Exception as e:
                logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] [ERRO] Falha ao ler servidor {s.address}: {e}")

        def exec_action(target_list, action_msg, action_cmd):
            for t in target_list:
                try:
                    if t["is_local"]: 
                        _service_action_local(t["key"], action_cmd)
                    else: 
                        _service_action_remote(t["server"], t["key"], action_cmd)
                    logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] [OK] {action_msg} {t['display']} ({t['server']})")
                except Exception as e:
                    logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] [ERRO] Falha ao {action_msg.lower()} {t['display']} ({t['server']}): {str(e)}")

        if command in ("stop", "restart"):
            logs.append(f"<br><b>[{datetime.now().strftime('%H:%M:%S')}] --- FASE DE DESLIGAMENTO ---</b>")
            logs.append("A parar serviços APPSERVER...")
            exec_action(protheus_svcs["APPSERVER"], "A parar", "stop")
            
            logs.append("A parar serviços DBACCESS...")
            exec_action(protheus_svcs["DBACCESS"], "A parar", "stop")
            
            logs.append("A parar serviços LICENSE SERVER...")
            exec_action(protheus_svcs["LICENSE"], "A parar", "stop")

        if command in ("start", "restart"):
            logs.append(f"<br><b>[{datetime.now().strftime('%H:%M:%S')}] --- FASE DE RELIGAMENTO ---</b>")
            logs.append("A iniciar serviços LICENSE SERVER...")
            exec_action(protheus_svcs["LICENSE"], "A iniciar", "start")
            
            if len(protheus_svcs["LICENSE"]) > 0:
                logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] A aguardar 20 segundos para estabilização do License Server...")
                time.sleep(20)

            logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] A iniciar serviços DBACCESS...")
            exec_action(protheus_svcs["DBACCESS"], "A iniciar", "start")
            
            logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] A iniciar serviços APPSERVER...")
            exec_action(protheus_svcs["APPSERVER"], "A iniciar", "start")

        logs.append(f"<br><b>[{datetime.now().strftime('%H:%M:%S')}] --- FASE DE RESTAURAÇÃO DAS REGRAS ---</b>")
        restored_count = 0
        for addr, key in saved_monitors:
            m = ServiceMonitor.query.filter_by(server_address=addr, service_key=key).first()
            if m:
                m.is_active = True
                restored_count += 1
        db.session.commit()
        logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Auto-Healing restaurado para {restored_count} serviços originais.")
        logs.append(f"<br><b>[{datetime.now().strftime('%H:%M:%S')}] PROCEDIMENTO CONCLUÍDO COM SUCESSO</b>")

    except Exception as general_err:
        logs.append(f"<br><b>[ERRO CRÍTICO] O procedimento foi interrompido: {general_err}</b>")
    
    if is_scheduled:
        email_intro = f"A operação agendada ({action_title}) foi feita com sucesso."
        subject = f"[Weepulse] Operação Programada do Protheus ({action_title})"
        team_title = f"🔄 {action_title.capitalize()} Agendado Concluído"
        team_msg = f"A operação de {action_title.lower()} automática da madrugada foi concluída com sucesso. Regras de proteção (travados e pulados) foram respeitadas."
    else:
        email_intro = f"O sistema executou o procedimento global ({action_title}) a pedido do utilizador."
        subject = f"[Weepulse] Relatório de Operação Global ({action_title})"
        team_title = f"🔄 Operação Manual Concluída ({action_title})"
        team_msg = f"A operação de {action_title.lower()} global do Protheus foi executada manualmente via Weepulse."

    try:
        cfg = AppConfig.query.first()
        if cfg and getattr(cfg, 'webhook_url', None):
            from .webhook import send_webhook_alert
            send_webhook_alert(cfg.webhook_url, team_title, team_msg, "success")
    except Exception as e: 
        print(f"[ERRO TEAMS RESTART-ALL] {e}")

    email_html = f"""
    <div style="font-family: Arial, sans-serif; background-color: #f8fafc; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px;">
        <h2 style="color: #1e293b; border-bottom: 2px solid #cbd5e1; padding-bottom: 10px;">Log do Procedimento de Sistema</h2>
        <div style="color: #334155; margin-bottom: 15px;">{email_intro} Segue o registo detalhado:</div>
        <div style="background-color: #0f172a; color: #10b981; padding: 15px; border-radius: 6px; font-family: 'Courier New', monospace; font-size: 13px; line-height: 1.5; white-space: nowrap; overflow-x: auto;">
            {'<br>'.join(logs)}
        </div>
    </div>
    """
    try: 
        send_alert_email(subject, email_html)
    except Exception as mail_err: 
        print(f"Erro email log: {mail_err}")
        
    return True

# Wrapper para manter compatibilidade com o background_worker.py
def run_restart_all_sequence(is_scheduled=False):
    return run_global_sequence("restart", is_scheduled)

# =============================================================================
# Rotas Flask (Botões da Interface Web)
# =============================================================================

@services_bp.route("/audit-logs", methods=["GET"])
@login_required
def get_audit_logs():
    limit = request.args.get("limit", 100, type=int)
    author = request.args.get("author", "").strip()
    action = request.args.get("action", "").strip()
    target = request.args.get("target", "").strip()
    details = request.args.get("details", "").strip()

    query = AuditLog.query
    
    if author: 
        query = query.filter(AuditLog.author.ilike(f"%{author}%"))
    if action: 
        query = query.filter(AuditLog.action.ilike(f"%{action}%"))
    if target: 
        query = query.filter(AuditLog.target.ilike(f"%{target}%"))
    if details: 
        query = query.filter(AuditLog.details.ilike(f"%{details}%"))

    logs = query.order_by(AuditLog.timestamp.desc()).limit(limit).all()
    res = []
    
    for l in logs:
        res.append({
            "timestamp": l.timestamp.strftime("%d/%m/%Y %H:%M:%S"), 
            "author": l.author, 
            "action": l.action, 
            "target": l.target, 
            "details": l.details or ""
        })
        
    return jsonify({"ok": True, "logs": res})

@services_bp.route("/", methods=["GET"])
@login_required
def services_home():
    cfg = AppConfig.query.first()
    servers = Server.query.order_by(Server.id.asc()).all()
    results: List[dict] = []
    for s in servers:
        try: 
            results.extend(scan_protheus_services(s.address, s.name))
        except Exception as e: 
            results.append({
                "server_address": s.address, 
                "server_name": s.name, 
                "service_key": "", 
                "service_name": f"ERRO AO ESCANEAR: {e}", 
                "type": "ERROR", 
                "status": "ERROR", 
                "uptime": "-", 
                "cpu_percent": 0.0, 
                "mem_mb": 0.0, 
                "is_monitored": False
            })

    refresh = request.args.get("refresh", "10")
    return render_template("pages/services.html", servers=servers, services=results, refresh=refresh, cfg=cfg)

@services_bp.route("/scan", methods=["GET"])
@login_required
def scan_json():
    servers = Server.query.order_by(Server.id.asc()).all()
    results: List[dict] = []
    for s in servers:
        try: 
            results.extend(scan_protheus_services(s.address, s.name))
        except Exception as e: 
            results.append({
                "server_address": s.address, 
                "server_name": s.name, 
                "service_key": "", 
                "service_name": f"ERRO AO ESCANEAR: {e}", 
                "type": "ERROR", 
                "status": "ERROR", 
                "uptime": "-", 
                "cpu_percent": 0.0, 
                "mem_mb": 0.0, 
                "is_monitored": False
            })
            
    return jsonify({"ok": True, "services": results})

@services_bp.route("/toggle-monitor", methods=["POST"])
@login_required
def toggle_monitor():
    data = request.json
    server_address = data.get("server_address")
    service_key = data.get("service_key")
    
    m_obj = ServiceMonitor.query.filter_by(server_address=server_address, service_key=service_key).first()
    if m_obj: 
        m_obj.is_active = not m_obj.is_active
    else:
        m_obj = ServiceMonitor(server_address=server_address, service_key=service_key, is_active=True)
        db.session.add(m_obj)
    
    _record_audit("CONFIG_CHANGE", f"Serviço: {service_key} ({server_address})", f"Auto-Healing alterado para: {'ON' if m_obj.is_active else 'OFF'}")
    db.session.commit()
    
    return jsonify({"ok": True, "is_monitored": m_obj.is_active})

@services_bp.route("/toggle-ignore", methods=["POST"])
@login_required
def toggle_ignore():
    data = request.json
    server_address = data.get("server_address")
    service_key = data.get("service_key")
    
    m_obj = ServiceMonitor.query.filter_by(server_address=server_address, service_key=service_key).first()
    if m_obj:
        current_state = getattr(m_obj, 'is_ignored', False)
        m_obj.is_ignored = not current_state
    else:
        m_obj = ServiceMonitor(server_address=server_address, service_key=service_key, is_active=False)
        m_obj.is_ignored = True
        db.session.add(m_obj)
    
    estado_novo = 'TRAVADO (Imune a ações globais)' if m_obj.is_ignored else 'DESTRAVADO (Sujeito a comandos globais)'
    _record_audit("CONFIG_CHANGE", f"Serviço: {service_key} ({server_address})", f"Regra de Proteção alterada para: {estado_novo}")
        
    db.session.commit()
    
    return jsonify({"ok": True, "is_ignored": m_obj.is_ignored})

# -------------------------------------------------------------
# NOVAS ROTAS PARA AÇÕES EM MASSA (TOGGLE ALL)
# -------------------------------------------------------------
@services_bp.route("/toggle-all-monitor", methods=["POST"])
@login_required
def toggle_all_monitor():
    data = request.json or {}
    target_state = data.get("state", True)
    services = data.get("services", [])
    
    for svc in services:
        addr = svc.get("server_address")
        key = svc.get("service_key")
        if not addr or not key: continue
        
        m = ServiceMonitor.query.filter_by(server_address=addr, service_key=key).first()
        if m:
            m.is_active = target_state
        else:
            m = ServiceMonitor(server_address=addr, service_key=key, is_active=target_state)
            db.session.add(m)
            
    _record_audit("CONFIG_CHANGE", "Todos os Serviços", f"Auto-Healing em massa alterado para: {'ON' if target_state else 'OFF'}")
    db.session.commit()
    return jsonify({"ok": True})

@services_bp.route("/toggle-all-ignore", methods=["POST"])
@login_required
def toggle_all_ignore():
    data = request.json or {}
    target_state = data.get("state", True)
    services = data.get("services", [])
    
    for svc in services:
        addr = svc.get("server_address")
        key = svc.get("service_key")
        if not addr or not key: continue
        
        m = ServiceMonitor.query.filter_by(server_address=addr, service_key=key).first()
        if m:
            m.is_ignored = target_state
        else:
            m = ServiceMonitor(server_address=addr, service_key=key, is_active=False, is_ignored=target_state)
            db.session.add(m)
            
    estado_str = "TRAVADOS" if target_state else "DESTRAVADOS"
    _record_audit("CONFIG_CHANGE", "Todos os Serviços", f"Regra de Proteção em massa alterada para: {estado_str}")
    db.session.commit()
    return jsonify({"ok": True})

@services_bp.route("/add-server", methods=["POST"])
@login_required
def add_server():
    name = request.form.get("name", "").strip()
    address = request.form.get("address", "").strip()
    role = request.form.get("role", "ERP").strip()
    
    if not name or not address: 
        return jsonify({"ok": False, "message": "Informe nome e endereço"}), 400
        
    new_server = Server(name=name, address=address, role=role)
    db.session.add(new_server)
    
    _record_audit("SERVER_ADD", f"Servidor: {name}", f"IP/Hostname: {address} | Tipo: {role}")
    db.session.commit()
    
    # AGORA ELE DEVOLVE OS DADOS DO NOVO SERVIDOR PARA O FRONTEND
    return jsonify({
        "ok": True, 
        "server": {
            "id": new_server.id,
            "name": new_server.name,
            "address": new_server.address,
            "role": new_server.role
        }
    })

@services_bp.route("/delete-server/<int:server_id>", methods=["POST"])
@login_required
def delete_server(server_id):
    srv = Server.query.get_or_404(server_id)
    _record_audit("SERVER_DELETE", f"Servidor: {srv.name}", f"IP/Hostname: {srv.address} foi removido do Weepulse.")
    
    db.session.delete(srv)
    db.session.commit()
    
    return jsonify({"ok": True})

@services_bp.route("/action", methods=["POST"])
@login_required
def do_action():
    data = request.get_json(force=True, silent=True) or {}
    server = (data.get("server_address") or data.get("server") or "").strip()
    action = data.get("action")
    service_key = (data.get("service_key") or "").strip()

    if action not in ("start", "stop", "restart"): 
        return jsonify({"ok": False, "message": "Ação inválida"}), 400
    if not service_key: 
        return jsonify({"ok": False, "message": "service_key não informado."}), 400

    try:
        if _is_local_target(server): 
            _service_action_local(service_key, action)
        else: 
            _service_action_remote(server, service_key, action)
        
        _record_audit(f"MANUAL_{action.upper()}", f"Serviço: {service_key} ({server})", "Comando forçado via Web executado com sucesso.")
        return jsonify({"ok": True, "message": ""})
    except Exception as e:
        _record_audit(f"MANUAL_{action.upper()}_FAIL", f"Serviço: {service_key} ({server})", f"Falha na execução: {str(e)}")
        return jsonify({"ok": False, "message": str(e)}), 500

@services_bp.route("/global-action", methods=["POST"])
@login_required
def global_action_route():
    data = request.get_json(silent=True) or {}
    command = data.get("command", "restart")
    is_scheduled = data.get("is_scheduled", False)
    run_global_sequence(command, is_scheduled)
    return jsonify({"ok": True})

@services_bp.route("/restart-all", methods=["POST"])
@login_required
def legacy_restart_all():
    data = request.get_json(silent=True) or {}
    run_global_sequence("restart", data.get("is_scheduled", False))
    return jsonify({"ok": True})
