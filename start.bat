@echo off
REM 统一入口：自动检测系统，打开角色选择页
REM 控制端：普通运行即可；被控端目前只支持 Ubuntu / Linux
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0app\pair_console.py" %*
    goto :done
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%~dp0app\pair_console.py" %*
    goto :done
)

echo 没找到 Python 3。请先安装 Python，或在命令行里指定解释器路径。
pause
exit /b 1

:done
if errorlevel 1 pause
