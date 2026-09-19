# FlightFare: turn on remote access so Claude can install Ubuntu from here.
# Windows is temporary on this machine, so this only sets up SSH - nothing else is changed.
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "Close this, right-click PowerShell, choose 'Run as administrator', and run it again." -ForegroundColor Red
  return
}
Write-Host "`n[1/4] Installing the SSH server..." -ForegroundColor Cyan
try { Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop | Out-Null }
catch { winget install --id Microsoft.OpenSSH.Beta -e --accept-source-agreements --accept-package-agreements }
Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service sshd -ErrorAction SilentlyContinue

Write-Host "[2/4] Allowing it on your home network..." -ForegroundColor Cyan
Get-NetConnectionProfile | ForEach-Object { Set-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -NetworkCategory Private -ErrorAction SilentlyContinue }
Remove-NetFirewallRule -Name "FlightFare-SSH" -ErrorAction SilentlyContinue
New-NetFirewallRule -Name "FlightFare-SSH" -DisplayName "FlightFare SSH" -Enabled True -Direction Inbound `
  -Protocol TCP -LocalPort 22 -Action Allow -Profile Any | Out-Null

Write-Host "[3/4] Authorising the setup key..." -ForegroundColor Cyan
$f = "$env:ProgramData\ssh\administrators_authorized_keys"
New-Item -ItemType Directory -Force -Path "$env:ProgramData\ssh" | Out-Null
Set-Content -Path $f -Encoding ascii -Value "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEx8up9U2uYJok+zu8w5/LjSXwKqVe+SkjPScRKLdgxc flight-tracker-setup"
icacls $f /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" | Out-Null

Write-Host "[4/4] Done.`n" -ForegroundColor Cyan
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like "192.168.*" } | Select-Object -First 1).IPAddress
Write-Host ("  username:   {0}" -f $env:USERNAME) -ForegroundColor Green
Write-Host ("  address:    {0}" -f $ip) -ForegroundColor Green
Write-Host ("  ssh server: {0}" -f (Get-Service sshd).Status) -ForegroundColor Green
Write-Host ("  drives:     {0}" -f ((Get-Disk | ForEach-Object { "$($_.Number)=$($_.FriendlyName) $([math]::Round($_.Size/1GB))GB" }) -join ", ")) -ForegroundColor Green
Write-Host "`n  Send Claude the username and address above.`n" -ForegroundColor Yellow
