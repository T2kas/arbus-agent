@echo off
REM Update this folder to the latest code. Just type:  pull
REM (Windows only. Same thing as: git pull origin <branch>)
cd /d "%~dp0"
for /f %%b in ('git rev-parse --abbrev-ref HEAD') do set BRANCH=%%b
echo Updating branch %BRANCH% ...
git pull origin %BRANCH%
if errorlevel 1 (
  echo.
  echo Pull failed. If it complains about data\arbus.db, run:
  echo     git checkout -- data
  echo and then type pull again.
)
python -m arbus --help
