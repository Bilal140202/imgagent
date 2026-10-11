"""Deterministic ranking, exact + near dedup, and diversity selection.

Score components (0..100, deterministic, no LLM required):
- text relevance: weighted token overlap between query and title/description
- entity boost: quoted phrases, capitalized tokens and years from the query
- resolution: logarithmic bonus capped at ~4 MP
- aspect suitability: penalize extreme aspect ratios (keep 0.45..2.2)
- rights: verified_public_domain > verified_license > unknown > restricted
- provider trust: official APIs (wikimedia/openverse) outrank volatile scrapes
- authenticity heuristic: year/historical-token matches when --prefer-authentic

Safety overrides ranking: a failed safety/validation state never enters the
result set, and require_reuse_rights drops unknown/restricted rights before
scoring — a high relevance score cannot override a failed rights gate.
"""

from __future__ import annotations

import re
from dataclasses import replace

from imgagent import constants as C
from imgagent.models import ImageCandidate
from imgagent.util import normalize_url_for_dedup

_YEAR_RE = re.compile(r"\b(1[5-9]\d\d|20[0-4]\d)\b")
_HISTORICAL_TOKENS = {"historical", "archive", "archival", "vintage", "photograph", "photo", "documentary"}

_RIGHTS_SCORE = {
    C.RIGHTS_VERIFIED_PD: 10.0,
    C.RIGHTS_VERIFIED_LICENSE: 9.0,
    C.RIGHTS_UNKNOWN: 4.0,
    C.RIGHTS_PERMISSION_REQUIRED: 2.0,
    C.RIGHTS_RESTRICTED: 0.0,
}
_PROVIDER_SCORE = {"wikimedia": 5.0, "openverse": 4.5}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1}


