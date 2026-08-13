param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 7863,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"

function Import-DotEnvUtf8 {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false, $true)
    foreach ($line in [System.IO.File]::ReadAllLines($Path, $utf8NoBom)) {
        if ($line -notmatch '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            continue
        }
        $name = $Matches[1]
        if ($null -ne [Environment]::GetEnvironmentVariable($name, "Process")) {
            continue
        }
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or
             ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        } else {
            $value = [regex]::Replace($value, '\s+#.*$', '').TrimEnd()
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

$ProjectRoot = Split-Path -Parent $PSCommandPath
Import-DotEnvUtf8 -Path (Join-Path $ProjectRoot ".env")

if (-not $PSBoundParameters.ContainsKey("HostAddress") -and
    -not [string]::IsNullOrWhiteSpace($env:WEB_HOST)) {
    $HostAddress = $env:WEB_HOST.Trim()
}
if (-not $PSBoundParameters.ContainsKey("Port") -and
    -not [string]::IsNullOrWhiteSpace($env:WEB_PORT)) {
    $parsedPort = 0
    if (-not [int]::TryParse($env:WEB_PORT.Trim(), [ref]$parsedPort)) {
        throw "WEB_PORT must be an integer from 1 to 65535."
    }
    $Port = $parsedPort
}
if ($Port -lt 1 -or $Port -gt 65535) {
    throw "WEB_PORT must be in the range from 1 to 65535."
}

$normalizedHost = if ($HostAddress.StartsWith("[") -and $HostAddress.EndsWith("]")) {
    $HostAddress.Substring(1, $HostAddress.Length - 2)
} else {
    $HostAddress
}
$parsedAddress = $null
$isLoopback = $normalizedHost -ieq "localhost"
if (-not $isLoopback -and
    [System.Net.IPAddress]::TryParse($normalizedHost, [ref]$parsedAddress)) {
    $isLoopback = [System.Net.IPAddress]::IsLoopback($parsedAddress)
}
if (-not $isLoopback) {
    throw "The web interface can only listen on a loopback address."
}

$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Virtual environment Python was not found: $PythonPath"
}

$displayHost = if ($normalizedHost.Contains(":")) { "[$normalizedHost]" } else { $normalizedHost }
$serviceUrl = "http://${displayHost}:$Port"

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($listener) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$serviceUrl/api/health" -TimeoutSec 2
        $health = $response.Content | ConvertFrom-Json
        if ($response.StatusCode -eq 200 -and
            $health.service -eq "ocr-video-frame-sorting" -and
            $health.status -eq "ok") {
            Write-Host "Video Frame Sorter web already running: $serviceUrl"
            exit 0
        }
    } catch {
    }
    throw "Port $Port is busy by PID=$($listener.OwningProcess). Use another port."
}

Set-Location -LiteralPath $ProjectRoot
$env:WEB_HOST = $normalizedHost
$env:WEB_PORT = $Port.ToString()
$env:WEB_RELOAD = if ($Reload) { "1" } else { "0" }

Write-Host "Video Frame Sorter: $serviceUrl"
& $PythonPath -B -m frame_sorter.web
exit $LASTEXITCODE
