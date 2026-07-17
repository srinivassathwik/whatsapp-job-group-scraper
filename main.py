"""
WhatsApp C2C Job Scraper  —  Playwright
========================================
All settings in config.json. Never edit this file unless adding new features.

Features:
  - Persistent login (QR scan once)
  - All groups in one run
  - Last N hours filter (configurable)
  - Scroll up to load old messages
  - Seen-jobs dedup (seen_jobs.json)
  - Spam/ad filter
  - Unicode bold text normalization (WhatsApp bold fonts)
  - Fuzzy group name matching
  - Three output files:
      jobs.json         — extracted job data
      raw_messages.json — all messages with is_job flag
      review.json       — every message with filter reason

Install:
  pip install playwright
  playwright install chromium

Run:
  python main.py
"""

import hashlib
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ══════════════════════════════════════════════════════════════════════
#  LOAD CONFIG
# ══════════════════════════════════════════════════════════════════════

def load_config(path: str = "config.json") -> dict:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items()
            if not k.startswith("-") and not k.startswith("_")}


CFG = load_config()

OUTPUT_DIR  = Path(CFG["output_dir"])
RAW_FILE    = OUTPUT_DIR / CFG["raw_messages_file"]
JOBS_FILE   = OUTPUT_DIR / CFG["jobs_file"]
SEEN_FILE   = OUTPUT_DIR / CFG["seen_ids_file"]
REVIEW_FILE = OUTPUT_DIR / "review.json"
SESSION_DIR = CFG["session_dir"]
LOG_FILE    = OUTPUT_DIR / "scraper.log"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

GROUPS          = CFG["groups"]
MAX_MSGS        = CFG["max_messages_per_group"]
ONLY_LAST_HOURS = CFG["only_last_hours"]
LOGIN_TIMEOUT   = CFG["login_timeout_seconds"]
HEADLESS        = CFG["headless"]

JOB_KEYWORDS    = [k.lower() for k in CFG["job_keywords"]]
US_LOCATIONS    = CFG["us_locations"]
SKILLS          = CFG["skills"]
VISA_TYPES      = CFG["visa_types"]
CONTRACT_TYPES  = CFG["contract_types"]


# ══════════════════════════════════════════════════════════════════════
#  LOGGING  —  Windows-safe (no emoji in terminal)
# ══════════════════════════════════════════════════════════════════════

class SafeStreamHandler(logging.StreamHandler):
    SUBS = {"\u2705":"[OK]","\u274c":"[ERR]","\u26a0":"[WARN]",
            "\u2192":"->","\u2714":"[OK]","\u2500":"-"}
    def emit(self, record):
        try:
            msg = self.format(record)
            for ch, sub in self.SUBS.items():
                msg = msg.replace(ch, sub)
            msg = msg.encode("cp1252", errors="replace").decode("cp1252")
            self.stream.write(msg + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)

def setup_logging():
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
    ch  = SafeStreamHandler(sys.stdout); ch.setFormatter(fmt)
    fh  = logging.FileHandler(LOG_FILE, encoding="utf-8"); fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(ch)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
#  SEEN-JOBS REGISTRY
# ══════════════════════════════════════════════════════════════════════

def make_msg_hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode()).hexdigest()

def load_seen_ids() -> set:
    if SEEN_FILE.exists():
        with open(SEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen_ids", []))
    return set()

def save_seen_ids(seen: set):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "description": "Hashes of scraped messages. Delete to re-scrape everything.",
            "total": len(seen),
            "last_updated": datetime.now().isoformat(),
            "seen_ids": sorted(seen),
        }, f, indent=2, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════
#  TIME FILTER
# ══════════════════════════════════════════════════════════════════════

def parse_wa_timestamp(ts_str: str, scrape_time: datetime):
    """
    Parse WhatsApp Web timestamp into datetime.
    WhatsApp formats seen in real usage:
      "9:56 PM, 7/9/2026"   <- most common (date + time)
      "9:56 PM"             <- today only
      "Yesterday 9:56 PM"
    Returns None if unparseable (caller should KEEP the message — safe default).
    """
    if not ts_str:
        return None

    ts = ts_str.strip()
    tsl = ts.lower()

    # "Yesterday 9:56 PM"
    if tsl.startswith("yesterday"):
        rest = ts[9:].strip()
        dt = parse_wa_timestamp(rest, scrape_time)
        return dt - timedelta(days=1) if dt else None

    # Try all known full-date formats first
    for fmt in (
        "%I:%M %p, %m/%d/%Y",   # "9:56 PM, 7/9/2026"  ← WhatsApp actual format
        "%I:%M %p, %m/%d/%y",   # "9:56 PM, 7/9/26"
        "%m/%d/%Y, %I:%M %p",   # "7/9/2026, 9:56 PM"
        "%m/%d/%y, %I:%M %p",   # "7/9/26, 9:56 PM"
        "%d/%m/%Y, %H:%M",
        "%m/%d/%Y, %H:%M",
        "%Y-%m-%dT%H:%M:%S",    # ISO
    ):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            pass

    # Time-only (today's messages): "9:56 PM" or "21:56"
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)?$", tsl)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        ap = m.group(3)
        if ap == "pm" and h < 12: h += 12
        elif ap == "am" and h == 12: h = 0
        try:
            return scrape_time.replace(hour=h, minute=mn, second=0, microsecond=0)
        except ValueError:
            return None

    return None


