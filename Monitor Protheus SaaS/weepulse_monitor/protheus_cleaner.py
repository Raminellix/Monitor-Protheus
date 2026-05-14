import json
import base64
import html
from .mailer import send_alert_email
from .services_scan import _run_ps_local, _run_ps_winrm, _is_local_target

def run_protheus_cleanup(servers_list):
    print("\n[CLEANER] Iniciando mapeamento inteligente e rotina de limpeza (SYSTEM/SPOOL)...")
    
    for srv in servers_list:
        address = srv.address
        server_name = srv.name
        
        # Utilizamos r""" (Raw String) para enviar o script protegido para o servidor
        ps_script = r"""
        $ErrorActionPreference = 'SilentlyContinue'
        $days = 7
        $results = @()
        $processedSystem = @{} 

        # 1. Pega apenas AppServers ignorando TSS e TOTVS Sped
        $svcs = Get-CimInstance Win32_Service | Where-Object { $_.PathName -match '(?i)appserver\.exe' -and $_.PathName -notmatch '(?i)tss|totvssped' }

        foreach ($svc in $svcs) {
            $pathName = $svc.PathName
            $exePath = ($pathName -replace '^"|"$', '') -split ' -' | Select-Object -First 1

            # 2. Descobre onde está o INI
            $iniPath = ""
            $argsArray = $pathName -split '\s+'
            for ($i=0; $i -lt $argsArray.Length; $i++) {
                if ($argsArray[$i] -match '(?i)^[-/]ini=(.*)') {
                    $iniPath = $matches[1] -replace '^["'']|["'']$', ''; break
                } elseif ($argsArray[$i] -match '(?i)^[-/]ini$') {
                    if ($i + 1 -lt $argsArray.Length) {
                        $iniPath = $argsArray[$i+1] -replace '^["'']|["'']$', ''; break
                    }
                }
            }

            if ([string]::IsNullOrWhiteSpace($iniPath)) {
                $iniPath = [System.IO.Path]::ChangeExtension($exePath, ".ini")
            } elseif (-not [System.IO.Path]::IsPathRooted($iniPath)) {
                $iniPath = Join-Path ([System.IO.Path]::GetDirectoryName($exePath)) $iniPath
            }

            if (-not (Test-Path $iniPath)) { continue }

            # 3. Lê o INI para achar RootPath e StartPath
            $content = Get-Content $iniPath -ErrorAction SilentlyContinue
            $iniData = @{}
            $orderedEnvs = @()
            $currentSection = ""

            foreach ($line in $content) {
                $line = $line.Trim()
                if ($line -match '^\s*\[(.*)\]\s*$') {
                    $currentSection = $matches[1].Trim().ToLower()
                    if (-not $iniData.Contains($currentSection)) {
                        $iniData[$currentSection] = @{}
                        $orderedEnvs += $currentSection
                    }
                } elseif ($line -match '^([^=]+)=(.*)$' -and $currentSection -ne "") {
                    $k = $matches[1].Trim().ToLower()
                    $v = $matches[2].Trim()
                    $iniData[$currentSection][$k] = $v
                }
            }

            $envName = ""
            if ($iniData.Contains("general") -and $iniData["general"].Contains("app_environment")) {
                $envName = $iniData["general"]["app_environment"].ToLower()
            } else {
                foreach ($sec in $orderedEnvs) {
                    if ($iniData[$sec].Contains("rootpath") -and $iniData[$sec].Contains("startpath")) {
                        $envName = $sec; break
                    }
                }
            }

            if ($envName -ne "" -and $iniData.Contains($envName)) {
                $rootPath = $iniData[$envName]["rootpath"]
                $startPath = $iniData[$envName]["startpath"]

                if ($rootPath -and $startPath) {
                    $rootPath = $rootPath.TrimEnd('\', '/')
                    $startPath = $startPath.TrimStart('\', '/')
                    
                    $systemPath = Join-Path $rootPath $startPath
                    $spoolPath = Join-Path $rootPath "spool"

                    # Função de Limpeza Inteligente e Dividida por Regras
                    function Clean-Folder {
                        param($targetPath, $type)
                        if (-not (Test-Path $targetPath)) { return $null }
                        
                        $resolvedPath = (Resolve-Path $targetPath).Path
                        # Evita limpar o mesmo caminho duas vezes se 2 ambientes apontarem para a mesma root
                        if ($processedSystem.Contains($resolvedPath)) { return $null }
                        $processedSystem[$resolvedPath] = $true

                        $cutoff = (Get-Date).AddDays(-$days)
                        $totalBefore = 0; $totalDeleted = 0; $bytesDeleted = 0
                        
                        # Lista ultrarrápida para guardar o nome dos ficheiros
                        $deletedFilesList = New-Object System.Collections.Generic.List[System.String]

                        # REGRAS SEPARADAS DE LIMPEZA (Garantindo que não entra em subpastas)
                        if ($type -eq "SYSTEM") {
                            # No System: Apaga SÓ os ficheiros lixo conhecidos
                            $files = Get-ChildItem -Path $resolvedPath -File -ErrorAction SilentlyContinue | Where-Object {
                                ($_.Name -match '(?i)\.tmp$' -or $_.Name -match '(?i)^sc.*\.log$' -or $_.Name -match '(?i)^sc.*\.dtc$' -or $_.Name -match '(?i)^sc.*\.cdx$' -or $_.Name -match '(?i)^sc') -and $_.LastWriteTime -lt $cutoff
                            }
                        } else {
                            # No Spool: Apaga TODOS os ficheiros (*.*) mais antigos que $days
                            $files = Get-ChildItem -Path $resolvedPath -File -ErrorAction SilentlyContinue | Where-Object {
                                $_.LastWriteTime -lt $cutoff
                            }
                        }

                        if ($files) {
                            foreach ($f in $files) {
                                $totalBefore++
                                $size = $f.Length
                                try {
                                    Remove-Item -Path $f.FullName -Force -ErrorAction Stop
                                    $totalDeleted++
                                    $bytesDeleted += $size
                                    $deletedFilesList.Add($f.Name)
                                } catch { 
                                    # Ignora ficheiros travados em uso
                                }
                            }
                        }

                        return @{
                            folder_type = $type
                            path = $resolvedPath
                            total_before = $totalBefore
                            total_deleted = $totalDeleted
                            mb_freed = [math]::Round($bytesDeleted / 1MB, 2)
                            deleted_files = $deletedFilesList.ToArray()
                        }
                    }

                    $sysRes = Clean-Folder $systemPath "SYSTEM"
                    if ($sysRes) { $results += $sysRes }

                    $spoolRes = Clean-Folder $spoolPath "SPOOL"
                    if ($spoolRes) { $results += $spoolRes }
                }
            }
        }

        Write-Output "===JSON-START==="
        Write-Output ($results | ConvertTo-Json -Compress)
        Write-Output "===JSON-END==="
        """
        
        try:
            if _is_local_target(address):
                code, out, err = _run_ps_local(ps_script, timeout=300)
            else:
                code, out, err = _run_ps_winrm(address, ps_script)
                
            start_idx = out.find("===JSON-START===")
            end_idx = out.find("===JSON-END===")
            
            if start_idx != -1 and end_idx != -1:
                clean_json = out[start_idx + 16:end_idx].strip()
                if clean_json:
                    data = json.loads(clean_json)
                    _send_cleanup_report(server_name, address, data)
                else:
                    _send_cleanup_report(server_name, address, [])
            else:
                if err:
                    print(f"[CLEANER ERRO] Resposta inválida ou com erro PS: {err}")
        except Exception as e:
            print(f"[CLEANER ERRO CRÍTICO] Falha ao processar {server_name}: {e}")


