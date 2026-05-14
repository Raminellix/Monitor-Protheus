import os
import re
import time
import socket
import subprocess
import base64
import json
import uuid
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required
from .models import Server, AppConfig, ServerMetric
from .extensions import db
from .mailer import send_alert_email

servers_bp = Blueprint("servers", __name__)

DEBUG_METRICS = True
ALERT_COOLDOWN_CACHE = {}
COOLDOWN_SECONDS = 60

TOP_CPU_PROCESSES = 10
TOP_MEM_PROCESSES = 10

# Força chamar o PowerShell "Windows PowerShell" (não pwsh)
POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


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


def get_ping_latency(address: str) -> str:
    """Dispara um ping rápido para medir a latência real de rede do servidor"""
    if _is_local_target(address):
        return "< 1ms"
    try:
        r = subprocess.run(["ping", "-n", "1", "-w", "500", address], capture_output=True, text=True)
        match = re.search(r"tempo[=<](\d+ms)", r.stdout, re.IGNORECASE) or re.search(
            r"time[=<](\d+ms)", r.stdout, re.IGNORECASE
        )
        if match:
            lat = match.group(1)
            return lat.replace("ms", " ms")
        return "Timeout"
    except Exception:
        return "Erro"


def _parse_ps_json(txt: str):
    txt = (txt or "").strip()
    if not txt:
        return {}

    start_idx = txt.find("===JSON-START===")
    end_idx = txt.find("===JSON-END===")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        inner = txt[start_idx + 16 : end_idx].strip()
        if inner:
            try:
                return json.loads(inner)
            except Exception as e:
                print(f"[ERRO JSON SERVER marker] {e}")

    m = re.search(r"(\{.*\})", txt, flags=re.DOTALL)
    if m:
        candidate = m.group(1).strip()
        try:
            return json.loads(candidate)
        except Exception as e:
            print(f"[ERRO JSON SERVER fallback] {e}")
            return {}

    return {}


def _run_ps_local(ps: str, timeout: int = 120) -> str:
    ps = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + ps
    encoded_ps = base64.b64encode(ps.encode("utf-16le")).decode("utf-8")
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_ps],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=timeout,
    )
    return r.stdout or ""


def _winrm_session(address: str):
    import winrm

    cfg = AppConfig.query.first()

    user = cfg.winrm_user if cfg and cfg.winrm_user else ""
    pwd = cfg.winrm_password if cfg and cfg.winrm_password else ""
    transport = cfg.winrm_transport if cfg and cfg.winrm_transport else "ntlm"
    use_ssl = cfg.winrm_ssl if cfg else False

    scheme = "https" if use_ssl else "http"
    port = 5986 if use_ssl else 5985

    return winrm.Session(f"{scheme}://{address}:{port}/wsman", auth=(user, pwd), transport=transport)


def _run_cmd_winrm(session, cmd, args):
    r = session.run_cmd(cmd, args)
    stdout = (r.std_out or b"").decode("utf-8", errors="ignore")
    stderr = (r.std_err or b"").decode("utf-8", errors="ignore")
    status_code = getattr(r, "status_code", None)
    return stdout, stderr, status_code


def _ps_command_encoded(cmd_text: str) -> str:
    raw = cmd_text or ""
    return base64.b64encode(raw.encode("utf-16le")).decode("ascii")


