"""
POCKET WATCH SCOUT - Victorian Silver Pocket Watch Hunter
=========================================================
Scans eBay IE for undervalued Victorian silver pocket watches.
Alerts when estimated value is 2x or more above listing price.

Based on Scout Engine v2 — same pattern as Map Scout and Rug Scout.

SETUP (Railway environment variables):
  ANTHROPIC_API_KEY
  EBAY_APP_ID
  EBAY_CLIENT_SECRET
  SENDGRID_API_KEY
  ALERT_EMAIL        your email — all alerts go here first
  FROM_EMAIL         verified SendGrid sender
  CLIENT_EMAIL       client's email for alerts (james@hartley.ie)
"""

import os
import json
import time
import base64
import logging
import requests
import threading
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, render_template
from werkzeug.utils import secure_filename

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Flask ─────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_FILE  = os.path.join(os.environ.get("DATA_DIR", BASE_DIR), "watch_scout_data.json")
UPLOAD_DIR = os.path.join(os.environ.get("DATA_DIR", BASE_DIR), "watch_uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── Credentials ───────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
EBAY_APP_ID        = os.environ.get("EBAY_APP_ID", "")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
SENDGRID_API_KEY   = os.environ.get("SENDGRID_API_KEY", "")
ALERT_EMAIL        = os.environ.get("ALERT_EMAIL", "")
FROM_EMAIL         = os.environ.get("FROM_EMAIL", "")
CLIENT_EMAIL       = os.environ.get("CLIENT_EMAIL", "james@hartley.ie")

# ── Currency ──────────────────────────────────────────────────────────────────
EUR_TO_GBP = 0.85
GBP_TO_EUR = 1.18

def gbp_to_eur(gbp):
    return round((gbp or 0) * GBP_TO_EUR, 0)

def eur_to_gbp(eur):
    return round((eur or 0) * EUR_TO_GBP, 2)

# ── Claude pricing ────────────────────────────────────────────────────────────
COST_PER_1K_INPUT_TOKENS  = 0.003
COST_PER_1K_OUTPUT_TOKENS = 0.015

# ── Scan pause flag ───────────────────────────────────────────────────────────
_scan_paused = False

def is_scan_paused():
    return _scan_paused

def set_scan_paused(paused: bool):
    global _scan_paused
    _scan_paused = paused
    logger.info(f"Scanning {'PAUSED' if paused else 'RESUMED'}")

# ── Configuration ─────────────────────────────────────────────────────────────
CONFIG = {
    "scan_interval_hours":   2,
    "min_listing_price_gbp": 20,
    "undervalue_threshold":  2.0,   # 2x for watches — more common than maps
    "max_listings_per_scan": 60,
    "auto_archive_days":     14,    # watches stay relevant longer
    "monthly_spend_cap_gbp": 5.0,
    "find_limit":            3,     # stop after 3 matches per client brief
    "client_name":           "James Hartley",
}

# ── Search Keywords ───────────────────────────────────────────────────────────
EBAY_KEYWORDS = [
    "silver full hunter pocket watch Victorian",
    "antique silver pocket watch hallmarked",
    "English silver pocket watch 1880s",
    "silver half hunter pocket watch antique",
    "Victorian pocket watch sterling silver",
    "antique pocket watch London hallmark silver",
    "Dent pocket watch silver",
    "Frodsham pocket watch silver",
    "Waltham pocket watch silver antique",
    "antique pocket watch Birmingham hallmark silver",
]

# ── Exclusion Keywords ────────────────────────────────────────────────────────
EXCLUSION_KEYWORDS = [
    "gold", "gold filled", "gold plated", "yellow gold", "rose gold",
    "plated", "gold tone", "gilded",
    "replica", "reproduction", "repro", "copy", "fake",
    "quartz", "battery", "modern", "new",
    "parts only", "spares", "repair", "chain only", "fob only",
    "pocket watch stand", "pocket watch display",
    "pocket watch case only", "movement only",
]

EXCLUDED_COUNTRIES = ["US", "CA", "AU", "CN", "HK", "JP"]

# ── Data helpers ──────────────────────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "listings":     [],
        "last_scan":    None,
        "scan_count":   0,
        "matches_found": 0,
        "stats":        {},
        "cost": {
            "total_input_tokens":  0,
            "total_output_tokens": 0,
            "total_cost_gbp":      0.0,
            "this_month_cost_gbp": 0.0,
            "last_scan_cost_gbp":  0.0,
            "scans": [],
        }
    }

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def find_listing(data, item_id):
    for l in data["listings"]:
        if l["item_id"] == item_id:
            return l
    return None

