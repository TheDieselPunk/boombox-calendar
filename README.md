# boombox-calendar

An iCalendar feed of upcoming events at [The Boombox Miami](https://shotgun.live/en/venues/the-boombox-miami),
rebuilt from Shotgun every 6 hours by a Windows scheduled task and served from GitHub Pages.

**Feed:** `https://thedieselpunk.github.io/boombox-calendar/boombox.ics`
**Landing page:** https://thedieselpunk.github.io/boombox-calendar/

## How it works

1. `boombox_ics.py` loads the venue page to find upcoming event slugs (and the promoter's genre chips).
2. Each event page embeds a `schema.org/MusicEvent` JSON-LD block with real start/end times, address,
   lineup and ticket tiers — that's what goes into each `VEVENT`.
3. `UID` is the Shotgun slug, so a re-run updates or removes events rather than duplicating them.
4. `update.cmd` (Task Scheduler, every 6 h) commits `boombox.ics` only when it changed. If any fetch fails
   the script exits non-zero and the previous feed stays published (a partial feed would make Google
   delete the missing events).

## Why not GitHub Actions?

Shotgun returns `429 Too Many Requests` instantly to GitHub's runner IPs (a datacenter-IP block, not
rate limiting). The workflow in `.github/workflows/` is kept for manual runs only; the scheduled build
has to come from a residential IP.

## Scheduling on Windows (current setup)

The clone lives at `C:\Users\david\OneDrive\Desktop\boombox-calendar`. A scheduled task named
**"Boombox calendar feed"** runs `update.cmd` at 00:17 / 06:17 / 12:17 / 18:17 local time, only while
the user is logged on (so it can use Git Credential Manager for the push), catching up on the next
wake if a run was missed. `conhost --headless` keeps it from flashing a console window. Output goes
to `update.log` in the repo (git-ignored).

Re-create it from an elevated-or-not PowerShell (it registers under the current user):

```powershell
$repo = "C:\Users\david\OneDrive\Desktop\boombox-calendar"
$action  = New-ScheduledTaskAction -Execute "conhost.exe" -WorkingDirectory $repo `
           -Argument "--headless cmd.exe /c `"`"$repo\update.cmd`" >> `"$repo\update.log`" 2>&1`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(17) -RepetitionInterval (New-TimeSpan -Hours 6)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Boombox calendar feed" -Action $action -Trigger $trigger -Settings $settings -Force
```

Useful: `Start-ScheduledTask "Boombox calendar feed"` to run now, `Get-ScheduledTaskInfo "Boombox calendar feed"`
for last run time/result, `Get-Content update.log -Tail 20` for output.

## Alternative: cron on a Linux box

`update.sh` is the same logic for cron (`17 */6 * * *`). It needs a residential IP, a git identity, and
push auth — a write-enabled deploy key scoped to this repo is the cleanest
(`ssh-keygen -t ed25519 -f ~/.ssh/boombox_deploy`, then `gh repo deploy-key add --allow-write`, and
`git config core.sshCommand "ssh -i ~/.ssh/boombox_deploy -o IdentitiesOnly=yes"`).

## Running locally

```bash
python boombox_ics.py                       # -> boombox.ics
python boombox_ics.py --venue <slug> --name "Some Venue" --out other.ics
```

No dependencies beyond the standard library.

## Notes

- Google Calendar polls subscribed URLs on its own schedule, typically every 12–24 hours.
- Shotgun rate-limits bare clients; the script sends a normal browser header set and retries on 429.
