"""Reproducible, privacy-preserving analysis of public ICLR review scores.

Responsibility: test pre-specified candidate "high-score" features on public
ICLR 2023/2024/2026 OpenReview metadata (via papercopilot/paperlists), separate
review score from acceptance and from later citations, and run a temporal
hold-out check that uses only pre-review, paper-side features.

Entry point: python3 analysis.py --data-dir DIR --iclr2027-papers FILE --out results.json
Inputs (downloaded by the user, never committed; see README.md):
  https://raw.githubusercontent.com/papercopilot/paperlists/main/iclr/iclr{2023,2024}.json
  https://media.githubusercontent.com/media/papercopilot/paperlists/main/iclr/iclr2026.json (git-lfs)
  https://raw.githubusercontent.com/shiqiangw/iclr-2027-scores/main/docs/data/papers.json
Dependencies: numpy only. p-values use large-sample normal / Wilson-Hilferty
approximations (every analysed sample has n > 1,000), not exact tests.
Tests: python3 -m pytest test_analysis.py (synthetic data, no network).

Privacy: author names, ids, affiliations, gender, profiles and review text are
never read into the analysis tables; only paper-level aggregates are written.
"""
import argparse
import hashlib
import json
import math
import re
from pathlib import Path

import numpy as np

SEED = 20261118
ACCEPTED = {"Poster", "Spotlight", "Oral", "Top-25%", "Top-5%"}
REJECTED = {"Reject"}
WITHDRAWN = {"Withdraw"}

# Pre-specified lexical proxies, fixed before looking at any outcome.
FEATURES = {
    "simple_claim": r"\b(simple|simpler|simplicity|simply)\b",
    "mechanism_or_alternatives": (
        r"(rul(e|es|ed|ing) out|alternative explanation|confound|controlled experiment|"
        r"counterfactual|disentangl|isolat(e|es|ing) the|ablat|mechanis(m|ms|tic)|"
        r"causal (effect|role|evidence|mechanism))"
    ),
    "theory": r"\b(theorem|provabl[ey]|we prove|guarantee[sd]?|upper bound|lower bound)\b",
    "sota_claim": r"(state[- ]of[- ]the[- ]art|\bsota\b|outperform)",
    "llm_topic": r"\b(llms?|large language models?)\b",
}
# Exploratory split added after seeing that the broad proxy above is negative:
# separates explicit alternative-explanation language from "mechanism" wording.
EXPLORATORY = {
    "alternatives_strict": r"(rul(e|es|ed|ing) out|alternative explanation|confound|controlled experiment|counterfactual)",
    "mechanism_word": r"mechanis(m|ms|tic)",
}
TITLE_FEATURES = {k: FEATURES[k] for k in ("simple_claim", "theory", "llm_topic")}

OVERALL = {2023: "recommendation", 2024: "rating", 2026: "rating"}
SUBSCORES = {
    2023: ["correctness", "technical_novelty", "empirical_novelty"],
    2024: ["soundness", "contribution", "presentation"],
    2026: ["soundness", "contribution", "presentation"],
}


def parse_scores(s):
    if not s:
        return []
    out = []
    for tok in str(s).split(";"):
        tok = tok.strip()
        try:
            out.append(float(tok))
        except ValueError:
            pass
    return out


def load_year(data_dir, year):
    raw = json.loads((Path(data_dir) / f"iclr{year}.json").read_text())
    rows = []
    for x in raw:
        if x.get("track", "main") != "main":
            continue
        status = x.get("status", "")
        if status not in ACCEPTED | REJECTED | WITHDRAWN:
            continue
        ratings = parse_scores(x.get(OVERALL[year]))
        if not ratings:
            continue
        text = f"{x.get('title', '')} {x.get('abstract', '')}"
        row = {
            "status": status,
            "accepted": status in ACCEPTED,
            "decided": status in ACCEPTED | REJECTED,
            "withdrawn": status in WITHDRAWN,
            "ratings": ratings,
            "mean_rating": float(np.mean(ratings)),
            "area": x.get("primary_area") or "",
            "abstract_words": len((x.get("abstract") or "").split()),
            "title_words": len((x.get("title") or "").split()),
            "pdf_mb": (float(x["pdf_size"]) / 1e6) if str(x.get("pdf_size", "0")).isdigit() and float(x["pdf_size"]) > 0 else None,
            "citations": float(x["gs_citation"]) if str(x.get("gs_citation", "")).lstrip("-").isdigit() and float(x["gs_citation"]) >= 0 else None,
        }
        for sub in SUBSCORES[year]:
            v = parse_scores(x.get(sub))
            row[sub] = float(np.mean(v)) if v else None
        for name, pat in {**FEATURES, **EXPLORATORY}.items():
            row[name] = bool(re.search(pat, text, flags=re.I))
        for name, pat in TITLE_FEATURES.items():
            row[f"title_{name}"] = bool(re.search(pat, x.get("title") or "", flags=re.I))
        rows.append(row)
    return rows


