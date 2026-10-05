@echo off
rem Пакетная проверка: в папке D по подпапке на дело, в каждой файлы Application* и Passport*
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
set D=F:\Programming\Python\PyTextRecognition\Dataset
for /d %%S in ("%D%\*") do (
  for %%A in ("%%S\Application*") do (
    for %%P in ("%%S\Passport*") do (
      python -m doctool -q check -a "%%A" -p "%%P" --case "%%~nxS" -o results
    )
  )
)
pause
