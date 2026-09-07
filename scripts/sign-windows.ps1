# sign-windows.ps1 - Authenticode-sign Zariff Windows binaries for Smart App Control.
#
# Smart App Control (Windows 11) blocks unsigned apps. `target/release/zariff.exe`
# and the NSIS/MSI bundles are unsigned by default, so SAC blocks them with
# "Smart App Control blocked an app".
#
# This script signs binaries with signtool.exe:
#   1. Classic OV/EV code-signing cert (thumbprint in cert store, or PFX file), OR
#   2. Azure Trusted Signing (Microsoft's hosted signing, recommended for indie devs).
#
# USAGE:
#   # Option A - cert already in CurrentUser\My by thumbprint:
#   .\scripts\sign-windows.ps1 -FilePath .\tauri-app\src-tauri\target\release\zariff.exe -Thumbprint <THUMBPRINT>
#
#   # Option B - PFX file:
#   .\scripts\sign-windows.ps1 -FilePath <file> -PfxPath <cert.pfx> -PfxPassword <password>
#
#   # Option C - Azure Trusted Signing (needs Azure.CodeSigning.Dlib installed):
#   .\scripts\sign-windows.ps1 -FilePath <file> -TrustedSigningAccount <account> -TrustedSigningProfile <profile>
#
#   Sign everything at once (exe + NSIS + MSI):
#   .\scripts\sign-windows.ps1 -SignAll
#
# To have Tauri sign automatically during `tauri build`, set in tauri.conf.json:
#   "bundle": { "windows": {
#     "certificateThumbprint": "<THUMBPRINT>",
#     "digestAlgorithm": "sha256",
#     "timestampUrl": "http://timestamp.digicert.com"
#   }}
# or set "signCommand" to call this script with "%1" as the file placeholder.
#
# NOTE: a brand-new certificate has no reputation. SAC/SmartScreen may still warn
# until the cert builds trust. An EV cert or Microsoft Store release skips this
# warm-up. Self-signed certs do NOT satisfy SAC for other machines - use them
# for local testing only (install the cert to Trusted Publishers on that PC).

[CmdletBinding(DefaultParameterSetName = "Single")]
param(
    [Parameter(ParameterSetName = "Single", Mandatory = $true)]
    [string]$FilePath,

    [Parameter(ParameterSetName = "All")]
    [switch]$SignAll,

    [string]$Thumbprint = $env:TAURI_WINDOWS_CERT_THUMBPRINT,
    [string]$PfxPath = $env:TAURI_WINDOWS_CERT_PFX_PATH,
    [string]$PfxPassword = $env:TAURI_WINDOWS_CERT_PASSWORD,
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [string]$TrustedSigningAccount = $env:AZURE_TRUSTED_SIGNING_ACCOUNT,
    [string]$TrustedSigningProfile = $env:AZURE_TRUSTED_SIGNING_CERT_PROFILE,
    [string]$TrustedSigningEndpointSuffix = "trustedsigning.azure.net"
)

$ErrorActionPreference = "Stop"

function Find-Signtool {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $install = & $vswhere -latest -requires Microsoft.Component.MSBuild -property installationPath 2>$null
        if ($install) {
            $candidate = Get-ChildItem -Path (Join-Path $install "Common7\Tools\..\..\Windows Kits\10\bin") -Filter "signtool.exe" -Recurse -ErrorAction SilentlyContinue |
                Sort-Object FullName -Descending | Select-Object -First 1
            if ($candidate) { return $candidate.FullName }
        }
    }
    $sdk = Get-ChildItem -Path "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Filter "signtool.exe" -Recurse -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($sdk) { return $sdk.FullName }
    $onPath = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    throw "signtool.exe not found. Install Windows SDK (https://developer.microsoft.com/windows/downloads/windows-sdk/) or VS Build Tools."
}

function Invoke-Sign([string]$Target) {
    if (-not (Test-Path $Target)) { throw "File not found: $Target" }
    $signtool = Find-Signtool
    Write-Host "[info] Signing $Target with $signtool" -ForegroundColor Cyan

    $args = @("sign", "/fd", "SHA256", "/tr", $TimestampUrl, "/td", "SHA256")

    if ($TrustedSigningAccount -and $TrustedSigningProfile) {
        # Azure Trusted Signing path. Requires Trusted Signing dlib NuGet package.
        # See: https://learn.microsoft.com/azure/trusted-signing/how-to-signing-integrations
        $endpoint = "https://$TrustedSigningAccount.$TrustedSigningEndpointSuffix"
        $dlib = Get-ChildItem -Path "$PSScriptRoot\..\.trusted-signing" -Filter "Azure.CodeSigning.Dlib.dll" -Recurse -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $dlib) {
            throw "Azure Trusted Signing selected but Azure.CodeSigning.Dlib.dll not found under .trusted-signing/. Install it per MS docs, then re-run."
        }
        $metadata = "{ `"Endpoint`": `"$endpoint`", `"CodeSigningAccountName`": `"$TrustedSigningAccount`", `"CertificateProfileName`": `"$TrustedSigningProfile`", `"ExcludeCredentials`": false }"
        $args += @("/dlib", $dlib.FullName, "/dmdf", $metadata)
    }
    elseif ($Thumbprint) {
        $args += @("/sha1", $Thumbprint, "/sm")
    }
    elseif ($PfxPath) {
        if (-not (Test-Path $PfxPath)) { throw "PFX not found: $PfxPath" }
        $args += @("/f", $PfxPath)
        if ($PfxPassword) { $args += @("/p", $PfxPassword) }
    }
    else {
        throw "No signing identity. Pass -Thumbprint, -PfxPath, or -TrustedSigningAccount/-TrustedSigningProfile (or set TAURI_WINDOWS_CERT_THUMBPRINT / TAURI_WINDOWS_CERT_PFX_PATH env vars)."
    }

    $args += @($Target)
    & $signtool @args
    if ($LASTEXITCODE -ne 0) { throw "signtool failed with exit code $LASTEXITCODE for $Target" }

    $sig = Get-AuthenticodeSignature -FilePath $Target
    if ($sig.Status -ne "Valid") {
        throw "Signature invalid after signing $Target : $($sig.Status) - $($sig.StatusMessage)"
    }
    Write-Host "[ok] Signed and verified: $Target ($($sig.SignerCertificate.Subject))" -ForegroundColor Green
}

if ($SignAll) {
    $root = Join-Path $PSScriptRoot "..\tauri-app\src-tauri\target\release"
    $targets = @()
    $targets += Join-Path $root "zariff.exe"
    $targets += Get-ChildItem -Path (Join-Path $root "bundle") -Include "*.exe", "*.msi" -Recurse -ErrorAction SilentlyContinue |
        ForEach-Object { $_.FullName }
    if (-not $targets) { throw "No binaries found under $root. Run `npm run tauri build` first." }
    foreach ($t in $targets) { Invoke-Sign $t }
}
else {
    Invoke-Sign $FilePath
}
