@echo off
:: ==========================================
::   🌅 晚霞预报服务 — 启动脚本
::   双击运行 → 最小化到任务栏
:: ==========================================
::   迁移到新机器：复制整个项目目录即可
::   要求：Python 3.7+（无需安装任何包）
:: ==========================================

chcp 65001 >nul

:: ─── 配置区（按你的需要修改） ───
:: 城市：逗号分隔，如 杭州,北京,上海
:: 经纬度：逗号分隔，如 30.27,34.80
:: FEISHU_WEBHOOK_URL：飞书自定义机器人 Webhook 地址
:: SERVE_TIME：每日推送时间 HH:MM
set LOCATION=杭州
set LAT=
set LNG=
set FEISHU_WEBHOOK_URL=
set SERVE_TIME=16:00

:: ─── 测试模式 ───
:: 如果设置了 RUN_TEST=1，则运行诊断测试后退出
:: 命令行: set RUN_TEST=1 && start.bat
if "%RUN_TEST%"=="1" (
    cd /d "%~dp0"
    title 晚霞预报 - 诊断测试
    echo.
    python -u scripts\predict-sunset.py --location %LOCATION% --test %FEISHU_ARGS%
    echo.
    pause
    exit
)

:: ─── 最小化启动 ───
if not "%1"=="minimized" (
    start "" /min "%~f0" minimized
    exit
)

:: ─── 启动服务 ───
cd /d "%~dp0"
title 晚霞预报服务

echo ═══════════════════════════════════════════════════════
echo   🌅 晚霞预报服务 v2.0
echo ═══════════════════════════════════════════════════════
if not "%LOCATION%"==""  echo   城市: %LOCATION%
if not "%LAT%"==""       echo   纬度: %LAT%
if not "%LNG%"==""       echo   经度: %LNG%
if not "%FEISHU_WEBHOOK_URL%"=="" (
    echo   飞书: ✅ 已配置
) else (
    echo   飞书: ⚠️ 未配置，仅本地输出
)
echo   推送: 每天 %SERVE_TIME%
echo ═══════════════════════════════════════════════════════
echo.

:: 构建命令行
set CMD_ARGS=--serve --serve-time %SERVE_TIME%
if not "%LOCATION%"=="" set CMD_ARGS=%CMD_ARGS% --location %LOCATION%
if not "%LAT%"==""       set CMD_ARGS=%CMD_ARGS% --lat %LAT%
if not "%LNG%"==""       set CMD_ARGS=%CMD_ARGS% --lng %LNG%
if not "%FEISHU_WEBHOOK_URL%"=="" set CMD_ARGS=%CMD_ARGS% --feishu %FEISHU_WEBHOOK_URL%

python -u scripts\predict-sunset.py %CMD_ARGS%

pause
