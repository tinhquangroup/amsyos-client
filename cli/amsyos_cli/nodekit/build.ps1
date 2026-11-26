# A service's environment on an organisation's own Windows node, made by `amsyos deploy`
# (where: node) through node.exec, as the node's own account:
#
#   mkdir src && tar -xf in\file1.zip -C src && powershell -NoProfile -NonInteractive
#     -ExecutionPolicy Bypass -File src\_node\build.ps1 in\file1.zip src
#
# src\_node\build.json says what goes in, from the card: the Python version (the code's
# .python-version, else 3.12), the pip packages, PyTorch's CPU build when the code imports it,
# the card's `models` (public, pinned) and its `setup` (a cmd line). uv makes it, under the
# build home: AMSYOS_BUILD_HOME of the node's account, else
# %ProgramData%\AMSYOS\Node\builds\<organisation> (one home per organisation, never shared):
#
#   envs\<key>                 one virtual environment per hash of what is in it: a new
#                              version with the same packages takes it as it is
#   images\<image>@<version>   the code and what `setup` made there; its jobs run in it,
#                              through _node\run.cmd, which reads its _node\where.env
#   python\                    the Pythons uv installed (UV_PYTHON_INSTALL_DIR)
#   cache\                     uv's cache (UV_CACHE_DIR)
#   hf\                        Hugging Face's cache of the models (jobs read it offline)
#   fetched\                   what amsyos-fetch downloaded, once a machine
#   tools\uv-<version>\uv.exe  uv, pinned, checked by its sha256
#   tmp\<process>              the build's temporary files (TEMP), removed at its end
#
# TODO(review): isolation - the home's ACL is replaced once: SYSTEM, Administrators and the
# node's user full, the job account amsyos-job read and run, never write, so that nothing a
# job can write is ever run by the node's account; amsyos-job may pass through the default
# home's parent `builds`. Its jobs then run as amsyos-job.
#
# It ends with out\build.json, which deploy reads: {"ok": true, "image_dir", ...} or
# {"ok": false, "error", "hint"}; its log is what it printed. It puts no token or key here.
# Windows PowerShell 5.1, ASCII only. $ErrorActionPreference stays 'Continue' (5.1 with 'Stop'
# fails on any line a native program writes to stderr) and each program's exit code is checked.
param([string]$Package, [string]$Source)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$UvVersion = '0.13.8'
$UvUrl = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
$UvSha256 = '529e78a9cec6cd7e3e014f7ecd8bda0c113969d4ed0f2efeedb85c30943325b8'
$UvMost = 64MB
$PlainName = '^[A-Za-z0-9][A-Za-z0-9._-]*$'
$Job = $env:AMSYOS_JOB
if (-not $Job) { $Job = (Get-Location).Path }
$Utf8 = New-Object System.Text.UTF8Encoding $false
$script:Told = $null
$script:Temp = $null
$script:Refused = $null

function Write-Log([string]$Message) {
    [Console]::Out.WriteLine('[build ' + (Get-Date -Format 'HH:mm:ss') + '] ' + $Message)
    [Console]::Out.Flush()
}

# What stops the build: its code and a hint, said in out\build.json.
function Stop-Build([string]$Code, [string]$Hint) {
    $script:Refused = [ordered]@{ ok = $false; error = $Code; hint = $Hint }
    throw $Hint
}

function Set-Progress([int]$Percent) {
    try { [IO.File]::WriteAllText((Join-Path $Job 'progress'), [string]$Percent) } catch { }
}

