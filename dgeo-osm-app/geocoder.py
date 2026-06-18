from __future__ import annotations

"""
geocoder.py – geocoding helpers for the Streamlit app.

Cloud version: SSL verification is enabled (no corporate proxy).
"""

import re
import time
import logging
import requests

log = logging.getLogger(__name__)

NOMINATIM_BASE  = "https://nominatim.openstreetmap.org"
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT      = "NLI-GeoOSM-App/1.0 (yael.vardinagherman@nli.org.il)"

NOMINATIM_DELAY = 1.1
SPARQL_DELAY    = 1.0
SPARQL_BATCH    = 50

TYPE_SCORES = {
    "boundary": 1.0, "place": 1.0, "historic": 1.0,
    "natural":  0.9, "waterway": 0.9,
    "landuse":  0.7, "leisure": 0.7,
    "amenity":  0.1, "building": 0.1,
    "highway":  0.1, "shop": 0.1, "railway": 0.1,
}

# ── Parsing helpers ────────────────────────────────────────────────────────────

def find_col(df, prefix: str):
    for c in df.columns:
        if c.strip().startswith(prefix):
            return c
    return None


def extract_qid(cell) -> str | None:
    if not isinstance(cell, str):
        return None
    m = re.search(r'\$\$a(Q\d+)\$\$2wikidata', cell)
    return m.group(1) if m else None


def extract_latin_name(cell) -> str | None:
    if not isinstance(cell, str):
        return None
    m = re.search(r'\$\$a(.+?)\$\$9lat', cell)
    return m.group(1).strip() if m else None


def clean_name(name: str) -> str:
    return re.sub(r'(\s*\([^)]+\))+\s*$', '', name).strip()


def extract_all_names(cell) -> list[str]:
    if not isinstance(cell, str):
        return []
    raw = re.findall(r'\$\$a(.+?)\$\$9\w+', cell)
    return [clean_name(v.strip()) for v in raw if v.strip()]


def collect_all_names(row, col_151, cols_451) -> list[str]:
    import pandas as pd
    names = []
    for col in [col_151] + cols_451:
        if col and pd.notna(row.get(col)):
            names.extend(extract_all_names(str(row[col])))
    seen, unique = set(), []
    for n in names:
        key = n.lower()
        if key not in seen and n:
            seen.add(key)
            unique.append(n)
    return unique

# ── Empty result ───────────────────────────────────────────────────────────────

def _empty_result() -> dict:
    return {
        "osm_type": None, "osm_id": None,
        "lat": None, "lon": None,
        "d": None, "e": None, "f": None, "g": None,
        "importance": 0.0, "score": 0.0,
        "osm_class": None, "osm_place_type": None,
        "wikidata_osm": None, "wikipedia": None,
        "name_he": None, "name_en": None, "name_ar": None, "alt_name": None,
        "display_name": None,
    }

# ── Scoring ────────────────────────────────────────────────────────────────────

def _name_match(query: str, item: dict) -> float:
    query_lower = query.lower()
    namedetails = item.get("namedetails") or {}
    candidates = list(namedetails.values()) + [item.get("display_name", "")]
    for val in candidates:
        if query_lower in str(val).lower():
            return 1.0
    q_tok = set(re.findall(r"\w+", query_lower))
    d_tok = set(re.findall(r"\w+", item.get("display_name", "").lower()))
    return len(q_tok & d_tok) / len(q_tok) if q_tok else 0.0


def score_match(query_name: str, item: dict) -> float:
    if not item or not item.get("osm_id"):
        return 0.0
    name_sim   = _name_match(query_name, item)
    type_score = TYPE_SCORES.get(item.get("class", ""), 0.3)
    importance = min(float(item.get("importance") or 0), 1.0)
    return name_sim * 0.5 + type_score * 0.3 + importance * 0.2

# ── Nominatim internals ────────────────────────────────────────────────────────

def _parse_item(item: dict, score: float) -> dict:
    bb = item.get("boundingbox")
    d = e = f = g = None
    if bb:
        min_lat, max_lat, min_lon, max_lon = map(float, bb)
        d, e, f, g = float(min_lon), float(max_lon), float(max_lat), float(min_lat)

    namedetails = item.get("namedetails") or {}
    extratags   = item.get("extratags")   or {}

    return {
        "osm_type":       item.get("osm_type"),
        "osm_id":         str(item.get("osm_id", "")),
        "lat":            float(item["lat"])  if item.get("lat") else None,
        "lon":            float(item["lon"])  if item.get("lon") else None,
        "d": d, "e": e, "f": f, "g": g,
        "importance":     min(float(item.get("importance") or 0), 1.0),
        "score":          round(score, 4),
        "osm_class":      item.get("class"),
        "osm_place_type": item.get("type"),
        "wikidata_osm":   extratags.get("wikidata"),
        "wikipedia":      extratags.get("wikipedia"),
        "name_he":        namedetails.get("name:he"),
        "name_en":        namedetails.get("name:en") or namedetails.get("name:en-US"),
        "name_ar":        namedetails.get("name:ar"),
        "alt_name":       namedetails.get("alt_name"),
        "display_name":   item.get("display_name"),
    }


