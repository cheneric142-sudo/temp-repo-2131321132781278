#!/usr/bin/env python3
"""
Internship listing watcher.

Monitors three community-maintained internship-tracking GitHub repos and
sends Discord + email notifications whenever a new listing appears:

  - vanshb03/Summer2027-Internships        (markdown table, branch: dev)
  - SimplifyJobs/Summer2027-Internships    (structured listings.json, branch: dev)
  - speedyapply/2027-SWE-College-Jobs      (markdown tables, branch: main)

State (the set of listing IDs we've already notified about) is persisted to
state.json, which the GitHub Actions workflow commits back to the repo after
every run so state survives between runs.
"""

import hashlib
import json
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
REQUEST_TIMEOUT = 30
DISCORD_BATCH_SIZE = 8  # embeds per Discord message (limit is 10)

SOURCES = {
    "vansh": {
        "label": "vanshb03/Summer2027-Internships",
        "url": "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/README.md",
        "color": 0x3B82F6,  # blue
    },
    "simplify": {
        "label": "SimplifyJobs/Summer2027-Internships",
        "url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
        "color": 0x22C55E,  # green
    },
    "speedyapply": {
        "label": "speedyapply/2027-SWE-College-Jobs",
        "url": "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/README.md",
        "color": 0xF97316,  # orange
    },
}


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_text(url):
    resp = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "internship-watcher (+github actions)"},
    )
    resp.raise_for_status()
    return resp.text


# --------------------------------------------------------------------------
# Markdown table parsing helpers (shared by vansh + speedyapply)
# --------------------------------------------------------------------------

def extract_table_rows(md_text):
    """Return a list of cell-lists for every markdown table row, skipping
    header/separator rows."""
    rows = []
    for line in md_text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        # separator row, e.g. |---|:---:|---|
        if re.fullmatch(r"\|[\s\-:|]+\|?", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or cells[0].strip().lower() in ("company", ""):
            continue
        rows.append(cells)
    return rows


def extract_last_url(cell):
    """Pull the job/apply URL out of a table cell. Handles both markdown
    cells, e.g. [![Apply](img_url)](job_url), and the raw HTML some rows
    use instead, e.g. <a href="job_url"><img src="img_url"/></a>."""
    md_urls = re.findall(r"\]\((https?://[^)]+)\)", cell)
    if md_urls:
        return md_urls[-1]
    html_urls = re.findall(r'href="(https?://[^"]+)"', cell)
    return html_urls[-1] if html_urls else None


def clean_text(cell):
    """Strip markdown/HTML link and bold syntax down to plain text.
    Some rows use markdown ([**Company**](url)), others use raw HTML
    (<a href="url"><strong>Company</strong></a>) - handle both."""
    text = re.sub(r"\*\*\[?([^\]*]+)\]?\*\*", r"\1", cell)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)  # strip any remaining HTML tags
    text = text.replace("*", "").strip()
    return text


def fallback_id(prefix, *parts):
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


# --------------------------------------------------------------------------
# Per-source parsers -> each returns a list of dicts:
#   {id, company, role, location, link, extra}
# --------------------------------------------------------------------------

def parse_vansh(md_text):
    listings = []
    last_company = None
    for cells in extract_table_rows(md_text):
        if len(cells) < 5:
            continue
        company_cell, role, location, link_cell, date_posted = cells[:5]
        company_cell_stripped = company_cell.strip()
        if company_cell_stripped in ("\u21b3", "-", ""):
            company = last_company or "Unknown"
        else:
            company = clean_text(company_cell)
            last_company = company

        url = extract_last_url(link_cell)
        uid = url or fallback_id("vansh", company, role, location, date_posted)

        listings.append(
            {
                "id": uid,
                "company": company,
                "role": clean_text(role),
                "location": location,
                "link": url,
                "extra": date_posted,
            }
        )
    return listings


def parse_speedyapply(md_text):
    listings = []
    for cells in extract_table_rows(md_text):
        if len(cells) < 4:
            continue
        company = clean_text(cells[0])
        role = clean_text(cells[1])
        location = cells[2]
        link_cell = cells[-2]
        age = cells[-1]

        url = extract_last_url(link_cell)
        uid = url or fallback_id("speedyapply", company, role, location)

        listings.append(
            {
                "id": uid,
                "company": company,
                "role": role,
                "location": location,
                "link": url,
                "extra": f"posted {age} ago" if age else None,
            }
        )
    return listings


SIMPLIFY_TARGET_TERM = "Summer 2027"


def parse_simplify(json_text):
    data = json.loads(json_text)
    listings = []
    for entry in data:
        if not entry.get("is_visible", True):
            continue
        # listings.json is Simplify's whole multi-season archive (going back
        # to Summer 2025 and forward to Spring 2028), not just this repo's
        # season - so it must be filtered by term explicitly.
        if SIMPLIFY_TARGET_TERM not in (entry.get("terms") or []):
            continue
        company = entry.get("company_name", "Unknown")
        role = entry.get("title", "")
        location = ", ".join(entry.get("locations", []) or [])
        url = entry.get("url")
        uid = entry.get("id") or url or fallback_id("simplify", company, role, location)
        closed = not entry.get("active", True)

        listings.append(
            {
                "id": uid,
                "company": company,
                "role": role,
                "location": location,
                "link": url,
                "extra": "closed" if closed else None,
            }
        )
    return listings


