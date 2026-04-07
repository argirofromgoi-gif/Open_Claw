import asyncio
import aiohttp
import json
import base64
import re
from html.parser import HTMLParser
from urllib.parse import urlparse
from openai import AsyncOpenAI

# =========================
# Universal WordPress Translator
# Μεταφράζει αυτόματα:
# - Posts & Pages
# - Custom Post Types
# - Elementor content
# - Menus (via TranslatePress)
# - Widgets/Sidebars
# - WooCommerce products
# - Theme strings (via TranslatePress)
# =========================

translation_sessions = {}


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self):
        return "".join(self._parts)


def strip_html(text):
    if not text:
        return text
    s = _HTMLStripper()
    s.feed(text)
    return s.get_text()


# =========================
# WP API HELPERS
# =========================

def make_auth_header(username, password):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json"
    }


async def wp_get(session, url, auth, params=None):
    async with session.get(url, headers=auth, params=params) as r:
        if r.status == 200:
            return await r.json()
        return None


async def wp_post(session, url, auth, data):
    async with session.post(url, headers=auth, json=data) as r:
        if r.status in (200, 201):
            return await r.json()
        text = await r.text()
        raise Exception(f"{r.status}: {text[:150]}")


async def wp_put(session, url, auth, data):
    async with session.put(url, headers=auth, json=data) as r:
        if r.status == 200:
            return await r.json()
        text = await r.text()
        raise Exception(f"{r.status}: {text[:150]}")


async def _tp_send(session, base_url, auth, original_text, translated_text, post_id, locale):
    """Low-level POST to the TranslatePress REST endpoint."""
    payload = {
        "original_text":   original_text,
        "translated_text": translated_text,
        "locale":          locale,
    }
    if post_id:
        payload["post_id"] = post_id
    try:
        url = f"{base_url}/wp-json/tp/v1/translate"
        async with session.post(url, headers=auth, json=payload) as r:
            return await r.json() if r.status == 200 else None
    except Exception:
        return None


async def tp_insert_translation(session, base_url, auth, original_text, translated_text, post_id=None, locale="el_GR"):
    """Inserts a translation pair into TranslatePress database via custom REST endpoint.

    Pushes two variants so TranslatePress can match regardless of whether it
    stored the source string with or without HTML tags:
      1. HTML-intact  — original strings exactly as received
      2. HTML-stripped — plain-text version of both sides
    """
    if not original_text or not translated_text:
        return

    # --- variant 1: with HTML intact ---
    if original_text.strip() != translated_text.strip():
        await _tp_send(session, base_url, auth, original_text, translated_text, post_id, locale)

    # --- variant 2: HTML stripped ---
    orig_stripped = strip_html(original_text)
    tr_stripped   = strip_html(translated_text)
    if not orig_stripped or not tr_stripped:
        return
    if orig_stripped.strip() == tr_stripped.strip():
        return
    # Only send if different from the intact version (avoids a redundant duplicate)
    if orig_stripped != original_text or tr_stripped != translated_text:
        await _tp_send(session, base_url, auth, orig_stripped, tr_stripped, post_id, locale)


# =========================
# SITE DETECTION
# =========================

async def detect_site_features(base_url, auth_headers):
    features = {
        "has_elementor":   False,
        "has_woocommerce": False,
        "has_menus":       False,
        "has_widgets":     False,
        "has_loco":        False,
        "post_types":      ["posts", "pages"],
    }

    async with aiohttp.ClientSession() as session:
        plugins = await wp_get(session, f"{base_url}/wp-json/wp/v2/plugins", auth_headers)
        if plugins:
            for p in plugins:
                slug = p.get("plugin", "").lower()
                if "elementor" in slug:
                    features["has_elementor"] = True
                if "woocommerce" in slug:
                    features["has_woocommerce"] = True
                if "loco" in slug:
                    features["has_loco"] = True

        menus = await wp_get(session, f"{base_url}/wp-json/wp/v2/menus", auth_headers)
        if menus:
            features["has_menus"] = True

        types = await wp_get(session, f"{base_url}/wp-json/wp/v2/types", auth_headers)
        if types:
            for key, val in types.items():
                if key not in ("post", "page", "attachment", "wp_block", "wp_template",
                               "wp_template_part", "wp_navigation", "wp_font_family", "wp_font_face"):
                    if val.get("rest_base"):
                        features["post_types"].append(val["rest_base"])

        if features["has_woocommerce"]:
            features["post_types"].append("products")

        widgets = await wp_get(session, f"{base_url}/wp-json/wp/v2/widgets", auth_headers)
        if widgets:
            features["has_widgets"] = True

    return features


