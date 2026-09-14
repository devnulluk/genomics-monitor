from __future__ import annotations

import gzip
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .db import connect


def _open(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open(encoding="utf-8")


def import_vcf(path_text: str, genome_build: str) -> dict:
    path = Path(path_text).resolve()
    if not path.is_file():
        raise ValueError("VCF file does not exist")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    rows: list[tuple] = []
    sample_column = None
    with _open(path) as handle:
        for line in handle:
            if line.startswith("#CHROM"):
                sample_column = 9 if len(line.rstrip().split("\t")) > 9 else None
                continue
            if line.startswith("#"):
                continue
            fields = line.rstrip().split("\t")
            if len(fields) < 8:
                continue
            chrom, pos, rsid, ref, alts, qual, filter_status = fields[:7]
            genotype = None
            phased = 0
            if sample_column is not None and len(fields) > sample_column:
                keys = fields[8].split(":")
                values = fields[sample_column].split(":")
                if "GT" in keys and keys.index("GT") < len(values):
                    genotype = values[keys.index("GT")]
                    phased = int("|" in genotype)
            for alt in alts.split(","):
                rows.append((chrom.removeprefix("chr"), int(pos), ref, alt, None if rsid == "." else rsid, genotype, phased, filter_status, None if qual == "." else float(qual)))
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        existing = db.execute("SELECT id, variant_count FROM imports WHERE source_sha256=?", (digest,)).fetchone()
        if existing:
            return {"import_id": existing["id"], "variant_count": existing["variant_count"], "already_imported": True}
        cur = db.execute(
            "INSERT INTO imports(source_name,source_sha256,genome_build,imported_at,variant_count) VALUES(?,?,?,?,?)",
            (path.name, digest, genome_build, now, len(rows)),
        )
        db.executemany(
            "INSERT INTO variants(chrom,pos,ref,alt,rsid,genotype,phased,filter_status,quality,source_import_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [row + (cur.lastrowid,) for row in rows],
        )
    return {"import_id": cur.lastrowid, "variant_count": len(rows), "already_imported": False, "sha256": digest}

