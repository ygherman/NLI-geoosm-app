# GeoOSM – Place Authority Geocoder

A Streamlit app for enriching library place name authority records with coordinates and OpenStreetMap identifiers.

Developed at the **National Library of Israel** for geocoding MARC authority records exported from Alma/Ex Libris.

---

## What it does

Given an Excel file of place name authority records, the app:

1. Extracts place names and Wikidata identifiers from MARC fields 151, 024, and 451.
2. Detects the country automatically from the parenthetical qualifier in field 151 (e.g. `(Israel)` → `countrycode=il`, `(France)` → `countrycode=fr`).
3. Geocodes each record via the [Nominatim OpenStreetMap API](https://nominatim.org/), using Wikidata as a high-confidence shortcut where available.
4. Presents each result on an embedded map for human review — confirm, reject, or re-search.
5. Exports a spreadsheet with MARC 034 bounding-box coordinates and OSM metadata ready for import.

---

## Input format

The app expects an Excel file (`.xlsx` or `.xls`) with the following columns.
Column names are matched by prefix, so extra text after the field number is fine.

| Column | Required | Description |
|---|---|---|
| `MMS ID` | Yes | Authority record identifier from the ILS |
| `151` | Yes | MARC 151 field — authorized name of the place, in MARC subfield notation (e.g. `$$aRekem (Extinct city) (Israel)$$9lat`) |
| `024` | No | MARC 024 field — Wikidata Q-ID if present (e.g. `$$aQ193473$$2wikidata`) |
| `451` | No | MARC 451 variant name fields — one or more columns, all languages |

### Example 151 cell

```
$$aRekem (Extinct city) (Israel)$$9lat$$aרקם$$9heb$$aRaqim$$9lat
```

The app extracts:
- Latin authorized name: `Rekem`
- Country code: `il` (from `(Israel)`)
- All name variants for searching: `Rekem`, `רקם`, `Raqim`

---

## Geocoding process

### Two-phase approach

To avoid long blocking waits, geocoding is split into two phases:

**Phase 1 — Pre-scan (instant)**
- Annotates every record with names, country code, and Wikidata Q-ID.
- Batch-queries Wikidata SPARQL for OSM relation IDs (property P402).
- Checks the local cache for every record.
- Records with cache hits are resolved immediately — no Nominatim calls needed.

**Phase 2 — Batch processing (user-triggered)**
- Uncached records are queued and processed in user-selected batch sizes (10–200).
- Each batch takes approximately `batch_size × 1.1` seconds (Nominatim rate limit).
- A live progress bar shows the current record name and estimated time remaining.
- The user can review results between batches before requesting the next one.

---

### Path A — Records with a Wikidata ID (field 024)

```
024 field → Wikidata Q-ID → SPARQL query → OSM relation ID (P402) → Nominatim /lookup
```

1. **Wikidata SPARQL** — batch-queries the Wikidata endpoint for OSM relation IDs linked via property P402.
2. **Nominatim `/lookup`** — fetches the full OSM record directly by relation ID. This is an authoritative match (score = **1.00**, always cached).
3. **Fallback** — if the Wikidata item has no P402, falls through to Path B name search.

---

### Path B — Records without a Wikidata ID (or no P402)

```
151 / 451 fields → name variants → country detection → Nominatim /search → scored result
```

1. **Name extraction** — every `$$a` subfield from fields 151 and 451 is extracted across all languages (Latin, Hebrew, Arabic, …). Trailing qualifiers are stripped before searching: `"Nimrin (Extinct city) (Israel)"` → `"Nimrin"`.
2. **Country detection** — the last parenthetical in the 151 `$$a` value is matched against a built-in table of ~240 country name forms (English and Hebrew) to produce an ISO 3166-1 alpha-2 code. A fallback country code can be set manually in the sidebar.
3. **Nominatim `/search`** — each name variant is searched; the highest-scoring result across all variants is kept.

---

### Match scoring (Path B)

```
score = name_match × 0.5  +  type_score × 0.3  +  importance × 0.2
```

| Signal | Weight | How it is computed |
|---|---|---|
| `name_match` | 50 % | 1.0 if the queried name appears verbatim in any OSM name field (`name:he`, `name:en`, `name:ar` …); otherwise token-overlap ratio against `display_name`. |
| `type_score` | 30 % | 1.0 for `place` / `boundary` / `historic` / `natural` / `waterway` · 0.7 for `landuse` / `leisure` · 0.1 for `amenity` / `building` / `highway` / `shop` / `railway`. |
| `importance` | 20 % | Nominatim's own 0–1 float reflecting the place's global significance in OSM. |

Results with **score ≥ confidence threshold** (default 0.65, adjustable in the sidebar) are cached and marked *confirmed*. Results below the threshold are marked *uncertain* and are **not cached**, so re-running the app will retry them with a potentially improved query.

---

## Review workflow

The **🔍 Review** tab displays all geocoded records. For each record:

- An embedded map shows the matched location with a colour-coded marker:
  - 🟢 Green — confirmed match
  - 🟡 Orange — uncertain (below threshold)
  - 🔴 Red — no match
- **✅ Confirm** — marks the record as accepted and removes it from the pending dropdown.
- **❌ Reject** — marks the record as rejected and removes it from the pending dropdown.
- **🔍 Re-search** — opens a search panel to query Nominatim manually with a custom name and country, and pick from the top 5 candidates. Selecting a candidate marks the record confirmed.

Filters let you narrow the view by match quality (Confirmed / Uncertain / No match) and review status (Pending / Confirmed / Rejected). A text search filters by any name field.

---

## Source values

| Source | Icon | Match quality | Meaning |
|---|---|---|---|
| `A-lookup` | 🟢 | Authoritative | Wikidata P402 → Nominatim lookup by OSM relation ID. Score always 1.00. |
| `A-fallback-confirmed` | 🟢 | High confidence | Wikidata ID present but no P402; name search used instead. Score ≥ threshold. |
| `A-fallback-uncertain` | 🟡 | Low confidence | Wikidata ID present but no P402; name search returned a result below the threshold. |
| `A-fallback-no-match` | 🔴 | No match | Wikidata ID present but no P402, and Nominatim returned nothing. |
| `B-confirmed` | 🟢 | High confidence | No Wikidata ID; name search found a result above the threshold. |
| `B-uncertain` | 🟡 | Low confidence | No Wikidata ID; name search returned a result below the threshold. |
| `B-no-match` | 🔴 | No match | No Wikidata ID and Nominatim returned nothing for any name variant. |
| `manual-override` | ✏️ | Manually confirmed | The reviewer selected a specific match from the Re-search panel. |

---

## Output

Click **⬇ Download results (.xlsx)** in the sidebar at any time. The spreadsheet contains all geocoded records with the following columns:

| Column | Description |
|---|---|
| `MMSID` | Original MMS ID from the authority record |
| `wikidata_id` | Wikidata Q-ID extracted from field 024 |
| `name_lat` | Latin-script authorized name from field 151 (`$$9lat`) |
| `country_code` | ISO 3166-1 alpha-2 code detected from the 151 parenthetical |
| `osm_type` | OSM element type: `relation`, `way`, or `node` |
| `osm_id` | Numeric OSM identifier |
| `lat` / `lon` | Centroid coordinates of the matched OSM element |
| `34$d` | MARC 034 subfield d — west longitude of bounding box |
| `34$e` | MARC 034 subfield e — east longitude of bounding box |
| `34$f` | MARC 034 subfield f — north latitude of bounding box |
| `34$g` | MARC 034 subfield g — south latitude of bounding box |
| `osm_class` | OSM class (e.g. `place`, `boundary`, `historic`, `natural`) |
| `osm_place_type` | OSM type (e.g. `village`, `city`, `archaeological_site`, `peak`) |
| `wikidata_osm` | Wikidata Q-ID stored on the OSM entity — used for cross-validation |
| `wikipedia` | Wikipedia link stored on the OSM entity (e.g. `en:Tira, Israel`) |
| `name_he` | Hebrew name from OSM namedetails |
| `name_en` | English name from OSM namedetails |
| `name_ar` | Arabic name from OSM namedetails |
| `alt_name` | Alternative name from OSM namedetails |
| `match_name` | The specific name variant that produced the winning Nominatim result |
| `match_score` | Composite score 0–1 |
| `source` | Geocoding path and confidence level (see Source values table above) |
| `review_status` | Reviewer decision: `confirmed`, `rejected`, or `pending` |

---

## Cache

Confirmed API responses are stored in `nominatim_cache.json` in the parent directory. On the next run, every cached record is resolved instantly with no Nominatim calls.

The cache is shared with the standalone `geocode_israel.py` script in the parent directory, so results from either tool carry over.

To clear the cache, click **Clear cache** in the sidebar.

---

## Running locally

### Requirements

Python 3.9 or later.

```bash
pip install streamlit pandas openpyxl xlrd requests urllib3
```

Or with [uv](https://github.com/astral-sh/uv):

```bash
uv pip install streamlit pandas openpyxl xlrd requests urllib3
```

### Start the app

From the `dgeo-osm-app` directory (or its parent):

```bash
python -m streamlit run dgeo-osm-app/app.py
```

### Network note

This version uses `verify=False` on all outbound HTTPS requests to work through an institutional proxy that intercepts TLS with a self-signed certificate (NLI corporate network). If your network does not require this, use the `dgeo-osm-cloud` version instead, which has standard SSL verification enabled.

---

## Files

| File | Description |
|---|---|
| `app.py` | Main Streamlit application |
| `geocoder.py` | Nominatim and Wikidata geocoding logic |
| `country_codes.py` | Country name → ISO 3166-1 alpha-2 mapping (~240 entries, English and Hebrew) |
| `requirements.txt` | Python dependencies |

---

## Rate limits

Nominatim's [usage policy](https://operations.osmfoundation.org/policies/nominatim/) requires a maximum of **1 request per second**. The app enforces a 1.1-second delay between calls. Wikidata SPARQL is queried in batches of 50 Q-IDs to minimize requests.

---

## Related

- [`dgeo-osm-cloud/`](../dgeo-osm-cloud/) — cloud-ready version for deployment on [Streamlit Community Cloud](https://share.streamlit.io): uses `st.file_uploader` instead of a local file path, standard SSL verification, and cache upload/download instead of a local JSON file.
- [`geocode_israel.py`](../geocode_israel.py) — original standalone Python script (no UI) for batch processing.
