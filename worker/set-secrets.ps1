# Sets the bot's two secrets without wrangler's typing prompt (pasting into it can drop characters):
#   DISCORD_WEBHOOK_URL  the stock-alerts channel's webhook, taken from your clipboard
#   DROP_MODE_KEY        a new random key, given to drop mode too, so the two always match
# Copy the stock-alerts channel's webhook in Discord first, and have drop mode running.
# Nothing secret is shown or kept (the file wrangler reads is deleted straight after).
# Run it with:  powershell -ExecutionPolicy Bypass -File set-secrets.ps1

$ErrorActionPreference = "Stop"
$drop = "http://127.0.0.1:8765"

$hook = "$(Get-Clipboard -Raw)".Trim()
if ($hook -notmatch '^https://(canary\.|ptb\.)?discord(app)?\.com/api/webhooks/\d+/[\w-]+$') {
    Write-Host "First copy the stock-alerts channel's webhook: in Discord, Edit Channel > Integrations > Webhooks > Copy Webhook URL. Then run this again." -ForegroundColor Yellow
    exit 1
}
try {
    Invoke-RestMethod -Uri "$drop/api/discord" -TimeoutSec 5 | Out-Null
} catch {
    Write-Host "Start drop mode first (it gets the new key too), then run this again." -ForegroundColor Yellow
    exit 1
}

# A new key: 40 random letters and numbers
$abc = [char[]]"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
$bytes = New-Object byte[] 40
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$key = -join ($bytes | ForEach-Object { $abc[$_ % $abc.Length] })

# Both secrets to the bot, from a file that's deleted straight after
$file = Join-Path $env:TEMP ("bot-secrets-" + [guid]::NewGuid().ToString("N") + ".json")
$ok = $false
try {
    [System.IO.File]::WriteAllText($file, (@{ DISCORD_WEBHOOK_URL = $hook; DROP_MODE_KEY = $key } | ConvertTo-Json -Compress))
    Push-Location $PSScriptRoot
    npx.cmd wrangler secret bulk $file
    $ok = $LASTEXITCODE -eq 0
} finally {
    Pop-Location
    Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
}
if (-not $ok) {
    Write-Host "The bot's secrets weren't changed (wrangler's message is above)." -ForegroundColor Red
    exit 1
}

# The same key to drop mode, then wait for it to connect (the bot can take a few seconds to use the new key)
Invoke-RestMethod -Uri "$drop/api/discord" -Method Post -ContentType "application/json" -Body (@{ botKey = $key } | ConvertTo-Json -Compress) | Out-Null
Set-Clipboard -Value " "  # (the webhook link off your clipboard)
$state = ""
for ($i = 0; $i -lt 12; $i++) {
    $state = (Invoke-RestMethod -Uri "$drop/api/discord").lists.state
    if ($state -eq "ok") { break }
    Start-Sleep -Seconds 2
}
if ($state -eq "ok") {
    Write-Host "Done: the bot posts its stock alerts in that channel, and drop mode is connected with the new key." -ForegroundColor Green
} else {
    Write-Host "The bot has both secrets, but drop mode hasn't connected yet ($state). Give it a minute and look at Settings > Bot." -ForegroundColor Yellow
}
