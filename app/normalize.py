"""Merchant-name normalization."""
from __future__ import annotations

import re

# (regex, canonical name). Order matters: first match wins.
ALIASES: list[tuple[str, str]] = [
    (r"NETFLIX", "Netflix"),
    (r"SPOTIFY", "Spotify"),
    (r"HULU", "Hulu"),
    (r"DISNEY ?(PLUS|\+)|DISNEYPLUS", "Disney+"),
    (r"PRIME ?VIDEO|AMZN ?PRIME|AMAZON ?PRIME|PRIME MEMBERSHIP", "Amazon Prime"),
    (r"AUDIBLE", "Audible"),
    (r"KINDLE ?UNLTD|KINDLE UNLIMITED", "Kindle Unlimited"),
    (r"APPLE\.COM|APPLE COM BILL|ITUNES|ICLOUD|APL\*", "Apple"),
    (r"ADOBE", "Adobe"),
    (r"XBOX|GAME ?PASS", "Xbox Game Pass"),
    (r"MICROSOFT|MSFT|OFFICE ?365|M365", "Microsoft 365"),
    (r"YOUTUBE ?PREMIUM|YOUTUBE ?TV|GOOGLE \*?YOUTUBE|YOUTUBEPREMIUM", "YouTube Premium"),
    (r"GOOGLE ?\*?(ONE|STORAGE)|GOOGLE ?\*?PLAY|GOOGLE ?\*?SERVICES", "Google Play / Google One"),
    (r"HBO ?MAX|MAX\.COM|HBOMAX", "HBO Max"),
    (r"PARAMOUNT", "Paramount+"),
    (r"PEACOCK", "Peacock"),
    (r"DROPBOX", "Dropbox"),
    (r"NYTIMES|NY TIMES|NEW YORK TIMES", "New York Times"),
    (r"OPENAI|CHATGPT", "ChatGPT"),
    (r"PLAYSTATION|SONY ?INTERACTIVE|PSN", "PlayStation Plus"),
    (r"NINTENDO", "Nintendo Switch Online"),
    (r"LINKEDIN", "LinkedIn Premium"),
    (r"SIRIUS ?XM|SIRIUSXM", "SiriusXM"),
    (r"PLANET ?FITNESS|PLANET FIT", "Planet Fitness"),
    (r"DASHPASS|DOORDASH ?DASH ?PASS", "DoorDash DashPass"),
    (r"UBER ?ONE", "Uber One"),
    (r"PELOTON", "Peloton"),
    (r"DUOLINGO", "Duolingo"),
    (r"CANVA", "Canva"),
]
_ALIAS_RE = [(re.compile(p), name) for p, name in ALIASES]
KNOWN_SUBSCRIPTIONS = {name for _, name in ALIASES}

# Payment-processor prefixes that hide the real merchant.
_PREFIX_RE = re.compile(
    r"^(SQ ?\*|TST ?\*|PAYPAL ?\*|PP ?\*|SP ?\*|GOOGLE ?\*|PY ?\*|IC ?\*|POS |DEBIT CARD PURCHASE |"
    r"PURCHASE AUTHORIZED ON \d\d/\d\d |RECURRING (DEBIT )?(PAYMENT|PURCHASE) |ACH (DEBIT )?|CHECKCARD \d* ?|"
    r"CARD PURCHASE( WITH PIN)? |DBT CRD \d* ?)+"
)
_NOISE_RE = re.compile(
    r"(\bWWW\.|\.COM\b|\.NET\b|\bINC\b|\bLLC\b|\bCORP\b|\bCO\b|\bUS\b|\bUSA\b|\bHTTPS?\S*|"
    r"\b[A-Z]{2}\s*$|#\S+|\*\S+|\S*\d\S*)"
)

def mask_digits(text: str) -> str:
    """Mask long digit runs (card/account/reference numbers) before storing."""
    return re.sub(r"\d{6,}", lambda m: "•" * 4 + m.group()[-2:], text)

def normalize_merchant(description: str) -> str:
    raw = (description or "").upper().strip()
    for rx, name in _ALIAS_RE:
        if rx.search(raw):
            return name
    s = _PREFIX_RE.sub("", raw)
    s = _NOISE_RE.sub(" ", s)
    s = re.sub(r"[^A-Z&' ]+", " ", s)
    words = [w for w in s.split() if len(w) > 1 or w == "&"]
    if not words:
        return (description or "Unknown").strip()[:40] or "Unknown"
    return " ".join(words[:3]).title()

def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unknown"
