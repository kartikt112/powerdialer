"""Indian phone numbers: parse a list cell into dialable numbers.

A cell can hold several numbers and uses shorthand that a naive split gets
wrong:

    "044-45050301 / +91 99652 51951"      two numbers, landline then mobile
    "+91 80 47780293 / 4692"               same number, last four digits 4692
    "0422-4330330 / 2332100"               same STD code, new local number
    "1800 425 5758 (toll free)"            toll-free
    "+1-855-259-3709"                      not Indian: goes to the international list

Each number comes back as {e164, dial, kind, national, raw}. `kind` is
mobile, landline or tollfree. "Starts with 6-9 means mobile" misfiles
Bangalore (080 4...), Ahmedabad (079 4...), Mysore, Belgaum and Pithampur
landlines, so the `phonenumbers` metadata decides when it is installed; when
it cannot tell (or is missing) the way the number was written decides:
a trunk 0 or an STD-style first group means landline, a 5+5 split or a single
10-digit block means mobile.

Stdlib only, Python 3.9+. `phonenumbers` is optional.
"""

import re

try:                                    # precise India ranges when available
    import phonenumbers as _pn
    from phonenumbers import PhoneNumberType as _T
except ImportError:                     # pragma: no cover - exercised by the fallback test
    _pn = None

PRECISE = _pn is not None

# Landline blocks in big cities whose codes start with 7 or 8. Only used by
# the stdlib fallback; phonenumbers knows the full picture.
_FALLBACK_LANDLINE_PREFIXES = ("802", "803", "804", "805", "806", "792", "793", "794", "796")


def _groups_after_country(text):
    """Digit groups as written, with the +91 / 0091 / trunk 0 taken off."""
    t = text.strip()
    t = re.sub(r"^\+\s*91|^0091", "", t).strip(" -.")
    trunk = t.startswith("0") or t.startswith("(0")
    groups = re.findall(r"\d+", t)
    if groups and groups[0].startswith("0"):
        groups[0] = groups[0][1:]
        if not groups[0]:
            groups = groups[1:]
    return groups, trunk


def _hint(written):
    """'landline' | 'mobile' | None from the way a number was written."""
    groups, trunk = _groups_after_country(written)
    if trunk:
        return "landline"
    if len(groups) >= 2:
        first = len(groups[0])
        if first == 5 and len(groups[1]) == 5:
            return "mobile"
        if 2 <= first <= 4:
            return "landline"
    if len(groups) == 1 and len(groups[0]) == 10:
        return "mobile"
    return None


def classify(national, written=""):
    """mobile | landline | tollfree for a 10-digit national number
    (toll-free numbers are 10 or 11 digits starting 1800 / 1860)."""
    if national.startswith(("1800", "1860")):
        return "tollfree"
    if national[0] in "12345":
        return "landline"                      # mobile series only start 6-9
    if national[0] == "9":
        return "mobile"                        # and no STD code starts with 9
    if PRECISE:
        try:
            kind = _pn.number_type(_pn.parse("+91" + national, None))
        except Exception:
            kind = None
        if kind == _T.MOBILE:
            return "mobile"
        if kind == _T.FIXED_LINE:
            return "landline"
        if kind == _T.TOLL_FREE:
            return "tollfree"
    hint = _hint(written)
    if hint:
        if not PRECISE and hint == "mobile" and national.startswith(_FALLBACK_LANDLINE_PREFIXES):
            return "landline"
        return hint
    if not PRECISE and national.startswith(_FALLBACK_LANDLINE_PREFIXES):
        return "landline"
    return "mobile"


def _strip_notes(text):
    """Drop '(toll free)', '(board)' and similar; keep '(020)'."""
    return re.sub(r"\([^)]*[A-Za-z][^)]*\)", " ", text or "")


def parse_cell(raw):
    """Returns (numbers, international, problems).

    numbers        list of dicts in the order written, de-duplicated
    international  raw strings of non-Indian numbers (+1 ...)
    problems       raw parts that could not be read as a number
    """
    text = _strip_notes(raw).strip()
    if not text:
        return [], [], []
    parts = [p.strip() for p in re.split(r"\s*(?:/|,|;|\bor\b|\|)\s*", text) if p.strip()]
    numbers, international, problems, seen = [], [], [], set()
    prev = None
    for part in parts:
        digits = re.sub(r"\D", "", part)
        if not digits:
            continue
        compact = part.replace(" ", "")
        if compact.startswith("+") and not compact.startswith("+91"):
            international.append(part)
            continue
        if compact.startswith("00") and not compact.startswith("0091"):
            international.append(part)
            continue
        if compact.startswith("+91") or compact.startswith("0091"):
            digits = digits[2:] if compact.startswith("+") else digits[4:]
        elif len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        if digits.startswith("0"):
            digits = digits[1:]
        if digits.startswith(("1800", "1860")):
            national = digits
            if not 10 <= len(national) <= 11:
                problems.append(part)
                continue
        else:
            if len(digits) < 10 and prev and len(digits) >= 3:
                # "/ 4692" keeps the front of the previous number; "/ 2332100"
                # keeps its STD code. Both are "fill the rest from before".
                digits = prev[: 10 - len(digits)] + digits
            if len(digits) != 10 or digits[0] == "0":
                problems.append(part)
                continue
            national = digits
        kind = classify(national, part)
        e164 = "+91" + national
        if e164 in seen:
            continue
        seen.add(e164)
        numbers.append({
            "e164": e164,
            "national": national,
            # Toll-free numbers only work dialled domestically, without +91.
            "dial": national if kind == "tollfree" else e164,
            "kind": kind,
            "raw": part,
        })
        if kind != "tollfree":
            prev = national
    return numbers, international, problems


def parse_one(raw):
    """A single typed number (manual dial, referral, DM mobile). None if unreadable."""
    numbers, _intl, _bad = parse_cell(raw)
    return numbers[0] if numbers else None


_TWO_DIGIT_STD = ("11", "20", "22", "33", "40", "44", "79", "80")


def pretty(e164_or_national, kind=None):
    """98765 43210 for mobiles, 080 4116 1000 for landlines, 1800 425 5758."""
    digits = re.sub(r"\D", "", str(e164_or_national or ""))
    if digits.startswith("91") and len(digits) in (12, 13):
        digits = digits[2:]
    if digits.startswith(("1800", "1860")):
        return digits[:4] + " " + digits[4:7] + " " + digits[7:]
    if len(digits) != 10:
        return str(e164_or_national or "")
    kind = kind or classify(digits)
    if kind == "mobile":
        return digits[:5] + " " + digits[5:]
    std = 2 if digits[:2] in _TWO_DIGIT_STD else 0
    if not std and PRECISE:
        try:
            std = min(4, _pn.length_of_geographical_area_code(_pn.parse("+91" + digits, None)))
        except Exception:
            std = 0
    if not std:
        std = 3 if digits[0] in "1234567" else 4
    local = digits[std:]
    split = len(local) // 2 if len(local) == 8 else 3
    return "0" + digits[:std] + " " + local[:split] + " " + local[split:]


def wa_digits(e164):
    """wa.me wants country code + number, digits only."""
    return re.sub(r"\D", "", str(e164 or ""))
