"""Deterministic normalisation and comparison helpers.

Nothing here guesses. When a value cannot be parsed unambiguously the helpers
return ``None`` (or a problem code) and the caller marks the field for review.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import List, Optional, Set, Tuple

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
TWO_PLACES = Decimal("0.01")


def western_digits(s: str) -> str:
    return s.translate(_DIGITS)


# ---------------------------------------------------------------- amounts

_AMOUNT_RE = re.compile(r"^-?\d+(\.\d+)?$")


def parse_amount(raw: Optional[str]) -> Optional[Decimal]:
    """Parse a printed amount into a Decimal. Returns None if it is not a clean number."""
    if raw is None:
        return None
    s = western_digits(str(raw)).strip()
    s = s.replace("٫", ".").replace("٬", ",").replace("٫", ".")
    s = re.sub(r"[^\d.,\-]", "", s)
    if not s or s in ("-", ".", ","):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    if not _AMOUNT_RE.match(s):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def money(d: Optional[Decimal]) -> Optional[str]:
    """Canonical string form used in JSON: two decimal places, no grouping."""
    if d is None:
        return None
    return str(d.quantize(TWO_PLACES, rounding=ROUND_HALF_UP))


def fmt_money(d: Optional[Decimal]) -> str:
    if d is None:
        return "—"
    return format(d.quantize(TWO_PLACES, rounding=ROUND_HALF_UP), ",.2f")


def amounts_equal(a: Optional[Decimal], b: Optional[Decimal]) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= TWO_PLACES / 2


# ---------------------------------------------------------------- currency

KNOWN_CURRENCIES = {
    "SAR", "USD", "EUR", "GBP", "AED", "BHD", "KWD", "OMR", "QAR", "EGP", "JOD", "TRY", "INR",
    "PKR", "CHF", "CAD", "AUD", "JPY", "CNY", "MYR", "SGD", "THB", "LBP", "MAD", "TND", "DZD",
    "IDR", "PHP", "BDT", "LKR", "KRW", "ZAR", "SEK", "NOK", "DKK", "NZD", "HKD", "BRL", "MXN",
    "IQD", "SDG", "YER", "SYP", "IRR", "AZN", "GEL", "KZT", "UZS", "ETB", "KES", "NGN",
}
# Only symbols/names that identify exactly one currency. "$" or bare "ريال" are
# deliberately absent: they are ambiguous and must be confirmed by the member.
_CURRENCY_ALIASES = {
    "SR": "SAR",
    "S.R": "SAR",
    "S.R.": "SAR",
    "ر.س": "SAR",
    "ر.س.": "SAR",
    "رس": "SAR",
    "ريال سعودي": "SAR",
    "SAUDI RIYAL": "SAR",
    "SAUDI RIYALS": "SAR",
    "US$": "USD",
    "US DOLLAR": "USD",
    "دولار امريكي": "USD",
    "€": "EUR",
    "EURO": "EUR",
    "£": "GBP",
    "د.إ": "AED",
    "درهم اماراتي": "AED",
    "UAE DIRHAM": "AED",
    "ج.م": "EGP",
    "جنيه مصري": "EGP",
}


def norm_currency(raw: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (ISO code, problem). problem is None when the code is recognised."""
    if raw is None or not str(raw).strip():
        return None, None
    s = " ".join(str(raw).strip().split())
    up = norm_arabic(s).upper()
    if up in KNOWN_CURRENCIES:
        return up, None
    alias = _CURRENCY_ALIASES.get(up) or _CURRENCY_ALIASES.get(s)
    if alias:
        return alias, None
    return None, "currency_unrecognized"


# ---------------------------------------------------------------- dates

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_iso_date(raw: Optional[str]) -> Optional[date]:
    if raw is None:
        return None
    s = western_digits(str(raw)).strip()
    m = _ISO_RE.match(s)
    if not m:
        return None
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    if d.year < 1990 or d.year > 2100:
        return None
    return d


def fmt_date(d: Optional[date]) -> str:
    if d is None:
        return "—"
    return d.strftime("%d/%m/%Y")


# ---------------------------------------------------------------- text / names

_AR_MARKS = re.compile(r"[ؐ-ًؚ-ٰٟـ]")
_AR_LETTER = re.compile(r"[؀-ۿ]")


def norm_arabic(s: str) -> str:
    s = _AR_MARKS.sub("", s)
    s = re.sub("[أإآٱ]", "ا", s)
    return s.replace("ة", "ه").replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي")


def script_of(s: str) -> str:
    return "ar" if _AR_LETTER.search(s or "") else "latin"