# ── Cost tracking ─────────────────────────────────────────────────────────────
def record_cost(data, input_tokens, output_tokens, label="analysis"):
    cost_gbp = (
        (input_tokens  / 1000) * COST_PER_1K_INPUT_TOKENS +
        (output_tokens / 1000) * COST_PER_1K_OUTPUT_TOKENS
    )
    c = data.setdefault("cost", {
        "total_input_tokens":  0,
        "total_output_tokens": 0,
        "total_cost_gbp":      0.0,
        "this_month_cost_gbp": 0.0,
        "last_scan_cost_gbp":  0.0,
        "scans": [],
    })
    c["total_input_tokens"]  = c.get("total_input_tokens",  0) + input_tokens
    c["total_output_tokens"] = c.get("total_output_tokens", 0) + output_tokens
    c["total_cost_gbp"]      = round(c.get("total_cost_gbp", 0.0) + cost_gbp, 4)

    now       = datetime.utcnow()
    month_key = f"{now.year}-{now.month:02d}"
    monthly   = [s for s in c.get("scans", []) if s.get("month") == month_key]
    this_month = sum(s.get("cost_gbp", 0) for s in monthly) + cost_gbp
    c["this_month_cost_gbp"] = round(this_month, 4)

    c.setdefault("scans", []).append({
        "ts":            now.isoformat(),
        "month":         month_key,
        "label":         label,
        "input_tokens":  input_tokens,
        "output_tokens": output_tokens,
        "cost_gbp":      round(cost_gbp, 4),
    })
    return cost_gbp

def check_spend_cap(data):
    cap        = CONFIG.get("monthly_spend_cap_gbp", 5.0)
    this_month = data.get("cost", {}).get("this_month_cost_gbp", 0.0)
    if this_month >= cap:
        logger.warning(f"Monthly spend cap reached: £{this_month:.2f} / £{cap:.2f}")
        set_scan_paused(True)
        return False
    return True

# ── eBay token ────────────────────────────────────────────────────────────────
_ebay_token        = None
_ebay_token_expiry = 0

def get_ebay_token():
    global _ebay_token, _ebay_token_expiry
    if _ebay_token and time.time() < _ebay_token_expiry - 60:
        return _ebay_token
    credentials = base64.b64encode(
        f"{EBAY_APP_ID}:{EBAY_CLIENT_SECRET}".encode()
    ).decode()
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data="grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope",
        timeout=15,
    )
    resp.raise_for_status()
    token_data     = resp.json()
    _ebay_token    = token_data["access_token"]
    _ebay_token_expiry = time.time() + token_data.get("expires_in", 7200)
    return _ebay_token

def fetch_all_listing_images(item_id, token):
    try:
        resp = requests.get(
            f"https://api.ebay.com/buy/browse/v1/item/{item_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=12,
        )
        if resp.status_code == 200:
            detail = resp.json()
            images = []
            if detail.get("image"):
                images.append(detail["image"].get("imageUrl", ""))
            for img in detail.get("additionalImages", []):
                url = img.get("imageUrl", "")
                if url and url not in images:
                    images.append(url)
            return [u for u in images if u]
    except Exception as e:
        logger.warning(f"Image fetch error for {item_id}: {e}")
    return []

