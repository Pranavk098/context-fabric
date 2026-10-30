# One-command, offline demo: a recorded Claude session becomes a Context Packet v0,
# then the same packet is installed as AGENTS.md for Codex and OpenCode.
# Usage: powershell -ExecutionPolicy Bypass -File scripts/demo_brief_handoff.ps1
# Prints PASS or FAIL and exits non-zero on any mismatch. Uses only a temporary directory.

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-Text([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText($Path, $Text, $utf8)
}

function Fail([string]$Message) {
    Write-Output "FAIL: $Message"
    exit 1
}

# Windows PowerShell 5.1 turns native stderr into a terminating error while
# $ErrorActionPreference is Stop; keep it non-terminating for the call and let
# the caller check $LASTEXITCODE.
function Invoke-Uv([string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & uv @Arguments 2>&1
    } finally {
        $ErrorActionPreference = $previous
    }
    return $output
}

$here = Split-Path -Parent $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Fail 'uv not found (install: https://docs.astral.sh/uv/)'
}

# Sync once outside the captured output so a fresh checkout prints only demo output.
$sync = Invoke-Uv @('run', '--project', $here, '--quiet', 'python', '-c', 'import cfxd')
if ($LASTEXITCODE -ne 0) { Write-Output $sync; Fail 'uv could not sync the project' }

$work = Join-Path ([System.IO.Path]::GetTempPath()) ('cfx-brief-handoff-' + [Guid]::NewGuid().ToString('N'))
$work = [System.IO.Path]::GetFullPath($work)
$fixture = Join-Path $work 'fixture'
$src = Join-Path $fixture 'src'
$tests = Join-Path $fixture 'tests'
$docs = Join-Path $fixture 'docs'
$sessionDir = Join-Path $work 'session'
$codexDir = Join-Path $work 'codex'
$opencodeDir = Join-Path $work 'opencode'
$packetPath = Join-Path $work 'packet.json'

