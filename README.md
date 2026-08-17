# Internship Watcher

Watches these three repos and pings Discord + email when a new listing appears:

- vanshb03/Summer2027-Internships
- SimplifyJobs/Summer2027-Internships
- speedyapply/2027-SWE-College-Jobs

Runs entirely on GitHub Actions (free) — no server needed.

## Setup

**1. Create a new GitHub repo**
Can be public or private. Private is fine — GitHub Actions works the same either way and this doesn't need to be visible to anyone else.

**2. Add these three files, keeping the folder structure**
```
your-repo/
├── check_internships.py
├── requirements.txt
└── .github/
    └── workflows/
        └── check-internships.yml
```
Easiest way: on GitHub, use "Add file → Upload files" and drag all three in (GitHub preserves the `.github/workflows/` path automatically), or clone the empty repo locally and `git add` + `git push`.

**3. Set up a Discord webhook (optional, skip if you only want email)**
- In Discord: Server Settings → Integrations → Webhooks → New Webhook
- Pick the channel, copy the Webhook URL

**4. Set up email (optional, skip if you only want Discord)**
Easiest with Gmail:
- Turn on 2-Step Verification on your Google account
- Go to https://myaccount.google.com/apppasswords and generate an App Password
- Use `smtp.gmail.com`, port `587`, your Gmail address as the user, and the app password (not your real password)

Any SMTP provider works the same way (Outlook, Fastmail, a transactional email service, etc.) — just swap the host/port.

**5. Add repo secrets**
Repo → Settings → Secrets and variables → Actions → New repository secret. Add whichever of these apply:

| Secret | Example |
|---|---|
| `DISCORD_WEBHOOK_URL` | `https://discord.com/api/webhooks/...` |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `you@gmail.com` |
| `SMTP_PASS` | *(the app password)* |
| `EMAIL_FROM` | `you@gmail.com` |
| `EMAIL_TO` | `you@gmail.com` |

You only need Discord secrets, only email secrets, or both — the script skips whichever channel isn't configured.

**6. Run it once manually to build a baseline**
Go to the Actions tab → "Check Internship Listings" → Run workflow (the `workflow_dispatch` trigger). This first run saves every *currently listed* internship to `state.json` **without** notifying you — otherwise you'd get slammed with a notification for every existing row the first time it runs. After this baseline run, `state.json` will appear committed in your repo.

**7. Done**
From here it runs automatically every 15 minutes via the schedule in the workflow file, and you'll get a Discord message / email only for genuinely new listings.

## Adjusting the check frequency
Edit the cron line in `.github/workflows/check-internships.yml`. `*/15 * * * *` = every 15 minutes. GitHub's minimum is every 5 minutes (`*/5 * * * *`), but note GitHub explicitly says scheduled workflows can be delayed during periods of high load, so don't rely on sub-minute precision.

## How duplicate/repeat listings are handled
Each source is tracked separately in `state.json`, keyed by a stable ID:
- **SimplifyJobs** has a real unique `id` per listing in their `listings.json`, so that's used directly — most reliable of the three.
- **vanshb03** and **speedyapply** don't expose structured IDs, so the script uses the job's **application URL** as the ID (extracted from the row). That's what actually identifies a unique posting — not the row's position or the "posted Xd ago" text, which changes daily and would otherwise cause false "new" alerts.
- If a row has no application link (e.g. a 🔒 closed listing with no URL), it falls back to a hash of company + role + location so it still gets a stable ID.

`state.json` stores every ID ever seen (not just the current list), so a listing won't re-trigger a notification if it temporarily disappears and comes back, and Age-column text or table reordering never causes false positives.

**One thing this does *not* do:** dedupe *across* the three repos. If the same real-world internship gets posted to both vanshb03's list and SimplifyJobs' list (common, since they source from overlapping community submissions), you'll get two separate notifications — one per repo — since they're tracked independently. Say the word if you'd like me to add cross-repo dedup (matching on company + role, or normalized apply URL) — it's a fairly small change to `main()`. 
