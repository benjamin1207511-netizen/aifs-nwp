@echo off
chcp 65001 >nul
title 安装自动更新计划任务
cd /d "%~dp0"

set "TASK=BAMC-AIFS-AutoUpdate"

echo 正在创建 Windows 计划任务：%TASK%
echo 频率：每 30 分钟检查一次，有完整新场次才重新出图（无窗口运行）。
echo.

schtasks /Create /F /SC MINUTE /MO 30 /TN "%TASK%" /TR "\"%~dp0run_auto_update_once.bat\""
if errorlevel 1 (
  echo.
  echo  [失败] 创建计划任务失败。请右键本文件选择「以管理员身份运行」后重试。
  pause
  exit /b 1
)

echo.
echo  已创建。当前状态：
schtasks /Query /TN "%TASK%" /FO LIST
echo.
echo  提示：
echo   - 手动运行一次：schtasks /Run /TN "%TASK%"
echo   - 停止自动更新：双击 uninstall_auto_update_task.bat
echo   - 更新日志：auto_update.log 与 update_task.log
pause