# =========================
# TRANSLATION HELPERS
# =========================

async def translate_text(openai_client, text, target_lang="Greek"):
    if not text or not str(text).strip():
        return text

    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": f"""You are an expert SEO translator to {target_lang}.
Rules:
- Preserve ALL HTML tags exactly
- Translate only visible text content
- Never translate URLs, CSS classes, or HTML attributes
- Use natural fluent {target_lang}
- Keep brand names and proper nouns as-is
- Return ONLY the translated content, nothing else"""
            },
            {"role": "user", "content": str(text)}
        ],
        max_tokens=4000
    )
    return response.choices[0].message.content.strip()


def extract_elementor_text_pairs(original, translated):
    TEXT_KEYS = {"text", "title", "description", "caption", "button_text",
                 "heading", "editor", "html", "content", "label"}
    pairs = []

    def _walk(orig, tr):
        if isinstance(orig, dict) and isinstance(tr, dict):
            for key in TEXT_KEYS:
                if key in orig and key in tr:
                    o, t = orig[key], tr[key]
                    if o and t and isinstance(o, str) and isinstance(t, str) and o.strip() and o != t:
                        pairs.append((o, t))
            for k in orig:
                if k in tr and isinstance(orig[k], (dict, list)):
                    _walk(orig[k], tr[k])
        elif isinstance(orig, list) and isinstance(tr, list):
            for o_item, t_item in zip(orig, tr):
                _walk(o_item, t_item)

    _walk(original, translated)
    return pairs


async def translate_elementor_data(openai_client, elementor_json_str, target_lang="Greek"):
    if not elementor_json_str:
        return elementor_json_str

    try:
        data = json.loads(elementor_json_str) if isinstance(elementor_json_str, str) else elementor_json_str
    except Exception:
        return elementor_json_str

    async def translate_node(node):
        if isinstance(node, dict):
            for key in ["text", "title", "description", "caption", "button_text",
                        "heading", "editor", "html", "content", "label"]:
                if key in node and node[key] and isinstance(node[key], str):
                    node[key] = await translate_text(openai_client, node[key], target_lang)
            for val in node.values():
                if isinstance(val, (dict, list)):
                    await translate_node(val)
        elif isinstance(node, list):
            for item in node:
                await translate_node(item)

    await translate_node(data)
    return json.dumps(data)


# =========================
# POST/PAGE TRANSLATION
# =========================

async def get_all_content(base_url, auth_headers, post_types, slug=None):
    all_items = []
    async with aiohttp.ClientSession() as session:
        for pt in post_types:
            if slug:
                results = await wp_get(
                    session,
                    f"{base_url}/wp-json/wp/v2/{pt}",
                    auth_headers,
                    params={"slug": slug, "context": "edit"}
                )
                if results:
                    for r in results:
                        r["_post_type"] = pt
                    all_items.extend(results)
            else:
                page = 1
                while True:
                    results = await wp_get(
                        session,
                        f"{base_url}/wp-json/wp/v2/{pt}",
                        auth_headers,
                        params={"per_page": 100, "page": page, "status": "publish", "context": "edit"}
                    )
                    if not results:
                        break
                    for r in results:
                        r["_post_type"] = pt
                    all_items.extend(results)
                    if len(results) < 100:
                        break
                    page += 1
    return all_items


async def translate_post(openai_client, session, base_url, auth_headers, post, target_lang, lang_code):
    title   = post.get("title", {}).get("rendered", "") or ""
    content = post.get("content", {}).get("rendered", "") or ""
    excerpt = post.get("excerpt", {}).get("rendered", "") or ""

    yoast   = post.get("yoast_head_json", {}) or {}
    y_title = yoast.get("title", title)
    y_desc  = yoast.get("description", excerpt)

    results = await asyncio.gather(
        translate_text(openai_client, title, target_lang),
        translate_text(openai_client, content, target_lang),
        translate_text(openai_client, excerpt, target_lang),
        translate_text(openai_client, y_title, target_lang),
        translate_text(openai_client, y_desc, target_lang),
        return_exceptions=True
    )

    title_tr, content_tr, excerpt_tr, yt_tr, yd_tr = [
        r if not isinstance(r, Exception) else "" for r in results
    ]

    elementor_data = post.get("meta", {}).get("_elementor_data")
    elementor_tr   = None
    if elementor_data:
        try:
            elementor_tr = await translate_elementor_data(openai_client, elementor_data, target_lang)
        except Exception:
            pass

    post_id = post.get("id")
    tp_pairs = [
        (title,   title_tr),
        (content, content_tr),
        (excerpt, excerpt_tr),
        (y_title, yt_tr),
        (y_desc,  yd_tr),
    ]
    for orig, tr in tp_pairs:
        if orig and tr and orig != tr:
            await tp_insert_translation(session, base_url, auth_headers, orig, tr, post_id, lang_code)

    if elementor_data and elementor_tr and elementor_data != elementor_tr:
        try:
            orig_parsed = json.loads(elementor_data) if isinstance(elementor_data, str) else elementor_data
            tr_parsed   = json.loads(elementor_tr)   if isinstance(elementor_tr, str)   else elementor_tr
            for orig_str, tr_str in extract_elementor_text_pairs(orig_parsed, tr_parsed):
                await tp_insert_translation(session, base_url, auth_headers, orig_str, tr_str, post_id, lang_code)
        except Exception:
            pass


# =========================
# MENUS TRANSLATION (TranslatePress)
# =========================

async def translate_menus(openai_client, base_url, auth_headers, target_lang, lang_code, channel):
    """Μεταφράζει menus μέσω TranslatePress — χωρίς Polylang."""
    await channel.send("🍔 Μεταφράζω menus...")
    translated = 0

    async with aiohttp.ClientSession() as session:
        menus = await wp_get(session, f"{base_url}/wp-json/wp/v2/menus", auth_headers)
        if not menus:
            menus = await wp_get(session, f"{base_url}/wp-json/menus/v1/menus", auth_headers)
        if not menus:
            await channel.send("⚠️ Δεν βρέθηκαν menus.")
            return

        for menu in menus:
            menu_id   = menu["id"]
            menu_name = menu.get("name") or menu.get("title", {}).get("rendered", "") or ""

            # Μετάφρασε και πέρασε το όνομα του menu στο TranslatePress
            if menu_name:
                menu_name_tr = await translate_text(openai_client, menu_name, target_lang)
                await tp_insert_translation(session, base_url, auth_headers, menu_name, menu_name_tr, locale=lang_code)

            # Πάρε τα items
            items = await wp_get(
                session,
                f"{base_url}/wp-json/wp/v2/menu-items",
                auth_headers,
                params={"menus": menu_id, "per_page": 100}
            )
            if not items:
                continue

            for item in items:
                raw_title = item.get("title") or {}
                label = raw_title.get("raw") or re.sub(r'<[^>]+>', '', raw_title.get("rendered", "")).strip()
                if not label:
                    continue

                label_tr = await translate_text(openai_client, label, target_lang)
                await tp_insert_translation(session, base_url, auth_headers, label, label_tr, locale=lang_code)

                desc = item.get("description", "")
                if desc:
                    desc_tr = await translate_text(openai_client, desc, target_lang)
                    await tp_insert_translation(session, base_url, auth_headers, desc, desc_tr, locale=lang_code)

            translated += 1

    await channel.send(f"✅ Menus μεταφράστηκαν: {translated}")


# =========================
# WIDGETS TRANSLATION
# =========================

async def translate_widgets(openai_client, base_url, auth_headers, target_lang, channel):
    await channel.send("🔧 Μεταφράζω widgets...")

    async with aiohttp.ClientSession() as session:
        widgets = await wp_get(session, f"{base_url}/wp-json/wp/v2/widgets", auth_headers)
        if not widgets:
            await channel.send("⚠️ Δεν βρέθηκαν widgets.")
            return

        translated = 0
        for widget in widgets:
            widget_id    = widget.get("id")
            id_base      = widget.get("id_base", "") or ""
            instance     = widget.get("instance") or {}
            raw_instance = instance.get("raw") or {}

            if not raw_instance and not id_base:
                continue

            try:
                if "elementor" in id_base.lower():
                    elementor_data = raw_instance.get("elementor_data") or raw_instance.get("content")
                    if not elementor_data:
                        continue
                    if not isinstance(elementor_data, str):
                        elementor_data = json.dumps(elementor_data)
                    translated_elementor = await translate_elementor_data(
                        openai_client, elementor_data, target_lang
                    )
                    await wp_put(
                        session,
                        f"{base_url}/wp-json/wp/v2/widgets/{widget_id}",
                        auth_headers,
                        {"instance": {"raw": {**raw_instance, "elementor_data": translated_elementor}}}
                    )
                    translated += 1
                elif raw_instance:
                    updated = dict(raw_instance)
                    changed = False
                    for field in ("title", "text", "content", "description", "label"):
                        if updated.get(field) and isinstance(updated[field], str):
                            updated[field] = await translate_text(openai_client, updated[field], target_lang)
                            changed = True
                    if changed:
                        await wp_put(
                            session,
                            f"{base_url}/wp-json/wp/v2/widgets/{widget_id}",
                            auth_headers,
                            {"instance": {"raw": updated}}
                        )
                        translated += 1
            except Exception:
                pass

    await channel.send(f"✅ Widgets μεταφράστηκαν: {translated}")


# =========================
# THEME STRINGS TRANSLATION (TranslatePress)
# =========================

THEME_STRINGS = [
    "Previous", "Next", "Leave a Reply", "No responses yet",
    "Search", "Search for:", "Submit", "Cancel reply", "Comment",
    "Name", "Email", "Website",
    "Save my name, email, and website in this browser for the next time I comment.",
    "Your email address will not be published.", "Required fields are marked *",
    "Newer posts", "Older posts", "Comments are closed.",
    "Posted in", "Tagged", "Leave a comment", "Edit", "Read more",
    "Filed Under", "Categories", "Tags", "Archives", "Recent Posts",
    "Recent Comments", "Log in", "Entries feed", "Comments feed",
]


async def translate_theme_strings(openai_client, base_url, auth_headers, target_lang, lang_code, channel):
    """Μεταφράζει theme strings απευθείας μέσω TranslatePress."""
    await channel.send("🔤 Μεταφράζω theme strings...")
    translated = 0

    async with aiohttp.ClientSession() as session:
        for s in THEME_STRINGS:
            try:
                tr = await translate_text(openai_client, s, target_lang)
                await tp_insert_translation(session, base_url, auth_headers, s, tr, locale=lang_code)
                translated += 1
            except Exception:
                pass

    await channel.send(f"✅ Theme strings μεταφράστηκαν: {translated}")


# =========================
# WOOCOMMERCE TRANSLATION
# =========================

async def translate_woocommerce(openai_client, base_url, auth_headers, target_lang, lang_code, channel):
    await channel.send("🛒 Μεταφράζω WooCommerce products...")

    async with aiohttp.ClientSession() as session:
        page = 1
        translated = 0
        while True:
            products = await wp_get(
                session,
                f"{base_url}/wp-json/wc/v3/products",
                auth_headers,
                params={"per_page": 100, "page": page}
            )
            if not products:
                break

            for product in products:
                try:
                    name_tr       = await translate_text(openai_client, product.get("name", ""), target_lang)
                    desc_tr       = await translate_text(openai_client, product.get("description", ""), target_lang)
                    short_desc_tr = await translate_text(openai_client, product.get("short_description", ""), target_lang)

                    await tp_insert_translation(session, base_url, auth_headers, product.get("name", ""), name_tr, locale=lang_code)
                    await tp_insert_translation(session, base_url, auth_headers, product.get("description", ""), desc_tr, locale=lang_code)
                    await tp_insert_translation(session, base_url, auth_headers, product.get("short_description", ""), short_desc_tr, locale=lang_code)

                    translated += 1
                except Exception:
                    pass

            if len(products) < 100:
                break
            page += 1

    await channel.send(f"✅ WooCommerce products μεταφράστηκαν: {translated}")


# =========================
# MAIN TRANSLATION RUNNER
# =========================

async def run_translation(site_url, username, password, target_lang, lang_code, channel, openai_client, slug=None):
    auth = make_auth_header(username, password)

    await channel.send("🔍 Ανιχνεύω τις δυνατότητες του site...")

    try:
        features = await detect_site_features(site_url, auth)
    except Exception as e:
        await channel.send(f"❌ Αδυναμία σύνδεσης: {str(e)}")
        return

    detection_msg = (
        f"✅ **Site ανιχνεύθηκε!**\n"
        f"{'✅' if features['has_elementor']   else '❌'} Elementor\n"
        f"{'✅' if features['has_woocommerce'] else '❌'} WooCommerce\n"
        f"{'✅' if features['has_menus']       else '❌'} Menus\n"
        f"{'✅' if features['has_widgets']     else '❌'} Widgets\n"
        f"📄 Post types: {', '.join(features['post_types'])}\n\n"
        f"🚀 Ξεκινώ μετάφραση σε **{target_lang}**..."
    )
    await channel.send(detection_msg)

    await channel.send("📄 Μεταφράζω posts & pages...")
    posts = await get_all_content(site_url, auth, features["post_types"], slug=slug)
    total      = len(posts)
    translated = 0
    failed     = 0
    report     = []

    async def _translate_one(session, post):
        title = post.get("title", {}).get("rendered", "Unknown")
        try:
            await translate_post(openai_client, session, site_url, auth, post, target_lang, lang_code)
            return ("ok", title)
        except Exception as e:
            return ("err", title, str(e))

    async with aiohttp.ClientSession() as session:
        batch_size = 5
        for batch_start in range(0, total, batch_size):
            batch = posts[batch_start:batch_start + batch_size]
            results = await asyncio.gather(
                *[_translate_one(session, p) for p in batch]
            )
            for result in results:
                if result[0] == "ok":
                    translated += 1
                    report.append(f"✅ `{result[1][:45]}`")
                else:
                    failed += 1
                    report.append(f"❌ `{result[1][:45]}` — {result[2][:50]}")

            done = batch_start + len(batch)
            if done % 10 == 0 or done == total:
                await channel.send(f"📊 Πρόοδος: {done}/{total} | ✅ {translated} | ❌ {failed}")

    await channel.send(f"✅ Posts/Pages: {translated} επιτυχείς, {failed} αποτυχίες")

    if features["has_menus"]:
        await translate_menus(openai_client, site_url, auth, target_lang, lang_code, channel)

    if features["has_widgets"]:
        await translate_widgets(openai_client, site_url, auth, target_lang, channel)

    await translate_theme_strings(openai_client, site_url, auth, target_lang, lang_code, channel)

    if features["has_woocommerce"]:
        await translate_woocommerce(openai_client, site_url, auth, target_lang, lang_code, channel)

    await channel.send(
        f"🎉 **Μετάφραση ολοκληρώθηκε!**\n"
        f"🌐 Site: {site_url}\n"
        f"🗣️ Γλώσσα: {target_lang}\n"
        f"📄 Posts/Pages: {translated}/{total}\n\n"
        f"✅ Οι μεταφράσεις αποθηκεύτηκαν στο TranslatePress."
    )

    chunk = ""
    for line in report:
        if len(chunk) + len(line) > 1800:
            await channel.send(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await channel.send(chunk)


# =========================
# DISCORD COMMAND HANDLER
# =========================

SUPPORTED_LANGUAGES = {
    "greek":    ("Greek",   "el"),
    "english":  ("English", "en"),
    "german":   ("German",  "de"),
    "french":   ("French",  "fr"),
    "spanish":  ("Spanish", "es"),
    "italian":  ("Italian", "it"),
}


async def handle_translate_command(message, openai_client):
    user_id = message.author.id
    content = message.content.strip()

    if "!translate" in content.lower():
        translation_sessions[user_id] = {"step": "url", "data": {}}
        await message.channel.send(
            f"🌐 **WordPress Universal Translator**\n\n"
            f"Θα μεταφράσω αυτόματα:\n"
            f"• Posts & Pages\n"
            f"• Elementor content\n"
            f"• Menus\n"
            f"• Widgets\n"
            f"• WooCommerce (αν υπάρχει)\n\n"
            f"**Βήμα 1/4:** Στείλε μου το URL του site σου\n"
            f"π.χ. `https://mysite.com`"
        )
        return True

    session = translation_sessions.get(user_id)
    if not session:
        return False

    step = session["step"]

    if step == "url":
        url = content.rstrip("/")
        if not url.startswith("http"):
            await message.channel.send("⚠️ Το URL πρέπει να ξεκινά με `http://` ή `https://`")
            return True
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        slug = parsed.path.strip("/") or None
        session["data"]["url"] = base_url
        session["data"]["slug"] = slug
        session["step"] = "username"
        await message.channel.send("**Βήμα 2/4:** Στείλε μου το WordPress **username** σου:")
        return True

    if step == "username":
        session["data"]["username"] = content
        session["step"] = "password"
        await message.channel.send(
            "**Βήμα 3/4:** Στείλε μου το **Application Password** σου:\n"
            "_(Dashboard → Users → Profile → Application Passwords)_"
        )
        return True

    if step == "password":
        session["data"]["password"] = content
        session["step"] = "language"
        langs = ", ".join(SUPPORTED_LANGUAGES.keys())
        await message.channel.send(
            f"**Βήμα 4/4:** Σε ποια γλώσσα να μεταφράσω;\n"
            f"Διαθέσιμες: `{langs}`"
        )
        return True

    if step == "language":
        lang_key = content.lower().strip()
        if lang_key not in SUPPORTED_LANGUAGES:
            await message.channel.send(
                f"⚠️ Μη αναγνωρίσιμη γλώσσα. Επίλεξε από: `{', '.join(SUPPORTED_LANGUAGES.keys())}`"
            )
            return True

        target_lang, lang_code = SUPPORTED_LANGUAGES[lang_key]
        data = session["data"]
        del translation_sessions[user_id]

        await message.channel.send(
            f"✅ **Έτοιμο!**\n"
            f"🌐 Site: `{data['url']}`\n"
            f"👤 User: `{data['username']}`\n"
            f"🗣️ Γλώσσα: **{target_lang}**\n\n"
            f"🚀 Ξεκινώ..."
        )

        asyncio.create_task(
            run_translation(
                data["url"],
                data["username"],
                data["password"],
                target_lang,
                lang_code,
                message.channel,
                openai_client,
                slug=data.get("slug"),
            )
        )
        return True

    return False
