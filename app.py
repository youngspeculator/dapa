"""
pweza/app.py
Flask application — schema-aware routes and search layer.

Database: SQLite + FTS5 (odpc.db)
Schema:   schema.sql v2

Search architecture (two modes):
  /api/search      — used by both Explore (NL freeform) and Sector Search
                     (sector-filtered). A single backend endpoint handles both;
                     the `source` param tells the renderer which fields to surface.
  /api/glitch/search — glitch table with layer / sector / date filters.
  /api/corpus-stats  — pre-aggregated counts for homepage stats + sidebar widgets.
"""

import sqlite3
import re
import json
import os
import math as _math
from datetime import datetime
from functools import lru_cache
from flask import Flask, render_template, request, jsonify, g
from flask import abort

# ── APP INIT ──────────────────────────────────────────────
READ_ONLY = os.environ.get("READ_ONLY", "false").lower() == "true"

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "odpc.db")

# ── CONTROLLED VOCABULARIES ───────────────────────────────
# Single source of truth in app.py — must match importas.py.

SECTORS = [
    "Banking & Finance", "Digital Lending", "Betting & Gaming",
    "Children or Minors", "Insurance", "SACCOs", "Healthcare",
    "Hospitality", "Education", "Logistics & Transport",
    "Telecommunications", "Employment & HR", "Real Estate",
    "Government & Public Sector", "Media & Marketing", "Sports",
    "Retail & E-Commerce", "Technology & ICT", "Manufacturing", "Energy",
    "NGOs, CBOs, IGOs & Non-Profits", "Individuals",
]

SYSTEM_LAYERS = [
    "Application layer", "Infrastructure layer", "Process layer",
    "Policy layer", "Human layer", "Integration layer",
]

INCIDENT_TYPES = [
    "Unauthorised disclosure", "Unauthorised data collection",
    "Unauthorised data use", "Unlawful data acquisition",
    "Unlawful commercial use of personal data",
    "Unlawful internal data sharing",
    "Unlawful commercial use of likeness/image",
    "Consent mechanism failure", "Failure to action data subject rights",
    "Integration layer failure", "Governance failure",
    "Obstruction of Data Commissioner",
    "Processing of minors' personal data without parental consent"
    "Other",
]

OUTCOME_TYPES = [
    "Compensation Only", "Enforcement Notice", "Compensation & Enforcement Notice",
    "Dismissed", "Resolved", "Prosecution Recommended",
    "Compensation & Enforcement Notice & Prosecution Recommended",
    "Conditional Enforcement Notice", "Compensation & Conditional Enforcement Notice",
    "Penalty Notice", "Compensation & Prosecution Recommended",
    "Compensation & Cease-User Order", "Compensation & Direct Order",
]

# Curated landmark cases surfaced in Explore sidebar.
# These IDs are updated manually after ingestion is complete.
# Format: list of dicts with id, title, sector, year.
LANDMARK_CASES = [
    # populated post-ingestion; keep as empty list for now
]


# _____ SYNONYM MAP EXPANDABLE WITH CORPUS
SYNONYM_MAP = {
    "image":       ["photograph", "picture", "likeness", "photo"],
    "social media":["instagram", "facebook", "twitter", "whatsapp", "X", "tinder", "mtandao"],
    "loan":        ["credit", "lending", "microfinance", "digital lending", "kopa"],
    "consent":     ["permission", "authorisation", "agreement"],
    "employee":    ["staff", "worker", "personnel"],
}


# ── DATABASE CONNECTION ────────────────────────────────────
def get_db():
    """Return a thread-local SQLite connection (WAL mode, FK on)."""
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL;")
        db.execute("PRAGMA foreign_keys=ON;")
    return db


@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


# ── TEMPLATE CONTEXT ──────────────────────────────────────
@app.context_processor
def inject_globals():
    return {
        "current_year": datetime.now().year,
        # active_nav is set per route via render_template kwarg
    }


# ════════════════════════════════════════════════════════════
# PAGE ROUTES
# ════════════════════════════════════════════════════════════

@app.route("/")
def index():
    stats = _corpus_stats()
    return render_template(
        "index.html",
        active_nav="home",
        corpus_stats=stats,
    )


@app.route("/explore")
def explore():
    return render_template(
        "explore.html",
        active_nav="explore",
        landmark_cases=LANDMARK_CASES,
    )


@app.route("/sector-search")
def sector_search():
    return render_template(
        "sector_search.html",
        active_nav="sector-search",
        sectors=SECTORS,
        outcome_types=OUTCOME_TYPES,
    )


@app.route("/glitch")
def glitch():
    return render_template(
        "glitch.html",
        active_nav="glitch",
        system_layers=SYSTEM_LAYERS,
        incident_types=INCIDENT_TYPES,
        sectors=SECTORS,
    )


@app.route("/wall")
def wall():
    stats  = _corpus_stats()
    return render_template(
        "wall.html",
        active_nav="wall",
        corpus_stats=stats,
        sectors=SECTORS,
    )


@app.route("/patterns")
def patterns():
    return render_template(
        "patterns.html",
        active_nav="patterns",
    )


@app.route("/case/<int:case_id>")
def case_detail(case_id):
    """
    Case detail page. Fetches the full determination record plus
    its related violations, section_reg_pairs, technical_terms,
    and cross_citations for render in caseDetail.html.
    """
    db   = get_db()
    case = db.execute(
        "SELECT * FROM determinations WHERE id = ?", (case_id,)
    ).fetchone()

    if not case:
        return render_template("404.html"), 404

    violations = db.execute(
        """
        SELECT v.*, s.section_number, s.title AS section_title
        FROM violations v
        LEFT JOIN sections s ON s.id = v.section_id
        WHERE v.determination_id = ?
        ORDER BY v.severity DESC
        """,
        (case_id,),
    ).fetchall()

    srp = db.execute(
        """
        SELECT sp.*, s.section_number, s.title AS section_title
        FROM section_reg_pairs sp
        LEFT JOIN sections s ON s.id = sp.section_id
        WHERE sp.determination_id = ?
        """,
        (case_id,),
    ).fetchall()

    terms = db.execute(
        "SELECT * FROM technical_terms WHERE determination_id = ? ORDER BY term",
        (case_id,),
    ).fetchall()

    citations = db.execute(
        "SELECT * FROM cross_citations WHERE citing_case_id = ? ORDER BY cited_type",
        (case_id,),
    ).fetchall()

    glitch_entry = db.execute(
        "SELECT id, case_title, incident_type, system_layer FROM glitch WHERE determination_id = ?",
        (case_id,),
    ).fetchone()

    return render_template(
        "caseDetail.html",
        active_nav="",
        case=case,
        violations=violations,
        srp=srp,
        terms=terms,
        citations=citations,
        glitch_entry=glitch_entry,
    )


@app.route("/glitch/<int:glitch_id>")
def glitch_detail(glitch_id):
    db    = get_db()
    entry = db.execute("SELECT * FROM glitch WHERE id = ?", (glitch_id,)).fetchone()

    if not entry:
        return render_template("404.html"), 404

    det = None
    terms = []
    citations = []
    violations = []

    if entry["determination_id"]:
        det = db.execute(
            "SELECT * FROM determinations WHERE id = ?",
            (entry["determination_id"],),
        ).fetchone()

        # Technical terms — power the keyword sidebar
        terms = db.execute(
            "SELECT term, definition, category FROM technical_terms "
            "WHERE determination_id = ? ORDER BY term",
            (entry["determination_id"],),
        ).fetchall()

        # Cross-citations — link out from keyword sidebar
        citations = db.execute(
            "SELECT cited_reference, cited_type, jurisdiction, citation_context "
            "FROM cross_citations WHERE citing_case_id = ? ORDER BY cited_type",
            (entry["determination_id"],),
        ).fetchall()

        # Violations — for the case metadata sidebar
        violations = db.execute(
            """
            SELECT v.violation_type, v.severity, s.section_number, s.title AS section_title
            FROM violations v
            LEFT JOIN sections s ON s.id = v.section_id
            WHERE v.determination_id = ?
            ORDER BY v.severity DESC
            """,
            (entry["determination_id"],),
        ).fetchall()

    return render_template(
        "glitchDetail.html",
        active_nav="glitch",
        entry=entry,
        det=det,
        terms=terms,
        citations=citations,
        violations=violations,
    )

