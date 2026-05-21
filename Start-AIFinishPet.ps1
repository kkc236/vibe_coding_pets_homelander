[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$app = Join-Path $PSScriptRoot "assistant_pet.py"
if (-not (Test-Path -LiteralPath $app)) {
    throw "AI Finish Pet app not found: $app"
}

$candidates = New-Object System.Collections.Generic.List[object]

function Add-PythonCandidate {
    param(
        [string]$Path,
        [string[]]$ArgsPrefix = @()
    )
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return
    }
    if ($Path -match "\\WindowsApps\\pythonw?\.exe$") {
        return
    }
    if (Test-Path -LiteralPath $Path) {
        $script:candidates.Add([pscustomobject]@{
            Path = $Path
            ArgsPrefix = $ArgsPrefix
        }) | Out-Null
    }
}

Add-PythonCandidate -Path $env:AI_FINISH_PET_PYTHON

$bundledPythonDir = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python"
Add-PythonCandidate -Path (Join-Path $bundledPythonDir "pythonw.exe")
Add-PythonCandidate -Path (Join-Path $bundledPythonDir "python.exe")

foreach ($name in @("pythonw", "python", "py")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) {
        continue
    }
    $prefix = @()
    if ($cmd.Name -like "py*") {
        $prefix = @("-3")
    }
    Add-PythonCandidate -Path $cmd.Source -ArgsPrefix $prefix
}

if ($candidates.Count -eq 0) {
    throw "Python not found. Install Python 3 and make sure python or pythonw is on PATH."
}

$python = $candidates[0]
$args = @($python.ArgsPrefix)
$args += "`"$app`""

Start-Process `
    -FilePath $python.Path `
    -ArgumentList $args `
    -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden
