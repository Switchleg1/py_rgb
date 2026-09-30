# Stops MSI Center RGB services (they hold the SMBus) and starts OpenRGB's SDK server.
$ErrorActionPreference = 'SilentlyContinue'
Stop-Process -Name OpenRGB -Force
Stop-Service Mystic_Light_Service, MSI_Case_Service, LightKeeperService -Force
Start-Sleep -Seconds 2
Start-Process -FilePath 'C:\Program Files\OpenRGB\OpenRGB.exe' -ArgumentList '--server'
Start-Sleep -Seconds 8