def is_within_hours(ts_str: str, hours: int, scrape_time: datetime) -> bool:
    dt = parse_wa_timestamp(ts_str, scrape_time)
    if dt is None:
        # Cannot parse — KEEP the message (safe default, better than silent loss)
        print(f"  [WARN] Cannot parse timestamp '{ts_str}' — keeping message")
        return True
    in_window = dt >= (scrape_time - timedelta(hours=hours))
    if not in_window:
        print(f"  [SKIP] Outside {hours}h (ts='{ts_str}' parsed={dt.strftime('%m/%d %H:%M')})")
    return in_window


# ══════════════════════════════════════════════════════════════════════
#  SPAM PATTERNS
# ══════════════════════════════════════════════════════════════════════

SPAM_PATTERNS = [
    # Paid / premium services
    r"paid\s+service", r"paid\s+career\s+service", r"paid\s+marketing",
    r"resume\s+build", r"ats.?friendly",
    r"interview\s+support\s+service", r"proxy\s+support", r"interview\s+&\s+proxy",
    r"we\s+market\s+your\s+(profile|resume)", r"our\s+premium\s+service",
    r"marketing\s+support\s+service", r"us\s+it\s+marketing\s+support",
    r"better\s+us\s+it\s+marketing", r"mock\s+interview",
    r"100%\s+success\s+rate", r"100%\s+genuine",
    r"live\s+technical.*guidance", r"pre.interview\s+setup",
    r"train\s+with.*industry\s+expert", r"technical\s+interview\s+support",
    r"free\s+demo\s+session",
    # Greetings / non-job openers
    r"^greeting\s+of\s+the\s+day", r"^greetings\s+of\s+the\s+day",
    r"^good\s+(morning|evening|afternoon)\s+(?:all|team|recruiter)",
    r"^dear\s+(all|team|recruiter|hiring)",
    r"^hello\s+(all|team|recruiter|connection)",
    r"^hi\s+(all|team|everyone|connections|recruiter)",
    # Hotlist / bench self-promotion
    r"tech\s+hotlist\s+of\s+my", r"hotlist\s+of\s+(my\s+)?(bench|available|experienced)",
    r"bench\s+consultants\s+available", r"available\s+consultants",
    r"my\s+bench\s+candidate", r"hotlist\s+candidates\s+available",
    r"\bavailable\s+for\s+c2c\s+opportunities\b", r"\bopen\s+to\s+c2c\b",
    r"actively\s+looking\s+for\s+(new\s+)?opportunities",
    r"available\s+immediately\s+for\s+c2c",
    # Recruiter / staffing self-intros
    r"i\s+am\s+a\s+(senior\s+)?recruiter", r"i\s+am\s+a\s+staffing",
    r"we\s+are\s+a\s+staffing", r"we\s+provide\s+staffing",
    r"my\s+name\s+is\s+\w+.*(?:recruiter|staffing|bench\s+sales)",
    r"i\s+am\s+a\s+us\s+it\s+recruiter",
    # BDM / business ads
    r"bdm\s+freelancer", r"looking\s+for\s+bdm",
    r"business\s+development\s+manager.*intrested",
    # LinkedIn / account sales
    r"linkedin\s+for\s+sale", r"linkedin\s+account\s+for\s+sale",
    r"20\d\d\s+linkedin",
    # WA group invites
    r"join\s+my\s+whatsapp\s+group", r"follow\s+this\s+link\s+to\s+join",
    # Known spam
    r"\bpratap@logisofttechinc\.com\b", r"\bwa\.me/91798151\b",
    # Recruiter hiring ads (not C2C tech jobs)
    r"hiring\s+a?\s+(?:freelance|part.?time)?\s*(?:us\s+it\s+)?recruiter",
    r"looking\s+for\s+(?:an?\s+)?(?:us\s+it\s+)?(?:bench\s+sales\s+)?recruiter",
    r"we\s+are\s+(?:hiring|looking\s+for)\s+(?:an?\s+)?recruiter",
    # India-based jobs (not US C2C) — night shift / Indian cities
    r"night\s+shift.*us\s+shift|us\s+shift.*night\s+shift",
    r"location[:\s]+(?:hyderabad|bangalore|chennai|pune|mumbai|noida|gurgaon|visakhapatnam|vizag|india|kolkata|ahmedabad|kochi|coimbatore)",
    r"(?:technical\s+it\s+recruiter|it\s+recruiter|bench\s+sales)\s*[\|].*(?:visakhapatnam|hyderabad|bangalore|india)",
    r"(?:bench\s+sales\s+recruiter|us\s+it\s+recruiter).*(?:hyderabad|bangalore|india)",
    # Training / course ads
    r"free\s+live\s+demo\s+session",
    r"ready\s+to\s+become.*expert\?",
    r"join\s+our\s+(?:free\s+)?(?:live\s+)?(?:demo|training|bootcamp|workshop|course)",
    r"enroll\s+now|register\s+now|sign\s+up\s+(?:now|today|free)",
    r"(?:course|training|bootcamp|workshop)\s+(?:starts?|begins?|available)",
    r"learn\s+(?:ai|ml|python|cloud|aws|azure)\s+(?:from|with|for)",
    # LinkedIn posts / social shares (not jobs)
    r"linkedin\.com/posts?/",
    r"#opentowork",
    r"liked\s+this\s+post|please\s+(?:like|share|repost)",
]

