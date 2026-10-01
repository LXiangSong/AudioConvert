$ErrorActionPreference = 'Stop'
# Quick Edit selection pauses console output and can make a completed build look stuck.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class AudioConvertConsole {
    [DllImport("kernel32.dll")] public static extern IntPtr GetStdHandle(int id);
    [DllImport("kernel32.dll")] public static extern bool GetConsoleMode(IntPtr handle, out uint mode);
    [DllImport("kernel32.dll")] public static extern bool SetConsoleMode(IntPtr handle, uint mode);
}
'@
$consoleInput = [AudioConvertConsole]::GetStdHandle(-10)
[uint32]$consoleMode = 0
if ([AudioConvertConsole]::GetConsoleMode($consoleInput, [ref]$consoleMode)) {
    [void][AudioConvertConsole]::SetConsoleMode($consoleInput, (($consoleMode -bor 0x80) -band 0xFFBF))
}
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$outputDir = Join-Path $PSScriptRoot 'output'
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
$logFile = Join-Path $outputDir 'build.log'
Set-Content -LiteralPath $logFile -Value ("AudioConvert build: " + (Get-Date -Format s)) -Encoding UTF8

function Invoke-Logged {
    param([string]$Program, [string[]]$Arguments)
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Program @Arguments 2>&1 | ForEach-Object {
            $line = $_.ToString()
            Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
            Write-Host $line
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedPreference
    }
    if ($code -ne 0) { throw "Command failed (exit $code): $Program" }
}

try {
    Write-Host '[1/3] Preparing Python 3.13 x64...' -ForegroundColor Cyan
    $pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            Invoke-Logged -Program 'py' -Arguments @('-3.13', '-m', 'venv', '.venv')
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            Invoke-Logged -Program 'python' -Arguments @('-c', 'import sys,struct; sys.exit(not(sys.version_info[:2] == (3,13) and struct.calcsize(chr(80)) == 8))')
            Invoke-Logged -Program 'python' -Arguments @('-m', 'venv', '.venv')
        } else {
            throw 'Install Python 3.13 x64 with the Python launcher, then run build.bat again.'
        }
    }
    Invoke-Logged -Program $pythonExe -Arguments @('-c', 'import sys,struct; sys.exit(not(sys.version_info[:2] == (3,13) and struct.calcsize(chr(80)) == 8))')
    Write-Host '[2/3] Checking and installing dependencies...' -ForegroundColor Cyan
    Invoke-Logged -Program $pythonExe -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', 'requirements.txt')
    Write-Host '[3/3] Building EXE and checking startup...' -ForegroundColor Cyan
    Invoke-Logged -Program $pythonExe -Arguments @('build.py')
    Write-Host ''
    Write-Host 'SUCCESS: dist\AudioConvert.exe' -ForegroundColor Green
    Write-Host 'You can copy this EXE to another Windows x64 computer.'
    Start-Process explorer.exe -ArgumentList ('/select,"' + (Join-Path $PSScriptRoot 'dist\AudioConvert.exe') + '"')
    exit 0
} catch {
    $message = $_.Exception.Message
    Add-Content -LiteralPath $logFile -Value ("FAILED: " + $message) -Encoding UTF8
    Write-Host ("FAILED: " + $message) -ForegroundColor Red
    Write-Host ("Log: " + $logFile)
    exit 1
}
