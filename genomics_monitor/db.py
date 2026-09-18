from __future__ import annotations

import os
import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS imports (
  id INTEGER PRIMARY KEY,
  source_name TEXT NOT NULL,
  source_sha256 TEXT NOT NULL UNIQUE,
  genome_build TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  variant_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS variants (
  id INTEGER PRIMARY KEY,
  chrom TEXT NOT NULL,
  pos INTEGER NOT NULL,
  ref TEXT NOT NULL,
  alt TEXT NOT NULL,
  alt_index INTEGER NOT NULL DEFAULT 1,
  rsid TEXT,
  genotype TEXT,
  phased INTEGER NOT NULL DEFAULT 0,
  filter_status TEXT,
  quality REAL,
  source_import_id INTEGER NOT NULL REFERENCES imports(id),
  UNIQUE(chrom, pos, ref, alt, source_import_id)
);
CREATE INDEX IF NOT EXISTS variants_locus ON variants(chrom, pos, ref, alt);
CREATE INDEX IF NOT EXISTS variants_rsid ON variants(rsid);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  source_record_id TEXT NOT NULL,
  source_version TEXT,
  observed_at TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  category TEXT NOT NULL,
  evidence_level TEXT NOT NULL,
  clinical_significance TEXT,
  chrom TEXT,
  pos INTEGER,
  ref TEXT,
  alt TEXT,
  rsid TEXT,
  effect_allele TEXT,
  trait_id TEXT,
  trait_label TEXT,
  population TEXT,
  effect_size TEXT,
  p_value TEXT,
  url TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  UNIQUE(source, source_record_id, payload_sha256)
);
CREATE INDEX IF NOT EXISTS evidence_locus ON evidence(chrom, pos, ref, alt);
CREATE INDEX IF NOT EXISTS evidence_rsid ON evidence(rsid);
CREATE TABLE IF NOT EXISTS feedback (
  evidence_id INTEGER PRIMARY KEY REFERENCES evidence(id),
  response TEXT NOT NULL,
  note TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_sync (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  source_version TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  status TEXT NOT NULL,
  records_scanned INTEGER NOT NULL DEFAULT 0,
  matched_records INTEGER NOT NULL DEFAULT 0,
  inserted_records INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
"""


def connect() -> sqlite3.Connection:
    path = Path(os.getenv("GENOMICS_DB", "data/genomics.sqlite"))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    columns = {row[1] for row in db.execute("PRAGMA table_info(variants)")}
    if "alt_index" not in columns:
        db.execute("ALTER TABLE variants ADD COLUMN alt_index INTEGER NOT NULL DEFAULT 1")
    return db
