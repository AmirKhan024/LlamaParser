"""Run the demo dataset through the REAL app: Stage 1 (LlamaParse + Groq
extraction), employee corrections, confirm, submit; then Stage 3 (policy check).

Everything goes through the FastAPI app in-process, impersonating each employee
by overriding get_current_employee -- the same code paths the UI uses.
Resumable: progress is kept in demo/state.json.

Usage:
  python demo/run_demo.py stage1            # upload -> extract -> corrections -> confirm -> submit
  python demo/run_demo.py stage3            # POST /evaluate for every submitted claim
  Env: POLICY_MODEL (default the Stage 3 default), STAGE1_GAP_SECONDS (pause between uploads)
"""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(BASE))

os.environ["PIPELINE_MODE"] = "real"   # the point: no cached extraction

import server  # noqa: E402  (loads .env; .env's GROQ key beats a stale machine one)
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from db import get_sessionmaker  # noqa: E402
from models import Employee  # noqa: E402

STATE = HERE / "state.json"
GAP = float(os.environ.get("STAGE1_GAP_SECONDS", "10"))


def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {"employees": {}, "claims": {}, "files": {}, "oddities": []}


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=2), encoding="utf-8")


def ensure_employees(manifest: dict, st: dict) -> dict:
    out = {}
    with get_sessionmaker()() as s:
        for e in manifest["employees"]:
            email = f"demo.{e['key']}@example.com"
            row = s.scalar(select(Employee).where(Employee.email == email))
            if row is None:
                row = Employee(name=e["name"], email=email, role="employee", grade=e["grade"], base_city=e["base_city"],
                               department="Demo")
                s.add(row)
                s.commit()
                s.refresh(row)
            out[e["key"]] = row
            st["employees"][e["key"]] = str(row.id)
    return out


def as_employee(emp: Employee) -> None:
    server.app.dependency_overrides[server.get_current_employee] = lambda: emp


def wait_processed(client, doc_id: str, timeout: float = 240) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        d = client.get(f"/api/documents/{doc_id}").json()
        if d["status"] != "processing":
            return d
        time.sleep(2)
    return {"status": "timeout"}


def stage1() -> None:
    manifest = json.loads((HERE / "manifest.json").read_text())
    st = load_state()
    emps = ensure_employees(manifest, st)
    with TestClient(server.app) as client:
        by_claim: dict[tuple, list] = {}
        for it in manifest["items"]:
            by_claim.setdefault((it["employee"], it["claim"]), []).append(it)
        for (ekey, ckey), items in by_claim.items():
            as_employee(emps[ekey])
            name = next(e["name"] for e in manifest["employees"] if e["key"] == ekey)
            cid = st["claims"].get(f"{ekey}/{ckey}")
            if cid is None:
                cid = client.post("/api/claims", json={"title": f"{name} - {ckey}"}).json()["id"]
                st["claims"][f"{ekey}/{ckey}"] = cid
                save_state(st)
            for it in items:
                rec = st["files"].setdefault(it["file"], {})
                if rec.get("confirmed"):
                    continue
                path = HERE / it["file"]
                if not rec.get("document_id"):
                    with open(path, "rb") as f:
                        r = client.post(f"/api/claims/{cid}/documents", files={"file": (path.name, f, "image/png")})
                    if r.status_code != 200:
                        rec["error"] = f"upload {r.status_code}: {r.text[:200]}"
                        st["oddities"].append({"file": it["file"], "what": rec["error"]})
                        save_state(st)
                        continue
                    rec["document_id"] = r.json()["id"]
                    save_state(st)
                doc = wait_processed(client, rec["document_id"])
                for attempt in range(2):
                    if doc["status"] != "failed":
                        break
                    st["oddities"].append({"file": it["file"], "what": f"extraction failed: {doc.get('error_message')}; retry {attempt + 1}"})
                    time.sleep(45)
                    client.post(f"/api/documents/{rec['document_id']}/retry")
                    doc = wait_processed(client, rec["document_id"])
                rec["status_after_extract"] = doc["status"]
                if doc["status"] in ("failed", "timeout"):
                    save_state(st)
                    continue
                body = {"edits": {}}
                if it.get("edit"):
                    body = {"edits": {"amount": it["edit"]["amount"]}, "reason": it["edit"]["reason"]}
                r = client.post(f"/api/documents/{rec['document_id']}/confirm", json=body)
                if r.status_code == 200:
                    rec["confirmed"] = True
                else:
                    rec["confirm_error"] = f"{r.status_code}: {r.text[:300]}"
                    st["oddities"].append({"file": it["file"], "what": "confirm " + rec["confirm_error"]})
                save_state(st)
                time.sleep(GAP)
            if all(st["files"].get(i["file"], {}).get("confirmed") for i in items):
                r = client.post(f"/api/claims/{cid}/submit")
                if r.status_code != 200:
                    st["oddities"].append({"claim": f"{ekey}/{ckey}", "what": f"submit {r.status_code}: {r.text[:200]}"})
                else:
                    st.setdefault("submitted", {})[cid] = True
            else:
                st["oddities"].append({"claim": f"{ekey}/{ckey}", "what": "not submitted: some documents unconfirmed/failed"})
            save_state(st)
    print("stage1 done:", sum(1 for f in st["files"].values() if f.get("confirmed")), "of", len(manifest["items"]), "confirmed")


def stage3() -> None:
    manifest = json.loads((HERE / "manifest.json").read_text())
    st = load_state()
    emps = ensure_employees(manifest, st)
    with TestClient(server.app) as client:
        # STAGE3_REVERSE=1: a second worker walks the claim list from the other end (its own model);
        # it does not write state.json (the decisions themselves are in the DB).
        items = list(st["claims"].items())
        if os.environ.get("STAGE3_REVERSE"):
            items.reverse()
            global save_state
            save_state = lambda _st: None  # noqa: E731
        for key, cid in items:
            if st.setdefault("evaluated", {}).get(cid):
                continue
            as_employee(emps[key.split("/")[0]])
            for attempt in range(6):
                r = client.post(f"/api/claims/{cid}/evaluate")
                if r.status_code == 200:
                    st["evaluated"][cid] = r.json()["verdict"]
                    break
                if r.status_code == 503:
                    st["oddities"].append({"claim": key, "what": f"evaluate 503: {r.text[:160]}"})
                    save_state(st)
                    time.sleep(90)
                    continue
                st["oddities"].append({"claim": key, "what": f"evaluate {r.status_code}: {r.text[:200]}"})
                break
            save_state(st)
            print(key, st["evaluated"].get(cid))
    print("stage3 done:", len(st.get("evaluated", {})), "claims evaluated")


if __name__ == "__main__":
    {"stage1": stage1, "stage3": stage3}[sys.argv[1]]()