def search_ebay(keyword, token):
    params = {
        "q": keyword,
        "filter": (
            f"price:[{CONFIG['min_listing_price_gbp']}..500],"
            "priceCurrency:GBP,"
            "conditions:{USED|UNSPECIFIED}"
        ),
        "limit": "50",
        "sort": "newlyListed",
    }
    try:
        resp = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
            },
            params=params,
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("itemSummaries", [])
        logger.warning(f"eBay search {keyword}: {resp.status_code}")
    except Exception as e:
        logger.error(f"eBay search error: {e}")
    return []

def passes_exclusion_filter(title, description=""):
    combined = (title + " " + description).lower()
    for excl in EXCLUSION_KEYWORDS:
        if excl.lower() in combined:
            return False
    return True

def already_seen(item_id, data):
    return any(l["item_id"] == item_id for l in data["listings"])

# ── Claude prompts ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are an expert horologist and antique pocket watch specialist
with 30 years of experience in auction houses and specialist watch dealers.

You are hunting specifically for: Victorian era English silver pocket watches,
full hunter or half hunter style, London or Birmingham hallmark preferred,
quality makers such as Dent, Frodsham, Waltham, or similar.
Budget range: £20–£400. Client wants working order preferred.

Analyse the listing images and description carefully. Return ONLY a valid JSON object:
{
  "is_target_watch": true or false,
  "confidence": "high" | "medium" | "low",
  "watch_type": "full hunter | half hunter | open face | unknown",
  "maker": "identified maker or Unknown",
  "hallmark": "London | Birmingham | Chester | other | not visible | none",
  "period": "approximate date e.g. 1880s, Victorian, Edwardian",
  "material": "sterling silver | silver plated | gold | unknown",
  "movement": "mechanical | quartz | unknown",
  "condition": "excellent | good | fair | poor | unknown",
  "condition_notes": "brief note on visible condition from images",
  "case_style": "brief description of case decoration and style",
  "estimated_value_gbp": estimated fair market value as a number,
  "value_rationale": "2-3 sentences explaining valuation",
  "red_flags": "any concerns about authenticity, plating, or condition",
  "recommendation": "strong buy | buy | watch | pass"
}

Key guidance:
- Check carefully for hallmarks — genuine sterling silver will have clear hallmarks
- Gold filled or plated cases are worth much less — look carefully at colour and description
- Quartz movements are modern and not what client wants — check description carefully
- Working order is preferred but good cosmetic condition is acceptable
- Named makers (Dent, Frodsham, Waltham, Elgin) significantly increase value
- Look at ALL images including case back for hallmarks and movement shots
- Be conservative with valuations
- Respond with ONLY the JSON object, no other text"""

DEEP_ANALYSIS_PROMPT = """You are an expert horologist and antique pocket watch specialist.
A listing has passed initial screening. Perform a DEEP ANALYSIS with all available images.

Look extremely carefully for:
- Hallmark details — lion passant, date letter, assay office mark, maker's mark
- Case material — true sterling silver vs silver plate vs gold filled
- Movement quality — visible through dial or movement shots
- Case condition — dents, repairs, hinge condition, bow and crown
- Dial condition — original enamel, hairline cracks, restoration
- Any signs of marriage (mismatched case and movement)
- Maker identification from movement or dust cover engraving
- Any paper labels, case numbers, or retailer engravings

