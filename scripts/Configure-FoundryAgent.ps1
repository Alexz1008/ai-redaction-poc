#Requires -Version 7.0

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^https://[^/]+\.services\.ai\.azure\.com/api/projects/[^/?#]+/?$')]
    [string]$ProjectEndpoint,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$AgentName,

    [switch]$ReplaceTools
)

$ErrorActionPreference = 'Stop'
$endpoint = $ProjectEndpoint.TrimEnd('/')
$agentPath = [Uri]::EscapeDataString($AgentName)
$apiVersion = '2025-11-15-preview'
$versionsUrl = "$endpoint/agents/$agentPath/versions"
$resource = 'https://ai.azure.com'

$result = & az rest --method GET --url $versionsUrl `
    --url-parameters "api-version=$apiVersion" 'limit=1' 'order=desc' `
    --resource $resource --output json --only-show-errors
if ($LASTEXITCODE -ne 0) {
    throw 'Could not read the existing agent. Check its endpoint, name, sign-in, and permissions.'
}
$versions = ($result | ConvertFrom-Json -AsHashtable).data
if (-not $versions -or $versions.Count -eq 0) {
    throw 'The existing agent has no published version. Create and save it in Foundry first.'
}

$current = $versions[0]
$definition = $current.definition
if ($definition.kind -ne 'prompt') {
    throw 'The existing agent must be a prompt agent.'
}
$before = $definition | ConvertTo-Json -Depth 100 -Compress
$tools = $definition.tools
if ($tools.Count -gt 0 -and ($tools.Count -ne 1 -or $tools[0].type -ne 'code_interpreter')) {
    if (-not $ReplaceTools) {
        throw 'Other tools are configured. Rerun with -ReplaceTools only if replacing them with native Code Interpreter is intended.'
    }
    $tools = @()
}

$tool = if ($tools.Count -eq 1) { $tools[0] } else { @{ type = 'code_interpreter' } }
if ($tool.container -isnot [System.Collections.IDictionary]) {
    $tool.container = @{}
}
$tool.container.type = 'auto'
$tool.container.file_ids = @('{{analysis_file_id}}')
$definition.tools = @($tool)

if ($null -eq $definition.structured_inputs) {
    $definition.structured_inputs = @{}
}
if ($null -eq $definition.structured_inputs.analysis_file_id) {
    $definition.structured_inputs.analysis_file_id = @{}
}
$binding = $definition.structured_inputs.analysis_file_id
if (-not $binding.Contains('description')) {
    $binding.description = 'Original file uploaded for this request.'
}
$binding.required = $true
$binding.schema = @{ type = 'string' }

$after = $definition | ConvertTo-Json -Depth 100 -Compress
if ($before -ceq $after) {
    Write-Output "Already configured: existing agent '$AgentName', version $($current.version). No new version created."
    return
}

$bodyPath = [IO.Path]::GetTempFileName()
try {
    $body = @{ definition = $definition } | ConvertTo-Json -Depth 100
    [IO.File]::WriteAllText($bodyPath, $body, [Text.UTF8Encoding]::new($false))
    $published = & az rest --method POST --url "${versionsUrl}?api-version=$apiVersion" `
        --resource $resource --headers 'Content-Type=application/json' --body "@$bodyPath" `
        --query '{name:name,version:version}' --output json --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw 'Publishing the agent version failed. Check the error above; no successful setup is being reported.'
    }
    $version = $published | ConvertFrom-Json -AsHashtable
    Write-Output "Configured: existing agent '$AgentName', version $($version.version)."
    Write-Output 'Instructions and model preserved. Previous versions retained. No separate agent created.'
}
finally {
    Remove-Item -LiteralPath $bodyPath -Force
}
