@echo off
rem ============================================================
rem  XiangQiLens 启动器（批处理版，替代原 启动XiangQiLens.vbs）
rem    双击本文件                    无窗口启动 app_vision.py
rem    启动XiangQiLens.bat debug      控制台前台运行，报错可见，退出后暂停
rem    启动XiangQiLens.bat --selftest 其他参数原样传给 app_vision.py
rem  注意：本文件必须是 GBK(936) 编码，存成 UTF-8 会被 cmd 解析乱码。
rem ============================================================
setlocal EnableExtensions
chcp 936 >nul 2>nul

set "BASE=%~dp0"
if "%BASE:~-1%"=="\" set "BASE=%BASE:~0,-1%"
set "SCRIPT=%BASE%\app_vision.py"
set "LOG=%BASE%\_launch_log.txt"

set "PYW=C:\Users\mango\AppData\Local\Programs\Python\Python313\pythonw.exe"
if not exist "%PYW%" call :find_pyw
if not exist "%PYW%" goto :err_pyw
set "PYN=%PYW:pythonw.exe=python.exe%"
if not exist "%PYN%" set "PYN=%PYW%"

if not exist "%SCRIPT%" goto :err_script

if /i "%~1"=="debug"   goto :console
if /i "%~1"=="console" goto :console
if not "%~1"=="" goto :console_args

rem ---------------- 无窗口启动（等同原 VBS 行为） ----------------
cd /d "%BASE%"
start "" "%PYW%" "%SCRIPT%"
if errorlevel 1 goto :err_start
>>"%LOG%" echo [%date% %time%] GUI ok: "%PYW%" "%SCRIPT%"
exit /b 0


rem ============================ 子过程 ============================

:find_pyw
rem 硬编码路径失效时兜底：PATH 里的 pythonw.exe，再退到 py 启动器
set "PYW="
for /f "delims=" %%i in ('where pythonw.exe 2^>nul ^| findstr /v /i "WindowsApps"') do if not defined PYW set "PYW=%%i"
if defined PYW exit /b
for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PYW set "PYW=%%i"
if defined PYW set "PYW=%PYW:python.exe=pythonw.exe%"
exit /b


:console
title XiangQiLens - 控制台调试
cd /d "%BASE%"
echo [XiangQiLens] 解释器: "%PYN%"
echo [XiangQiLens] 主程序: "%SCRIPT%"
echo.
"%PYN%" "%SCRIPT%"
set "RC=%ERRORLEVEL%"
echo.
echo [XiangQiLens] 程序已退出，退出码 %RC%
>>"%LOG%" echo [%date% %time%] console rc=%RC%
echo.
pause
exit /b %RC%


:console_args
title XiangQiLens - 参数运行
cd /d "%BASE%"
echo [XiangQiLens] 命令行: "%PYN%" "%SCRIPT%" %*
echo.
"%PYN%" "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"
echo.
echo [XiangQiLens] 程序已退出，退出码 %RC%
>>"%LOG%" echo [%date% %time%] args rc=%RC%
echo.
pause
exit /b %RC%


:err_script
echo.
echo [XiangQiLens] 错误: 找不到主程序 app_vision.py
echo     %SCRIPT%
echo.
pause
exit /b 1


:err_pyw
echo.
echo [XiangQiLens] 错误: 找不到 pythonw.exe
echo     %PYW%
echo     请安装 Python 3.13，或修改本文件顶部的 PYW 路径。
echo.
pause
exit /b 1


:err_start
echo.
echo [XiangQiLens] 错误: 启动失败
echo     "%PYW%" "%SCRIPT%"
echo.
pause
exit /b 1
