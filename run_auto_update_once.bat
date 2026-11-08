@echo off
chcp 65001 >nul
rem 供「任务计划程序」调用：静默检查一次新场次，有更新就重新出图。
cd /d "%~dp0"

setlocal
set "PYW="

if exist "%~dp0python\pythonw.exe" set "PYW=%~dp0python\pythonw.exe"
if not defined PYW (
  where pythonw >nul 2>nul && set "PYW=pythonw"
)
if not defined PYW (
  where python >nul 2>nul && set "PYW=python"
)
if not defined PYW (
  if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PYW=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
)

if not defined PYW exit /b 1

"%PYW%" auto_update.py --once >> update_task.log 2>&1
exit /b %errorlevel%
