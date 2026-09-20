@echo off
rem Task Scheduler entry point (Windows). Mirrors update.sh: rebuild the feed
rem and push only when it changed. Any failure exits non-zero before the commit,
rem so the previously published feed stays in place.
rem
rem Registered as the "Boombox calendar feed" task (see README), running:
rem   conhost.exe --headless cmd.exe /c ""%~dp0update.cmd" >> "%~dp0update.log" 2>&1"
setlocal
cd /d "%~dp0" || exit /b 1

for /f "usebackq delims=" %%t in (`powershell -NoProfile -Command "[datetime]::UtcNow.ToString('yyyy-MM-ddTHH:mmZ')"`) do set "STAMP=%%t"
echo == %STAMP%

git pull --rebase --autostash --quiet || (echo git pull failed & exit /b 1)
python boombox_ics.py || (echo build failed - keeping previous feed & exit /b 1)
git add boombox.ics || exit /b 1
git diff --cached --quiet && (echo Feed unchanged & exit /b 0)
git commit --quiet -m "Update feed %STAMP%" || exit /b 1
git push --quiet || (echo git push failed & exit /b 1)
echo Pushed