_TITLES = {"mr", "mrs", "ms", "miss", "dr", "السيد", "السيده", "المريض", "المريضه", "د", "patient", "name"}


def _tokens(s: str) -> List[str]:
    s = norm_arabic(western_digits(s).lower())
    s = re.sub(r"[^\w\s]", " ", s)
    raw = [t for t in s.split() if t]
    out: List[str] = []
    i = 0
    while i < len(raw):
        t = raw[i]
        if t in ("al", "el") and i + 1 < len(raw):  # "Al Mutairi" == "Almutairi"
            out.append(t + raw[i + 1])
            i += 2
            continue
        out.append(t)
        i += 1
    return out


def name_tokens(s: str) -> List[str]:
    return [t for t in _tokens(s) if t not in _TITLES]


def names_compatible(a: Optional[str], b: Optional[str]) -> Optional[bool]:
    """True if two person names plausibly refer to the same person.

    None means "cannot tell" (different scripts or empty), which callers must
    treat as needing confirmation rather than as a match.
    """
    if not a or not b:
        return None
    if script_of(a) != script_of(b):
        return None
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return None
    if ta == tb:
        return True
    sa, sb = set(ta), set(tb)
    small, big = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if len(small) >= 2 and small <= big and ta[0] == tb[0]:
        return True
    return False


def norm_ident(raw: Optional[str]) -> Optional[str]:
    """Normalise an identifier (invoice no., MRN, transaction ref) for equality checks."""
    if raw is None:
        return None
    s = western_digits(str(raw)).upper()
    s = re.sub(r"^(INVOICE|INV\.?\s*NO\.?|NO\.?|رقم الفاتوره|رقم)\s*[:#]?", "", norm_arabic(s)).strip()
    s = re.sub(r"[\s\-_/#.:]", "", s)
    if s.isdigit():
        s = s.lstrip("0") or "0"
    return s or None


_PROVIDER_STOP = {
    "clinic", "clinics", "medical", "center", "centre", "complex", "hospital", "polyclinic",
    "co", "company", "ltd", "llc", "est", "establishment", "pharmacy", "sample", "demo", "the",
    "and", "for", "group", "dental", "lab", "laboratory", "labs", "health", "care", "healthcare",
    "al", "el", "مجمع", "عياده", "عيادات", "مستشفي", "مركز", "طبي", "الطبي", "الطبيه", "شركه",
    "مؤسسه", "صيدليه", "عينه", "مختبر", "مختبرات", "لطب", "الاسنان", "اسنان", "للاسنان", "و",
    "التجريبي", "التجريبيه", "تجريبي", "تجريبيه", "مستوصف",
}


def _provider_tokens(s: str) -> Set[str]:
    out = set()
    for t in _tokens(s):
        if t in _PROVIDER_STOP:
            continue
        if t.startswith("ال") and len(t) > 3:
            t = t[2:]
        if t.startswith("al") and len(t) > 4 and script_of(t) == "latin":
            t = t[2:]
        if t in _PROVIDER_STOP or len(t) < 2:
            continue
        out.add(t)
    return out


def providers_compatible(a: Optional[str], b: Optional[str]) -> Optional[bool]:
    """Compare provider names. None = could not compare (e.g. Arabic vs English)."""
    if not a or not b:
        return None
    parts_a = [p for p in re.split(r"[/|]", a) if p.strip()]
    parts_b = [p for p in re.split(r"[/|]", b) if p.strip()]
    results = []
    for pa in parts_a:
        for pb in parts_b:
            if script_of(pa) != script_of(pb):
                continue
            ta, tb = _provider_tokens(pa), _provider_tokens(pb)
            if not ta or not tb:
                continue
            results.append(bool(ta & tb))
    if any(results):
        return True
    if results:
        return False
    return None


_KSA = {
    "saudi arabia", "ksa", "kingdom of saudi arabia", "sa", "saudi", "k.s.a", "k s a",
    "المملكه العربيه السعوديه", "السعوديه", "المملكه",
}


def country_to_location(raw: Optional[str]) -> Optional[str]:
    """Map a printed country to 'domestic' / 'international'. Never inferred from currency."""
    if not raw or not str(raw).strip():
        return None
    s = norm_arabic(" ".join(str(raw).lower().replace(",", " ").split()))
    s = s.strip(" .")
    if s in _KSA or "saudi" in s or "السعوديه" in s:
        return "domestic"
    return "international"


def norm_text(s: Optional[str]) -> str:
    return " ".join(_tokens(s or ""))
