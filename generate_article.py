#!/usr/bin/env python3
"""
WordPress article generator for growthmedia.gr
Reads today's assignment from 'GOI Content Calendar' Google Sheet,
generates the article via Claude Code CLI, publishes to WordPress,
and writes the article URL back to column E.

Usage:
    python3 generate_article.py              # auto-mode: reads from Google Sheets
    python3 generate_article.py trending     # legacy override (no sheet lookup)
    python3 generate_article.py evergreen    # legacy override (no sheet lookup)
"""

import sys
import subprocess
import logging
import os
import re
import json
import requests
from datetime import datetime

LOG_FILE = "/home/ubuntu/article_generation.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

CLAUDE_BIN = os.path.expanduser("~/.npm-global/bin/claude")
TOKENS_DIR = "/home/ubuntu/tokens"

WP_URL = "https://growthmedia.gr"
WP_USER = "argiro"
WP_PASS = "N8vt b412 UXWZ a7Xx t3AA KbgY"

# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

def _refresh_token(token_file: str) -> str | None:
    """Refresh an OAuth token and return a fresh access token, or None on failure."""
    try:
        with open(token_file) as f:
            data = json.load(f)
        resp = requests.post(data["token_uri"], data={
            "client_id": data["client_id"],
            "client_secret": data["client_secret"],
            "refresh_token": data["refresh_token"],
            "grant_type": "refresh_token",
        }, timeout=15)
        resp.raise_for_status()
        return resp.json()["access_token"]
    except Exception as exc:
        logging.warning("Token refresh failed for %s: %s", token_file, exc)
        return None


def _get_access_token() -> str:
    """Try all token files in TOKENS_DIR and return the first working access token."""
    try:
        token_files = [
            os.path.join(TOKENS_DIR, f)
            for f in os.listdir(TOKENS_DIR)
            if f.endswith(".json")
        ]
    except FileNotFoundError:
        raise RuntimeError(f"Tokens directory not found: {TOKENS_DIR}")

    for tf in sorted(token_files):
        token = _refresh_token(tf)
        if token:
            logging.info("Refreshed token from %s", os.path.basename(tf))
            return token

    raise RuntimeError("All token files failed to refresh. Cannot connect to Google Sheets.")


def _find_spreadsheet_id(access_token: str, name: str) -> str:
    """Search Google Drive for a spreadsheet by exact name and return its ID."""
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(
        "https://www.googleapis.com/drive/v3/files",
        headers=headers,
        params={
            "q": f"name='{name}' and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
            "fields": "files(id,name)",
            "pageSize": 5,
        },
        timeout=15,
    )
    resp.raise_for_status()
    files = resp.json().get("files", [])
    if not files:
        raise RuntimeError(f"Spreadsheet '{name}' not found in Google Drive.")
    spreadsheet_id = files[0]["id"]
    logging.info("Found spreadsheet '%s' with ID: %s", name, spreadsheet_id)
    return spreadsheet_id


def _read_sheet_values(access_token: str, spreadsheet_id: str, sheet_range: str = "A:E") -> list[list[str]]:
    """Read values from a sheet range and return as a list of rows."""
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{sheet_range}",
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("values", [])


