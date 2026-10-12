# Windows 로그온 시 dealbot 자동 실행 등록 (재부팅 후 자동 시작)
# 카카오톡 창 자동화는 '로그인된 데스크톱 세션'이 필요하므로 서비스가 아니라 로그온 트리거를 쓴다.
# 사용법 (PowerShell): powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1
param([string]$TaskName = "dealbot")

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$bat = Join-Path $root "deploy\windows\run_dealbot.bat"

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT2M"   # 로그온 2분 후 (카카오톡이 먼저 뜨도록)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null
Write-Host "등록 완료: 작업 '$TaskName' (로그온 시 자동 실행). 지금 시작: Start-ScheduledTask -TaskName $TaskName"
