#!/usr/bin/env python3
"""
WordPress article generator for growthmedia.gr and chrisfountoulis.com.
Reads today's assignment from the respective Google Sheet, generates the
article via Claude Code CLI, publishes to WordPress, and writes the URL
back to column E.

On Sundays, sends a summary email covering both sites.

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
import base64
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

LOG_FILE = "/home/ubuntu/article_generation.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

CLAUDE_BIN = os.path.expanduser("~/.npm-global/bin/claude")
TOKENS_DIR = "/home/ubuntu/tokens"

# growthmedia.gr credentials
WP_URL  = "https://growthmedia.gr"
WP_USER = "argiro"
WP_PASS = "N8vt b412 UXWZ a7Xx t3AA KbgY"

# chrisfountoulis.com credentials
CF_WP_URL  = "https://chrisfountoulis.com"
CF_WP_USER = "fountoulisc@gmail.com"
CF_WP_PASS = "kZK4 8biH 93g5 CVCU Pd3H vcLS"

# Sunday summary email recipient
SUMMARY_EMAIL_TO = "chris@chrisfountoulis.com"

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


def get_cf_assignment() -> dict | None:
    """
    Connect to Google Sheets, find 'Chris Fountoulis Content Calendar', and return
    the row whose column A matches today's date (DD/MM/YYYY) and column E is empty.

    Returns a dict with keys: topic, keyword, article_type, row_index, spreadsheet_id, access_token
    or None if no matching row is found.
    """
    today_str = datetime.now().strftime("%d/%m/%Y")
    logging.info("[CF] Looking for sheet row with date: %s", today_str)

    access_token = _get_access_token()
    spreadsheet_id = _find_spreadsheet_id(access_token, "Chris Fountoulis Content Calendar")
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
                "[CF] Found assignment: date=%s, topic=%s, keyword=%s, type=%s (sheet row %d)",
                today_str, topic, keyword, article_type, i + 1,
            )
            return {
                "topic": topic,
                "keyword": keyword,
                "article_type": article_type,
                "row_index": i + 1,
                "spreadsheet_id": spreadsheet_id,
                "access_token": access_token,
            }

    logging.info("[CF] No unpublished row found for date %s in 'Chris Fountoulis Content Calendar'.", today_str)
    return None


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def build_prompt(article_type: str, topic: str = "", keyword: str = "") -> str:
    """Build the Claude prompt for a given article type, topic and keyword (Greek, growthmedia.gr)."""

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
Use the WebSearch tool to find the top trending digital marketing topics in Greece and globally this week ({current_month_year}).
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

    current_month_year = datetime.now().strftime("%B %Y")
    year = datetime.now().strftime("%Y")
    if keyword:
        slug_hint = re.sub(r"[^\w\s-]", "", keyword).strip().lower().replace(" ", "-")
    elif topic:
        slug_hint = re.sub(r"[^\w\s-]", "", topic[:30]).strip().lower().replace(" ", "-")
    else:
        slug_hint = f"digital-marketing-greece-{year}" if article_type == "trending" else "content-marketing-stratigi"
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
  - Include the publication date context ({current_month_year}) where relevant.
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


def build_cf_prompt(article_type: str, topic: str = "", keyword: str = "") -> str:
    """Build the Claude prompt for chrisfountoulis.com — English articles, same SEO rules."""

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
Use the WebSearch tool to find the top trending marketing, business, or personal branding topics globally this week.
Pick the single best trending topic with a rising search volume (trending upward right now).
Find at least 3 authoritative sources (industry reports, major publications) to cite.
Include recent data published within the last 30 days.
Check what competitors are ranking for on the target keyword.
Identify a strong 2-4 word English focus keyphrase for SEO.
Identify related LSI keywords and synonyms to use naturally in the article.
"""
        else:
            research_section = """
## STEP 1 — RESEARCH
Use the WebSearch tool to find evergreen marketing, business, or personal branding topics with broad English-speaking appeal.
Choose a timeless topic with high search volume and low competition that will remain relevant for 12+ months.
Find at least 3 authoritative sources (industry reports, major publications) to cite.
Check what competitors are ranking for on the target keyword.
Identify a strong 2-4 word English focus keyphrase for SEO.
Identify related LSI keywords and synonyms to use naturally in the article.
"""

    current_month_year = datetime.now().strftime("%B %Y")
    year = datetime.now().strftime("%Y")
    if keyword:
        slug_hint = re.sub(r"[^\w\s-]", "", keyword).strip().lower().replace(" ", "-")
    elif topic:
        slug_hint = re.sub(r"[^\w\s-]", "", topic[:30]).strip().lower().replace(" ", "-")
    else:
        slug_hint = f"personal-branding-{year}" if article_type == "trending" else "content-marketing-strategy"
    type_note = "trending" if article_type == "trending" else "evergreen / timeless"

    return f"""
You are an expert English-language writer for the personal brand website chrisfountoulis.com.
Chris Fountoulis is a digital marketing consultant and strategist.
Write with authority and expertise (E-E-A-T: Experience, Expertise, Authority, Trust).

Your task today is to research, write, and publish ONE {type_note} article IN ENGLISH to WordPress.

{research_section}

## STEP 2 — WRITE THE ARTICLE (STRICTLY IN ENGLISH)
Write a complete article following ALL rules below:

LANGUAGE: English — every word of the article must be in English.
WORD COUNT: Minimum 800 words, ideal 1000-1200 words. Never go below 600 words under any circumstances.
TITLE (H1): Must contain the focus keyphrase. 50-60 characters maximum. Compelling and click-worthy.
INTRODUCTION (first 100 words): First paragraph MUST contain the focus keyphrase. Hook the reader immediately. Tell them what they will learn.
SUBHEADINGS:
  - At least 3 H2 subheadings.
  - At least ONE H2 must contain the focus keyphrase or a close variant.
  - Subheadings must be descriptive and informative.
BODY CONTENT:
  - Focus keyphrase appears naturally every 100-150 words — do NOT keyword stuff.
  - Use synonyms and related LSI terms naturally throughout subheadings and body.
  - Sentences maximum 20 words each — no exceptions.
  - Active voice throughout — passive voice must be less than 10% of sentences.
  - Short paragraphs: maximum 3-4 sentences per paragraph.
  - Use bullet points and numbered lists where appropriate.
  - Include real data, statistics, and facts with sources cited.
  - Write from a position of expertise — demonstrate experience and authority on the topic.
  - Include the publication date context ({current_month_year}) where relevant.
  - Aim for a Flesch Reading Ease score of 60-80: clear, readable prose.
  - Vary sentence length for rhythm.
  - Use transition words (however, therefore, additionally, furthermore, in addition, as a result, moreover, consequently) in at least 30% of sentences.
  - Simple language: write at maximum 8th grade reading level, as if explaining to a 10-year-old. Use short, everyday words. Avoid jargon. If a technical term is needed, explain it right away in plain words.
  - NO bold text (<strong>) in body paragraphs or lists. Bold is ONLY allowed inside heading tags (H1, H2, H3).
  - NO em-dashes (—) anywhere. Replace every em-dash with a comma or a period.
LINKS:
  - Minimum 2 outbound links to authoritative sources (industry reports, .gov, .edu, or major publications). Use real URLs found during research.
  - Minimum 3 internal links to other pages or articles on chrisfountoulis.com (e.g. href="https://chrisfountoulis.com/blog/", href="https://chrisfountoulis.com/services/", href="https://chrisfountoulis.com/about/", etc.). Use relevant anchor text.
  - All links must be relevant and add value.
CONCLUSION: Summarize key takeaways. Include a clear Call to Action. Repeat the focus keyphrase one final time.

FORMATTING RULES (STRICT):
  - NO bold text (<strong> or **) in the article body (paragraphs, lists). Bold formatting is ONLY allowed in headings (H1, H2, H3).
  - NO em-dashes (—) anywhere in the article. Replace every em-dash with a comma or a period.
  - Simple language: write at a maximum 8th grade reading level, as if explaining to a 10-year-old. Use short, common, everyday words. Avoid technical jargon. If you must use a technical term, explain it immediately in plain words.

## STEP 3 — YOAST SEO FIELDS
Prepare ALL of these:
- Focus keyphrase: 2-4 words, in English — must appear in: title, introduction, at least one H2, meta description, and slug.
- SEO title: MUST START with the focus keyphrase, 50-60 characters maximum, format: [Keyphrase]: [Description] | Chris Fountoulis
- Meta description: EXACTLY 120-155 characters (count carefully), contains focus keyphrase, compelling and encourages clicks, summarizes the article accurately.
- Slug: focus keyphrase in lowercase, hyphens between words, no stop words (a, the, and, or, etc.), e.g. {slug_hint}

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
  [ ] At least 3 internal links to chrisfountoulis.com pages included
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
   Pick the first result. Use its `urls.regular` field as the image URL.

2. Download the image bytes using Python requests:
   img_response = requests.get(image_url, timeout=30)

3. Upload to WordPress Media Library:
   POST {CF_WP_URL}/wp-json/wp/v2/media
   Headers:
     Authorization: Basic <base64(username:password)>
     Content-Disposition: attachment; filename="<slug>.jpg"
     Content-Type: image/jpeg
   Body: the raw image bytes

4. From the media upload response, get the media `id`.

5. Update the media record with SEO metadata:
   POST {CF_WP_URL}/wp-json/wp/v2/media/<id>
   JSON body:
     alt_text: "<focus keyphrase> — [brief description of image]" (max 125 characters, contains focus keyphrase)
     title: "<focus keyphrase> image"
     caption: "Photo: Unsplash"

6. Build an HTML img tag to embed in the article content:
   <img src="<image_url>" alt="<focus keyphrase> — [brief description]" title="<focus keyphrase>" class="wp-post-image" style="width:100%;height:auto;margin-bottom:1.5em;" />

7. Include the media id as `featured_media` in the post creation payload.

## STEP 6 — PUBLISH TO WORDPRESS VIA REST API
Use the following WordPress credentials to publish the article as a DRAFT:
  - WordPress URL: {CF_WP_URL}
  - Username: {CF_WP_USER}
  - Application Password: {CF_WP_PASS}

Use the WordPress REST API endpoint: {CF_WP_URL}/wp-json/wp/v2/posts
Method: POST with Basic Authentication (base64 of username:password).

The JSON payload must include:
  - title: the H1 title
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
    IMPORTANT: these must be sent as direct keys at the root of the JSON body, NOT inside a "meta" object.
  - featured_media: the media ID from Step 5

Use Python's `requests` library or `urllib` to make the API call.

After publishing, output a line in EXACTLY this format so it can be parsed:
CF_PUBLISHED_URL: <the full URL of the draft post>

Example: CF_PUBLISHED_URL: https://chrisfountoulis.com/?p=1234

Log the result clearly at the end.

## ABSOLUTE PROHIBITIONS — NEVER DO THESE
- Never publish below 600 words
- Never leave any Yoast field empty, and always send them as TOP-LEVEL keys in the REST API payload
- Never use passive voice in more than 10% of sentences
- Never write a sentence longer than 20 words
- Never keyword stuff — use the keyphrase naturally
- Never publish without at least 2 outbound links
- Never publish without at least 3 internal links to chrisfountoulis.com
- Never publish without the focus keyphrase in the introduction
- Never skip the pre-publish checklist (Step 4)
- Never publish as status "publish" — always use "draft" unless explicitly instructed otherwise
- Never use bold text (<strong>) in body paragraphs or lists — bold is for headings only
- Never use em-dashes (—) in the article — use commas or periods instead
- Never write above 8th grade reading level — keep all language simple and easy to understand
- Never publish without a featured image fetched from Unsplash and uploaded to WordPress
- Never publish without the featured image img tag inserted in the article content immediately after the H1 tag
"""


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