# ════════════════════════════════════════════════════════════
# API ROUTES
# ════════════════════════════════════════════════════════════

@app.route("/api/search")
def api_search():
    """
    Unified search endpoint used by both Explore and Sector Search.

    Query params:
      q        — free-text query (natural language / keyword)
      sector   — sector name or 'all' (default 'all')
      year     — YYYY or ''
      month    — MM or ''
      outcome  — outcome_type filter or ''
      source   — 'explore' | 'sector' (controls matched_fields surfacing)
      page     — integer, default 1

    Returns:
      {
        results:       [ case objects ],
        total:         int,
        sector_counts: { sector_name: count, … },
        total_corpus:  int,
        highest_quantum: float | null,
      }

    Search strategy:
      1. If q is present → FTS5 full-text search across
         merits + technical_summary + important_flags fields
         (the three most analytically rich prose fields).
         Then filter by sector / year / month / outcome.
      2. If q is absent but sector != 'all' → simple sector browse,
         ordered by determination_date DESC.
      3. Sector counts are always returned for sidebar hydration.

    TODO: extend FTS5 virtual table to also match violations.violation_type
          and technical_terms.definition. Requires FTS5 content table or
          a separate search_index table joining all text fields.
    """
    q       = request.args.get("q", "").strip()
    sector  = request.args.get("sector", "all").strip()
    year    = request.args.get("year", "").strip()
    month   = request.args.get("month", "").strip()
    outcome = request.args.get("outcome", "").strip()
    page    = max(1, int(request.args.get("page", 1)))
    per_page = 20

    db = get_db()

    # ── Check if FTS5 table exists ──────────────────────────
    fts_exists = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='det_fts'"
    ).fetchone()

    # ── Build WHERE clause ──────────────────────────────────
    conditions = []
    params     = []

    if sector and sector != "all":
        conditions.append("d.sector = ?")
        params.append(sector)

    if year:
        conditions.append("strftime('%Y', d.determination_date) = ?")
        params.append(year)

    if month:
        conditions.append("strftime('%m', d.determination_date) = ?")
        params.append(month)

    if outcome:
        conditions.append("d.outcome_type = ?")
        params.append(outcome)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # ── Execute search ──────────────────────────────────────
    if q and fts_exists:
        # FTS5 path: join det_fts on rowid
        fts_q = _sanitise_fts_query(q)
        fts_conditions = ["det_fts MATCH ?"] + conditions
        fts_where = "WHERE " + " AND ".join(fts_conditions)
        fts_params = [fts_q] + params

        count_row = db.execute(
            f"""
            SELECT COUNT(*) FROM det_fts
            JOIN determinations d ON d.id = det_fts.rowid
            {fts_where}
            """,
            fts_params,
        ).fetchone()

        rows = db.execute(
            f"""
            SELECT d.*,
                   bm25(det_fts) AS relevance_score
            FROM det_fts
            JOIN determinations d ON d.id = det_fts.rowid
            {fts_where}
            ORDER BY relevance_score
            LIMIT ? OFFSET ?
            """,
            fts_params + [per_page, (page - 1) * per_page],
        ).fetchall()

    elif q and not fts_exists:
        # Fallback: LIKE search across key prose fields (slow but functional
        # while FTS5 index is being built or first run before indexing)
        like_conditions = conditions + [
            "(d.merits LIKE ? OR d.technical_summary LIKE ? "
            "OR d.important_flags LIKE ? "
            "OR d.respondent LIKE ? OR d.complainant LIKE ? "
            "OR d.case_reference LIKE ? OR d.complaint_number LIKE ?)"
        ]
        like_where = "WHERE " + " AND ".join(like_conditions)
        wildcard   = f"%{q}%"
        like_params = params + [wildcard] * 7

        count_row = db.execute(
            f"SELECT COUNT(*) FROM determinations d {like_where}", like_params
        ).fetchone()

        rows = db.execute(
            f"""
            SELECT d.*, NULL AS relevance_score
            FROM determinations d
            {like_where}
            ORDER BY d.determination_date DESC
            LIMIT ? OFFSET ?
            """,
            like_params + [per_page, (page - 1) * per_page],
        ).fetchall()

    else:
        # Browse mode: no query, sector/date/outcome filter only
        count_row = db.execute(
            f"SELECT COUNT(*) FROM determinations d {where_clause}", params
        ).fetchone()

        rows = db.execute(
            f"""
            SELECT d.*, NULL AS relevance_score
            FROM determinations d
            {where_clause}
            ORDER BY d.determination_date DESC
            LIMIT ? OFFSET ?
            """,
            params + [per_page, (page - 1) * per_page],
        ).fetchall()

    total = count_row[0] if count_row else 0

    # ── Sector counts (for sidebar + sector grid) ───────────
    sector_counts = _sector_counts(db)
    total_corpus  = sum(sector_counts.values())
    highest_q     = _highest_quantum_in_set(db, where_clause, params) if rows else None

    # ── Serialise rows ──────────────────────────────────────
    results = []
    for row in rows:
        r = dict(row)
        results.append({
            "id":             r["id"],
            "title":          _case_display_title(r),
            "case_ref":       r.get("case_reference"),
            "complaint_no":   r.get("complaint_number"),
            "sector":         r.get("sector"),
            "date":           r.get("determination_date", "")[:10] if r.get("determination_date") else None,
            "outcome_type":   r.get("outcome_type"),
            "quantum":        r.get("quantum"),
            "theme":          _extract_theme(r.get("important_flags")),
            "summary":        _truncate(r.get("merits"), 280),
            "citations":      None,   # populated below if needed
            "matched_fields": _infer_matched_fields(r, q),
        })

    # Hydrate cross-citations for the result set (one extra query)
    if results:
        ids      = [r["id"] for r in results]
        placeholders = ",".join("?" * len(ids))
        cit_rows = db.execute(
            f"""
            SELECT citing_case_id, cited_reference, citation_context
            FROM cross_citations
            WHERE citing_case_id IN ({placeholders})
            ORDER BY citing_case_id
            """,
            ids,
        ).fetchall()
        cit_map = {}
        for cr in cit_rows:
            cit_map.setdefault(cr["citing_case_id"], []).append(cr["cited_reference"])
        for r in results:
            refs = cit_map.get(r["id"])
            if refs:
                r["citations"] = " · ".join(refs[:3])  # cap at 3 for display

    return jsonify({
        "results":        results,
        "total":          total,
	"total_pages":    _math.ceil(total / per_page),
        "sector_counts":  sector_counts,
        "total_corpus":   total_corpus,
        "highest_quantum": highest_q,
    })


