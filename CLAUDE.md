# OpenClaw GOI — Agent Instructions

## MANDATORY WORDPRESS SEO PUBLISHING RULES

These rules are NON-NEGOTIABLE and apply to EVERY article or blog post published to WordPress, regardless of what the user requests. Never skip any of these steps.

---

## STEP 1 — RESEARCH BEFORE WRITING

Before writing any article:
1. Use `search_web` to find the latest information on the topic
2. Find at least 3 authoritative sources to cite
3. Check what competitors are ranking for on the target keyword
4. Identify related keywords and LSI terms to include naturally

---

## STEP 2 — ARTICLE STRUCTURE (MANDATORY)

Every article MUST follow this structure:

### Word Count
- MINIMUM 600 words — never publish below this
- IDEAL: 800-1200 words for most topics
- Never reduce word count just because the user asks for "short" — 600 is the absolute minimum for SEO

### Title (H1)
- Must contain the focus keyphrase
- 50-60 characters maximum
- Compelling and click-worthy

### Introduction (first 100 words)
- Must contain the focus keyphrase in the FIRST paragraph
- Hook the reader immediately
- Tell them what they will learn

### Subheadings (H2, H3)
- At least 3 H2 subheadings
- At least ONE H2 must contain the focus keyphrase or a close variant
- Subheadings must be descriptive and informative

### Body Content
- Focus keyphrase must appear every 100-150 words naturally
- Use short sentences — MAXIMUM 20 words per sentence
- Use active voice — avoid passive voice
- Use bullet points and numbered lists where appropriate
- Include real data, statistics, and facts with sources
- Write in a clear, professional tone
- NO bold text in the article body — bold formatting is allowed ONLY in headings (H1, H2, H3), never in paragraphs or lists
- Simple language — maximum 8th grade reading level, as if explaining to a 10-year-old. Use short, common words. Avoid jargon.
- NO em-dashes (—) anywhere in the article — replace with a comma or a period instead

### Links
- MINIMUM 2 outbound links to authoritative sources (e.g., industry reports, .gov, .edu, well-known publications)
- MINIMUM 3 internal links to other pages or articles on growthmedia.gr (e.g. blog posts, service pages, category pages)
- All links must be relevant and add value

### Conclusion
- Summarize key takeaways
- Include a clear Call to Action (CTA)
- Can repeat the focus keyphrase one final time

---

## STEP 3 — YOAST SEO FIELDS (MANDATORY)

Every post MUST include ALL of the following Yoast SEO fields via REST API:

### Focus Keyphrase
- Field: `yoast_focus_keyword`
- Must be 2-4 words
- Must appear in: title, introduction, at least one H2, meta description, slug

### SEO Title
- Field: `yoast_seo_title`
- Must START with the focus keyphrase
- 50-60 characters maximum
- Format: `[Keyphrase]: [Compelling Description] | [Brand]`

### Meta Description
- Field: `yoast_meta_description`
- EXACTLY 120-155 characters (count carefully)
- Must contain the focus keyphrase
- Must be compelling and encourage clicks
- Must summarize the article accurately

### Slug
- Must contain the focus keyphrase
- Use hyphens between words
- Lowercase only
- No stop words (a, the, and, or, etc.)
- Example: `influencer-marketing-greece-2026`

---

## STEP 4 — PRE-PUBLISH CHECKLIST

Before calling the WordPress REST API, verify ALL of these:

- [ ] Word count ≥ 600
- [ ] Focus keyphrase in H1 title
- [ ] Focus keyphrase in first paragraph
- [ ] Focus keyphrase in meta description
- [ ] Focus keyphrase in SEO title (at the start)
- [ ] Focus keyphrase in slug
- [ ] Focus keyphrase in at least 1 H2
- [ ] Meta description is 120-155 characters
- [ ] At least 2 outbound links added
- [ ] At least 3 internal links to growthmedia.gr pages added
- [ ] No sentences longer than 20 words
- [ ] Active voice used throughout
- [ ] At least 3 H2 subheadings
- [ ] Conclusion with CTA included
- [ ] All Yoast fields set: yoast_focus_keyword, yoast_seo_title, yoast_meta_description
- [ ] NO bold text in body paragraphs or lists (bold only in headings)
- [ ] NO em-dashes (—) anywhere in the article
- [ ] Simple language used throughout (max 8th grade level)
- [ ] Unsplash featured image uploaded and set as featured_media

