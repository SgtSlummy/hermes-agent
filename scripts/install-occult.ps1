# Signed GitHub-first installer for the Tarot Router local public release.
#
# Download this script from the matching Hermes GitHub release before running
# it. Piping the script directly to Invoke-Expression is intentionally rejected
# because the running file is compared with the signed release copy.

[CmdletBinding()]
param(
    [string]$Version = "1.0.5",
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Occult"),
    [switch]$InitializeLocal,
    [switch]$SkipCouncil,
    [switch]$VerifyOnly,
    [string]$Model = "qwen2.5:3b"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$HermesRepository = "SgtSlummy/hermes-agent"
$CouncilRepository = "SgtSlummy/agents-council"
$BootstrapUvVersion = "0.11.28"
$BootstrapUvWindowsAsset = "uv-x86_64-pc-windows-msvc.zip"
$BootstrapUvWindowsSha256 = "0a23463216d09c6a72ff80ef5dc5a795f07dc1575cb84d24596c2f124a441b7b"
$PinnedSigstoreVersion = "4.5.0"
$SigstoreRequirementsAsset = "occult-sigstore-requirements.lock"
$SigstoreRequirementsSha256 = "bcb33aef02d914b025ad423450250d9ffaf22a727d600d58d4cce5d746836b04"
$ExpectedIssuer = "https://token.actions.githubusercontent.com"

function Write-Step {
    param([string]$Message)
    Write-Host "[Occult] $Message" -ForegroundColor Cyan
}

function Fail {
    param([string]$Message)
    throw "Occult installation stopped safely: $Message"
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        Fail "$FailureMessage (exit code $LASTEXITCODE)"
    }
}

function Download-Asset {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $lastError = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing -TimeoutSec 60
            $lastError = $null
            break
        } catch {
            $lastError = $_.Exception.Message
            Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
            if ($attempt -lt 3) {
                Start-Sleep -Seconds $attempt
            }
        }
    }
    if ($null -ne $lastError) {
        Fail "could not download $Label after 3 attempts. Check the version, network connection, and GitHub status. $lastError"
    }
    if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
        Fail "$Label download did not create a file"
    }
}

function Get-SafeAssetName {
    param([Parameter(Mandatory = $true)][string]$Name)
    if (
        $Name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$' -or
        $Name.Contains("..") -or
        $Name.Contains("/") -or
        $Name.Contains("\")
    ) {
        Fail "release metadata contains an unsafe asset name"
    }
    return $Name
}

function Assert-SafeTarArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Tar,
        [Parameter(Mandatory = $true)][string]$Archive
    )
    $entries = & $Tar -tzf $Archive
    if ($LASTEXITCODE -ne 0) {
        Fail "Council archive could not be inspected safely"
    }
    foreach ($entry in $entries) {
        $normalized = ([string]$entry).Trim().Replace("\", "/")
        if (-not $normalized) {
            continue
        }
        $segments = @(
            $normalized.Split(
                "/",
                [StringSplitOptions]::RemoveEmptyEntries
            )
        )
        if (
            $normalized.StartsWith("/") -or
            $normalized -match '^[A-Za-z]:' -or
            $segments -contains ".."
        ) {
            Fail "Council archive contains an unsafe path"
        }
    }
}

function Get-ExpectedHash {
    param(
        [Parameter(Mandatory = $true)][string]$ChecksumFile,
        [Parameter(Mandatory = $true)][string]$AssetName
    )
    foreach ($line in Get-Content -LiteralPath $ChecksumFile) {
        if ($line -match '^([0-9a-fA-F]{64})\s+\*?(.+)$') {
            $listedName = $Matches[2].Trim()
            if ($listedName.StartsWith("./")) {
                $listedName = $listedName.Substring(2)
            }
            if ($listedName -eq $AssetName) {
                return $Matches[1].ToLowerInvariant()
            }
        }
    }
    Fail "signed checksum manifest does not list $AssetName"
}

