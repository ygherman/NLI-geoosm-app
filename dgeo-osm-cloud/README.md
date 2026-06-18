# GeoOSM – Place Authority Geocoder

Streamlit app for geocoding NLI place name authority records using OpenStreetMap (Nominatim).

## Deploy on Streamlit Community Cloud

1. Push this folder to a GitHub repository (public or private).
2. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
3. Click **New app** → select your repository → set **Main file path** to `app.py`.
4. Click **Deploy**.

That's it. No environment variables or secrets are needed.

## Files

| File | Purpose |
|---|---|
| `app.py` | Main Streamlit app |
| `geocoder.py` | Nominatim + Wikidata geocoding logic |
| `country_codes.py` | Country name → ISO 3166-1 alpha-2 mapping |
| `requirements.txt` | Python dependencies |
| `.streamlit/config.toml` | Streamlit theme configuration |

## Input

Upload an Excel file (`.xlsx` or `.xls`) with these columns:

| Column | Description |
|---|---|
| MMS ID | Authority record identifier |
| 151 | MARC 151 field (authorized place name, MARC subfield format) |
| 024 | MARC 024 field containing a Wikidata Q-ID (optional) |
| 451 | MARC 451 variant name fields (optional, multiple columns allowed) |

## Saving progress between sessions

Because the cloud app has no local disk, results are saved manually:

1. After each geocoding batch, click **⬇ Save cache (.json)** in the sidebar.
2. Next session, upload the same `nominatim_cache.json` — already-geocoded records resolve instantly.
3. When finished, click **⬇ Download results (.xlsx)** for the full output spreadsheet.

## Geocoding process

**Path A** (records with a Wikidata ID in field 024):
- Queries Wikidata SPARQL for OSM relation ID (property P402).
- Fetches coordinates directly from Nominatim by relation ID — authoritative, score = 1.00.
- Falls back to name search if no P402 is found.

**Path B** (records without a Wikidata ID):
- Extracts every name variant from fields 151 and 451.
- Detects country from the last parenthetical in 151 (e.g. `(Israel)` → `countrycode=il`).
- Searches Nominatim for each name variant; keeps the highest-scoring result.

**Scoring formula:**
```
score = name_match × 0.5  +  type_score × 0.3  +  importance × 0.2
```

Results with score ≥ 0.65 (adjustable) are cached. Lower scores are marked *uncertain* and retried on the next run.

## Rate limits

Nominatim's terms of service require a maximum of 1 request per second.
The app enforces this automatically and displays an estimated time remaining.
