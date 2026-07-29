@echo off
REM SRT Restreamer — one-command start (delegates to Git Bash).
cd /d %~dp0
if exist "%ProgramFiles%\Git\bin\bash.exe" (
    "%ProgramFiles%\Git\bin\bash.exe" start.sh %*
) else (
    echo Git Bash not found. Run "bash start.sh" from Git Bash instead.
    exit /b 1
)
