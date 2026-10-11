"""Ranking, rights gating, dedup and diversity tests."""

from __future__ import annotations

from imgagent import constants as C
from imgagent.models import ImageCandidate
from imgagent.ranking import (
    mark_near_duplicates,
    passes_rights_gate,
    quality_score,
    rank_candidates,
    score_candidate,
    select_diverse,
)


def cand(provider="wikimedia", title="Isdal Woman Bergen", image_url=None, **kw):
    return ImageCandidate(provider=provider, image_url=image_url or f"https://x.example/{title}.jpg",
                          search_query="isdal woman", title=title, **kw)


def test_relevance_prefers_text_matches():
    q = "Isdal Woman Bergen 1970"
    strong = cand(title="Isdal Woman Bergen 1970 police photograph")
    weak = cand(title="A completely unrelated sunset landscape")
    assert score_candidate(strong, q) > score_candidate(weak, q)


def test_entity_boost():
    q = '"Isdal Woman" Bergen'
    with_entity = cand(title="Isdal Woman found in Bergen")
    without = cand(title="some photo found in Bergen")
    assert score_candidate(with_entity, q) > score_candidate(without, q)


def test_resolution_and_aspect_affect_quality():
    big = cand(title="a", width=3000, height=2000, byte_size=900_000)
    tiny = cand(title="b", width=80, height=80, byte_size=900)
    extreme = cand(title="c", width=3000, height=100, byte_size=900_000)
    assert quality_score(big) > quality_score(tiny)
    assert quality_score(big) > quality_score(extreme)


def test_rights_state_influences_score():
    pd = cand(title="same title", rights_status=C.RIGHTS_VERIFIED_PD)
    unknown = cand(title="same title", rights_status=C.RIGHTS_UNKNOWN)
    assert score_candidate(pd, "same title") > score_candidate(unknown, "same title")


def test_rights_gate_blocks_unknown_when_required():
    assert not passes_rights_gate(cand(rights_status=C.RIGHTS_UNKNOWN), require_reuse_rights=True)
    assert passes_rights_gate(cand(rights_status=C.RIGHTS_VERIFIED_PD), require_reuse_rights=True)
    assert passes_rights_gate(cand(rights_status=C.RIGHTS_VERIFIED_LICENSE), require_reuse_rights=True)


def test_rank_sorts_deterministically():
    def build():
        cands = [cand(title=f"photo {i}") for i in range(5)]
        cands[2].width, cands[2].height = 4000, 3000
        return cands
    ranked = rank_candidates(build(), "photo")
    scores = [c.relevance_score for c in ranked]
    assert scores == sorted(scores, reverse=True)
    # same input set -> same order (deterministic tie-break by candidate_id)
    ranked2 = rank_candidates(build(), "photo")
    assert [c.candidate_id for c in ranked] == [c.candidate_id for c in ranked2]


def test_cross_provider_url_dedup():
    url = "https://img.example/same.jpg"
    a = cand(provider="wikimedia", image_url=url, title="a")
    b = cand(provider="duckduckgo", image_url=url, title="b")
    ranked = rank_candidates([a, b], "photo")
    assert len(ranked) == 1
    assert ranked[0].provider == "wikimedia"  # trusted provider kept


def test_select_diverse_caps_host_variety():
    cands = []
    for i in range(10):
        cands.append(cand(title=f"photo {i}", image_url=f"https://host-{i % 2}.example/{i}.jpg"))
    picked = select_diverse(rank_candidates(cands, "photo"), top_n=4, threshold=0)
    assert len(picked) == 4
    hosts = [c.image_url.split("/")[2] for c in picked]
    assert hosts.count(hosts[0]) <= 3


def test_near_duplicate_marking():
    cands = [cand(title="a", image_url="https://x/1.jpg"),
             cand(title="a-dup", image_url="https://x/2.jpg")]
    cands[0].thumbnail_url = "https://t/1.jpg"
    cands[1].thumbnail_url = "https://t/1.jpg"
    mark_near_duplicates(cands)
    assert cands[1].extra.get("near_duplicate_of") == cands[0].candidate_id


def test_prefer_authentic_boost():
    q = "Isdal Woman Norway 1970 historical photographs"
    historical = cand(title="Isdal Woman Norway 1970 archive photograph")
    modern = cand(title="modern reenactment cosplay picture")
    assert score_candidate(historical, q, prefer_authentic=True) > \
           score_candidate(historical, q, prefer_authentic=False)
    assert score_candidate(historical, q, prefer_authentic=True) > \
           score_candidate(modern, q, prefer_authentic=True)
