from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Security, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import __version__
from .clinvar import latest_sync, sync_clinvar
from .gwas import latest_sync as latest_gwas_sync, sync_gwas
from .db import connect
from .evidence import findings, ingest
from .importer import import_vcf
from .reports import initial_report
from .notifier import send as send_notification

app = FastAPI(title="Genomics Monitor", version=__version__)
bearer_scheme = HTTPBearer(auto_error=False)


def _token() -> str:
    secret_file = os.getenv("GENOMICS_API_TOKEN_FILE")
    if secret_file:
        return Path(secret_file).read_text(encoding="utf-8").strip()
    return os.getenv("GENOMICS_API_TOKEN", "")


def require_token(credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme)) -> None:
    expected = _token()
    supplied = credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else None
    if expected and supplied != expected:
        raise HTTPException(status_code=401, detail="Unauthorised")


class VcfImport(BaseModel):
    path: str
    genome_build: str = Field(pattern="^GRCh(37|38)$")


class EvidenceBatch(BaseModel):
    records: list[dict]


@app.on_event("startup")
def startup() -> None:
    with connect() as db:
        db.execute("UPDATE evidence_sync SET status='interrupted',completed_at=datetime('now'),error='service_restarted' WHERE status='running'")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "genomics-monitor", "version": __version__}


@app.get("/status", dependencies=[Depends(require_token)])
def status() -> dict:
    with connect() as db:
        variant_count = db.execute("SELECT COUNT(*) FROM variants").fetchone()[0]
        evidence_count = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        finding_count = db.execute("""SELECT COUNT(*) FROM evidence e WHERE e.source='GWAS Catalog' OR EXISTS(
            SELECT 1 FROM variants v WHERE (e.rsid IS NOT NULL AND e.rsid=v.rsid) OR
            (e.chrom IS NOT NULL AND e.chrom=v.chrom AND e.pos=v.pos AND e.ref=v.ref AND e.alt=v.alt))""").fetchone()[0]
        latest = db.execute("SELECT imported_at,genome_build FROM imports ORDER BY id DESC LIMIT 1").fetchone()
    return {"variants": variant_count, "evidence_records": evidence_count, "matched_findings": finding_count,
            "genome_build": latest["genome_build"] if latest else None, "last_genome_import": latest["imported_at"] if latest else None,
            "clinvar_sync": latest_sync(), "gwas_sync": latest_gwas_sync()}


@app.post("/sync/clinvar", dependencies=[Depends(require_token)], status_code=202)
def start_clinvar_sync() -> dict:
    current = latest_sync()
    if current and current.get("status") == "running":
        return {"status": "already_running", "sync": current}
    threading.Thread(target=sync_clinvar, daemon=True, name="clinvar-sync").start()
    return {"status": "started"}


@app.post("/sync/gwas", dependencies=[Depends(require_token)], status_code=202)
def start_gwas_sync() -> dict:
    current = latest_gwas_sync()
    if current and current.get("status") == "running":
        return {"status": "already_running", "sync": current}
    threading.Thread(target=sync_gwas, daemon=True, name="gwas-sync").start()
    return {"status": "started"}


@app.get("/reports/initial", dependencies=[Depends(require_token)])
def get_initial_report() -> dict:
    return initial_report()


@app.post("/reports/initial/notify", dependencies=[Depends(require_token)])
def notify_initial_report() -> dict:
    report = initial_report()
    sources = ", ".join(f"{name}: {count:,}" for name, count in sorted(report["by_source"].items()))
    body = (
        f"{report['indexed_calls']:,} indexed GRCh38 calls; {report['evidence_matches']:,} matched evidence records.\n"
        f"{sources}\nEvidence inventory only — not diagnoses or personalised medical advice."
    )
    return {"sent": send_notification("Your initial genome evidence report", body), "report": report}


@app.post("/imports/vcf", dependencies=[Depends(require_token)])
def upload_vcf(request: VcfImport) -> dict:
    try:
        return import_vcf(request.path, request.genome_build)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/imports/vcf-file", dependencies=[Depends(require_token)])
def upload_vcf_file(
    genome_build: str = Form(pattern="^GRCh(37|38)$"),
    file: UploadFile = File(),
) -> dict:
    filename = Path(file.filename or "genome.vcf.gz").name
    if not (filename.endswith(".vcf") or filename.endswith(".vcf.gz")):
        raise HTTPException(status_code=400, detail="Only .vcf and .vcf.gz files are accepted")
    input_dir = Path(os.getenv("GENOMICS_INPUT_DIR", "/data/input"))
    input_dir.mkdir(parents=True, exist_ok=True)
    maximum = int(os.getenv("GENOMICS_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))
    size = 0
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=input_dir, prefix="upload-", suffix=".tmp", delete=False) as target:
            temporary_path = Path(target.name)
            while chunk := file.file.read(8 * 1024 * 1024):
                size += len(chunk)
                if size > maximum:
                    raise HTTPException(status_code=413, detail="Upload exceeds configured limit")
                target.write(chunk)
        final_path = input_dir / filename
        os.replace(temporary_path, final_path)
        result = import_vcf(str(final_path), genome_build)
        result["uploaded_bytes"] = size
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        file.file.close()
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


@app.post("/evidence", dependencies=[Depends(require_token)])
def upload_evidence(batch: EvidenceBatch) -> dict:
    try:
        return ingest(batch.records)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/findings", dependencies=[Depends(require_token)])
def get_findings(limit: int = 100) -> dict:
    return {"items": findings(min(max(limit, 1), 500))}
