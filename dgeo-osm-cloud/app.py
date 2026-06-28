from __future__ import annotations

"""
GeoOSM – Place Authority Geocoder
Streamlit app for geocoding NLI place name authority records via Nominatim.

Cloud version: file upload via st.file_uploader; cache persisted as a
downloadable JSON file (upload at start to resume, download to save progress).

Processing flow (two phases):
  Phase 1 – instant: annotate rows, fetch Wikidata, resolve cache hits.
             Results appear immediately in the Review tab.
  Phase 2 – batched: user triggers N-record chunks for uncached rows.
             Each chunk adds ~N seconds of Nominatim wait, then the
             user can review before requesting the next batch.
"""

import json
import math
import time
from io import BytesIO

import pandas as pd
import streamlit as st

from country_codes import COUNTRY_NAME_TO_CODE, CODE_TO_NAME, detect_country_code
from geocoder import (
    find_col,
    extract_qid,
    extract_latin_name,
    collect_all_names,
    fetch_osm_ids_from_wikidata,
    geocode_row,
    build_output_row,
    nominatim_search_candidates,
    is_cached,
    NOMINATIM_DELAY,
)

# ── Page config ────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="GeoOSM – Place Authority Geocoder",
    page_icon="🗺️",
    layout="wide",
)

# Make the primary action button green throughout the app
st.markdown("""
<style>
button[kind="primary"] {
    background-color: #27ae60 !important;
    border-color: #27ae60 !important;
    color: white !important;
}
button[kind="primary"]:hover {
    background-color: #219150 !important;
    border-color: #219150 !important;
}
</style>
""", unsafe_allow_html=True)

# ── Session state ──────────────────────────────────────────────────────────────