def _nominatim_get(url: str, params: dict) -> list:
    for attempt in range(1, 5):
        try:
            r = requests.get(
                url, params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=10,
            )
            if r.status_code in (429, 502, 503, 504):
                time.sleep(10 * attempt)
                continue
            r.raise_for_status()
            time.sleep(NOMINATIM_DELAY)
            return r.json()
        except Exception as exc:
            log.warning(f"Nominatim attempt {attempt} failed: {exc}")
            time.sleep(5 * attempt)
    return []


def _cache_read(cache: dict, key: str) -> dict | None:
    if key not in cache:
        return None
    v = cache[key]
    if v is None:
        return _empty_result()
    if isinstance(v, dict):
        return v
    del cache[key]
    return None

# ── Public Nominatim functions ─────────────────────────────────────────────────

def nominatim_lookup(cache: dict, osm_relation_id: str) -> dict:
    """Path A — look up by OSM relation ID. Always cached (score = 1.0)."""
    key = f"lookup:R{osm_relation_id}"
    cached = _cache_read(cache, key)
    if cached is not None:
        return cached

    data = _nominatim_get(
        f"{NOMINATIM_BASE}/lookup",
        {"osm_ids": f"R{osm_relation_id}", "format": "json",
         "namedetails": 1, "extratags": 1},
    )
    if not data:
        return _empty_result()
    result = _parse_item(data[0], score=1.0)
    cache[key] = result
    return result


def nominatim_search(
    cache: dict,
    name: str,
    country_code: str | None,
    match_threshold: float = 0.65,
) -> dict:
    """Search by name, optionally restricted to a country. Caches confirmed hits."""
    cc_part = f"|cc={country_code}" if country_code else ""
    key = f"search:{name.lower()}{cc_part}"
    cached = _cache_read(cache, key)
    if cached is not None:
        return cached

    params = {
        "q": name, "format": "json",
        "limit": 1, "namedetails": 1, "extratags": 1,
    }
    if country_code:
        params["countrycode"] = country_code

    data = _nominatim_get(f"{NOMINATIM_BASE}/search", params)
    if not data:
        return _empty_result()

    item   = data[0]
    score  = score_match(name, item)
    result = _parse_item(item, score)

    if score >= match_threshold:
        cache[key] = result

    return result


def nominatim_search_candidates(
    name: str,
    country_code: str | None,
    limit: int = 5,
) -> list[dict]:
    """
    Return up to `limit` scored candidates for manual review.
    Does NOT cache — intended for interactive re-search only.
    """
    params = {
        "q": name, "format": "json",
        "limit": limit, "namedetails": 1, "extratags": 1,
    }
    if country_code:
        params["countrycode"] = country_code

    data = _nominatim_get(f"{NOMINATIM_BASE}/search", params)
    results = []
    for item in data:
        score  = score_match(name, item)
        result = _parse_item(item, score)
        results.append(result)
    return sorted(results, key=lambda r: r["score"], reverse=True)


def nominatim_search_best(
    cache: dict,
    names: list[str],
    country_code: str | None,
    match_threshold: float = 0.65,
) -> tuple[dict, str | None]:
    """Try every name variant; return (best_result, winning_name)."""
    best, best_name = _empty_result(), None
    for name in names:
        try:
            result = nominatim_search(cache, name, country_code, match_threshold)
            if result["score"] > best["score"]:
                best, best_name = result, name
        except Exception as exc:
            log.warning(f"Search failed for '{name}': {exc}")
    return best, best_name

# ── Wikidata SPARQL ────────────────────────────────────────────────────────────

