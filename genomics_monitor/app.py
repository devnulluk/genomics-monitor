from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import __version__
from .db import connect
from .evidence import findings, ingest
from .importer import import_vcf

app = FastAPI(title="Genomics Monitor", version=__version__)


def _token() -> str:
    secret_file = os.getenv("GENOMICS_API_TOKEN_FILE")
    if secret_file:
        return Path(secret_file).read_text(encoding="utf-8").strip()
    return os.getenv("GENOMICS_API_TOKEN", "")


def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = _token()
    if expected and authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Unauthorised")


class VcfImport(BaseModel):
    path: str
    genome_build: str = Field(pattern="^GRCh(37|38)$")


class EvidenceBatch(BaseModel):
    records: list[dict]


@app.on_event("startup")
def startup() -> None:
    connect().close()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "genomics-monitor", "version": __version__}


@app.get("/status", dependencies=[Depends(require_token)])
def status() -> dict:
    with connect() as db:
        variant_count = db.execute("SELECT COUNT(*) FROM variants").fetchone()[0]
        evidence_count = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        finding_count = db.execute("""SELECT COUNT(*) FROM evidence e WHERE EXISTS(
            SELECT 1 FROM variants v WHERE (e.rsid IS NOT NULL AND e.rsid=v.rsid) OR
            (e.chrom IS NOT NULL AND e.chrom=v.chrom AND e.pos=v.pos AND e.ref=v.ref AND e.alt=v.alt))""").fetchone()[0]
        latest = db.execute("SELECT imported_at,genome_build FROM imports ORDER BY id DESC LIMIT 1").fetchone()
    return {"variants": variant_count, "evidence_records": evidence_count, "matched_findings": finding_count,
            "genome_build": latest["genome_build"] if latest else None, "last_genome_import": latest["imported_at"] if latest else None}


@app.post("/imports/vcf", dependencies=[Depends(require_token)])
def upload_vcf(request: VcfImport) -> dict:
    try:
        return import_vcf(request.path, request.genome_build)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/evidence", dependencies=[Depends(require_token)])
def upload_evidence(batch: EvidenceBatch) -> dict:
    try:
        return ingest(batch.records)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/findings", dependencies=[Depends(require_token)])
def get_findings(limit: int = 100) -> dict:
    return {"items": findings(min(max(limit, 1), 500))}

