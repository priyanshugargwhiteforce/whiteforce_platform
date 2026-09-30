"""
POST-LLM VERIFICATION OF A RESUME<->JD MATCH
----------------------------------------------
The LLM's field_breakdown is a judgement; this module checks it against the
resume itself so the final numbers are trustworthy and comparable across a
batch of candidates matched to the same JD:

  1. Completeness  - every JD requirement gets exactly one row, in JD order.
                     A row the model skipped is filled in by a plain text
                     check instead of silently disappearing; rows for
                     requirements that are not in the JD are dropped.
  2. Evidence      - a "match" must quote text that actually exists in the
                     candidate's profile or raw resume text. Unsupported
                     claims are downgraded to 0 (no hallucinated matches).
  3. False negatives - a skill the model marked missing but that appears
                     verbatim in the resume text is upgraded (parsing can
                     miss a skill that the raw resume does contain).
  4. Experience    - computed from the numbers (total_experience vs the
                     JD's min/max) whenever the years can be read, instead
                     of trusting the model's arithmetic.

Pure functions over plain data: no Django / LLM / network dependencies.
"""
import re
from typing import Optional

from .experience_calc import parse_years

_SKILL_FIELDS = {"must_have_skill", "good_to_have_skill"}
_EVIDENCE_OVERLAP = 0.6   # share of an evidence snippet's words that must exist in the resume
_MATCHED_AT = 60          # match_percent at/above which a row counts as matched

_TOKEN_RE = re.compile(r"[a-z0-9+#.]{2,}")


def _flatten(value) -> list:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _flatten(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _flatten(v)]
    if value is None or isinstance(value, bool):
        return []
    return [str(value)]


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


def _tokens(text: str) -> set:
    return {t.strip(".") for t in _TOKEN_RE.findall(str(text).lower()) if t.strip(".")}


def _contains_term(term: str, text_lower: str) -> bool:
    """Whole-term search that is safe for 'c++', '.net', 'node.js', 'ci/cd'."""
    term = _norm_ws(term)
    if len(term) < 2:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text_lower) is not None


def _evidence_supported(evidence: str, corpus_tokens: set) -> bool:
    """True if enough of the words in each ' | '-joined snippet appear in the resume."""
    for snippet in re.split(r"\s*\|\s*", str(evidence or "")):
        words = _tokens(snippet)
        if not words:
            continue
        if sum(1 for w in words if w in corpus_tokens) / len(words) >= _EVIDENCE_OVERLAP:
            return True
    return False


def _to_float(value) -> Optional[float]:
    try:
        return float(str(value).strip()) if str(value).strip() != "" else None
    except ValueError:
        return None


def _fmt(num: float) -> str:
    return str(int(num)) if float(num).is_integer() else f"{num:g}"


def _yrs(num: float) -> str:
    return "1 year" if num == 1 else f"{_fmt(num)} years"


def _row(field, requirement, evidence, matched, percent, note) -> dict:
    return {
        "field": field, "jd_requirement": requirement, "candidate_value": evidence,
        "matched": bool(matched), "match_percent": float(percent), "note": note,
    }


def _find_llm_row(rows: list, field_group: set, requirement: str, used: set):
    req = _norm_ws(requirement)
    for idx, row in enumerate(rows):
        if idx in used or (row.get("field") or "").strip().lower() not in field_group:
            continue
        got = _norm_ws(row.get("jd_requirement"))
        if got and (got == req or got in req or req in got):
            used.add(idx)
            return row
    return None