@app.route("/api/glitch/search")
def api_glitch_search():
    """
    Glitch search endpoint.

    Query params:
      q      — free text
      sector — sector filter (via join to determinations)
      date   — YYYY filter
      layer  — system_layer filter
      page   — integer, default 1

    Returns: { results: [...], total: int }
    """
    q      = request.args.get("q", "").strip()
    sector = request.args.get("sector", "").strip()
    date   = request.args.get("date", "").strip()
    layer  = request.args.get("layer", "").strip()
    page   = max(1, int(request.args.get("page", 1)))
    per_page = 20

    db         = get_db()
    conditions = []
    params     = []

    if layer:
        conditions.append("LOWER(g.system_layer) LIKE LOWER(?)")
        params.append(f"%{layer}%")

    if sector:
        conditions.append("d.sector = ?")
        params.append(sector)

    if date:
        conditions.append("strftime('%Y', g.published_date) = ?")
        params.append(date)

    if q:
        conditions.append(
            "(g.case_title LIKE ? OR g.technical_failure LIKE ? "
            "OR g.regulatory_signal LIKE ? OR g.macro_pattern LIKE ? "
            "OR g.incident_type LIKE ?)"
        )
        wildcard = f"%{q}%"
        params.extend([wildcard] * 5)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    count_row = db.execute(
        f"""
        SELECT COUNT(*) FROM glitch g
        LEFT JOIN determinations d ON d.id = g.determination_id
        {where}
        """,
        params,
    ).fetchone()

    rows = db.execute(
        f"""
        SELECT g.*, d.respondent, d.complainant, d.sector,
               d.determination_date, d.case_reference
        FROM glitch g
        LEFT JOIN determinations d ON d.id = g.determination_id
        {where}
        ORDER BY g.published_date DESC
        LIMIT ? OFFSET ?
        """,
        params + [per_page, (page - 1) * per_page],
    ).fetchall()

    results = []
    for row in rows:
        r = dict(row)
        # Parse incident_type for tag display
        themes = [t.strip() for t in (r.get("incident_type") or "").split("/") if t.strip()]
        results.append({
            "id":         r["id"],
            "title":      r.get("case_title"),
            "case_name":  _case_display_title(r) if r.get("respondent") else None,
            "layer":      r.get("system_layer"),
            "themes":     themes,
            "overview":   _truncate(r.get("technical_failure"), 320),
            "sector":     r.get("sector"),
            "date":       (r.get("determination_date") or "")[:10] or None,
        })

    return jsonify({
        "results": results,
        "total":   count_row[0] if count_row else 0,
    })

@app.route("/sections")
def sections():
    return render_template("sections.html", active_nav="sections")


@app.route("/api/sections/index")
def api_sections_index():
    """
    Returns all sections grouped by thematic_category.
    Each section includes: section_number, title, thematic_category,
    total violation count, and which reg_set it belongs to (DPA / General / Enforcement).

    reg_set is inferred from section_number prefix:
      Starts with 'Reg' + contains 'Gen'  → general_regs
      Starts with 'Reg' + contains 'Enf'  → enforcement_regs
      Otherwise                            → dpa
    """
    import math as _math
    db = get_db()
    rows = db.execute("""
        SELECT
            s.id,
            s.section_number,
            s.title,
            s.thematic_category,
            COUNT(v.id) AS violation_count
        FROM sections s
        LEFT JOIN violations v ON v.section_id = s.id
        GROUP BY s.id
        ORDER BY s.thematic_category, s.section_number
    """).fetchall()

    def infer_reg_set(number):
        n = (number or '').lower()
        if 'reg' not in n:
            return 'dpa'
        if 'gen' in n:
            return 'general_regs'
        if 'enf' in n or 'enfor' in n:
            return 'enforcement_regs'
        return 'dpa'

    result = {}
    for row in rows:
        r = dict(row)
        r['reg_set'] = infer_reg_set(r['section_number'])
        cat = r['thematic_category'] or 'Other'
        result.setdefault(cat, []).append(r)

    return jsonify(result)
 
@app.route("/api/sections/<path:section_number>/cases")
def api_section_cases(section_number):
    import math as _math
    db      = get_db()
    page    = max(1, int(request.args.get("page", 1)))
    per_page = 20
    section = db.execute("SELECT * FROM sections WHERE section_number = ?", (section_number,)).fetchone()
    if not section:
        return jsonify({"error": "Section not found"}), 404
    section_id = section["id"]

    is_regulation = "reg" in (section_number or "").lower()

    if not is_regulation:
        # ── unchanged — existing DPA-section path via violations ──
        stats_row = db.execute("""
            SELECT COUNT(v.id) AS total_violations,
                   COUNT(DISTINCT d.sector) AS sectors_affected,
                   COALESCE(SUM(d.quantum), 0) AS total_quantum,
                   COALESCE(ROUND(AVG(NULLIF(d.quantum,0)),0), 0) AS avg_quantum,
                   ROUND(AVG(v.severity), 1) AS avg_severity
            FROM violations v JOIN determinations d ON d.id = v.determination_id
            WHERE v.section_id = ?
        """, (section_id,)).fetchone()
        most_common_sector = db.execute("""
            SELECT d.sector, COUNT(*) AS c FROM violations v
            JOIN determinations d ON d.id = v.determination_id
            WHERE v.section_id = ? AND d.sector IS NOT NULL
            GROUP BY d.sector ORDER BY c DESC LIMIT 1
        """, (section_id,)).fetchone()
        total = db.execute("SELECT COUNT(*) FROM violations WHERE section_id = ?", (section_id,)).fetchone()[0]
        rows = db.execute("""
            SELECT d.id, d.complainant_initials, d.complainant, d.respondent, d.case_reference,
                   d.sector, d.determination_date, d.outcome_type, d.quantum,
                   v.violation_type, v.severity
            FROM violations v JOIN determinations d ON d.id = v.determination_id
            WHERE v.section_id = ?
            ORDER BY v.severity DESC, d.determination_date DESC
            LIMIT ? OFFSET ?
        """, (section_id, per_page, (page - 1) * per_page)).fetchall()
    else:
        # ── new — regulation path via section_reg_pairs, one level up at determinations ──
        stats_row = db.execute("""
            SELECT COUNT(srp.id) AS total_violations,
                   COUNT(DISTINCT d.sector) AS sectors_affected,
                   COALESCE(SUM(d.quantum), 0) AS total_quantum,
                   COALESCE(ROUND(AVG(NULLIF(d.quantum,0)),0), 0) AS avg_quantum,
                   NULL AS avg_severity
            FROM section_reg_pairs srp JOIN determinations d ON d.id = srp.determination_id
            WHERE srp.regulation_section_id = ?
        """, (section_id,)).fetchone()
        most_common_sector = db.execute("""
            SELECT d.sector, COUNT(*) AS c FROM section_reg_pairs srp
            JOIN determinations d ON d.id = srp.determination_id
            WHERE srp.regulation_section_id = ? AND d.sector IS NOT NULL
            GROUP BY d.sector ORDER BY c DESC LIMIT 1
        """, (section_id,)).fetchone()
        total = db.execute("SELECT COUNT(*) FROM section_reg_pairs WHERE regulation_section_id = ?", (section_id,)).fetchone()[0]
        rows = db.execute("""
            SELECT d.id, d.complainant_initials, d.complainant, d.respondent, d.case_reference,
                   d.sector, d.determination_date, d.outcome_type, d.quantum,
                   srp.interpretive_note, srp.pairing_type
            FROM section_reg_pairs srp JOIN determinations d ON d.id = srp.determination_id
            WHERE srp.regulation_section_id = ?
            ORDER BY d.determination_date DESC
            LIMIT ? OFFSET ?
        """, (section_id, per_page, (page - 1) * per_page)).fetchall()

    results = []
    for row in rows:
        r = dict(row)
        complainant = r.get("complainant_initials") or r.get("complainant") or "?"
        results.append({
            "id":             r["id"],
            "title":          f"{complainant} v {r['respondent']}",
            "case_ref":       r["case_reference"],
            "sector":         r["sector"],
            "date":           (r["determination_date"] or "")[:10],
            "outcome_type":   r["outcome_type"],
            "quantum":        r["quantum"],
            "violation_type": r.get("violation_type") or r.get("interpretive_note"),  # see note below
            "severity":       r.get("severity"),
        })

    stats = dict(stats_row) if stats_row else {}
    stats["most_common_sector"] = most_common_sector["sector"] if most_common_sector else None
    return jsonify({
        "section": dict(section), "stats": stats, "results": results,
        "total": total, "total_pages": _math.ceil(total / per_page) if total else 1,
    })


