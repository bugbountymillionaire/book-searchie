@echo off
rem Start Book-Searchie on Windows: pick up new or changed books, then open the search page.
rem Double-click it, or make a desktop shortcut to it. Close the window to stop Book-Searchie.
title Book-Searchie
cd /d "%~dp0.."

if not exist ".venv\Scripts\book-searchie.exe" (
    echo Book-Searchie isn't installed in %CD%\.venv yet. See "Quick start" in README.md.
    goto failed
)

echo Checking your library for new, changed or deleted books...
".venv\Scripts\book-searchie.exe" index -q
if errorlevel 1 goto failed

echo.
echo Starting Book-Searchie. It opens in your browser; close this window to stop it.
echo.
".venv\Scripts\book-searchie.exe" serve
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo Book-Searchie couldn't start. The message above says why.
pause
exit /b 1
