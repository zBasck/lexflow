#!/bin/bash
# LexFlow - script de inicializacao para Linux/macOS
# Uso: chmod +x scripts/run.sh && ./scripts/run.sh

set -e
cd "$(dirname "$0")/.."

echo "============================================"
echo "  LexFlow - Sistema de Gestao Juridica"
echo "============================================"
echo ""

# Verifica Python
if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERRO] Python 3 nao encontrado."
    echo "Instale com: sudo apt install python3   (Debian/Ubuntu)"
    echo "             brew install python3        (macOS)"
    exit 1
fi

echo "Iniciando em http://localhost:8765"
echo "Acesse no navegador. Ctrl+C para parar."
echo "============================================"
echo ""

mkdir -p data
python3 backend/server.py