@app.route("/api/corpus-stats")
def api_corpus_stats():
    """
    Returns pre-aggregated corpus statistics for homepage stats,
    explore sidebar, and sector-search grid count hydration.

    Response shape:
    {
      total_cases:    int,
      sectors:        int,          # distinct sectors with ≥1 case
      year_range:     "2021–2025",
      highest_award:  "900K",
      by_sector:      { "Digital Lending": 18, … },
      by_outcome:     { "compensation": 45, … },
    }
    """
    stats = _corpus_stats()
    return jsonify(stats)


@app.route("/api/case/<int:case_id>/transform")
def api_case_transform(case_id):
    """
    On-demand text transformation for caseDetail panel buttons.

    Query params:
      mode  — 'simple' | 'swahili'

    Both modes call the Anthropic API. Results are cached in the
    `case_transforms` table (created below if absent) keyed on
    (determination_id, mode). Subsequent requests for the same
    case+mode are served from cache — no repeat API calls.

    Returns: { text: str, cached: bool }

    The Anthropic API key is read from the ANTHROPIC_API_KEY
    environment variable. If absent, returns a 503 with a clear
    error message so the UI can display it gracefully.
    """
    import urllib.request
    import json as _json

    mode = request.args.get("mode", "").strip().lower()
    if mode not in ("simple", "swahili"):
        return jsonify({"error": "mode must be 'simple' or 'swahili'"}), 400

    if READ_ONLY:
        return render_template("read_only_notice.html",
		               corpus_stats=_corpus_stats()), 403

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set."}), 503

    db = get_db()

    # ── Ensure cache table exists ─────────────────────────
    db.execute("""
        CREATE TABLE IF NOT EXISTS case_transforms (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            determination_id INTEGER NOT NULL,
            mode             TEXT NOT NULL,
            result_text      TEXT NOT NULL,
            created_at       TEXT DEFAULT (datetime('now')),
            UNIQUE(determination_id, mode)
        )
    """)
    db.commit()

    # ── Check cache ───────────────────────────────────────
    cached = db.execute(
        "SELECT result_text FROM case_transforms WHERE determination_id=? AND mode=?",
        (case_id, mode)
    ).fetchone()

    if cached:
        return jsonify({"text": cached["result_text"], "cached": True})

    # ── Fetch source text from determinations ─────────────
    row = db.execute(
        "SELECT merits, technical_summary, complainant_initials, respondent FROM determinations WHERE id=?",
        (case_id,)
    ).fetchone()

    if not row:
        return jsonify({"error": "Case not found"}), 404

    source = "\n\n".join(filter(None, [row["merits"], row["technical_summary"]]))
    title  = f"{row['complainant_initials'] or '?'} v {row['respondent']}"

    # ── Build prompt by mode ──────────────────────────────
    if mode == "simple":
        prompt = (
            f"The following is a legal case summary from a Kenyan data protection determination: "
            f"{title}.\n\nRewrite this in clear, simple English that a non-lawyer can understand. "
            f"Avoid legal jargon. Use short sentences. Preserve the key facts and outcome. "
            f"Do not add information not present in the source.\n\nSOURCE:\n{source}"
        )
    else:  # swahili
        prompt = (
            f"Tafsiri muhtasari huu wa kisheria kutoka uamuzi wa ulinzi wa data nchini Kenya "
            f"({title}) kwa Kiswahili sahihi na rahisi kuelewa. "
            f"Hifadhi ukweli muhimu na matokeo. Usijumuishe taarifa ambazo hazipo katika chanzo.\n\n"
            f"CHANZO:\n{source}"
        )

    # ── Call Anthropic API ────────────────────────────────
    try:
        payload = _json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read())

        result_text = data["content"][0]["text"].strip()

    except Exception as e:
        return jsonify({"error": f"API call failed: {e}"}), 502

    # ── Cache result ──────────────────────────────────────
    db.execute(
        "INSERT OR REPLACE INTO case_transforms (determination_id, mode, result_text) VALUES (?,?,?)",
        (case_id, mode, result_text)
    )
    db.commit()

    return jsonify({"text": result_text, "cached": False})


@app.route("/api/patterns/data")
def api_patterns_data():
    """
    Pre-aggregated data for patterns.html visualisations.
    Single payload — all charts render client-side from this one fetch.
    """
    db = get_db()

    quantum_by_sector = [dict(r) for r in db.execute("""
        SELECT sector,
               ROUND(AVG(quantum),0) AS avg_quantum,
               MAX(quantum)          AS max_quantum,
               COUNT(*)              AS case_count
        FROM determinations
        WHERE sector IS NOT NULL AND quantum > 0
        GROUP BY sector
        ORDER BY avg_quantum DESC
    """).fetchall()]

    outcomes_by_sector = [dict(r) for r in db.execute("""
        SELECT sector, outcome_type, COUNT(*) AS count
        FROM determinations
        WHERE sector IS NOT NULL AND outcome_type IS NOT NULL
        GROUP BY sector, outcome_type
        ORDER BY sector, count DESC
    """).fetchall()]

    cases_over_time = [dict(r) for r in db.execute("""
        SELECT strftime('%Y-%m', determination_date) AS year_month,
               COUNT(*) AS count
        FROM determinations
        WHERE determination_date IS NOT NULL
        GROUP BY year_month
        ORDER BY year_month
    """).fetchall()]

    sections_frequency = [dict(r) for r in db.execute("""
        SELECT s.section_number, s.title, COUNT(*) AS count
        FROM violations v
        JOIN sections s ON s.id = v.section_id
        GROUP BY v.section_id
        ORDER BY count DESC
        LIMIT 20
    """).fetchall()]

    enforcement_rate = [dict(r) for r in db.execute("""
        SELECT sector,
               SUM(enforcement_notice) AS en_count,
               COUNT(*)                AS total,
               ROUND(100.0 * SUM(enforcement_notice) / COUNT(*), 1) AS pct
        FROM determinations
        WHERE sector IS NOT NULL
        GROUP BY sector
        ORDER BY pct DESC
    """).fetchall()]

    # ── Victim latency buckets (breach date → complaint filed) ─
    latency_rows = db.execute("""
        SELECT CAST(
            (julianday(complaint_date) - julianday(breach_date))
        AS INTEGER) AS latency_days
        FROM determinations
        WHERE complaint_date IS NOT NULL AND breach_date IS NOT NULL
    """).fetchall()

    buckets = {"0–30": 0, "31–90": 0, "91–180": 0, "181–365": 0, "365+": 0}
    for row in latency_rows:
        d = row["latency_days"]
        if d is None or d < 0:
            continue
        if d <= 30:     buckets["0–30"]   += 1
        elif d <= 90:   buckets["31–90"]  += 1
        elif d <= 180:  buckets["91–180"] += 1
        elif d <= 365:  buckets["181–365"] += 1
        else:           buckets["365+"]   += 1
    victim_latency_buckets = [{"bucket": k, "count": v} for k, v in buckets.items()]

    # ── ODPC processing speed: complaint → determination (days) ─
    # Broken into fast (<60), standard (60–120), slow (121–180), extended (180+)
    speed_rows = db.execute("""
        SELECT
            CAST((julianday(determination_date) - julianday(complaint_date)) AS INTEGER) AS proc_days,
            sector
        FROM determinations
        WHERE determination_date IS NOT NULL AND complaint_date IS NOT NULL
    """).fetchall()

    speed_buckets = {"<60 days": 0, "60–120 days": 0, "121–180 days": 0, "180+ days": 0}
    speed_by_sector = {}
    for row in speed_rows:
        d = row["proc_days"]
        if d is None or d < 0:
            continue
        if d < 60:     speed_buckets["<60 days"]    += 1
        elif d <= 120: speed_buckets["60–120 days"] += 1
        elif d <= 180: speed_buckets["121–180 days"] += 1
        else:          speed_buckets["180+ days"]   += 1
        # Also accumulate avg per sector
        s = row["sector"] or "Unknown"
        speed_by_sector.setdefault(s, []).append(d)

    processing_speed = [{"bucket": k, "count": v} for k, v in speed_buckets.items()]
    avg_speed_by_sector = [
        {"sector": s, "avg_days": round(sum(vals) / len(vals), 1)}
        for s, vals in speed_by_sector.items()
        if vals
    ]
    avg_speed_by_sector.sort(key=lambda x: x["avg_days"])

    # ── COA → determination timeline (full case lifecycle) ───────
    lifecycle_rows = db.execute("""
        SELECT
            CAST((julianday(determination_date) - julianday(breach_date)) AS INTEGER) AS coa_to_det,
            sector
        FROM determinations
        WHERE determination_date IS NOT NULL AND breach_date IS NOT NULL
    """).fetchall()

    lifecycle_buckets = {"<180 days": 0, "180–365 days": 0, "1–2 years": 0, "2+ years": 0}
    for row in lifecycle_rows:
        d = row["coa_to_det"]
        if d is None or d < 0:
            continue
        if d < 180:     lifecycle_buckets["<180 days"]    += 1
        elif d <= 365:  lifecycle_buckets["180–365 days"] += 1
        elif d <= 730:  lifecycle_buckets["1–2 years"]    += 1
        else:           lifecycle_buckets["2+ years"]     += 1
    case_lifecycle = [{"bucket": k, "count": v} for k, v in lifecycle_buckets.items()]

    # ── Organisational layer breach frequency (from glitch) ──────
    layer_rows = db.execute("""
        SELECT system_layer, COUNT(*) AS count
        FROM glitch
        WHERE system_layer IS NOT NULL
        GROUP BY system_layer
        ORDER BY count DESC
    """).fetchall()

    # Split multi-layer entries e.g. "Application layer / Policy layer"
    layer_counts = {}
    for row in layer_rows:
        layers = [l.strip() for l in (row["system_layer"] or "").split("/") if l.strip()]
        for l in layers:
            layer_counts[l] = layer_counts.get(l, 0) + row["count"]
    layer_frequency = [{"layer": k, "count": v}
                       for k, v in sorted(layer_counts.items(), key=lambda x: -x[1])]

    # ── DSR laziness rate by sector ───────────────────────────────
    dsr_rows = db.execute("""
        SELECT sector,
               SUM(dsr_laziness_flag) AS lazy_count,
               COUNT(*) AS total,
               ROUND(100.0 * SUM(dsr_laziness_flag) / COUNT(*), 1) AS pct
        FROM determinations
        WHERE sector IS NOT NULL
        GROUP BY sector
        HAVING lazy_count > 0
        ORDER BY pct DESC
    """).fetchall()
    dsr_by_sector = [dict(r) for r in dsr_rows]

    # ── Citation graph ────────────────────────────────────────────
    citation_graph = _build_citation_graph(db)

    return jsonify({
        "quantum_by_sector":      quantum_by_sector,
        "outcomes_by_sector":     outcomes_by_sector,
        "cases_over_time":        cases_over_time,
        "sections_frequency":     sections_frequency,
        "enforcement_rate":       enforcement_rate,
        "victim_latency_buckets": victim_latency_buckets,
        "processing_speed":       processing_speed,
        "avg_speed_by_sector":    avg_speed_by_sector,
        "case_lifecycle":         case_lifecycle,
        "layer_frequency":        layer_frequency,
        "dsr_by_sector":          dsr_by_sector,
        "citation_graph":         citation_graph,
    })

