param(
    [string]$Repo = $env:KIM_RELEASE_REPO,
    [string]$Version = $env:KIM_VERSION,
    [string]$InstallDir = $env:KIM_INSTALL_DIR
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Override KIM_RELEASE_REPO env var to point at a different fork (#66).
if ([string]::IsNullOrWhiteSpace($Repo)) {
    if (-not [string]::IsNullOrWhiteSpace($env:KIM_RELEASE_REPO)) {
        $Repo = $env:KIM_RELEASE_REPO
    } else {
        $Repo = "AdamMagued/kim"
    }
}

if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = "latest"
}

if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    $InstallDir = Join-Path $env:USERPROFILE ".kim\bin"
}

$arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
switch ($arch) {
    "x64" { $cpu = "x86_64" }
    "arm64" { $cpu = "aarch64" }
    default {
        throw "Unsupported Windows architecture: $arch"
    }
}

$asset = "kim-$cpu-pc-windows-msvc.zip"
if ($Version -eq "latest") {
    $url = "https://github.com/$Repo/releases/latest/download/$asset"
} else {
    $url = "https://github.com/$Repo/releases/download/$Version/$asset"
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("kim-install-" + [Guid]::NewGuid().ToString("N"))
$archivePath = Join-Path $tempRoot $asset
$extractDir = Join-Path $tempRoot "out"

New-Item -ItemType Directory -Force -Path $tempRoot, $extractDir, $InstallDir | Out-Null

try {
    Write-Host "Downloading Kim TUI: $url"
    $headers = @{}
    if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_TOKEN)) {
        $headers["Authorization"] = "Bearer $env:GITHUB_TOKEN"
    }

    try {
        Invoke-WebRequest -Uri $url -OutFile $archivePath -Headers $headers -UseBasicParsing
    } catch {
        throw "Could not download $asset. Publish a Windows release asset with this exact name first, or set GITHUB_TOKEN for a private release."
    }

    # Checksum verification (#58): download the .sha256 sidecar published by
    # release.yml and verify before install. Missing checksum material is a
    # HARD FAILURE — an unverifiable download must never install silently.
    # Set KIM_SKIP_CHECKSUM=1 to bypass (not recommended).
    $skipChecksum = $env:KIM_SKIP_CHECKSUM -eq "1"
    if (-not $skipChecksum) {
        $shaUrl = $url -replace '\.(zip|tar\.gz)$', '.sha256'
        $shaPath = Join-Path $tempRoot "SHA256SUMS"
        try {
            Invoke-WebRequest -Uri $shaUrl -OutFile $shaPath -Headers $headers -UseBasicParsing -ErrorAction Stop
        } catch {
            throw "Checksum sidecar not available at $shaUrl. Refusing to install an unverifiable download. Set KIM_SKIP_CHECKSUM=1 to override (not recommended)."
        }
        $shaContent = Get-Content $shaPath -Raw
        $expectedLine = $shaContent -split "`n" | Where-Object { $_ -match [regex]::Escape($asset) } | Select-Object -First 1
        if (-not $expectedLine) {
            throw "No checksum entry found for $asset in $shaUrl. Aborting."
        }
        $expectedHash = ($expectedLine -split '\s+')[0].Trim().ToLower()
        $actualHash = (Get-FileHash -Path $archivePath -Algorithm SHA256).Hash.ToLower()
        if ($actualHash -ne $expectedHash) {
            throw "Checksum mismatch for $asset (expected $expectedHash, got $actualHash). Aborting."
        }
        Write-Host "Checksum verified: $expectedHash"
    }

    Expand-Archive -Path $archivePath -DestinationPath $extractDir -Force

    $kimExe = Get-ChildItem -Path $extractDir -Recurse -Filter "kim.exe" | Select-Object -First 1
    if ($null -eq $kimExe) {
        throw "Release asset did not contain kim.exe."
    }

    $dest = Join-Path $InstallDir "kim.exe"
    Copy-Item -Path $kimExe.FullName -Destination $dest -Force

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $pathParts = @()
    if (-not [string]::IsNullOrWhiteSpace($userPath)) {
        $pathParts = $userPath -split ";"
    }

    $alreadyOnPath = $false
    foreach ($pathPart in $pathParts) {
        if ($pathPart.TrimEnd("\") -ieq $InstallDir.TrimEnd("\")) {
            $alreadyOnPath = $true
            break
        }
    }

    if (-not $alreadyOnPath) {
        $newUserPath = if ([string]::IsNullOrWhiteSpace($userPath)) { $InstallDir } else { "$userPath;$InstallDir" }
        [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
        Write-Host "Added $InstallDir to your user PATH. Open a new terminal before running kim."
    }

    Write-Host "Kim TUI installed at $dest"
    Write-Host "Next: open a new terminal, run 'kim', then type /login."
} finally {
    Remove-Item -Path $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