def is_spam(text: str) -> bool:
    lower = text.lower()
    return any(re.search(p, lower) for p in SPAM_PATTERNS)

def spam_reason(text: str) -> str:
    lower = text.lower()
    for p in SPAM_PATTERNS:
        if re.search(p, lower):
            return p
    return ""


# ══════════════════════════════════════════════════════════════════════
#  JOB CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════

JOB_TITLE_PATTERNS = [
    r"(?:^|\n)\s*(?:job\s+)?(?:title|role|position|designation|req(?:uirement)?)\s*[:\-\u2013]\s*\S",
    r"(?:^|\n)\s*[*\u2022\-]?\s*[Hh]iring\s*[:\-\u2013]\s*\S",
]
JOB_LOCATION_PATTERNS = [
    r"(?:^|\n)\s*location\s*[:\-\u2013]\s*\S",
    r"(?:^|\n)\s*rate\s*[:\-\u2013]\s*\S",
    r"(?:^|\n)\s*duration\s*[:\-\u2013]\s*\S",
    r"(?:^|\n)\s*experience\s*[:\-\u2013]\s*\S",
]

def has_job_content(text: str) -> bool:
    """True if message has BOTH a role/title line AND a location/rate/exp line."""
    has_title    = any(re.search(p, text, re.IGNORECASE | re.MULTILINE) for p in JOB_TITLE_PATTERNS)
    has_location = any(re.search(p, text, re.IGNORECASE | re.MULTILINE) for p in JOB_LOCATION_PATTERNS)
    return has_title and has_location

def is_job_message(text: str) -> bool:
    if is_spam(text): return False
    lower = text.lower()
    return any(kw in lower for kw in JOB_KEYWORDS)

def classify_message(text: str) -> tuple:
    """
    Returns (status, reason).
    status = "job" | "spam" | "no_keywords"
    Logic:
      1. If message has Role: + Location: structure → always a job (even with spam opener)
      2. Spam check
      3. Keyword match
    """
    if len(text.strip()) < 10:
        return "no_keywords", "Message too short"

    # Pure URL messages (e.g. LinkedIn post links) are not job posts
    stripped = text.strip()
    if re.match(r"^https?://\S+$", stripped) or re.match(r"^https?://\S+\s*$", stripped):
        return "no_keywords", "Message is just a URL link"

    if has_job_content(text):
        lower = text.lower()
        kw = next((k for k in JOB_KEYWORDS if k in lower), "job content")
        return "job", f"Has job structure (Role/Title/Location) + keyword: {kw!r}"

    sp = spam_reason(text)
    if sp:
        return "spam", f"Matched spam pattern: {sp}"

    lower = text.lower()
    kw = next((k for k in JOB_KEYWORDS if k in lower), None)
    if kw:
        return "job", f"Matched keyword: {kw!r}"
    return "no_keywords", "No job keywords found"


# ══════════════════════════════════════════════════════════════════════
#  JOB EXTRACTION
# ══════════════════════════════════════════════════════════════════════

def normalize_unicode(s: str) -> str:
    """Convert WhatsApp bold/italic Unicode math chars to plain ASCII."""
    import unicodedata
    result = []
    for ch in s:
        cp = ord(ch)
        if   0x1D400 <= cp <= 0x1D419: result.append(chr(cp - 0x1D400 + ord('A')))
        elif 0x1D41A <= cp <= 0x1D433: result.append(chr(cp - 0x1D41A + ord('a')))
        elif 0x1D434 <= cp <= 0x1D44D: result.append(chr(cp - 0x1D434 + ord('A')))
        elif 0x1D44E <= cp <= 0x1D467: result.append(chr(cp - 0x1D44E + ord('a')))
        elif 0x1D468 <= cp <= 0x1D481: result.append(chr(cp - 0x1D468 + ord('A')))
        elif 0x1D482 <= cp <= 0x1D49B: result.append(chr(cp - 0x1D482 + ord('a')))
        elif 0x1D49C <= cp <= 0x1D7FF:
            norm = unicodedata.normalize('NFKC', ch)
            result.append(norm if norm.isascii() else ch)
        elif 0x1D7CE <= cp <= 0x1D7D7: result.append(chr(cp - 0x1D7CE + ord('0')))
        else: result.append(ch)
    return ''.join(result)