Return ONLY a valid JSON object:
{
  "verdict": "genuine" | "likely_genuine" | "uncertain" | "likely_not_target" | "not_target",
  "confidence": "high" | "medium" | "low",
  "maker": "confirmed or revised identification",
  "hallmark_detail": "specific hallmark findings",
  "period": "confirmed or revised date",
  "material_confirmed": "sterling silver | silver plated | gold filled | other",
  "estimated_value_gbp": revised fair market value as number,
  "key_findings": "3-5 sentences on most important deep analysis findings",
  "condition_risk": "none" | "low" | "medium" | "high",
  "recommendation": "strong buy | buy | watch | pass",
  "red_flags": "specific concerns or none"
}"""

# ── Image helpers ─────────────────────────────────────────────────────────────
def build_image_content(image_urls, max_images=1):
    content = []
    loaded  = 0
    for url in image_urls:
        if loaded >= max_images:
            break
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                ct  = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                b64 = base64.b64encode(resp.content).decode()
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": ct, "data": b64}
                })
                loaded += 1
        except Exception as e:
            logger.warning(f"Image fetch error {url}: {e}")
    return content

# ── Claude analysis ───────────────────────────────────────────────────────────
def analyse_with_claude(listing, data=None):
    user_content = build_image_content(listing.get("all_image_urls", []), max_images=1)
    user_content.append({
        "type": "text",
        "text": (
            f"eBay Listing:\n"
            f"Title: {listing['title']}\n"
            f"Description: {listing.get('description', 'No description')[:800]}\n"
            f"Asking Price: £{listing['price_gbp']:.2f}\n\n"
            f"Please assess this listing."
        )
    })
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 700,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_content}],
            },
            timeout=60,
        )
        if resp.status_code == 200:
            result = resp.json()
            if data is not None:
                usage = result.get("usage", {})
                record_cost(data, usage.get("input_tokens", 0),
                           usage.get("output_tokens", 0), "initial_analysis")
            raw = result["content"][0]["text"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
        logger.error(f"Claude error: {resp.status_code}")
    except Exception as e:
        logger.error(f"Claude request error: {e}")
    return None

def deep_analyse_with_claude(listing, data=None):
    all_images     = listing.get("all_image_urls", [])
    manual_paths   = listing.get("manual_image_paths", [])
    user_content   = build_image_content(all_images, max_images=8)

    for path in manual_paths[:4]:
        full_path = os.path.join(BASE_DIR, path)
        if os.path.exists(full_path):
            try:
                with open(full_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                ext = path.rsplit(".", 1)[-1].lower()
                mt  = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                       "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
                user_content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mt, "data": b64}
                })
            except Exception as e:
                logger.warning(f"Manual image error {path}: {e}")

    init = listing.get("analysis", {}) or {}
    user_content.append({
        "type": "text",
        "text": (
            f"Deep analysis request:\n"
            f"Title: {listing['title']}\n"
            f"Asking Price: £{listing['price_gbp']:.2f}\n"
            f"Initial assessment: {init.get('maker','Unknown')}, "
            f"{init.get('period','Unknown')}, "
            f"Est. £{init.get('estimated_value_gbp',0):.0f}\n"
            f"Initial red flags: {init.get('red_flags','None')}\n\n"
            f"Please perform a thorough deep analysis."
        )
    })

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 900,
                "system": DEEP_ANALYSIS_PROMPT,
                "messages": [{"role": "user", "content": user_content}],
            },
            timeout=90,
        )
        if resp.status_code == 200:
            result = resp.json()
            if data is not None:
                usage = result.get("usage", {})
                cost  = record_cost(data, usage.get("input_tokens", 0),
                                   usage.get("output_tokens", 0), "deep_analysis")
                logger.info(f"Deep analysis cost: £{cost:.4f}")
            raw = result["content"][0]["text"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
        logger.error(f"Deep analysis Claude error: {resp.status_code}")
    except Exception as e:
        logger.error(f"Deep analysis error: {e}")
    return None

# ── Email alerts ──────────────────────────────────────────────────────────────
def send_alert_email(listing, recipient_email=None):
    if not all([SENDGRID_API_KEY, ALERT_EMAIL, FROM_EMAIL]):
        logger.warning("SendGrid not configured")
        return

    to_email = recipient_email or ALERT_EMAIL
    a        = listing.get("analysis", {}) or {}
    ask_gbp  = listing.get("price_gbp", 0)
    est_gbp  = a.get("estimated_value_gbp", 0)
    ratio    = round(est_gbp / ask_gbp, 1) if ask_gbp else "?"

    deep = listing.get("deep_analysis")
    deep_section = ""
    if deep:
        deep_section = f"""
        <hr>
        <h3>🔬 Deep Analysis Results</h3>
        <p><strong>Verdict:</strong> {deep.get('verdict','—')}</p>
        <p><strong>Recommendation:</strong> {deep.get('recommendation','—')}</p>
        <p><strong>Material Confirmed:</strong> {deep.get('material_confirmed','—')}</p>
        <p><strong>Hallmark Detail:</strong> {deep.get('hallmark_detail','—')}</p>
        <p><strong>Revised Value:</strong> £{deep.get('estimated_value_gbp',0):.0f} / €{gbp_to_eur(deep.get('estimated_value_gbp',0)):.0f}</p>
        <p><strong>Key Findings:</strong> {deep.get('key_findings','—')}</p>
        <p><strong>Red Flags:</strong> {deep.get('red_flags','None')}</p>
        """

    subject = (
        f"⌚ Watch Scout Alert: {a.get('maker','Unknown')} — "
        f"£{ask_gbp:.0f} listed / £{est_gbp:.0f} est. value "
        f"({ratio}x undervalued)"
    )

    html_body = f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:auto;padding:20px;">
<h2 style="color:#1a3a5c;">⌚ Pocket Watch Scout — Deal Alert</h2>
<p><strong>Client:</strong> {CONFIG['client_name']}</p>
<h3>{listing['title']}</h3>
<p>eBay · Found {listing['found_at'][:16].replace('T',' ')} UTC</p>
<p><strong>Asking price:</strong> £{ask_gbp:.2f} / €{gbp_to_eur(ask_gbp):.0f}</p>
<p><strong>Estimated value:</strong>
  <span style="color:#1a4a2e;">£{est_gbp:.0f} / €{gbp_to_eur(est_gbp):.0f}</span></p>
<p><strong>Undervalued by:</strong>
  <span style="color:#8B3A3A;">{ratio}×</span></p>
<p><strong>Watch type:</strong> {a.get('watch_type','—')}</p>
<p><strong>Maker:</strong> {a.get('maker','—')}</p>
<p><strong>Hallmark:</strong> {a.get('hallmark','—')}</p>
<p><strong>Period:</strong> {a.get('period','—')}</p>
<p><strong>Material:</strong> {a.get('material','—')}</p>
<p><strong>Movement:</strong> {a.get('movement','—')}</p>
<p><strong>Condition:</strong> {a.get('condition','—')}</p>
<p><strong>Recommendation:</strong> {a.get('recommendation','—').upper()}</p>
<p>{a.get('value_rationale','')}</p>
{f'<p><strong>Red Flags:</strong> {a.get("red_flags","None")}</p>' if a.get('red_flags') and a.get('red_flags') != 'none' else ''}
{deep_section}
{f'<p><a href="{listing["url"]}" style="background:#1a3a5c;color:white;padding:10px 20px;border-radius:6px;text-decoration:none;">View on eBay →</a></p>' if listing.get('url') else ''}
<p style="color:#aaa;font-size:12px;">
  eBay images: {len(listing.get('all_image_urls',[]))} |
  Manual uploads: {len(listing.get('manual_image_paths',[]))}
</p>
<p style="color:#aaa;font-size:11px;">
  AI valuations are estimates only — always inspect before purchasing.
</p>
</body></html>"""

    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "personalizations": [{"to": [{"email": to_email}]}],
                "from": {"email": FROM_EMAIL},
                "subject": subject,
                "content": [{"type": "text/html", "value": html_body}],
            },
            timeout=15,
        )
        if resp.status_code in (200, 202):
            logger.info(f"Alert sent to {to_email}: {listing['title'][:60]}")
        else:
            logger.error(f"SendGrid error: {resp.status_code}")
    except Exception as e:
        logger.error(f"Email error: {e}")

