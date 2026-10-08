@echo off
chcp 65001 >nul
rem (0.7.0-dev, эксперимент) Веб-интерфейс: поля заявления читает модель qwen3:8b вместо регулярок бланка.
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat (call .venv\Scripts\activate.bat) else if exist ..\doctool\.venv\Scripts\activate.bat call ..\doctool\.venv\Scripts\activate.bat
set DOCTOOL_LLM_FIELDS=qwen3:8b
set DOCTOOL_LLM_THINK=1
set DOCTOOL_LLM_TIMEOUT=240
python -m doctool web
pause
