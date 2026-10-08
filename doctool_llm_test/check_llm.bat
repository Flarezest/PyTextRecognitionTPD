@echo off
chcp 65001 >nul
rem (0.7.0-dev, эксперимент) Прогон наборов Dataset\PhysicalPersonAdminChange в двух режимах:
rem регулярки бланка и модель qwen3:8b (с рассуждением). Сводка - results_cmp\llm\сравнение.html
rem Нужно: Ollama запущена, модель скачана (ollama pull qwen3:8b).
rem Параметры можно дописать: check_llm.bat --think off   /   --sets set8 set13   /   --skip-regex
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat (call .venv\Scripts\activate.bat) else if exist ..\doctool\.venv\Scripts\activate.bat call ..\doctool\.venv\Scripts\activate.bat
python llm_test\run_compare.py --model qwen3:8b --think on %*
pause
