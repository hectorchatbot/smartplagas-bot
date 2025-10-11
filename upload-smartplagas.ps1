param(
    [switch]$SendWA,
    [string]$TwilioSid,
    [string]$TwilioToken,
    [string]$FromWA,
    [string]$ToWA
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---- Defaults (si no pasas parámetros) ----
if (-not $FromWA -or $FromWA.Trim() -eq "") { $FromWA = "whatsapp:+14155238886" }  # sandbox por defecto
if (-not $ToWA) { $ToWA = "" }

# --- CONFIG del server ---
$domain = "https://web-production-62037.up.railway.app"
$token  = "super-token-seguro-6377"

$rootBotOut = "C:\Users\hecto\OneDrive\Escritorio\ServiciosChatBot\ProyectoChatBot\smartplagasbot\smartplagas-bot\out"
$rootStatic = "C:\Users\hecto\OneDrive\Escritorio\ServiciosChatBot\ProyectoChatBot\smartplagasbot\smartplagas-bot\static"

function Get-LastPdf {
    param([string]$p1, [string]$p2)
    $pdf = $null
    if (Test-Path $p1) {
        $pdf = Get-ChildItem -Path $p1 -Filter *.pdf -ErrorAction SilentlyContinue |
               Sort-Object LastWriteTime -Descending |
               Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $pdf -and (Test-Path $p2)) {
        $pdf = Get-ChildItem -Path $p2 -Filter *.pdf -ErrorAction SilentlyContinue |
               Sort-Object LastWriteTime -Descending |
               Select-Object -First 1 -ExpandProperty FullName
    }
    return $pdf
}

function Invoke-UploadWithRetry {
    param(
        [string]$PdfPath,
        [string]$FileName,
        [int]$MaxAttempts = 3,
        [int]$DelayMs = 800
    )
    $form = "file=@`"$PdfPath`";type=application/pdf;filename=`"$FileName`""
    for ($i=1; $i -le $MaxAttempts; $i++) {
        if (Test-Path response.json) { Remove-Item response.json -Force -ErrorAction SilentlyContinue }
        $args = @(
          "-s","-w","%{http_code}","-o","response.json",
          "-X","POST","$domain/upload",
          "-H","X-Upload-Token: $token",
          "--form",$form
        )
        $http = & curl.exe @args
        if ($http -eq "200") {
            $body = ""
            if (Test-Path response.json) { $body = Get-Content response.json -Raw }
            return @{ Code = 200; Body = $body }
        }
        Write-Host ("Intento {0}/{1} falló (HTTP {2})." -f $i,$MaxAttempts,$http) -ForegroundColor DarkYellow
        Start-Sleep -Milliseconds $DelayMs
    }
    $body = ""
    if (Test-Path response.json) { $body = Get-Content response.json -Raw }
    return @{ Code = 0; Body = $body }
}

# --- ELEGIR PDF MAS RECIENTE ---
$pdf = Get-LastPdf -p1 $rootBotOut -p2 $rootStatic
if (-not $pdf) { throw "No se encontró ningún PDF en 'out' ni en 'static'." }

$pdf = (Resolve-Path -LiteralPath $pdf).Path
$filename = Split-Path -Leaf $pdf
Write-Host ("Usando PDF: {0}" -f $filename) -ForegroundColor Yellow
if ((Get-Item -LiteralPath $pdf).Length -le 0) { throw "El PDF está vacío: $pdf" }

# --- SUBIR con reintento ---
Write-Host ("Subiendo {0} a {1}/upload ..." -f $filename,$domain) -ForegroundColor Cyan
$result = Invoke-UploadWithRetry -PdfPath $pdf -FileName $filename
if ($result.Code -ne 200) {
    Write-Host "Error al subir tras reintentos." -ForegroundColor Red
    if ($result.Body) { $result.Body | Write-Host }
    exit 1
}

# Parsear respuesta segura
try {
    $json = $result.Body | ConvertFrom-Json
} catch {
    Write-Host "Respuesta no es JSON válido:" -ForegroundColor Yellow
    $result.Body | Write-Host
    exit 1
}

if (-not $json.url) {
    Write-Host "La respuesta no incluye 'url':" -ForegroundColor Yellow
    $result.Body | Write-Host
    exit 1
}

$url = [string]$json.url
Write-Host "Subida exitosa." -ForegroundColor Green
Write-Host "URL publica:" -ForegroundColor Yellow
Write-Host $url -ForegroundColor Cyan

# Copiar al portapapeles
try { Set-Clipboard -Value $url; Write-Host "La URL se copio al portapapeles." -ForegroundColor Green } catch {}

# --- Envío opcional por WhatsApp (Twilio) ---
if ($SendWA) {
    # Normaliza y valida formato whatsapp:+CCNNN...
    $FromWA = ($FromWA -replace '\s','')
    $ToWA   = ($ToWA   -replace '\s','')

    if ($FromWA -notmatch '^whatsapp:\+\d{6,15}$') {
        Write-Host "FromWA inválido. Usa formato: whatsapp:+14155238886 (sandbox) o tu número WABA." -ForegroundColor Red
        exit 1
    }
    if ($ToWA -notmatch '^whatsapp:\+\d{6,15}$') {
        Write-Host "ToWA inválido. Usa formato: whatsapp:+569XXXXXXXX." -ForegroundColor Red
        exit 1
    }
    if (-not $TwilioSid -or -not $TwilioToken) {
        Write-Host "Faltan TwilioSid/TwilioToken." -ForegroundColor Red
        exit 1
    }

    $twilioUrl = "https://api.twilio.com/2010-04-01/Accounts/$TwilioSid/Messages.json"
    $body = "Cotizacion SmartPlagas: $filename"

    $postData = "From=$([uri]::EscapeDataString($FromWA))" +
                "&To=$([uri]::EscapeDataString($ToWA))" +
                "&Body=$([uri]::EscapeDataString($body))" +
                "&MediaUrl=$([uri]::EscapeDataString($url))"

    Write-Host "Enviando WhatsApp via Twilio..." -ForegroundColor Cyan
    if (Test-Path twilio.json) { Remove-Item twilio.json -Force -ErrorAction SilentlyContinue }
    $twArgs = @(
      "-s","-w","%{http_code}","-o","twilio.json",
      "-u","$TwilioSid`:$TwilioToken",
      "-H","Content-Type: application/x-www-form-urlencoded",
      "-X","POST",$twilioUrl,
      "--data",$postData
    )
    $twHttp = & curl.exe @twArgs

    if ($twHttp -eq "201") {
        Write-Host "WhatsApp enviado (201 Created)." -ForegroundColor Green
    } else {
        Write-Host ("Fallo Twilio (HTTP {0}). Respuesta:" -f $twHttp) -ForegroundColor Red
        if (Test-Path twilio.json) { Get-Content twilio.json -Raw | Write-Host }
    }
}
