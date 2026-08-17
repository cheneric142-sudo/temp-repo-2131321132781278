#!/usr/bin/env python3
"""
Off-season internship listing watcher.

Monitors SimplifyJobs' off-season internship tracker (Fall/Winter/Spring
roles, tracked separately from their Summer README) and sends Discord +
email notifications whenever a new listing appears.

  SimplifyJobs/Summer2027-Internships, README-Off-Season.md, branch: dev

Structurally this source is unlike vansh/simplify's-JSON/speedyapply:
listings live in raw HTML <table> blocks (not markdown pipe tables, not
JSON), grouped under category headings (Software Engineering, Product
Management, Data Science/AI/ML, Quantitative Finance, Hardware
Engineering), with closed listings collapsed into
<details>"Inactive roles"</details> blocks per category. Those are
deliberately skipped - there's no value in being notified about a listing
that's already closed.

State (the set of listing IDs already notified about) persists to
STATE_FILE, which the GitHub Actions workflow commits back to the repo
after every run.
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

STATE_FILE = os.environ.get("STATE_FILE", "state-offseason.json")
REQUEST_TIMEOUT = 30
DISCORD_BATCH_SIZE = 8  # embeds per Discord message (limit is 10)

SOURCE_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README-Off-Season.md"
SOURCE_LABEL = "SimplifyJobs/Summer2027-Internships (Off-Season)"
EMBED_COLOR = 0xA855F7  # purple - distinct from the other watcher's per-source colors


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_text(url):
    resp = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "internship-watcher-offseason (+github actions)"},
    )
    resp.raise_for_status()
    return resp.text


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def strip_tags(text):
    return re.sub(r"<[^>]+>", "", text).strip()


def clean_location(cell):
    """Locations are <br>-separated within the cell, e.g.
    'Boston, MA<br>Peabody, MA' -> 'Boston, MA, Peabody, MA'."""
    parts = [p.strip() for p in re.split(r"<br\s*/?>", cell) if p.strip()]
    return ", ".join(parts)


def category_name_from_heading(heading_line):
    """'\U0001F4BB Software Engineering Internship Roles' -> 'Software Engineering'.
    Strips emoji/symbols by keeping only letters, spaces, commas and '&',
    then drops the constant 'Internship Roles' suffix."""
    text = re.sub(r"[^A-Za-z ,&]", "", heading_line)
    text = re.sub(r"\s*Internship Roles\s*$", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text or "General"


def split_by_category(html_text):
    """Split the document into (heading, section_text) pairs on '## ' headings."""
    chunks = re.split(r"\n## ", html_text)
    sections = []
    for chunk in chunks[1:]:  # chunks[0] is boilerplate before the first heading
        heading, _, body = chunk.partition("\n")
        sections.append((heading, body))
    return sections


def fallback_id(*parts):
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"offseason-{digest}"


def parse_offseason(html_text):
    listings = []

    # Drop every <details>...</details> block up front - these hold the
    # collapsed "Inactive roles" tables for each category, which this
    # watcher never surfaces.
    active_html = re.sub(r"<details>.*?</details>", "", html_text, flags=re.S)

    for heading, section in split_by_category(active_html):
        category = category_name_from_heading(heading)
        last_company = None

        for row in re.findall(r"<tr>(.*?)</tr>", section, re.S):
            cells = re.findall(r"<td>(.*?)</td>", row, re.S)
            if len(cells) < 6:
                continue  # header row or malformed row
            company_cell, role_cell, location_cell, terms_cell, app_cell, _age_cell = [
                c.strip() for c in cells[:6]
            ]

            if company_cell in ("", "\u21b3"):
                company = last_company or "Unknown"
            else:
                name_match = re.search(r">([^<]+)</a>", company_cell)
                company = name_match.group(1).strip() if name_match else strip_tags(company_cell)
                last_company = company

            role = strip_tags(role_cell)
            location = clean_location(location_cell)
            terms = strip_tags(terms_cell)

            if "\U0001F512" in app_cell or 'href="' not in app_cell:
                # Closed listing surfaced outside a <details> block (rare,
                # but seen in the wild) - skip, same as the collapsed ones.
                continue

            # The Application cell holds the employer's own "Apply" link
            # followed by a Simplify referral link, in that fixed order,
            # whenever both are present - confirmed against the live file.
            # Take the first: it's the real apply link, not the tracking one.
            urls = re.findall(r'href="([^"]+)"', app_cell)
            url = urls[0] if urls else None
            uid = url or fallback_id(company, role, location, terms)

            listings.append(
                {
                    "id": uid,
                    "company": company,
                    "role": role,
                    "location": location,
                    "link": url,
                    "extra": f"{terms} \u00b7 {category}" if terms else category,
                }
            )

    return listings


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
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def send_discord(new_listings):
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL_OFFSEASON")
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL_OFFSEASON not set, skipping Discord notification.")
        return

    embeds = []
    for job in new_listings:
        title = truncate(f"{job['company']} \u2014 {job['role']}", 256)
        description_lines = [truncate(job["location"], 500)] if job["location"] else []
        if job.get("extra"):
            description_lines.append(str(job["extra"]))
        embeds.append(
            {
                "title": title,
                "description": "\n".join(description_lines) or None,
                "url": job["link"],
                "color": EMBED_COLOR,
                "footer": {"text": SOURCE_LABEL},
            }
        )

    if not embeds:
        return

    total = len(embeds)
    for i in range(0, total, DISCORD_BATCH_SIZE):
        batch = embeds[i : i + DISCORD_BATCH_SIZE]
        payload = {"embeds": batch}
        if i == 0:
            payload["content"] = (
                f"\U0001F4E2 **{total} new off-season internship listing"
                f"{'s' if total != 1 else ''} found!**"
            )
        resp = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 300:
            print(f"Discord webhook error {resp.status_code}: {resp.text}", file=sys.stderr)


def send_email(new_listings):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("EMAIL_TO")
    from_addr = os.environ.get("EMAIL_FROM") or user

    if not all([host, user, password, to_addr]):
        print("SMTP settings not fully configured, skipping email notification.")
        return

    # `or`, not dict.get()'s default: GitHub Actions sets an env var to an
    # empty string for a secret that was never configured, rather than
    # omitting it, so .get(key, "587") wouldn't catch that - `or` does.
    port = int(os.environ.get("SMTP_PORT") or "587")

    total = len(new_listings)
    if total == 0:
        return

    subject = f"{total} new off-season internship listing{'s' if total != 1 else ''} found"

    text_lines = [SOURCE_LABEL, "-" * len(SOURCE_LABEL)]
    html_lines = ["<html><body>", f"<h3>{SOURCE_LABEL}</h3><ul>"]
    for job in new_listings:
        line = f"{job['company']} \u2014 {job['role']}"
        if job["location"]:
            line += f" ({job['location']})"
        link = job["link"] or ""
        text_lines.append(f"- {line}\n  {link}")
        html_lines.append(
            f"<li><b>{job['company']}</b> \u2014 {job['role']}"
            f"{' (' + job['location'] + ')' if job['location'] else ''}"
            f"{' \u2014 <a href=\"' + link + '\">apply</a>' if link else ''}</li>"
        )
    html_lines.append("</ul></body></html>")

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
        state = {"seen_ids": []}

    try:
        raw = fetch_text(SOURCE_URL)
        listings = parse_offseason(raw)
    except Exception as exc:  # noqa: BLE001
        # Single-source watcher: a failed fetch means nothing useful can be
        # done this run. Fail loudly (non-zero exit) rather than silently
        # continuing, so a broken/moved source shows up as a red X in the
        # Actions tab instead of going unnoticed. state.json is untouched.
        print(f"ERROR: failed to fetch/parse {SOURCE_LABEL}: {exc}", file=sys.stderr)
        sys.exit(1)

    current_ids = {job["id"] for job in listings}
    print(f"{SOURCE_LABEL}: {len(listings)} active listings parsed.")

    if first_run:
        save_state({"seen_ids": sorted(current_ids)})
        print(f"First run: baseline of {len(current_ids)} existing listings saved. No notifications sent.")
        return

    previously_seen = set(state.get("seen_ids", []))
    new_ids = current_ids - previously_seen
    new_listings = [job for job in listings if job["id"] in new_ids]

    print(f"{len(new_listings)} new listing(s) found this run.")

    if new_listings:
        # Each channel is isolated: a bug or outage in one (e.g. SMTP
        # misconfiguration) must never prevent state from being saved, or
        # already-notified listings would get re-notified on every
        # subsequent run until the underlying bug is fixed.
        try:
            send_discord(new_listings)
        except Exception as exc:  # noqa: BLE001
            print(f"Discord notification failed: {exc}", file=sys.stderr)
        try:
            send_email(new_listings)
        except Exception as exc:  # noqa: BLE001
            print(f"Email notification failed: {exc}", file=sys.stderr)

    merged_ids = previously_seen | current_ids
    save_state({"seen_ids": sorted(merged_ids)})


if __name__ == "__main__":
    main()