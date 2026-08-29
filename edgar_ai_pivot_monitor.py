#!/usr/bin/env python3
"""
EDGAR AI Pivot Monitor
======================
Monitors SEC EDGAR for nano-cap companies announcing AI rebrands/pivots.
Tracks 8-K filings matching AI rebrand keywords, scrapes Finviz for
pre-pivot candidates, and sends Discord alerts.

Pattern: dying nano-cap Nasdaq shells with near-zero revenue suddenly
file an 8-K announcing an AI rebrand → stock pops 300-600% overnight.
Examples: $BIRD (+582%), $MYSE (+200%), $AIXC, $GRDX

Usage:
    python edgar_ai_pivot_monitor.py                  # one-time scan (last 3 days)
    python edgar_ai_pivot_monitor.py --days 7         # scan last 7 days
    python edgar_ai_pivot_monitor.py --watch           # continuous polling (every 5 min)
    python edgar_ai_pivot_monitor.py --schedule        # smart polling (Tue-Thu 4-8pm ET only)
    python edgar_ai_pivot_monitor.py --screener        # Finviz nano-cap screener
    python edgar_ai_pivot_monitor.py --watchlist       # print watchlist

Setup:
    pip install -r requirements.txt
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

DISCLAIMER: Research tool only. Not financial advice. These are extremely
speculative, high-risk situations. You can lose 100% of your capital.
"""

import requests
import json
import time
import sys
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False


# ============================================================
# CONFIGURATION
# ============================================================

# Keywords scored by signal strength
HIGH_SIGNAL_KEYWORDS = [
    # Direct rebrands / name changes
    '"GPU-as-a-Service"',
    '"GPU as a Service"',
    '"AI compute infrastructure"',
    '"rebrand" "artificial intelligence"',
    '"name change" "AI"',
    '"neocloud"',
    '"AI-native cloud"',
    '"AI infrastructure" "rebrand"',
    # BIRD-pattern: GPU lease / AI infra acquisition plays
    '"purchase GPU assets"',
    '"GPU assets" "AI model training"',
    '"AI infrastructure sector"',
    '"lease" "GPU" "AI"',
]

MEDIUM_SIGNAL_KEYWORDS = [
    '"pivot" "GPU"',
    '"pivot" "artificial intelligence"',
    '"high-performance compute" "infrastructure"',
    '"data center" "rebrand"',
    '"agentic AI" "rebrand"',
    '"AI infrastructure" "name change"',
    '"charter amendment" "artificial intelligence"',
    # BIRD-pattern: capital raise for AI/GPU
    '"convertible notes" "GPU"',
    '"convertible notes" "AI infrastructure"',
    '"proceeds" "GPU" "AI"',
    '"purchased assets" "AI"',
]

LOW_SIGNAL_KEYWORDS = [
    '"strategic transformation" "AI"',
    '"name change" "technology"',
    '"corporate name" "artificial intelligence"',
    '"AI" "reverse merger"',
    '"GPU" "lease" "data center"',
    '"asset sale" "AI"',
    '"dissolution" "AI"',
]

# Forms to scan. 8-Ks are primary, but proxy statements carry the juicy
# shell-conversion / AI pivot details (BIRD used PREM14A for its GPU plan)
FORM_TYPES = "8-K,PREM14A,DEF 14A,DEFA14A,PRE 14A"

# EDGAR EFTS (full-text search) API
EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

# SEC requires a User-Agent header with contact info
HEADERS = {
    "User-Agent": "AIpivotMonitor research@example.com",
    "Accept": "application/json",
}

# Polling interval (seconds)
POLL_INTERVAL = 300  # 5 minutes

# Default lookback
DEFAULT_LOOKBACK_DAYS = 3

# Webhook URLs (set via environment variables)
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

# ntfy.sh topic — instant phone pushes (no Discord delay)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}" if NTFY_TOPIC else ""

# OpenAI verification — final sanity check before firing klaxon
# Set via env var. Without it, verification is skipped (klaxon fires on regex alone).
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")  # gpt-4o for accuracy, gpt-4o-mini for cost
OPENAI_MIN_CONFIDENCE = 7  # On 1-10 scale, require >= this to fire klaxon


# Eastern timezone for schedule mode
ET = ZoneInfo("America/New_York")

# Finviz settings
FINVIZ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}
MAX_MARKET_CAP_M = 10  # Filter Finviz results to under $10M
MAX_ALERT_MARKET_CAP_M = 200  # Skip Discord alerts for companies over $200M
# Note: raised from $50M because BIRD-pattern shell conversions can show
# post-pop market cap (e.g. BIRD at $107M after +500% spike). The pre-filing
# market cap was much lower. When in doubt, alert and let analysis decide.

# Persistent deduplication — track which filings we've already alerted on
# so we don't ping Discord for the same 8-K every 5 minutes
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ALERTED_FILE = os.path.join(SCRIPT_DIR, "alerted_filings.json")
EVALUATED_FILE = os.path.join(SCRIPT_DIR, "evaluated_filings.json")  # Tracks every filing GPT has read (regardless of verdict)
HEARTBEAT_FILE = os.path.join(SCRIPT_DIR, ".last_heartbeat")


# ============================================================
# PERSISTENT DEDUPLICATION
# ============================================================