def _run_ps_winrm_via_tempfile(address: str, ps_script: str):
    session = _winrm_session(address)

    token = uuid.uuid4().hex
    ps1_name = f"weepulse_{token}.ps1"
    b64_name = f"weepulse_{token}.b64"

    ps_full = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + (ps_script or "")
    b64 = base64.b64encode(ps_full.encode("utf-8")).decode("ascii")

    chunk_size = 900
    chunks = [b64[i : i + chunk_size] for i in range(0, len(b64), chunk_size)]

    init_cmd = (
        f"$b64Path = (Join-Path $env:TEMP '{b64_name}'); "
        f"$ps1Path = (Join-Path $env:TEMP '{ps1_name}'); "
        f"if (Test-Path -LiteralPath $b64Path) {{ Remove-Item -LiteralPath $b64Path -Force }}; "
        f"if (Test-Path -LiteralPath $ps1Path) {{ Remove-Item -LiteralPath $ps1Path -Force }}; "
        f"[IO.File]::WriteAllText($b64Path, '', [Text.Encoding]::ASCII)"
    )
    out, err, sc = _run_cmd_winrm(
        session,
        POWERSHELL_EXE,
        ["-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", _ps_command_encoded(init_cmd)],
    )
    if sc not in (0, None):
        return out, err or "Falha ao inicializar arquivo .b64 no remoto.", sc

    for c in chunks:
        append_cmd = (
            f"$b64Path = (Join-Path $env:TEMP '{b64_name}'); "
            f"[IO.File]::AppendAllText($b64Path, '{c}', [Text.Encoding]::ASCII)"
        )
        out, err, sc = _run_cmd_winrm(
            session,
            POWERSHELL_EXE,
            ["-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", _ps_command_encoded(append_cmd)],
        )
        if sc not in (0, None):
            return out, err or "Falha ao enviar chunk base64 (PowerShell append).", sc

    build_cmd = (
        f"$b64Path = (Join-Path $env:TEMP '{b64_name}'); "
        f"$ps1Path = (Join-Path $env:TEMP '{ps1_name}'); "
        f"$b64 = [IO.File]::ReadAllText($b64Path, [Text.Encoding]::ASCII); "
        f"$bytes = [Convert]::FromBase64String($b64); "
        f"[IO.File]::WriteAllBytes($ps1Path, $bytes)"
    )
    out, err, sc = _run_cmd_winrm(
        session,
        POWERSHELL_EXE,
        ["-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", _ps_command_encoded(build_cmd)],
    )
    if sc not in (0, None):
        return out, err or "Falha ao montar .ps1 no remoto (decode base64).", sc

    exec_cmd = (
        f"$ps1Path = (Join-Path $env:TEMP '{ps1_name}'); "
        f"& {POWERSHELL_EXE} -NoProfile -ExecutionPolicy Bypass -File $ps1Path"
    )
    exec_out, exec_err, exec_sc = _run_cmd_winrm(
        session,
        POWERSHELL_EXE,
        ["-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", _ps_command_encoded(exec_cmd)],
    )

    cleanup_cmd = (
        f"$b64Path = (Join-Path $env:TEMP '{b64_name}'); "
        f"$ps1Path = (Join-Path $env:TEMP '{ps1_name}'); "
        f"if (Test-Path -LiteralPath $b64Path) {{ Remove-Item -LiteralPath $b64Path -Force }}; "
        f"if (Test-Path -LiteralPath $ps1Path) {{ Remove-Item -LiteralPath $ps1Path -Force }}"
    )
    _run_cmd_winrm(
        session,
        POWERSHELL_EXE,
        ["-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", _ps_command_encoded(cleanup_cmd)],
    )

    return exec_out, exec_err, exec_sc


