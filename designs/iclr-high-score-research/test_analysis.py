"""Executable specification for analysis.py on synthetic data (no network, no real data)."""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analysis  # noqa: E402

PERSONAL_KEYS = ("author", "authorids", "aff", "gender", "homepage", "email", "orcid", "google_scholar", "reviewers")


def _paper(i, status, ratings, abstract, area="learning theory", sub=3):
    return {
        "id": f"p{i}", "title": f"Paper {i}", "track": "main", "status": status,
        "abstract": abstract, "primary_area": area, "pdf_size": "1000000", "gs_citation": str(i % 7),
        "rating": ";".join(map(str, ratings)), "soundness": ";".join([str(sub)] * len(ratings)),
        "contribution": ";".join([str(sub)] * len(ratings)), "presentation": ";".join([str(1 + i % 4)] * len(ratings)),
        "author": "Secret Person", "authorids": "~Secret_Person1", "aff": "Secret U", "gender": "F",
        "reviewers": "abcd;efgh;ijkl",
    }


def _synthetic_year(n=400, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        q = rng.normal()
        ratings = [int(np.clip(round(5 + 2 * q + rng.normal()), 1, 10)) for _ in range(4)]
        mean = float(np.mean(ratings))
        status = "Poster" if mean >= 5.5 else ("Withdraw" if i % 9 == 0 else "Reject")
        abstract = ("We prove a theorem. " if q > 0 else "") + ("A simple method. " if i % 3 == 0 else "Plain text.")
        area = ["learning theory", "generative models"][i % 2]
        out.append(_paper(i, status, ratings, abstract, area, sub=int(np.clip(round(3 + q), 1, 4))))
    out.append({**_paper(n, "ICLR 2026 Workshop X", [8, 8, 8], "workshop"), "track": "main"})
    return out


def test_rank_helpers_match_definitions():
    assert list(analysis.rankdata([10, 20, 20, 30])) == [1.0, 2.5, 2.5, 4.0]
    assert analysis.auc([1, 2, 3, 4], [0, 0, 1, 1]) == 1.0
    assert analysis.auc([1, 1, 1, 1], [0, 1, 0, 1]) == 0.5
    rho, p = analysis.spearman(list(range(100)), [x ** 3 for x in range(100)])
    assert abs(rho - 1.0) < 1e-12 and p < 1e-10


def test_icc_is_one_for_identical_reviewers_and_near_zero_for_noise():
    perfect = [{"ratings": [v, v, v]} for v in range(1, 11)]
    assert abs(analysis.icc1(perfect)["icc_single_reviewer"] - 1.0) < 1e-12
    rng = np.random.default_rng(1)
    noise = [{"ratings": list(rng.integers(1, 5, 3).astype(float))} for _ in range(3000)]
    assert abs(analysis.icc1(noise)["icc_single_reviewer"]) < 0.05


def test_kruskal_detects_shift_and_not_null():
    rng = np.random.default_rng(2)
    _, p_shift = analysis.kruskal_h([rng.normal(0, 1, 500), rng.normal(0.5, 1, 500)])
    _, p_null = analysis.kruskal_h([rng.normal(0, 1, 500), rng.normal(0, 1, 500)])
    assert p_shift < 1e-6 and p_null > 0.01


def test_pipeline_excludes_workshops_and_writes_no_personal_fields(tmp_path):
    for year in (2023, 2024, 2026):
        rows = _synthetic_year(seed=year)
        if year == 2023:
            for r in rows:
                r["recommendation"] = r.pop("rating")
                r["correctness"], r["technical_novelty"], r["empirical_novelty"] = r["soundness"], r["contribution"], r["presentation"]
        (tmp_path / f"iclr{year}.json").write_text(json.dumps(rows))
    papers = {"columns": ["number", "forum", "title", "ratings", "confidence_mean", "first_mean"],
              "rows": [[i, f"f{i}", ("A Simple " if i % 4 == 0 else "") + f"Title {i}", [1 + i % 4, 2, 3, 2], 3.0, None] for i in range(300)]}
    (tmp_path / "p27.json").write_text(json.dumps(papers))
    out = tmp_path / "results.json"
    sys.argv = ["analysis.py", "--data-dir", str(tmp_path), "--iclr2027-papers", str(tmp_path / "p27.json"), "--out", str(out)]
    analysis.main()
    first = out.read_bytes()
    analysis.main()
    assert out.read_bytes() == first, "fixed seed must make results byte-identical"
    res = json.loads(first)
    rep = res["years"]["2024"]
    assert rep["n_scored_main_track"] == 400, "workshop rows must be excluded"
    assert rep["features"]["theory"]["z_rating_diff"] > 0.5, "planted theory->rating signal must be recovered"
    assert 0.5 <= rep["auc_mean_rating_for_acceptance"] <= 1.0
    text = first.decode()
    for key in PERSONAL_KEYS:
        assert f'"{key}' not in text
    assert "Secret" not in text and "Title 1" not in text and "Paper 1" not in text