def load_alerted() -> dict:
    """Load dict of already-alerted filing accession numbers -> ISO timestamp."""
    if not os.path.exists(ALERTED_FILE):
        return {}
    try:
        with open(ALERTED_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_alerted(alerted: dict):
    """Save alerted filings, purging entries older than 30 days."""
    try:
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        pruned = {k: v for k, v in alerted.items() if v >= cutoff}
        with open(ALERTED_FILE, "w") as f:
            json.dump(pruned, f, indent=2)
    except Exception as e:
        print(f"  [!] Could not save alerted file: {e}")


def mark_alerted(adsh: str, alerted: dict):
    """Mark an accession number as alerted."""
    if adsh:
        alerted[adsh] = datetime.now().isoformat()
        save_alerted(alerted)


def load_evaluated() -> dict:
    """Load dict of filing accession numbers that GPT has already evaluated.
    Maps adsh -> {timestamp, verdict, confidence}."""
    if not os.path.exists(EVALUATED_FILE):
        return {}
    try:
        with open(EVALUATED_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_evaluated(evaluated: dict):
    """Save evaluated filings, purging entries older than 30 days."""
    try:
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        pruned = {k: v for k, v in evaluated.items()
                  if isinstance(v, dict) and v.get("timestamp", "") >= cutoff}
        with open(EVALUATED_FILE, "w") as f:
            json.dump(pruned, f, indent=2)
    except Exception as e:
        print(f"  [!] Could not save evaluated file: {e}")


def mark_evaluated(adsh: str, verdict: str, confidence: int, evaluated: dict):
    """Mark an accession number as GPT-evaluated (regardless of result)."""
    if adsh:
        evaluated[adsh] = {
            "timestamp": datetime.now().isoformat(),
            "verdict": verdict,
            "confidence": confidence,
        }
        save_evaluated(evaluated)


# ============================================================
# 8-K ITEM DECODER — tells you WHY the filing matters
# ============================================================

ITEM_DESCRIPTIONS = {
    "1.01": "Material Agreement",
    "1.02": "Termination of Agreement",
    "1.03": "Bankruptcy",
    "2.01": "Asset Acquisition/Disposition",
    "2.02": "Financial Results",
    "2.03": "Off-Balance Sheet Arrangement",
    "2.04": "Delisting / Transfer Notice",
    "2.05": "Impairment Charge",
    "2.06": "Material Impairment",
    "3.01": "Delisted from Exchange",
    "3.02": "Unregistered Securities Sale",
    "3.03": "Rights Modification",
    "4.01": "Auditor Change",
    "4.02": "Non-Reliance on Financials",
    "5.01": "Corp Governance Change",
    "5.02": "Officer/Director Change",
    "5.03": "CHARTER AMENDMENT / NAME CHANGE",  # <-- THE MONEY ITEM
    "5.05": "Bylaw Amendment",
    "5.06": "SHELL COMPANY STATUS CHANGE",      # <-- ALSO HUGE
    "5.07": "Shareholder Vote",
    "5.08": "Shareholder Director Nominations",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements & Exhibits",
}

# SIC codes for "dying business" sectors — extra signal
DYING_SICS = {
    "2834": "Pharma",
    "2835": "Diagnostics",
    "2836": "Biologics",
    "4812": "Telecom",
    "4813": "Telecom",
    "4822": "Telegraph/Comms",
    "4899": "Comms Services",
    "5040": "Electronics Wholesale",
    "5065": "Electronics Parts",
    "6199": "Finance Services",
    "7372": "Software (legacy)",
    "7374": "Data Processing",
    "7389": "Misc Business Services",
}


# ============================================================
# EDGAR FULL-TEXT SEARCH
# ============================================================

def search_edgar(query: str, forms: str = "8-K", days_back: int = 3) -> list:
    """
    Search EDGAR EFTS for filings matching a query.
    Returns list of parsed filing results.
    """
    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = datetime.now().strftime("%Y-%m-%d")

    params = {
        "q": query,
        "forms": forms,
        "dateRange": "custom",
        "startdt": date_from,
        "enddt": date_to,
    }

    for attempt in range(2):
        try:
            resp = requests.get(
                EDGAR_SEARCH_URL, params=params, headers=HEADERS, timeout=20
            )
            if resp.status_code == 200:
                data = resp.json()
                hits = data.get("hits", {}).get("hits", [])
                return [_parse_edgar_hit(h) for h in hits]
            elif resp.status_code == 500 and attempt == 0:
                # EDGAR intermittently 500s on broad queries — retry once
                time.sleep(2)
                continue
            else:
                print(f"  [!] EDGAR returned {resp.status_code} for: {query}")
                return []
        except requests.exceptions.Timeout:
            if attempt == 0:
                time.sleep(2)
                continue
            print(f"  [!] EDGAR timeout for: {query}")
        except Exception as e:
            print(f"  [!] EDGAR error: {e}")
            break

    return []


def _parse_edgar_hit(hit: dict) -> dict:
    """Parse a raw EDGAR EFTS hit into a clean dict."""
    src = hit.get("_source", {})

    # Extract company name and ticker from display_names
    # Format: "Myseum.AI, Inc.  (MYSE, MYSEW)  (CIK 0001648960)"
    display = ""
    display_names = src.get("display_names", [])
    if display_names:
        display = display_names[0] if isinstance(display_names, list) else str(display_names)

    company_name = display
    ticker = ""
    # Always strip "(CIK 00012345)" from company name
    company_name = re.sub(r'\s*\(CIK\s+\d+\)', '', display).strip()
    # Require ticker to be followed by "," or ")" or "W)" — excludes CIK identifier
    ticker_match = re.search(r'\(([A-Z]{1,5})(?:[,)]|W[,)])', display)
    if ticker_match:
        candidate = ticker_match.group(1)
        if candidate != "CIK":
            ticker = candidate

    # Build filing URL from CIK + accession number
    cik = ""
    ciks = src.get("ciks", [])
    if ciks:
        cik = ciks[0].lstrip("0")  # Strip leading zeros

    adsh = src.get("adsh", "")
    filing_url = ""
    if cik and adsh:
        adsh_clean = adsh.replace("-", "")
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh_clean}/"

    # Extract document filename from hit _id (format: "adsh:filename.htm")
    hit_id = hit.get("_id", "")
    doc_filename = hit_id.split(":")[1] if ":" in hit_id else ""
    doc_url = ""
    if filing_url and doc_filename:
        doc_url = f"{filing_url}{doc_filename}"

    return {
        "company": company_name,
        "ticker": ticker,
        "cik": cik,
        "adsh": adsh,
        "form": src.get("form", "8-K"),
        "file_date": src.get("file_date", ""),
        "file_description": src.get("file_description", ""),
        "items": src.get("items", []),
        "sic": src.get("sics", [""])[0] if src.get("sics") else "",
        "inc_state": src.get("inc_states", [""])[0] if src.get("inc_states") else "",
        "biz_location": src.get("biz_locations", [""])[0] if src.get("biz_locations") else "",
        "url": filing_url,
        "doc_url": doc_url,
    }


# ============================================================
# FILING CONTENT ANALYSIS
# ============================================================

def fetch_filing_text(doc_url: str, max_chars: int = 500000) -> str:
    """Fetch an EDGAR filing document and extract plain text.
    Default 500K chars to handle large proxy statements (PREM14A can be 400K+)."""
    if not doc_url:
        return ""
    try:
        resp = requests.get(doc_url, headers=HEADERS, timeout=30)
        if resp.status_code != 200:
            return ""
        if HAS_BS4:
            soup = BeautifulSoup(resp.text, "html.parser")
            text = soup.get_text(separator=" ", strip=True)
        else:
            # Fallback: strip HTML tags with regex
            text = re.sub(r"<[^>]+>", " ", resp.text)
            text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception:
        return ""


def _smart_excerpt(text: str, target_chars: int = 18000) -> str:
    """Return a focused excerpt: first 8K + windows around trigger terms.
    Better than truncation — captures the lead AND any relevant body sections."""
    if len(text) <= target_chars:
        return text

    chunks = [text[:8000]]  # Always keep the lead/header
    used = 8000
    remaining = target_chars - used

    triggers = [
        "rebrand", "name change", "pivot", "artificial intelligence",
        "GPU", "AI infrastructure", "neocloud", "data center",
        "asset purchase agreement", "purchase GPU", "AI model training",
        "shell company", "reverse merger", "dissolution", "wind-down",
    ]
    seen_starts = set()
    for term in triggers:
        for m in re.finditer(term, text[8000:], re.IGNORECASE):
            real_start = m.start() + 8000
            window_start = max(8000, real_start - 1500)
            window_end = min(len(text), real_start + 1500)
            # Avoid overlapping windows
            key = window_start // 1000
            if key in seen_starts:
                continue
            seen_starts.add(key)
            window_size = window_end - window_start
            if window_size > remaining:
                break
            chunks.append(f"\n[...]\n{text[window_start:window_end]}")
            remaining -= window_size + 10
            if remaining < 1000:
                break
        if remaining < 1000:
            break

    return "".join(chunks)


def evaluate_filing(filing: dict, market_data: Optional[dict] = None) -> dict:
    """
    Pure-GPT evaluation. The ONE function that decides whether a filing is a
    real nano-cap AI pivot worthy of klaxon, or a false positive to ignore.
    Replaces all the regex heuristics — GPT reads the filing and judges.

    Returns dict with: confirmed (bool), confidence (1-10), reasoning (str),
                       new_name (str, if name change), summary (str), tokens info.
    """
    if not OPENAI_API_KEY:
        return {"confirmed": False, "confidence": 0,
                "reasoning": "No OpenAI API key configured",
                "tokens_in": 0, "tokens_out": 0}

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        return {"confirmed": False, "confidence": 0,
                "reasoning": f"OpenAI client init failed: {e}",
                "tokens_in": 0, "tokens_out": 0}

    text = fetch_filing_text(filing.get("doc_url", ""), max_chars=500000)
    if not text:
        return {"confirmed": False, "confidence": 0,
                "reasoning": "Could not fetch filing text",
                "tokens_in": 0, "tokens_out": 0}

    excerpt = _smart_excerpt(text, target_chars=18000)

    ticker = filing.get("ticker", "N/A")
    company = filing.get("company", "Unknown")
    form = filing.get("form", "?")
    items = ", ".join(filing.get("items", [])) or "none"
    file_date = filing.get("file_date", "?")
    file_desc = filing.get("file_description", "?")
    inc_state = filing.get("inc_state", "?")
    sic = filing.get("sic", "?")

    mcap = "?"
    price = "?"
    revenue = "?"
    if market_data:
        mcap = market_data.get("market_cap_str", "?")
        price = market_data.get("price", "?")
        revenue = market_data.get("sales", "?")

    system_prompt = """You are a financial filing analyst specializing in nano-cap stock pump catalysts.

Your job: read SEC filings and judge whether the company is announcing a NEW pivot to AI/GPU/AI-infrastructure that would qualify as a "nano-cap AI rebrand pump" candidate. Two reference cases:
- $MYSE: rebranded from "DatChat Inc." to "Myseum.AI, Inc." in April 2026 with an Item 5.03 charter amendment. Stock popped 200%+.
- $BIRD: Allbirds entered an asset purchase agreement to sell its footwear business, then announced plans to rename to "NewBird AI, Inc." and use proceeds to purchase GPU assets for AI model training. Stock popped 500%+.

CONFIRM (confidence 8-10) ONLY if ALL of these are true:
1. The company is small/nano-cap (< $200M market cap; sweet spot < $50M)
2. The filing announces a CURRENT, NEW event happening NOW (not historical recap)
3. The pivot is specifically to AI / GPU / AI-compute / neocloud / AI-infrastructure
4. There's concrete actionable language, e.g.:
   - "today announced rebrand to [X AI]"
   - "entered into definitive asset purchase agreement... proceeds to purchase GPU assets"
   - "filed certificate of amendment changing name to [X AI Inc]"
   - "consummation of the merger... will operate as [X AI]"

REJECT (confidence 1-5) if ANY of these:
- Company is already an established AI company (existing business, not a NEW pivot)
- Filing is a routine annual proxy / 10-K / quarterly earnings recapping company history
- Filing is an investor presentation describing existing operations
- Filing is large-cap or mid-cap (> $200M market cap)
- AI mentions are incidental (boilerplate, describing one division being sold off, "we use AI internally", etc.)
- The pivot target is something else (entertainment, ticketing, real estate, biotech, mining, defense, etc.)
- The "name change" or "rebrand" happened months/years ago and is only mentioned as historical context
- The filing is just registering shares or amending a prior filing without new business news
- The company's name already contains "AI" but no new pivot event is announced (they were already an AI company)

Respond with ONLY a JSON object (no markdown, no preamble):
{
  "confirmed": <true|false>,
  "confidence": <1-10 integer>,
  "reasoning": "<2-4 sentence explanation>",
  "new_name": "<the new company name if there's a rebrand, else empty string>",
  "summary": "<one-sentence punchy summary of what the filing actually announces>"
}"""

    user_prompt = f"""Filing metadata:
- Ticker: ${ticker}
- Company: {company}
- Form: {form} | Date: {file_date} | Description: {file_desc}
- 8-K Items: {items}
- Inc. State: {inc_state} | SIC: {sic}
- Market Cap: {mcap} | Price: ${price} | Revenue: {revenue}

Filing excerpt (first 8K chars + ±1.5K windows around trigger terms):
---
{excerpt}
---

Is this a real nano-cap AI pivot announcement worthy of buying immediately, or a false positive? Respond ONLY with the JSON object."""

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        result = json.loads(content)

        return {
            "confirmed": bool(result.get("confirmed", False)),
            "confidence": int(result.get("confidence", 0)),
            "reasoning": str(result.get("reasoning", ""))[:600],
            "new_name": str(result.get("new_name", ""))[:100],
            "summary": str(result.get("summary", ""))[:300],
            "model": OPENAI_MODEL,
            "tokens_in": resp.usage.prompt_tokens,
            "tokens_out": resp.usage.completion_tokens,
        }
    except Exception as e:
        print(f"  [!] OpenAI evaluation failed: {e}")
        return {"confirmed": False, "confidence": 0,
                "reasoning": f"OpenAI error: {e}",
                "tokens_in": 0, "tokens_out": 0}


def analyze_filing(filing: dict) -> dict:
    """
    Fetch and analyze a filing to determine if it's a REAL AI pivot/rebrand.
    Uses proximity analysis — AI terms must appear near pivot/rebrand language,
    not just anywhere in the same document.
    Returns analysis dict with verdict, signals, old_name, summary, score.
    """
    doc_url = filing.get("doc_url", "")
    text = fetch_filing_text(doc_url)

    if not text:
        return {"verdict": "UNREADABLE", "summary": "Could not fetch filing text", "signals": [], "score": 0}

    text_lower = text.lower()
    signals = []
    score = 0

    # === HISTORICAL CONTEXT FILTER ===
    # Reject filings that are routine annual proxies just recapping company history.
    # Annual proxies always discuss past name changes, SPAC mergers, etc.
    # Only NEW pivot events should trigger our high-confidence signals.
    file_desc_lower = filing.get("file_description", "").lower()
    form_type = filing.get("form", "").upper()
    is_annual_proxy = (
        ("def 14a" in form_type.lower() or form_type == "DEF 14A")
        and ("annual meeting" in text_lower[:5000] or "annual proxy" in text_lower[:5000])
    )

    # Detect "current pivot language" — must be present-tense / current event
    current_pivot_phrases = [
        "today announced", "today reported", "is announcing",
        "effective immediately", "effective today",
        "will operate under the new name",
        "will be known as",
        "has entered into a definitive",
        "has signed a definitive",
        "approved a name change",
        "filed a certificate of amendment",
    ]
    has_current_pivot = any(p in text_lower for p in current_pivot_phrases)

    # === CORE CHECK: Does the new company name contain "AI"? ===
    ai_in_name = False
    new_name = ""
    name_with_ai_patterns = [
        r'(?:rebrand(?:s|ed|ing)?|new name|operate under the new name|changed? its (?:corporate )?name to)\s+([A-Z][A-Za-z0-9\.\s]+?AI[A-Za-z0-9\.\s]*?)(?:\s+Inc|\s+Corp|\s+Ltd|,|\.|to )',
        r'(?:rebrand(?:s|ed|ing)?\s+as)\s+([A-Z][A-Za-z0-9\.\s]*?AI[A-Za-z0-9\.\s]*?)(?:\s+to\s|\s+Inc|\s+Corp|\s+Ltd|,|\.|$)',
    ]
    for pat in name_with_ai_patterns:
        m = re.search(pat, text)
        if m:
            # Check for HISTORICAL context: past dates, "in [Month] [Year]", "previously"
            window_start = max(0, m.start() - 200)
            window = text[window_start:m.start()].lower()
            historical_markers = [
                r'in (january|february|march|april|may|june|july|august|september|october|november|december) \d{4}',
                r'in \d{4}',
                r'previously',
                r'historically',
                r'prior to',
                r'legacy',
                r'(formed|founded|incorporated|organized) in',
            ]
            is_historical = any(re.search(p, window) for p in historical_markers)

            if is_historical:
                signals.append(f"HISTORICAL_NAME_CHANGE_REJECTED: {m.group(1).strip()}")
                score -= 10  # Penalty for historical context, no bonus
            else:
                new_name = m.group(1).strip()
                ai_in_name = True
                signals.append(f"NEW_NAME_HAS_AI: {new_name}")
                score += 40
            break

    # === SIGNAL 1: Name change / rebrand (general) ===
    name_change = False
    name_patterns = [
        r'(?:rebrand|rebranding|rebranded)\s+(?:as|to|into)\s+',
        r'(?:new name|operate under the new name|changed? its (?:corporate )?name to)\s+',
        r'(?:name change|change of name|charter amendment)',
    ]
    for pat in name_patterns:
        if re.search(pat, text, re.IGNORECASE):
            name_change = True
            break

    if name_change or "rebrand" in text_lower:
        # Only credit name-change signal if there's CURRENT pivot language
        # (historical mentions in annual proxies don't count)
        if has_current_pivot:
            if not ai_in_name:
                signals.append("NAME_CHANGE")
            score += 15
        else:
            signals.append("NAME_CHANGE_HISTORICAL (no current pivot language)")
            # No score bonus

    # Check for "formerly" — but require it to be near company name, not in a bio
    old_name = ""
    formerly_matches = re.findall(r'\(formerly\s+([^)]+)\)', text, re.IGNORECASE)
    if formerly_matches:
        candidate_old = formerly_matches[0].strip()
        # Reject if it looks like a stock ticker reference (NYSE: XXX, Nasdaq: XXX)
        # — those are usually board members' previous companies, not the issuer's old name
        if re.search(r'(NYSE|Nasdaq|NYSE American|NYSE MKT|OTC):\s*[A-Z]+', candidate_old):
            signals.append(f"FORMERLY_REJECTED: {candidate_old} (looks like a ticker reference, not issuer's old name)")
        else:
            old_name = candidate_old
            signals.append(f"FORMERLY: {old_name}")
            score += 10

    # === SIGNAL 2: AI buzzwords — PROXIMITY CHECK ===
    # Count AI terms, but also check if they appear NEAR pivot/rebrand language
    ai_terms = [
        "artificial intelligence", "GPU-as-a-Service", "GPU as a Service",
        "AI infrastructure", "AI compute", "agentic AI", "AI-native",
        "neocloud", "AI platform", "machine learning infrastructure",
        "high-performance compute", "GPU cluster", "AI data center",
        "GPU lease", "AI rebrand",
    ]
    ai_hits = []
    total_ai_count = 0
    for term in ai_terms:
        count = text_lower.count(term.lower())
        if count > 0:
            ai_hits.append(f"{term} (x{count})")
            total_ai_count += count

    if ai_hits:
        signals.append(f"AI_TERMS: {', '.join(ai_hits[:5])}")
        # Score based on density: 1 mention = weak, 3+ = strong
        if total_ai_count >= 5:
            score += 30
        elif total_ai_count >= 3:
            score += 20
        elif total_ai_count >= 2:
            score += 10
        else:
            score += 5  # Single mention = very weak, could be incidental

    # === SIGNAL 3: PROXIMITY — AI terms near pivot/rebrand language ===
    # This catches KUST-type false positives where "AI" and "pivot" appear
    # in the same doc but are about completely different things
    pivot_near_ai = False
    pivot_terms_re = r'(?:pivot|rebrand|name change|new name|transformation)'
    ai_terms_re = r'(?:artificial intelligence|AI infrastructure|GPU|AI compute|AI platform|AI data center|neocloud|AI-native)'
    # Check if pivot and AI terms appear within 300 chars of each other
    for m in re.finditer(pivot_terms_re, text_lower):
        window_start = max(0, m.start() - 300)
        window_end = min(len(text_lower), m.end() + 300)
        window = text_lower[window_start:window_end]
        if re.search(ai_terms_re, window):
            pivot_near_ai = True
            break

    if pivot_near_ai:
        signals.append("PIVOT_NEAR_AI (confirmed proximity)")
        score += 20
    elif total_ai_count > 0:
        signals.append("AI_NOT_NEAR_PIVOT (likely incidental)")
        score -= 15  # Penalty — AI mentioned but not related to the pivot

    # === SIGNAL 4: What are they pivoting TO? ===
    # Check if pivot language points to AI vs something else
    pivot_to_ai = False
    pivot_to_other = ""
    pivot_to_patterns = [
        (r'pivot\s+(?:to|toward|towards|into)\s+(?:the\s+)?(.{10,80}?)(?:\.|,|$)', None),
    ]
    for pat, _ in pivot_to_patterns:
        m = re.search(pat, text_lower)
        if m:
            pivot_target = m.group(1).strip()
            if any(t in pivot_target for t in ["ai", "artificial intelligence", "gpu", "compute", "data center"]):
                pivot_to_ai = True
                signals.append(f"PIVOT_TO_AI: '{pivot_target[:60]}'")
                score += 15
            else:
                pivot_to_other = pivot_target[:60]
                signals.append(f"PIVOT_TO_OTHER: '{pivot_to_other}'")
                score -= 10  # Pivoting to something else, not AI
            break

    # === SIGNAL 5: Revenue / business signals ===
    if any(x in text_lower for x in ["zero revenue", "no revenue", "pre-revenue",
                                       "minimal revenue", "limited operations"]):
        signals.append("ZERO_REVENUE")
        score += 15

    # === SIGNAL 6: Shell company / reverse merger ===
    # Must be ACTIVE shell status, not historical "we used to be a blank check company"
    shell_terms_active = [
        "change of shell company status",
        "ceased to be a shell company",
        "is currently a shell company",
        "we are a shell company",
        "the company is a shell company",
        "completion of the merger" ,
        "consummation of the merger",
    ]
    shell_terms_historical_markers = [
        "prior to the business combination",
        "prior to the merger",
        "we were a blank check",
        "we were a shell",
        "previously a shell",
    ]
    has_active_shell = any(t in text_lower for t in shell_terms_active)
    has_historical_shell_only = (
        any(t in text_lower for t in ["shell company", "blank check"])
        and any(t in text_lower for t in shell_terms_historical_markers)
        and not has_active_shell
    )
    if has_active_shell:
        signals.append("SHELL_COMPANY (active)")
        score += 20
    elif has_historical_shell_only:
        signals.append("SHELL_HISTORICAL_REJECTED (was a shell, not anymore)")
        # No score

    # === SIGNAL 6b: GPU lease / AI infrastructure acquisition (BIRD pattern) ===
    gpu_acquisition_patterns = [
        ("purchase gpu assets", 40, "GPU_ASSET_PURCHASE"),
        ("gpu assets", 20, "GPU_ASSETS"),
        ("ai infrastructure sector", 35, "AI_INFRA_SECTOR"),
        ("ai model training", 20, "AI_MODEL_TRAINING"),
        ("lease" , 0, None),  # just for proximity check
    ]
    bird_pattern_score = 0
    for phrase, pts, sig_name in gpu_acquisition_patterns:
        if phrase in text_lower and sig_name:
            bird_pattern_score = max(bird_pattern_score, pts)
            signals.append(sig_name)

    if bird_pattern_score > 0:
        score += bird_pattern_score
        # Override the "AI_NOT_NEAR_PIVOT" penalty if we see this pattern
        signals = [s for s in signals if s != "AI_NOT_NEAR_PIVOT (likely incidental)"]

    # Check for the full BIRD pattern: asset sale/wind-down + GPU acquisition
    # Must be ACTIONABLE language (entering into, proceeds to purchase GPUs) —
    # NOT passive mentions like "gain on asset sales" in financial statements
    # or descriptions of an existing AI business.
    actionable_asset_sale = any(x in text_lower for x in [
        "definitive asset purchase agreement",
        "entered into an asset purchase agreement",
        "entered into a definitive agreement",
        "asset purchase agreement with",
        "dissolution and winding down",
        "wind-down of the company",
        "plan of dissolution",
        "asset sale and subsequent dissolution",
    ])
    proceeds_to_ai = any(x in text_lower for x in [
        "proceeds" + " " + y for y in ["to purchase gpu", "used to purchase gpu", "to acquire gpu"]
    ]) or "proceeds from the facility are anticipated to be used to purchase gpu" in text_lower \
       or ("purchase gpu assets" in text_lower and "proceeds" in text_lower)

    # Reject if this is just an investor presentation describing an existing business
    file_desc_lower = filing.get("file_description", "").lower()
    is_presentation = any(x in file_desc_lower for x in [
        "investor presentation", "corporate presentation", "slide deck",
        "powerpoint presentation", "investor deck",
    ])

    if actionable_asset_sale and proceeds_to_ai and not is_presentation:
        signals.append("SHELL_CONVERSION_TO_AI (new asset sale + proceeds to GPU)")
        score += 40
    elif actionable_asset_sale and proceeds_to_ai and is_presentation:
        signals.append("WEAK_SHELL_PATTERN (presentation only — verify manually)")
        score += 10

    # === SIGNAL 7: 8-K Items check ===
    items = filing.get("items", [])
    if "5.03" in items:
        signals.append("ITEM_5.03: Charter Amendment (name change filing)")
        score += 10
    if "5.06" in items:
        signals.append("ITEM_5.06: Shell Company Status Change")
        score += 15

    # === SIGNAL 8: Convertible / dilutive financing ===
    if any(x in text_lower for x in ["convertible note", "convertible debenture",
                                       "registered direct", "pipe offering"]):
        signals.append("DILUTIVE_FINANCING")
        score += 5

    # === Extract key sentences for summary ===
    summary_sentences = []
    sentences = re.split(r'(?<=[.!])\s+', text[:8000])
    priority_terms = ["rebrand", "name change", "pivot to", "pivot toward",
                      "artificial intelligence", "GPU", "AI infrastructure",
                      "new name", "formerly", "AI data center"]
    for sent in sentences:
        sent_lower = sent.lower()
        if any(t in sent_lower for t in priority_terms):
            clean = sent.strip()[:200]
            if len(clean) > 30:
                summary_sentences.append(clean)
            if len(summary_sentences) >= 3:
                break

    summary = " | ".join(summary_sentences) if summary_sentences else "No clear pivot language found."

    # === Final verdict ===
    score = max(score, 0)  # Floor at 0
    # BIRD-pattern: new asset sale + proceeds going to GPU purchase (strict)
    shell_to_ai_strict = "SHELL_CONVERSION_TO_AI (new asset sale + proceeds to GPU)" in signals
    gpu_purchase = "GPU_ASSET_PURCHASE" in signals
    ai_infra = "AI_INFRA_SECTOR" in signals

    if ai_in_name and score >= 60:
        verdict = "CONFIRMED_PIVOT"
    elif pivot_near_ai and score >= 50 and ai_in_name:
        verdict = "CONFIRMED_PIVOT"
    elif shell_to_ai_strict and gpu_purchase:
        verdict = "CONFIRMED_PIVOT"  # BIRD pattern — verified new shell conversion
    elif score >= 50:
        verdict = "LIKELY_PIVOT"
    elif score >= 30:
        verdict = "POSSIBLE"
    else:
        verdict = "UNLIKELY"

    return {
        "verdict": verdict,
        "score": score,
        "signals": signals,
        "summary": summary[:500],
        "old_name": old_name,
        "new_name": new_name,
    }


# ============================================================
# FINVIZ SCREENER
# ============================================================

def scrape_finviz_screener() -> list:
    """
    Scrape Finviz for Nasdaq nano-cap stocks with declining revenue.
    Returns list of dicts with ticker, company, market cap, price, etc.
    """
    if not HAS_BS4:
        print("  [!] beautifulsoup4 required for Finviz scraper: pip install beautifulsoup4")
        return []

    # Finviz screener filters:
    # cap_nano = under $50M market cap
    # exch_nasd = NASDAQ
    # fa_salesqoq_neg = declining quarterly revenue
    base_url = "https://finviz.com/screener.ashx"
    filters = "cap_nano,exch_nasd,fa_salesqoq_neg"

    all_results = []
    offset = 1  # Finviz pagination starts at 1

    while True:
        params = {
            "v": "111",  # Overview view: No|Ticker|Company|Sector|Industry|Country|MktCap|P/E|Price|Change|Volume
            "f": filters,
            "r": str(offset),
        }

        try:
            resp = requests.get(
                base_url, params=params, headers=FINVIZ_HEADERS, timeout=15
            )
            if resp.status_code != 200:
                print(f"  [!] Finviz returned {resp.status_code}")
                break

            soup = BeautifulSoup(resp.text, "html.parser")

            # Find the screener results table - look for the table with ticker data
            table = None
            for t in soup.find_all("table"):
                # The results table contains rows with ticker links
                if t.find("a", class_="screener-link-primary"):
                    table = t
                    break

            if not table:
                # Fallback: look for table with class
                table = soup.find("table", class_="screener_table")

            if not table:
                break

            rows = table.find_all("tr")[1:]  # Skip header
            if not rows:
                break

            page_results = []
            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 11:
                    continue

                # v=111 columns: No|Ticker|Company|Sector|Industry|Country|MktCap|P/E|Price|Change|Volume
                # Parse market cap (e.g., "5.23M", "1.2B")
                mcap_str = cols[6].get_text(strip=True)
                mcap_m = _parse_market_cap(mcap_str)

                # Filter to under MAX_MARKET_CAP_M
                if mcap_m is not None and mcap_m > MAX_MARKET_CAP_M:
                    continue

                result = {
                    "ticker": cols[1].get_text(strip=True),
                    "company": cols[2].get_text(strip=True),
                    "sector": cols[3].get_text(strip=True),
                    "industry": cols[4].get_text(strip=True),
                    "country": cols[5].get_text(strip=True),
                    "market_cap_m": mcap_m,
                    "market_cap_str": mcap_str,
                    "pe": cols[7].get_text(strip=True),
                    "price": cols[8].get_text(strip=True),
                    "volume": cols[10].get_text(strip=True) if len(cols) > 10 else "",
                }
                page_results.append(result)

            all_results.extend(page_results)

            # Check if there are more pages (Finviz shows 20 per page)
            if len(rows) < 20:
                break
            offset += 20

            # Rate limit - be nice to Finviz
            time.sleep(1)

        except Exception as e:
            print(f"  [!] Finviz scraper error: {e}")
            break

    return all_results


def _parse_market_cap(mcap_str: str) -> Optional[float]:
    """Parse Finviz market cap string to millions. '5.23M' -> 5.23, '1.2B' -> 1200"""
    if not mcap_str or mcap_str == "-":
        return None
    mcap_str = mcap_str.strip()
    try:
        if mcap_str.endswith("B"):
            return float(mcap_str[:-1]) * 1000
        elif mcap_str.endswith("M"):
            return float(mcap_str[:-1])
        elif mcap_str.endswith("K"):
            return float(mcap_str[:-1]) / 1000
        else:
            return float(mcap_str)
    except ValueError:
        return None


def lookup_ticker_finviz(ticker: str) -> Optional[dict]:
    """Quick Finviz lookup for a single ticker. Returns market cap, price, volume, etc."""
    if not ticker or not HAS_BS4:
        return None

    try:
        url = f"https://finviz.com/quote.ashx?t={ticker}"
        resp = requests.get(url, headers=FINVIZ_HEADERS, timeout=8)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        data = {}

        # Finviz snapshot table uses class "snapshot-td2" for both labels and values
        # Labels have "cursor-pointer", values are the next sibling td
        FIELDS = {
            "Market Cap": ("market_cap_str", None),
            "Price": ("price", None),
            "Avg Volume": ("avg_volume", None),
            "Volume": ("volume", None),
            "Shs Float": ("float", None),
            "Short Float": ("short_float", None),
            "Inst Own": ("inst_own", None),
            "Income": ("income", None),
            "Sales": ("sales", None),
        }

        all_tds = soup.find_all("td", class_="snapshot-td2")
        for td in all_tds:
            text = td.get_text(strip=True)
            if text in FIELDS:
                value_cell = td.find_next_sibling("td")
                if value_cell:
                    val = value_cell.get_text(strip=True)
                    key, _ = FIELDS[text]
                    data[key] = val

        # Parse market cap to numeric
        if "market_cap_str" in data:
            data["market_cap_m"] = _parse_market_cap(data["market_cap_str"])

        return data if data else None

    except Exception:
        return None


def print_finviz_results(results: list):
    """Print Finviz screener results."""
    print(f"\n{'='*70}")
    print(f"  FINVIZ NANO-CAP SCREENER (Nasdaq, declining revenue, <${MAX_MARKET_CAP_M}M)")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Found {len(results)} candidates")
    print(f"{'='*70}")
    print(f"{'Ticker':<8} {'Mkt Cap':>8} {'Price':>8} {'Company':<30} {'Industry'}")
    print(f"{'-'*8} {'-'*8} {'-'*8} {'-'*30} {'-'*25}")
    for r in results:
        mcap = f"${r['market_cap_m']:.1f}M" if r['market_cap_m'] else r['market_cap_str']
        print(f"{r['ticker']:<8} {mcap:>8} {r['price']:>8} {r['company'][:30]:<30} {r['industry'][:25]}")
    print()


# ============================================================
# NOTIFICATION SYSTEM
# ============================================================

def _decode_items(items: list) -> str:
    """Decode 8-K item numbers into readable descriptions. Flag key items."""
    if not items:
        return "N/A"
    parts = []
    for item in items:
        desc = ITEM_DESCRIPTIONS.get(item, item)
        # Flag the critical items
        if item in ("5.03", "5.06"):
            parts.append(f"**{item}: {desc}** !!!!")
        else:
            parts.append(f"{item}: {desc}")
    return "\n".join(parts)


def _sic_label(sic: str) -> str:
    """Get human-readable label for SIC code."""
    if sic in DYING_SICS:
        return f"{sic} ({DYING_SICS[sic]}) — dying sector"
    return sic or "N/A"


def send_discord_alert(signal_level: str, filing: dict, keyword: str,
                       market_data: Optional[dict] = None,
                       analysis: Optional[dict] = None):
    """Send rich actionable alert to Discord."""
    if not DISCORD_WEBHOOK:
        return

    company = filing.get("company", "Unknown")
    ticker = filing.get("ticker", "")
    url = filing.get("url", "")
    file_date = filing.get("file_date", "")
    filing_desc = filing.get("file_description", filing.get("form", "8-K"))
    inc_state = filing.get("inc_state", "")
    sic = filing.get("sic", "")
    items = filing.get("items", [])

    # Color based on analysis verdict, not just keyword signal level
    verdict = analysis.get("verdict", "") if analysis else ""
    if verdict == "CONFIRMED_PIVOT":
        color = 0xFF0000  # Red — go time
    elif verdict == "LIKELY_PIVOT":
        color = 0xFF8C00  # Orange — worth checking
    elif verdict == "POSSIBLE":
        color = 0xFFFF00  # Yellow — maybe
    else:
        color = 0x3498DB  # Blue — low confidence

    verdict_emoji = {
        "CONFIRMED_PIVOT": ":rotating_light: CONFIRMED PIVOT",
        "LIKELY_PIVOT": ":warning: LIKELY PIVOT",
        "POSSIBLE": ":grey_question: POSSIBLE",
        "UNLIKELY": ":x: UNLIKELY",
        "UNREADABLE": ":question: UNREADABLE",
    }.get(verdict, verdict)

    # Build title with ticker and verdict
    if ticker:
        title = f"${ticker} — {verdict_emoji}"
    else:
        title = f"{company[:30]} — {verdict_emoji}"

    # Core fields
    fields = [
        {"name": "Company", "value": company[:100], "inline": True},
        {"name": "Ticker", "value": f"**${ticker}**" if ticker else "N/A", "inline": True},
        {"name": "Filed", "value": file_date or "N/A", "inline": True},
    ]

    # Market data from Finviz (if available)
    if market_data:
        mcap = market_data.get("market_cap_str", "N/A")
        price = market_data.get("price", "N/A")
        vol = market_data.get("volume", market_data.get("avg_volume", "N/A"))
        inst = market_data.get("inst_own", "N/A")
        short_f = market_data.get("short_float", "")
        sales = market_data.get("sales", "")

        fields.append({"name": "Mkt Cap", "value": mcap, "inline": True})
        fields.append({"name": "Price", "value": f"${price}", "inline": True})
        fields.append({"name": "Volume", "value": vol, "inline": True})
        if inst and inst != "N/A":
            fields.append({"name": "Inst Own", "value": inst, "inline": True})
        if short_f:
            fields.append({"name": "Short Float", "value": short_f, "inline": True})
        if sales:
            fields.append({"name": "Revenue", "value": sales, "inline": True})
    else:
        fields.append({"name": "Mkt Cap", "value": "lookup failed", "inline": True})

    # --- GPT-4o ANALYSIS (the only thing that matters now) ---
    if analysis:
        gpt = analysis.get("gpt_verification") or {}
        if gpt:
            conf = gpt.get("confidence", 0)
            fields.append({
                "name": "🤖 GPT-4o Verdict",
                "value": f"**CONFIRMED** — confidence **{conf}/10**",
                "inline": False,
            })
            # Filing summary (GPT's one-liner)
            gpt_summary = gpt.get("summary", "") or analysis.get("summary", "")
            if gpt_summary:
                fields.append({"name": "What's Happening", "value": gpt_summary[:500], "inline": False})
            # Reasoning
            fields.append({
                "name": "Why It's Real",
                "value": f"_{gpt.get('reasoning','')[:600]}_",
                "inline": False,
            })
            # New name if known
            new_n = gpt.get("new_name", "") or analysis.get("new_name", "")
            if new_n:
                fields.append({"name": "New Company Name", "value": f"**{new_n}**", "inline": False})

    # Filing details
    fields.append({"name": "Filing", "value": filing_desc[:100] or "8-K", "inline": False})

    # 8-K items decoded
    decoded = _decode_items(items)
    fields.append({"name": "8-K Items", "value": decoded[:300], "inline": False})

    # Incorporation state + SIC
    state_flag = ""
    if inc_state in ("NV", "DE"):
        state_flag = f"**{inc_state}** (easy name change)"
    elif inc_state:
        state_flag = inc_state
    else:
        state_flag = "N/A"

    fields.append({"name": "Inc. State", "value": state_flag, "inline": True})
    fields.append({"name": "Old Biz (SIC)", "value": _sic_label(sic), "inline": True})

    # --- BUY LINK — make it huge and obvious ---
    if ticker:
        robinhood_url = f"https://robinhood.com/stocks/{ticker}?source=search"
        fields.append({
            "name": "🟢 BUY ON ROBINHOOD 🟢",
            "value": f"# [**➤ ${ticker} on Robinhood**]({robinhood_url})",
            "inline": False,
        })

    # Secondary links (smaller)
    secondary = []
    if url:
        secondary.append(f"[EDGAR]({url})")
    if ticker:
        secondary.append(f"[Finviz](https://finviz.com/quote.ashx?t={ticker})")
        secondary.append(f"[TradingView](https://www.tradingview.com/chart/?symbol={ticker})")
        secondary.append(f"[Stocktwits](https://stocktwits.com/symbol/{ticker})")
    if secondary:
        fields.append({"name": "More", "value": " · ".join(secondary), "inline": False})

    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if url:
        embed["url"] = url

    # Attention-grabbing content for CONFIRMED pivots only — @everyone pings through DND
    if verdict == "CONFIRMED_PIVOT":
        content = (
            f"@everyone\n"
            f"# 🚨🚨🚨 CONFIRMED AI PIVOT 🚨🚨🚨\n"
            f"## ${ticker} — {company[:60]}\n"
            f"### ➤ [**BUY ON ROBINHOOD**](https://robinhood.com/stocks/{ticker}?source=search)"
            if ticker
            else f"@everyone\n# 🚨🚨🚨 CONFIRMED AI PIVOT 🚨🚨🚨\n## {company[:60]}"
        )
    else:
        content = ""

    payload = {
        "content": content,
        "embeds": [embed],
        "allowed_mentions": {"parse": ["everyone"]},
    }

    try:
        resp = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if resp.status_code not in (200, 204):
            print(f"  [!] Discord webhook returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"  [!] Discord alert failed: {e}")


def send_slack_alert(message: str):
    """Send alert to Slack webhook."""
    if not SLACK_WEBHOOK:
        return
    try:
        requests.post(SLACK_WEBHOOK, json={"text": message}, timeout=10)
    except Exception as e:
        print(f"  [!] Slack alert failed: {e}")


def send_desktop_notification(title: str, message: str):
    """Send macOS desktop notification."""
    try:
        # Escape quotes to prevent injection
        safe_msg = message[:200].replace('"', '\\"')
        safe_title = title.replace('"', '\\"')
        os.system(f'''osascript -e 'display notification "{safe_msg}" with title "{safe_title}"' ''')
    except Exception:
        pass


def klaxon_macos(ticker: str, company: str):
    """Full audio+visual alarm on the Mac for a CONFIRMED pivot.
    Loud sound + spoken voice + modal dialog that requires action to dismiss."""
    try:
        # Play loud system alert sound (Sosumi is sharp & attention-grabbing)
        os.system("afplay /System/Library/Sounds/Sosumi.aiff &")
        os.system("afplay /System/Library/Sounds/Sosumi.aiff &")
        # Speak it out loud
        safe_ticker = re.sub(r'[^A-Z]', '', (ticker or "").upper()) or "UNKNOWN"
        safe_company = re.sub(r'[^a-zA-Z0-9 ]', '', company[:40]) or "a nano-cap"
        os.system(f'''say -v Samantha "Attention. Confirmed A I pivot detected. Ticker {' '.join(safe_ticker)}. {safe_company}. Buy window open." &''')
        # Flashing modal dialog (stays up until dismissed)
        safe_dialog = f"CONFIRMED AI PIVOT\\n\\n${safe_ticker}\\n{company[:60]}\\n\\nTap Buy to open Robinhood".replace('"', '\\"')
        os.system(f'''osascript -e 'display dialog "{safe_dialog}" with title "🚨 AI PIVOT ALERT 🚨" buttons {{"Dismiss", "Buy on Robinhood"}} default button 2 with icon stop' -e 'if button returned of result is "Buy on Robinhood" then do shell script "open https://robinhood.com/stocks/{safe_ticker}?source=search"' &''')
    except Exception as e:
        print(f"  [!] Klaxon failed: {e}")


def send_ntfy_alert(title: str, message: str, ticker: str = "",
                    edgar_url: str = "", priority: int = 5,
                    tags: Optional[list] = None):
    """
    Send instant push to phone via ntfy.sh using JSON POST (supports UTF-8/emoji).
    Bypasses Discord's mobile notification delay entirely.
    """
    if not NTFY_TOPIC:
        return
    if tags is None:
        tags = ["rotating_light", "moneybag"]

    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags,
    }

    # Tapping the notification opens Robinhood directly
    if ticker:
        payload["click"] = f"https://robinhood.com/stocks/{ticker}?source=search"
        actions = [{
            "action": "view",
            "label": "Buy on Robinhood",
            "url": f"https://robinhood.com/stocks/{ticker}?source=search",
        }]
        if edgar_url:
            actions.append({
                "action": "view",
                "label": "EDGAR Filing",
                "url": edgar_url,
            })
        actions.append({
            "action": "view",
            "label": "Finviz Chart",
            "url": f"https://finviz.com/quote.ashx?t={ticker}",
        })
        payload["actions"] = actions

    try:
        resp = requests.post("https://ntfy.sh", json=payload, timeout=8)
        if resp.status_code not in (200, 204):
            print(f"  [!] ntfy returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"  [!] ntfy alert failed: {e}")


def send_heartbeat_if_due():
    """Send 'bot is still running' ping to ntfy every ~1 hour."""
    if not NTFY_TOPIC:
        return
    now = datetime.now()
    last_sent = None
    if os.path.exists(HEARTBEAT_FILE):
        try:
            with open(HEARTBEAT_FILE, "r") as f:
                last_sent = datetime.fromisoformat(f.read().strip())
        except Exception:
            last_sent = None

    # Send if never sent, or if >= 1 hour since last
    if last_sent is None or (now - last_sent).total_seconds() >= 3600:
        try:
            requests.post("https://ntfy.sh", json={
                "topic": NTFY_TOPIC,
                "title": "AI Pivot Monitor - Heartbeat",
                "message": "Bot is still running no worries :)",
                "priority": 2,
                "tags": ["green_heart"],
            }, timeout=8)
            with open(HEARTBEAT_FILE, "w") as f:
                f.write(now.isoformat())
            print(f"  [HEARTBEAT] Sent 'still running' ping to ntfy")
        except Exception as e:
            print(f"  [!] Heartbeat failed: {e}")


def alert(signal_level: str, filing: dict, keyword: str,
          alerted: Optional[dict] = None,
          evaluated: Optional[dict] = None) -> bool:
    """
    Pure-GPT pipeline:
    1. Dedup check (already evaluated this filing? skip)
    2. Market cap filter (>$200M → skip)
    3. GPT-4o reads the filing and decides
    4. If confirmed with high confidence → fire full klaxon
    5. Mark as evaluated regardless (so we don't re-call GPT next scan)
    """
    company = filing.get("company", "Unknown")
    ticker = filing.get("ticker", "")
    url = filing.get("url", "")
    file_date = filing.get("file_date", "")
    filing_desc = filing.get("file_description", filing.get("form", "8-K"))
    inc_state = filing.get("inc_state", "")
    sic = filing.get("sic", "")
    items = filing.get("items", [])
    adsh = filing.get("adsh", "")

    # === DEDUP 1: Already evaluated this filing? Skip without calling GPT. ===
    if evaluated is not None and adsh and adsh in evaluated:
        prev = evaluated[adsh]
        prev_verdict = prev.get("verdict", "?") if isinstance(prev, dict) else "?"
        print(f"  [DEDUP-EVAL] {adsh} — ${ticker or company[:15]} already evaluated ({prev_verdict})")
        return False

    # === DEDUP 2: Already alerted? (legacy) ===
    if alerted is not None and adsh and adsh in alerted:
        print(f"  [DEDUP-ALERT] {adsh} — ${ticker or company[:15]} already alerted")
        return False

    # === FILTER: Market cap (cheap noise reduction before paying for GPT) ===
    market_data = None
    if ticker:
        market_data = lookup_ticker_finviz(ticker)
    if market_data and market_data.get("market_cap_m"):
        mcap = market_data["market_cap_m"]
        if mcap > MAX_ALERT_MARKET_CAP_M:
            print(f"  [SKIP] ${ticker} — ${mcap:.0f}M market cap (over ${MAX_ALERT_MARKET_CAP_M}M)")
            # Don't waste GPT call on this; mark as evaluated so we don't recheck mcap
            if evaluated is not None:
                mark_evaluated(adsh, "REJECTED_MCAP", 0, evaluated)
            return False

    # === GPT-4o reads the filing and decides ===
    ticker_display = f" (${ticker})" if ticker else ""
    print(f"  [GPT] Reading filing for {company[:30]}{ticker_display}...")
    result = evaluate_filing(filing, market_data)

    confirmed = result.get("confirmed", False)
    confidence = result.get("confidence", 0)
    reasoning = result.get("reasoning", "")
    new_name = result.get("new_name", "")
    summary = result.get("summary", "")
    tin = result.get("tokens_in", 0)
    tout = result.get("tokens_out", 0)

    # Approx cost (gpt-4o pricing: $2.50/M in, $10/M out)
    cost = (tin / 1_000_000 * 2.50) + (tout / 1_000_000 * 10.0)

    # Console summary
    verdict_emoji = "✅ CONFIRMED" if confirmed else "❌ REJECTED"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"""
  {verdict_emoji} (confidence {confidence}/10)  cost ${cost:.4f}  ({tin}+{tout} tokens)
  Company:  {company}{ticker_display}
  Filing:   {filing_desc} ({form_type_with_items(filing)})
  Filed:    {file_date}
  Reasoning: {reasoning[:300]}
  Summary:  {summary}
  URL:      {url}
  Time:     {timestamp}""")
    print("-" * 60)

    # Mark as evaluated regardless of verdict (prevents re-asking GPT)
    if evaluated is not None:
        mark_evaluated(adsh, "CONFIRMED" if confirmed else "REJECTED", confidence, evaluated)

    # Build analysis dict for Discord embed (compatible with existing send_discord_alert)
    analysis = {
        "verdict": "CONFIRMED_PIVOT" if confirmed else "REJECTED",
        "score": confidence * 10,  # 1-10 → 10-100 for display
        "signals": [],
        "summary": summary,
        "new_name": new_name,
        "old_name": "",
        "gpt_verification": result,
    }

    # === Klaxon (only if CONFIRMED with sufficient confidence) ===
    if confirmed and confidence >= OPENAI_MIN_CONFIDENCE:
        print(f"\n  🚨🚨🚨 FIRING FULL KLAXON FOR ${ticker} 🚨🚨🚨\n")
        print(f"  GPT confidence: {confidence}/10")
        print(f"  Reasoning: {reasoning[:200]}\n")

        # 1. Discord — @everyone + headline + embed
        send_discord_alert(signal_level, filing, keyword, market_data, analysis)

        # 2. ntfy.sh — TRIPLE BURST
        mcap_s = market_data.get("market_cap_str", "?") if market_data else "?"
        price_s = market_data.get("price", "?") if market_data else "?"

        # Burst 1: alarm
        send_ntfy_alert(
            title=f"🚨🚨🚨 ${ticker} AI PIVOT 🚨🚨🚨" if ticker else "🚨🚨🚨 AI PIVOT 🚨🚨🚨",
            message=f"BUY WINDOW OPEN — ${ticker}\n{summary[:120]}" if ticker else f"AI pivot confirmed: {summary[:120]}",
            ticker=ticker, edgar_url=url, priority=5,
            tags=["rotating_light", "rotating_light", "rotating_light"],
        )
        time.sleep(1)

        # Burst 2: details
        ntfy_body = f"""{company[:80]}
GPT confidence: {confidence}/10
Mkt Cap: {mcap_s} | Price: ${price_s}
Inc: {inc_state} | Items: {', '.join(items[:4])}
{summary[:120]}"""
        send_ntfy_alert(
            title=f"💰 ${ticker} — {mcap_s} nano-cap" if ticker else f"💰 {company[:30]}",
            message=ntfy_body,
            ticker=ticker, edgar_url=url, priority=5,
            tags=["moneybag", "chart_with_upwards_trend"],
        )
        time.sleep(1)

        # Burst 3: action
        send_ntfy_alert(
            title=f"⚡ ACT NOW — ${ticker}" if ticker else "⚡ ACT NOW",
            message=f"GPT-4o confirmed AI pivot ({confidence}/10). Tap to buy on Robinhood.",
            ticker=ticker, edgar_url=url, priority=5,
            tags=["zap", "rocket"],
        )

        # 3. macOS klaxon
        klaxon_macos(ticker, company)

        # 4. Slack
        slack_msg = f"🚨 AI PIVOT [CONFIRMED {confidence}/10] — {company}{ticker_display}\nFiling: {filing_desc} ({file_date})\nReasoning: {reasoning[:200]}\n{url}"
        send_slack_alert(slack_msg)

        # 5. macOS banner notification
        send_desktop_notification(
            f"🚨 CONFIRMED AI PIVOT{ticker_display}",
            f"{company}: {summary[:100]}"
        )

        # Mark as alerted (legacy compatibility)
        if alerted is not None:
            mark_alerted(adsh, alerted)
            print(f"  [ALERTED] ${ticker} — added to dedup file")
        return True

    # GPT rejected or low confidence — silent, already logged
    if confirmed and confidence < OPENAI_MIN_CONFIDENCE:
        print(f"  🤔 GPT confirmed but confidence {confidence}/10 below threshold {OPENAI_MIN_CONFIDENCE}/10 — no alert")
    return False


def form_type_with_items(filing: dict) -> str:
    """Format form type and any 8-K items for display."""
    form = filing.get("form", "")
    items = filing.get("items", [])
    if items:
        return f"{form} items={','.join(items)}"
    return form


# ============================================================
# MAIN SCANNER
# ============================================================

def scan_once(days_back: int = 3) -> int:
    """Run a single scan across all keyword groups."""
    print(f"\n{'='*60}")
    print(f"  EDGAR AI PIVOT SCANNER")
    print(f"  Scanning last {days_back} days of 8-K filings")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    # Load persistent dedup state
    alerted = load_alerted()
    evaluated = load_evaluated()
    print(f"  Dedup: {len(alerted)} previously alerted + {len(evaluated)} previously evaluated\n")

    # Send hourly heartbeat to confirm bot is running
    send_heartbeat_if_due()

    seen_adshs = set()  # Within-scan dedup (a filing can match multiple keywords)
    total_hits = 0

    keyword_groups = [
        ("HIGH", HIGH_SIGNAL_KEYWORDS),
        ("MEDIUM", MEDIUM_SIGNAL_KEYWORDS),
        ("LOW", LOW_SIGNAL_KEYWORDS),
    ]

    for level, keywords in keyword_groups:
        print(f"[{level} SIGNAL KEYWORDS]")
        for kw in keywords:
            print(f"  Searching: {kw}...")
            results = search_edgar(kw, forms=FORM_TYPES, days_back=days_back)

            for filing in results:
                adsh = filing.get("adsh", "")
                if adsh in seen_adshs:
                    continue
                seen_adshs.add(adsh)
                total_hits += 1
                alert(level, filing, kw, alerted=alerted, evaluated=evaluated)

            # Rate limit — SEC asks for max 10 requests/sec
            time.sleep(0.5)
        print()

    print(f"{'='*60}")
    print(f"  Scan complete. {total_hits} unique filings matched.")
    print(f"{'='*60}\n")

    return total_hits


# ============================================================
# POLLING MODES
# ============================================================

def is_in_schedule_window() -> bool:
    """Check if current time is within the after-hours polling window.
    Window: Tuesday-Thursday, 4:00 PM - 8:00 PM Eastern."""
    now_et = datetime.now(ET)
    weekday = now_et.weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
    hour = now_et.hour

    # Tue=1, Wed=2, Thu=3
    if weekday not in (1, 2, 3):
        return False
    # 4 PM to 8 PM ET (16:00 - 20:00)
    if hour < 16 or hour >= 20:
        return False
    return True


def next_schedule_window() -> datetime:
    """Calculate the next polling window start time."""
    now_et = datetime.now(ET)

    # Check today first, then future days
    for days_ahead in range(0, 8):
        candidate = now_et + timedelta(days=days_ahead)
        candidate = candidate.replace(hour=16, minute=0, second=0, microsecond=0)
        # Must be Tue/Wed/Thu and in the future
        if candidate.weekday() in (1, 2, 3) and candidate > now_et:
            return candidate

    # Fallback
    return now_et + timedelta(days=1)


def watch_mode(days_back: int = 1):
    """Continuous polling every 5 minutes."""
    print(f"Starting watch mode. Polling every {POLL_INTERVAL}s...")
    print(f"Press Ctrl+C to stop.\n")

    while True:
        try:
            scan_once(days_back=days_back)
            print(f"Next scan in {POLL_INTERVAL}s...\n")
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\nStopped.")
            break


def schedule_mode(days_back: int = 1):
    """Smart polling — only runs during after-hours windows (Tue-Thu 4-8pm ET)."""
    print(f"Starting schedule mode.")
    print(f"Polling window: Tue-Thu 4:00-8:00 PM ET, every {POLL_INTERVAL}s")
    print(f"Press Ctrl+C to stop.\n")

    while True:
        try:
            if is_in_schedule_window():
                now_et = datetime.now(ET)
                print(f"[{now_et.strftime('%a %H:%M ET')}] Inside polling window — scanning...")
                scan_once(days_back=days_back)
                print(f"Next scan in {POLL_INTERVAL}s...\n")
                time.sleep(POLL_INTERVAL)
            else:
                next_window = next_schedule_window()
                now_et = datetime.now(ET)
                wait_seconds = (next_window - now_et).total_seconds()
                wait_hours = wait_seconds / 3600

                print(f"[{now_et.strftime('%a %H:%M ET')}] Outside polling window.")
                print(f"  Next window: {next_window.strftime('%A %B %d at %I:%M %p ET')}")
                print(f"  Sleeping for {wait_hours:.1f} hours...")

                # Sleep in 60s chunks so Ctrl+C works
                while wait_seconds > 0:
                    time.sleep(min(60, wait_seconds))
                    wait_seconds -= 60
                    if is_in_schedule_window():
                        break

        except KeyboardInterrupt:
            print("\nStopped.")
            break


# ============================================================
# WATCHLIST
# ============================================================

# Pre-pivot candidate companies
WATCHLIST = [
    # (ticker, company_name, market_cap_M, notes)
    ("JTAI", "Jet.AI", 3.6, "Aviation->AI data center pivot. Merger deadline Apr 30."),
    ("AIXC", "AIxCrypto Holdings", 26.5, "Biotech->AI/crypto. Could drop crypto angle."),
    ("PRFX", "PRF Technologies", 5.0, "Pharma->AI solar analytics. Already rebranded once."),
    # Add more as you find them via --screener
]


def print_watchlist():
    """Print current watchlist."""
    print(f"\n{'='*60}")
    print(f"  AI PIVOT WATCHLIST")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    print(f"{'Ticker':<8} {'Mkt Cap':>8} {'Company':<25} {'Notes'}")
    print(f"{'-'*8} {'-'*8} {'-'*25} {'-'*40}")
    for ticker, name, mcap, notes in WATCHLIST:
        print(f"{ticker:<8} ${mcap:>6.1f}M {name:<25} {notes}")
    print()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    args = sys.argv[1:]

    days = DEFAULT_LOOKBACK_DAYS
    if "--days" in args:
        idx = args.index("--days")
        if idx + 1 < len(args):
            days = int(args[idx + 1])

    if "--screener" in args:
        print("Running Finviz nano-cap screener...")
        results = scrape_finviz_screener()
        print_finviz_results(results)

    elif "--watchlist" in args:
        print_watchlist()

    elif "--schedule" in args:
        print_watchlist()
        schedule_mode(days_back=min(days, 1))

    elif "--watch" in args:
        print_watchlist()
        watch_mode(days_back=min(days, 1))

    else:
        print_watchlist()
        scan_once(days_back=days)