def extract_published_url(output: str) -> str | None:
    """Parse the PUBLISHED_URL line from Claude's stdout (growthmedia.gr)."""
    match = re.search(r"PUBLISHED_URL:\s*(https?://\S+)", output)
    if match:
        return match.group(1).rstrip(".")
    # Fallback: look only for draft post URLs (/?p=ID pattern)
    match = re.search(r"(https?://growthmedia\.gr/\?p=\d+)", output)
    if match:
        return match.group(1).rstrip(".")
    return None


def extract_cf_published_url(output: str) -> str | None:
    """Parse the CF_PUBLISHED_URL line from Claude's stdout (chrisfountoulis.com)."""
    match = re.search(r"CF_PUBLISHED_URL:\s*(https?://\S+)", output)
    if match:
        return match.group(1).rstrip(".")
    # Fallback: look only for draft post URLs (/?p=ID pattern)
    match = re.search(r"(https?://chrisfountoulis\.com/\?p=\d+)", output)
    if match:
        return match.group(1).rstrip(".")
    return None


# ---------------------------------------------------------------------------
# Sunday summary email
# ---------------------------------------------------------------------------

def _send_gmail(access_token: str, to: str, subject: str, html_body: str) -> None:
    """Send an email via Gmail API using an OAuth access token."""
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["Subject"] = subject
    msg["From"] = "me"
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    resp = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"raw": raw},
        timeout=20,
    )
    resp.raise_for_status()
    logging.info("Sunday summary email sent to %s (message id: %s)", to, resp.json().get("id"))


