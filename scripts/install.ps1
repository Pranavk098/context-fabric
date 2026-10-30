# Context Fabric alpha installer (Windows PowerShell). Documented one-liner:
#   irm https://raw.githubusercontent.com/Pranavk098/context-fabric/main/scripts/install.ps1 | iex
# To pass -DryRun, save the file and run: powershell -File install.ps1 -DryRun
# Checks prerequisites, runs `cfxd doctor`, adds the plugin. Never needs admin rights.
# Safe to re-run: an already-installed plugin is left alone.
param([switch]$DryRun)

$Repo = 'Pranavk098/context-fabric'
$Cfxd = "uvx --from git+https://github.com/$Repo cfxd"

function Say($m) { Write-Host "[context-fabric] $m" }
function Invoke-Step([string]$exe, [string[]]$a) {
  if ($DryRun) { Say "dry-run: would run: $exe $($a -join ' ')"; return $true }
  Say "running: $exe $($a -join ' ')"
  & $exe @a
  return ($LASTEXITCODE -eq 0)
}

if ($DryRun) { Say 'dry-run: nothing will be changed' }

$missing = $false
if (Get-Command uv -ErrorAction SilentlyContinue) { Say 'found uv' } else {
  Write-Host '[context-fabric] missing: uv (install: https://docs.astral.sh/uv/getting-started/installation/)'; $missing = $true }
if (Get-Command claude -ErrorAction SilentlyContinue) { Say 'found claude' } else {
  Write-Host '[context-fabric] missing: claude (Claude Code: https://docs.claude.com/en/docs/claude-code)'; $missing = $true }
if (-not (Get-Command sh -ErrorAction SilentlyContinue)) {
  Write-Host '[context-fabric] missing: sh from Git for Windows (the plugin hooks need it): https://git-scm.com/download/win'; $missing = $true }
if ($missing) {
  if ($DryRun) { Say 'dry-run: would stop here until the prerequisites above are installed' }
  else { Write-Error 'prerequisites missing'; return }
}

Say "step 1/3: cfxd doctor (informational; 'not running' is normal before the first session)"
if ($DryRun) { Say "dry-run: would run: $Cfxd doctor" }
else {
  uvx --from "git+https://github.com/$Repo" cfxd doctor
  if ($LASTEXITCODE -ne 0) { Say 'doctor reported a problem; continuing, see docs/INSTALL-ALPHA.md section 7' }
}

Say 'step 2/3: add the marketplace and install the plugin'
$installed = $false
if (-not $DryRun) { $installed = [bool]((claude plugin list 2>$null) -match 'context-fabric@context-fabric') }
if ($installed) { Say 'plugin already installed; nothing to do' } else {
  if (-not (Invoke-Step 'claude' @('plugin', 'marketplace', 'add', $Repo))) { Say 'marketplace may already be added; continuing' }
  if (-not (Invoke-Step 'claude' @('plugin', 'install', 'context-fabric@context-fabric'))) { Write-Error 'plugin install failed'; return }
}

Say 'step 3/3: next steps'
Write-Host @"
Restart Claude Code; the first session starts the local helper.
Optional and EXPERIMENTAL (effect unproven): turn on Brief for sub-agent handoffs:
  $Cfxd set brief on
  $Cfxd set brief_v1 on
After a session that used sub-agents, see the receipts:
  $Cfxd brief
"@