function Assert-FileHash {
    param(
        [Parameter(Mandatory = $true)][string]$ChecksumFile,
        [Parameter(Mandatory = $true)][string]$AssetName,
        [Parameter(Mandatory = $true)][string]$FilePath
    )
    $expected = Get-ExpectedHash -ChecksumFile $ChecksumFile -AssetName $AssetName
    $actual = (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        Fail "SHA-256 verification failed for $AssetName"
    }
    return $actual
}

function Resolve-Uv {
    param([Parameter(Mandatory = $true)][string]$TemporaryRoot)
    Write-Step "Bootstrapping verified uv $BootstrapUvVersion in the temporary verifier directory"
    $bootstrapArchive = Join-Path $TemporaryRoot $BootstrapUvWindowsAsset
    Download-Asset `
        -Url "https://github.com/astral-sh/uv/releases/download/$BootstrapUvVersion/$BootstrapUvWindowsAsset" `
        -Destination $bootstrapArchive `
        -Label "the pinned uv archive"
    $bootstrapHash = (
        Get-FileHash -LiteralPath $bootstrapArchive -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($bootstrapHash -ne $BootstrapUvWindowsSha256) {
        Fail "SHA-256 verification failed for the pinned uv archive"
    }
    $uvBin = Join-Path $TemporaryRoot "uv-bin"
    Expand-Archive -LiteralPath $bootstrapArchive -DestinationPath $uvBin -Force
    $uv = Get-ChildItem -LiteralPath $uvBin -Filter "uv.exe" -Recurse -File |
        Select-Object -First 1
    if (-not $uv) {
        Fail "the verified uv archive did not contain uv.exe"
    }
    $versionOutput = (& $uv.FullName --version | Out-String).Trim()
    if ($versionOutput -notmatch "^uv $([Regex]::Escape($BootstrapUvVersion))(\s|$)") {
        Fail "the verified uv executable reported an unexpected version"
    }
    return $uv.FullName
}

function New-SigstoreVerifier {
    param(
        [Parameter(Mandatory = $true)][string]$Uv,
        [Parameter(Mandatory = $true)][string]$TemporaryRoot,
        [Parameter(Mandatory = $true)][string]$ReleaseBase
    )
    $requirements = Join-Path $TemporaryRoot $SigstoreRequirementsAsset
    Download-Asset `
        -Url "$ReleaseBase/$SigstoreRequirementsAsset" `
        -Destination $requirements `
        -Label "the pinned Sigstore dependency lock"
    $requirementsHash = (
        Get-FileHash -LiteralPath $requirements -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($requirementsHash -ne $SigstoreRequirementsSha256) {
        Fail "SHA-256 verification failed for the pinned Sigstore dependency lock"
    }
    $verifierVenv = Join-Path $TemporaryRoot "sigstore-verifier"
    Invoke-Checked `
        -Executable $Uv `
        -Arguments @(
            "venv",
            "--no-config",
            "--python", "3.11",
            $verifierVenv
        ) `
        -FailureMessage "Sigstore verifier environment creation failed"
    $verifierPython = Join-Path $verifierVenv "Scripts\python.exe"
    Invoke-Checked `
        -Executable $Uv `
        -Arguments @(
            "pip", "sync",
            "--no-config",
            "--python", $verifierPython,
            "--require-hashes",
            $requirements
        ) `
        -FailureMessage "Sigstore verifier dependency installation failed"
    $sigstore = Join-Path $verifierVenv "Scripts\sigstore.exe"
    if (-not (Test-Path -LiteralPath $sigstore -PathType Leaf)) {
        Fail "the hash-locked verifier did not create sigstore.exe"
    }
    return $sigstore
}

function Assert-SigstoreIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$Sigstore,
        [Parameter(Mandatory = $true)][string]$Subject,
        [Parameter(Mandatory = $true)][string]$Bundle,
        [Parameter(Mandatory = $true)][string]$Identity
    )
    Write-Step "Verifying Sigstore identity for $(Split-Path -Leaf $Subject)"
    Invoke-Checked `
        -Executable $Sigstore `
        -Arguments @(
            "verify", "identity",
            $Subject,
            "--bundle", $Bundle,
            "--offline",
            "--cert-identity", $Identity,
            "--cert-oidc-issuer", $ExpectedIssuer
        ) `
        -FailureMessage "Sigstore identity verification failed"
}

function Add-UserPath {
    param([Parameter(Mandatory = $true)][string]$Directory)
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $segments = @()
    if ($userPath) {
        $segments = @($userPath.Split(";") | Where-Object { $_ })
    }
    if (-not ($segments | Where-Object { $_.TrimEnd("\") -ieq $Directory.TrimEnd("\") })) {
        $updated = (($segments + $Directory) -join ";")
        [Environment]::SetEnvironmentVariable("Path", $updated, "User")
    }
    if (-not (($env:Path.Split(";")) | Where-Object { $_.TrimEnd("\") -ieq $Directory.TrimEnd("\") })) {
        $env:Path = "$Directory;$env:Path"
    }
}

if ($env:OS -ne "Windows_NT") {
    Fail "this entrypoint supports Windows only; use install-occult.sh on Linux or macOS"
}
if (-not $PSCommandPath) {
    Fail "download the script to a file before running it; direct pipe-to-execution is not supported"
}

$normalizedVersion = $Version.Trim()
if ($normalizedVersion.StartsWith("v")) {
    $normalizedVersion = $normalizedVersion.Substring(1)
}
if ($normalizedVersion -notmatch '^\d+\.\d+\.\d+$') {
    Fail "--version must be a semantic version such as 1.0.5"
}
if ([string]::IsNullOrWhiteSpace($Model)) {
    Fail "--model cannot be empty"
}

$architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
if (-not $SkipCouncil -and $architecture -ne "x64") {
    Fail "Windows $architecture is not supported by the bundled Agents Council release; use Windows x64 or -SkipCouncil"
}
$platformKey = "windows-x64"
$resolvedInstallRoot = [IO.Path]::GetFullPath(
    [Environment]::ExpandEnvironmentVariables($InstallRoot)
)
$releaseTag = "v$normalizedVersion"
$hermesReleaseBase = "https://github.com/$HermesRepository/releases/download/$releaseTag"
$temporaryBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$temporaryRoot = Join-Path $temporaryBase ("occult-install-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryRoot -Force | Out-Null

try {
    $uv = Resolve-Uv -TemporaryRoot $temporaryRoot
    $sigstore = New-SigstoreVerifier `
        -Uv $uv `
        -TemporaryRoot $temporaryRoot `
        -ReleaseBase $hermesReleaseBase
    $installChecksums = Join-Path $temporaryRoot "OCCULT-INSTALL-SHA256SUMS.txt"
    $installBundle = "$installChecksums.sigstore.json"
    Download-Asset `
        -Url "$hermesReleaseBase/OCCULT-INSTALL-SHA256SUMS.txt" `
        -Destination $installChecksums `
        -Label "the Hermes install checksum manifest"
    Download-Asset `
        -Url "$hermesReleaseBase/OCCULT-INSTALL-SHA256SUMS.txt.sigstore.json" `
        -Destination $installBundle `
        -Label "the Hermes Sigstore bundle"
    Assert-SigstoreIdentity `
        -Sigstore $sigstore `
        -Subject $installChecksums `
        -Bundle $installBundle `
        -Identity "https://github.com/$HermesRepository/.github/workflows/occult-production-gate.yml@refs/heads/main"

    $manifestPath = Join-Path $temporaryRoot "occult-install-manifest.json"
    Download-Asset `
        -Url "$hermesReleaseBase/occult-install-manifest.json" `
        -Destination $manifestPath `
        -Label "the Occult install manifest"
    $manifestHash = Assert-FileHash `
        -ChecksumFile $installChecksums `
        -AssetName "occult-install-manifest.json" `
        -FilePath $manifestPath
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.schema_version -ne "1.0.0") {
        Fail "unsupported install manifest schema"
    }
    if ($manifest.occult_release_version -ne $normalizedVersion) {
        Fail "the requested version does not match the signed install manifest"
    }
    if ($manifest.uv_version -ne $BootstrapUvVersion) {
        Fail "the signed manifest does not match the pinned uv verifier"
    }
    if ($manifest.sigstore_python_version -ne $PinnedSigstoreVersion) {
        Fail "the signed manifest does not match the pinned Sigstore verifier"
    }
    if (
        $manifest.sigstore_requirements_asset -ne $SigstoreRequirementsAsset -or
        $manifest.sigstore_requirements_sha256 -ne $SigstoreRequirementsSha256
    ) {
        Fail "the signed manifest does not match the hash-locked Sigstore dependency set"
    }

    $signedScript = Join-Path $temporaryRoot "install-occult.ps1"
    Download-Asset `
        -Url "$hermesReleaseBase/install-occult.ps1" `
        -Destination $signedScript `
        -Label "the signed Windows installer"
    $signedScriptHash = Assert-FileHash `
        -ChecksumFile $installChecksums `
        -AssetName "install-occult.ps1" `
        -FilePath $signedScript
    $runningScriptHash = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($runningScriptHash -ne $signedScriptHash) {
        Fail "the running installer does not match the Sigstore-verified release copy"
    }

    $wheelAsset = Get-SafeAssetName ([string]$manifest.hermes_wheel_asset)
    $wheelPath = Join-Path $temporaryRoot $wheelAsset
    Download-Asset `
        -Url "$hermesReleaseBase/$wheelAsset" `
        -Destination $wheelPath `
        -Label "the Hermes wheel"
    $wheelHash = Assert-FileHash `
        -ChecksumFile $installChecksums `
        -AssetName $wheelAsset `
        -FilePath $wheelPath
    $requirementsAsset = Get-SafeAssetName (
        [string]$manifest.hermes_requirements_asset
    )
    $requirementsPath = Join-Path $temporaryRoot $requirementsAsset
    Download-Asset `
        -Url "$hermesReleaseBase/$requirementsAsset" `
        -Destination $requirementsPath `
        -Label "the locked Hermes dependency set"
    $requirementsHash = Assert-FileHash `
        -ChecksumFile $installChecksums `
        -AssetName $requirementsAsset `
        -FilePath $requirementsPath

    $councilArchive = $null
    $councilHash = $null
    $councilChecksums = $null
    if (-not $SkipCouncil) {
        $councilTag = [string]$manifest.council.release_tag
        if ($councilTag -notmatch '^v\d+\.\d+\.\d+$') {
            Fail "the signed install manifest contains an invalid Council release tag"
        }
        $councilAsset = Get-SafeAssetName ([string]$manifest.council.assets.$platformKey)
        $councilBase = "https://github.com/$CouncilRepository/releases/download/$councilTag"
        $councilChecksums = Join-Path $temporaryRoot "RELEASE-SHA256SUMS.txt"
        $councilBundle = "$councilChecksums.sigstore.json"
        $councilArchive = Join-Path $temporaryRoot $councilAsset
        Download-Asset `
            -Url "$councilBase/RELEASE-SHA256SUMS.txt" `
            -Destination $councilChecksums `
            -Label "the Council release checksum manifest"
        Download-Asset `
            -Url "$councilBase/RELEASE-SHA256SUMS.txt.sigstore.json" `
            -Destination $councilBundle `
            -Label "the Council Sigstore bundle"
        Assert-SigstoreIdentity `
            -Sigstore $sigstore `
            -Subject $councilChecksums `
            -Bundle $councilBundle `
            -Identity "https://github.com/$CouncilRepository/.github/workflows/release.yml@refs/tags/$councilTag"
        Download-Asset `
            -Url "$councilBase/$councilAsset" `
            -Destination $councilArchive `
            -Label "the Council $platformKey archive"
        $councilHash = Assert-FileHash `
            -ChecksumFile $councilChecksums `
            -AssetName $councilAsset `
            -FilePath $councilArchive
    }

    if ($VerifyOnly) {
        Write-Step "All requested release assets passed Sigstore and SHA-256 verification"
        return
    }

    $binRoot = Join-Path $resolvedInstallRoot "bin"
    $environmentId = "$normalizedVersion-" + [Guid]::NewGuid().ToString("N")
    $hermesEnvironmentsRoot = Join-Path $resolvedInstallRoot "hermes-environments"
    $hermesVenv = Join-Path $hermesEnvironmentsRoot $environmentId
    New-Item -ItemType Directory -Path $binRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $hermesEnvironmentsRoot -Force | Out-Null
    Write-Step "Installing the verified Hermes wheel and hash-locked dependencies per-user"
    Invoke-Checked `
        -Executable $uv `
        -Arguments @(
            "venv",
            "--no-config",
            "--python", "3.11",
            $hermesVenv
        ) `
        -FailureMessage "Hermes environment creation failed"
    $venvPython = Join-Path $hermesVenv "Scripts\python.exe"
    Invoke-Checked `
        -Executable $uv `
        -Arguments @(
            "pip", "sync",
            "--no-config",
            "--python", $venvPython,
            "--require-hashes",
            $requirementsPath
        ) `
        -FailureMessage "Hermes locked dependency installation failed"
    Invoke-Checked `
        -Executable $uv `
        -Arguments @(
            "pip", "install",
            "--no-config",
            "--python", $venvPython,
            "--no-deps",
            "--no-index",
            $wheelPath
        ) `
        -FailureMessage "Hermes wheel installation failed"

    $venvHermesExecutable = Join-Path $hermesVenv "Scripts\hermes.exe"
    if (-not (Test-Path -LiteralPath $venvHermesExecutable -PathType Leaf)) {
        Fail "Hermes installed without creating hermes.exe"
    }
    $hermesExecutable = Join-Path $binRoot "hermes.exe"
    # Keep .exe as the final suffix so Windows PowerShell recognizes the
    # staged command as an executable before it is atomically activated.
    $hermesStagedExecutable = Join-Path $binRoot "hermes.new-$environmentId.exe"
    Copy-Item `
        -LiteralPath $venvHermesExecutable `
        -Destination $hermesStagedExecutable `
        -Force
    $hermesVersionOutput = (& $hermesStagedExecutable --version | Out-String).Trim()
    if ($hermesVersionOutput -notmatch [Regex]::Escape([string]$manifest.hermes_cli_version)) {
        Fail "Hermes executable version does not match signed release metadata"
    }

    $councilVersionOutput = $null
    $councilEnvironment = $null
    $councilStagedExecutable = $null
    if (-not $SkipCouncil) {
        $councilTag = [string]$manifest.council.release_tag
        $councilEnvironmentsRoot = Join-Path $resolvedInstallRoot "council-environments"
        $councilEnvironment = Join-Path `
            $councilEnvironmentsRoot `
            ("$councilTag-" + [Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $councilEnvironment -Force | Out-Null
        $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
        if (-not $tar) {
            Fail "Windows tar.exe is required to install Agents Council"
        }
        Assert-SafeTarArchive -Tar $tar.Source -Archive $councilArchive
        Invoke-Checked `
            -Executable $tar.Source `
            -Arguments @("-xzf", $councilArchive, "-C", $councilEnvironment) `
            -FailureMessage "Council archive extraction failed"
        $packagedCouncil = Join-Path $councilEnvironment "cli\council.exe"
        if (-not (Test-Path -LiteralPath $packagedCouncil -PathType Leaf)) {
            Fail "the verified Council archive does not contain cli\council.exe"
        }
        $councilStagedExecutable = Join-Path `
            $binRoot `
            ("council.new-" + [Guid]::NewGuid().ToString("N") + ".exe")
        Copy-Item `
            -LiteralPath $packagedCouncil `
            -Destination $councilStagedExecutable `
            -Force
        $councilVersionOutput = (
            & $councilStagedExecutable --version | Out-String
        ).Trim()
        if ($councilVersionOutput -notmatch [Regex]::Escape($councilTag.TrimStart("v"))) {
            Fail "Council executable version does not match signed release metadata"
        }
    }

    if ($InitializeLocal) {
        $ollama = Get-Command ollama -ErrorAction SilentlyContinue
        if (-not $ollama) {
            Fail "Ollama is required for --initialize-local. Install it from https://ollama.com/download and rerun this command"
        }
        Write-Step "Pulling the explicitly requested local model $Model"
        Invoke-Checked `
            -Executable $ollama.Source `
            -Arguments @("pull", $Model) `
            -FailureMessage "Ollama could not pull $Model"
        Write-Step "Explicitly initializing the local Occult profile"
        Invoke-Checked `
            -Executable $hermesStagedExecutable `
            -Arguments @("occult", "init", "--model", $Model) `
            -FailureMessage "hermes occult init failed"
    }

    $stateScript = @'
import json
from hermes_cli import config
raw = config.read_raw_config() or {}
occult = raw.get("occult")
initialized = isinstance(occult, dict) and bool(occult.get("local_model"))
enabled = initialized and occult.get("enabled") is True
print(json.dumps({"initialized": initialized, "enabled": enabled}))
'@
    # Windows PowerShell 5 can strip embedded quotes from native `-c`
    # arguments. Execute an installer-owned temporary file so the state probe
    # reaches Python byte-for-byte.
    $stateScriptPath = Join-Path $temporaryRoot "inspect-occult-state.py"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($stateScriptPath, $stateScript, $utf8NoBom)
    $stateJson = (& $venvPython $stateScriptPath | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        Fail "could not inspect the preserved Occult initialization state"
    }
    $state = $stateJson | ConvertFrom-Json
    $initialized = [bool]$state.initialized
    $enabled = [bool]$state.enabled

    Write-Step "Activating the fully staged local commands"
    Move-Item `
        -LiteralPath $hermesStagedExecutable `
        -Destination $hermesExecutable `
        -Force
    if (-not $SkipCouncil) {
        $councilExecutable = Join-Path $binRoot "council.exe"
        Move-Item `
            -LiteralPath $councilStagedExecutable `
            -Destination $councilExecutable `
            -Force
    }

    Add-UserPath -Directory $binRoot

    $receipt = [ordered]@{
        schema_version = "1.0.0"
        occult_release_version = $normalizedVersion
        hermes_cli_version = [string]$manifest.hermes_cli_version
        hermes_wheel = $wheelAsset
        hermes_wheel_sha256 = $wheelHash
        hermes_requirements = $requirementsAsset
        hermes_requirements_sha256 = $requirementsHash
        sigstore_requirements = $SigstoreRequirementsAsset
        sigstore_requirements_sha256 = $SigstoreRequirementsSha256
        hermes_environment = $environmentId
        install_manifest_sha256 = $manifestHash
        council_release = if ($SkipCouncil) { $null } else { [string]$manifest.council.release_tag }
        council_archive_sha256 = $councilHash
        council_environment = if ($SkipCouncil) { $null } else { Split-Path -Leaf $councilEnvironment }
        contract_version = [string]$manifest.council.contract_version
        council_state_schema = [int]$manifest.council.state_schema
        occult_initialized = $initialized
        occult_enabled = $enabled
    }
    $receiptPath = Join-Path $resolvedInstallRoot "occult-install-receipt.json"
    $receiptTemporary = "$receiptPath.tmp"
    $receipt | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $receiptTemporary -Encoding UTF8
    Move-Item -LiteralPath $receiptTemporary -Destination $receiptPath -Force

    Write-Step "Installed Occult release v$normalizedVersion in $resolvedInstallRoot"
    Write-Host $hermesVersionOutput
    if ($councilVersionOutput) {
        Write-Host "Agents Council $councilVersionOutput"
    }
    if ($initialized) {
        if ($InitializeLocal) {
            Write-Step "Local initialization completed explicitly with $Model"
        } else {
            $stateLabel = if ($enabled) { "enabled" } else { "disabled" }
            Write-Step "Existing Occult initialization was preserved and remains $stateLabel"
        }
    } else {
        Write-Step "Occult remains disabled. Run this installer again with -InitializeLocal when ready"
    }
} finally {
    $resolvedTemporaryRoot = [IO.Path]::GetFullPath($temporaryRoot)
    $temporaryPrefix = $temporaryBase.TrimEnd("\") + "\"
    $isOwnedTemporaryRoot = (
        $resolvedTemporaryRoot.StartsWith(
            $temporaryPrefix,
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        (Split-Path -Leaf $resolvedTemporaryRoot).StartsWith(
            "occult-install-",
            [StringComparison]::OrdinalIgnoreCase
        )
    )
    if ($isOwnedTemporaryRoot -and (Test-Path -LiteralPath $resolvedTemporaryRoot)) {
        Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
