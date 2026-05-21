@echo off
chcp 65001 >nul
title 【磐石】系统启动器

:: ========================================================
:: ⚠️ 必须把下面这行的路径，改成你第一步建立的那个真实文件夹路径！
:: ========================================================
set APP_PATH=%~dp0

echo 正在进入文件夹: %APP_PATH%
cd /d %APP_PATH%

echo.
echo 正在激活量化沙盒环境...
call conda activate quant_env

echo.
echo 🚀 正在启动 Web 界面，请勿关闭此窗口...
streamlit run app.py

pause