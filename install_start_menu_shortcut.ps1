$ErrorActionPreference = "Stop"

$AppName = "StreamVault"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonPath = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\pythonw3.12.exe"
$ConsolePythonPath = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\python3.12.exe"
$LauncherPath = Join-Path $ProjectRoot "launcher.pyw"
$IconPath = Join-Path $ProjectRoot "icons\movie.ico"
$StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$ShortcutPath = Join-Path $StartMenuDir "$AppName.lnk"

if (-not (Test-Path -LiteralPath $PythonPath)) {
    if (Test-Path -LiteralPath $ConsolePythonPath) {
        $PythonPath = $ConsolePythonPath
    } else {
        throw "Could not find Python 3.12 in $env:LOCALAPPDATA\Microsoft\WindowsApps."
    }
}

if (-not (Test-Path -LiteralPath $LauncherPath)) {
    throw "Could not find $LauncherPath."
}

if (-not (Test-Path -LiteralPath $IconPath)) {
    $IconPath = $PythonPath
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $PythonPath
$shortcut.Arguments = "`"$LauncherPath`""
$shortcut.WorkingDirectory = $ProjectRoot
$shortcut.IconLocation = "$IconPath,0"
$shortcut.Description = "Launch StreamVault"
$shortcut.Save()

Write-Host "Created Start Menu shortcut:"
Write-Host $ShortcutPath
Write-Host ""
Write-Host "Open Start, search StreamVault, then right-click it and choose Pin to Start."
