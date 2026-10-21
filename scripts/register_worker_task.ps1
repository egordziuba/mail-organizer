param(
    [Parameter(Mandatory=$true)][string]$ProjectPath,
    [Parameter(Mandatory=$true)][string]$ConfigPath
)
$ErrorActionPreference = 'Stop'
$taskName = 'MailOrganizer.HiddenWorker'
$ProjectPath = [IO.Path]::GetFullPath($ProjectPath)
$ConfigPath = [IO.Path]::GetFullPath($ConfigPath)
$python = Join-Path $ProjectPath '.venv\Scripts\python.exe'
$oldPythonw = Join-Path $ProjectPath '.venv\Scripts\pythonw.exe'
if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath $ConfigPath)) { throw 'Project Python or runtime configuration is missing.' }
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$xmlPath = Join-Path $ProjectPath '.runtime\scheduled-task.xml'
$env:PYTHONHOME = $null
$env:PYTHONPATH = $null
& $python -E -X utf8 -m mailorganizer.windows_task --project $ProjectPath --config $ConfigPath --user-sid $sid --output $xmlPath
if ($LASTEXITCODE -ne 0) { throw 'Task definition generation failed.' }
$xml = Get-Content -LiteralPath $xmlPath -Raw -Encoding UTF8
[xml]$definition = $xml
$expectedExe = $definition.Task.Actions.Exec.Command
$expectedArgs = $definition.Task.Actions.Exec.Arguments
$existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
$status = 'registered'
if ($null -ne $existing) {
    if ($existing.Actions.Execute -eq $expectedExe -and $existing.Actions.Arguments -eq $expectedArgs) {
        $status='already_registered'
    } else {
        $oldArgs = '-E -X utf8 -m mailorganizer.runtime run --config "' + $ConfigPath + '"'
        if ($existing.Actions.Execute -ne $oldPythonw -or $existing.Actions.Arguments -ne $oldArgs -or $existing.Actions.WorkingDirectory -ne $ProjectPath) {
            throw 'Task name belongs to another definition; nothing was overwritten.'
        }
        $action=New-ScheduledTaskAction -Execute $expectedExe -Argument $expectedArgs -WorkingDirectory $ProjectPath
        Set-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action | Out-Null
        $status='upgraded_gui_action_only'
    }
} else {
    Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Xml $xml -ErrorAction Stop | Out-Null
}
$task = Get-ScheduledTask -TaskName $taskName -TaskPath '\'
[pscustomobject]@{status=$status; task_name=$taskName; state=$task.State.ToString(); user_id=$task.Principal.UserId; run_level=$task.Principal.RunLevel.ToString(); hidden=$task.Settings.Hidden; executable=$task.Actions.Execute; arguments=$task.Actions.Arguments} | ConvertTo-Json -Compress
