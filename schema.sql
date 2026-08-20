-- ============================================================
-- /dapa — ODPC Regulatory Intelligence
-- schema.sql v2 — expanded for full intake structure
-- ============================================================
--
-- MANDATORY fields (NOT NULL or enforced by importas.py):
--   determinations: case_reference, respondent, determination_date
--
-- OPTIONAL fields: everything else — NULL is a valid state
--   representing "not recorded" not "absent"
--
-- MIGRATION from v1:
--   If upgrading an existing odpc.db run migrate_v1_to_v2.sql
--   For a fresh database just run this file.
--
-- Usage (fresh):
--   python3 -c "
--   import sqlite3
--   conn = sqlite3.connect('odpc.db')
--   with open('schema.sql') as f: conn.executescript(f.read())
--   conn.close(); print('done')
--   "
-- ============================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode  = WAL;

-- ── Sections ─────────────────────────────────────────────────────────────────
-- Sections of the Data Protection Act, 2019 referenced in violations.
-- Seeded by seed_sections.py. Do not drop — violations FK to this table.
CREATE TABLE IF NOT EXISTS sections (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    section_number    TEXT NOT NULL UNIQUE,  -- e.g. "25", "26(a)", "30(1)(b)"
    act_name          TEXT DEFAULT 'Data Protection Act, 2019',
    title             TEXT,                  -- short human-readable description
    thematic_category TEXT,                  -- "Consent", "Data Subject Rights"
    created_at        TEXT DEFAULT (datetime('now'))
);

-- ── Determinations ───────────────────────────────────────────────────────────
-- Core table. One row per determination (or consolidated group).
-- Fields are grouped: identity → parties → dates → outcome → intelligence
CREATE TABLE IF NOT EXISTS determinations (

    id                      INTEGER PRIMARY KEY AUTOINCREMENT,

    -- ── Identity ─────────────────────────────────────────────────────────
    -- MANDATORY
    case_reference          TEXT UNIQUE NOT NULL,
        -- Primary reference. Use serial ref (ODPC/CIE/CON/...) for
        -- consolidated cases. Use CIE/CON... for single complaints.
    complaint_number             TEXT,
        -- The ODPC/COMP serial number if distinct from case_reference
    consolidated_complaints       TEXT,
        -- Comma-separated original complaint numbers for consolidated cases
        -- e.g. "ODPC/COMP/0063/2025, ODPC/COMP/0064/2025"
    ingestion_status        TEXT DEFAULT 'complete',
        -- 'complete' | 'incomplete' | 'placeholder'
        -- Use 'incomplete' where source doc was unavailable at import time

    -- ── Parties ──────────────────────────────────────────────────────────
    complainant             TEXT,            -- Full name(s) or initials									
    complainant_initials    TEXT,            -- main complainant e.g. "AKO", "C.M.P"
    complainant_2	    TEXT,	     -- Full names of alternative complainants or initials
    respondent              TEXT NOT NULL,   -- MANDATORY — primary respondent
    respondent_2            TEXT,            -- Second respondent name if present
    breach_actor_role       TEXT,    	     -- role of the breach actor in the company
    breach_medium     	    TEXT,	     -- tech substrate that propagated breach				
    respondent_type         TEXT,
        -- 'private_company' | 'public_body' | 'individual' | 'ngo'
    sector                  TEXT,            -- controlled vocabulary (see SECTORS in app.py)

    -- ── Dates ────────────────────────────────────────────────────────────
    -- MANDATORY
    determination_date      TEXT NOT NULL,   -- ISO 8601: YYYY-MM-DD

    complaint_date          TEXT,            -- Date ODPC received the complaint
    breach_date             TEXT,            -- Cause of action date (COA)
    coa_type                TEXT,
        -- 'confirmed' | 'approximate' | 'inferred'
        -- Confidence level of the breach_date value

    -- ── Outcome ──────────────────────────────────────────────────────────
    quantum                 REAL DEFAULT 0,  -- Compensation awarded (KES)
    quantum_requested       REAL DEFAULT 0,  -- What the complainant asked for
    outcome_type            TEXT,
        -- 'compensation' | 'enforcement_notice' | 'dismissed' |
        -- 'resolved' | 'compensation_and_en' | 'prosecution_recommended'
    conditional_notice      INTEGER DEFAULT 0,   -- BOOLEAN: 1 = CN issued		
    enforcement_notice      INTEGER DEFAULT 0,   -- BOOLEAN: 1 = EN issued
    enforcement_notice_r2   INTEGER DEFAULT 0,   -- BOOLEAN: EN against R2
    enforcement_notice_content TEXT,             -- What the EN requires, if stated
    penalty_notice	    INTEGER DEFAULT 0,   -- BOOLEAN 1 = PN issued
    prosecution_recommended INTEGER DEFAULT 0,   -- BOOLEAN: s61 prosecution rec.
    parallel_proceedings    INTEGER DEFAULT 0,   -- BOOLEAN: court/DCI etc. active
    incident		    TEXT,		 -- Unauthorised data processing etc	

    -- ── Process intelligence ─────────────────────────────────────────────
    data_status_at_det      TEXT,
        -- 'taken_down_before_filing' | 'taken_down_during_process' |
        -- 'still_live_at_determination' | 'rectified_during_process' |
        -- 'data_used_in_proceedings' | 'na'
    remediation_timing      TEXT,
        -- 'pre_complaint' | 'during_process_post_notification' |
        -- 'claimed_unverified' | 'none_pre_determination' | 'na'
    investigation_method    TEXT,
        -- 'papers_only' | 'site_visit' | 'technical_audit' |
        -- 'third_party_summons' | 'papers_only+third_party_summons'
    complainant_represented TEXT,
        -- 'yes' | 'no' | 'probable' | 'unlikely'
    defence_category        TEXT,            -- Free text — key defence types used
    personal_liability      TEXT,
        -- 'no' | 'yes' | 'recommended' | 'prosecution_recommended'

    -- ── Flags ────────────────────────────────────────────────────────────
    dsr_laziness_flag       INTEGER DEFAULT 0,
        -- BOOLEAN: 1 = respondent failed to action DSR prior to ODPC
    first_port_of_call      INTEGER DEFAULT 0,
        -- BOOLEAN: 1 = complainant went to ODPC without first contacting respondent
    reg21_contract_vintage  TEXT,
        -- 'pre_dpa' | 'post_dpa' — for third-party data sharing agreements

    -- ── Narrative intelligence ───────────────────────────────────────────
    introduction	    TEXT,	     -- Introductory sentence summarizes case		
    merits                  TEXT,            -- Prose summary of the case
    technical_summary       TEXT,            -- Legal-technical analysis
    important_flags         TEXT,            -- Free text for IMPORTANT_FLAG entries
        -- Pipe-separated: "UNVERIFIED_DISCIPLINARY_CLAIM|PRE_DPA_CONTRACT"

    -- ── Metadata ─────────────────────────────────────────────────────────
    updated_at              TEXT DEFAULT (datetime('now')),
    created_at              TEXT DEFAULT (datetime('now'))
);

