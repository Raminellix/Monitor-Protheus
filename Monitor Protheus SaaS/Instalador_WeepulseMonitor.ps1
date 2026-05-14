# Exige privilégios de Administrador
if (!([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning "Por favor, execute este script como Administrador!"
    Pause
    Exit
}

$PythonVersion = "3.12.2"
$PythonInstallerUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
$InstallerPath = "$env:TEMP\python-installer.exe"

Write-Host "=== INSTALADOR AUTOMATIZADO WEEPULSE ===" -ForegroundColor Cyan

function Test-PythonExe {
    param([string]$ExePath)
    if ([string]::IsNullOrWhiteSpace($ExePath)) { return $false }
    if (!(Test-Path $ExePath)) { return $false }
    try {
        $out = & $ExePath --version 2>&1
        return ($LASTEXITCODE -eq 0 -and ($out -match "Python\s+\d+\.\d+\.\d+"))
    } catch {
        return $false
    }
}

function Find-WorkingPython {
    # 1) Tenta "python" do PATH (mas pode ser alias da Store)
    try {
        $cmd = Get-Command "python" -ErrorAction SilentlyContinue
        if ($cmd -and (Test-PythonExe -ExePath $cmd.Source)) {
            return $cmd.Source
        }
    } catch {}

    # 2) Tenta locais comuns (instalação all-users)
    $candidates = @(
        "C:\Program Files\Python312\python.exe",
        "C:\Program Files\Python311\python.exe",
        "C:\Program Files\Python310\python.exe",
        "C:\Program Files\Python39\python.exe",
        "C:\Program Files\Python38\python.exe"
    )

    foreach ($c in $candidates) {
        if (Test-PythonExe -ExePath $c) { return $c }
    }

    # 3) Tenta instalação por usuário
    $userCandidates = @(
        "$env:LocalAppData\Programs\Python\Python312\python.exe",
        "$env:LocalAppData\Programs\Python\Python311\python.exe",
        "$env:LocalAppData\Programs\Python\Python310\python.exe"
    )
    foreach ($c in $userCandidates) {
        if (Test-PythonExe -ExePath $c) { return $c }
    }

    return $null
}

# =================================================================================
# 1. Verifica se existe Python REAL (não alias)
# =================================================================================
$PythonExe = Find-WorkingPython

if (-not $PythonExe) {
    Write-Host "Python não detectado (ou alias da Microsoft Store). Baixando instalador silencioso..." -ForegroundColor Yellow
    try {
        Invoke-WebRequest -Uri $PythonInstallerUrl -OutFile $InstallerPath -UseBasicParsing
    } catch {
        Write-Error "Falha ao baixar o instalador do Python: $($_.Exception.Message)"
        Pause
        Exit
    }

    Write-Host "Instalando Python $PythonVersion (Isso pode levar alguns minutos)..." -ForegroundColor Yellow
    # Instala para todos os usuários, adiciona ao PATH e não abre janelas
    $installArgs = "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0"
    Start-Process -FilePath $InstallerPath -ArgumentList $installArgs -Wait -NoNewWindow

    # Atualiza variáveis de ambiente no processo atual
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

    # Re-tenta localizar Python após instalação
    $PythonExe = Find-WorkingPython
    if (-not $PythonExe) {
        Write-Error "Python foi instalado, mas ainda não foi possível localizar um python.exe funcional no PATH ou nos diretórios padrão."
        Write-Host "Dica: reinicie o PowerShell e rode novamente este instalador." -ForegroundColor Yellow
        Pause
        Exit
    }

    Write-Host "Python instalado e detectado em: $PythonExe" -ForegroundColor Green
} else {
    Write-Host "Python detectado em: $PythonExe" -ForegroundColor Green
}

# =================================================================================
# 2. PEGA A PASTA EXATA ONDE O SCRIPT ESTÁ SENDO EXECUTADO
# =================================================================================
$ScriptDir = $PSScriptRoot
if ([string]::IsNullOrEmpty($ScriptDir)) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
Set-Location -Path $ScriptDir
Write-Host "Trabalhando no diretório base: $ScriptDir" -ForegroundColor DarkGray

# =================================================================================
# 3. Atualiza o PIP e instala os pacotes apontando para o arquivo exato
# =================================================================================
Write-Host "Instalando bibliotecas requeridas (Flask, PyWin32, WinRM, etc)..." -ForegroundColor Yellow
$RequirementsPath = Join-Path -Path $ScriptDir -ChildPath "requirements.txt"

try {
    & $PythonExe -m ensurepip --upgrade | Out-Null
} catch {}

& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install -r $RequirementsPath

Write-Host "Executando script de pós-instalação do PyWin32..." -ForegroundColor Yellow

# O pywin32_postinstall.py fica dentro de site-packages/pywin32_system32 ou Scripts dependendo do ambiente.
# Tentamos localizar automaticamente.
$pywin32PostInstall = $null
try {
    $site = & $PythonExe -c "import sys,site; print(site.getsitepackages()[0])" 2>$null
    if ($site) {
        $candidate1 = Join-Path $site "Scripts\pywin32_postinstall.py"
        $candidate2 = Join-Path $site "pywin32_system32\pywin32_postinstall.py"
        if (Test-Path $candidate1) { $pywin32PostInstall = $candidate1 }
        elseif (Test-Path $candidate2) { $pywin32PostInstall = $candidate2 }
    }
} catch {}

if (-not $pywin32PostInstall) {
    # fallback: tentar caminho relativo antigo
    $candidateLocal = Join-Path $ScriptDir "Scripts\pywin32_postinstall.py"
    if (Test-Path $candidateLocal) { $pywin32PostInstall = $candidateLocal }
}

if ($pywin32PostInstall) {
    & $PythonExe $pywin32PostInstall -install -quiet
} else {
    Write-Warning "Não foi possível localizar pywin32_postinstall.py automaticamente. Se tiver erro de COM/pywin32 depois, reinstale pywin32."
}

# =================================================================================
# 4. INSTALA E INICIA O SERVIÇO COM CAMINHO ABSOLUTO
# =================================================================================
Write-Host "Configurando o Serviço do Windows..." -ForegroundColor Yellow

$ServiceScript = Join-Path -Path $ScriptDir -ChildPath "weepulse_service.py"

& $PythonExe $ServiceScript install
Start-Sleep -Seconds 2
& $PythonExe $ServiceScript start

Write-Host "=== INSTALAÇÃO CONCLUÍDA COM SUCESSO! ===" -ForegroundColor Cyan
Write-Host "O Weepulse Monitor foi registrado e está rodando a partir de: $ScriptDir"
Pause