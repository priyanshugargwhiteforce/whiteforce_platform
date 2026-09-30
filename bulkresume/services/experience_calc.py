"""
EXPERIENCE ARITHMETIC (deterministic)
---------------------------------------
Years of experience used to be worked out by the LLM ("add up the
durations"), which gives a different answer on different runs of the same
resume (2 years one time, 3 the next) -- and total experience drives the
experience row of every JD match. These helpers do the arithmetic in code
so the same resume always gives the same number.

Pure functions, no Django / LLM dependencies.
"""
import datetime
import re
from typing import Optional

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH_RE = "|".join(_MONTHS)
_PRESENT_RE = re.compile(r"\b(present|current|currently|till date|to date|now|ongoing|today)\b", re.I)
_RANGE_SPLIT_RE = re.compile(r"\s*(?:-|–|—|\bto\b|\btill\b|\buntil\b)\s*", re.I)
_MONTH_YEAR_RE = re.compile(rf"(?:\b({_MONTH_RE})[a-z]*\.?[\s,'’-]*)?((?:19|20)\d{{2}})", re.I)
_NUM_DATE_RE = re.compile(r"\b(\d{1,2})[/.](?:0?)((?:19|20)\d{2})\b")


def parse_years(text) -> Optional[float]:
    """'4 years' -> 4.0, '3.5 yrs' -> 3.5, '4y 6m' -> 4.5, '18 months' -> 1.5.
    Returns None when no unit is present (a bare '5' is ambiguous)."""
    t = str(text or "").lower()
    years = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?|y)\b", t)
    months = re.search(r"(\d+(?:\.\d+)?)\s*(?:months?|mos?|m)\b", t)
    if not years and not months:
        return None
    total = (float(years.group(1)) if years else 0.0) + (float(months.group(1)) / 12 if months else 0.0)
    return round(total, 2)


def _point(text: str, today: datetime.date, default_month: int) -> Optional[int]:
    """One end of a date range -> month index (year*12 + month), or None."""
    if _PRESENT_RE.search(text):
        return today.year * 12 + today.month
    numeric = _NUM_DATE_RE.search(text)
    if numeric:
        month = int(numeric.group(1))
        if 1 <= month <= 12:
            return int(numeric.group(2)) * 12 + month
    found = _MONTH_YEAR_RE.search(text)
    if not found:
        return None
    month = _MONTHS.index(found.group(1).lower()) + 1 if found.group(1) else default_month
    return int(found.group(2)) * 12 + month


def _range_months(duration: str, today: datetime.date):
    """'Jan 2021 - Present' -> (start_idx, end_idx); None if it isn't a date range."""
    parts = _RANGE_SPLIT_RE.split(str(duration or ""), maxsplit=1)
    if len(parts) != 2:
        return None
    start, end = _point(parts[0], today, 6), _point(parts[1], today, 6)
    if start is None or end is None or end < start:
        return None
    return start, end


def estimate_total_experience(durations, today: Optional[datetime.date] = None) -> str:
    """
    Sum of the employment durations actually written on the resume, as a
    string like "3 years" / "3.5 years" / "8 months", or "" if none of them
    can be read. Overlapping date ranges are counted once. Durations given as
    a length only ("2 years") are added on top of the dated ranges.
    """
    today = today or datetime.date.today()
    intervals, extra_years = [], 0.0
    for duration in durations or []:
        rng = _range_months(duration, today)
        if rng:
            intervals.append(rng)
            continue
        length = parse_years(duration)
        if length:
            extra_years += length

    intervals.sort()
    merged_months = 0
    cur_start = cur_end = None
    for start, end in intervals:
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                merged_months += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_end is not None:
        merged_months += cur_end - cur_start

    total_years = merged_months / 12 + extra_years
    if total_years <= 0:
        return ""
    if total_years < 1:
        return f"{max(1, round(total_years * 12))} months"
    rounded = round(total_years * 2) / 2   # nearest half year
    if rounded == 1:
        return "1 year"
    return f"{int(rounded)} years" if float(rounded).is_integer() else f"{rounded:g} years"