-- ── Violations ───────────────────────────────────────────────────────────────
-- One determination → many violations.
-- Each row is one section violation with severity and recurrence flag.
CREATE TABLE IF NOT EXISTS violations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    determination_id    INTEGER NOT NULL
                            REFERENCES determinations(id) ON DELETE CASCADE,
    section_id      	INTEGER
                            REFERENCES sections(id) ON DELETE SET NULL,
    violation_type      TEXT,               -- human-readable description
    severity            REAL DEFAULT 0,     -- 0–5 scale
    recurring_pattern   INTEGER DEFAULT 0,  -- BOOLEAN: 1 = seen across cases
    section_number_raw	TEXT,  -- denormalised from source, used as fallback display
    created_at          TEXT DEFAULT (datetime('now'))
);

-- ── Glitch ───────────────────────────────────────────────────────────────────
-- Curated technical signal layer — the moat.
-- Three analytical bands per entry.
CREATE TABLE IF NOT EXISTS glitch (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    determination_id    INTEGER
                            REFERENCES determinations(id) ON DELETE SET NULL,
    case_title          TEXT NOT NULL,
    incident_type       TEXT,           -- controlled vocabulary
    system_layer        TEXT,           -- controlled vocabulary
    technical_failure   TEXT,           -- band 1: what failed technically
    regulatory_signal   TEXT,           -- band 2: what the ODPC is signalling
    macro_pattern       TEXT,           -- band 3: cross-case systemic insight
    published_date      TEXT,           -- ISO 8601 — mirrors determination_date
    created_at          TEXT DEFAULT (datetime('now'))
);

-- ── Section-Regulation Pairs ──────────────────────────────────────────────────
-- Records the interpretive pairings between DPA sections and subsidiary regs.
-- These pairings are the legislative cross-reference layer.
CREATE TABLE IF NOT EXISTS section_reg_pairs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    determination_id    INTEGER NOT NULL
                            REFERENCES determinations(id) ON DELETE CASCADE,
    section_id          INTEGER
                            REFERENCES sections(id) ON DELETE SET NULL,
    section_number      TEXT,               -- denormalised for readability
    regulation_ref      TEXT,               -- e.g. "Regulation 14(1) General Regs"
    pairing_type        TEXT DEFAULT 'explicit',
        -- 'explicit' = ODPC stated both | 'inferred' = reconstructed from context
    interpretive_note   TEXT,               -- what the pairing establishes
    created_at          TEXT DEFAULT (datetime('now'))
);

