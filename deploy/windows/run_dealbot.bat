@echo off
REM dealbot 상주 실행 (작업 스케줄러에서 호출). 프로젝트 폴더 기준으로 동작한다.
cd /d "%~dp0\..\.."
if exist ".venv\Scripts\python.exe" (
    set PY=.venv\Scripts\python.exe
) else (
    set PY=python
)
if not exist logs mkdir logs
%PY% -m dealbot serve >> logs\console.log 2>&1
