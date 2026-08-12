@echo off
echo Excluindo arquivos PDF das pastas Entrada e Saida...

rem Defina o caminho completo das pastas
set "caminho_entrada=C:\PDF_Automacao\Entrada"
set "caminho_saida=C:\PDF_Automacao\Saida"

rem Verifica se a pasta Entrada existe e exclui arquivos PDF
if exist "%caminho_entrada%\*.pdf" (
    echo Excluindo arquivos da pasta Entrada...
    del /Q "%caminho_entrada%\*.pdf"
) else (
    echo A pasta Entrada não foi encontrada ou não há arquivos PDF.
)

rem Verifica se a pasta Saida existe e exclui arquivos PDF
if exist "%caminho_saida%\*.pdf" (
    echo Excluindo arquivos da pasta Saida...
    del /Q "%caminho_saida%\*.pdf"
) else (
    echo A pasta Saida não foi encontrada ou não há arquivos PDF.
)

echo Processo concluído.
pause