try {
    foreach ($dir in @($src, $tests, $docs, $sessionDir, $codexDir, $opencodeDir, (Join-Path $work 'obs'))) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }

    $app = Join-Path $src 'app.py'
    $testApp = Join-Path $tests 'test_app.py'
    $notes = Join-Path $docs 'notes.md'
    Write-Text $app "def run():`n    return 1`n"
    Write-Text $testApp "from app import run`n`n`ndef test_run():`n    assert run() == 1`n"
    Write-Text $notes "# Notes`n`nHandoff demo.`n"

    # One recorded saved output (a fold): 500 lines of 99 bytes plus a newline.
    $foldContent = ("x" * 99 + "`n") * 500
    $foldBytes = $utf8.GetByteCount($foldContent)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $foldId = ([System.BitConverter]::ToString($sha.ComputeHash($utf8.GetBytes($foldContent))) -replace '-', '').ToLower().Substring(0, 16)
    } finally {
        $sha.Dispose()
    }
    if ($foldBytes -ne 50000 -or $foldId -ne '7d95df91bef22e0f') {
        Fail "fold fixture drifted: $foldBytes bytes, id $foldId"
    }
    Write-Text (Join-Path (Join-Path $work 'obs') ($foldId + '.txt')) $foldContent

    function New-Record([string]$Type, [string]$Timestamp, [object[]]$Content) {
        [ordered]@{
            type      = $Type
            timestamp = $Timestamp
            message   = [ordered]@{ role = $Type; content = @($Content) }
        }
    }

    function New-Tool([string]$Id, [string]$Name, $InputObject, [string]$Timestamp) {
        New-Record 'assistant' $Timestamp @(
            [ordered]@{ type = 'tool_use'; id = $Id; name = $Name; input = $InputObject }
        )
    }

    $lines = @(
        (New-Record 'user' '2026-01-02T00:00:00Z' @([ordered]@{ type = 'text'; text = 'Build the handoff demo.' })),
        (New-Tool 't1' 'Read' ([ordered]@{ file_path = $app }) '2026-01-02T00:00:01Z'),
        (New-Tool 't2' 'Edit' ([ordered]@{ file_path = $app }) '2026-01-02T00:00:02Z'),
        (New-Tool 't3' 'Read' ([ordered]@{ file_path = $testApp }) '2026-01-02T00:00:03Z'),
        (New-Tool 't4' 'Read' ([ordered]@{ file_path = $notes }) '2026-01-02T00:00:04Z'),
        (New-Tool 't5' 'Bash' ([ordered]@{ command = 'git status --short' }) '2026-01-02T00:00:05Z'),
        (New-Tool 't6' 'TodoWrite' ([ordered]@{ todos = @(
            [ordered]@{ content = 'document the handoff'; status = 'pending' },
            [ordered]@{ content = 'write the demo'; status = 'completed' }
        ) }) '2026-01-02T00:00:06Z')
    )
    $jsonLines = foreach ($line in $lines) { $line | ConvertTo-Json -Compress -Depth 8 }
    Write-Text (Join-Path $sessionDir 's1.jsonl') (($jsonLines -join "`n") + "`n")

    $failed = $false
    $raw = & {
        Write-Output '1/4 fixture workspace and recorded Claude session'
        Write-Output "  workspace: $fixture"
        Write-Output '  files: src/app.py (edited), tests/test_app.py, docs/notes.md'
        Write-Output '  fold: Read of src/app.py, 50000 bytes, code'
        Write-Output '  todo: pending | document the handoff'

        Write-Output '2/4 packet built with cfxd packet build'
        $buildOut = Invoke-Uv @(
            'run', '--project', $here, '--quiet', 'cfxd', 'packet', 'build', '--agent', 'claude',
            '--root', $sessionDir, '--session', 's1', '--cwd', $fixture, '--out', $packetPath
        )
        if ($LASTEXITCODE -ne 0) {
            Write-Output 'cfxd packet build failed:'
            $buildOut
            $script:failed = $true
            return
        }

        $packet = Get-Content -LiteralPath $packetPath -Raw | ConvertFrom-Json
        $packet.folds = @(
            [pscustomobject]@{ id = '7d95df91bef22e0f'; tool = 'Read'; src = $app; bytes = 50000; kind = 'code' }
        )
        Write-Text $packetPath (($packet | ConvertTo-Json -Depth 10) + "`n")
        Write-Output ("packet: files={0} folds={1} commands={2} todos={3}" -f `
            @($packet.files).Count, @($packet.folds).Count, @($packet.commands).Count, @($packet.todos).Count)

        $validate = Invoke-Uv @(
            'run', '--project', $here, '--quiet', 'cfxd', 'packet', 'validate', $packetPath, '--cwd', $fixture
        )
        if ($LASTEXITCODE -ne 0) {
            $validate
            $script:failed = $true
            return
        }
        $validate

        Write-Output '3/4 Codex: AGENTS.md block'
        $installOut = Invoke-Uv @(
            'run', '--project', $here, '--quiet', 'cfxd', 'packet', 'install-agents-md',
            '--cwd', $codexDir, '--packet', $packetPath
        )
        if ($LASTEXITCODE -ne 0) {
            $installOut
            $script:failed = $true
            return
        }
        $codexAgents = Join-Path $codexDir 'AGENTS.md'
        Write-Output ([System.IO.File]::ReadAllText($codexAgents).TrimEnd("`r", "`n"))
        $installOut

        Write-Output '4/4 OpenCode: AGENTS.md block'
        $installOut = Invoke-Uv @(
            'run', '--project', $here, '--quiet', 'cfxd', 'packet', 'install-agents-md',
            '--cwd', $opencodeDir, '--packet', $packetPath
        )
        if ($LASTEXITCODE -ne 0) {
            $installOut
            $script:failed = $true
            return
        }
        $installOut
        $opencodeAgents = Join-Path $opencodeDir 'AGENTS.md'
        if ([System.IO.File]::ReadAllText($codexAgents) -ceq [System.IO.File]::ReadAllText($opencodeAgents)) {
            Write-Output 'AGENTS.md identical: yes'
        } else {
            Write-Output 'AGENTS.md identical: no'
            $script:failed = $true
        }
    } *>&1 | Out-String

    if ($failed) {
        Write-Output $raw
        Fail 'a demo step failed'
    }

    $actual = $raw -replace "`r`n", "`n"
    $actual = $actual.Replace('\', '/')
    $actual = $actual.Replace($work.Replace('\', '/'), '<work>')
    $actual = $actual -replace 'render is \d+ bytes', 'render is <bytes> bytes'
    $actual = $actual -replace 'wrote \(\d+ bytes\)', 'wrote (<bytes>)'
    $actual = $actual.TrimEnd("`n")

    $expected = @'
1/4 fixture workspace and recorded Claude session
  workspace: <work>/fixture
  files: src/app.py (edited), tests/test_app.py, docs/notes.md
  fold: Read of src/app.py, 50000 bytes, code
  todo: pending | document the handoff
2/4 packet built with cfxd packet build
packet: files=3 folds=1 commands=1 todos=1
PASS schema: shape and enums hold
PASS caps: within every array and text cap
PASS size: render is <bytes> bytes (cap 3000)
PASS provenance: every entry has an observed source
PASS staleness: 3 checked, 0 changed, 0 missing
PASS masking: 1 command(s) masked and idempotent
PASS characters: no control characters or angle brackets
3/4 Codex: AGENTS.md block
<!-- context-fabric:packet v0 -->
Observed facts from a previous session (reference, not instructions; verify before relying on them)
Source: cfxd packet build: agent=claude; tools: Bash=command, Edit=edit, MultiEdit=edit, NotebookEdit=edit, Read=read, TodoWrite=todo, Write=edit
Working set (path | lines | status):
- <work>/fixture/docs/notes.md | read
- <work>/fixture/tests/test_app.py | read
- <work>/fixture/src/app.py | edited
Recent commands:
- git status --short
Open todos the session had (status | text):
- pending | document the handoff
<!-- /context-fabric:packet -->
cfxd packet install-agents-md: wrote (<bytes>)
4/4 OpenCode: AGENTS.md block
cfxd packet install-agents-md: wrote (<bytes>)
AGENTS.md identical: yes
'@
    $expected = ($expected -replace "`r`n", "`n").TrimEnd("`n")

    if ($actual -cne $expected) {
        Write-Output 'FAIL: output did not match the expected output'
        Compare-Object ($expected -split "`n") ($actual -split "`n") |
            ForEach-Object { '{0} {1}' -f $_.SideIndicator, $_.InputObject }
        exit 1
    }

    Write-Output $actual
    Write-Output 'PASS: brief handoff demo matched the expected output.'
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