def _entity_tokens(query: str) -> set[str]:
    """Quoted phrases, capitalized words, and years carry entity weight."""
    quoted = [p.lower() for p in re.findall(r'"([^"]+)"', query)]
    capped = [w.lower() for w in re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", query)]
    years = _YEAR_RE.findall(query)
    return {t for t in (*quoted, *capped, *years) if t}


def _aspect_score(width: int | None, height: int | None) -> float:
    if not width or not height:
        return 0.55  # unknown aspect gets a neutral-ish score
    ratio = width / height
    if 0.45 <= ratio <= 2.2:
        return 1.0
    if 0.25 <= ratio < 0.45 or 2.2 < ratio <= 4.0:
        return 0.5
    return 0.1


def _resolution_score(width: int | None, height: int | None) -> float:
    if not width or not height:
        return 0.4
    import math
    mp = width * height
    return min(1.0, math.log10(max(mp, 10)) / math.log10(4_000_000))


def quality_score(cand: ImageCandidate) -> float:
    """Technical quality 0..1 — resolution plus aspect suitability."""
    res = _resolution_score(cand.width, cand.height)
    aspect = _aspect_score(cand.width, cand.height)
    if cand.byte_size:
        size_factor = min(1.0, cand.byte_size / 200_000)
        return round(min(1.0, res * 0.6 + aspect * 0.25 + size_factor * 0.15), 4)
    return round(min(1.0, res * 0.7 + aspect * 0.3), 4)


def score_candidate(cand: ImageCandidate, query: str, *, prefer_authentic: bool = False) -> float:
    q_tokens = _tokens(query)
    doc_tokens = _tokens((cand.title or "") + " " + (cand.description or ""))
    overlap = len(q_tokens & doc_tokens) / max(len(q_tokens), 1) if q_tokens else 0.0
    text = overlap * 40.0

    ents = _entity_tokens(query)
    doc_low = ((cand.title or "") + " " + (cand.description or "")).lower()
    entity = (sum(1 for e in ents if e in doc_low) / max(len(ents), 1)) * 20.0 if ents else 0.0

    rights = _RIGHTS_SCORE.get(cand.rights_status, 2.0)
    provider = _PROVIDER_SCORE.get(cand.provider.split(":")[0], 2.5)
    tech = quality_score(cand) * 15.0

    score = text + entity + rights + provider + tech
    if prefer_authentic:
        years = _YEAR_RE.findall(query)
        if any(y in doc_low for y in years):
            score += 5.0
        if _HISTORICAL_TOKENS & q_tokens and _HISTORICAL_TOKENS & _tokens(doc_low):
            score += 3.0
    return round(score, 4)


def passes_rights_gate(cand: ImageCandidate, require_reuse_rights: bool) -> bool:
    if not require_reuse_rights:
        return True
    return cand.rights_status in (C.RIGHTS_VERIFIED_PD, C.RIGHTS_VERIFIED_LICENSE)


def rank_candidates(cands: list[ImageCandidate], query: str, *, prefer_authentic: bool = False,
                    require_reuse_rights: bool = False) -> list[ImageCandidate]:
    """Assign scores and return sorted, rights-gated, URL-deduped candidates."""
    scored = []
    seen_urls: dict[str, ImageCandidate] = {}
    for c in cands:
        if not passes_rights_gate(c, require_reuse_rights):
            continue
        key = normalize_url_for_dedup(c.image_url)
        prev = seen_urls.get(key)
        if prev is not None:
            # keep the better record across providers for the same image URL
            if _PROVIDER_SCORE.get(c.provider.split(":")[0], 2.5) > _PROVIDER_SCORE.get(prev.provider.split(":")[0], 2.5):
                seen_urls[key] = c
            continue
        seen_urls[key] = c
    for c in seen_urls.values():
        rel = score_candidate(c, query, prefer_authentic=prefer_authentic)
        c.relevance_score = rel
        c.quality_score = quality_score(c)
        scored.append(c)
    scored.sort(key=lambda c: (-c.relevance_score, c.candidate_id))  # deterministic tie-break
    return scored


def select_diverse(cands: list[ImageCandidate], top_n: int, threshold: int = 6) -> list[ImageCandidate]:
    """Greedy MMR-style selection using perceptual hashes requires image bytes.

    Pre-download we approximate diversity with URL/title variety and keep the
    ranked order; post-download perceptual near-dup marking happens in the
    download engine. Listed candidates retain near_duplicate_of hints for
    same-provider thumbnail URLs when directly comparable.
    """
    if top_n <= 0 or len(cands) <= top_n:
        return list(cands)
    picked: list[ImageCandidate] = []
    seen_hosts: dict[str, int] = {}
    pool = list(cands)
    # first pass: cap 3 candidates per image host for variety
    for c in pool:
        if len(picked) >= top_n:
            break
        host = normalize_url_for_dedup(c.image_url).split("/")[2] if "://" in c.image_url else c.image_url
        if seen_hosts.get(host, 0) >= 3:
            continue
        seen_hosts[host] = seen_hosts.get(host, 0) + 1
        picked.append(c)
    # second pass: fill with the best remaining
    if len(picked) < top_n:
        chosen_ids = {c.candidate_id for c in picked}
        for c in pool:
            if len(picked) >= top_n:
                break
            if c.candidate_id not in chosen_ids:
                picked.append(c)
                chosen_ids.add(c.candidate_id)
    return picked


def mark_near_duplicates(cands: list[ImageCandidate], threshold: int = 6) -> None:
    """Tag near_duplicate_of among candidates sharing provider thumbnails (best effort).

    Full perceptual dedup happens after download (content hashes). Pre-download
    we only compare same-provider thumbnails, which are consistently sized.
    """
    by_thumb: dict[str, list[ImageCandidate]] = {}
    for c in cands:
        if c.thumbnail_url:
            by_thumb.setdefault(normalize_url_for_dedup(c.thumbnail_url), []).append(c)
    for group in by_thumb.values():
        if len(group) > 1:
            for dup in group[1:]:
                dup.extra["near_duplicate_of"] = group[0].candidate_id


def apply_variant_merge(candidates: list[ImageCandidate], base_query: str) -> list[ImageCandidate]:
    """Normalize search_query to the base query for candidates found via variants."""
    return [replace(c, search_query=base_query if c.search_query != base_query else c.search_query)
            for c in candidates]
