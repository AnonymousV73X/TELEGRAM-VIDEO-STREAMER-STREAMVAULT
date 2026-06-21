$ErrorActionPreference = "Stop"

$ShortcutPath = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\StreamVault.lnk"
if (-not (Test-Path -LiteralPath $ShortcutPath)) {
    throw "Shortcut not found: $ShortcutPath"
}

$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($ShortcutPath)
Write-Host "Shortcut=$ShortcutPath"
Write-Host "Target=$($shortcut.TargetPath)"
Write-Host "Arguments=$($shortcut.Arguments)"
Write-Host "WorkingDirectory=$($shortcut.WorkingDirectory)"
Write-Host "Icon=$($shortcut.IconLocation)"