def extract_job(text: str, group: str, sender: str, wa_timestamp: str, scrape_ts: str) -> dict:
    job = {
        "job_title": None, "client": None, "vendor": None,
        "location": None, "duration": None, "rate": None,
        "experience": None, "visa_types": [], "contract_type": None,
        "skills": [], "contact_name": None, "contact_phone": None,
        "contact_email": None, "apply_link": None, "interview_type": None,
        "source_group": group, "sender": sender,
        "wa_timestamp": wa_timestamp, "scraped_at": scrape_ts,
        "raw_message": text.strip(),
    }

    text_norm = normalize_unicode(text)

    # ── Title ────────────────────────────────────────────────────────
    title_pats = [
        r"(?:^|\n)\s*(?:job\s+)?(?:title|role|position|designation|opening|req(?:uirement)?)\s*[:\-\u2013]\s*([^\n\|]{3,90})",
        r"(?:^|\n)\s*[*\u2022\-]?\s*[Hh]iring\s*[:\-\u2013]\s*([^\n\|]{3,90})",
        r"(?:looking\s+for\s+(?:a\s+)?|need\s+(?:a\s+)?)([A-Z][^\n\|,?]{5,80})",
        r"(?:^|\n)\s*[*#\u2022\-]?\s*([A-Z][A-Za-z0-9\s/()&.,+#\u2013\-]{2,70}"
        r"(?:Developer|Engineer|Architect|Analyst|Manager|Lead|Consultant|Specialist|"
        r"Designer|Tester|SDET|DBA|Admin|DevOps|Scrum\s*Master|BA|PM|PMP|SME|"
        r"Expert|Trainer|Contractor|Recruiter|Support))\s*[*\-|]?\s*$",
    ]
    for p in title_pats:
        m = re.search(p, text_norm, re.IGNORECASE | re.MULTILINE)
        if m:
            raw = m.group(1).strip().strip("*#:\u2013- \t")
            raw = re.sub(r"\s+\d+\+?\s*(?:years?|yrs?)?\s*$", "", raw).strip()
            raw = re.split(r"\s*[|]\s*", raw)[0].strip()
            if "\n" not in raw and not raw.endswith("?") and 3 < len(raw) < 100:
                job["job_title"] = raw
                break

    # Fallback: first meaningful line
    if not job["job_title"]:
        for line in text_norm.split("\n"):
            line = line.strip().strip("*#\u2022\u2013- \t")
            if not line: continue
            line = re.sub(
                r"^(?:hot|urgent|immediate)\s+req(?:uirement)?\s*[:\-\u2013]+\s*",
                "", line, flags=re.IGNORECASE).strip()
            skip = ("http","www.","dear","hi ","hi,","hello","hey","greetings",
                    "good morning","good evening","good afternoon","we are","we're",
                    "i am","i'm","my name","this is","please","note:","email:",
                    "contact:","for more","kindly","sharing","below is","find the",
                    "tech hotlist","hotlist of","bench candidate","available for",
                    "open to","actively looking","looking for c2c","we provide",
                    "we offer","our client","on behalf")
            if any(line.lower().startswith(s) for s in skip): continue
            if 5 < len(line) < 100 and line.count(" ") < 12:
                line = re.split(r"\s*[|]\s*", line)[0].strip()
                if len(line) > 5:
                    job["job_title"] = line
                break

    # ── Client ───────────────────────────────────────────────────────
    for p in [
        r"(?:end\s+client|end-client)[:\-\s]+([^\n\|,]{2,50})",
        r"(?:^|\n)\s*client[:\-\s]+([^\n\|,]{2,50})",
        r"(?:for our client)[:\-\s]+([^\n\|,]{2,50})",
    ]:
        m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
        if m:
            val = m.group(1).strip().strip("*#:-")
            bad = ("skill","experience","year","communication","strong","must",
                   "required","hiring","looking","need","position","role","contract")
            if val and not any(w in val.lower() for w in bad) and len(val) < 50:
                job["client"] = val; break

    # ── Vendor ───────────────────────────────────────────────────────
    for p in [
        r"(?:vendor|prime\s+vendor|staffing\s+company|our\s+company|firm)[:\-\s]+([^\n\|,]{2,50})",
        r"(?:at|from)\s+([A-Z][A-Za-z0-9\s&.]{2,40}(?:Inc|Corp|LLC|Ltd|Solutions|Technologies|Consulting|Staffing|Group|Services)\.?)\b",
    ]:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().strip("*#:-")
            if 2 < len(val) < 50: job["vendor"] = val; break

    # ── Location ─────────────────────────────────────────────────────
    m = re.search(r"(?:location|loc|city|state|place|work\s+location|job\s+location)[:\-\s]+([^\n\|]{2,80})", text, re.IGNORECASE)
    if m: job["location"] = m.group(1).strip().strip("*#:-")
    if not job["location"]:
        for loc in US_LOCATIONS:
            if re.search(r"\b" + re.escape(loc) + r"\b", text, re.IGNORECASE):
                job["location"] = loc; break

    # ── Duration ─────────────────────────────────────────────────────
    for p in [
        r"(?:duration|contract\s+length|engagement\s+length|term)[:\-\s]+([^\n\|,]{2,60})",
        r"(\d+\s*(?:\+)?\s*(?:months?|weeks?)(?:\s*contract|\s*engagement|\s*c2h|\s*cth)?)",
        r"(long.?term(?:\s+contract)?|short.?term(?:\s+contract)?|ongoing|permanent)",
        r"(\d+\s*months?\s*contract.?to.?hire)",
    ]:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().strip("*#:-")
            if not re.match(r"^\d+\+?\s*years?$", val, re.IGNORECASE):
                job["duration"] = val; break

    # ── Rate ─────────────────────────────────────────────────────────
    for p in [
        # Explicit label (most reliable)
        r"(?:rate|bill\s+rate|pay\s+rate|c2c\s+rate|w2\s+rate|hourly\s+rate|max\s+rate)[:\-\s]+([^\n\|]{2,60})",
        # "$65/hr", "$50-$70/hr on C2C"
        r"(\$\s*\d+(?:\.\d+)?\s*(?:[-]\s*\$?\s*\d+)?\s*(?:/\s*hr|/\s*hour|per\s+hour|hourly|/hr)\b[^\n]{0,30})",
        # "up to $65/hr", "upto $70 on c2c"
        r"((?:up\s*to|upto?|max|maximum)\s+\$\s*\d+[^\n]{0,30})",
        # "65/hr" without $ sign
        r"\b(\d{2,3}(?:\.\d+)?/hr)\b",
        # "65 dollars per hour"
        r"(\d+\s*(?:dollars?|usd)\s*(?:/\s*hr|per\s+hour)?)",
    ]:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().strip("*#:-").strip()
            if re.search(r"\$|\d+\s*/\s*hr|/hour|per\s+hour|usd|dollar|/hr", val, re.IGNORECASE):
                nums = re.findall(r"\d+(?:\.\d+)?", val)
                if any(10 <= float(n) <= 500 for n in nums):
                    job["rate"] = val; break

    # ── Experience ───────────────────────────────────────────────────
    exp_val = None
    # 1. Labeled line
    m = re.search(r"(?:^|\n)\s*(?:experience|exp(?:erience)?|yoe)[:\-\s]+(\d[^\n,]{0,30})",
                  text, re.IGNORECASE | re.MULTILINE)
    if m: exp_val = m.group(1).strip().strip("*#:-")
    # 2. "X years of experience"
    if not exp_val:
        m = re.search(r"(\d+\s*[-+\u2013]?\s*\d*)\s*(?:years?|yrs?)(?:\s+of\s+(?:relevant\s+|total\s+|IT\s+)?experience)", text, re.IGNORECASE)
        if m: exp_val = m.group(1).strip() + " years"
    # 3. "Need/require X years"
    if not exp_val:
        m = re.search(r"(?:need|require|must\s+have|minimum)\s+(\d+\+?\s*(?:years?|yrs?))", text, re.IGNORECASE)
        if m: exp_val = m.group(1).strip()
    # 4. "X years in/with ..."
    if not exp_val:
        m = re.search(r"(\d+\+?\s*(?:years?|yrs?))\s+(?:in|with|of)\b", text, re.IGNORECASE)
        if m: exp_val = m.group(1).strip()
    # 5. Level word fallback
    if not exp_val:
        m = re.search(r"\b(entry.?level|junior|senior|mid.?level|staff|principal)\b", text, re.IGNORECASE)
        if m: exp_val = m.group(1).strip().title()
    if exp_val: job["experience"] = exp_val

    # ── Visa, contract, skills ───────────────────────────────────────
    job["visa_types"]   = [v for v in VISA_TYPES   if re.search(r"\b" + re.escape(v) + r"\b", text, re.IGNORECASE)]
    job["contract_type"] = next((c for c in CONTRACT_TYPES if re.search(r"\b" + re.escape(c) + r"\b", text, re.IGNORECASE)), None)
    job["skills"]        = [s for s in SKILLS        if re.search(r"\b" + re.escape(s) + r"\b", text, re.IGNORECASE)]

    # ── Interview type ───────────────────────────────────────────────
    m = re.search(r"(?:interview)[:\-\s]*(video|phone|in.person|onsite|virtual|skype|teams|zoom)", text, re.IGNORECASE)
    if m: job["interview_type"] = m.group(1).lower()

    # ── Email ────────────────────────────────────────────────────────
    m = re.search(r"[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,6}", text)
    if m: job["contact_email"] = m.group(0)

    # ── Phone ────────────────────────────────────────────────────────
    for p in [
        # Explicit label
        r"(?:contact|call|reach|text|phone|mob|cell|whatsapp\s*#?)[:\-\s]+([\+\d][\d\s\-().]{7,18})",
        # US format: +1 (xxx) xxx-xxxx
        r"\b(\+?1[-. ]?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4})\b",
        # Phone/Whatsapp label with number
        r"(?:Phone|Whats\s*app)[:\s#]*(\+?[\d\s\-().]{10,18})",
        # +91 India numbers
        r"\b(\+?91[-\s]?\d{10})\b",
        # Plain 10-digit US number
        r"\b(\d{10})\b",
    ]:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            digits = re.sub(r"\D","",val)
            if len(digits) >= 10:
                job["contact_phone"] = val; break

    # ── Contact name ─────────────────────────────────────────────────
    m = re.search(r"(?:contact|reach\s+out\s+to|ping|dm)\s+([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20}){1,2})\b", text, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        bad = {"me personally","me asap","me directly","our team","details","cloud",
               "support","info","team","us","me","above","resume","profile","recruiter","asap"}
        if val.lower() not in bad and len(val) > 4 and "asap" not in val.lower():
            job["contact_name"] = val

    # ── Apply link ───────────────────────────────────────────────────
    m = re.search(r"(https?://[^\s)\"'<>]+)", text)
    if m: job["apply_link"] = m.group(1)

    return job


# ══════════════════════════════════════════════════════════════════════
#  JSON HELPERS
# ══════════════════════════════════════════════════════════════════════

def load_json(path: Path) -> list:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return []

def save_json(data: list, path: Path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("Saved %d records -> %s", len(data), path)


# ══════════════════════════════════════════════════════════════════════
#  PLAYWRIGHT — BROWSER
# ══════════════════════════════════════════════════════════════════════

def launch_browser(playwright):
    Path(SESSION_DIR).mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        SESSION_DIR, headless=HEADLESS,
        args=["--start-maximized"], no_viewport=True,
    )

def wait_for_login(page):
    log.info("Opening WhatsApp Web (scan QR if prompted -- %ds timeout)...", LOGIN_TIMEOUT)
    page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
    try:
        page.wait_for_selector(
            'div[aria-label="Chat list"], div[data-testid="chat-list"]',
            timeout=LOGIN_TIMEOUT * 1000)
        log.info("[OK] Logged in to WhatsApp Web")
    except PWTimeout:
        raise RuntimeError("Timed out waiting for WhatsApp Web login.")

def get_search_input(page):
    """Find the WhatsApp search box — tries multiple selectors for resilience."""
    for sel in [
        'input[aria-label="Search or start a new chat"]',
        'input[placeholder="Search or start a new chat"]',
        'input[data-tab="3"]',
        'div[data-testid="search-input"] > div[contenteditable="true"]',
        'div[contenteditable="true"][data-tab="3"]',
        '#side div[contenteditable="true"]',
    ]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2500):
                print(f"  [SEARCH] Found via: {sel}")
                return loc
        except Exception:
            pass
    print("  [SEARCH] All selectors failed")
    return None