@app.route("/api/wall/match")
def api_wall_match():
    """
    Civic situation → corpus precedents + plain-English AI assessment.

    Query params:
      q   — plain-language situation description (English or Swahili)

    Strategy:
      1. FTS5 search across det_fts (same as /api/search)
      2. Top 5 results passed to Anthropic as context
      3. AI returns a plain-English assessment grounded in those cases
      4. Both raw results and AI response returned to client

    Returns:
      {
        results:     [ case objects ],
        total:       int,
        ai_response: str | null,
      }
    """
    import urllib.request as _urllib_req
    import json as _json

    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": [], "total": 0, "ai_response": None})

    db = get_db()
    per_page = 5

    fts_exists = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='det_fts'"
    ).fetchone()

    if fts_exists:
        fts_q = _sanitise_fts_query(q)
        rows = db.execute(
            """
            SELECT d.*,
                   bm25(det_fts) AS relevance_score
            FROM det_fts
            JOIN determinations d ON d.id = det_fts.rowid
            WHERE det_fts MATCH ?
            ORDER BY relevance_score
            LIMIT ?
            """,
            [fts_q, per_page],
        ).fetchall()
    else:
        wildcard = f"%{q}%"
        rows = db.execute(
            """
            SELECT d.*, NULL AS relevance_score
            FROM determinations d
            WHERE d.merits LIKE ? OR d.technical_summary LIKE ?
               OR d.respondent LIKE ? OR d.important_flags LIKE ?
            ORDER BY d.determination_date DESC
            LIMIT ?
            """,
            [wildcard, wildcard, wildcard, wildcard, per_page],
        ).fetchall()

    results = []
    for row in rows:
        r = dict(row)
        results.append({
            "id":           r["id"],
            "title":        _case_display_title(r),
            "case_ref":     r.get("case_reference"),
            "sector":       r.get("sector"),
            "date":         (r.get("determination_date") or "")[:10],
            "outcome_type": r.get("outcome_type"),
            "quantum":      r.get("quantum"),
            "summary":      _truncate(r.get("merits"), 240),
        })

    # ── AI plain-English assessment ───────────────────────────
    ai_response = None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if api_key and results:
        case_summaries = "\n\n".join([
            f"Case {i+1}: {r['title']} ({r['sector'] or '—'}, {r['date'] or '—'})\n"
            f"Outcome: {r['outcome_type'] or '—'}"
            + (f" | Award: KES {int(r['quantum']):,}" if r.get('quantum') and float(r['quantum']) > 0 else "")
            + f"\nSummary: {r['summary'] or '—'}"
            for i, r in enumerate(results)
        ])

        prompt = _build_wall_match_prompt(q, case_summaries)

        try:
            payload = _json.dumps({
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 600,
                "messages": [{"role": "user", "content": prompt}]
            }).encode()

            req = _urllib_req.Request(
                "https://api.anthropic.com/v1/messages",
                data=payload,
                headers={
                    "Content-Type":      "application/json",
                    "x-api-key":         api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST"
            )

            with _urllib_req.urlopen(req, timeout=25) as resp:
                data = _json.loads(resp.read())
            ai_response = data["content"][0]["text"].strip()

        except Exception as e:
            ai_response = None  # fail silently — results still returned

    return jsonify({
        "results":     results,
        "total":       len(results),
        "ai_response": ai_response,
    })


@app.route("/api/wall/letter", methods=["POST"])
def api_wall_letter():
    """
    Generate a DPA-grounded demand letter for a data subject.

    POST body (JSON):
      right    — 'erasure' | 'access' | 'object' | 'correction' | 'informed'
      company  — respondent name string
      name     — complainant name/initials
      sector   — sector string (optional)
      context  — plain-language description of the issue
      language — 'english' (default) | 'swahili'

    Returns: { letter: str, citations: str }
    """
    import urllib.request as _urllib_req
    import json as _json

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set."}), 503

    data     = request.get_json(force=True) or {}
    right    = data.get("right", "").strip()
    company  = data.get("company", "").strip()
    name     = data.get("name", "").strip() or "the data subject"
    sector   = data.get("sector", "").strip()
    context  = data.get("context", "").strip()
    language = data.get("language", "english").strip().lower()

    if not right or not company:
        return jsonify({"error": "right and company are required."}), 400

    RIGHT_META = {
        "erasure":    ("Section 26(e)", "erasure of false or misleading personal data"),
        "access":     ("Section 26(b)", "access to personal data held about me"),
        "object":     ("Section 26(c)", "objection to processing of my personal data"),
        "correction": ("Section 26(d)", "correction of false or misleading personal data"),
        "informed":   ("Section 26(a)", "information about how my personal data is being used"),
    }

    section, right_desc = RIGHT_META.get(right, ("Section 26", "exercise of my data subject rights"))

    # Pull relevant precedent citations from corpus
    db = get_db()
    cite_rows = db.execute(
        """
        SELECT d.complainant_initials, d.respondent, d.case_reference, d.sector,
               d.outcome_type, d.quantum, d.determination_date
        FROM determinations d
        WHERE (d.sector = ? OR d.outcome_type LIKE '%compensation%')
          AND d.outcome_type != 'dismissed'
        ORDER BY d.determination_date DESC
        LIMIT 3
        """,
        (sector,)
    ).fetchall() if sector else []

    citations_text = ""
    if cite_rows:
        citations_text = "; ".join([
            f"{r['complainant_initials'] or '?'} v {r['respondent']} ({r['case_reference'] or r['determination_date'][:7]})"
            for r in cite_rows if r['case_reference'] or r['determination_date']
        ])

    if language == "swahili":
        prompt = f"""Andika barua rasmi ya kisheria kwa Kiswahili kwa niaba ya mtu anayeomba haki yao 
ya ulinzi wa data chini ya Sheria ya Ulinzi wa Data ya Kenya, 2019.

Mwandishi wa barua: {name}
Kampuni inayolengwa: {company}
Haki inayodaiwa: {right_desc} ({section} ya DPA 2019)
Maelezo ya tatizo: {context or 'Kampuni imeshindwa kutekeleza ombi la haki ya mada ya data.'}
{f'Sekta: {sector}' if sector else ''}

Barua inapaswa:
- Kuanza kwa "Kwa Afisa wa Ulinzi wa Data, {company}"
- Kunukuu {section} ya Sheria ya Ulinzi wa Data, 2019 kwa usahihi
- Kueleza haki inayodaiwa kwa lugha wazi na ya kisheria
- Kutoa muda wa siku 14 kwa kampuni kujibu
- Kumtahadharisha kwamba kushindwa kujibu kutasababisha malalamiko kwa ODPC
- Kuisha kwa "Wako kwa uaminifu" na nafasi ya sahihi
- Kuwa ya kiwango cha kisheria lakini kueleweka kwa mtu wa kawaida"""
    else:
        prompt = f"""Draft a formal legal demand letter under the Data Protection Act, 2019 (Kenya) 
on behalf of a data subject asserting their rights.

Letter author: {name}
Target company: {company}
Right being asserted: {right_desc} ({section} DPA 2019)
Issue description: {context or 'The company has failed to action a data subject rights request.'}
{f'Sector: {sector}' if sector else ''}
{f'Relevant precedents: {citations_text}' if citations_text else ''}

Requirements:
- Open with "Dear Data Protection Officer, {company},"
- Cite {section} of the Data Protection Act, 2019 precisely and accurately
- State the right being asserted clearly and the specific relief demanded
- Give the company 14 days to comply
- Warn that failure to comply will result in a complaint to the Office of the Data Protection Commissioner (ODPC)
- Reference the ODPC's enforcement powers under Section 58 DPA 2019
- Close with "Yours faithfully," and a signature block space
- Write in plain, clear English that a non-lawyer can understand — no unnecessary legal jargon
- Do not fabricate case citations or facts not provided above
- Keep the letter under 350 words"""

    try:
        payload = _json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 800,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()

        req = _urllib_req.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST"
        )

        with _urllib_req.urlopen(req, timeout=30) as resp:
            resp_data = _json.loads(resp.read())

        letter_text = resp_data["content"][0]["text"].strip()

    except Exception as e:
        return jsonify({"error": f"Letter generation failed: {e}"}), 502

    return jsonify({
        "letter":    letter_text,
        "citations": citations_text or None,
    })