def rankdata(x):
    """Average ranks (1-based) with ties sharing their mean rank."""
    _, inverse, counts = np.unique(np.asarray(x, float), return_inverse=True, return_counts=True)
    ends = np.cumsum(counts)
    return ((ends - counts + 1 + ends) / 2.0)[inverse]


def normal_two_sided_p(z):
    return math.erfc(abs(z) / math.sqrt(2.0))


def spearman(a, b):
    """Spearman rho; p from the t statistic under a normal approximation (large n)."""
    ra, rb = rankdata(a), rankdata(b)
    if ra.std() == 0 or rb.std() == 0:
        return None, None
    rho = float(np.corrcoef(ra, rb)[0, 1])
    n = len(ra)
    t = rho * math.sqrt((n - 2) / max(1e-300, 1.0 - rho * rho))
    return rho, normal_two_sided_p(t)


def kruskal_h(groups):
    """Kruskal-Wallis H with tie correction; p via Wilson-Hilferty chi-square approximation."""
    allv = np.concatenate([np.asarray(g, float) for g in groups])
    ranks = rankdata(allv)
    n = len(allv)
    h, start = 0.0, 0
    for g in groups:
        r = ranks[start:start + len(g)]
        h += r.sum() ** 2 / len(g)
        start += len(g)
    h = 12.0 / (n * (n + 1)) * h - 3 * (n + 1)
    _, counts = np.unique(allv, return_counts=True)
    h /= 1.0 - float((counts ** 3 - counts).sum()) / (n ** 3 - n)
    df = len(groups) - 1
    z = ((h / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
    return h, 0.5 * math.erfc(z / math.sqrt(2.0))


def bootstrap_diff(a, b, rng, n=2000):
    a, b = np.asarray(a), np.asarray(b)
    if len(a) < 2 or len(b) < 2:
        return None
    diffs = np.empty(n)
    for i in range(n):
        diffs[i] = rng.choice(a, len(a)).mean() - rng.choice(b, len(b)).mean()
    return [float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))]


def zscore_within_year(v):
    v = np.asarray(v, float)
    return (v - v.mean()) / v.std()


def area_stratified_diff(rows, feat, outcome):
    """Weighted mean of within-area differences (area as a confounder)."""
    by_area = {}
    for r in rows:
        by_area.setdefault(r["area"], []).append(r)
    num = den = 0.0
    for group in by_area.values():
        yes = [r[outcome] for r in group if r[feat]]
        no = [r[outcome] for r in group if not r[feat]]
        if len(yes) >= 10 and len(no) >= 10:
            w = 1.0 / (1.0 / len(yes) + 1.0 / len(no))
            num += w * (np.mean(yes) - np.mean(no))
            den += w
    return float(num / den) if den else None


def logistic_fit(X, y, l2=1e-6, iters=100):
    X = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y) + l2 * w
        H = (X * (p * (1 - p))[:, None]).T @ X + l2 * np.eye(len(w))
        step = np.linalg.solve(H, g)
        w -= step
        if np.max(np.abs(step)) < 1e-9:
            break
    p = 1 / (1 + np.exp(-X @ w))
    cov = np.linalg.inv((X * (p * (1 - p))[:, None]).T @ X + l2 * np.eye(len(w)))
    return w, np.sqrt(np.diag(cov))