def fuzzy_match(config_name: str, result_title: str) -> bool:
    """
    True if config_name and result_title are the same group.

    Strategy:
    1. Exact match after stripping punctuation/spaces
    2. ALL words of config_name appear as WHOLE WORDS in result_title
       (uses word boundaries to prevent 'IT' matching inside 'recruiTIng')
    """
    def clean(s): return re.sub(r'[|,_\-\s]+', '', s).lower()

    # 1. Exact clean match
    if clean(config_name) == clean(result_title):
        return True

    # 2. Word-boundary check — ALL config words must appear as whole words
    words = [w for w in re.split(r'[|,_\-\s]+', config_name) if len(w) > 1]
    if not words:
        return False
    return all(
        bool(re.search(r'\b' + re.escape(w) + r'\b', result_title, re.IGNORECASE))
        for w in words
    )
def open_group(page, group_name: str) -> bool:
    """
    Open a WhatsApp group by name.
    1. Search exact name
    2. If not found, retry with simplified/short name (fuzzy)
    """
    print(f"\n  [STEP] open_group(): '{group_name}'")

    def try_search(query: str) -> bool:
        si = get_search_input(page)
        if si is None: return False
        si.click(timeout=5000)
        time.sleep(0.4)
        page.keyboard.press("Control+a")
        time.sleep(0.2)
        si.fill(query)
        print(f"  [STEP 2] Typed '{query}', waiting for results...")
        time.sleep(2.5)

        rows = page.locator('div[data-testid^="list-item-"]').all()
        print(f"  [STEP 3] {len(rows)} rows found")
        in_chats = False

        for row in rows:
            try: row_text = (row.text_content() or "").strip()
            except: row_text = ""

            if row_text.lower() in ("chats", "contacts"):
                in_chats = True; continue
            if row_text.lower() in ("messages", "groups"):
                in_chats = False; continue

            for span in row.locator("span[title]").all():
                title = (span.get_attribute("title") or span.text_content() or "").strip()
                title_clean = title.encode("ascii","ignore").decode().strip()
                print(f"    Row: '{title[:60]}' | chats={in_chats}")

                if fuzzy_match(group_name, title_clean):
                    print(f"  [MATCH] '{title[:60]}' -> clicking")
                    try:
                        row.locator('div[data-testid="cell-frame-container"]').first.click(timeout=3000)
                    except Exception:
                        span.click()
                    time.sleep(2)
                    log.info("  [OK] Opened: %s", group_name)
                    return True
        return False

    try:
        if try_search(group_name):
            return True

        # Fuzzy fallback: simplify the name
        simplified = " ".join(
            w for w in re.split(r'[|,_\-\s]+', group_name) if len(w) > 1
        )[:40]
        short = " ".join(simplified.split()[:3])
        if short and short.lower() != group_name.lower():
            print(f"  [RETRY] Simplified search: '{short}'")
            if try_search(short):
                return True

        log.warning("  [WARN] Group not found: '%s'", group_name)
        page.keyboard.press("Escape")
        time.sleep(0.5)
        return False

    except Exception as e:
        log.error("  [ERR] open_group error: %s", e)
        try: page.keyboard.press("Escape")
        except: pass
        return False