# ── Auto-archive ──────────────────────────────────────────────────────────────
def auto_archive_old_listings(data):
    cutoff    = datetime.utcnow() - timedelta(days=CONFIG["auto_archive_days"])
    archived  = 0
    for listing in data["listings"]:
        if listing.get("archived"):
            continue
        try:
            found_at = datetime.fromisoformat(listing.get("found_at", ""))
            if found_at < cutoff:
                listing["archived"] = True
                archived += 1
        except:
            pass
    if archived:
        logger.info(f"Auto-archived {archived} old listings")

# ── Main scan ─────────────────────────────────────────────────────────────────
def run_scan():
    if is_scan_paused():
        logger.info("Scan skipped — paused")
        return

    logger.info("=== Pocket Watch Scout scan starting ===")
    data = load_data()

    # Check find limit
    matches = data.get("matches_found", 0)
    if matches >= CONFIG["find_limit"]:
        logger.info(f"Find limit reached ({matches}/{CONFIG['find_limit']}) — pausing")
        set_scan_paused(True)
        return

    if not check_spend_cap(data):
        save_data(data)
        return

    data["last_scan"]  = datetime.utcnow().isoformat()
    data["scan_count"] = data.get("scan_count", 0) + 1

    token = get_ebay_token()
    if not token:
        logger.error("Could not get eBay token")
        save_data(data)
        return

    scan_cost_start = data.get("cost", {}).get("total_cost_gbp", 0.0)
    seen_ids        = set()
    new_listings    = []
    analysed_count  = 0

    for keyword in EBAY_KEYWORDS:
        if is_scan_paused():
            break
        if analysed_count >= CONFIG["max_listings_per_scan"]:
            break

        items = search_ebay(keyword, token)
        logger.info(f"Keyword '{keyword}': {len(items)} results")

        for item in items:
            if analysed_count >= CONFIG["max_listings_per_scan"]:
                break
            if is_scan_paused():
                break

            item_id = item.get("itemId", "")
            if not item_id or item_id in seen_ids:
                continue
            if already_seen(item_id, data):
                continue
            if item.get("itemLocation", {}).get("country", "") in EXCLUDED_COUNTRIES:
                continue

            try:
                price_gbp = float(item.get("price", {}).get("value", "0"))
            except ValueError:
                continue
            if price_gbp < CONFIG["min_listing_price_gbp"]:
                continue

            title       = item.get("title", "")
            description = item.get("shortDescription", "")

            if not passes_exclusion_filter(title, description):
                logger.info(f"Excluded: {title[:60]}")
                continue

            seen_ids.add(item_id)

            primary_image = None
            images = item.get("thumbnailImages") or item.get("image")
            if isinstance(images, list) and images:
                primary_image = images[0].get("imageUrl")
            elif isinstance(images, dict):
                primary_image = images.get("imageUrl")

            all_image_urls = fetch_all_listing_images(item_id, token)
            if not all_image_urls and primary_image:
                all_image_urls = [primary_image]

            ebay_url = item.get("itemWebUrl", f"https://www.ebay.co.uk/itm/{item_id}")

            listing_record = {
                "item_id":            item_id,
                "title":              title,
                "description":        description,
                "price_gbp":          price_gbp,
                "image_url":          primary_image or (all_image_urls[0] if all_image_urls else ""),
                "all_image_urls":     all_image_urls,
                "manual_image_paths": [],
                "url":                ebay_url,
                "found_at":           datetime.utcnow().isoformat(),
                "analysis":           None,
                "deep_analysis":      None,
                "deep_analysis_at":   None,
                "archived":           False,
                "alerted":            False,
                "keyword_matched":    keyword,
            }

            logger.info(f"Analysing: {title[:70]} — £{price_gbp:.2f} ({len(all_image_urls)} images)")
            analysis = analyse_with_claude(listing_record, data)
            analysed_count += 1
            time.sleep(1)

            if not analysis:
                continue

            listing_record["analysis"] = analysis

            if not analysis.get("is_target_watch"):
                logger.info(f"Not target watch: {title[:60]}")
                data["listings"].append(listing_record)
                save_data(data)
                continue

            if analysis.get("confidence") == "low":
                logger.info(f"Low confidence: {title[:60]}")
                data["listings"].append(listing_record)
                save_data(data)
                continue

            if analysis.get("movement") == "quartz":
                logger.info(f"Quartz movement — skipping: {title[:60]}")
                data["listings"].append(listing_record)
                save_data(data)
                continue

            estimated_value = float(analysis.get("estimated_value_gbp", 0))
            if estimated_value <= 0:
                data["listings"].append(listing_record)
                save_data(data)
                continue

            undervalue_ratio = estimated_value / price_gbp

            data["listings"].append(listing_record)
            new_listings.append(listing_record)
            logger.info(
                f"✅ Added: {title[:60]} | "
                f"£{price_gbp:.2f} → £{estimated_value:.2f} ({undervalue_ratio:.1f}x) | "
                f"{len(all_image_urls)} images"
            )

            if undervalue_ratio >= CONFIG["undervalue_threshold"]:
                logger.info(f"ALERT: {undervalue_ratio:.1f}x — sending email")
                listing_record["alerted"] = True
                # Alert you first
                send_alert_email(listing_record, ALERT_EMAIL)
                # Also alert client
                if CLIENT_EMAIL and CLIENT_EMAIL != ALERT_EMAIL:
                    send_alert_email(listing_record, CLIENT_EMAIL)
                data["matches_found"] = data.get("matches_found", 0) + 1
                save_data(data)

                # Check find limit
                if data["matches_found"] >= CONFIG["find_limit"]:
                    logger.info(f"Find limit reached — pausing scanner")
                    set_scan_paused(True)
                    break

            save_data(data)

    auto_archive_old_listings(data)

    scan_cost_end   = data.get("cost", {}).get("total_cost_gbp", 0.0)
    this_scan_cost  = round(scan_cost_end - scan_cost_start, 4)
    data.setdefault("cost", {})["last_scan_cost_gbp"] = this_scan_cost

    data["stats"] = {
        "total_listings":     len([l for l in data["listings"] if not l.get("archived")]),
        "total_archived":     len([l for l in data["listings"] if l.get("archived")]),
        "last_scan_new":      len(new_listings),
        "last_scan_analysed": analysed_count,
        "last_scan_cost_gbp": this_scan_cost,
        "matches_found":      data.get("matches_found", 0),
        "find_limit":         CONFIG["find_limit"],
    }

    save_data(data)
    logger.info(
        f"=== Scan complete: {analysed_count} analysed, "
        f"{len(new_listings)} new, cost £{this_scan_cost:.4f} ==="
    )

