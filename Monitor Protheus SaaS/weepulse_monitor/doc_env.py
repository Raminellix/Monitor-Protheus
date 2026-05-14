import os
import socket
import subprocess
import base64
import json
import uuid
from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required
from .models import Server, AppConfig
from .extensions import db

doc_env_bp = Blueprint("doc_env", __name__)

def _is_local_target(address: str) -> bool:
    a = (address or "").strip().lower()
    if a in ("localhost", "127.0.0.1", "::1"): return True
    try:
        if a == socket.gethostname().lower(): return True
    except Exception: pass
    return False

def _run_ps_local(ps: str, timeout: int = 300):
    ps = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + ps
    encoded_ps = base64.b64encode(ps.encode('utf-16le')).decode('utf-8')
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_ps],
        capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=timeout
    )
    return r.stdout or "", r.stderr or ""

# ======================================================================================
# MOTOR WINRM FRAGMENTADO (Bypassa o limite de 8191 caracteres do Windows)
# ======================================================================================
def _run_ps_winrm(address: str, ps: str):
    import winrm
    
    cfg = AppConfig.query.first()
    user = cfg.winrm_user if cfg and cfg.winrm_user else ""
    pwd = cfg.winrm_password if cfg and cfg.winrm_password else ""
    transport = cfg.winrm_transport if cfg and cfg.winrm_transport else "ntlm"
    use_ssl = cfg.winrm_ssl if cfg else False
    
    scheme = "https" if use_ssl else "http"
    port = 5986 if use_ssl else 5985
    
    session = winrm.Session(f"{scheme}://{address}:{port}/wsman", auth=(user, pwd), transport=transport)
    
    ps_final = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;\n" + ps
    b64_script = base64.b64encode(ps_final.encode('utf-8')).decode('utf-8')
    
    # Se o script for pequeno, roda normalmente
    if len(b64_script) < 2500:
        encoded_ps = base64.b64encode(ps_final.encode('utf-16le')).decode('utf-8')
        r = session.run_cmd('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded_ps])
        return (r.std_out or b"").decode('utf-8', errors="ignore"), (r.std_err or b"").decode('utf-8', errors="ignore")

    # Se for gigante, pica em pedaços e remonta no servidor destino
    file_id = str(uuid.uuid4()).replace("-", "")
    tmp_path = f"$env:TEMP\\weepulse_{file_id}"
    
    init_cmd = f'[System.IO.File]::WriteAllText("{tmp_path}.b64", "")'
    session.run_cmd('powershell', ['-EncodedCommand', base64.b64encode(init_cmd.encode('utf-16le')).decode('utf-8')])
    
    chunk_size = 2000
    for i in range(0, len(b64_script), chunk_size):
        chunk = b64_script[i:i+chunk_size]
        append_cmd = f'[System.IO.File]::AppendAllText("{tmp_path}.b64", "{chunk}")'
        session.run_cmd('powershell', ['-EncodedCommand', base64.b64encode(append_cmd.encode('utf-16le')).decode('utf-8')])
        
    runner_ps = f"""
    $ErrorActionPreference = 'SilentlyContinue'
    $b64 = [System.IO.File]::ReadAllText("{tmp_path}.b64")
    $bytes = [System.Convert]::FromBase64String($b64)
    $scriptContent = [System.Text.Encoding]::UTF8.GetString($bytes)
    $scriptContent | Out-File -FilePath "{tmp_path}.ps1" -Encoding UTF8
    & "{tmp_path}.ps1"
    Remove-Item "{tmp_path}.b64" -Force
    Remove-Item "{tmp_path}.ps1" -Force
    """
    enc_runner = base64.b64encode(runner_ps.encode('utf-16le')).decode('utf-8')
    r = session.run_cmd('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', enc_runner])
    
    return (r.std_out or b"").decode('utf-8', errors="ignore"), (r.std_err or b"").decode('utf-8', errors="ignore")

# ======================================================================================

@doc_env_bp.route("/", methods=["GET"])
@login_required
def doc_home():
    servers = Server.query.order_by(Server.id.asc()).all()
    return render_template("pages/doc_env.html", servers=servers)

