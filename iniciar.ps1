$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$ruralPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $ruralPython)) {
    throw 'Crie o ambiente Python e instale requirements.txt conforme o README.'
}
if (Test-Path -LiteralPath '.local-postgres\data\PG_VERSION') {
    & $ruralPython banco_local.py iniciar
    if ($LASTEXITCODE -ne 0) { throw 'Não foi possível iniciar o banco.' }
}
& $ruralPython -m flask --app app init-db
if ($LASTEXITCODE -ne 0) { throw 'Não foi possível inicializar as tabelas.' }
Write-Host 'RuralFin disponível em http://127.0.0.1:5001. Use Ctrl+C para encerrar o site.'
& $ruralPython -m waitress --listen=127.0.0.1:5001 --threads=4 app:app
