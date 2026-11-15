"""Artificial Analysis enrichment for canonical Creator/Model identities.

Responsibility: fetch the Artificial Analysis model dataset, cache the public
dataset within an explicit resource budget, and project benchmark scores and
measured speed (Performance v3) onto the caller's canonical Models.  A Model is
``fast`` when its measured output speed exceeds the median of every model the
same snapshot measured; ``routing_speed_index`` serves that verdict to the
dispatcher.  Provider, Offering, Deployment, alias, and route state are
deliberately outside this module.

The caller supplies the effective owner credential.  This module never reads
or persists plaintext credentials and never returns one.  Ordinary reads are
non-blocking: stale, missing or older-schema data schedules one bounded
background refresh; only explicit user refresh/save actions fetch inline.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from lib.log import get_logger
from lib.runtime.runtime_paths import data_root

from ._score_matching import match_model, normalize_name


logger = get_logger(__name__)

# The free-shape endpoint accepts Free, Pro, and Commercial keys and contains
# every headline index used here.  The richer sibling would reject Free keys
# while adding fields this model-only projection deliberately does not consume.
AA_API_URL = "https://artificialanalysis.ai/api/v2/language/models/free"
AA_SOURCE_LABEL = "Artificial Analysis"
AA_SOURCE_URL = "https://artificialanalysis.ai/"

_TTL_ENV = "TOFU_AA_CACHE_TTL_SECONDS"
_DEFAULT_TTL_SECONDS = 24 * 60 * 60
# Row schema this module writes. 2 added the Performance v3 means: a dataset
# of an older schema lacks fields current readers need (a 2026-11-11 cache
# without them disabled every speed verdict), so it is never fresh.
_CACHE_SCHEMA = 2
_FETCH_TIMEOUT_SECONDS = 8.0
_BACKGROUND_MIN_INTERVAL_SECONDS = 60.0
_MAX_DATASET_ROWS = 4096
_MAX_DATASET_PAGES = 24

# Performance v3 headline means served by the free-shape endpoint: the
# average over AA's 1k/10k/100k-token and multi-turn workloads, measured on
# the model's representative endpoint (usually the creator's own API).
_PERFORMANCE_FIELDS = (
    ("output_tokens_per_second", "mean_output_tokens_per_second"),
    ("time_to_first_token_seconds", "mean_time_to_first_token_seconds"),
    ("time_to_first_answer_token_seconds", "mean_time_to_first_answer_token_seconds"),
)

_lock = threading.Lock()
_memo: dict[str, Any] | None = None
_background: dict[str, Any] = {
    "running": False,
    "last_attempt": 0.0,
    "thread": None,
}
# Exact cache of the parsed disk dataset, invalidated by file identity: every
# write replaces the file, so a refresh made by the other server process is
# seen on the next read without re-reading an unchanged file per selection.
_disk_memo: tuple[tuple[int, int, int], dict[str, Any]] | None = None
_routing_memo: tuple[list[dict[str, Any]], RoutingSpeedIndex] | None = None


def _cache_path() -> str:
    return os.path.join(data_root(), "aa_index", "models.json")


def _ttl_seconds() -> int:
    try:
        return max(60, int(os.environ.get(_TTL_ENV, "") or _DEFAULT_TTL_SECONDS))
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def _index_version(value: Any) -> int | float | None:
    """AA's ``intelligence_index_version`` (major.minor, e.g. ``5``) or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _measurement(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _parse_dataset(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("Artificial Analysis payload is missing its data array")
    parsed: list[dict[str, Any]] = []
    for raw in rows[:_MAX_DATASET_ROWS]:
        if not isinstance(raw, Mapping):
            continue
        evaluations = raw.get("evaluations")
        if not isinstance(evaluations, Mapping):
            continue

        def number(field: str) -> float | None:
            value = evaluations.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            value = float(value)
            return value if math.isfinite(value) and value >= 0 else None

        performance = raw.get("performance_v3")
        if not isinstance(performance, Mapping):
            performance = {}
        name = str(raw.get("name") or "").strip()
        slug = str(raw.get("slug") or "").strip()
        raw_creator = raw.get("model_creator")
        creator_name = str(
            raw_creator.get("name") if isinstance(raw_creator, Mapping) else ""
        ).strip()
        keys = {key for key in (normalize_name(name), normalize_name(slug)) if key}
        if not keys:
            continue
        parsed.append({
            "aa_name": name,
            "aa_slug": slug,
            "aa_creator": creator_name,
            "intelligence": number("artificial_analysis_intelligence_index"),
            # AA retired its Coding, Agentic and Math indices (the API keeps
            # Coding/Agentic as always-null fields); they remain for cached
            # legacy rows only.
            "coding": number("artificial_analysis_coding_index"),
            "agentic": number("artificial_analysis_agentic_index"),
            "math": number("artificial_analysis_math_index"),
            **{
                field: _measurement(performance.get(source))
                for field, source in _PERFORMANCE_FIELDS
            },
            "_keys": keys,
            "_creator_keys": {
                key for key in (normalize_name(creator_name),) if key
            },
        })
    return parsed


