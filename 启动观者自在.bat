@echo off
cd /d "%~dp0"
set "ANA="
for %%D in ("%USERPROFILE%\anaconda3" "%USERPROFILE%\Anaconda3" "%USERPROFILE%\miniconda3" "%LOCALAPPDATA%\anaconda3" "%LOCALAPPDATA%\Continuum\anaconda3" "%ProgramData%\anaconda3" "%ProgramData%\Anaconda3" "%ProgramData%\miniconda3" "C:\anaconda3" "D:\anaconda3" "E:\anaconda3" "D:\Anaconda3" "E:\Anaconda3") do (
  if not defined ANA if exist "%%~D\python.exe" set "ANA=%%~D"
)
if defined ANA (
  call "%ANA%\Scripts\activate.bat" "%ANA%" >nul 2>nul
  start "" "%ANA%\pythonw.exe" -m guanzhe app
  exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw -m guanzhe app
  exit /b 0
)
echo 没找到 Python（Anaconda）。
echo 请把这个窗口截图发给 Claude，并在 Anaconda Prompt 里输入 where python，把结果也截图发过去。
pause