def _send_cleanup_report(server_name, server_address, data):
    if data is None:
        data = []
        
    total_found = sum(item.get("total_before", 0) for item in data)
    total_deleted = sum(item.get("total_deleted", 0) for item in data)
    total_freed = sum(item.get("mb_freed", 0) for item in data)

    rows_html = ""
    details_html = ""
    
    if total_found > 0:
        for item in data:
            if item.get('total_before', 0) > 0:
                rows_html += f"""
                <tr>
                    <td style="padding: 10px; border-bottom: 1px solid #e2e8f0; font-family: monospace; font-size: 12px; color: #475569;">
                        <b style="color: #6366f1;">[{item.get('folder_type')}]</b><br>{html.escape(item.get('path'))}
                    </td>
                    <td style="padding: 10px; border-bottom: 1px solid #e2e8f0; text-align: center; color: #f59e0b; font-weight: bold;">{item.get('total_before')}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #e2e8f0; text-align: center; color: #10b981; font-weight: bold;">{item.get('total_deleted')}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #e2e8f0; text-align: center; color: #3b82f6; font-weight: bold;">{item.get('mb_freed')} MB</td>
                </tr>
                """
                
                deleted_files = item.get('deleted_files', [])
                if deleted_files:
                    files_str = "<br>".join(f"🗑️ {html.escape(f)}" for f in deleted_files)
                    details_html += f"""
                    <div style="margin-top: 25px;">
                        <div style="font-size: 13px; font-weight: bold; color: #1e293b; margin-bottom: 5px; border-bottom: 2px solid #cbd5e1; padding-bottom: 4px;">
                            📄 Registo de ficheiros eliminados na pasta: <span style="color: #6366f1;">[{item.get('folder_type')}]</span>
                        </div>
                        <div style="background-color: #0f172a; color: #10b981; font-family: 'Courier New', monospace; font-size: 11px; padding: 12px; border-radius: 6px; max-height: 250px; overflow-y: auto; white-space: nowrap; border: 1px solid #334155;">
                            {files_str}
                        </div>
                    </div>
                    """
    else:
        rows_html = f"""
        <tr>
            <td colspan="4" style="padding: 20px; text-align: center; color: #10b981; font-weight: bold; font-style: italic;">
                ✨ Nenhum ficheiro temporário precisou ser limpo. O servidor já está otimizado!
            </td>
        </tr>
        """

    # ==========================================
    # 1. DISPARO DO RESUMO PARA O TEAMS (SEMPRE ENVIA)
    # ==========================================
    try:
        from .models import AppConfig
        from .webhook import send_webhook_alert
        
        cfg = AppConfig.query.first()
        if cfg and getattr(cfg, 'webhook_url', None):
            if total_found == 0:
                team_title = f"✨ Servidor Limpo: {server_name}"
                team_msg = f"A faxina automática rodou com sucesso, mas o servidor **{server_name}** já estava limpo! Não foi necessário remover ficheiros temporários antigos."
            else:
                team_title = f"🧹 Limpeza Concluída: {server_name}"
                team_msg = f"A faxina automática das pastas System e Spool terminou!\n\n- **Ficheiros Removidos:** {total_deleted}\n- **Espaço Libertado:** {round(total_freed, 2)} MB\n\n*Os detalhes foram enviados para o e-mail de alerta.*"
            
            send_webhook_alert(cfg.webhook_url, team_title, team_msg, "success")
            print(f"[TEAMS] Resumo da limpeza enviado para o servidor {server_name}.")
    except Exception as e:
        print(f"[ERRO TEAMS CLEANER] Não foi possível alertar no Teams: {e}")

    # ==========================================
    # 2. DISPARO DO RELATÓRIO COMPLETO POR E-MAIL (SEMPRE ENVIA)
    # ==========================================
    html_body = f"""
    <div style="font-family: Arial, sans-serif; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px; background-color: #f8fafc;">
        <h2 style="color: #3b82f6; margin-top: 0; border-bottom: 1px solid #cbd5e1; padding-bottom: 10px;">🧹 Relatório de Limpeza e Auditoria</h2>
        <p style="color: #334155;">A rotina automática varreu o Protheus. Foram aplicadas regras de segurança rigorosas consoante o tipo de diretório.</p>
        
        <div style="margin-bottom: 15px; font-size: 14px;"><b>💻 Servidor:</b> {server_name} <span style="color: #64748b;">({server_address})</span></div>
        
        <table style="width: 100%; border-collapse: collapse; background-color: #fff; border: 1px solid #e2e8f0; margin-bottom: 10px;">
            <thead>
                <tr style="background-color: #f1f5f9;">
                    <th style="padding: 10px; border-bottom: 2px solid #cbd5e1; text-align: left; font-size: 13px;">Diretório (Raiz)</th>
                    <th style="padding: 10px; border-bottom: 2px solid #cbd5e1; text-align: center; font-size: 13px;">Ficheiros<br>Localizados</th>
                    <th style="padding: 10px; border-bottom: 2px solid #cbd5e1; text-align: center; font-size: 13px;">Ficheiros<br>Eliminados</th>
                    <th style="padding: 10px; border-bottom: 2px solid #cbd5e1; text-align: center; font-size: 13px;">Espaço<br>Libertado</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
            <tfoot>
                <tr style="background-color: #f8fafc;">
                    <td style="padding: 10px; font-weight: bold; text-align: right; color: #1e293b;">TOTAIS GERAIS:</td>
                    <td style="padding: 10px; text-align: center; font-weight: bold; color: #f59e0b;">{total_found}</td>
                    <td style="padding: 10px; text-align: center; font-weight: bold; color: #10b981;">{total_deleted}</td>
                    <td style="padding: 10px; text-align: center; font-weight: bold; color: #3b82f6;">{round(total_freed, 2)} MB</td>
                </tr>
            </tfoot>
        </table>
        
        <div style="font-size: 11px; color: #94a3b8; line-height: 1.4; margin-bottom: 10px;">
            * <b>Regra da pasta SYSTEM:</b> Exclui apenas *.tmp e sc* (log/dtc/cdx) criados há mais de 7 dias e limita-se ao diretório base.<br>
            * <b>Regra da pasta SPOOL:</b> Exclui *todos os ficheiros* criados há mais de 7 dias limitando-se ao diretório base.
        </div>
        
        {details_html}
        
    </div>
    """
    
    send_alert_email(
        subject=f"[Weepulse] Faxina Concluída - {server_name}", 
        html_content=html_body, 
        title="LIMPEZA E AUDITORIA", 
        intro_text="Resultados da varredura inteligente do sistema."
    )