def _expand_read_more(bubble):
    """Click WhatsApp's 'Read more' so long messages are fully expanded."""
    try:
        rm = bubble.locator(
            'span[role="button"]:has-text("Read more"), '
            'span.read-more-button, '
            'span[data-testid="read-more-button"], '
            'div[aria-label="Read more"]'
        ).first
        if rm.is_visible(timeout=250):
            rm.click(timeout=1000)
            time.sleep(0.1)
    except Exception:
        pass


def _extract_bubble_data(bubble):
    """Return {'sender','wa_timestamp','text'} for one bubble, or None."""
    try:
        _expand_read_more(bubble)

        # ── Text ─────────────────────────────────────────────────────
        try:
            text = bubble.evaluate(
                "el => {"
                "  const spans = el.querySelectorAll('span.selectable-text');"
                "  return Array.from(spans).map(s => s.innerText || s.textContent).join('\\n').trim();"
                "}"
            ) or ""
        except Exception:
            try:
                text = (bubble.locator("span.selectable-text").first
                        .text_content(timeout=1500) or "").strip()
            except Exception:
                text = ""
        text = text.strip()
        if not text:
            return None

        # ── Sender + timestamp from data-pre-plain-text ──────────────
        # Format: "[9:56 PM, 7/9/2026] Sender Name:"
        sender, ts = "Unknown", ""
        try:
            pre = bubble.evaluate(
                "el => {"
                "  const n = el.querySelector('[data-pre-plain-text]');"
                "  return n ? n.getAttribute('data-pre-plain-text') : '';"
                "}"
            ) or ""
            if pre:
                m_ts = re.search(r"\[([^\]]+)\]", pre)
                if m_ts: ts = m_ts.group(1).strip()
                m_sn = re.search(r"\]\s*(.+?)\s*:?\s*$", pre.strip())
                if m_sn: sender = m_sn.group(1).strip()
        except Exception:
            pass

        # Fallback timestamp from msg-meta
        if not ts:
            for sel in ["span[data-testid='msg-meta'] span",
                        "span[data-testid='msg-time']"]:
                try:
                    val = bubble.locator(sel).first.text_content(timeout=400) or ""
                    if val.strip(): ts = val.strip(); break
                except Exception:
                    pass

        # Fallback sender from author span
        if sender == "Unknown":
            try:
                val = bubble.locator("span[data-testid='author']").first.text_content(timeout=400) or ""
                if val.strip() and "\n" not in val and len(val) < 60:
                    sender = val.strip()
            except Exception:
                pass

        ts = re.sub(r"^Edited\s*", "", ts, flags=re.IGNORECASE).strip()
        return {"sender": sender, "wa_timestamp": ts, "text": text}
    except Exception:
        return None


