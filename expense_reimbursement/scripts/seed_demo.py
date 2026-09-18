"""Seed a demo draft claim for the seeded employee, containing the 3
real documents in uploads/, extracted via PIPELINE_MODE=fake (replays
the cached outputs/*_result.json for each -- no LlamaParse/Groq calls,
no API keys needed). Forces fake mode regardless of .env's own setting,
since the whole point of this script is to work without credits."""

import hashlib
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["PIPELINE_MODE"] = "fake"

import repository
from db import get_sessionmaker
from server import STORAGE_DIR, UPLOADS_DIR, _run_pipeline


def main() -> None:
    with get_sessionmaker()() as session:
        employee = repository.get_or_create_seed_employee(session)
        claim = repository.create_claim(session, employee.id, "Demo claim: May 2026 expenses")

    source_paths = sorted(UPLOADS_DIR.glob("*.pdf"))
    if not source_paths:
        print(f"No files found in {UPLOADS_DIR}")
        return

    for source_path in source_paths:
        content = source_path.read_bytes()
        sha256 = hashlib.sha256(content).hexdigest()
        document_id = uuid.uuid4()
        file_key = f"{claim.id}/{document_id}{source_path.suffix}"
        full_path = STORAGE_DIR / file_key
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(content)

        with get_sessionmaker()() as session:
            repository.create_document(
                session,
                document_id=document_id,
                claim_id=claim.id,
                actor_id=employee.id,
                original_name=source_path.name,
                file_key=file_key,
                file_sha256=sha256,
                mime_type="application/pdf",
            )

        _run_pipeline(document_id, full_path, employee.id)

        with get_sessionmaker()() as session:
            refreshed = repository.get_document(session, document_id)
            print(f"  added {source_path.name} -> status={refreshed.status}")

    with get_sessionmaker()() as session:
        total = repository.recompute_claim_total(session, claim.id)
        print(f"Demo claim created: {claim.id} (total {total})")


if __name__ == "__main__":
    main()