def fetch_osm_ids_from_wikidata(cache: dict, qids: list[str]) -> dict[str, str]:
    osm_map, uncached = {}, []
    for qid in qids:
        val = cache.get(f"wikidata:{qid}", "MISSING")
        if val != "MISSING":
            if val:
                osm_map[qid] = val
        else:
            uncached.append(qid)

    if not uncached:
        return osm_map

    for i in range(0, len(uncached), SPARQL_BATCH):
        batch  = uncached[i : i + SPARQL_BATCH]
        values = " ".join(f"wd:{q}" for q in batch)
        query  = (
            "SELECT ?item ?osmId WHERE { "
            f"VALUES ?item {{ {values} }} "
            "OPTIONAL { ?item wdt:P402 ?osmId . } "
            "}"
        )
        for attempt in range(1, 5):
            try:
                r = requests.get(
                    SPARQL_ENDPOINT,
                    params={"query": query, "format": "json"},
                    headers={"User-Agent": USER_AGENT},
                    timeout=60,
                )
                if r.status_code in (429, 502, 503, 504):
                    time.sleep(10 * attempt)
                    continue
                r.raise_for_status()
                break
            except Exception:
                time.sleep(10 * attempt)
        else:
            continue

        found = {
            row["item"]["value"].rsplit("/", 1)[-1]: row["osmId"]["value"]
            for row in r.json()["results"]["bindings"]
            if "osmId" in row
        }
        for qid in batch:
            osm_rel = found.get(qid)
            cache[f"wikidata:{qid}"] = osm_rel
            if osm_rel:
                osm_map[qid] = osm_rel
        time.sleep(SPARQL_DELAY)

    return osm_map

# ── Cache-hit check (no API calls) ────────────────────────────────────────────

def is_cached(row_dict: dict, cache: dict) -> bool:
    """
    Return True if every lookup needed for this row is already in the cache,
    meaning geocode_row will complete instantly without any HTTP requests.
    """
    qid          = row_dict.get("_qid")
    names        = row_dict.get("_all_names", [])
    country_code = row_dict.get("_country_code")

    # Path A: Wikidata → OSM relation → lookup
    if qid:
        wikidata_val = cache.get(f"wikidata:{qid}", "MISSING")
        if wikidata_val != "MISSING":
            osm_rel = wikidata_val
            if osm_rel and f"lookup:R{osm_rel}" in cache:
                return True
            # fallback search names below

    # Path A fallback or Path B: any name variant cached?
    for name in names:
        cc_part = f"|cc={country_code}" if country_code else ""
        if f"search:{name.lower()}{cc_part}" in cache:
            return True

    return False

# ── Per-row geocoding ──────────────────────────────────────────────────────────

def _search_source(prefix: str, osm_id, score: float, threshold: float) -> str:
    if not osm_id:
        return f"{prefix}-no-match"
    return f"{prefix}-confirmed" if score >= threshold else f"{prefix}-uncertain"


def geocode_row(
    row: dict,
    qid_to_osm: dict,
    cache: dict,
    match_threshold: float = 0.65,
) -> tuple[dict, str | None, str]:
    """Returns (result_dict, match_name, source)."""
    qid          = row.get("_qid")
    names        = row.get("_all_names", [])
    country_code = row.get("_country_code")

    # Path A – Wikidata → OSM relation → Nominatim lookup
    if qid:
        osm_rel = qid_to_osm.get(qid)
        if osm_rel:
            try:
                result = nominatim_lookup(cache, osm_rel)
                if result["osm_id"]:
                    return result, f"R{osm_rel}", "A-lookup"
            except Exception as exc:
                log.warning(f"Lookup failed for {qid} (R{osm_rel}): {exc}")
        if names:
            result, win = nominatim_search_best(cache, names, country_code, match_threshold)
            return result, win, _search_source("A-fallback", result["osm_id"], result["score"], match_threshold)
        return _empty_result(), None, "A-no-name"

    # Path B – name search only
    if names:
        result, win = nominatim_search_best(cache, names, country_code, match_threshold)
        return result, win, _search_source("B", result["osm_id"], result["score"], match_threshold)

    return _empty_result(), None, "B-no-name"


def build_output_row(row: dict, geocode_result: tuple) -> dict:
    result, match_name, source = geocode_result
    return {
        "MMSID":          row.get("MMSID"),
        "wikidata_id":    row.get("_qid"),
        "name_lat":       row.get("_name_lat"),
        "country_code":   row.get("_country_code"),
        "osm_type":       result["osm_type"],
        "osm_id":         result["osm_id"],
        "lat":            result["lat"],
        "lon":            result["lon"],
        "34$d":           result["d"],
        "34$e":           result["e"],
        "34$f":           result["f"],
        "34$g":           result["g"],
        "osm_class":      result["osm_class"],
        "osm_place_type": result["osm_place_type"],
        "wikidata_osm":   result["wikidata_osm"],
        "wikipedia":      result["wikipedia"],
        "name_he":        result["name_he"],
        "name_en":        result["name_en"],
        "name_ar":        result["name_ar"],
        "alt_name":       result["alt_name"],
        "display_name":   result["display_name"],
        "match_name":     match_name,
        "match_score":    result["score"] or None,
        "source":         source,
    }
