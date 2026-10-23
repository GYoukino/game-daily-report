@echo off
setlocal enabledelayedexpansion

rem ============================================
rem  游戏活动监测 - 注册《官方社区前瞻总览》每日定时任务
rem  右键本文件 -> 以管理员身份运行
rem  效果：每天 10:00 自动运行 run_community.bat（抓取+生成报告+飞书推送）
rem ============================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 需要管理员权限。请右键本文件，选择"以管理员身份运行"。
    echo.
    pause
    exit /b 1
)

set "DIR=%~dp0"

echo 注册每日 10:00 定时任务（官方社区前瞻总览 + 飞书推送）...
schtasks /create /tn "GameActivityMonitor_Community" /tr "\"%DIR%run_community.bat\"" /sc daily /st 10:00 /rl highest /f >nul
if !errorlevel! neq 0 (
    echo [失败] 注册失败，请重试或以管理员身份运行。
    echo.
    pause
    exit /b 1
)
echo.
echo ============================================
echo  注册成功！
echo.
echo  - 每天 10:00 自动运行，无需手动操作
echo  - 运行日志：logs\community.log
echo  - 报告产物：reports\官方社区前瞻总览_日期.html
echo  - 飞书推送：配置见 config.toml [notify.feishu]
echo  - 卸载任务：右键运行 unregister_community_task.bat
echo ============================================
echo.
pause