def collect_server_health(address: str, server_name: str, cfg: AppConfig):
    ps_script = f"""
    $ProgressPreference = 'SilentlyContinue'

    function Emit-Json($obj) {{
        Write-Output "===JSON-START==="
        Write-Output (($obj | ConvertTo-Json -Depth 6 -Compress))
        Write-Output "===JSON-END==="
    }}

    $res = [ordered]@{{
        cpu_percent = 0.0
        mem_percent = 0.0
        uptime = "-"
        disks = @()
        top_cpu_processes = @()
        top_mem_processes = @()
        ps_error = ""
        ps_debug = @{{}}
    }}

    try {{
        # =====================================================================
        # 1. LENDO O TOTAL DA CPU (Valor Exato do Gestor de Tarefas)
        # =====================================================================
        try {{
            $cpuObj = Get-CimInstance Win32_Processor -ErrorAction Stop | Measure-Object -Property LoadPercentage -Average
            if ($cpuObj -and $cpuObj.Average -ne $null) {{ 
                $res.cpu_percent = [math]::Round([double]$cpuObj.Average, 1) 
            }}
        }} catch {{
            $res.ps_debug.cpu_err = $_.Exception.Message
        }}

        # =====================================================================
        # 2. LENDO O TOTAL DE MEMÓRIA (Em Uso Real) E UPTIME
        # =====================================================================
        try {{
            $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
            if ($os) {{
                if ($os.TotalVisibleMemorySize -gt 0) {{
                    $memUsed = $os.TotalVisibleMemorySize - $os.FreePhysicalMemory
                    $res.mem_percent = [math]::Round(($memUsed / $os.TotalVisibleMemorySize) * 100, 1)
                }}
                if ($os.LastBootUpTime) {{
                    $ts = (Get-Date) - $os.LastBootUpTime
                    $res.uptime = "$($ts.Days)d $($ts.Hours)h $($ts.Minutes)m"
                }}
            }}
        }} catch {{
            $res.ps_debug.mem_err = $_.Exception.Message
        }}

        # =====================================================================
        # 3. LENDO DISCOS
        # =====================================================================
        try {{
            $perfDisks = Get-CimInstance Win32_PerfFormattedData_PerfDisk_LogicalDisk -ErrorAction SilentlyContinue
            $disks = @()
            foreach ($d in Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" -ErrorAction Stop) {{
                $totGb = [math]::Round($d.Size / 1GB, 1)
                $freeGb = [math]::Round($d.FreeSpace / 1GB, 1)
                $usedGb = [math]::Round(($totGb - $freeGb), 1)
                $freePct = 0.0
                if ($totGb -gt 0) {{ $freePct = [math]::Round(($freeGb / $totGb) * 100, 1) }}

                $readMs = 0.0
                $writeMs = 0.0
                if ($perfDisks) {{
                    $perf = $perfDisks | Where-Object {{ $_.Name -like "$($d.DeviceID)*" }} | Select-Object -First 1
                    if ($perf) {{
                        if ($perf.AvgDisksecPerRead -ne $null) {{ $readMs = [math]::Round([double]$perf.AvgDisksecPerRead * 1000, 1) }}
                        if ($perf.AvgDisksecPerWrite -ne $null) {{ $writeMs = [math]::Round([double]$perf.AvgDisksecPerWrite * 1000, 1) }}
                    }}
                }}

                $disks += [PSCustomObject]@{{
                    mount = $d.DeviceID
                    total_gb = $totGb
                    used_gb = $usedGb
                    free_gb = $freeGb
                    free_percent = $freePct
                    read_ms = $readMs
                    write_ms = $writeMs
                }}
            }}
            $res.disks = $disks
        }} catch {{
            $res.ps_debug.disk_err = $_.Exception.Message
        }}

        # =====================================================================
        # 4. TOP 10 PROCESSOS DE MEMÓRIA
        # =====================================================================
        try {{
            $res.top_mem_processes = @(
                Get-Process -ErrorAction SilentlyContinue |
                  Sort-Object WorkingSet64 -Descending |
                  Select-Object -First {TOP_MEM_PROCESSES} |
                  ForEach-Object {{
                      [PSCustomObject]@{{
                          name = $_.ProcessName
                          pid = $_.Id
                          working_set_mb = [math]::Round(($_.WorkingSet64 / 1MB), 1)
                      }}
                  }}
            )
        }} catch {{
            $res.ps_debug.topmem_err = $_.Exception.Message
        }}

        # =====================================================================
        # 5. TOP 10 PROCESSOS DA CPU (Sem subscrever o Total)
        # =====================================================================
        try {{
            $cpuCount = 1
            try {{
                $cpuCount = (Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue).NumberOfLogicalProcessors
                if (-not $cpuCount -or $cpuCount -lt 1) {{ $cpuCount = 1 }}
            }} catch {{ $cpuCount = 1 }}

            $perfProcs = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process -ErrorAction SilentlyContinue

            $rows = @()
            foreach ($p in $perfProcs) {{
                # IGNORA o _Total (Para não quebrar a estatística) e o Tempo Ocioso (Idle)
                if ($p.Name -eq '_Total' -or $p.Name -eq 'Idle') {{ continue }}

                $pct = [math]::Round(($p.PercentProcessorTime / $cpuCount), 1)
                if ($pct -gt 0) {{
                    $rows += [PSCustomObject]@{{
                        name = $p.Name
                        pid = $p.IDProcess
                        cpu_percent = $pct
                        working_set_mb = [math]::Round(($p.WorkingSet / 1MB), 1)
                    }}
                }}
            }}

            $res.top_cpu_processes = @(
              $rows | Sort-Object cpu_percent -Descending | Select-Object -First {TOP_CPU_PROCESSES}
            )
        }} catch {{
            $res.ps_debug.topcpu_err = $_.Exception.Message
        }}

        { "if (-not " + ("$true" if DEBUG_METRICS else "$false") + ") { $res.Remove('ps_debug') }" }

        Emit-Json($res)
    }} catch {{
        $res.ps_error = $_.Exception.Message
        { "if (-not " + ("$true" if DEBUG_METRICS else "$false") + ") { $res.Remove('ps_debug') }" }
        Emit-Json($res)
    }}
    """

    try:
        if _is_local_target(address):
            stdout = _run_ps_local(ps_script)
            stderr = ""
            status_code = None
        else:
            stdout, stderr, status_code = _run_ps_winrm_via_tempfile(address, ps_script)

        data = _parse_ps_json(stdout)

        if not data or "cpu_percent" not in data:
            preview = (stdout or "").strip().replace("\r", "")
            preview = preview[:800]
            return {
                "server_id": None,
                "server_address": address,
                "server_name": server_name,
                "cpu_percent": 0.0,
                "mem_percent": 0.0,
                "uptime": "-",
                "latency": "Erro",
                "disks": [],
                "top_cpu_processes": [],
                "top_mem_processes": [],
                "status": "ERRO: WinRM/PowerShell retornou vazio ou fora do padrão JSON",
                "alerts": [],
                "email_sent": False,
                "ps_debug": {"python_err": "Resposta vazia ou inválida do PowerShell"} if DEBUG_METRICS else {},
                "winrm_status_code": status_code,
                "winrm_stderr": (stderr or "").strip(),
                "winrm_stdout_preview": preview,
            }

        if DEBUG_METRICS:
            data["winrm_status_code"] = status_code
            if stderr:
                data["winrm_stderr"] = (stderr or "").strip()

        alerts_triggered = []
        email_sent = False

        if cfg:
            if data.get("cpu_percent", 0) >= cfg.cpu_max_percent:
                alerts_triggered.append(
                    f"CPU atingiu {data.get('cpu_percent')}% (Máx configurado: {cfg.cpu_max_percent}%)"
                )

            if data.get("mem_percent", 0) >= cfg.mem_max_percent:
                alerts_triggered.append(
                    f"Memória RAM atingiu {data.get('mem_percent')}% (Máx configurado: {cfg.mem_max_percent}%)"
                )

            for d in data.get("disks", []):
                if d.get("free_percent", 100) <= cfg.disk_min_free_percent:
                    alerts_triggered.append(
                        f"Disco {d.get('mount')} lotando! Restam apenas {d.get('free_percent')}% livres (Mín configurado: {cfg.disk_min_free_percent}%)"
                    )

            if alerts_triggered:
                now = time.time()
                last_alert = ALERT_COOLDOWN_CACHE.get(address, 0)
                if (now - last_alert) > COOLDOWN_SECONDS:
                    if getattr(cfg, "webhook_url", None):
                        try:
                            from .webhook import send_webhook_alert

                            team_msg_items = "\n".join([f"- {a}" for a in alerts_triggered])
                            team_title = f"⚠️ Alerta de Recursos Críticos: {server_name}"
                            team_msg = (
                                f"**Servidor:** {server_name} ({address})\n\n"
                                f"**O sistema atingiu limites de segurança:**\n{team_msg_items}\n\n"
                                "Por favor, verifique imediatamente para evitar paragens no sistema."
                            )
                            send_webhook_alert(cfg.webhook_url, team_title, team_msg, "danger")
                        except Exception as webhook_err:
                            print(f"[ERRO TEAMS HARDWARE] {webhook_err}")

                    if cfg.alert_email_to:
                        subj = f"[Weepulse] Alerta de Recursos - {server_name}"
                        body = f"""
                        <h3 style="color: #1e293b; font-size: 20px; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; margin-bottom: 20px;">Métricas de Performance</h3>
                        <h4 style="color: #333; font-size: 16px; margin-bottom: 10px;">{server_name} ({address})</h4>
                        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 20px; font-family: 'Courier New', Courier, monospace; color: #475569; font-size: 14px; line-height: 1.8;">
                        """
                        for a in alerts_triggered:
                            body += f"<div style='color: #ef4444; font-weight: bold;'>- [CRÍTICO] {a}</div>"
                        body += "</div>"

                        success, err_msg = send_alert_email(subj, body)
                        if success:
                            email_sent = True
                        else:
                            print(f"[ALERTA EMAIL FALHOU] {err_msg}")

                    ALERT_COOLDOWN_CACHE[address] = now

        return {
            "server_id": None,
            "server_address": address,
            "server_name": server_name,
            "cpu_percent": data.get("cpu_percent", 0.0),
            "mem_percent": data.get("mem_percent", 0.0),
            "uptime": data.get("uptime", "-"),
            "latency": get_ping_latency(address),
            "disks": data.get("disks", []),
            "top_cpu_processes": data.get("top_cpu_processes", []),
            "top_mem_processes": data.get("top_mem_processes", []),
            "status": "OK" if not data.get("ps_error") else f"ERRO PS: {data.get('ps_error')}",
            "alerts": alerts_triggered,
            "email_sent": email_sent,
            "ps_debug": data.get("ps_debug", {}) if DEBUG_METRICS else {},
            "winrm_status_code": data.get("winrm_status_code", None) if DEBUG_METRICS else None,
            "winrm_stderr": data.get("winrm_stderr", "") if DEBUG_METRICS else "",
            "winrm_stdout_preview": "",
        }
    except Exception as e:
        return {
            "server_id": None,
            "server_address": address,
            "server_name": server_name,
            "cpu_percent": 0.0,
            "mem_percent": 0.0,
            "uptime": "-",
            "latency": "Erro",
            "disks": [],
            "top_cpu_processes": [],
            "top_mem_processes": [],
            "status": f"ERRO: {str(e)}",
            "alerts": [],
            "email_sent": False,
            "ps_debug": {"python_err": str(e)} if DEBUG_METRICS else {},
            "winrm_status_code": None,
            "winrm_stderr": "",
            "winrm_stdout_preview": "",
        }


