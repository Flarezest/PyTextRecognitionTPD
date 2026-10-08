@echo off
rem Пакетная проверка наборов «смена администратора, физлицо»: в папке D по подпапке на дело.
rem   Application* и Passport*  - заявление и паспорт;
rem   только Passport*          - «Только проверка паспорта»;
rem   иначе                     - первый PDF/JPG/PNG папки как заявление (сканы-комплекты без паспорта).
rem Если в папке и заявление, и паспорт, но названы иначе - переименуйте их в Application... и Passport...
rem Итоги всех дел - results\реестр_проверок.xlsx
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
set D=F:\Programming\Python\PyTextRecognition\Dataset\PhysicalPersonAdminChange
for /d %%S in ("%D%\*") do call :one "%%S" "%%~nxS"
pause
goto :eof

:one
set "A="
set "P="
set "F="
for %%X in ("%~1\Application*") do set "A=%%X"
for %%X in ("%~1\Passport*") do set "P=%%X"
if defined A if defined P (
  python -m doctool -q check -a "%A%" -p "%P%" --case "%~2" -o results
  goto :eof
)
if defined P (
  python -m doctool -q check -t passport_only -p "%P%" --case "%~2" -o results
  goto :eof
)
for %%X in ("%~1\*.pdf" "%~1\*.jpg" "%~1\*.jpeg" "%~1\*.png") do if not defined F set "F=%%X"
if defined F python -m doctool -q check -a "%F%" --case "%~2" -o results
goto :eof
