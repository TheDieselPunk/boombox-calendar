@echo off
rem Task Scheduler entry point (Windows). Mirrors update.sh: rebuild the feeds
rem and push only when something changed. Any failure exits non-zero before the
rem commit, so the previously published feeds stay in place.
rem
rem Registered as the "Miami calendar feeds" task (see README), running:
rem   conhost.exe --headless cmd.exe /c ""%~dp0update.cmd" >> "%~dp0update.log" 2>&1"
setlocal
cd /d "%~dp0" || exit /b 1

for /f "usebackq delims=" %%t in (`powershell -NoProfile -Command "[datetime]::UtcNow.ToString('yyyy-MM-ddTHH:mmZ')"`) do set "STAMP=%%t"
echo == %STAMP%

git pull --rebase --autostash --quiet || (echo git pull failed & exit /b 1)
python build_feeds.py || (echo build failed - keeping previous feeds & exit /b 1)
git add *.ics events.json feeds.json || exit /b 1
git diff --cached --quiet && (echo Feeds unchanged & exit /b 0)
git commit --quiet -m "Update feeds %STAMP%" || exit /b 1
git push --quiet || (echo git push failed & exit /b 1)
echo Pushed