def verify_field_breakdown(jd: dict, candidate: dict, raw_text: str, llm_rows: list) -> tuple[list, bool, str]:
    """
    Returns (rows, changed, experience_match_text).
      rows                 - one verified row per JD requirement, JD order
      changed              - True if any LLM row was corrected/dropped/added
                             (the LLM's prose summaries may then be stale)
      experience_match_text - a numeric sentence, or "" if years couldn't be read
    """
    candidate_text = " ".join(_flatten(candidate))
    corpus_lower = _norm_ws(candidate_text + " " + (raw_text or ""))
    corpus_tokens = _tokens(corpus_lower)

    rows, changed, used = [], False, set()
    llm_rows = [r for r in (llm_rows or []) if isinstance(r, dict)]

    def take_single(field_name: str):
        """First not-yet-used LLM row of the given field (experience/location
        appear at most once per JD)."""
        for idx, row in enumerate(llm_rows):
            if idx not in used and (row.get("field") or "").strip().lower() == field_name:
                used.add(idx)
                return row
        return None

    def check_requirement(field: str, requirement: str, group: set):
        nonlocal changed
        row = _find_llm_row(llm_rows, group, requirement, used)
        verbatim = _contains_term(requirement, corpus_lower)

        if row is None:
            # The model skipped this requirement: decide by plain text search.
            changed = True
            rows.append(_row(field, requirement,
                             "Found verbatim in resume" if verbatim else "",
                             verbatim, 100 if verbatim else 0,
                             "Checked by text search" if verbatim else "No evidence in resume"))
            return

        pct = row.get("match_percent")
        pct = float(pct) if isinstance(pct, (int, float)) else (100.0 if row.get("matched") else 0.0)
        evidence = str(row.get("candidate_value") or "")
        note = str(row.get("note") or "")

        if pct > 0:
            supported = _evidence_supported(evidence, corpus_tokens) or verbatim
            if not supported:
                # Claimed match, but nothing in the resume backs it up.
                changed = True
                pct, evidence, note = 0.0, "", "Evidence not found in resume"
        elif field in _SKILL_FIELDS and verbatim and not evidence:
            # Model said "missing" but the exact skill is written in the resume.
            changed = True
            pct, evidence, note = 100.0, "Found verbatim in resume", "Verbatim match in resume text"

        matched = pct >= _MATCHED_AT
        if matched != bool(row.get("matched")):
            changed = True
        rows.append(_row(field, requirement, evidence, matched, pct, note))

    for req in jd.get("must_have_skills") or []:
        check_requirement("must_have_skill", req, {"must_have_skill"})
    for req in jd.get("good_to_have_skills") or []:
        check_requirement("good_to_have_skill", req, {"good_to_have_skill"})
    for req in jd.get("qualifications") or []:
        check_requirement("qualification", req, {"qualification"})

    # ── experience: numbers, not model arithmetic ─────────────────────────
    min_years = _to_float(jd.get("min_experience_years"))
    max_years = _to_float(jd.get("max_experience_years"))
    experience_text = ""
    if min_years is not None or max_years is not None:
        llm_exp = take_single("experience")
        wanted = (f"{_fmt(min_years)}-{_fmt(max_years)} years" if min_years is not None and max_years is not None
                  else f"{_fmt(min_years)}+ years" if min_years is not None
                  else f"up to {_fmt(max_years)} years")
        have = parse_years(candidate.get("total_experience"))

        if have is not None and min_years is not None:
            meets = have >= min_years
            pct = 100.0 if meets else round(have / min_years * 100, 1) if min_years else 100.0
            experience_text = (
                f"Meets requirement: {_yrs(have)} vs {_fmt(min_years)}+ required"
                if meets else f"Below requirement: {_yrs(have)} vs {_fmt(min_years)}+ required"
            )
            if meets and max_years is not None and have > max_years:
                experience_text += f" (above the {_fmt(max_years)}-year maximum)"
            rows.append(_row("experience", wanted, _yrs(have), meets, pct, experience_text))
            changed = changed or llm_exp is None
        elif llm_exp is not None:
            pct = llm_exp.get("match_percent")
            pct = float(pct) if isinstance(pct, (int, float)) else 0.0
            rows.append(_row("experience", wanted, str(llm_exp.get("candidate_value") or ""),
                             pct >= _MATCHED_AT, pct, str(llm_exp.get("note") or "")))
        else:
            changed = True
            rows.append(_row("experience", wanted, "", False, 0, "Candidate experience not stated"))

    # ── location ──────────────────────────────────────────────────────────
    location = (jd.get("location") or "").strip()
    if location:
        llm_loc = take_single("location")
        if llm_loc is not None:
            pct = llm_loc.get("match_percent")
            pct = float(pct) if isinstance(pct, (int, float)) else (100.0 if llm_loc.get("matched") else 0.0)
            evidence = str(llm_loc.get("candidate_value") or "")
            if pct > 0 and not (_evidence_supported(evidence, corpus_tokens) or "remote" in location.lower()):
                changed, pct, evidence = True, 0.0, ""
            rows.append(_row("location", location, evidence, pct >= _MATCHED_AT, pct, str(llm_loc.get("note") or "")))
        else:
            changed = True
            in_address = _contains_term(location, _norm_ws(candidate.get("candidate_address")))
            rows.append(_row("location", location, str(candidate.get("candidate_address") or "") if in_address else "",
                             in_address, 100 if in_address else 0,
                             "Address matches job location" if in_address else "Location not matched"))

    if len(used) < len(llm_rows):
        changed = True  # the model added rows that aren't JD requirements; they were dropped
    return rows, changed, experience_text
