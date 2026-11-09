# LOTR clips to Telegram — GitHub Actions

One-time batch, 3 categories x 10 clips x 10 seconds = 30 clips max.
yt-dlp only, no YouTube Data API key. HD >= 720p, no subtitles, middle-10s trim,
H.264 + AAC + faststart, under 50 MB per clip.

## How long will it take?

Measured on Colab (same script, same work):
- Pilot: 3 clips (1 per category) took **~35 minutes**.
- Full 30 clips: **about 4-6 hours in total if run sequentially**.
- This workflow splits it into **3 parallel jobs, one per category**,
  so each job should take **~1.5-2 hours**. Each job is capped at 6 hours
  by GitHub — that cap cannot be raised.

GitHub free limits (checked Nov 2026):
- Private repo: **2,000 Linux minutes/month**. 3 jobs x ~2 hours = ~360 minutes
  for one full run — well within that, but a second full run in the same month
  would use ~720 minutes total.
- Public repo: standard runners are free, no minute cap.
- One job is killed at **6 hours**. That is why the work is split by category.

## Important — read before you run it

1. **GitHub runners are Azure datacenter IPs, like the VM that was blocked.**
   YouTube may block them the same way ("Sign in to confirm you're not a bot",
   storyboard-only, no HD formats). The workflow now uses your YouTube
   cookies for every search and download, and its HD probe step (also
   with cookies) fails the job if no 720p+ format is found.
   Honest warning: cookies remove the bot-check page, but they do NOT
   change the runner's IP reputation. On the VM and on 7 free proxies,
   valid cookies still returned only storyboard thumbnails and zero HD
   formats. So cookies may help on GitHub's IP, and they may not —
   the probe will tell you in the first minute, before any clips
   are attempted. If the probe fails, do not re-run it repeatedly;
   wait for a cooldown or use Colab, where HD passed.
2. **Do NOT run this at the same time as the Colab full run.**
   Two runs in parallel will download twice, burn the IP twice, and can
   send duplicate clips. Run GitHub only after Colab finishes, or stop Colab first.
3. **Do NOT seed it with the pilot records.** The 3 pilot clips were never
   confirmed received by the user, so this run starts fresh (0 sent IDs)
   and re-sends those 3 as part of the 30. If you upload an old
   `sent_clips.json`, the run will skip those 3 scenes and claim 30/30
   while you only received 27.
4. The token lives only in GitHub Secrets, never in the repo files.

## Setup (5 minutes, once)

1. Create a **new private repo** on GitHub, e.g. `lotr-clips`.
2. Upload these files to it:
   - `lotr_telegram_batch_v2.py` (in the repo root)
   - `.github/workflows/lotr-batch.yml`
   - this `README.md` (optional)
3. In the repo, go to **Settings > Secrets and variables > Actions > New repository secret** and add:
   - Name: `TELEGRAM_BOT_TOKEN`, Value: your bot token
   - Name: `TELEGRAM_CHAT_ID`, Value: your chat ID
   - Name: `YOUTUBE_COOKIES`, Value: the **full contents** of a Netscape-format
     YouTube cookies.txt file. Export it fresh from your desktop browser
     with a cookies.txt extension (e.g. "Get cookies.txt LOCALLY") for
     youtube.com, open the file, copy all of its text, and paste it as
     the Secret value. Its first line must be
     `# Netscape HTTP Cookie File`. Do NOT paste cookie values into any
     file, commit, or workflow — only into this Secret.
   - Never paste any of these into a file, a commit, or a public repo.
   - The workflow writes the cookies to `youtube_cookies.txt` (mode 600)
     at runtime, passes `--cookies-file youtube_cookies.txt` to every
     yt-dlp search and download, and deletes the file at the end
     (even if the job fails). It never prints the cookie values.
4. Go to **Actions > LOTR clips to Telegram > Run workflow**.
   - `max_per_category`: 10 (or 1 for a pilot)
   - `dry_run`: false for a real run, true to test cutting without uploading
   - Click **Run workflow**.
5. Watch the 3 jobs. Each one logs its own category, progress, and
   `Uploaded OK, message_id=...` lines. When all 3 finish, check your
   Telegram DM for the clips.
6. After the run, download the `lotr-records-<category>` artifacts if you
   want the tracking files (`sent_clips.json`, `sent_lotr_3cat_10s.json`).
   Colab / GitHub runners have no persistent disk — the files are gone
   when the job ends unless you download the artifact.

## Files

- `lotr_telegram_batch_v2.py` — v2 script, plus a `--category` flag
  (`all` / `landscape` / `battle` / `iconic`) for the matrix jobs.
  Everything else is identical to the Colab version.
- `.github/workflows/lotr-batch.yml` — the workflow above.

## Fixes after the first failed run (2026-11-09 ~19:05)

The first run failed in 38s with "Missing credentials" because the
Secrets were not set in the repo. The workflow now checks for that
first, before installing anything, and fails with a clear message
telling you exactly where to add the two Secrets.

Two more real bugs were found in the audit and fixed:
1. **HD probe did not actually gate the run.** It piped `yt-dlp -F` to
   `head`, whose exit code is always 0, so the step passed green even
   on a blocked runner (that is why it showed 1s / green in the failed
   run). It now saves the output, fails on "Sign in to confirm", and
   fails unless a 720p+ format is listed.
2. **Upload tracking files could fail when no files exist** (e.g. after
   an early failure). It now uses `if-no-files-found: warn` instead
   of failing the job.
