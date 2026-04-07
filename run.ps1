Set-Location "D:\Lakshay\Auto-1"

Write-Output "Starting docker compose at $(Get-Date)" | Out-File "D:\Lakshay\Auto-1\scheduler.log" -Append

& "C:\Program Files\Docker\Docker\resources\bin\docker.exe" compose up -d

Write-Output "Completed at $(Get-Date)" | Out-File "D:\Lakshay\Auto-1\scheduler.log" -Append