@echo off
:: fake_ytdlp.bat  —  Windows wrapper for fake_ytdlp.py
::
:: Point YT_DLP_PATH_WINDOWS at THIS file, not at fake_ytdlp.py directly.
:: Windows can't execute a .py file as a subprocess the way Linux uses the
:: shebang line.  This wrapper calls python explicitly, passing all
:: arguments through unchanged.
::
:: In test.conf:
::   YT_DLP_PATH_WINDOWS = C:\Users\owner\jj-dlp\test\fake_ytdlp\fake_ytdlp.bat

:: Resolve the directory this .bat lives in, then find fake_ytdlp.py next to it.
set "SCRIPT_DIR=%~dp0"
set "PY_SCRIPT=%SCRIPT_DIR%fake_ytdlp.py"

python "%PY_SCRIPT%" %*
exit /b %ERRORLEVEL%