@app.route("/api/wall/breach")
def api_wall_breach():
    """
    Search determinations by respondent name for the breach checker.

    Query params:
      q — company/respondent name

    Returns: { results: [...], total: int }
    """
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": [], "total": 0})

    db       = get_db()
    wildcard = f"%{q}%"

    rows = db.execute(
        """
        SELECT id, complainant_initials, complainant, respondent,
               case_reference, sector, determination_date,
               outcome_type, quantum, merits
        FROM determinations
        WHERE respondent LIKE ? OR respondent_2 LIKE ?
        ORDER BY determination_date DESC
        LIMIT 20
        """,
        [wildcard, wildcard],
    ).fetchall()

    results = []
    for row in rows:
        r = dict(row)
        results.append({
            "id":           r["id"],
            "title":        _case_display_title(r),
            "case_ref":     r.get("case_reference"),
            "sector":       r.get("sector"),
            "date":         (r.get("determination_date") or "")[:10],
            "outcome_type": r.get("outcome_type"),
            "quantum":      r.get("quantum"),
            "summary":      _truncate(r.get("merits"), 200),
        })

    return jsonify({"results": results, "total": len(results)})

@app.route("/api/wall/glossary")
def api_wall_glossary():
    """
    Returns deduplicated technical terms from the corpus for the DSR lingo panel.
    Deduplicates by term (case-insensitive), keeping the most common definition.

    Returns: { terms: [ { term, definition, category } ] }
    """
    db = get_db()

    rows = db.execute(
        """
        SELECT term,
               definition,
               category,
               COUNT(*) AS freq
        FROM technical_terms
        WHERE term IS NOT NULL AND definition IS NOT NULL
        GROUP BY LOWER(term)
        ORDER BY freq DESC, term ASC
        LIMIT 200
        """
    ).fetchall()

    terms = [
        {
            "term":       r["term"],
            "definition": r["definition"],
            "category":   r["category"] or "general",
        }
        for r in rows
    ]

    return jsonify({"terms": terms, "total": len(terms)})