def auc(scores, labels):
    scores, labels = np.asarray(scores), np.asarray(labels).astype(bool)
    n_pos, n_neg = int(labels.sum()), int((~labels).sum())
    u = rankdata(scores)[labels].sum() - n_pos * (n_pos + 1) / 2.0
    return float(u / (n_pos * n_neg))


def icc1(rows):
    """One-way random-effects ICC(1) for a single reviewer, papers with >=3 reviews."""
    groups = [r["ratings"] for r in rows if len(r["ratings"]) >= 3]
    k = np.mean([len(g) for g in groups])
    grand = np.mean([v for g in groups for v in g])
    n = len(groups)
    ssb = sum(len(g) * (np.mean(g) - grand) ** 2 for g in groups)
    ssw = sum(sum((v - np.mean(g)) ** 2 for v in g) for g in groups)
    dfb, dfw = n - 1, sum(len(g) for g in groups) - n
    msb, msw = ssb / dfb, ssw / dfw
    single = (msb - msw) / (msb + (k - 1) * msw)
    avg_k = (msb - msw) / msb
    return {"papers": n, "mean_reviews": float(k), "icc_single_reviewer": float(single), "icc_mean_of_k": float(avg_k)}


def split_half_accept_agreement(rows, rng, accept_rate):
    """Simulated re-review: rank papers by two disjoint reviewer subsets, compare top-k sets."""
    pool = [r for r in rows if len(r["ratings"]) >= 4]
    a, b = [], []
    for r in pool:
        perm = rng.permutation(r["ratings"])
        h = len(perm) // 2
        a.append(np.mean(perm[:h]))
        b.append(np.mean(perm[h:2 * h]))
    a, b = np.asarray(a) + rng.uniform(0, 1e-6, len(a)), np.asarray(b) + rng.uniform(0, 1e-6, len(b))
    k = int(round(accept_rate * len(pool)))
    top_a, top_b = set(np.argsort(-a)[:k]), set(np.argsort(-b)[:k])
    return {"papers_with_4plus_reviews": len(pool), "top_k": k, "overlap_of_accept_sets": len(top_a & top_b) / k}


