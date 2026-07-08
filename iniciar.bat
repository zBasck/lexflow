@echo off
setlocal
title LexFlow - Servidor Local
cd /d "%~dp0"

set "URL=http://localhost:8765"

echo ============================================
echo   LexFlow - Sistema de Gestao Juridica
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERRO] Python nao foi encontrado neste computador.
    echo.
    echo Instale o Python uma unica vez.
    echo   1. Acesse https://www.python.org/downloads/
    echo   2. Baixe a versao mais recente do Python 3
    echo   3. NA INSTALACAO, marque a opcao "Add Python to PATH"
    echo   4. Apos instalar, execute este arquivo novamente
    echo.
    echo Ou rode o arquivo instalar_python.bat que baixa tudo automatico.
    echo.
    pause
    exit /b 1
)

echo Iniciando LexFlow em %URL%
echo.
echo Acesse esta URL no navegador (Chrome, Edge ou Firefox).
echo.
echo Para encerrar o servidor, feche esta janela ou pressione Ctrl+C.
echo ============================================
echo.

if not exist "data" mkdir data

python backend\server.py
if errorlevel 1 (
    echo.
    echo [ERRO] O servidor Python nao conseguiu iniciar.
    echo Verifique a mensagem acima para mais detalhes.
    pause
)

endlocal