@app.route("/api/wall/dyk")
def api_wall_dyk():
    """
    Returns Did You Know insights for the civic panel.

    Priority: rows from corpus_insights table if it exists.
    Fallback: compute live from corpus (5 pre-built queries).

    Returns: { insights: [ { fact, source } ] }
    """
    db = get_db()

    # Try corpus_insights table first
    has_insights = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='corpus_insights'"
    ).fetchone()

    if has_insights:
        rows = db.execute(
            "SELECT fact, source FROM corpus_insights ORDER BY RANDOM() LIMIT 6"
        ).fetchall()
        if rows:
            return jsonify({"insights": [dict(r) for r in rows]})

    # Live fallback — compute from corpus
    insights = []

    # 1. Top sector
    top_sector = db.execute(
        "SELECT sector, COUNT(*) AS c FROM determinations WHERE sector IS NOT NULL "
        "GROUP BY sector ORDER BY c DESC LIMIT 1"
    ).fetchone()
    if top_sector:
        insights.append({
            "fact":   f"<strong>{top_sector['sector']} is the most complained-about sector</strong> with {top_sector['c']} ODPC determinations on record in the /dapa corpus.",
            "source": "/dapa corpus — sector distribution"
        })

    # 2. DSR laziness rate
    dsr_total = db.execute("SELECT COUNT(*) FROM determinations").fetchone()[0]
    dsr_lazy  = db.execute("SELECT COUNT(*) FROM determinations WHERE dsr_laziness_flag = 1").fetchone()[0]
    if dsr_total and dsr_lazy:
        pct = round(100 * dsr_lazy / dsr_total)
        insights.append({
            "fact":   f"<strong>In {pct}% of cases</strong> in the corpus, the respondent had already ignored the complainant's data rights request before the ODPC complaint was filed. This 'DSR laziness' is treated as an aggravating factor in compensation decisions.",
            "source": "/dapa corpus — dsr_laziness_flag analysis"
        })

    # 3. Unrepresented complainants
    unrep = db.execute(
        "SELECT COUNT(*) FROM determinations WHERE complainant_represented IN ('no','unlikely')"
    ).fetchone()[0]
    if unrep and dsr_total:
        pct2 = round(100 * unrep / dsr_total)
        insights.append({
            "fact":   f"<strong>You do not need a lawyer.</strong> Approximately {pct2}% of complainants in the /dapa corpus were unrepresented. The ODPC complaint process is designed to be accessible to ordinary citizens at no cost.",
            "source": "/dapa corpus — complainant_represented field"
        })

    # 4. Average quantum
    avg_q = db.execute(
        "SELECT ROUND(AVG(quantum), 0) AS avg_q FROM determinations WHERE quantum > 0"
    ).fetchone()
    if avg_q and avg_q["avg_q"]:
        insights.append({
            "fact":   f"<strong>The average compensation award</strong> in cases where the ODPC awarded damages is KES {int(avg_q['avg_q']):,}. The highest single award in the corpus is {db.execute('SELECT MAX(quantum) FROM determinations').fetchone()[0] and 'KES {:,}'.format(int(db.execute('SELECT MAX(quantum) FROM determinations').fetchone()[0]))}.",
            "source": "/dapa corpus — quantum field analysis"
        })

    # 5. First port of call
    fpc = db.execute(
        "SELECT COUNT(*) FROM determinations WHERE first_port_of_call = 1"
    ).fetchone()[0]
    if fpc and dsr_total:
        pct3 = round(100 * fpc / dsr_total)
        insights.append({
            "fact":   f"<strong>{pct3}% of complainants</strong> went straight to the ODPC without first contacting the company. The ODPC notes this — contacting the company first and keeping a record of their response (or silence) strengthens your complaint.",
            "source": "/dapa corpus — first_port_of_call field"
        })

    # 6. Enforcement rate
    en_count = db.execute(
        "SELECT COUNT(*) FROM determinations WHERE enforcement_notice = 1"
    ).fetchone()[0]
    if en_count and dsr_total:
        pct4 = round(100 * en_count / dsr_total)
        insights.append({
            "fact":   f"<strong>The ODPC has issued enforcement notices in {pct4}% of cases</strong> — binding orders requiring the company to change its data practices. Failure to comply with an enforcement notice is a criminal offence.",
            "source": "/dapa corpus — enforcement_notice field"
        })

    return jsonify({"insights": insights[:6]})