If ANY item is not checked, FIX IT before publishing. Do not publish until all items are checked.

---

## STEP 5 — FEATURED IMAGE (MANDATORY)

Before publishing, fetch a free relevant image from Unsplash and set it as the featured image:

1. Call the Unsplash API: `https://api.unsplash.com/search/photos?query=<topic>&per_page=1&orientation=landscape`
   - Use a search query based on the article topic and focus keyphrase
   - Pick the first result's `urls.regular` (or `urls.full`) URL
2. Download the image bytes
3. Upload to WordPress Media Library via `POST /wp-json/wp/v2/media`
   - Set `Content-Disposition: attachment; filename="<slug>.jpg"`
   - Set `Content-Type: image/jpeg`
   - In the same request or a follow-up PATCH, set:
     - `alt_text`: SEO-optimized alt text containing the focus keyphrase (max 125 characters)
     - `title`: the focus keyphrase + brief description
     - `caption`: Photo credit: Unsplash
4. Use the returned media ID as `featured_media` in the post payload

---

## STEP 6 — WORDPRESS REST API PUBLISHING

When publishing to WordPress:

1. Always publish as **draft** (status: "draft") unless explicitly told to publish
2. Use the registered Yoast REST fields:
   - `yoast_focus_keyword`
   - `yoast_seo_title`
   - `yoast_meta_description`
3. Set the correct post type (post for articles, page for pages)
4. Add relevant categories and tags
5. Set a proper excerpt (first 2 sentences of the article)
6. Set `featured_media` to the uploaded Unsplash image ID

---

## ADDITIONAL CONTENT QUALITY RULES

### Readability
- Flesch Reading Ease score target: 60-80
- Short paragraphs: maximum 3-4 sentences
- Use transition words between sections (however, therefore, additionally, etc.)
- Vary sentence length for rhythm
- Use transition words (however, therefore, additionally, furthermore, in addition, as a result, moreover, consequently) in at least 30% of sentences

### E-E-A-T (Experience, Expertise, Authority, Trust)
- Include specific data and statistics
- Cite authoritative sources
- Write from a position of expertise
- Include the publication date context

### Keyword Usage
- Do NOT keyword stuff — use the keyphrase naturally
- Use synonyms and related terms
- LSI keywords should appear naturally in subheadings and body

---

## CRONJOB ARTICLE GENERATION RULES

When generating articles automatically (weekly cronjob):

### Article 1 — Trending Topic
- Use `search_web` to find trending marketing topics this week
- Target a trending keyword with rising search volume
- Include recent data (within last 30 days)

### Article 2 — Evergreen Topic
- Choose a timeless marketing topic
- Target a high-volume, low-competition keyword
- Content should remain relevant for 12+ months

Both articles must follow ALL the rules above before publishing.

---

## NEVER DO THESE

- Never publish below 600 words
- Never leave Yoast fields empty
- Never use passive voice more than 10% of sentences
- Never use sentences longer than 20 words
- Never keyword stuff
- Never publish without at least 2 outbound links
- Never publish without at least 3 internal links to growthmedia.gr
- Never publish without the focus keyphrase in the introduction
- Never ignore the pre-publish checklist
- Never use bold text in the article body (paragraphs, lists) — bold is for headings only
- Never use em-dashes (—) in the article — use commas or periods instead
- Never write above 8th grade reading level — keep language simple and clear
- Never publish without fetching and setting a featured image from Unsplash