# ── Scheduler ─────────────────────────────────────────────────────────────────
def scheduler():
    time.sleep(10)
    while True:
        try:
            if not is_scan_paused():
                run_scan()
        except Exception as e:
            logger.error(f"Scan error: {e}")
        interval = CONFIG["scan_interval_hours"] * 3600
        logger.info(f"Next scan in {CONFIG['scan_interval_hours']} hours")
        time.sleep(interval)

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/listings")
def get_listings():
    data          = load_data()
    show_archived = request.args.get("show_archived", "false").lower() == "true"
    listings      = data["listings"]
    if not show_archived:
        listings = [l for l in listings if not l.get("archived")]

    def sort_key(l):
        ask = l.get("price_gbp", 0)
        val = (l.get("analysis") or {}).get("estimated_value_gbp", 0)
        return (-(val / ask) if ask > 0 and val > 0 else 0)

    listings.sort(key=sort_key)
    return jsonify({
        "listings":    listings[:100],
        "last_scan":   data.get("last_scan"),
        "scan_count":  data.get("scan_count", 0),
        "stats":       data.get("stats", {}),
        "cost":        data.get("cost", {}),
        "scan_paused": is_scan_paused(),
        "gbp_to_eur":  GBP_TO_EUR,
        "client_name": CONFIG["client_name"],
        "find_limit":  CONFIG["find_limit"],
        "matches_found": data.get("matches_found", 0),
    })

