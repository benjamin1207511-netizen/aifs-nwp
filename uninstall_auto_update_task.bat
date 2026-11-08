@echo off
chcp 65001 >nul
title 卸载自动更新计划任务
cd /d "%~dp0"

set "TASK=BAMC-AIFS-AutoUpdate"

schtasks /Delete /F /TN "%TASK%"
if errorlevel 1 (
  echo.
  echo  未找到计划任务 %TASK%（可能本来就没安装）。
) else (
  echo.
  echo  已删除计划任务 %TASK%。
)
pause