# One argument as a Windows program reads its command line (the MSVC rules).
function ConvertTo-Argument([string]$Text) {
    if ($Text.Length -gt 0 -and $Text -notmatch '[\s"]') { return $Text }
    $out = New-Object System.Text.StringBuilder
    [void]$out.Append('"')
    $slashes = 0
    foreach ($c in $Text.ToCharArray()) {
        if ($c -eq [char]'\') { $slashes++; continue }
        if ($c -eq [char]'"') { [void]$out.Append('\' * (2 * $slashes + 1)); [void]$out.Append('"') }
        else { [void]$out.Append('\' * $slashes); [void]$out.Append($c) }
        $slashes = 0
    }
    [void]$out.Append('\' * (2 * $slashes))
    [void]$out.Append('"')
    return $out.ToString()
}

# A program, its output going straight to this build's log (the handles it inherits); its
# exit code, or -1 when it could not start.
function Invoke-Native([string]$Exe, [string]$Arguments, [string]$Directory) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = $Arguments
    $psi.UseShellExecute = $false
    if ($Directory) { $psi.WorkingDirectory = $Directory }
    try { $p = [System.Diagnostics.Process]::Start($psi) }
    catch { Write-Log ('cannot start ' + $Exe + ': ' + $_.Exception.Message); return -1 }
    $p.WaitForExit()
    return $p.ExitCode
}

function Invoke-Step([string]$Step, [string]$Hint, [string]$Exe, [string[]]$Arguments, [string]$Directory) {
    $line = (@($Arguments) | ForEach-Object { ConvertTo-Argument $_ }) -join ' '
    $said = '$ ' + (Split-Path -Leaf $Exe) + ' ' + $line
    if ($said.Length -gt 400) { $said = $said.Substring(0, 400) }
    Write-Log $said
    $code = Invoke-Native $Exe $line $Directory
    if ($code -ne 0) { Stop-Build 'BUILD_FAILED' ('{0}: {1} (exit code {2})' -f $Step, $Hint, $code) }
}

function Get-List($Value) {
    return @(@($Value) | Where-Object { $null -ne $_ -and ([string]$_).Trim() -ne '' } | ForEach-Object { ([string]$_).Trim() })
}

# `$Inner` is `$Outer` or lies inside it (no case on Windows).
function Test-Inside([string]$Inner, [string]$Outer) {
    $i = $Inner.TrimEnd('\').ToLowerInvariant()
    $o = $Outer.TrimEnd('\').ToLowerInvariant()
    return ($i -eq $o) -or $i.StartsWith($o + '\')
}

# TODO(review): isolation - which folder the organisation's builds go in.
function Get-BuildHome($Asked) {
    if ($env:AMSYOS_BUILD_HOME) { $folder = $env:AMSYOS_BUILD_HOME }
    else {
        $org = [string]$Asked.org
        if ($org -notmatch '^[A-Za-z0-9_-]{1,64}$') {
            Stop-Build 'INVALID_PARAMS' 'build.json: org is no organisation id (deploy again with the newest amsyos)'
        }
        $folder = Join-Path $env:ProgramData ('AMSYOS\Node\builds\' + $org)
    }
    $folder = [IO.Path]::GetFullPath($folder).TrimEnd('\')
    if ($folder -notmatch '^[A-Za-z]:\\[A-Za-z0-9 _.\\-]+$') {
        Stop-Build 'UNSUPPORTED' ("the build home $folder is not a folder on a drive named in ASCII letters, digits, " +
            "spaces and _ . - only (cmd reads it): set AMSYOS_BUILD_HOME of the node's account to such a folder")
    }
    $held = @(@('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData', 'USERPROFILE') |
        ForEach-Object { [Environment]::GetEnvironmentVariable($_) } | Where-Object { $_ })
    if ($env:USERPROFILE) { $held += (Split-Path -Parent $env:USERPROFILE) }
    foreach ($s in $held) {
        if (Test-Inside $s $folder) {
            Stop-Build 'UNSUPPORTED' "the build home $folder holds $s, whose ACL deploy would replace: set AMSYOS_BUILD_HOME of the node's account to a folder of its own"
        }
    }
    return $folder
}

# TODO(review): isolation - the home gets a protected ACL, once (a mark says it was done):
# SYSTEM, Administrators and the node's user full, amsyos-job read and run; what is made in it
# inherits that. The default home's parent lets amsyos-job pass through and read its own
# attributes only, as setup does for the folders above a job's.
function Protect-BuildHome([string]$HomeDir) {
    $mark = Join-Path $HomeDir '.acl'
    if (Test-Path -LiteralPath $mark) { return }
    $icacls = Join-Path $env:SystemRoot 'System32\icacls.exe'
    $me = $env:USERDOMAIN + '\' + $env:USERNAME
    $jobAccount = $env:COMPUTERNAME + '\amsyos-job'
    $hint = 'icacls could not protect it: is the job account set up (amsyos-node setup, in an administrator window)?'
    Invoke-Step 'the build home' $hint $icacls @($HomeDir, '/reset', '/Q') ''
    Invoke-Step 'the build home' $hint $icacls @($HomeDir, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F',
        '*S-1-5-32-544:(OI)(CI)F', "${me}:(OI)(CI)F", "${jobAccount}:(OI)(CI)RX", '/Q') ''
    if (-not $env:AMSYOS_BUILD_HOME) {
        Invoke-Step 'the build home' $hint $icacls @((Split-Path -Parent $HomeDir), '/grant', "${jobAccount}:(S,X,RA)", '/Q') ''
    }
    [IO.File]::WriteAllText($mark, "SYSTEM, Administrators and $me full; $jobAccount read and run`r`n", $Utf8)
    Write-Log "the build home $HomeDir`: SYSTEM, Administrators and $me only; $jobAccount may read and run it"
}

function Get-Uv([string]$HomeDir) {
    $dir = Join-Path $HomeDir ('tools\uv-' + $UvVersion)
    $uv = Join-Path $dir 'uv.exe'
    if (Test-Path -LiteralPath $uv) { return $uv }  # checked when it was fetched
    New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null
    $zip = Join-Path $dir 'uv.zip.part'
    $staging = Join-Path $dir 'staging'
    Write-Log "uv: fetching $UvVersion, pinned"
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    try { (New-Object System.Net.WebClient).DownloadFile($UvUrl, $zip) }
    catch { Stop-Build 'BUILD_FAILED' ('uv could not be fetched from GitHub: ' + $_.Exception.Message) }
    if ((Get-Item -LiteralPath $zip).Length -gt $UvMost) {
        Remove-Item -LiteralPath $zip -Force
        Stop-Build 'BUILD_FAILED' "uv's download is larger than it should be"
    }
    $hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne $UvSha256) {
        Remove-Item -LiteralPath $zip -Force
        Stop-Build 'BUILD_FAILED' "uv's download did not match its pinned sha256"
    }
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction Stop }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($zip, $staging)
    Move-Item -LiteralPath (Join-Path $staging 'uv.exe') -Destination $uv -ErrorAction Stop
    Remove-Item -LiteralPath $staging -Recurse -Force
    Remove-Item -LiteralPath $zip -Force
    return $uv
}

function Invoke-Build {
    $began = Get-Date
    $asked = [IO.File]::ReadAllText((Join-Path $Source '_node\build.json')) | ConvertFrom-Json
    foreach ($key in 'image', 'version', 'env') {
        if ([string]$asked.$key -notmatch $PlainName) { Stop-Build 'INVALID_PARAMS' "build.json: $key is no plain name" }
    }
    $python = [string]$asked.python
    if ($python -notmatch '^3\.\d{1,2}(\.\d{1,2})?$') {
        Stop-Build 'INVALID_PARAMS' "build.json: python $python is no version such as 3.12 (the code's .python-version)"
    }
    $requirements = [string]$asked.requirements
    if ($requirements -and $requirements -notmatch $PlainName) {
        Stop-Build 'INVALID_PARAMS' 'build.json: requirements is no file of the top folder'
    }
    $base = Get-BuildHome $asked
    New-Item -ItemType Directory -Force -Path $base -ErrorAction Stop | Out-Null
    Protect-BuildHome $base
    foreach ($sub in 'envs', 'images', 'python', 'cache', 'hf', 'fetched', 'tools') {
        New-Item -ItemType Directory -Force -Path (Join-Path $base $sub) -ErrorAction Stop | Out-Null
    }
    $imageName = [string]$asked.image + '@' + [string]$asked.version
    Write-Log ("image $imageName, environment $($asked.env) (Python $python), under $base")
    Set-Progress 5

    # TODO(review): isolation - temporary files in the home too: a file moved within a drive
    # keeps its ACL, and the job's own folder lets amsyos-job write.
    $script:Temp = Join-Path $base ('tmp\' + $PID)
    New-Item -ItemType Directory -Force -Path $script:Temp -ErrorAction Stop | Out-Null
    $env:TEMP = $script:Temp
    $env:TMP = $script:Temp
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $base 'python'
    $env:UV_CACHE_DIR = Join-Path $base 'cache'
    $env:UV_NO_PROGRESS = '1'
    $uv = Get-Uv $base
    $envDir = Join-Path $base ('envs\' + [string]$asked.env)
    $py = Join-Path $envDir 'Scripts\python.exe'
    $reused = Test-Path -LiteralPath (Join-Path $envDir '.ready')
    if ($reused) { Write-Log "environment $($asked.env): made before, taken as it is" }
    else {
        if (Test-Path -LiteralPath $envDir) { Remove-Item -LiteralPath $envDir -Recurse -Force -ErrorAction Stop }
        Set-Progress 10
        Invoke-Step 'Python' "uv could not install Python $python`: is it a version uv has?" $uv @('python', 'install', '--no-bin', '--no-registry', $python) $base
        Invoke-Step 'environment' "uv could not make a virtual environment of Python $python" $uv @('venv', '--seed', '--managed-python', '--python', $python, $envDir) $base
        Set-Progress 30
        $pip = @('pip', 'install', '--python', $py, '--compile-bytecode')
        if ($asked.torch) {
            $torch = @(Get-List $asked.torch.packages)
            Invoke-Step 'PyTorch' 'uv could not install PyTorch from its index' $uv ($pip + @('--default-index', [string]$asked.torch.index) + $torch) $base
        }
        $packages = @(Get-List $asked.packages)
        if ($packages.Count -gt 0) {
            $listed = Join-Path $envDir 'requirements.txt'
            [IO.File]::WriteAllText($listed, (($packages -join "`n") + "`n"), $Utf8)
            Invoke-Step 'packages' 'uv could not install them; its words above say which (a version that does not exist, or one with no wheel for Windows that needs a compiler)' $uv ($pip + @('-r', $listed)) $base
        }
        if ($requirements) {
            Invoke-Step 'packages' "uv could not install $requirements; its words above say which" $uv ($pip + @('-r', (Join-Path $Source $requirements))) $base
        }
        [IO.File]::WriteAllText((Join-Path $envDir '.ready'), ('{"made": "' + (Get-Date -Format 's') + '"}'), $Utf8)
    }
    if (@(Get-List $asked.models).Count -gt 0) {  # also in an environment made before without them
        Invoke-Step 'models' 'uv could not install huggingface_hub' $uv @('pip', 'install', '--python', $py, '--compile-bytecode', 'huggingface_hub>=0.30') $base
    }
    Set-Progress 60
    if ($asked.needs_module) {
        $code = Invoke-Native $py '-c "import amsyos"' $Source
        if ($code -ne 0) {
            Stop-Build 'NODE_TOO_OLD' "a job here finds no amsyos module, which AMSYOS Node $($asked.node_min) and later gives every job"
        }
    }

    $env:HF_HOME = Join-Path $base 'hf'
    $env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
    $fetchModel = "import sys; from huggingface_hub import snapshot_download; repo, _, rest = sys.argv[1].partition('@'); " +
        "rev, _, only = rest.partition(':'); path = snapshot_download(repo, revision=rev or None, " +
        "allow_patterns=only.split(',') if only else None); print('model', repo, rev[:8], 'at', path, flush=True)"
    foreach ($model in @(Get-List $asked.models)) {
        Invoke-Step 'models' "$model could not be fetched from Hugging Face (public, at its pinned commit)" $py @('-c', $fetchModel, $model) $base
    }
    Set-Progress 75

    # The image: the package unpacked in the home (where amsyos-job cannot write), setup run
    # there, then put in place under its name.
    $images = Join-Path $base 'images'
    $image = Join-Path $images $imageName
    $stage = Join-Path $images ('.new-' + $imageName)
    $old = Join-Path $images ('.old-' + $imageName)
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction Stop }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory([IO.Path]::GetFullPath($Package), $stage)
    $setup = ([string]$asked.setup).Trim()
    if ($setup) {
        $env:PATH = (Join-Path $stage '_node\bin') + ';' + (Join-Path $envDir 'Scripts') + ';' + $env:PATH
        $env:VIRTUAL_ENV = $envDir
        $env:AMSYOS_FETCH_STORE = Join-Path $base 'fetched'
        $env:PYTHONDONTWRITEBYTECODE = '1'
        Write-Log ('$ setup: ' + $setup)
        $code = Invoke-Native (Join-Path $env:SystemRoot 'System32\cmd.exe') ('/d /s /c "' + $setup + '"') $stage
        if ($code -ne 0) { Stop-Build 'BUILD_FAILED' "setup: the card's setup failed; its words above say why (exit code $code)" }
        # TODO(review): isolation - what setup moved in keeps the ACL it had: all of it takes
        # the home's again.
        Invoke-Step 'setup' 'icacls could not give what setup made the build home ACL' (Join-Path $env:SystemRoot 'System32\icacls.exe') @($stage, '/reset', '/T', '/Q') ''
    }
    Set-Progress 90
    $where = 'ENV_DIR=' + $envDir + "`r`n" + 'HF_DIR=' + (Join-Path $base 'hf') + "`r`n" + 'FETCHED=' + (Join-Path $base 'fetched') + "`r`n"
    [IO.File]::WriteAllText((Join-Path $stage '_node\where.env'), $where, $Utf8)
    try {
        if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction Stop }
        if (Test-Path -LiteralPath $image) { Rename-Item -LiteralPath $image -NewName ('.old-' + $imageName) -ErrorAction Stop }
        Rename-Item -LiteralPath $stage -NewName $imageName -ErrorAction Stop
    } catch {
        Stop-Build 'BUILD_FAILED' ("$image could not be put in place (a job running in it holds it?): " + $_.Exception.Message)
    }
    if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue }
    $free = (New-Object System.IO.DriveInfo ($base.Substring(0, 1))).AvailableFreeSpace
    $seconds = [int]((Get-Date) - $began).TotalSeconds
    $script:Told = [ordered]@{
        ok = $true; os = 'windows'; image_dir = $image; closed_at = $null; env = [string]$asked.env
        env_reused = [bool]$reused; base = $base; python = $python; node_version = $null
        seconds = $seconds; disk_free_gb = [math]::Round($free / 1e9, 1)
    }
    Write-Log ("image ready in $seconds s: $image; " + [math]::Round($free / 1e9, 1) + ' GB free')
}

try {
    if (-not $Package -or -not $Source) { Stop-Build 'INVALID_PARAMS' 'usage: build.ps1 <package.zip> <its unpacked folder>' }
    $Source = [IO.Path]::GetFullPath($Source)
    Invoke-Build | Out-Null
} catch {
    if ($script:Refused) { $script:Told = $script:Refused }
    else {
        Write-Log ($_ | Out-String)
        $script:Told = [ordered]@{ ok = $false; error = 'BUILD_FAILED'; hint = ($_.Exception.GetType().Name + ': ' + $_.Exception.Message) }
    }
} finally {
    if ($script:Temp) { Remove-Item -LiteralPath $script:Temp -Recurse -Force -ErrorAction SilentlyContinue }
}
if (-not $script:Told.ok) { Write-Log ('FAILED: ' + $script:Told.hint) }
$out = Join-Path $Job 'out'
New-Item -ItemType Directory -Force -Path $out | Out-Null
[IO.File]::WriteAllText((Join-Path $out 'build.json'), ($script:Told | ConvertTo-Json -Compress -Depth 4), $Utf8)
exit 0