def _update_cell(access_token: str, spreadsheet_id: str, cell: str, value: str) -> None:
    """Write a single value to a cell (e.g. 'E3')."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    resp = requests.put(
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{cell}",
        headers=headers,
        params={"valueInputOption": "USER_ENTERED"},
        json={"values": [[value]]},
        timeout=15,
    )
    resp.raise_for_status()
    logging.info("Updated cell %s with value: %s", cell, value)


# ---------------------------------------------------------------------------
# Sheet lookup
# ---------------------------------------------------------------------------

def get_today_assignment() -> dict | None:
    """
    Connect to Google Sheets, find 'GOI Content Calendar', and return the row
    whose column A matches today's date (DD/MM/YYYY) and column E is empty.

    Returns a dict with keys: topic, keyword, article_type, row_index, spreadsheet_id, access_token
    or None if no matching row is found.
    """
    today_str = datetime.now().strftime("%d/%m/%Y")
    logging.info("Looking for sheet row with date: %s", today_str)

    access_token = _get_access_token()
    spreadsheet_id = _find_spreadsheet_id(access_token, "GOI Content Calendar")
    rows = _read_sheet_values(access_token, spreadsheet_id)

    for i, row in enumerate(rows):
        if not row:
            continue
        cell_date = row[0].strip() if len(row) > 0 else ""
        cell_url = row[4].strip() if len(row) > 4 else ""

        if cell_date == today_str and cell_url == "":
            topic = row[1].strip() if len(row) > 1 else ""
            keyword = row[2].strip() if len(row) > 2 else ""
            article_type = row[3].strip().lower() if len(row) > 3 else "trending"

            logging.info(
                "Found assignment: date=%s, topic=%s, keyword=%s, type=%s (sheet row %d)",
                today_str, topic, keyword, article_type, i + 1,
            )
            return {
                "topic": topic,
                "keyword": keyword,
                "article_type": article_type,
                "row_index": i + 1,   # 1-based sheet row number
                "spreadsheet_id": spreadsheet_id,
                "access_token": access_token,
            }

    logging.info("No unpublished row found for date %s in 'GOI Content Calendar'.", today_str)
    return None


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def build_prompt(article_type: str, topic: str = "", keyword: str = "") -> str:
    """Build the Claude prompt for a given article type, topic and keyword."""

    if topic and keyword:
        research_section = f"""
## STEP 1 — RESEARCH
The topic for today's article has already been assigned: **{topic}**
The focus keyphrase is: **{keyword}**

Use the WebSearch tool to:
- Find the latest data, statistics and trends about this topic (within the last 6 months where possible)
- Find at least 3 authoritative sources to cite (industry reports, major publications, .gov or .edu sites)
- Check what competitors are ranking for on the target keyphrase "{keyword}"
- Identify related LSI keywords and synonyms to use naturally in the article
"""
    else:
        if article_type == "trending":
            research_section = """
## STEP 1 — RESEARCH
Use the WebSearch tool to find the top trending digital marketing topics in Greece and globally this week (April 2026).
Pick the single best trending topic with a rising search volume (trending upward right now).
Find at least 3 authoritative sources (industry reports, major publications) to cite.
Include recent data published within the last 30 days.
Check what competitors are ranking for on the target keyword.
Identify a strong 2-4 word Greek focus keyphrase for SEO.
Identify related LSI keywords and synonyms to use naturally in the article.
"""
        else:
            research_section = """
## STEP 1 — RESEARCH
Use the WebSearch tool to find evergreen digital marketing topics relevant to Greek marketing professionals.
Choose a timeless topic with high search volume and low competition that will remain relevant for 12+ months.
Find at least 3 authoritative sources (industry reports, major publications) to cite.
Check what competitors are ranking for on the target keyword.
Identify a strong 2-4 word Greek focus keyphrase for SEO.
Identify related LSI keywords and synonyms to use naturally in the article.
"""

    slug_hint = "digital-marketing-greece-2026" if article_type == "trending" else "content-marketing-stratigi"
    type_note = "trending" if article_type == "trending" else "evergreen / timeless"

    return f"""
You are an expert Greek-language digital marketing writer for the website growthmedia.gr.
Write with authority and expertise (E-E-A-T: Experience, Expertise, Authority, Trust).

Your task today is to research, write, and publish ONE {type_note} digital marketing article IN GREEK LANGUAGE to WordPress.

{research_section}

## STEP 2 — WRITE THE ARTICLE (STRICTLY IN GREEK)
Write a complete article following ALL rules below:

LANGUAGE: Greek (Ελληνικά) — every word of the article must be in Greek.
WORD COUNT: Minimum 800 words, ideal 1000-1200 words. Never go below 600 words under any circumstances.
TITLE (H1): Must contain the focus keyphrase. 50-60 characters maximum. Compelling and click-worthy. In Greek.
INTRODUCTION (first 100 words): First paragraph MUST contain the focus keyphrase. Hook the reader immediately. Tell them what they will learn.
SUBHEADINGS:
  - At least 3 H2 subheadings.
  - At least ONE H2 must contain the focus keyphrase or a close variant.
  - Subheadings must be descriptive and informative. All in Greek.