@doc_env_bp.route("/generate", methods=["POST"])
@login_required
def generate_doc():
    data = request.json
    server_address = data.get("server")
    update_desc = "$true" if data.get("updateDesc") else "$false"

    if not server_address:
        return jsonify({"ok": False, "message": "Selecione o servidor de destino."})

    # Usando string crua (r"") para evitar bugs de conversão de caracteres do Python
    ps_script = r"""
    $ErrorActionPreference = 'SilentlyContinue'
    $ATUALIZAR_DESCRICOES = __UPDATE_DESC__
    $EncodingANSI = [System.Text.Encoding]::GetEncoding("iso-8859-1")

    function Get-LocalIP {
        try {
            $ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notlike "*Loopback*" -and $_.IPAddress -notlike "169.*" } | Select-Object -First 1).IPAddress
            if ($ip) { return $ip } else { return "127.0.0.1" }
        } catch { return "127.0.0.1" }
    }

    function Get-IniContent ($FilePath) {
        $ini = @{}
        if (Test-Path $FilePath) {
            $lines = [System.IO.File]::ReadAllLines($FilePath, $EncodingANSI)
            $section = ""
            foreach ($line in $lines) {
                $line = $line.Trim()
                if ($line -match "^\[(.+)\]$") { 
                    $section = $matches[1].Trim().ToLower()
                    $ini[$section] = @{ "_OriginalName" = $matches[1].Trim() } 
                }
                elseif ($line -match "^([^=]+)=(.*)$" -and $section -ne "") {
                    $key = $matches[1].Trim().ToLower()
                    $val = $matches[2].Trim()
                    if (-not $ini[$section].ContainsKey($key)) { $ini[$section][$key] = $val }
                }
            }
        }
        return $ini
    }

    $appServers = @()
    $svcs = Get-CimInstance Win32_Service | Select-Object Name, DisplayName, PathName, Description
    
    foreach ($svc in $svcs) {
        $path = $svc.PathName
        if ([string]::IsNullOrWhiteSpace($path)) { continue }
        
        $exePath = $path -replace '^"?([^"]+\.exe)"?.*', '$1'
        if (-not $exePath.ToLower().EndsWith(".exe")) {
            $exePath = ($path -split " -")[0].Replace('"', '').Trim()
        }
        
        $hay = "$($svc.Name) $($svc.DisplayName)".ToLower()
        $exeLower = $exePath.ToLower()
        
        $isProtheus = $false
        if ($hay -match 'appserver|dbaccess|license|protheus|totvs') { $isProtheus = $true }
        elseif ($exeLower -match 'appserver\.exe|dbaccess\.exe|dbaccess64\.exe|licenseserver\.exe') { $isProtheus = $true }
        
        if ($isProtheus -and (Test-Path $exePath)) {
            $file = Get-Item $exePath
            $iniPath = [System.IO.Path]::ChangeExtension($exePath, ".ini")
            
            # Inteligência DBAccess (Procura o INI padrão mesmo se o exe tiver 64 no nome)
            if ($exeLower -match 'dbaccess') {
                $altIni1 = Join-Path $file.DirectoryName "dbaccess.ini"
                $altIni2 = Join-Path $file.DirectoryName "dbaccess64.ini"
                if (Test-Path $altIni1) { $iniPath = $altIni1 }
                elseif (Test-Path $altIni2) { $iniPath = $altIni2 }
            }
            
            if ($path -match '(?i)-i\s*=?\s*"?([^"]+\.ini)"?') {
                $paramIni = $matches[1]
                if ([System.IO.Path]::IsPathRooted($paramIni)) { $iniPath = $paramIni } 
                else { $iniPath = Join-Path $file.DirectoryName $paramIni }
            }
            
            if (Test-Path $iniPath) {
                $alreadyAdded = $false
                foreach ($existing in $appServers) {
                    if ($existing.ExePath -eq $file.FullName) { $alreadyAdded = $true; break }
                }
                if (-not $alreadyAdded) { 
                    $appServers += [PSCustomObject]@{
                        ExePath = $file.FullName
                        IniPath = $iniPath
                        DirName = $file.Directory.Name
                        FileName = $file.Name
                        SvcName = $svc.Name
                        SvcDisp = $svc.DisplayName
                        SvcDesc = $svc.Description
                    }
                }
            }
        }
    }

    $knownSections = @("general", "service", "tcp", "webapp", "ssl", "httprest", "http", "telnet", "webagent", "licenseclient", "onstart", "httpuri", "balance_http")
    $results = @()

    foreach ($app in $appServers) {
        $ini = Get-IniContent $app.IniPath
        $isDbAccess = ($app.FileName.ToLower() -match 'dbaccess')

        $obj = [PSCustomObject]@{
            Type = if ($isDbAccess) { "DBACCESS" } else { "APPSERVER" }
            FolderName = $app.DirName
            ExePath = $app.ExePath
            Broker = $null
            ServiceInfo = $null
            ConnectionPorts = @()
            WebAgent = $null
            License = $null
            HasJobs = $false
            WebServices = @()
            RestEndpoint = $null
            GeneralConf = @{}
            PrimaryEnv = $null
            AddEnvs = @()
            DbAccessEnvs = @()
            DbAccessDrivers = @()
        }

        if ($ini.ContainsKey("balance_http")) {
            $obj.Broker = $ini["balance_http"]
            $results += $obj
            continue
        }

        $descParts = @()
        $map = [ordered]@{ "tcp"="port"; "webapp"="port"; "httprest"="port"; "http"="port"; "ssl"="port"; "telnet"="port"; "webagent"="port" }
        
        if ($ini.ContainsKey("service")) {
            $svcName = $ini["service"]["name"]
            $svcDisp = if ($ini["service"].ContainsKey("displayname")) { $ini["service"]["displayname"] } else { "(N/A)" }
            foreach ($s in $map.Keys) {
                if ($ini.ContainsKey($s) -and $ini[$s].ContainsKey($map[$s])) { $descParts += "$($s.ToUpper()) $($ini[$s][$map[$s]])" }
            }
            $finalDesc = if ($descParts.Count -gt 0) { $descParts -join " | " } else { $app.SvcDesc }
            
            $obj.ServiceInfo = @{ Name = $svcName; DisplayName = $svcDisp; Description = $finalDesc }
            
            if ($ATUALIZAR_DESCRICOES -and $svcName) { & sc.exe description $svcName $finalDesc | Out-Null }
        } else {
            $obj.ServiceInfo = @{ Name = $app.SvcName; DisplayName = $app.SvcDisp; Description = $app.SvcDesc }
        }

        if ($obj.Type -eq "DBACCESS") {
            if ($ini.ContainsKey("general")) {
                $gen = $ini["general"]
                $lsrv = if ($gen.ContainsKey("licenseserver")) { $gen["licenseserver"] } else { "" }
                if ($lsrv -match "localhost|127.0.0.1") { $lsrv = (Get-LocalIP) }
                $lport = if ($gen.ContainsKey("licenseport")) { $gen["licenseport"] } else { "" }
                
                if ($lsrv -or $lport) { $obj.License = @{ Server = $lsrv; Port = $lport } }
            }

            foreach ($sec in $ini.Keys) {
                if ($sec -eq "general" -or $sec -eq "service") { continue }
                
                if ($sec -match "^(.+)/(.*)$") {
                    $x = $matches[1]
                    $s = $ini[$sec]
                    $obj.DbAccessEnvs += @{
                        Name = $s["_OriginalName"]
                        User = $s["user"]
                        Password = $s["password"]
                    }

                    if ($ini.ContainsKey($x.ToLower())) {
                        $xSec = $ini[$x.ToLower()]
                        $xData = @{ Name = $xSec["_OriginalName"] }
                        foreach ($k in $xSec.Keys) { 
                            if ($k -ne "_OriginalName") { $xData[$k] = $xSec[$k] } 
                        }
                        
                        $exists = $false
                        foreach ($d in $obj.DbAccessDrivers) { if ($d.Name -eq $xData.Name) { $exists = $true; break } }
                        if (-not $exists) { $obj.DbAccessDrivers += $xData }
                    }
                }
            }
        } 
        else {
            foreach ($sec in @("tcp", "webapp", "ssl", "httprest", "http", "telnet")) {
                if ($ini.ContainsKey($sec) -and $ini[$sec].ContainsKey("port")) {
                    $obj.ConnectionPorts += @{ Type = $ini[$sec]["_OriginalName"]; Port = $ini[$sec]["port"] }
                }
            }

            if ($ini.ContainsKey("webagent")) {
                $obj.WebAgent = @{ Version = $ini["webagent"]["version"]; Port = $ini["webagent"]["port"] }
            }

            if ($ini.ContainsKey("licenseclient")) {
                $lsrv = $ini["licenseclient"]["server"]
                if ($lsrv -match "localhost|127.0.0.1") { $lsrv = (Get-LocalIP) }
                $obj.License = @{ Server = $lsrv; Port = $ini["licenseclient"]["port"] }
            }

            if ($ini.ContainsKey("onstart")) { $obj.HasJobs = $true }

            if ($ini.ContainsKey("http") -and $ini["http"].ContainsKey("port")) {
                $hPort = $ini["http"]["port"]
                foreach ($sec in $ini.Keys) {
                    if ($sec -like "*$hPort*" -and $sec -ne "http" -and $sec -ne "httpuri") {
                        $obj.WebServices += $ini[$sec]["_OriginalName"]
                    }
                }
            }

            if ($ini.ContainsKey("httpuri") -and $ini.ContainsKey("httprest")) {
                $rip = Get-LocalIP
                $rport = $ini["httprest"]["port"]
                $rurl = $ini["httpuri"]["url"]
                $obj.RestEndpoint = "http://${rip}:${rport}${rurl}"
            }

            $primaryEnvLower = ""
            if ($ini.ContainsKey("general")) {
                $gen = $ini["general"]
                if ($gen.ContainsKey("app_environment")) { $primaryEnvLower = $gen["app_environment"].ToLower() }
                
                foreach ($k in @("app_environment", "buildkillusers", "consolefile", "canacceptdebugger")) {
                    if ($gen.ContainsKey($k)) { $obj.GeneralConf[$k] = $gen[$k] }
                }

                if ($primaryEnvLower -ne "" -and $ini.ContainsKey($primaryEnvLower)) {
                    $eSec = $ini[$primaryEnvLower]
                    $obj.PrimaryEnv = @{ Name = $eSec["_OriginalName"] }
                    foreach($tag in @("sourcepath", "rpocustom", "rootpath", "startpath", "dbdatabase", "dbalias", "dbserver", "dbport")){
                        if($eSec.ContainsKey($tag)){ $obj.PrimaryEnv[$tag] = $eSec[$tag] }
                    }
                }
            }

            foreach ($sec in $ini.Keys) {
                if ($knownSections -notcontains $sec -and $sec -ne $primaryEnvLower) {
                    $s = $ini[$sec]
                    if ($s.ContainsKey("sourcepath") -and $s.ContainsKey("rootpath") -and $s.ContainsKey("rpocustom")) {
                        $obj.AddEnvs += @{ Name = $s["_OriginalName"]; sourcepath = $s["sourcepath"]; rpocustom = $s["rpocustom"] }
                    }
                }
            }
        }

        $results += $obj
    }

    Write-Output "===JSON-START==="
    Write-Output ($results | ConvertTo-Json -Depth 5 -Compress)
    Write-Output "===JSON-END==="
    """
    
    ps_script = ps_script.replace("__UPDATE_DESC__", update_desc)

    try:
        if _is_local_target(server_address):
            out, err = _run_ps_local(ps_script)
        else:
            out, err = _run_ps_winrm(server_address, ps_script)
            
        start_idx = out.find("===JSON-START===")
        end_idx = out.find("===JSON-END===")
        
        if start_idx != -1 and end_idx != -1:
            json_str = out[start_idx + 16:end_idx].strip()
            if json_str:
                data = json.loads(json_str)
                if not data:
                    return jsonify({"ok": False, "message": "Nenhum arquivo validado. Verifique os caminhos no servidor."})
                return jsonify({"ok": True, "data": data})
                
        return jsonify({"ok": False, "message": "Ocorreu uma falha no formato de resposta JSON."})
            
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro de comunicação: {str(e)}"})