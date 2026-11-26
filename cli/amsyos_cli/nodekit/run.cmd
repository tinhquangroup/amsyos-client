@echo off
rem A job of a service that amsyos deploy built on this Windows node (where: node). Its
rem command and serve, as node.deploy has them and cmd runs them in the service's folder, call
rem _node\run.cmd with the image folder in quotes, then, when that succeeded, the card's command.
rem This sets what the card's command needs in that same cmd (no setlocal), then makes the
rem image folder that build.ps1 made the current one; the card's command runs after it and its
rem exit code is the job's. The environment's Scripts come first on PATH, VIRTUAL_ENV is it,
rem Python reads and writes UTF-8, the Hugging Face cache is read offline, and caches go in the
rem job's own temporary folder (TEMP, which the node sets). No job can write any of it.
set "AMSYOS_IMAGE=%~1"
if not exist "%AMSYOS_IMAGE%\_node\where.env" goto noimage
set "ENV_DIR="
set "HF_DIR="
set "FETCHED="
for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%AMSYOS_IMAGE%\_node\where.env") do (
  if "%%a"=="ENV_DIR" set "ENV_DIR=%%b"
  if "%%a"=="HF_DIR" set "HF_DIR=%%b"
  if "%%a"=="FETCHED" set "FETCHED=%%b"
)
if not defined ENV_DIR goto noimage
if not exist "%TEMP%\cache\" mkdir "%TEMP%\cache" 2>nul
set "PATH=%ENV_DIR%\Scripts;%AMSYOS_IMAGE%\_node\bin;%PATH%"
set "VIRTUAL_ENV=%ENV_DIR%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUNBUFFERED=1"
set "HF_HOME=%HF_DIR%"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
set "AMSYOS_FETCH_STORE=%FETCHED%"
set "XDG_CACHE_HOME=%TEMP%\cache"
set "MPLCONFIGDIR=%TEMP%\cache\mpl"
set "NUMBA_CACHE_DIR=%TEMP%\cache\numba"
cd /d "%AMSYOS_IMAGE%" || exit /b 2
exit /b 0
:noimage
echo amsyos: "%AMSYOS_IMAGE%" is no image amsyos deploy made on this machine: deploy again with a build, amsyos deploy --force 1>&2
exit /b 2
