
@echo off
SETLOCAL ENABLEDELAYEDEXPANSION

REM Caminhos das pastas
set "PASTA_ENTRADA=C:\PDF_Automacao\Entrada"
set "PASTA_SAIDA=C:\PDF_Automacao\Saida"

REM Loop para processar todos os PDFs da pasta de entrada
for %%f in ("%PASTA_ENTRADA%\*.pdf") do (
    echo Processando: %%f
    ocrmypdf --output-type pdfa -l por --skip-text "%%f" "%PASTA_SAIDA%\%%~nxf"
)

echo Conversao concluida!
pause