-- ── Technical Terms ───────────────────────────────────────────────────────────
-- Per-case glossary entries. Powers the ambient tooltip glossary layer.
-- Terms that appear across multiple cases are deduplicated at display time.
CREATE TABLE IF NOT EXISTS technical_terms (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    determination_id    INTEGER
                            REFERENCES determinations(id) ON DELETE CASCADE,
    term                TEXT NOT NULL,
    definition          TEXT NOT NULL,      -- one sentence for tooltip display
    full_definition     TEXT,               -- paragraph for /glossary page
    category            TEXT,  -- 'system' | 'legal' | 'procedural' |
			       -- 'product' | 'regulatory'
    created_at          TEXT DEFAULT (datetime('now'))
);


-- __ Important Flags ___________________________________________________________
CREATE TABLE IF NOT EXISTS important_flags (
    id			INTEGER PRIMARY KEY AUTOINCREMENT,
    determination_id	INTEGER
			    REFERENCES determinations(id) ON DELETE CASCADE,
    name		TEXT NOT NULL,
    text		TEXT,
    created_at		TEXT DEFAULT (datetime('now'))
);


-- ── Cross Citations ───────────────────────────────────────────────────────────
-- Records when a determination cites another case or authority.
-- Includes both Kenyan ODPC self-citations and foreign jurisdiction citations.
CREATE TABLE IF NOT EXISTS cross_citations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    citing_case_id      INTEGER NOT NULL
                            REFERENCES determinations(id) ON DELETE CASCADE,
    cited_case_id       INTEGER REFERENCES determinations(id) ON DELETE SET NULL,
    cited_reference     TEXT NOT NULL,
        -- e.g. "WM Morrison Supermarkets PLC v Various Claimants [2020] UKSC 12"
        -- or "ODPC/CONF/1/7/4 VOL 1(60)"
    cited_type          TEXT,
        -- 'odpc_prior' | 'kenyan_court' | 'foreign_jurisdiction' | 'legislation'
    jurisdiction        TEXT,               -- e.g. "Kenya", "UK", "EU"
    citation_context    TEXT,               -- why it was cited, what it established
    created_at          TEXT DEFAULT (datetime('now'))
);


-- ── Regulations ───────────────────────────────────────────────────────────────
-- Reference table for subsidiary legislation.
-- Seeded with the three core regulation sets.
CREATE TABLE IF NOT EXISTS regulations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    regulation  TEXT NOT NULL UNIQUE,	 --"Regulation 14(1) General Regs 2021"
    short_name  TEXT,                    --"Reg 14(1)"
    description TEXT,
    reg_set     TEXT,			 -- 'enforcement_regs' | 'general_regs' | 'registration_regs'
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Seed core regulation sets
INSERT OR IGNORE INTO regulations (regulation, short_name, description, reg_set) VALUES
    ('Regulation 4 Enforcement Regulations 2021',   'Reg 4 Enf.',   'Complaint filing basis',                         'enforcement_regs'),
    ('Regulation 9 Enforcement Regulations 2021',   'Reg 9 Enf.',   'Consolidation of related complaints',            'enforcement_regs'),
    ('Regulation 11 Enforcement Regulations 2021',  'Reg 11 Enf.',  'Notification of respondent',                     'enforcement_regs'),
    ('Regulation 13 Enforcement Regulations 2021',  'Reg 13 Enf.',  'Investigations procedure',                       'enforcement_regs'),
    ('Regulation 14 Enforcement Regulations 2021',  'Reg 14 Enf.',  'Determination and remedies',                     'enforcement_regs'),
    ('Regulation 16 Enforcement Regulations 2021',  'Reg 16 Enf.',  'Enforcement Notice procedure',                   'enforcement_regs'),
    ('Regulation 6 General Regulations 2021',       'Reg 6 Gen.',   'Lawful basis for processing',                    'general_regs'),
    ('Regulation 7 General Regulations 2021',       'Reg 7 Gen.',   'Right to restrict processing',                   'general_regs'),
    ('Regulation 8 General Regulations 2021',       'Reg 8 Gen.',   'Right to object to processing',                  'general_regs'),
    ('Regulation 9 General Regulations 2021',       'Reg 9 Gen.',   'Right of access — data subject',                 'general_regs'),
    ('Regulation 12 General Regulations 2021',      'Reg 12 Gen.',  'Right to erasure conditions',                    'general_regs'),
    ('Regulation 14 General Regulations 2021',      'Reg 14 Gen.',  'Commercial purposes — definition',               'general_regs'),
    ('Regulation 16 General Regulations 2021',      'Reg 16 Gen.',  'Opt-out mechanism for direct marketing',         'general_regs'),
    ('Regulation 21 General Regulations 2021',      'Reg 21 Gen.',  'Data sharing agreements — third parties',        'general_regs');