@app.route("/api/scan", methods=["POST"])
def trigger_scan():
    if is_scan_paused():
        return jsonify({"status": "paused", "message": "Scanning is paused"})
    thread = threading.Thread(target=run_scan, daemon=True)
    thread.start()
    return jsonify({"status": "Scan started"})

@app.route("/api/pause", methods=["POST"])
def pause_scan():
    set_scan_paused(True)
    return jsonify({"scan_paused": True})

@app.route("/api/resume", methods=["POST"])
def resume_scan():
    set_scan_paused(False)
    return jsonify({"scan_paused": False})

@app.route("/api/archive/<item_id>", methods=["POST"])
def toggle_archive(item_id):
    data    = load_data()
    listing = find_listing(data, item_id)
    if listing:
        listing["archived"] = not listing.get("archived", False)
        save_data(data)
        return jsonify({"archived": listing["archived"]})
    return jsonify({"error": "Not found"}), 404

@app.route("/api/delete/<item_id>", methods=["DELETE"])
def delete_listing(item_id):
    data = load_data()
    data["listings"] = [l for l in data["listings"] if l["item_id"] != item_id]
    save_data(data)
    return jsonify({"status": "deleted"})

@app.route("/api/deep-analysis/<item_id>", methods=["POST"])
def run_deep_analysis(item_id):
    data    = load_data()
    listing = find_listing(data, item_id)
    if not listing:
        return jsonify({"error": "Not found"}), 404
    logger.info(f"Deep analysis: {listing['title'][:60]}")
    result = deep_analyse_with_claude(listing, data)
    if not result:
        return jsonify({"error": "Deep analysis failed"}), 500
    listing["deep_analysis"]    = result
    listing["deep_analysis_at"] = datetime.utcnow().isoformat()
    save_data(data)
    # Send updated alert with deep analysis
    send_alert_email(listing, ALERT_EMAIL)
    return jsonify({"status": "ok", "deep_analysis": result})

