@echo off
chcp 65001 >nul
title AlternanceAssistant
cd /d "%~dp0"
python -c "import flask" 2>nul || (
  echo Installation de Flask...
  python -m pip install -r requirements.txt
)
echo.
echo  AlternanceAssistant tourne sur http://127.0.0.1:5055
echo  (ferme cette fenetre pour arreter)
echo.
python app.py
pause
