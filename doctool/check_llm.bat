@echo off
chcp 65001 >nul
rem Сравнение режимов на наборах Dataset\PhysicalPersonAdminChange: каждое заявление читается дважды -
rem регулярками бланка и нейросетью (qwen3:8b, с рассуждением). Сводка - results_cmp\llm\сравнение.html
rem Нужно: Ollama запущена, модель скачана (ollama pull qwen3:8b).
rem Параметры можно дописать: check_llm.bat --think off   /   --sets set8 set13   /   --skip-regex   /   --model qwen3-vl:8b-instruct
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
python llm_test\run_compare.py --model qwen3:8b --think on %*
pause
