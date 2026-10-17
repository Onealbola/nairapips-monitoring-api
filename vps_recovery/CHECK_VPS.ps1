$ErrorActionPreference = 'Stop'
$Engine = 'C:\Users\Administrator\Documents\nairapips_mt5_rotation_engine (1)'
$Names = @('NAIRAPIPS_DD_FLEET_SUPERVISOR_V3_20_CLEAN_LIVE_STATE_20261008.py','DD_POLICE_V3_20_CLEAN_LIVE_STATE_TELEMETRY_20261008.py','DD_BREACH_RELAY_V1_20261003.py','DD_ROSTER_WATCHER_V1_20261003.py')
$Files = @($Names | ForEach-Object {
    $Path = Join-Path $Engine $_
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $Item = Get-Item -LiteralPath $Path
        [PSCustomObject]@{Name=$Item.Name;Bytes=$Item.Length;SHA256=(Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash;HasMain=[bool](Select-String -LiteralPath $Path -Pattern '^def main\(' -Quiet)}
    } else { [PSCustomObject]@{Name=$_;Missing=$true} }
})
$All = @(Get-CimInstance Win32_Process)
$Processes = @($All | Where-Object {$_.Name -match '^python(w)?\.exe$'} | ForEach-Object {
    $Process = $_
    $ScriptNames = @([regex]::Matches($Process.CommandLine, '[^\\/"\s]+\.py\b') | ForEach-Object {$_.Value})
    $Parent = $All | Where-Object {$_.ProcessId -eq $Process.ParentProcessId} | Select-Object -First 1
    [PSCustomObject]@{PID=$Process.ProcessId;ParentPID=$Process.ParentProcessId;ParentName=$Parent.Name;Scripts=$ScriptNames;Started=$Process.CreationDate}
})
$Report = [PSCustomObject]@{
    CheckedAt=(Get-Date).ToUniversalTime().ToString('o');EngineFolder=$Engine;Files=$Files;PythonProcesses=$Processes
    UserLiveStateKeyConfigured=[bool][Environment]::GetEnvironmentVariable('NP_LIVE_STATE_INGEST_KEY','User')
    ProcessLiveStateKeyConfigured=[bool][Environment]::GetEnvironmentVariable('NP_LIVE_STATE_INGEST_KEY','Process')
    Note='Read-only process/file inspection. No raw command lines, credentials, account data, process stops, installations or network requests.'
}
$Output = Join-Path ([Environment]::GetFolderPath('Desktop')) 'NairaPips-VPS-check.json'
$Report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Output -Encoding UTF8
Write-Host "CHECK COMPLETE. Report saved to: $Output"
Write-Host 'Attach NairaPips-VPS-check.json to the chat. Running programs were not changed.'
