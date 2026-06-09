#!/usr/bin/env bash
# Inicia el Consultor Legislativo España
set -e

cd "$(dirname "$0")"

# Instala dependencias si faltan
if ! python3 -c "import fastapi, uvicorn, anthropic" 2>/dev/null; then
  echo "Instalando dependencias..."
  pip3 install -q -r requirements.txt
fi

echo ""
echo "╔════════════════════════════════════════════╗"
echo "║   Consultor Legislativo España             ║"
echo "║   http://localhost:8000                    ║"
echo "║                                            ║"
echo "║   • El índice se construye en segundo      ║"
echo "║     plano (primera vez ~60-90 s)           ║"
echo "║   • ANTHROPIC_API_KEY activa la IA         ║"
echo "║   • Ctrl+C para detener                    ║"
echo "╚════════════════════════════════════════════╝"
echo ""

python3 app.py