PARSERS = {
    "vansh": parse_vansh,
    "simplify": parse_simplify,
    "speedyapply": parse_speedyapply,
}


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def truncate(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def send_discord(new_by_source):
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL not set, skipping Discord notification.")
        return

    embeds = []
    for key, listings in new_by_source.items():
        meta = SOURCES[key]
        for job in listings:
            title = truncate(f"{job['company']} — {job['role']}", 256)
            description_lines = [truncate(job["location"], 500)] if job["location"] else []
            if job.get("extra"):
                description_lines.append(str(job["extra"]))
            embed = {
                "title": title,
                "description": "\n".join(description_lines) or None,
                "url": job["link"],
                "color": meta["color"],
                "footer": {"text": meta["label"]},
            }
            embeds.append(embed)

    if not embeds:
        return

    total = len(embeds)
    for i in range(0, total, DISCORD_BATCH_SIZE):
        batch = embeds[i : i + DISCORD_BATCH_SIZE]
        payload = {"embeds": batch}
        if i == 0:
            payload["content"] = f"📢 **{total} new internship listing{'s' if total != 1 else ''} found!**"
        resp = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 300:
            print(f"Discord webhook error {resp.status_code}: {resp.text}", file=sys.stderr)


def send_email(new_by_source):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("EMAIL_TO")
    from_addr = os.environ.get("EMAIL_FROM") or user

    if not all([host, user, password, to_addr]):
        print("SMTP settings not fully configured, skipping email notification.")
        return

    # `or` (not dict.get()'s default) because GitHub Actions sets an env var
    # to an empty string for a secret that was never configured, rather than
    # omitting it - .get(key, "587") wouldn't catch that, but `or` does.
    port = int(os.environ.get("SMTP_PORT") or "587")

    total = sum(len(v) for v in new_by_source.values())
    if total == 0:
        return

    subject = f"{total} new internship listing{'s' if total != 1 else ''} found"

    text_lines = []
    html_lines = ["<html><body>"]
    for key, listings in new_by_source.items():
        if not listings:
            continue
        meta = SOURCES[key]
        text_lines.append(f"\n{meta['label']}\n{'-' * len(meta['label'])}")
        html_lines.append(f"<h3>{meta['label']}</h3><ul>")
        for job in listings:
            line = f"{job['company']} — {job['role']}"
            if job["location"]:
                line += f" ({job['location']})"
            link = job["link"] or ""
            text_lines.append(f"- {line}\n  {link}")
            html_lines.append(
                f"<li><b>{job['company']}</b> — {job['role']}"
                f"{' (' + job['location'] + ')' if job['location'] else ''}"
                f"{' — <a href=\"' + link + '\">apply</a>' if link else ''}</li>"
            )
        html_lines.append("</ul>")
    html_lines.append("</body></html>")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText("\n".join(text_lines), "plain"))
    msg.attach(MIMEText("\n".join(html_lines), "html"))

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=REQUEST_TIMEOUT)
        else:
            server = smtplib.SMTP(host, port, timeout=REQUEST_TIMEOUT)
            server.starttls()
        server.login(user, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())
        server.quit()
        print(f"Email sent to {to_addr}.")
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to send email: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    state = load_state()
    first_run = state is None
    if state is None:
        state = {}

    current_ids = {}
    current_listings = {}

    for key, meta in SOURCES.items():
        try:
            raw = fetch_text(meta["url"])
            listings = PARSERS[key](raw)
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: failed to fetch/parse {meta['label']}: {exc}", file=sys.stderr)
            # keep previous state for this source untouched on failure
            current_ids[key] = set(state.get(key, []))
            current_listings[key] = []
            continue

        ids = {job["id"] for job in listings}
        current_ids[key] = ids
        current_listings[key] = listings
        print(f"{meta['label']}: {len(listings)} listings parsed.")

    if first_run:
        new_state = {key: sorted(ids) for key, ids in current_ids.items()}
        save_state(new_state)
        total = sum(len(v) for v in new_state.values())
        print(f"First run: baseline of {total} existing listings saved. No notifications sent.")
        return

    new_by_source = {}
    for key in SOURCES:
        previously_seen = set(state.get(key, []))
        new_ids = current_ids[key] - previously_seen
        new_by_source[key] = [job for job in current_listings[key] if job["id"] in new_ids]

    total_new = sum(len(v) for v in new_by_source.values())
    print(f"{total_new} new listing(s) found this run.")

    if total_new > 0:
        # Each channel is isolated: a bug or outage in one (e.g. SMTP
        # misconfiguration) must never prevent state.json from being saved,
        # or already-notified listings would get re-notified on every
        # subsequent run until the underlying bug is fixed.
        try:
            send_discord(new_by_source)
        except Exception as exc:  # noqa: BLE001
            print(f"Discord notification failed: {exc}", file=sys.stderr)
        try:
            send_email(new_by_source)
        except Exception as exc:  # noqa: BLE001
            print(f"Email notification failed: {exc}", file=sys.stderr)

    # merge: keep union of all IDs ever seen, per source
    merged_state = {}
    for key in SOURCES:
        merged_state[key] = sorted(set(state.get(key, [])) | current_ids[key])
    save_state(merged_state)


if __name__ == "__main__":
    main()