def send_sunday_summary(
    goi_topic: str, goi_keyword: str, goi_url: str | None,
    cf_topic: str, cf_keyword: str, cf_url: str | None,
    access_token: str,
) -> None:
    """Send a weekly summary email covering both sites' articles."""
    today_str = datetime.now().strftime("%d %B %Y")

    goi_url_html = (
        f'<a href="{goi_url}">{goi_url}</a>' if goi_url else "<em>URL not captured — check WordPress drafts</em>"
    )
    cf_url_html = (
        f'<a href="{cf_url}">{cf_url}</a>' if cf_url else "<em>URL not captured — check WordPress drafts</em>"
    )

    html_body = f"""
<html>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 640px; margin: 0 auto; padding: 24px;">

  <h1 style="color: #1a1a2e; border-bottom: 2px solid #e0e0e0; padding-bottom: 8px;">
    Weekly Article Summary — {today_str}
  </h1>

  <h2 style="color: #2c7be5; margin-top: 32px;">Section 1: growthmedia.gr (Greek)</h2>
  <table style="width: 100%; border-collapse: collapse; margin-top: 8px;">
    <tr>
      <td style="padding: 6px 12px 6px 0; font-weight: bold; width: 140px;">Topic</td>
      <td style="padding: 6px 0;">{goi_topic or "N/A"}</td>
    </tr>
    <tr style="background: #f9f9f9;">
      <td style="padding: 6px 12px 6px 0; font-weight: bold;">Focus keyphrase</td>
      <td style="padding: 6px 0;">{goi_keyword or "N/A"}</td>
    </tr>
    <tr>
      <td style="padding: 6px 12px 6px 0; font-weight: bold;">Draft URL</td>
      <td style="padding: 6px 0;">{goi_url_html}</td>
    </tr>
    <tr style="background: #f9f9f9;">
      <td style="padding: 6px 12px 6px 0; font-weight: bold;">Status</td>
      <td style="padding: 6px 0;">{"Published as draft" if goi_url else "Error — check logs"}</td>
    </tr>
  </table>

  <h2 style="color: #2c7be5; margin-top: 32px;">Section 2: chrisfountoulis.com (English)</h2>
  <table style="width: 100%; border-collapse: collapse; margin-top: 8px;">
    <tr>
      <td style="padding: 6px 12px 6px 0; font-weight: bold; width: 140px;">Topic</td>
      <td style="padding: 6px 0;">{cf_topic or "N/A"}</td>
    </tr>
    <tr style="background: #f9f9f9;">
      <td style="padding: 6px 12px 6px 0; font-weight: bold;">Focus keyphrase</td>
      <td style="padding: 6px 0;">{cf_keyword or "N/A"}</td>
    </tr>
    <tr>
      <td style="padding: 6px 12px 6px 0; font-weight: bold;">Draft URL</td>
      <td style="padding: 6px 0;">{cf_url_html}</td>
    </tr>
    <tr style="background: #f9f9f9;">
      <td style="padding: 6px 12px 6px 0; font-weight: bold;">Status</td>
      <td style="padding: 6px 0;">{"Published as draft" if cf_url else "Error — check logs"}</td>
    </tr>
  </table>

  <p style="margin-top: 32px; color: #666; font-size: 13px; border-top: 1px solid #e0e0e0; padding-top: 12px;">
    This email was generated automatically by the OpenClaw article generation system.
    Review and publish drafts from the WordPress admin panel.
  </p>

</body>
</html>
"""

    subject = f"Weekly Article Summary — {today_str}"
    try:
        _send_gmail(access_token, SUMMARY_EMAIL_TO, subject, html_body)
        print(f"Sunday summary email sent to {SUMMARY_EMAIL_TO}")
        logging.info("Sunday summary email sent successfully.")
    except Exception as exc:
        logging.error("Failed to send Sunday summary email: %s", exc)
        print(f"Warning: could not send Sunday summary email — {exc}")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(article_type: str, topic: str = "", keyword: str = "",
        spreadsheet_id: str = "", row_index: int = 0, access_token: str = "",
        site: str = "goi") -> tuple[int, str | None]:
    """
    Run article generation for one site.

    Returns (exit_code, published_url).
    site: "goi" for growthmedia.gr, "cf" for chrisfountoulis.com
    """
    if site == "cf":
        prompt = build_cf_prompt(article_type, topic, keyword)
        site_label = "chrisfountoulis.com"
        url_extractor = extract_cf_published_url
    else:
        prompt = build_prompt(article_type, topic, keyword)
        site_label = "growthmedia.gr"
        url_extractor = extract_published_url

    logging.info(
        "Starting %s article generation for %s (topic=%s, keyword=%s)",
        article_type, site_label, topic or "(auto)", keyword or "(auto)",
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
            logging.info("[%s] Claude output:\n%s", site_label, result.stdout[-3000:])

        if result.returncode != 0:
            logging.error(
                "[%s] Claude exited with code %d. stderr:\n%s",
                site_label, result.returncode, result.stderr[-2000:],
            )
            return result.returncode, None

        logging.info("[%s] Article generation completed successfully (type=%s)", site_label, article_type)

        published_url = url_extractor(result.stdout)

        # Update Google Sheet column E if we have a sheet reference
        if spreadsheet_id and row_index > 0 and access_token and published_url:
            cell = f"E{row_index}"
            try:
                _update_cell(access_token, spreadsheet_id, cell, published_url)
                logging.info("[%s] Sheet updated: row %d column E = %s", site_label, row_index, published_url)
                print(f"[{site_label}] Sheet updated: {cell} = {published_url}")
            except Exception as exc:
                logging.error("[%s] Failed to update Google Sheet: %s", site_label, exc)
                print(f"Warning: [{site_label}] could not update sheet cell {cell}: {exc}")
        elif spreadsheet_id and row_index > 0 and not published_url:
            logging.warning("[%s] Could not extract published URL from Claude output to update sheet.", site_label)
            print(f"Warning: [{site_label}] published URL not found in Claude output — sheet not updated.")

        return 0, published_url

    except subprocess.TimeoutExpired:
        logging.error("[%s] Claude process timed out after 15 minutes for type=%s", site_label, article_type)
        return 2, None
    except FileNotFoundError:
        logging.error("Claude CLI not found at %s", CLAUDE_BIN)
        return 3, None
    except Exception as exc:
        logging.error("[%s] Unexpected error: %s", site_label, exc)
        return 4, None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = sys.argv[1:]

    if args and args[0] in ("trending", "evergreen"):
        # Legacy / manual override — no sheet lookup, growthmedia.gr only
        article_type = args[0]
        print(f"Legacy mode: generating {article_type} article for growthmedia.gr without sheet lookup.")
        exit_code, _ = run(article_type, site="goi")
        sys.exit(exit_code)

    is_sunday = datetime.now().weekday() == 6  # Monday=0 … Sunday=6

    # --- growthmedia.gr ---
    print("Auto mode: looking up today's assignment in 'GOI Content Calendar'...")
    goi_topic = goi_keyword = ""
    goi_url = None
    goi_exit = 0

    try:
        goi_assignment = get_today_assignment()
    except Exception as exc:
        logging.error("GOI Google Sheets lookup failed: %s", exc)
        print(f"Error: could not read GOI Google Sheets — {exc}")
        goi_assignment = None
        goi_exit = 1

    if goi_assignment:
        goi_topic = goi_assignment["topic"]
        goi_keyword = goi_assignment["keyword"]
        print(
            f"[growthmedia.gr] Assignment found: [{goi_assignment['article_type']}] "
            f"topic='{goi_topic}', keyword='{goi_keyword}'"
        )
        goi_exit, goi_url = run(
            article_type=goi_assignment["article_type"],
            topic=goi_topic,
            keyword=goi_keyword,
            spreadsheet_id=goi_assignment["spreadsheet_id"],
            row_index=goi_assignment["row_index"],
            access_token=goi_assignment["access_token"],
            site="goi",
        )
    else:
        if goi_exit == 0:
            print("[growthmedia.gr] No article scheduled for today (or already published). Skipping.")

    # --- chrisfountoulis.com ---
    print("Auto mode: looking up today's assignment in 'Chris Fountoulis Content Calendar'...")
    cf_topic = cf_keyword = ""
    cf_url = None
    cf_exit = 0

    try:
        cf_assignment = get_cf_assignment()
    except Exception as exc:
        logging.error("CF Google Sheets lookup failed: %s", exc)
        print(f"Error: could not read CF Google Sheets — {exc}")
        cf_assignment = None
        cf_exit = 1

    if cf_assignment:
        cf_topic = cf_assignment["topic"]
        cf_keyword = cf_assignment["keyword"]
        print(
            f"[chrisfountoulis.com] Assignment found: [{cf_assignment['article_type']}] "
            f"topic='{cf_topic}', keyword='{cf_keyword}'"
        )
        cf_exit, cf_url = run(
            article_type=cf_assignment["article_type"],
            topic=cf_topic,
            keyword=cf_keyword,
            spreadsheet_id=cf_assignment["spreadsheet_id"],
            row_index=cf_assignment["row_index"],
            access_token=cf_assignment["access_token"],
            site="cf",
        )
    else:
        if cf_exit == 0:
            print("[chrisfountoulis.com] No article scheduled for today (or already published). Skipping.")

    # --- Sunday summary email ---
    if is_sunday and (goi_assignment or cf_assignment):
        print("Sunday detected — sending weekly summary email...")
        try:
            # Re-use a valid access token from whichever assignment succeeded
            summary_token = (
                (goi_assignment or {}).get("access_token")
                or (cf_assignment or {}).get("access_token")
                or _get_access_token()
            )
            send_sunday_summary(
                goi_topic=goi_topic,
                goi_keyword=goi_keyword,
                goi_url=goi_url,
                cf_topic=cf_topic,
                cf_keyword=cf_keyword,
                cf_url=cf_url,
                access_token=summary_token,
            )
        except Exception as exc:
            logging.error("Sunday summary email failed: %s", exc)
            print(f"Warning: Sunday summary email failed — {exc}")

    # Exit with worst error code seen
    sys.exit(max(goi_exit, cf_exit))