BODY CONTENT:
  - Focus keyphrase appears naturally every 100-150 words — do NOT keyword stuff.
  - Use synonyms and related LSI terms naturally throughout subheadings and body.
  - Sentences maximum 20 words each — no exceptions.
  - Active voice throughout — passive voice must be less than 10% of sentences.
  - Short paragraphs: maximum 3-4 sentences per paragraph.
  - Use bullet points and numbered lists where appropriate.
  - Include real data, statistics, and facts with sources cited.
  - Write from a position of expertise — demonstrate experience and authority on the topic.
  - Include the publication date context (April 2026) where relevant.
  - Aim for a Flesch Reading Ease score of 60-80: clear, readable prose.
  - Vary sentence length for rhythm.
  - Use transition words (ωστόσο, επομένως, επιπλέον, συνεπώς, επιπροσθέτως, κατά συνέπεια, παρ' όλα αυτά, αντίθετα) in at least 30% of sentences.
  - Simple language: write at maximum 8th grade reading level, as if explaining to a 10-year-old. Use short, everyday words. Avoid jargon. If a technical term is needed, explain it right away in plain words.
  - NO bold text (<strong>) in body paragraphs or lists. Bold is ONLY allowed inside heading tags (H1, H2, H3).
  - NO em-dashes (—) anywhere. Replace every em-dash with a comma or a period.
LINKS:
  - Minimum 2 outbound links to authoritative sources (industry reports, .gov, .edu, or major publications). Use real URLs found during research.
  - Minimum 3 internal links to other pages or articles on growthmedia.gr (e.g. href="https://growthmedia.gr/blog/", href="https://growthmedia.gr/digital-marketing/", etc.). Use relevant anchor text.
  - All links must be relevant and add value.
CONCLUSION: Summarize key takeaways. Include a clear Call to Action in Greek. Repeat the focus keyphrase one final time.

FORMATTING RULES (STRICT):
  - NO bold text (<strong> or **) in the article body (paragraphs, lists). Bold formatting is ONLY allowed in headings (H1, H2, H3).
  - NO em-dashes (—) anywhere in the article. Replace every em-dash with a comma or a period.
  - Simple language: write at a maximum 8th grade reading level, as if explaining to a 10-year-old. Use short, common, everyday words. Avoid technical jargon. If you must use a technical term, explain it immediately in plain words.

## STEP 3 — YOAST SEO FIELDS
Prepare ALL of these (in Greek where applicable):
- Focus keyphrase: 2-4 words, in Greek — must appear in: title, introduction, at least one H2, meta description, and slug.
- SEO title: MUST START with the focus keyphrase, 50-60 characters maximum, format: [Keyphrase]: [Description] | Growthmedia
- Meta description: EXACTLY 120-155 characters (count carefully), contains focus keyphrase, compelling and encourages clicks, summarizes the article accurately, in Greek.
- Slug: focus keyphrase in Latin transliteration, lowercase, hyphens between words, no stop words (a, the, and, or, etc.), e.g. {slug_hint}

## STEP 4 — PRE-PUBLISH CHECKLIST
Before making the WordPress API call, verify EVERY item below is true. Fix anything that is not:

  [ ] Word count >= 600 (aim for 800-1200)
  [ ] Focus keyphrase in H1 title
  [ ] Focus keyphrase in first paragraph (introduction)
  [ ] Focus keyphrase in at least 1 H2 subheading
  [ ] Focus keyphrase in meta description
  [ ] Focus keyphrase starts the SEO title
  [ ] Focus keyphrase in slug
  [ ] Meta description is 120-155 characters (count the exact characters)
  [ ] At least 2 outbound links to authoritative sources included
  [ ] At least 3 internal links to growthmedia.gr pages included
  [ ] No sentences longer than 20 words
  [ ] Active voice used throughout (passive < 10%)
  [ ] Short paragraphs (max 3-4 sentences)
  [ ] At least 3 H2 subheadings
  [ ] Conclusion with CTA included
  [ ] All Yoast fields ready and set as TOP-LEVEL keys (not in meta): yoast_focus_keyword, yoast_seo_title, yoast_meta_description
  [ ] No keyword stuffing — keyphrase used naturally
  [ ] NO bold text in body paragraphs or lists (bold only in headings)
  [ ] NO em-dashes (—) anywhere in the article
  [ ] Simple language throughout (max 8th grade level, clear everyday words)
  [ ] Unsplash featured image fetched, uploaded, and set as featured_media
  [ ] Featured image img tag inserted in content immediately after the H1 tag (before first paragraph), with alt text containing the focus keyphrase

Do NOT proceed to publishing if any item is unchecked. Fix it first.

## STEP 5 — FEATURED IMAGE FROM UNSPLASH
Before publishing the post, fetch a free image from Unsplash and upload it as the featured image:

1. Search Unsplash for a relevant image:
   GET https://api.unsplash.com/search/photos?query=<article-topic-in-english>&per_page=1&orientation=landscape&client_id=YOUR_CLIENT_ID
   (Use a descriptive English query based on the article topic. If no Unsplash API key is available, use the public demo endpoint or skip to step 2 with a direct photo URL.)
   Pick the first result. Use its `urls.regular` field as the image URL.

2. Download the image bytes using Python requests:
   img_response = requests.get(image_url, timeout=30)

3. Upload to WordPress Media Library:
   POST {WP_URL}/wp-json/wp/v2/media
   Headers:
     Authorization: Basic <base64(username:password)>
     Content-Disposition: attachment; filename="<slug>.jpg"
     Content-Type: image/jpeg
   Body: the raw image bytes

4. From the media upload response, get the media `id`.

5. Update the media record with SEO metadata:
   POST {WP_URL}/wp-json/wp/v2/media/<id>
   JSON body:
     alt_text: "<focus keyphrase> — [brief description of image]" (max 125 characters, contains focus keyphrase)
     title: "<focus keyphrase> image"
     caption: "Photo: Unsplash"

6. Build an HTML img tag to embed in the article content:
   <img src="<image_url>" alt="<focus keyphrase> — [brief description]" title="<focus keyphrase>" class="wp-post-image" style="width:100%;height:auto;margin-bottom:1.5em;" />
   The alt text MUST contain the focus keyphrase and be max 125 characters.

7. Include the media id as `featured_media` in the post creation payload.

## STEP 6 — PUBLISH TO WORDPRESS VIA REST API
Use the following WordPress credentials to publish the article as a DRAFT:
  - WordPress URL: {WP_URL}
  - Username: {WP_USER}
  - Application Password: {WP_PASS}

Use the WordPress REST API endpoint: {WP_URL}/wp-json/wp/v2/posts
Method: POST with Basic Authentication (base64 of username:password).

The JSON payload must include:
  - title: the H1 title (in Greek)
  - content: the full HTML article body. Structure it EXACTLY as follows:
      1. The H1 tag: <h1>...</h1>
      2. Immediately after the H1, the featured image img tag built in Step 5 (NO text between H1 and img)
      3. Then the introduction paragraph and the rest of the article
    Use <h2>, <h3>, <p>, <ul>, <li> tags — NO <strong> in body text, only in headings
  - status: "draft"
  - slug: the SEO slug
  - excerpt: first 2 sentences of the article
  - categories: relevant category IDs (look up or create appropriate categories via the API)
  - tags: relevant tag IDs (look up or create appropriate tags via the API)
  - Yoast SEO fields as TOP-LEVEL keys in the JSON payload (NOT nested inside "meta"):
      "yoast_focus_keyword": "<the 2-4 word focus keyphrase>",
      "yoast_seo_title": "<SEO title starting with keyphrase, 50-60 chars>",
      "yoast_meta_description": "<meta description, exactly 120-155 chars>"
    IMPORTANT: these must be sent as direct keys at the root of the JSON body, NOT inside a "meta" object. Example payload structure:
      {{"title": "...", "content": "...", "status": "draft", "yoast_focus_keyword": "...", "yoast_seo_title": "...", "yoast_meta_description": "..."}}
  - featured_media: the media ID from Step 5

Use Python's `requests` library or `urllib` to make the API call.

After publishing, output a line in EXACTLY this format so it can be parsed:
PUBLISHED_URL: <the full URL of the draft post>

Example: PUBLISHED_URL: https://growthmedia.gr/?p=1234

Log the result clearly at the end.

## ABSOLUTE PROHIBITIONS — NEVER DO THESE
- Never publish below 600 words
- Never leave any Yoast field empty, and always send them as TOP-LEVEL keys in the REST API payload, never nested inside "meta" (fields: yoast_focus_keyword, yoast_seo_title, yoast_meta_description)
- Never use passive voice in more than 10% of sentences
- Never write a sentence longer than 20 words
- Never keyword stuff — use the keyphrase naturally
- Never publish without at least 2 outbound links
- Never publish without at least 3 internal links to growthmedia.gr
- Never publish without the focus keyphrase in the introduction
- Never skip the pre-publish checklist (Step 4)
- Never publish as status "publish" — always use "draft" unless explicitly instructed otherwise
- Never use bold text (<strong>) in body paragraphs or lists — bold is for headings only
- Never use em-dashes (—) in the article — use commas or periods instead
- Never write above 8th grade reading level — keep all language simple and easy to understand
- Never publish without a featured image fetched from Unsplash and uploaded to WordPress
- Never publish without the featured image img tag inserted in the article content immediately after the H1 tag and before the first paragraph, with SEO alt text containing the focus keyphrase
"""


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

def extract_published_url(output: str) -> str | None:
    """Parse the PUBLISHED_URL line from Claude's stdout."""
    match = re.search(r"PUBLISHED_URL:\s*(https?://\S+)", output)
    if match:
        return match.group(1).rstrip(".")
    # Fallback: find any growthmedia.gr URL in the output
    match = re.search(r"(https?://growthmedia\.gr/[^\s\"'<>]+)", output)
    if match:
        return match.group(1).rstrip(".")
    return None


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(article_type: str, topic: str = "", keyword: str = "",
        spreadsheet_id: str = "", row_index: int = 0, access_token: str = "") -> int:

    prompt = build_prompt(article_type, topic, keyword)

    logging.info(
        "Starting %s article generation for growthmedia.gr (topic=%s, keyword=%s)",
        article_type, topic or "(auto)", keyword or "(auto)",
    )

    cmd = [
        CLAUDE_BIN,
        "--dangerously-skip-permissions",
        "-p",
        prompt,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=900,  # 15 minutes max
            env={**os.environ, "HOME": os.path.expanduser("~")},
        )

        if result.stdout:
            logging.info("Claude output:\n%s", result.stdout[-3000:])

        if result.returncode != 0:
            logging.error(
                "Claude exited with code %d. stderr:\n%s",
                result.returncode, result.stderr[-2000:],
            )
            return result.returncode

        logging.info("Article generation completed successfully (type=%s)", article_type)

        # Update Google Sheet column E if we have a sheet reference
        if spreadsheet_id and row_index and access_token:
            url = extract_published_url(result.stdout)
            if url:
                cell = f"E{row_index}"
                try:
                    _update_cell(access_token, spreadsheet_id, cell, url)
                    logging.info("Sheet updated: row %d column E = %s", row_index, url)
                    print(f"Sheet updated: {cell} = {url}")
                except Exception as exc:
                    logging.error("Failed to update Google Sheet: %s", exc)
                    print(f"Warning: could not update sheet cell {cell}: {exc}")
            else:
                logging.warning("Could not extract published URL from Claude output to update sheet.")
                print("Warning: published URL not found in Claude output — sheet not updated.")

        return 0

    except subprocess.TimeoutExpired:
        logging.error("Claude process timed out after 15 minutes for type=%s", article_type)
        return 2
    except FileNotFoundError:
        logging.error("Claude CLI not found at %s", CLAUDE_BIN)
        return 3
    except Exception as exc:
        logging.error("Unexpected error: %s", exc)
        return 4


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = sys.argv[1:]

    if args and args[0] in ("trending", "evergreen"):
        # Legacy / manual override — no sheet lookup
        article_type = args[0]
        print(f"Legacy mode: generating {article_type} article without sheet lookup.")
        sys.exit(run(article_type))

    # Auto mode: read assignment from Google Sheets
    print("Auto mode: looking up today's assignment in 'GOI Content Calendar'...")
    try:
        assignment = get_today_assignment()
    except Exception as exc:
        logging.error("Google Sheets lookup failed: %s", exc)
        print(f"Error: could not read Google Sheets — {exc}")
        sys.exit(1)

    if assignment is None:
        print("No article scheduled for today (or already published). Nothing to do.")
        sys.exit(0)

    print(
        f"Assignment found: [{assignment['article_type']}] "
        f"topic='{assignment['topic']}', keyword='{assignment['keyword']}'"
    )

    sys.exit(run(
        article_type=assignment["article_type"],
        topic=assignment["topic"],
        keyword=assignment["keyword"],
        spreadsheet_id=assignment["spreadsheet_id"],
        row_index=assignment["row_index"],
        access_token=assignment["access_token"],
    ))
