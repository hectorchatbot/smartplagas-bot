param(
  [Parameter(Mandatory = $true)][string]$DocxUrl,
  [Parameter(Mandatory = $true)][string]$UploadUrl,
  [Parameter(Mandatory = $true)][string]$UploadToken,
  [string]$OutDir = ".\out"
)

function Stop-Clean($msg) {
  Write-Host "[ERROR] $msg" -ForegroundColor Red
  throw $msg
}

# Crear carpeta de salida
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path

# Descargar DOCX
Write-Host "Descargando DOCX desde $DocxUrl ..."
$tmpDocx = Join-Path $OutDir (Split-Path -Leaf $DocxUrl)
try {
  Invoke-WebRequest -Uri $DocxUrl -OutFile $tmpDocx -UseBasicParsing -ErrorAction Stop
} catch {
  Stop-Clean "Error al descargar DOCX: $($_.Exception.Message)"
}

# Convertir a PDF usando Word COM
$pdfPath = [System.IO.Path]::ChangeExtension($tmpDocx, ".pdf")
Write-Host "Convirtiendo a PDF con Microsoft Word..."
try {
  $word = New-Object -ComObject Word.Application
  $word.Visible = $false
  $doc = $word.Documents.Open($tmpDocx)
  $doc.SaveAs([ref]$pdfPath, [ref]17)
  $doc.Close()
  $word.Quit()
  Write-Host "PDF generado: $pdfPath"
} catch {
  Stop-Clean "Error al convertir a PDF: $($_.Exception.Message)"
}

# --- SUBIR PDF (multipart/form-data) ---
try {
  # TLS 1.2 y sin Expect: 100-continue
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  [System.Net.ServicePointManager]::Expect100Continue = $false

  $headers = @{ Authorization = "Bearer $UploadToken" }
  $form    = @{ file = Get-Item -LiteralPath $pdfPath }   # <-- el campo se llama 'file'

  $resp = Invoke-WebRequest -Uri $UploadUrl -Method POST -Headers $headers -Form $form

  Write-Host "OK subida ->" $resp.StatusCode
  Write-Host $resp.Content
}
catch {
  Stop-Clean ("Fallo al subir el PDF: {0}" -f $_.Exception.Message)
}

Write-Host "Proceso completado correctamente."
