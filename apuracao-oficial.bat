@echo off
cd /d "%~dp0"
title Apuracao oficial - TSE
start "" http://localhost:8080
python servidor.py
pause