def _current_bubbles(page):
    """
    All message bubbles currently rendered in the DOM.
    Class-based selectors first (stable across WhatsApp versions and do NOT
    depend on data-testid), with data-testid / role fallbacks.
    """
    bubbles = []
    for sel in ("div.message-in", "div.message-out"):
        try:
            bubbles.extend(page.locator(sel).all())
        except Exception:
            pass
    if not bubbles:
        for sel in ('div[data-testid="msg-container"]', 'div[role="row"]'):
            try:
                bubbles.extend(page.locator(sel).all())
            except Exception:
                pass
    return bubbles


def read_messages(page, scrape_time: datetime) -> list:
    """
    Load the last ONLY_LAST_HOURS of history and return the in-window messages.

    WHY THIS IS WRITTEN THE WAY IT IS
    ---------------------------------
    WhatsApp Web *virtualises* the message list: only ~50-100 bubbles exist in
    the DOM at once. As you scroll up, WhatsApp renders older messages and
    DESTROYS the newer ones that are now far below the viewport.

    The old approach (scroll all the way up, THEN read bubbles once) therefore
    only ever saw a small, arbitrary slice of old messages — which is why only a
    handful of jobs came through. Two fixes:

      1. HARVEST after every scroll step and accumulate unique messages
         (keyed by text hash) so virtualisation can't lose them.
      2. STOP scrolling once we've loaded messages older than the time window —
         NOT when the DOM bubble count stops changing (with virtualisation the
         count stays roughly flat even while new content keeps loading, which
         made the old loop quit almost immediately).
    """
    cutoff = scrape_time - timedelta(hours=ONLY_LAST_HOURS)

    # ── Find the chat scroll container ───────────────────────────────
    chat = None
    for sel in [
        'div[data-testid="conversation-panel-messages"]',
        'div[role="application"]',
        '#main div[class*="message-list"]',
        '#main',
    ]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=3000):
                chat = loc; break
        except Exception:
            continue

    collected = {}   # text_hash -> message dict

    def harvest():
        """Read all rendered bubbles now and add any we haven't seen. Returns #new."""
        gained = 0
        for bubble in _current_bubbles(page):
            data = _extract_bubble_data(bubble)
            if not data:
                continue
            h = make_msg_hash(data["text"])
            if h not in collected:
                collected[h] = data
                gained += 1
        return gained

    def oldest_dt():
        oldest = None
        for d in collected.values():
            dt = parse_wa_timestamp(d["wa_timestamp"], scrape_time)
            if dt and (oldest is None or dt < oldest):
                oldest = dt
        return oldest

    # Grab whatever is already on screen (newest messages)
    harvest()

    if chat is None:
        print("  [WARN] Chat container not found — reading only the visible screen")
    else:
        max_attempts = 60           # hard safety cap so we never loop forever
        stale_rounds = 0
        log.info("  Scrolling up to load %dh of history (harvesting each step)...",
                 ONLY_LAST_HOURS)
        for i in range(max_attempts):
            try:
                chat.evaluate("el => el.scrollTop = 0")
            except Exception as e:
                print(f"  [SCROLL] Error: {e}"); break
            # First scroll needs longer for the initial history fetch
            time.sleep(3.0 if i == 0 else 1.6)

            gained = harvest()
            od = oldest_dt()
            od_str = od.strftime('%m/%d %H:%M') if od else "?"
            print(f"  [SCROLL] Round {i+1}: +{gained} new "
                  f"(total {len(collected)}, oldest loaded: {od_str})")

            # Stop once we've paged past the time window
            if od and od < cutoff:
                print("  [SCROLL] Reached messages older than the window — stopping")
                break

            # Only give up when NO new unique messages appear for several rounds
            # in a row (genuine top of chat / history limit).
            if gained == 0:
                stale_rounds += 1
                if stale_rounds >= 5:
                    print("  [SCROLL] No new messages after 5 rounds — top reached")
                    break
            else:
                stale_rounds = 0

        # One more harvest in case the final scroll rendered extra bubbles
        harvest()

    log.info("  Collected %d unique messages from DOM", len(collected))

    # ── Time-window filter ───────────────────────────────────────────
    messages = [d for d in collected.values()
                if is_within_hours(d["wa_timestamp"], ONLY_LAST_HOURS, scrape_time)]

    # ── Respect max_messages_per_group (keep the most recent) ────────
    if len(messages) > MAX_MSGS:
        messages.sort(
            key=lambda d: parse_wa_timestamp(d["wa_timestamp"], scrape_time) or scrape_time,
            reverse=True,
        )
        messages = messages[:MAX_MSGS]

    return messages

# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    scrape_time = datetime.now()
    scrape_ts   = scrape_time.isoformat()

    log.info("=" * 65)
    log.info("WhatsApp C2C Job Scraper  --  %s", scrape_ts)
    log.info("Groups        : %s", GROUPS)
    log.info("Last N hours  : %d", ONLY_LAST_HOURS)
    log.info("Max msgs/group: %d", MAX_MSGS)
    log.info("=" * 65)

    existing_raw    = load_json(RAW_FILE)
    existing_jobs   = load_json(JOBS_FILE)
    existing_review = load_json(REVIEW_FILE)
    seen_ids        = load_seen_ids()

    log.info("Seen IDs loaded: %d already-processed messages", len(seen_ids))

    new_raw, new_jobs, new_review = [], [], []
    new_seen = set()

    with sync_playwright() as pw:
        ctx  = launch_browser(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        wait_for_login(page)

        for group in GROUPS:
            log.info("-" * 55)
            log.info("Scraping group: %s", group)

            if not open_group(page, group):
                continue

            messages = read_messages(page, scrape_time)
            log.info("  Messages in last %dh: %d", ONLY_LAST_HOURS, len(messages))

            for msg in messages:
                text, sender, wa_ts = msg["text"], msg["sender"], msg["wa_timestamp"]
                msg_id = make_msg_hash(text)

                if msg_id in seen_ids:
                    continue

                new_seen.add(msg_id)
                status, reason = classify_message(text)
                is_job = (status == "job")

                new_review.append({
                    "msg_id": msg_id, "scraped_at": scrape_ts,
                    "source_group": group, "sender": sender,
                    "wa_timestamp": wa_ts, "status": status,
                    "filter_reason": reason, "raw_message": text,
                })
                new_raw.append({
                    "msg_id": msg_id, "scraped_at": scrape_ts,
                    "source_group": group, "sender": sender,
                    "wa_timestamp": wa_ts, "is_job": is_job,
                    "raw_message": text,
                })

                if is_job:
                    job = extract_job(text, group, sender, wa_ts, scrape_ts)
                    job["id"]     = f"job_{len(existing_jobs) + len(new_jobs) + 1:04d}"
                    job["msg_id"] = msg_id
                    new_jobs.append(job)
                    log.info("  [JOB] %s | %s | %s",
                             job.get("job_title") or "-",
                             job.get("location")  or "-",
                             job.get("contract_type") or "-")
                else:
                    log.info("  [SKIP-%s] %s", status.upper(), text[:50].replace("\n"," "))

        ctx.close()

    save_seen_ids(seen_ids | new_seen)
    save_json(existing_raw    + new_raw,    RAW_FILE)
    save_json(existing_jobs   + new_jobs,   JOBS_FILE)
    save_json(existing_review + new_review, REVIEW_FILE)

    n_job  = sum(1 for r in new_review if r["status"] == "job")
    n_spam = sum(1 for r in new_review if r["status"] == "spam")
    n_nokw = sum(1 for r in new_review if r["status"] == "no_keywords")

    log.info("=" * 65)
    log.info("Run complete!")
    log.info("  New messages : %d  (%d job | %d spam | %d no-keywords)",
             len(new_review), n_job, n_spam, n_nokw)
    log.info("  New jobs     : %d", len(new_jobs))
    log.info("  Total seen   : %d  (won't be re-processed)", len(seen_ids | new_seen))
    log.info("  Output dir   : %s", OUTPUT_DIR.resolve())
    log.info("  -> review.json  <- check this to verify nothing was missed")
    log.info("=" * 65)


if __name__ == "__main__":
    main()