-- ── SECTION REG PAIRS ───────────────────────────────────────────────────────── 
ALTER TABLE section_reg_pairs ADD COLUMN regulation_section_id INTEGER REFERENCES sections(id) ON DELETE SET NULL;


-- ── Ontology Versions ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ontology_versions (
    version     TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    changed_at  TEXT DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO ontology_versions (version, description) VALUES
    ('v0', 'Initial scheme. 2023–present. Banking baseline established.'),
    ('v1', 'Expanded intake structure. New fields: breach_date, enforcement_notice, prosecution_recommended, section_reg_pairs, technical_terms, cross_citations.');

-- ── Triggers ─────────────────────────────────────────────────────────────────
CREATE TRIGGER IF NOT EXISTS determinations_updated_at
AFTER UPDATE ON determinations
FOR EACH ROW
BEGIN
    UPDATE determinations
    SET updated_at = datetime('now')
    WHERE id = OLD.id;
END;

-- ── Indexes ───────────────────────────────────────────────────────────────────

-- determinations
CREATE INDEX IF NOT EXISTS idx_det_date
    ON determinations(determination_date DESC);
CREATE INDEX IF NOT EXISTS idx_det_breach_date
    ON determinations(breach_date);
CREATE INDEX IF NOT EXISTS idx_det_complaint_date
    ON determinations(complaint_date);
CREATE INDEX IF NOT EXISTS idx_det_sector
    ON determinations(sector);
CREATE INDEX IF NOT EXISTS idx_det_respondent
    ON determinations(respondent);
CREATE INDEX IF NOT EXISTS idx_det_outcome
    ON determinations(outcome_type);
CREATE INDEX IF NOT EXISTS idx_det_en
    ON determinations(enforcement_notice);
CREATE INDEX IF NOT EXISTS idx_det_prosecution
    ON determinations(prosecution_recommended);
CREATE INDEX IF NOT EXISTS idx_det_quantum
    ON determinations(quantum DESC);
CREATE INDEX IF NOT EXISTS idx_det_respondent_type
    ON determinations(respondent_type);
CREATE INDEX IF NOT EXISTS idx_det_status
    ON determinations(ingestion_status);

-- violations
CREATE INDEX IF NOT EXISTS idx_viol_determination
    ON violations(determination_id);
CREATE INDEX IF NOT EXISTS idx_viol_section
    ON violations(section_id);
CREATE INDEX IF NOT EXISTS idx_viol_recurring
    ON violations(recurring_pattern);
CREATE INDEX IF NOT EXISTS idx_viol_severity
    ON violations(severity DESC);

-- glitch
CREATE INDEX IF NOT EXISTS idx_glitch_determination
    ON glitch(determination_id);
CREATE INDEX IF NOT EXISTS idx_glitch_incident_type
    ON glitch(incident_type);
CREATE INDEX IF NOT EXISTS idx_glitch_system_layer
    ON glitch(system_layer);
CREATE INDEX IF NOT EXISTS idx_glitch_published
    ON glitch(published_date DESC);

-- section_reg_pairs
CREATE INDEX IF NOT EXISTS idx_srp_determination
    ON section_reg_pairs(determination_id);
CREATE INDEX IF NOT EXISTS idx_srp_section
    ON section_reg_pairs(section_id);

-- technical_terms
CREATE INDEX IF NOT EXISTS idx_terms_determination
    ON technical_terms(determination_id);
CREATE INDEX IF NOT EXISTS idx_terms_term
    ON technical_terms(term);
CREATE INDEX IF NOT EXISTS idx_terms_category
    ON technical_terms(category);

-- important_flags
CREATE INDEX IF NOT EXISTS idx_flags_text
    ON important_flags(determination_id);
CREATE INDEX IF NOT EXISTS idx_flags_name
    ON important_flags(name);

-- cross_citations
CREATE INDEX IF NOT EXISTS idx_citations_citing
    ON cross_citations(citing_case_id);
CREATE INDEX IF NOT EXISTS idx_citations_type
    ON cross_citations(cited_type);