def _init_state():
    if "cache" not in st.session_state:
        st.session_state.cache = {}

    defaults = {
        "phase":               "idle",
        "all_rows":            [],
        "pending_rows":        [],
        "qid_to_osm":          {},
        "result_rows":         [],
        "review_status":       {},
        "overrides":           {},
        "selected_idx":        0,
        "research_idx":        None,
        "research_candidates": [],
        "last_file_name":      None,
        "_cache_file_loaded":  None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()

# ── Helpers ────────────────────────────────────────────────────────────────────

SOURCE_BADGE = {
    "A-lookup":              "🟢",
    "A-fallback-confirmed":  "🟢",
    "A-fallback-uncertain":  "🟡",
    "A-fallback-no-match":   "🔴",
    "B-confirmed":           "🟢",
    "B-uncertain":           "🟡",
    "B-no-match":            "🔴",
    "A-no-name":             "🔴",
    "B-no-name":             "🔴",
    "manual-override":       "✏️",
}


def _osm_url(row: dict) -> str | None:
    if row.get("osm_type") and row.get("osm_id"):
        t = {"relation": "relation", "way": "way", "node": "node"}.get(
            row["osm_type"], "relation"
        )
        return f"https://www.openstreetmap.org/{t}/{row['osm_id']}"
    return None


def _score_label(score) -> str:
    if score is None or str(score) in ("", "None", "nan"):
        return "—"
    try:
        s = float(score)
        if s >= 0.85:   return f"🟢 {s:.2f}"
        elif s >= 0.65: return f"🟡 {s:.2f}"
        else:           return f"🔴 {s:.2f}"
    except (ValueError, TypeError):
        return "—"


def _row_effective(row: dict) -> dict:
    mmsid = str(row.get("MMSID", ""))
    if mmsid in st.session_state.overrides:
        merged = dict(row)
        merged.update(st.session_state.overrides[mmsid])
        return merged
    return row


def _results_df() -> pd.DataFrame:
    if not st.session_state.result_rows:
        return pd.DataFrame()
    return pd.DataFrame(st.session_state.result_rows)


def _eta_str(n_pending: int) -> str:
    secs = math.ceil(n_pending * NOMINATIM_DELAY)
    if secs < 120:
        return f"~{secs}s"
    return f"~{secs // 60}m {secs % 60}s"


def _cache_confirmed(mmsid: str, result_row: dict, cand_raw: dict | None = None) -> None:
    """Write a user-confirmed result to the session cache for every name variant.

    Next prescan: is_cached() will find it and geocode_row() resolves instantly.
    cand_raw: pass the raw _parse_item dict when the result comes from re-search.
    """
    orig_row = next(
        (r for r in st.session_state.all_rows if str(r.get("MMSID")) == mmsid), None
    )
    if not orig_row:
        return
    names        = orig_row.get("_all_names", [])
    country_code = orig_row.get("_country_code")
    if not names:
        return

    if cand_raw is not None:
        entry = {**cand_raw, "score": 1.0}
    else:
        if not result_row.get("osm_id"):
            return
        entry = {
            "osm_type":       result_row.get("osm_type"),
            "osm_id":         result_row.get("osm_id"),
            "lat":            result_row.get("lat"),
            "lon":            result_row.get("lon"),
            "d":              result_row.get("34$d"),
            "e":              result_row.get("34$e"),
            "f":              result_row.get("34$f"),
            "g":              result_row.get("34$g"),
            "importance":     0.0,
            "score":          1.0,
            "osm_class":      result_row.get("osm_class"),
            "osm_place_type": result_row.get("osm_place_type"),
            "wikidata_osm":   result_row.get("wikidata_osm"),
            "wikipedia":      result_row.get("wikipedia"),
            "name_he":        result_row.get("name_he"),
            "name_en":        result_row.get("name_en"),
            "name_ar":        result_row.get("name_ar"),
            "alt_name":       result_row.get("alt_name"),
            "display_name":   result_row.get("display_name"),
        }

    cc_part = f"|cc={country_code}" if country_code else ""
    for name in names:
        st.session_state.cache[f"search:{name.lower()}{cc_part}"] = entry


# ── Map renderer ───────────────────────────────────────────────────────────────

def show_map(row: dict) -> None:
    lat = row.get("lat")
    lon = row.get("lon")
    if lat and lon:
        try:
            source = row.get("source", "")
            if "confirmed" in source or source in ("A-lookup", "manual-override"):
                color = [46, 204, 113, 120]
            elif "uncertain" in source:
                color = [243, 156, 18, 120]
            else:
                color = [231, 76, 60, 120]

            st.map(
                pd.DataFrame({"lat": [float(lat)], "lon": [float(lon)]}),
                zoom=12,
                color=color,
                size=200,
            )
        except Exception as exc:
            st.warning(f"Map could not render: {exc}")
    else:
        st.info("No coordinates for this record.")

# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🗺️ GeoOSM")
    st.caption("Place Authority Geocoder")
    st.divider()

    # ── Excel file upload ──────────────────────────────────────────────────────
    uploaded_file = st.file_uploader(
        "Authority records (.xlsx / .xls)",
        type=["xlsx", "xls"],
        help="Upload the Excel file with columns: MMS ID, 151, 024, 451",
    )

    st.divider()

    # ── Cache: upload to resume a previous session ─────────────────────────────
    st.markdown("**Cache** (optional)")
    cache_upload = st.file_uploader(
        "Resume from saved cache (.json)",
        type=["json"],
        help="Upload a nominatim_cache.json downloaded from a previous session",
        key="cache_uploader",
    )
    if cache_upload is not None:
        if st.session_state["_cache_file_loaded"] != cache_upload.name:
            try:
                loaded = json.loads(cache_upload.getvalue().decode("utf-8"))
                if isinstance(loaded, dict):
                    st.session_state.cache.update(loaded)
                    st.session_state["_cache_file_loaded"] = cache_upload.name
            except Exception:
                st.error("Could not parse the cache file.")

    n_cache = len(st.session_state.cache)
    st.caption(f"{n_cache} cache entries loaded")

    if n_cache:
        cache_json = json.dumps(st.session_state.cache, ensure_ascii=False, indent=2)
        st.download_button(
            "⬇ Save cache (.json)",
            data=cache_json.encode("utf-8"),
            file_name="nominatim_cache.json",
            mime="application/json",
            width="stretch",
            help="Download the current cache — upload it next session to skip already-geocoded records",
        )
        if st.button("Clear cache", width="stretch"):
            st.session_state.cache = {}
            st.session_state["_cache_file_loaded"] = None
            st.rerun()

    st.divider()

    match_threshold = st.slider("Confidence threshold", 0.0, 1.0, 0.65, 0.05)
    fallback_cc = st.text_input(
        "Fallback country code", value="",
        placeholder="e.g. il",
        help="Used when no country is detected from the record",
    ).strip().lower() or None

    batch_size = st.select_slider(
        "Batch size (records per run)",
        options=[10, 25, 50, 100, 200],
        value=50,
        help="How many uncached records to process per click",
    )

    st.divider()

    if st.session_state.result_rows:
        df_out = _results_df()
        df_out["review_status"] = df_out["MMSID"].map(
            lambda x: st.session_state.review_status.get(str(x), "pending")
        )
        for mmsid, override in st.session_state.overrides.items():
            mask = df_out["MMSID"].astype(str) == mmsid
            for col, val in override.items():
                if col in df_out.columns:
                    df_out.loc[mask, col] = val

        buf = BytesIO()
        df_out.to_excel(buf, index=False, engine="openpyxl")
        st.download_button(
            "⬇ Download results (.xlsx)",
            data=buf.getvalue(),
            file_name="geocoded_results.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )

    if st.session_state.phase != "idle":
        if st.button("Reset / new file", width="stretch"):
            for k in ["phase", "all_rows", "pending_rows", "qid_to_osm",
                      "result_rows", "review_status", "overrides",
                      "selected_idx", "research_idx", "research_candidates",
                      "last_file_name"]:
                del st.session_state[k]
            st.rerun()

# ── Main ───────────────────────────────────────────────────────────────────────

st.title("Place Authority Geocoder")

if uploaded_file is None:
    st.info("Upload your authority records Excel file in the sidebar to get started.")
    st.stop()

if uploaded_file.name.lower().split(".")[-1] not in ("xlsx", "xls"):
    st.error("Please upload an .xlsx or .xls file.")
    st.stop()

# ── Parse Excel ────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def parse_excel(file_bytes: bytes, file_name: str) -> pd.DataFrame:
    df = pd.read_excel(BytesIO(file_bytes), dtype=str)
    df.columns = df.columns.str.strip()
    return df

df_raw = parse_excel(uploaded_file.getvalue(), uploaded_file.name)

col_mms  = find_col(df_raw, "MMS") or df_raw.columns[0]
col_0247 = find_col(df_raw, "024")
col_151  = find_col(df_raw, "151")
cols_451 = [c for c in df_raw.columns if c.strip().startswith("451")]

if col_151 is None:
    st.error("Could not find a '151' column. Please check column headers.")
    st.stop()

# Reset state when a different file is uploaded
file_key = uploaded_file.name
if st.session_state.last_file_name != file_key:
    st.session_state.phase               = "idle"
    st.session_state.all_rows            = []
    st.session_state.pending_rows        = []
    st.session_state.qid_to_osm         = {}
    st.session_state.result_rows         = []
    st.session_state.review_status       = {}
    st.session_state.overrides           = {}
    st.session_state.selected_idx        = 0
    st.session_state.research_idx        = None
    st.session_state.research_candidates = []
    st.session_state.last_file_name      = file_key

# ── File preview ───────────────────────────────────────────────────────────────

with st.expander("📋 File preview", expanded=(st.session_state.phase == "idle")):
    c1, c2 = st.columns(2)
    with c1:
        st.write("**Detected columns**")
        st.write(f"- MMS ID: `{col_mms}`")
        st.write(f"- 151: `{col_151}`")
        st.write(f"- 024 (Wikidata): `{col_0247 or 'not found'}`")
        st.write(f"- 451 variants: {cols_451 if cols_451 else ['none found']}")
        st.write(f"- Records: **{len(df_raw)}**")
    with c2:
        st.write("**Sample country detection (first 8 rows)**")
        sample = df_raw[col_151].head(8).apply(detect_country_code)
        sample.name = "country_code"
        st.dataframe(
            pd.concat([df_raw[[col_151]].head(8).rename(columns={col_151: "151"}), sample], axis=1),
            width="stretch", height=200,
        )

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Pre-scan: annotate rows, load Wikidata, resolve cache hits
# ══════════════════════════════════════════════════════════════════════════════

if st.session_state.phase == "idle":
    st.subheader("Step 1 — Pre-scan")
    st.write(
        "Checks the cache and Wikidata for every record. "
        "Cache hits appear immediately; uncached records are queued for Nominatim."
    )

    if st.button("🔍 Pre-scan & load cached results", type="primary"):
        with st.status("Pre-scanning…", expanded=True) as status:

            # ── Step 1: annotate rows ──────────────────────────────────────────
            n_raw = len(df_raw)
            prog1 = st.progress(0, text=f"Annotating records… 0 / {n_raw}")
            rows = []
            for i, (_, r) in enumerate(df_raw.iterrows()):
                rd = r.to_dict()
                rd["MMSID"]          = str(r.get(col_mms, ""))
                rd["_qid"]           = extract_qid(r.get(col_0247)) if col_0247 else None
                rd["_name_lat"]      = extract_latin_name(r.get(col_151))
                rd["_country_code"]  = detect_country_code(r.get(col_151)) or fallback_cc
                rd["_all_names"]     = collect_all_names(rd, col_151, cols_451)
                rows.append(rd)
                if i % 50 == 0 or i == n_raw - 1:
                    prog1.progress((i + 1) / n_raw,
                                   text=f"Annotating records… {i+1} / {n_raw}")
            prog1.progress(1.0, text=f"✓ Annotated {n_raw} records")
            st.session_state.all_rows = rows

            # ── Step 2: Wikidata batch ─────────────────────────────────────────
            qids = list({rd["_qid"] for rd in rows if rd["_qid"]})
            if qids:
                with st.spinner(f"Querying Wikidata for {len(qids)} Q-IDs…"):
                    st.session_state.qid_to_osm = \
                        fetch_osm_ids_from_wikidata(st.session_state.cache, qids)
                n_matched = len(st.session_state.qid_to_osm)
                st.write(f"✓ Wikidata: {n_matched} / {len(qids)} Q-IDs have an OSM relation")
            else:
                st.session_state.qid_to_osm = {}
                st.write("✓ No Wikidata Q-IDs found in this file")

            # ── Step 3: cache check ────────────────────────────────────────────
            prog3 = st.progress(0, text=f"Checking cache… 0 / {n_raw}")
            cached_rows, pending_rows = [], []
            for i, rd in enumerate(rows):
                (cached_rows if is_cached(rd, st.session_state.cache) else pending_rows).append(rd)
                if i % 50 == 0 or i == n_raw - 1:
                    prog3.progress((i + 1) / n_raw,
                                   text=f"Checking cache… {i+1} / {n_raw}")
            prog3.progress(1.0,
                           text=f"✓ Cache: {len(cached_rows)} hits, {len(pending_rows)} need Nominatim")
            st.session_state.pending_rows = pending_rows

            # ── Step 4: resolve cache hits ─────────────────────────────────────
            results = []
            if cached_rows:
                prog4 = st.progress(0, text=f"Loading {len(cached_rows)} cached results…")
                for i, rd in enumerate(cached_rows):
                    geo = geocode_row(rd, st.session_state.qid_to_osm,
                                     st.session_state.cache, match_threshold)
                    results.append(build_output_row(rd, geo))
                    if i % 50 == 0 or i == len(cached_rows) - 1:
                        prog4.progress((i + 1) / len(cached_rows),
                                       text=f"Loading cached results… {i+1} / {len(cached_rows)}")
                prog4.progress(1.0, text=f"✓ Loaded {len(cached_rows)} cached results")
            st.session_state.result_rows = results

            st.session_state.phase = "ready"
            status.update(label="Pre-scan complete!", state="complete")

        st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Batch processing (user-triggered)
# ══════════════════════════════════════════════════════════════════════════════

if st.session_state.phase in ("ready", "done"):

    n_done    = len(st.session_state.result_rows)
    n_pending = len(st.session_state.pending_rows)
    n_total   = len(st.session_state.all_rows)

    pct = n_done / n_total if n_total else 1.0
    pct_label = f"{pct*100:.0f}%"
    st.progress(
        pct,
        text=f"Overall: **{n_done} / {n_total}** records geocoded ({pct_label})"
             + (f" — {_eta_str(n_pending)} remaining" if n_pending else " — complete"),
    )

    if n_pending > 0:
        st.session_state.phase = "ready"

        this_batch = min(batch_size, n_pending)
        info_col, btn_col = st.columns([3, 1])
        with info_col:
            st.info(
                f"**{n_pending} records** still need Nominatim  "
                f"({_eta_str(n_pending)} remaining total).  "
                f"Next batch: **{this_batch} records** — {_eta_str(this_batch)}."
            )
        with btn_col:
            run_batch = st.button(
                f"▶ Process next {this_batch}",
                type="primary",
                width="stretch",
            )

        if run_batch:
            batch     = st.session_state.pending_rows[:this_batch]
            remaining = st.session_state.pending_rows[this_batch:]

            status_text = st.empty()
            prog = st.progress(0)
            t_start = time.monotonic()

            for i, rd in enumerate(batch):
                name = rd.get("_name_lat") or rd.get("MMSID") or "…"
                elapsed = time.monotonic() - t_start
                eta_secs = (elapsed / (i + 1)) * (len(batch) - i - 1) if i > 0 else None
                eta_str  = f" — ETA {_eta_str(int(eta_secs))}" if eta_secs else ""

                status_text.markdown(
                    f"Processing **{i+1} / {len(batch)}**: {name}{eta_str}"
                )
                prog.progress((i + 1) / len(batch))

                geo = geocode_row(rd, st.session_state.qid_to_osm,
                                  st.session_state.cache, match_threshold)
                st.session_state.result_rows.append(build_output_row(rd, geo))

            elapsed_total = time.monotonic() - t_start
            status_text.markdown(
                f"✓ Batch of {len(batch)} done in {elapsed_total:.0f}s — "
                "**save your cache** from the sidebar before closing!"
            )
            prog.progress(1.0)

            st.session_state.pending_rows = remaining
            st.rerun()

    else:
        st.session_state.phase = "done"
        st.success(
            f"All {n_total} records geocoded. "
            "Download the cache from the sidebar to save your progress."
        )

    st.divider()

    # ── Results tabs ───────────────────────────────────────────────────────────
    df = _results_df()
    if df.empty:
        st.info("No results yet — run the pre-scan to load cached records.")
        st.stop()

    confirmed = df["source"].str.contains("confirmed|A-lookup|manual", na=False).sum()
    uncertain = df["source"].str.contains("uncertain", na=False).sum()
    unmatched = df["osm_id"].isna().sum()

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Geocoded",  n_done)
    m2.metric("Confirmed", int(confirmed))
    m3.metric("Uncertain", int(uncertain))
    m4.metric("No match",  int(unmatched))

    tab_review, tab_stats, tab_help = st.tabs(["🔍 Review", "📊 Statistics", "📖 How it works"])

    # ── Review tab ─────────────────────────────────────────────────────────────
    with tab_review:
        fc1, fc2, fc3 = st.columns([2, 2, 3])
        with fc1:
            filter_status = st.selectbox(
                "Filter by match", ["All", "Confirmed", "Uncertain", "No match"],
            )
        with fc2:
            filter_review = st.selectbox(
                "Filter by review", ["All", "Pending", "Confirmed", "Rejected"],
            )
        with fc3:
            search_text = st.text_input("Search name", placeholder="Type to filter…")

        mask = pd.Series([True] * len(df))
        if filter_status == "Confirmed":
            mask &= df["source"].str.contains("confirmed|A-lookup|manual", na=False)
        elif filter_status == "Uncertain":
            mask &= df["source"].str.contains("uncertain", na=False)
        elif filter_status == "No match":
            mask &= df["osm_id"].isna()

        if filter_review != "All":
            rv_target = {"Pending": "pending", "Confirmed": "confirmed", "Rejected": "rejected"}[filter_review]
            mask &= df["MMSID"].map(
                lambda x: st.session_state.review_status.get(str(x), "pending")
            ) == rv_target

        if search_text:
            name_mask = (
                df["name_lat"].str.contains(search_text, case=False, na=False)
                | df["name_en"].str.contains(search_text, case=False, na=False)
                | df["name_he"].str.contains(search_text, case=False, na=False)
                | df["match_name"].str.contains(search_text, case=False, na=False)
            )
            mask &= name_mask

        df_view = df[mask].reset_index(drop=True)

        if df_view.empty:
            st.info("No records match the current filter.")
        else:
            left_col, right_col = st.columns([55, 45])

            with left_col:
                display_rows = []
                for _, row in df_view.iterrows():
                    mmsid  = str(row.get("MMSID", ""))
                    rv     = st.session_state.review_status.get(mmsid, "pending")
                    rv_sym = {"confirmed": "✅", "rejected": "❌", "pending": "…"}[rv]
                    icon   = SOURCE_BADGE.get(row.get("source", ""), "⚪")
                    display_rows.append({
                        "":            rv_sym,
                        "M":           icon,
                        "MMS ID":      mmsid,
                        "Name (Latin)": row.get("name_lat") or "",
                        "Name (EN)":   row.get("name_en") or "",
                        "Country":     row.get("country_code") or "",
                        "Score":       _score_label(row.get("match_score")),
                        "Source":      row.get("source") or "",
                    })

                st.dataframe(
                    pd.DataFrame(display_rows),
                    width="stretch",
                    height=380,
                    hide_index=False,
                )

                unconfirmed_indices = [
                    i for i, r in enumerate(display_rows)
                    if st.session_state.review_status.get(r["MMS ID"], "pending")
                    not in ("confirmed", "rejected")
                ]
                if not unconfirmed_indices:
                    unconfirmed_indices = list(range(len(display_rows)))

                dropdown_labels = {
                    i: f"{display_rows[i]['MMS ID']} – {display_rows[i]['Name (Latin)'] or display_rows[i]['Name (EN)'] or '?'}"
                    for i in unconfirmed_indices
                }

                current = st.session_state.selected_idx
                default = current if current in unconfirmed_indices else unconfirmed_indices[0]

                chosen = st.selectbox(
                    f"Select record to review ({len(unconfirmed_indices)} pending)",
                    options=unconfirmed_indices,
                    format_func=lambda i: dropdown_labels[i],
                    index=unconfirmed_indices.index(default),
                    key="record_selector",
                )
                st.session_state.selected_idx = chosen

            with right_col:
                idx = min(st.session_state.selected_idx, len(df_view) - 1)
                raw_row = df_view.iloc[idx].to_dict()
                eff_row = _row_effective(raw_row)
                mmsid   = str(eff_row.get("MMSID", ""))

                d1, d2 = st.columns(2)
                with d1:
                    st.markdown(f"**MMS ID:** `{mmsid}`")
                    st.markdown(f"**Name (Latin):** {eff_row.get('name_lat') or '—'}")
                    st.markdown(f"**Name (EN):** {eff_row.get('name_en') or '—'}")
                    st.markdown(f"**Name (HE):** {eff_row.get('name_he') or '—'}")
                with d2:
                    st.markdown(f"**Score:** {_score_label(eff_row.get('match_score'))}")
                    st.markdown(f"**Source:** {eff_row.get('source') or '—'}")
                    st.markdown(f"**OSM type:** {eff_row.get('osm_place_type') or '—'}")
                    st.markdown(f"**Country:** {eff_row.get('country_code') or '—'}")

                osm_link = _osm_url(eff_row)
                if osm_link:
                    st.markdown(f"[Open in OpenStreetMap ↗]({osm_link})")
                if eff_row.get("wikipedia"):
                    lang, _, title = str(eff_row["wikipedia"]).partition(":")
                    st.markdown(
                        f"[Wikipedia ↗](https://{lang}.wikipedia.org/wiki/{title.replace(' ', '_')})"
                    )

                rv = st.session_state.review_status.get(mmsid, "pending")
                ba, bb, bc = st.columns(3)
                with ba:
                    if st.button("✅ Confirm", width="stretch",
                                 type="primary" if rv != "confirmed" else "secondary"):
                        st.session_state.review_status[mmsid] = "confirmed"
                        _cache_confirmed(mmsid, eff_row)
                        st.rerun()
                with bb:
                    if st.button("❌ Reject", width="stretch"):
                        st.session_state.review_status[mmsid] = "rejected"
                        st.rerun()
                with bc:
                    if st.button("🔍 Re-search", width="stretch"):
                        st.session_state.research_idx        = idx
                        st.session_state.research_candidates = []
                        st.rerun()

                with st.expander("🗺️ Map", expanded=False):
                    show_map(eff_row)

                if st.session_state.research_idx == idx:
                    st.divider()
                    st.markdown("**Re-search**")
                    default_q = eff_row.get("name_lat") or eff_row.get("match_name") or ""
                    query = st.text_input("Search query", value=default_q, key="research_q")
                    cc_opts = ["(global – no filter)"] + sorted(set(COUNTRY_NAME_TO_CODE.values()))
                    cc_def  = eff_row.get("country_code") or "(global – no filter)"
                    cc_sel  = st.selectbox("Country code", cc_opts,
                                           index=cc_opts.index(cc_def) if cc_def in cc_opts else 0,
                                           key="research_cc")
                    cc_use = None if cc_sel == "(global – no filter)" else cc_sel

                    if st.button("Search", key="do_research"):
                        with st.spinner("Searching Nominatim…"):
                            st.session_state.research_candidates = \
                                nominatim_search_candidates(query, cc_use, limit=5)

                    for ci, cand in enumerate(st.session_state.research_candidates):
                        label = (
                            f"{_score_label(cand['score'])}  "
                            f"{(cand.get('name_en') or cand.get('display_name') or '?')[:60]}  "
                            f"({cand.get('osm_place_type','')})"
                        )
                        if st.button(label, key=f"pick_{ci}"):
                            override = {
                                k: cand[k] for k in [
                                    "lat","lon","d","e","f","g",
                                    "osm_type","osm_id","osm_class","osm_place_type",
                                    "wikidata_osm","wikipedia",
                                    "name_he","name_en","name_ar","alt_name","display_name",
                                ]
                            }
                            override["match_score"] = cand["score"]
                            override["34$d"] = cand["d"]
                            override["34$e"] = cand["e"]
                            override["34$f"] = cand["f"]
                            override["34$g"] = cand["g"]
                            override["source"] = "manual-override"
                            st.session_state.overrides[mmsid] = override
                            st.session_state.review_status[mmsid]  = "confirmed"
                            st.session_state.research_idx           = None
                            st.session_state.research_candidates    = []
                            _cache_confirmed(mmsid, eff_row, cand_raw=cand)
                            st.rerun()

    # ── Stats tab ───────────────────────────────────────────────────────────────
    with tab_stats:
        df_s = df.copy()
        df_s["review_status"] = df_s["MMSID"].map(
            lambda x: st.session_state.review_status.get(str(x), "pending")
        )

        sc1, sc2 = st.columns(2)
        with sc1:
            st.markdown("**Match source breakdown**")
            st.dataframe(
                df_s["source"].value_counts().rename_axis("source").reset_index(name="count"),
                width="stretch", hide_index=True,
            )
        with sc2:
            st.markdown("**Country distribution**")
            cc_df = (
                df_s["country_code"].fillna("(none)")
                .value_counts()
                .rename_axis("country_code")
                .reset_index(name="count")
            )
            cc_df["country"] = cc_df["country_code"].map(lambda c: CODE_TO_NAME.get(c, c))
            st.dataframe(cc_df[["country_code","country","count"]],
                         width="stretch", hide_index=True)

        st.markdown("**Review status**")
        st.dataframe(
            df_s["review_status"].value_counts().rename_axis("status").reset_index(name="count"),
            width="stretch", hide_index=True,
        )

        st.markdown("**Score distribution**")
        score_df = df_s["match_score"].dropna().astype(float)
        if not score_df.empty:
            st.bar_chart(score_df.round(1).value_counts().sort_index())

    # ── How it works tab ────────────────────────────────────────────────────────
    with tab_help:
        st.header("How the geocoding works")

        st.subheader("Source values explained")

        st.dataframe(
            pd.DataFrame([
                {"Source":         "A-lookup",
                 "Icon": "🟢",
                 "Match quality":  "Authoritative",
                 "Meaning": "Wikidata P402 → Nominatim lookup by OSM relation ID. Score always 1.00."},
                {"Source":         "A-fallback-confirmed",
                 "Icon": "🟢",
                 "Match quality":  "High confidence",
                 "Meaning": "Wikidata ID present but no P402; name search used instead. Score ≥ threshold."},
                {"Source":         "A-fallback-uncertain",
                 "Icon": "🟡",
                 "Match quality":  "Low confidence",
                 "Meaning": "Wikidata ID present but no P402; name search returned a result below the threshold. Needs review."},
                {"Source":         "A-fallback-no-match",
                 "Icon": "🔴",
                 "Match quality":  "No match",
                 "Meaning": "Wikidata ID present but no P402, and Nominatim returned nothing for any name variant."},
                {"Source":         "B-confirmed",
                 "Icon": "🟢",
                 "Match quality":  "High confidence",
                 "Meaning": "No Wikidata ID; name search found a result above the confidence threshold."},
                {"Source":         "B-uncertain",
                 "Icon": "🟡",
                 "Match quality":  "Low confidence",
                 "Meaning": "No Wikidata ID; name search returned a result below the threshold. Needs review."},
                {"Source":         "B-no-match",
                 "Icon": "🔴",
                 "Match quality":  "No match",
                 "Meaning": "No Wikidata ID and Nominatim returned nothing for any name variant."},
                {"Source":         "manual-override",
                 "Icon": "✏️",
                 "Match quality":  "Manually confirmed",
                 "Meaning": "The reviewer selected a specific match from the Re-search candidates list."},
            ]),
            width="stretch", hide_index=True,
        )

        st.divider()

        st.subheader("Path A — Records with a Wikidata ID (field 024)")
        st.markdown("""
1. **Wikidata SPARQL** — queries Wikidata for the OSM relation ID linked via property **P402**.
2. **Nominatim `/lookup`** — fetches the full OSM record directly by relation ID.
   This is an authoritative match (score = **1.00**) and is always cached.
3. **Fallback** — if Wikidata has no P402, falls through to name search (same as Path B).
        """)

        st.subheader("Path B — Records without a Wikidata ID (or no P402)")
        st.markdown("""
1. **Name variants** — every `$$a` value from fields **151** and **451** across all languages
   (Latin, Hebrew, Arabic …) is extracted. Trailing qualifiers are stripped before searching:
   `"Nimrin (Extinct city) (Israel)"` → `"Nimrin"`.
2. **Country detection** — the last parenthetical in 151 is matched to an ISO country code
   (e.g. `(Israel)` → `il`, `(France)` → `fr`) to restrict the Nominatim search.
3. **Nominatim `/search`** — each name variant is searched; the highest-scoring result is kept.
        """)

        st.subheader("Match scoring (Path B)")
        st.code("score = name_match × 0.5  +  type_score × 0.3  +  importance × 0.2")
        st.dataframe(
            pd.DataFrame([
                {"Signal": "name_match", "Weight": "50 %",
                 "How it is computed": "1.0 if the queried name appears in any OSM name field (name:he, name:en, name:ar …); otherwise token-overlap ratio against the OSM display_name."},
                {"Signal": "type_score", "Weight": "30 %",
                 "How it is computed": "1.0 for place / boundary / historic / natural / waterway · 0.7 for landuse / leisure · 0.1 for amenity / building / highway / shop / railway."},
                {"Signal": "importance", "Weight": "20 %",
                 "How it is computed": "Nominatim's own 0–1 float reflecting the global significance of the place in OSM."},
            ]),
            width="stretch", hide_index=True,
        )
        st.markdown("""
Results with **score ≥ confidence threshold** (default **0.65**, adjustable in the sidebar)
are cached and marked *confirmed*. Results below the threshold are *uncertain* and **not cached**,
so re-running the app will retry them.
        """)

        st.divider()

        st.subheader("Saving your progress")
        st.markdown("""
Because this app runs in the cloud, results are **not saved automatically**.

- After each batch, click **⬇ Save cache (.json)** in the sidebar and keep the file.
- Next session, upload the same `nominatim_cache.json` — every cached record resolves instantly without hitting Nominatim again.
- When all records are geocoded, click **⬇ Download results (.xlsx)** for the full output.
        """)

        st.divider()

        st.subheader("Output columns")

        st.dataframe(
            pd.DataFrame([
                {"Column": "MMSID",          "Description": "Original MMS ID from the authority record."},
                {"Column": "wikidata_id",     "Description": "Wikidata Q-ID extracted from field 024."},
                {"Column": "name_lat",        "Description": "Latin-script authorized name from field 151 ($$9lat)."},
                {"Column": "country_code",    "Description": "ISO 3166-1 alpha-2 code detected from the 151 parenthetical."},
                {"Column": "osm_type",        "Description": "OSM element type: relation, way, or node."},
                {"Column": "osm_id",          "Description": "Numeric OSM identifier."},
                {"Column": "lat / lon",       "Description": "Centroid coordinates of the matched OSM element."},
                {"Column": "34$d",            "Description": "MARC 034 subfield d — west longitude of bounding box."},
                {"Column": "34$e",            "Description": "MARC 034 subfield e — east longitude of bounding box."},
                {"Column": "34$f",            "Description": "MARC 034 subfield f — north latitude of bounding box."},
                {"Column": "34$g",            "Description": "MARC 034 subfield g — south latitude of bounding box."},
                {"Column": "osm_class",       "Description": "OSM class (e.g. place, boundary, historic, natural)."},
                {"Column": "osm_place_type",  "Description": "OSM type (e.g. village, city, archaeological_site, peak)."},
                {"Column": "wikidata_osm",    "Description": "Wikidata Q-ID stored on the OSM entity — used for cross-validation."},
                {"Column": "wikipedia",       "Description": "Wikipedia link stored on the OSM entity (e.g. en:Tira, Israel)."},
                {"Column": "name_he",         "Description": "Hebrew name from OSM namedetails."},
                {"Column": "name_en",         "Description": "English name from OSM namedetails."},
                {"Column": "name_ar",         "Description": "Arabic name from OSM namedetails."},
                {"Column": "alt_name",        "Description": "Alternative name from OSM namedetails."},
                {"Column": "match_name",      "Description": "The specific name variant that produced the winning Nominatim result."},
                {"Column": "match_score",     "Description": "Composite score 0–1 (see scoring formula above)."},
                {"Column": "source",          "Description": "Geocoding path and confidence level (see Source values table above)."},
                {"Column": "review_status",   "Description": "Reviewer decision: confirmed, rejected, or pending (added on export)."},
            ]),
            width="stretch", hide_index=True,
        )
