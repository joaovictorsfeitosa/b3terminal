@echo off
title B3 Terminal
color 0A
cls
echo.
echo  ========================================
echo    B3 TERMINAL - Iniciando...
echo  ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python nao encontrado!
    echo Instale em: https://www.python.org/downloads/
    pause & exit /b
)

echo [1/2] Instalando dependencias...
pip install flask flask-cors yfinance feedparser requests python-dateutil gunicorn -q

echo [2/2] Iniciando servidor...
echo.
echo  Acesse: http://localhost:5000
echo  Pressione CTRL+C para encerrar
echo.
timeout /t 2 >nul
start http://localhost:5000
python app.py
pause
