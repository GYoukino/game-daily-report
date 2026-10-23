@echo off
cd /d "%~dp0"

rem ============================================
rem  游戏活动监测 - 官方社区前瞻总览（每日定时用）
rem  无窗口运行 community_report.py，产出 + 飞书推送，日志写 logs\community.log
rem ============================================

if not exist "logs" mkdir "logs"

set "PY_EXE=C:/Users/sakur/.workbuddy/binaries/python/versions/3.13.12/python.exe"
if not exist "%PY_EXE%" set "PY_EXE=python"

set "PYTHONIOENCODING=gbk"

"%PY_EXE%" community_report.py >> "%~dp0logs\community.log" 2>&1
