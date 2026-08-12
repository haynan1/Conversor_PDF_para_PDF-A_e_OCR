@echo off
rem ===========================================================================
rem  Scriptor - OCR e PDF/A
rem
rem  Ponto de entrada unico do kit. Duplo clique e pronto.
rem
rem  Na primeira execucao prepara um ambiente Python isolado; nas seguintes,
rem  apenas abre a interface. O ambiente fica em %LOCALAPPDATA%, e nao dentro
rem  desta pasta, de proposito: um ambiente virtual tem milhares de arquivos
rem  pequenos e sincronizar isso pelo OneDrive deixa a maquina inutilizavel.
rem ===========================================================================

setlocal EnableExtensions DisableDelayedExpansion
title Scriptor - OCR e PDF/A
cd /d "%~dp0"

set "RAIZ=%~dp0"
set "CASA=%LOCALAPPDATA%\Scriptor"
set "VENV=%CASA%\venv"
set "PY=%VENV%\Scripts\python.exe"
set "MARCA=%CASA%\instalado.txt"
set "RODAS=%RAIZ%instaladores\wheels"

echo.
echo   SCRIPTOR   OCR e PDF/A
echo   ----------------------------------------------------------------
echo.

rem --------------------------------------------------------------- Python ---
if exist "%PY%" goto :verificar_atualizacao

set "BOOT="
py -3 --version >nul 2>&1
if not errorlevel 1 set "BOOT=py -3"
if defined BOOT goto :criar_ambiente

python --version >nul 2>&1
if not errorlevel 1 set "BOOT=python"
if defined BOOT goto :criar_ambiente

goto :sem_python

:criar_ambiente
echo   Preparando o ambiente. Isso acontece so na primeira vez.
echo.
if not exist "%CASA%" mkdir "%CASA%" >nul 2>&1
%BOOT% -m venv "%VENV%"
if errorlevel 1 goto :falha_ambiente
"%PY%" -m pip install --upgrade pip --quiet
goto :instalar

rem ------------------------------------------------- atualizacao do codigo ---
:verificar_atualizacao
if not exist "%MARCA%" goto :instalar
rem Reinstala quando o projeto mudou ou quando veio de outra copia.
for %%A in ("%RAIZ%pyproject.toml") do set "ALVO=%%~tA %RAIZ%"
set /p GRAVADO=<"%MARCA%"
if "%GRAVADO%"=="%ALVO%" goto :abrir
echo   Atualizando o Scriptor...
echo.

:instalar
if exist "%RODAS%" goto :instalar_offline
"%PY%" -m pip install --quiet --upgrade "%RAIZ%."
if errorlevel 1 goto :falha_instalacao
goto :marcar

:instalar_offline
echo   Instalando a partir dos pacotes locais...
"%PY%" -m pip install --quiet --no-index --find-links "%RODAS%" "%RAIZ%."
if errorlevel 1 goto :falha_instalacao

:marcar
for %%A in ("%RAIZ%pyproject.toml") do echo %%~tA %RAIZ%>"%MARCA%"

rem ------------------------------------------------------------- interface ---
:abrir
if not exist "%RAIZ%Documentos\scriptor.toml" "%PY%" -m scriptor init "%RAIZ%Documentos" >nul 2>&1

echo   Abrindo a interface no navegador...
echo   Mantenha esta janela aberta enquanto usa o Scriptor.
echo.
"%PY%" -m scriptor abrir --config "%RAIZ%Documentos\scriptor.toml"
goto :fim

rem ---------------------------------------------------------------- falhas ---
rem O projeto e distribuido de duas formas: como kit, com os instaladores
rem dentro da pasta, e como clone do repositorio, onde eles nao existem porque
rem binarios de centenas de megabytes nao sao versionados. Mandar o operador
rem abrir uma pasta inexistente e pior que nao dizer nada.
:sem_python
echo   [ ! ]  O Python nao esta instalado nesta maquina.
echo.
if exist "%RAIZ%instaladores\python-*.exe" goto :python_do_kit

echo   Instale com um dos dois caminhos:
echo.
echo       winget install --id Python.Python.3.13
echo.
echo   ou baixe em https://www.python.org/downloads/windows/
echo   marcando "Add python.exe to PATH" na primeira tela.
echo.
echo   Depois de instalar, feche esta janela e abra o Scriptor de novo.
goto :pausa

:python_do_kit
echo   Instale-o com o arquivo que acompanha o kit:
echo.
echo       instaladores\python-3.13.3-amd64.exe
echo.
echo   IMPORTANTE: na primeira tela do instalador, marque a caixa
echo   "Add python.exe to PATH" antes de clicar em "Install Now".
echo.
echo   Depois de instalar, feche esta janela e abra o Scriptor de novo.
echo.
start "" "%RAIZ%instaladores"
goto :pausa

:falha_ambiente
echo   [ ! ]  Nao foi possivel criar o ambiente Python em:
echo          %VENV%
echo.
echo   Verifique se ha espaco em disco e se o antivirus nao esta
echo   bloqueando a pasta acima.
goto :pausa

:falha_instalacao
echo   [ ! ]  Nao foi possivel instalar os componentes do Scriptor.
echo.
echo   A instalacao precisa de internet na primeira execucao.
if exist "%RAIZ%instaladores" (
echo   Se esta maquina nao tem acesso a rede, peca ao responsavel
echo   pelo kit os pacotes offline para a pasta:
echo.
echo       instaladores\wheels
) else (
echo   Sem rede, coloque os pacotes .whl numa pasta instaladores\wheels
echo   ao lado deste arquivo e execute de novo.
)
echo.
goto :pausa

:pausa
echo.
pause
exit /b 1

:fim
endlocal
