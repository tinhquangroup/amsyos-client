# text_stats: a contract 1 service in PowerShell (sdk/CONTRACT.md §3) for AMSYOS Node.
# It counts a text file and, for the isolation check, says what this job can do in each
# folder of `probe`: list it, make a file there; never what is in them.
$ErrorActionPreference = 'Stop'
$params = (amsyos-job params) | ConvertFrom-Json
$top = 5
if ($params.top) { $top = [int]$params.top }
$path = amsyos-job input text
if (-not $path) { amsyos-job fail "text: give a text file (an asset:// reference)"; exit 1 }
$text = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
$words = @([regex]::Matches($text.ToLowerInvariant(), "[\p{L}\p{N}']+") | ForEach-Object { $_.Value })
$most = @($words | Group-Object | Sort-Object -Property @{Expression = 'Count'; Descending = $true}, Name |
    Select-Object -First $top | ForEach-Object { [ordered]@{ word = $_.Name; count = $_.Count } })
# For each folder: "denied" (cannot list it), "readable", or "readable, writable" (a file
# could be made there, and was removed at once).
function Probe([string]$p) {
    $seen = 'denied'
    try { Get-ChildItem -LiteralPath $p -Force -ErrorAction Stop | Out-Null; $seen = 'readable' } catch {}
    $file = Join-Path $p ('.amsyos-probe-' + [guid]::NewGuid().ToString('N'))
    try {
        [System.IO.File]::WriteAllText($file, 'x')
        Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
        if ($seen -eq 'readable') { $seen = 'readable, writable' } else { $seen = 'writable' }
    } catch {}
    return $seen
}
$isolation = [ordered]@{ own_folder = (Probe $env:AMSYOS_JOB) }
if ($params.probe) {
    foreach ($p in ($params.probe -split ',')) {
        $p = $p.Trim()
        if ($p) { $isolation[$p] = Probe $p }
    }
}
$report = [ordered]@{
    lines = ($text -split "`r?`n" | Where-Object { $_ -ne '' }).Count
    words = $words.Count
    characters = $text.Length
    most_frequent = $most
    ran_as = [Environment]::UserName
    machine = $env:COMPUTERNAME
    # PowerShell's location and the process's own folder: both the service's folder.
    cwd = (Get-Location).Path
    process_cwd = [Environment]::CurrentDirectory
    isolation = $isolation
}
$out = amsyos-job output report .json
$json = $report | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($out, $json, (New-Object System.Text.UTF8Encoding $false))