def year_report(year, rows, rng):
    rep = {"n_scored_main_track": len(rows)}
    decided = [r for r in rows if r["decided"]]
    rep["n_decided"] = len(decided)
    rep["n_withdrawn_with_scores"] = sum(r["withdrawn"] for r in rows)
    rep["accept_rate_among_decided"] = float(np.mean([r["accepted"] for r in decided]))
    rep["mean_rating"] = {
        "accepted": float(np.mean([r["mean_rating"] for r in rows if r["accepted"]])),
        "rejected": float(np.mean([r["mean_rating"] for r in rows if r["status"] in REJECTED])),
        "withdrawn": float(np.mean([r["mean_rating"] for r in rows if r["withdrawn"]])),
    }
    # Score -> decision: how deterministic is acceptance given the mean rating?
    y = np.array([r["accepted"] for r in decided], float)
    s = np.array([r["mean_rating"] for r in decided])
    rep["auc_mean_rating_for_acceptance"] = auc(s, y)
    bins = np.quantile(s, np.linspace(0, 1, 11))
    rep["accept_rate_by_rating_decile"] = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (s >= lo) & (s <= hi)
        rep["accept_rate_by_rating_decile"].append({"lo": float(lo), "hi": float(hi), "n": int(m.sum()), "accept_rate": float(y[m].mean())})
    # Lexical / length features: score difference, area-stratified difference, effect beyond score on acceptance.
    rep["features"] = {}
    zr = zscore_within_year([r["mean_rating"] for r in rows])
    for r, z in zip(rows, zr):
        r["z_rating"] = float(z)
    zdec = np.array([r["z_rating"] for r in decided])
    for feat in list(FEATURES) + list(EXPLORATORY) + [f"title_{k}" for k in TITLE_FEATURES]:
        yes = [r["z_rating"] for r in rows if r[feat]]
        no = [r["z_rating"] for r in rows if not r[feat]]
        if len(yes) < 2 or len(no) < 2:
            rep["features"][feat] = {"prevalence": len(yes) / len(rows), "z_rating_diff": None, "note": "too few papers in one group"}
            continue
        X = np.column_stack([zdec, np.array([r[feat] for r in decided], float)])
        w, se = logistic_fit(X, y)
        rep["features"][feat] = {
            "prevalence": float(np.mean([r[feat] for r in rows])),
            "z_rating_diff": float(np.mean(yes) - np.mean(no)),
            "z_rating_diff_ci95": bootstrap_diff(yes, no, rng),
            "z_rating_diff_area_stratified": area_stratified_diff(rows, feat, "z_rating") if any(r["area"] for r in rows) else None,
            "accept_logodds_beyond_rating": float(w[2]),
            "accept_logodds_beyond_rating_se": float(se[2]),
        }
    for cont in ["abstract_words", "pdf_mb", "title_words"]:
        pairs = [(r[cont], r["mean_rating"]) for r in rows if r[cont] is not None]
        if len(pairs) < 30:
            rep["features"][cont] = {"n": len(pairs), "spearman_with_rating": None, "p": None}
            continue
        rho, p = spearman([a for a, _ in pairs], [b for _, b in pairs])
        rep["features"][cont] = {"n": len(pairs), "spearman_with_rating": rho, "p": p}
    # Sub-scores: which component tracks the overall rating (same reviewers -> halo; descriptive only).
    subs = SUBSCORES[year]
    full = [r for r in rows if all(r[sname] is not None for sname in subs)]
    Z = np.column_stack([zscore_within_year([r[sname] for r in full]) for sname in subs])
    yy = zscore_within_year([r["mean_rating"] for r in full])
    beta, *_ = np.linalg.lstsq(np.column_stack([np.ones(len(full)), Z]), yy, rcond=None)
    rep["subscore_std_coefficients_on_rating"] = {sname: float(b) for sname, b in zip(subs, beta[1:])}
    rep["subscore_spearman_with_rating"] = {sname: spearman(Z[:, i], yy)[0] for i, sname in enumerate(subs)}
    # Areas
    if any(r["area"] for r in rows):
        areas = {}
        for r in rows:
            areas.setdefault(r["area"], []).append(r)
        rep["areas"] = sorted(
            [
                {
                    "area": a,
                    "n": len(g),
                    "mean_rating": float(np.mean([r["mean_rating"] for r in g])),
                    "accept_rate_among_decided": float(np.mean([r["accepted"] for r in g if r["decided"]])) if any(r["decided"] for r in g) else None,
                }
                for a, g in areas.items()
                if len(g) >= 50
            ],
            key=lambda d: -d["mean_rating"],   )
        kw_h, kw_p = kruskal_h([[r["mean_rating"] for r in g] for g in areas.values() if len(g) >= 50])
        rep["area_kruskal_h"] = float(kw_h)
        rep["area_kruskal_p_approx"] = float(kw_p)
    # Noise
    rep["reviewer_agreement"] = icc1(rows)
    rep["simulated_rereview"] = split_half_accept_agreement(decided, rng, rep["accept_rate_among_decided"])
    # Score vs later citations (post-hoc; confounded by acceptance itself and by snapshot age)
    if any(r["citations"] is not None for r in rows):
        rep["citations"] = {}
        for label, grp in [("accepted", [r for r in decided if r["accepted"]]), ("rejected", [r for r in decided if not r["accepted"]]), ("all_decided", decided)]:
            pairs = [(r["mean_rating"], math.log1p(r["citations"])) for r in grp if r["citations"] is not None]
            rho, p = spearman([a for a, _ in pairs], [b for _, b in pairs])
            rep["citations"][label] = {"n": len(pairs), "spearman_rating_vs_log_citations": rho, "p": p}
    return rep


