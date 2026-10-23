@echo off

rem ============================================
rem  游戏活动监测 - 卸载《官方社区前瞻总览》每日定时任务
rem  右键本文件 -> 以管理员身份运行
rem ============================================

schtasks /delete /tn "GameActivityMonitor_Community" /f >nul 2>&1
echo 已删除定时任务 GameActivityMonitor_Community（如原本不存在会提示找不到，属正常）。
echo.
pause