@app.route("/api/wall/dsr-stats")
def api_wall_dsr_stats():
    db = get_db()
    row = db.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(dsr_laziness_flag) AS flagged,
            ROUND(AVG(CASE WHEN dsr_laziness_flag = 1 THEN quantum END), 0) AS avg_award_flagged,
            ROUND(AVG(CASE WHEN dsr_laziness_flag = 0 THEN quantum END), 0) AS avg_award_unflagged
        FROM determinations
    """).fetchone()
    d = dict(row)
    d["pct_flagged"] = round((d["flagged"] or 0) / d["total"] * 100, 1) if d["total"] else 0
    return jsonify(d)

@app.route("/appeals")
def appeals():
    abort(503)

@app.route("/about")
def about():
    abort(503)

@app.route("/disclaimer")
def disclaimer():
    abort(503)

@app.route("/methodology")
def methodology():
    abort(503)

# ════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ════════════════════════════════════════════════════════════

def _corpus_stats():
    """
    Return a dict of top-level corpus statistics.
    Uses lru_cache via the module-level cached wrapper below.
    Called by index route and /api/corpus-stats.
    """
    try:
        db = get_db()

        total = db.execute("SELECT COUNT(*) FROM determinations").fetchone()[0]

        sectors_row = db.execute(
            "SELECT COUNT(DISTINCT sector) FROM determinations WHERE sector IS NOT NULL"
        ).fetchone()
        sectors = sectors_row[0] if sectors_row else 0

        years = db.execute(
            """
            SELECT MIN(strftime('%Y', determination_date)),
                   MAX(strftime('%Y', determination_date))
            FROM determinations WHERE determination_date IS NOT NULL
            """
        ).fetchone()
        year_range = f"{years[0]}–{years[1]}" if years and years[0] else "2023–"

        max_q_row = db.execute("SELECT MAX(quantum) FROM determinations").fetchone()
        max_q     = max_q_row[0] if max_q_row and max_q_row[0] else 0
        highest   = f"{float(max_q/1000000)}M" if max_q >= 1000000 else f"{int(max_q/1000)}K" if max_q >= 1000 else str(int(max_q))

        total_q   = db.execute("SELECT SUM(quantum) FROM determinations WHERE quantum > 0").fetchone()[0]
        total_quantum = f"{float(total_q/1000000):.1f}M" if total_q >= 1000000 else f"{float(total_q/1000):.1f}k" if total_q >= 1000 else str(int(total_q))
        en_count  = db.execute("SELECT COUNT(*) FROM determinations WHERE enforcement_notice = 1").fetchone()[0]

        by_sector = {}
        for row in db.execute(
            "SELECT sector, COUNT(*) AS c FROM determinations "
            "WHERE sector IS NOT NULL GROUP BY sector ORDER BY c DESC"
        ).fetchall():
            by_sector[row["sector"]] = row["c"]

        by_outcome = {}
        for row in db.execute(
            "SELECT outcome_type, COUNT(*) AS c FROM determinations "
            "WHERE outcome_type IS NOT NULL GROUP BY outcome_type"
        ).fetchall():
            by_outcome[row["outcome_type"]] = row["c"]

        return {
            "total_cases":   total,
            "sectors":       sectors,
            "year_range":    year_range,
            "highest_award": highest,
            "by_sector":     by_sector,
            "by_outcome":    by_outcome,
            "total_award":   total_quantum,
            "en_count":	     en_count,       
        }
    except Exception:
        # Database not yet initialised — return placeholder values
        return {
            "total_cases":   0,
            "sectors":       0,
            "year_range":    "2023–",
            "highest_award": "—",
            "by_sector":     {},
            "by_outcome":    {},
            "total_award":   "_",
            "en_count":	     "_",	 	   
        }


def _sector_counts(db):
    rows = db.execute(
        "SELECT sector, COUNT(*) AS c FROM determinations "
        "WHERE sector IS NOT NULL GROUP BY sector"
    ).fetchall()
    return {r["sector"]: r["c"] for r in rows}


def _highest_quantum_in_set(db, where_clause, params):
    row = db.execute(
        f"SELECT MAX(quantum) FROM determinations d {where_clause}", params
    ).fetchone()
    return row[0] if row and row[0] else None


def _case_display_title(r):
    """
    Construct display title: "Complainant v Respondent"
    Falls back to respondent only if complainant is absent.
    """
    complainant = r.get("complainant_initials") or r.get("complainant")
    respondent  = r.get("respondent", "")
    if complainant:
        return f"{complainant} v {respondent}"
    return respondent


def _extract_theme(important_flags_text):
    """
    Pull the first strong sentence from important_flags for card display.
    important_flags is pipe-separated: "FLAG_NAME|description|FLAG2|…"
    Returns the first non-flag-name segment, truncated.
    """
    if not important_flags_text:
        return None
    parts = important_flags_text.split("|")
    for part in parts:
        part = part.strip()
        # Skip if it looks like an ALL_CAPS flag name
        if part and not re.match(r'^[A-Z_0-9]+$', part):
            return _truncate(part, 180)
    return None


def _truncate(text, max_len):
    if not text:
        return None
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "…"


def _sanitise_fts_query(q):
    """
    Convert free-text into an FTS5-safe OR query across meaningful tokens.
    Strips common stopwords so 'my photo was used without my permission'
    becomes photo OR used OR permission — a phrase match was too strict
    for natural-language civic queries.
    """
    STOPWORDS = {
        'a','an','the','my','your','his','her','their','our','is','was','were',
        'be','been','being','to','of','in','on','at','by','for','with','without',
        'and','or','but','if','as','it','this','that','i','you','he','she','they',
        'we','do','does','did','has','have','had','not','no','so','can','could',
        'would','should','will','about','from','me','us','them'
    }
    clean = re.sub(r'["\(\)\*\:\^]', ' ', q).strip()
    if not clean:
        return '""'
    tokens = [t for t in clean.lower().split() if t not in STOPWORDS and len(t) > 1]
    if not tokens:
        tokens = clean.split()  # fallback if everything was a stopword
    # OR query — FTS5 syntax: token1 OR token2 OR token3
    return ' OR '.join(f'"{t}"' for t in tokens)


def _infer_matched_fields(r, q):
    """
    After a search, surface which fields the query matched for the
    match-field badges in Explore. Heuristic: check if q tokens
    appear in each field.
    """
    if not q:
        return []
    tokens = q.lower().split()
    matches = []
    field_map = {
        "merits":             "Merits",
        "technical_summary":  "Technical Analysis",
        "important_flags":    "Flags",
        "respondent":         "Respondent",
        "complainant":        "Complainant",
        "sector":             "Sector",
        "case_reference":     "Case Ref",
        "complaint_number":   "Complaint No.",
        "defence_category":   "Defence",
    }
    for field, label in field_map.items():
        val = (r.get(field) or "").lower()
        if any(tok in val for tok in tokens):
            matches.append(label)
    return matches[:4]  # cap at 4 badges for display cleanliness


def _build_citation_graph(db):
    """
    Build a node + edge list for the cross-case citation graph
    in patterns.html. Only includes ODPC self-citations.
    """
    nodes_raw = db.execute(
        "SELECT id, respondent, complainant_initials, sector FROM determinations"
    ).fetchall()

    edges_raw = db.execute(
    """
    SELECT cc.citing_case_id, cc.cited_case_id
    FROM cross_citations cc
    WHERE cc.cited_type = 'odpc_prior'
      AND cc.cited_case_id IS NOT NULL
    """
    ).fetchall()


    nodes = [
        {
            "id":     r["id"],
            "label":  f"{r['complainant_initials'] or '?'} v {r['respondent']}",
            "sector": r["sector"],
        }
        for r in nodes_raw
    ]

    edges = [
    {"source": e["citing_case_id"], "target": e["cited_case_id"]}   # was "target_ref"
    for e in edges_raw
    ]

    return {"nodes": nodes, "edges": edges}

def _build_wall_match_prompt(user_situation, case_summaries):
    """
    Build the plain-English civic assessment prompt for /api/wall/match.
    Grounded strictly in the case summaries provided — no hallucination.
    """
    return f"""You are a civic data rights advisor for /dapa, a Kenyan data protection intelligence platform.
	A member of the public has described a situation involving their personal data.
	You have been given the most relevant ODPC determination summaries from the corpus.

	Your job: give them a clear, plain-English assessment of where they stand — what the ODPC has decided in similar cases, what their likely rights are, and what they should do next.

	Rules:
	- Base your response ONLY on the case summaries provided. Do not invent cases or cite cases not shown.
	- Write in plain English — no legal jargon unless immediately explained.
	- Be direct and specific: name the outcome patterns you see.
	- Keep it under 200 words.
	- End with one clear next step.
	- Do not mention /dapa or that you are an AI.

	PERSON'S SITUATION:
	{user_situation}
	
	RELEVANT ODPC CASES FROM CORPUS:
	{case_summaries}

	Provide your assessment now:"""



# ════════════════════════════════════════════════════════════
# FTS5 VIRTUAL TABLE CREATION UTILITY
# ════════════════════════════════════════════════════════════

def create_fts_index():
    """
    Create the FTS5 full-text search index for Pweza.

    Strategy: a VIEW (det_search_view) aggregates all text worth searching
    per determination — merits, technical_summary, important_flags,
    plus all technical_terms definitions (via GROUP_CONCAT). FTS5 indexes
    this view using an external-content table approach.

    Because FTS5 external-content tables cannot use a VIEW directly in
    older SQLite builds, we materialise the view into a shadow table
    (det_search_shadow) on each rebuild. This keeps the index in sync
    with both determinations and technical_terms.

    Run once after initial ingestion:
        python3 -c "from app import create_fts_index; create_fts_index()"

    After each subsequent importas.py run:
        python3 -c "from app import rebuild_fts; rebuild_fts()"
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")

        # 1. Shadow table — materialised union of all searchable text
        conn.execute("DROP TABLE IF EXISTS det_search_shadow;")
        conn.execute("""
            CREATE TABLE det_search_shadow AS
            SELECT
                d.id                                        AS rowid,
                COALESCE(d.merits, '')                      AS merits,
                COALESCE(d.technical_summary, '')           AS technical_summary,
                COALESCE(d.important_flags, '')             AS important_flags,
                COALESCE(d.respondent, '')                  AS respondent,
                COALESCE(d.complainant, '')                 AS complainant,
                COALESCE(d.complainant_initials, '')        AS complainant_initials,
                COALESCE(d.sector, '')                      AS sector,
                COALESCE(d.case_reference, '')              AS case_reference,
                COALESCE(d.complaint_number, '')            AS complaint_number,
                COALESCE(d.defence_category, '')            AS defence_category,
                COALESCE(
                    GROUP_CONCAT(t.term || ': ' || t.definition, ' | '), ''
                )                                           AS glossary_terms
            FROM determinations d
            LEFT JOIN technical_terms t ON t.determination_id = d.id
            GROUP BY d.id;
        """)

        # 2. FTS5 virtual table over the shadow table
        conn.execute("DROP TABLE IF EXISTS det_fts;")
        conn.execute("""
            CREATE VIRTUAL TABLE det_fts
            USING fts5(
                merits,
                technical_summary,
                important_flags,
                respondent,
                complainant,
                complainant_initials,
                sector,
                case_reference,
                complaint_number,
                defence_category,
                glossary_terms,
                content='det_search_shadow',
                content_rowid='rowid',
                tokenize='porter unicode61'
            );
        """)

        # 3. Populate
        conn.execute("INSERT INTO det_fts(det_fts) VALUES('rebuild');")
        conn.commit()

    count = sqlite3.connect(DB_PATH).execute(
        "SELECT COUNT(*) FROM det_fts"
    ).fetchone()[0]
    print(f"FTS5 index built — {count} determinations indexed.")


def rebuild_fts():
    """
    Refresh the FTS index after new cases are imported.
    Call this after every importas.py run.

    Usage:
        python3 -c "from app import rebuild_fts; rebuild_fts()"
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")

        # Re-materialise shadow table
        conn.execute("DELETE FROM det_search_shadow;")
        conn.execute("""
            INSERT INTO det_search_shadow
            SELECT
                d.id,
                COALESCE(d.merits, ''),
                COALESCE(d.technical_summary, ''),
                COALESCE(d.important_flags, ''),
                COALESCE(d.respondent, ''),
                COALESCE(d.complainant, ''),
                COALESCE(d.complainant_initials, ''),
                COALESCE(d.sector, ''),
                COALESCE(d.case_reference, ''),
                COALESCE(d.complaint_number, ''),
                COALESCE(d.defence_category, ''),
                COALESCE(GROUP_CONCAT(t.term || ': ' || t.definition, ' | '), '')
            FROM determinations d
            LEFT JOIN technical_terms t ON t.determination_id = d.id
            GROUP BY d.id;
        """)

        # Rebuild FTS index from shadow
        conn.execute("INSERT INTO det_fts(det_fts) VALUES('rebuild');")
        conn.commit()
    print("FTS index rebuilt.")


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html", active_nav=""), 404


@app.errorhandler(503)
def under_construction(e):
    stats = _corpus_stats()
    return render_template("503.html", active_nav="", corpus_stats=stats), 503


# ════════════════════════════════════════════════════════════
# RUN
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
