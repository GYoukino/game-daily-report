@echo off
cd /d "%~dp0"
chcp 936 >nul

rem ============================================
rem  游戏活动监测 - 前台验证运行（官方社区前瞻总览）
rem  作用：立刻跑一次，控制台实时显示全部输出，方便排查
rem  定时任务用的是 run_community.bat，日志写 logs\community.log
rem ============================================

set "PY_EXE=C:/Users/sakur/.workbuddy/binaries/python/versions/3.13.12/python.exe"
if not exist "%PY_EXE%" set "PY_EXE=python"

echo ============================================
echo   官方社区前瞻总览 - 手动验证运行
echo   预计耗时 1-3 分钟，请稍候...
echo ============================================
echo.

"%PY_EXE%" community_report.py

echo.
echo ============================================
echo   运行结束（退出码 %errorlevel%）
echo   报告目录：reports\
echo   定时任务日志：logs\community.log
echo ============================================
echo.
pause