def _fetch_dataset(api_key: str, fetcher: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Fetch every AA row plus the Intelligence Index version its scores use.

    AA serves scores for its current index version only: after a major bump
    every model not yet re-benchmarked reports null, so the version is what
    explains a sudden drop in scored models.
    """
    if not api_key:
        raise PermissionError("Artificial Analysis API key is not configured")
    if fetcher is None:
        from lib.http_transport.client import http_get

        fetcher = http_get
    models: list[dict[str, Any]] = []
    index_version: int | float | None = None
    page = 1
    while page <= _MAX_DATASET_PAGES and len(models) < _MAX_DATASET_ROWS:
        url = AA_API_URL if page == 1 else f"{AA_API_URL}?page={page}"
        response = fetcher(
            url,
            timeout=_FETCH_TIMEOUT_SECONDS,
            headers={"x-api-key": api_key},
        )
        response.raise_for_status()
        payload = response.json()
        models.extend(_parse_dataset(payload))
        if index_version is None and isinstance(payload, Mapping):
            index_version = _index_version(payload.get("intelligence_index_version"))
        pagination = payload.get("pagination") if isinstance(payload, Mapping) else None
        if not isinstance(pagination, Mapping) or pagination.get("has_more") is not True:
            break
        reported_page = pagination.get("page")
        page = reported_page + 1 if isinstance(reported_page, int) else page + 1
    return {"models": models[:_MAX_DATASET_ROWS], "index_version": index_version}


def _read_disk_cache() -> dict[str, Any] | None:
    try:
        with open(_cache_path(), encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(cached, Mapping) or not isinstance(cached.get("models"), list):
        return None
    models: list[dict[str, Any]] = []
    for raw in cached["models"][:_MAX_DATASET_ROWS]:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        row["_keys"] = {
            key for key in (
                normalize_name(row.get("aa_name")),
                normalize_name(row.get("aa_slug")),
            ) if key
        }
        row["_creator_keys"] = {
            key for key in (normalize_name(row.get("aa_creator")),) if key
        }
        models.append(row)
    return {
        "schema": cached.get("schema"),
        "fetched_at": cached.get("fetched_at"),
        "index_version": _index_version(cached.get("index_version")),
        "models": models,
    }


def _read_disk_cache_memoized() -> dict[str, Any] | None:
    """Parse the disk dataset once per file identity; callers hold ``_lock``."""
    global _disk_memo
    try:
        status = os.stat(_cache_path())
    except OSError:
        return None
    identity = (status.st_ino, status.st_mtime_ns, status.st_size)
    if _disk_memo is not None and _disk_memo[0] == identity:
        return _disk_memo[1]
    dataset = _read_disk_cache()
    _disk_memo = (identity, dataset) if dataset is not None else None
    return dataset


def _write_disk_cache(dataset: Mapping[str, Any]) -> None:
    path = _cache_path()
    models = dataset["models"]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary_path = f"{path}.tmp"
        public_rows = [
            {
                key: value for key, value in row.items()
                if key not in {"_keys", "_creator_keys"}
            }
            for row in models[:_MAX_DATASET_ROWS]
        ]
        with open(temporary_path, "w", encoding="utf-8") as handle:
            json.dump({
                "schema": _CACHE_SCHEMA,
                "fetched_at": dataset["fetched_at"],
                "index_version": dataset.get("index_version"),
                "models": public_rows,
            }, handle)
        os.replace(temporary_path, path)
    except OSError as exc:
        logger.debug("[ModelIntelligence] AA cache write failed: %s", exc)


def _background_refresh(api_key: str, fetcher: Callable[..., Any] | None) -> None:
    global _memo
    try:
        try:
            fetched = _fetch_dataset(api_key, fetcher)
        except Exception as exc:
            logger.warning("[ModelIntelligence] AA background refresh failed: %s", exc)
            return
        dataset = {"schema": _CACHE_SCHEMA, "fetched_at": time.time(), **fetched}
        with _lock:
            _memo = dataset
        _write_disk_cache(dataset)
    finally:
        with _lock:
            _background["running"] = False


def _schedule_background_refresh(
    api_key: str,
    now: float,
    fetcher: Callable[..., Any] | None,
) -> None:
    if (
        _background["running"]
        or now - float(_background["last_attempt"]) < _BACKGROUND_MIN_INTERVAL_SECONDS
    ):
        return
    _background["running"] = True
    _background["last_attempt"] = now
    worker = threading.Thread(
        target=_background_refresh,
        args=(api_key, fetcher),
        name="aa-index-refresh",
        daemon=True,
    )
    _background["thread"] = worker
    worker.start()


def _dataset(
    api_key: str,
    *,
    force: bool = False,
    allow_fetch: bool = True,
    fetcher: Callable[..., Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return a fresh/stale/no-key/unavailable bounded dataset state."""
    global _memo
    if not api_key:
        return {"status": "no_key", "fetched_at": None, "models": []}
    current_time = time.time() if now is None else now
    with _lock:
        candidates = [row for row in (_memo, _read_disk_cache()) if row]
        freshest = max(
            candidates,
            key=lambda row: float(row.get("fetched_at") or 0),
            default=None,
        )
        if (
            freshest is not None
            and not force
            and _is_current(freshest, current_time)
        ):
            _memo = freshest
            return {"status": "ok", **freshest}
        if not allow_fetch and not force:
            _schedule_background_refresh(api_key, current_time, fetcher)
            if freshest is not None:
                return {"status": "stale", **freshest}
            return {"status": "unavailable", "fetched_at": None, "models": []}
    try:
        fetched = _fetch_dataset(api_key, fetcher)
    except Exception as exc:
        logger.warning("[ModelIntelligence] AA refresh failed: %s", exc)
        if freshest is not None:
            return {"status": "stale", **freshest}
        return {"status": "unavailable", "fetched_at": None, "models": []}
    result = {"schema": _CACHE_SCHEMA, "fetched_at": current_time, **fetched}
    with _lock:
        _memo = result
    _write_disk_cache(result)
    return {"status": "ok", **result}


def _is_current(dataset: Mapping[str, Any], now: float) -> bool:
    """Written with this module's row schema and fetched within the TTL."""
    fetched_at = _fetched_at(dataset)
    return (
        dataset.get("schema") == _CACHE_SCHEMA
        and fetched_at is not None
        and 0 <= now - fetched_at < _ttl_seconds()
    )


def refresh_when_stale(api_key: str) -> str:
    """Schedule one background refresh unless the dataset is current.

    Never fetches inline and costs nothing for a current dataset or a missing
    key, so a boot path may call it unconditionally. Returns the dataset
    status: ``ok``, ``stale``, ``unavailable`` or ``no_key``.
    """
    try:
        return str(_dataset(api_key, allow_fetch=False).get("status")
                   or "unavailable")
    except Exception as exc:
        logger.warning("[ModelIntelligence] AA refresh scheduling failed: %s", exc)
        return "unavailable"


def _score_key(model: Mapping[str, Any]) -> str:
    return f"{model.get('creator_id') or ''}::{model.get('model_id') or ''}"


def _speed_reference(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The fast bar: the median measured output speed of the whole snapshot.

    Output speed is the only Performance v3 mean independent of the reasoning
    effort a caller selects; time to first (answer) token includes reasoning
    time for models that hide their reasoning, so it never sets the tier.
    """
    speeds = [
        speed for speed in (
            _measurement(row.get("output_tokens_per_second")) for row in rows)
        if speed is not None
    ]
    if not speeds:
        return None
    return {
        "median_output_tokens_per_second": statistics.median(speeds),
        "measured_models": len(speeds),
    }


def _is_fast(row: Mapping[str, Any], reference: Mapping[str, Any] | None) -> bool:
    speed = _measurement(row.get("output_tokens_per_second"))
    return (
        reference is not None and speed is not None
        and speed > reference["median_output_tokens_per_second"]
    )


def _speed_entry(
    row: Mapping[str, Any],
    reference: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    speed = _measurement(row.get("output_tokens_per_second"))
    if speed is None:
        return None
    return {
        "output_tokens_per_second": speed,
        "time_to_first_token_seconds": _measurement(
            row.get("time_to_first_token_seconds")),
        "time_to_first_answer_token_seconds": _measurement(
            row.get("time_to_first_answer_token_seconds")),
        "fast": _is_fast(row, reference),
        "aa_name": row.get("aa_name") or "",
        "aa_slug": row.get("aa_slug") or "",
    }


def _block(
    models: Sequence[Mapping[str, Any]],
    dataset: Mapping[str, Any],
    *,
    key_source: str | None,
    key_hint: str,
) -> dict[str, Any]:
    scores: dict[str, Any] = {}
    speeds: dict[str, Any] = {}
    source_rows = dataset.get("models")
    rows = source_rows if isinstance(source_rows, list) else []
    reference = _speed_reference(rows)
    for model in models:
        # Match against every listed row, scored or not, so an unscored row
        # still makes a shared identity ambiguous instead of ceding it.
        matched = match_model(model, rows)
        if matched is None:
            continue
        # A measured speed is published evidence even for a row the current
        # index version has not scored; it never becomes a score.
        speed = _speed_entry(matched, reference)
        if speed is not None:
            speeds[_score_key(model)] = speed
        metrics = {
            metric: matched.get(metric)
            for metric in ("intelligence", "coding", "agentic", "math")
        }
        # AA lists far more models than it scores on the current index
        # version; a listed-but-unscored row is not a score.
        if all(value is None for value in metrics.values()):
            continue
        scores[_score_key(model)] = {
            **metrics,
            "aa_name": matched.get("aa_name") or "",
            "aa_slug": matched.get("aa_slug") or "",
        }
    return {
        "status": dataset.get("status", "unavailable"),
        "source": AA_SOURCE_LABEL,
        "source_url": AA_SOURCE_URL,
        "fetched_at": dataset.get("fetched_at"),
        "index_version": dataset.get("index_version"),
        "key_source": key_source,
        "key_hint": key_hint,
        "scores": scores,
        "speeds": speeds,
        "speed_reference": reference,
    }


def aa_block_for_models(
    models: Sequence[Mapping[str, Any]],
    *,
    api_key: str,
    key_source: str | None,
    key_hint: str = "",
) -> dict[str, Any]:
    """Return the non-blocking AA projection for canonical Models."""
    try:
        return _block(
            models,
            _dataset(api_key, allow_fetch=False),
            key_source=key_source,
            key_hint=key_hint,
        )
    except Exception as exc:
        logger.warning("[ModelIntelligence] AA projection failed: %s", exc)
        return _block(
            models,
            {"status": "unavailable", "fetched_at": None, "models": []},
            key_source=key_source,
            key_hint=key_hint,
        )


def refresh_scores(
    models: Sequence[Mapping[str, Any]],
    *,
    api_key: str,
    key_source: str | None,
    key_hint: str = "",
) -> dict[str, Any]:
    """Force-refresh AA scores after an explicit user action."""
    try:
        return _block(
            models,
            _dataset(api_key, force=True),
            key_source=key_source,
            key_hint=key_hint,
        )
    except Exception as exc:
        logger.warning("[ModelIntelligence] AA forced projection failed: %s", exc)
        return _block(
            models,
            {"status": "unavailable", "fetched_at": None, "models": []},
            key_source=key_source,
            key_hint=key_hint,
        )


def cached_routing_scores(models: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Read public AA evidence without credentials, refreshes, or fuzzy aliases.

    Routing never turns a base-model benchmark into evidence for a different
    variant. A stale cache is descriptive only and returns no routing scores.
    """
    with _lock:
        dataset = _memo or _read_disk_cache()
    if not dataset:
        return {}
    try:
        fetched_at = float(dataset.get('fetched_at') or 0)
    except (TypeError, ValueError, OverflowError):
        return {}
    if not 0 <= time.time() - fetched_at <= _ttl_seconds():
        return {}
    rows = dataset.get('models') or []
    scores = {}
    for model in models:
        # Do not use display labels or prefix matching at this boundary.
        identity = normalize_name(model.get('model_id'))
        exact_rows = [row for row in rows if identity in row.get('_keys', ())]
        matched = match_model({**model, 'display_name': ''}, exact_rows)
        if matched is not None:
            scores[_score_key(model)] = {
                metric: matched.get(metric)
                for metric in ('intelligence', 'coding', 'agentic')
            } | {'source': AA_SOURCE_LABEL, 'match': 'exact',
                 'fetched_at': fetched_at, 'aa_slug': matched.get('aa_slug')}
    return scores


@dataclass(frozen=True)
class RoutingSpeed:
    """AA speed evidence for one exactly identified Model."""

    output_tokens_per_second: float
    intelligence: float | None
    fast: bool


class RoutingSpeedIndex:
    """Exact-identity speed evidence keyed by normalized Model id.

    Slots carry no creator, so a normalized id that more than one AA row
    claims is ambiguous and carries no evidence: this can only withhold a
    match that creator-aware score matching would make, never invent one.
    """

    __slots__ = ("_entries",)

    def __init__(self, entries: Mapping[str, RoutingSpeed] | None = None) -> None:
        self._entries = MappingProxyType(dict(entries or {}))

    def __bool__(self) -> bool:
        return bool(self._entries)

    def evidence(self, model_id: Any) -> RoutingSpeed | None:
        return self._entries.get(normalize_name(model_id))


_EMPTY_ROUTING_INDEX = RoutingSpeedIndex()


def _fetched_at(dataset: Mapping[str, Any]) -> float | None:
    try:
        return float(dataset.get("fetched_at") or 0)
    except (TypeError, ValueError, OverflowError):
        return None


def _build_routing_index(rows: Sequence[Mapping[str, Any]]) -> RoutingSpeedIndex:
    reference = _speed_reference(rows)
    claims: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        for key in row.get("_keys") or ():
            claims[key].append(row)
    entries: dict[str, RoutingSpeed] = {}
    for key, claimed in claims.items():
        if len(claimed) != 1:
            continue
        row = claimed[0]
        speed = _measurement(row.get("output_tokens_per_second"))
        if speed is None:
            continue
        entries[key] = RoutingSpeed(
            output_tokens_per_second=speed,
            intelligence=_measurement(row.get("intelligence")),
            fast=_is_fast(row, reference),
        )
    return RoutingSpeedIndex(entries)


def routing_speed_index() -> RoutingSpeedIndex:
    """Fresh exact-identity speed evidence, without credentials or refreshes.

    Uses the freshest of this process's dataset and the shared disk cache, so
    a refresh made by the other server process is honored. A stale, missing
    or older-schema dataset yields an empty index; the dispatcher then judges
    speed from its measured route throughput (``lib.llm_dispatch.
    measured_speed``) until a refresh lands.
    """
    global _routing_memo
    with _lock:
        candidates = [row for row in (_memo, _read_disk_cache_memoized()) if row]
    dataset = max(candidates, key=lambda row: _fetched_at(row) or 0.0, default=None)
    if dataset is None or not _is_current(dataset, time.time()):
        return _EMPTY_ROUTING_INDEX
    rows = dataset.get("models") or []
    with _lock:
        memo = _routing_memo
    if memo is not None and memo[0] is rows:
        return memo[1]
    index = _build_routing_index(rows)
    with _lock:
        _routing_memo = (rows, index)
    return index


__all__ = [
    "AA_API_URL",
    "RoutingSpeed",
    "RoutingSpeedIndex",
    "aa_block_for_models",
    "cached_routing_scores",
    "refresh_scores",
    "refresh_when_stale",
    "routing_speed_index",
]