def list_windows_event_errors(address: str):
    ps_script = """
    try {
        $ErrorActionPreference = 'SilentlyContinue'
        $events = Get-WinEvent -FilterHashtable @{LogName='System','Application'; Level=1,2; StartTime=(Get-Date).AddDays(-1)} -MaxEvents 50
        $res = @()
        foreach ($e in $events) {
            $res += [PSCustomObject]@{
                id = $e.RecordId
                time = $e.TimeCreated.ToString('dd/MM/yyyy HH:mm:ss')
                source = $e.ProviderName
                message = $e.Message
            }
        }
        Write-Output "===JSON-START==="
        Write-Output ($res | ConvertTo-Json -Depth 3 -Compress)
        Write-Output "===JSON-END==="
    } catch {
        Write-Output "===JSON-START==="
        Write-Output "[]"
        Write-Output "===JSON-END==="
    }
    """
    try:
        if _is_local_target(address):
            out = _run_ps_local(ps_script)
        else:
            out, _err, _sc = _run_ps_winrm_via_tempfile(address, ps_script)
        return _parse_ps_json(out)
    except Exception as e:
        print(f"[EVENT ERROR] {e}")
        return []


@servers_bp.route("/", methods=["GET"])
@login_required
def servers_home():
    cfg = AppConfig.query.first()
    if not cfg:
        cfg = AppConfig()
        db.session.add(cfg)
        db.session.commit()

    servers = Server.query.order_by(Server.id.asc()).all()
    health = []
    for s in servers:
        health.append(
            {
                "server_id": s.id,
                "server_address": s.address,
                "server_name": s.name,
                "cpu_percent": "-",
                "mem_percent": "-",
                "uptime": "-",
                "latency": "-",
                "disks": [],
                "top_cpu_processes": [],
                "top_mem_processes": [],
                "status": "Carregando...",
            }
        )

    return render_template("pages/servers.html", servers=servers, health=health, cfg=cfg)