@app.route("/api/upload-images/<item_id>", methods=["POST"])
def upload_images(item_id):
    data    = load_data()
    listing = find_listing(data, item_id)
    if not listing:
        return jsonify({"error": "Not found"}), 404
    saved     = []
    item_dir  = os.path.join(UPLOAD_DIR, item_id)
    try:
        os.makedirs(item_dir, exist_ok=True)
        for file in request.files.getlist("images"):
            if file and file.filename:
                filename  = secure_filename(file.filename)
                save_path = os.path.join(item_dir, filename)
                file.save(save_path)
                saved.append(save_path)
    except Exception as e:
        logger.warning(f"Upload error: {e}")
    listing.setdefault("manual_image_paths", []).extend(saved)
    save_data(data)
    return jsonify({"status": "ok", "saved": len(saved),
                    "total_manual": len(listing["manual_image_paths"])})

@app.route("/health")
def health():
    data = load_data()
    return jsonify({
        "status":        "ok",
        "scan_paused":   is_scan_paused(),
        "listings":      len(data.get("listings", [])),
        "matches_found": data.get("matches_found", 0),
        "find_limit":    CONFIG["find_limit"],
        "total_cost":    data.get("cost", {}).get("total_cost_gbp", 0.0),
        "timestamp":     datetime.utcnow().isoformat(),
    })

# ── Startup ───────────────────────────────────────────────────────────────────
logger.info(f"Starting Pocket Watch Scout — API key: {'set' if ANTHROPIC_API_KEY else 'NOT SET'}")
logger.info(f"Client: {CONFIG['client_name']} | Find limit: {CONFIG['find_limit']}")
logger.info(f"eBay App ID: {EBAY_APP_ID[:20] if EBAY_APP_ID else 'not set'}...")
threading.Thread(target=scheduler, daemon=True).start()
logger.info("Scheduler started — ready to scan!")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