def iclr2027_report(papers_path, rng):
    """Initial (pre-rebuttal) ICLR 2027 scores: titles + ratings only, no decisions yet."""
    p = json.loads(Path(papers_path).read_text())
    cols = p["columns"]
    rows = []
    for raw in p["rows"]:
        rec = dict(zip(cols, raw))
        ratings = [float(v) for v in rec.get("ratings") or []]
        if not ratings:
            continue
        row = {"ratings": ratings, "mean_rating": float(np.mean(ratings))}
        for name, pat in TITLE_FEATURES.items():
            row[f"title_{name}"] = bool(re.search(pat, rec.get("title") or "", flags=re.I))
        rows.append(row)
    z = zscore_within_year([r["mean_rating"] for r in rows])
    rep = {"n_papers_with_ratings": len(rows), "mean_of_paper_means": float(np.mean([r["mean_rating"] for r in rows]))}
    rep["title_features"] = {}
    for name in TITLE_FEATURES:
        yes = [zz for r, zz in zip(rows, z) if r[f"title_{name}"]]
        no = [zz for r, zz in zip(rows, z) if not r[f"title_{name}"]]
        if len(yes) < 2 or len(no) < 2:
            rep["title_features"][name] = {"prevalence": len(yes) / len(rows), "z_rating_diff": None, "note": "too few papers in one group"}
            continue
        rep["title_features"][name] = {
            "prevalence": len(yes) / len(rows),
            "z_rating_diff": float(np.mean(yes) - np.mean(no)),
            "z_rating_diff_ci95": bootstrap_diff(yes, no, rng),
        }
    rep["reviewer_agreement"] = icc1(rows)
    rep["simulated_rereview_top30pct"] = split_half_accept_agreement(rows, rng, 0.30)
    return rep


def design_matrix(rows, areas):
    cols = []
    for r in rows:
        v = [float(r[f]) for f in FEATURES]
        v += [math.log1p(r["abstract_words"]), math.log1p(r["title_words"])]
        v += [1.0 if r["area"] == a else 0.0 for a in areas]
        cols.append(v)
    X = np.asarray(cols)
    return X


def temporal_holdout(train_rows, test_rows):
    """Fit on one year, test on a later year; pre-review paper-side features only."""
    areas = sorted({r["area"] for r in train_rows} & {r["area"] for r in test_rows})[1:]
    def label(rows):
        z = np.array([r["mean_rating"] for r in rows])
        return (z >= np.quantile(z, 0.75)).astype(float)
    Xtr, Xte = design_matrix(train_rows, areas), design_matrix(test_rows, areas)
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    w, _ = logistic_fit((Xtr - mu) / sd, label(train_rows), l2=1.0)
    pte = np.column_stack([np.ones(len(Xte)), (Xte - mu) / sd]) @ w
    ptr = np.column_stack([np.ones(len(Xtr)), (Xtr - mu) / sd]) @ w
    names = list(FEATURES) + ["log_abstract_words", "log_title_words"] + [f"area={a}" for a in areas]
    return {
        "label": "top-quartile mean rating within year",
        "train_auc": auc(ptr, label(train_rows)),
        "test_auc": auc(pte, label(test_rows)),
        "n_train": len(train_rows),
        "n_test": len(test_rows),
        "std_coefficients_non_area": {n: float(c) for n, c in zip(names, w[1:]) if not n.startswith("area=")},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--iclr2027-papers", help="shiqiangw/iclr-2027-scores docs/data/papers.json")
    args = ap.parse_args()
    rng = np.random.default_rng(SEED)
    out = {"seed": SEED, "features_regex": FEATURES, "inputs_sha256": {}, "years": {}}
    data = {}
    for year in (2023, 2024, 2026):
        p = Path(args.data_dir) / f"iclr{year}.json"
        out["inputs_sha256"][str(year)] = hashlib.sha256(p.read_bytes()).hexdigest()
        data[year] = load_year(args.data_dir, year)
        out["years"][str(year)] = year_report(year, data[year], rng)
    out["temporal_holdout_2024_to_2026"] = temporal_holdout(data[2024], data[2026])
    if args.iclr2027_papers:
        out["inputs_sha256"]["2027_initial"] = hashlib.sha256(Path(args.iclr2027_papers).read_bytes()).hexdigest()
        out["iclr2027_initial"] = iclr2027_report(args.iclr2027_papers, rng)
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False, allow_nan=False))
    print(json.dumps({k: v for k, v in out.items() if k != "years"}, indent=1)[:3000])


if __name__ == "__main__":
    main()
