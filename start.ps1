[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 5000,

    [switch]$NoBrowser,

    [switch]$Install
)

$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$venvDirectory = Join-Path $projectRoot ".venv"
$pythonExecutable = Join-Path $venvDirectory "Scripts\python.exe"
$requirementsFile = Join-Path $projectRoot "requirements.txt"
$requirementsStamp = Join-Path $venvDirectory ".requirements.sha256"
$appFile = Join-Path $projectRoot "server\app.py"
$previousPortExists = Test-Path Env:PORT
$previousPort = $env:PORT

function New-ProjectVirtualEnvironment {
    Write-Host "Pravim Python virtualno okruzenje..." -ForegroundColor Cyan

    $launcher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($launcher) {
        & $launcher.Source -3 -m venv $venvDirectory
    }
    else {
        $launcher = Get-Command "python" -ErrorAction SilentlyContinue
        if (-not $launcher) {
            throw "Python nije pronadjen. Instaliraj Python 3 i pokusaj ponovo."
        }

        & $launcher.Source -m venv $venvDirectory
    }

    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonExecutable)) {
        throw "Pravljenje virtualnog okruzenja nije uspelo."
    }
}

function Get-Sha256 {
    param([string]$Path)

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $hashBytes = $sha256.ComputeHash($stream)
        return ([System.BitConverter]::ToString($hashBytes)).Replace("-", "")
    }
    finally {
        $stream.Dispose()
        $sha256.Dispose()
    }
}

function Test-TcpPortInUse {
    param([int]$TcpPort)

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect("127.0.0.1", $TcpPort)
        return $true
    }
    catch [System.Net.Sockets.SocketException] {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

Push-Location -LiteralPath $projectRoot
try {
    if (-not (Test-Path -LiteralPath $pythonExecutable)) {
        New-ProjectVirtualEnvironment
    }

    $requirementsHash = Get-Sha256 -Path $requirementsFile
    $installedHash = if (Test-Path -LiteralPath $requirementsStamp) {
        (Get-Content -LiteralPath $requirementsStamp -Raw).Trim()
    }
    else {
        ""
    }

    if ($Install -or $requirementsHash -ne $installedHash) {
        Write-Host "Instaliram Python pakete..." -ForegroundColor Cyan
        & $pythonExecutable -m pip install --disable-pip-version-check -r $requirementsFile

        if ($LASTEXITCODE -ne 0) {
            throw "Instalacija Python paketa nije uspela."
        }

        Set-Content -LiteralPath $requirementsStamp -Value $requirementsHash -NoNewline -Encoding Ascii
    }

    if (Test-TcpPortInUse -TcpPort $Port) {
        throw "Port $Port je vec zauzet. Probaj: .\start.cmd -Port 5001"
    }

    $env:PORT = $Port.ToString()
    $url = "http://localhost:$Port"
    $browserJob = $null

    Write-Host ""
    Write-Host "EloPremier je dostupan na $url" -ForegroundColor Green
    Write-Host "Za zaustavljanje pritisni Ctrl+C." -ForegroundColor DarkGray
    Write-Host ""

    if (-not $NoBrowser) {
        $browserJob = Start-Job -ScriptBlock {
            param($TargetUrl)

            for ($attempt = 0; $attempt -lt 80; $attempt++) {
                try {
                    $response = Invoke-WebRequest -Uri $TargetUrl -UseBasicParsing -TimeoutSec 1
                    if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                        Start-Process $TargetUrl
                        return
                    }
                }
                catch {
                    # Flask se jos pokrece; pokusaj ponovo.
                }

                Start-Sleep -Milliseconds 250
            }
        } -ArgumentList $url
    }

    try {
        & $pythonExecutable $appFile
        if ($LASTEXITCODE -notin @(0, 130, -1073741510)) {
            throw "Aplikacija je zavrsena sa kodom $LASTEXITCODE."
        }
    }
    finally {
        if ($browserJob) {
            Stop-Job -Job $browserJob -ErrorAction SilentlyContinue
            Remove-Job -Job $browserJob -Force -ErrorAction SilentlyContinue
        }
    }
}
finally {
    if ($previousPortExists) {
        $env:PORT = $previousPort
    }
    else {
        Remove-Item Env:PORT -ErrorAction SilentlyContinue
    }

    Pop-Location
}
