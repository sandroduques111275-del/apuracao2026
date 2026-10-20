@echo off
cd /d "%~dp0"
title Simulacao da apuracao (dados ficticios)
start "" http://localhost:8080
python servidor.py --demo --demo-minutos 3
pause
