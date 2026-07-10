@echo off
title LexFlow - Sistema de Gestao Juridica
echo ============================================================
echo   LexFlow - Sistema de Gestao Juridica
echo ============================================================
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python nao encontrado. Instale Python 3.10+ e adicione ao PATH.
    pause
    exit /b 1
)
echo [1/2] Verificando dependencias...
pip install -q -r requirements.txt 2>nul
if exist scripts\lexflow-monitor.py (
    start "LexFlow Monitor" /min python scripts\lexflow-monitor.py --interval 60
    echo [INFO] Worker de monitoramento iniciado em background.
)
echo [2/2] Iniciando servidor na porta 8765...
cd backend
python server.py