@servers_bp.route("/scan", methods=["GET"])
@login_required
def scan_json():
    cfg = AppConfig.query.first()
    servers = Server.query.order_by(Server.id.asc()).all()

    results = []
    for s in servers:
        data = collect_server_health(s.address, s.name, cfg)
        data["server_id"] = s.id
        results.append(data)

    return jsonify({"ok": True, "health": results})


@servers_bp.route("/history/<int:server_id>", methods=["GET"])
@login_required
def history_json(server_id):
    minutes = request.args.get("minutes", default=30, type=int)
    cutoff = datetime.now() - timedelta(minutes=minutes)

    metrics = (
        ServerMetric.query.filter(ServerMetric.server_id == server_id, ServerMetric.timestamp >= cutoff)
        .order_by(ServerMetric.timestamp.asc())
        .all()
    )

    data = []
    for m in metrics:
        data.append({"time": m.timestamp.isoformat(), "cpu": m.cpu_percent, "mem": m.mem_percent})

    return jsonify({"ok": True, "history": data})


@servers_bp.route("/event-logs", methods=["POST"])
@login_required
def event_logs():
    address = request.json.get("server")
    items = list_windows_event_errors(address)
    if not isinstance(items, list):
        items = [items] if items else []
    return jsonify({"ok": True, "items": items})
