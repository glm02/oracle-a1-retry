<#
.SYNOPSIS
  Corrige une erreur SSLException du CLI OCI sur Windows causée par un
  antivirus qui inspecte le trafic HTTPS (Avast, AVG, Kaspersky, Bitdefender...).

.DESCRIPTION
  Ces antivirus s'installent comme autorité de certification racine locale et
  remplacent à la volée le certificat des sites HTTPS par le leur, pour pouvoir
  scanner le contenu. Le CLI OCI (comme beaucoup d'outils Python) utilise son
  propre magasin de certificats de confiance ("certifi") et ne connaît pas ce
  certificat local -> toutes les requêtes échouent avec :

    SSLException: "It looks like you are missing some additional certificates..."

  Symptôme avant d'arriver à ce message précis : le CLI reste bloqué plusieurs
  dizaines de secondes à retenter silencieusement avant d'afficher l'erreur
  (désactive les retries avec --no-retry pour la voir immédiatement).

  Ce script trouve le certificat racine de l'antivirus dans le magasin Windows,
  l'exporte, et le fusionne avec le bundle de certificats "certifi" utilisé par
  le CLI OCI (ou tout autre outil Python). Il NE désactive PAS la protection
  antivirus.

.NOTES
  Nécessite le CLI OCI déjà installé (pour localiser certifi).
#>

param(
  [string]$OciPythonExe = "$HOME\lib\oracle-cli\Scripts\python.exe",
  [string]$OutDir = "$HOME\.oci"
)

$ErrorActionPreference = "Stop"

$candidates = "Avast", "AVG", "Kaspersky", "Bitdefender", "ESET", "Norton", "McAfee"
$pattern = ($candidates -join "|")

Write-Host "Recherche d'un certificat d'inspection TLS d'antivirus dans le magasin Windows..."
$cert = Get-ChildItem Cert:\LocalMachine\Root |
  Where-Object { $_.Subject -match $pattern } |
  Select-Object -First 1

if (-not $cert) {
  Write-Host "Aucun certificat d'antivirus connu trouvé. Si l'erreur persiste, cherche" -ForegroundColor Yellow
  Write-Host "manuellement avec : Get-ChildItem Cert:\LocalMachine\Root | Where-Object Subject -match 'scanning|SSL'" -ForegroundColor Yellow
  exit 1
}

Write-Host "Trouvé : $($cert.Subject)"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$cerPath = Join-Path $OutDir "antivirus_root.cer"
$pemPath = Join-Path $OutDir "antivirus_root.pem"
[System.IO.File]::WriteAllBytes($cerPath, $cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert))
certutil -encode $cerPath $pemPath | Out-Null

Write-Host "Localisation du bundle certifi utilisé par le CLI OCI..."
$certifiPath = & $OciPythonExe -c "import certifi; print(certifi.where())"
if (-not $certifiPath) {
  throw "Impossible de localiser certifi via $OciPythonExe. Ajuste -OciPythonExe."
}

$combinedPath = Join-Path $OutDir "combined_ca_bundle.pem"
Get-Content $certifiPath | Set-Content $combinedPath
Add-Content $combinedPath ""
Get-Content $pemPath | Add-Content $combinedPath

Write-Host ""
Write-Host "Bundle combiné créé : $combinedPath" -ForegroundColor Green
Write-Host ""
Write-Host "Pour l'utiliser à chaque session, définis la variable d'environnement :" -ForegroundColor Green
Write-Host "  [Environment]::SetEnvironmentVariable('REQUESTS_CA_BUNDLE', '$combinedPath', 'User')"
Write-Host ""
Write-Host "Puis ouvre un nouveau terminal (ou redéfinis-la dans le terminal courant) avant de relancer oci."
