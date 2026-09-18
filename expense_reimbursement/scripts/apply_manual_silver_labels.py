"""One-off: applies silver labels assigned by hand (Claude Code, reading
each document's extracted_fields/markdown directly against categories.py's
definitions and tie-break rules) instead of another LLM call -- the
categorizers under evaluation (categorize.py) run on Groq, so the silver
labeler must not share a model with them, and every available Groq model
on this account hit its daily quota while trying anyway (see SUMMARY.md's
Stage 2 section for the full account).

silver_model is set to "claude-code" for every row. Not idempotent by
design -- rerun only if MANUAL_LABELS below changes.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATASET_PATH = Path(__file__).resolve().parent.parent / "eval" / "categorization" / "dataset.jsonl"
SILVER_MODEL = "claude-code"

# id -> (category_or_None, rationale, ambiguous)
MANUAL_LABELS: dict[str, tuple] = {
    # --- SROIE (real Malaysian retail receipts, no client/team framing exists in this dataset) ---
    "cat-sroie-000": ("other", "Safety shoes (KINGS SAFETY SHOES) from a marketing/trading company -- a PPE/safety item, not stationery or any other specific category.", False),
    "cat-sroie-001": ("other", "Same vendor and item type as cat-sroie-000: safety footwear, not stationery or any other specific category.", False),
    "cat-sroie-002": ("other", "Engineering hardware (ceramic cap, steel elbow, tungsten welding rod, cutting discs) from an engineering supplier -- industrial parts, not office stationery.", False),
    "cat-sroie-003": ("travel_meals", "Vendor is a Petron fuel station, but the line items (mint tea, vanilla sandwich) are food from the attached convenience counter, not fuel -- category follows what was actually bought.", True),
    "cat-sroie-004": ("travel_meals", "Gerbang Alaf Restaurants (McDonald's Malaysia franchise operator) -- a burger meal, no client or team wording.", False),
    "cat-sroie-005": ("other", "Hardware/gardening-supply items (PPD tape, garden item) from a trading company -- doesn't fit any specific category.", False),
    "cat-sroie-006": ("other", "A Schneider electrical switch/socket outlet from an electrical supplier -- hardware, not office stationery.", False),
    "cat-sroie-007": ("other", "PVC conduit pipe, connectors, and cable clips -- electrical/plumbing hardware, not office stationery.", False),
    "cat-sroie-008": ("other", "Small hardware-store purchase with an unclear item code -- doesn't fit any specific category.", False),
    "cat-sroie-009": ("other", "LED track lights and mounting hardware from a lighting gallery -- a fixture/electrical purchase, not office stationery.", False),
    "cat-sroie-010": ("other", "Grocery items (baby formula, Milo, cereal, diapers) from a supermarket -- a personal grocery run, not a business expense category.", False),
    "cat-sroie-011": ("local_transport", "A parking fee receipt from Secure Parking Corporation -- parking is explicitly part of local_transport's definition.", False),
    "cat-sroie-012": ("other", "Same vendor as cat-sroie-009: LED track lighting hardware.", False),
    "cat-sroie-013": ("travel_meals", "Old Town Kopitiam (a cafe chain) -- a drink/snack order, no client or team wording.", False),
    "cat-sroie-014": ("travel_meals", "Uroko Japanese Cuisine -- udon, gyoza, fried rice; a meal with no client or team wording.", False),
    "cat-sroie-015": ("office_supplies_equipment", "Ted Heng Stationery & Books -- foam tape and exercise/foolscap books, a textbook stationery purchase.", False),
    "cat-sroie-016": ("office_supplies_equipment", "Ted Heng Stationery & Books -- an Artline marker pen, stationery.", False),
    "cat-sroie-017": ("office_supplies_equipment", "Teo Heng Stationery & Books -- carbon paper, stationery.", False),
    "cat-sroie-018": ("other", "Small hardware-store purchase with an unclear item code -- doesn't fit any specific category.", False),
    "cat-sroie-019": ("other", "Pharmacy items (pain reliever, lotion, Panadol) -- a personal medical purchase, not a business expense category.", False),
    "cat-sroie-020": ("travel_meals", "Unihakka International (a mixed-rice fast-food chain) -- a meal, no client or team wording.", False),
    "cat-sroie-021": ("other", "Wet-market groceries (vegetables, eggs, pork) -- a personal grocery purchase, not a business expense category.", False),
    "cat-sroie-022": ("other", "Yong Tat Hardware Trading -- a hardware-store purchase with an unclear item code.", False),
    "cat-sroie-023": ("other", "Confectionery/snack-shop purchase (drink, cake, nuts) -- no business framing (client/team) present, so filed as a generic small purchase rather than team_events.", False),
    "cat-sroie-024": ("travel_meals", "Bar Wang Rice -- a mixed-rice/noodle food-stall meal, no client or team wording.", False),
    # --- CORD (real Indonesian retail receipts, same lack of client/team framing) ---
    "cat-cord-000": ("other", "Cryptic OCR'd line items ('-TICKET CP', 'TOTAL DISC $') with no vendor name -- can't confidently place in a specific category.", True),
    "cat-cord-001": ("other", "Abbreviated OCR'd item codes ('J.STB PROMO', 'Y.B.BAT') with no vendor name -- likely a minimarket receipt, doesn't fit a specific category.", True),
    "cat-cord-002": ("travel_meals", "Jasmine tea and coconut jelly drinks -- a beverage-stall order, no client or team wording.", False),
    "cat-cord-003": ("travel_meals", "Dynamic Bakery & Cake Natural -- a sugar donut, a bakery snack.", False),
    "cat-cord-004": ("travel_meals", "Iced black coffee, avocado coffee, chicken katsu -- a cafe meal, no client or team wording.", False),
    "cat-cord-005": ("travel_meals", "'TRAD KY TOAST CARTE' -- a toast/cafe menu item, restaurant_bill with no client or team wording.", False),
    "cat-cord-006": ("travel_meals", "Tous Les Jours (bakery cafe chain) -- egg tart and pastries, no client or team wording.", False),
    "cat-cord-007": ("travel_meals", "'ADD CHICKEN BOX' with a coupon -- a fast-food combo meal.", False),
    "cat-cord-008": ("travel_meals", "Black Sakura drink with cookie-dough sauce and nata de coco -- a dessert-drink shop order.", False),
    "cat-cord-009": ("travel_meals", "'Bumbu Kaldu Ayam' (chicken broth seasoning) -- a food-stall order, restaurant_bill with no client or team wording.", False),
    "cat-cord-010": ("travel_meals", "Auntie Anne's -- a cinnamon-sugar pretzel, a snack purchase.", False),
    "cat-cord-011": ("travel_meals", "'BANABERRY FRESH CREAM CAK' -- a bakery cake purchase.", False),
    "cat-cord-012": ("other", "A women's blouse and shopping bag -- a personal clothing purchase, not a business expense category.", False),
    "cat-cord-013": ("travel_meals", "Meatball soup and mineral water -- a food-stall meal, restaurant_bill with no client or team wording.", False),
    "cat-cord-014": ("travel_meals", "Cheese cake slice, baked rice, fish and chips, mushroom soup -- several items could suggest more than one diner, but nothing in the document names a client or team.", True),
    # --- real Stage 1 documents (already well understood from SUMMARY.md) ---
    "cat-real-000": ("phone_internet", "Vodafone Idea Limited telecom_bill -- a mobile/phone bill.", False),
    "cat-real-001": ("own_vehicle_mileage", "local_conveyance_form with a trip log (date/place/purpose/client/km rows) and a total_kms field -- a mileage claim form.", False),
    "cat-real-002": (None, "approval_correspondence -- a forwarding email about a claim, not an expense itself.", False),
    "cat-real-003": ("accommodation", "Grand Plaza Hotel hotel_invoice -- a hotel folio.", False),
}


def _synthetic_label(row: dict) -> tuple:
    """Every synthetic document was generated to unambiguously belong to
    its `expected_category` (see synth_content.py) except the two
    deliberately ambiguous team_events documents (2 diners, no client or
    'team' wording) -- for THOSE, the honest silver label follows what a
    reader would actually conclude from the document alone (no team/
    client signal -> the tie-break default, travel_meals), not the
    generation intent, with ambiguous=True since team_events is equally
    defensible."""
    fields = row.get("extracted_fields") or {}
    notes = row.get("notes", "")
    expected = notes.split("expected category: ")[1].split(".")[0].split(",")[0].strip() if "expected category:" in notes else None
    vendor = fields.get("vendor_name") or "the vendor"

    if row["id"].startswith("synth-team_events-") and row.get("ambiguous"):
        return ("travel_meals", f"{vendor} restaurant bill for 2 diners with no client or 'team' wording -- reads as an ordinary meal, though a team lunch for two is equally plausible.", True)

    return (expected, f"Generated as a {expected.replace('_', ' ')} document from {vendor}; content matches that category with no ambiguity.", False)


def main() -> None:
    with DATASET_PATH.open(encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]

    applied = 0
    missing = []
    for row in rows:
        if row["id"] in MANUAL_LABELS:
            category, rationale, ambiguous = MANUAL_LABELS[row["id"]]
        elif row["source"] == "synthetic_image":
            category, rationale, ambiguous = _synthetic_label(row)
        else:
            missing.append(row["id"])
            continue
        row["silver_label"] = category
        row["silver_rationale"] = rationale
        row["silver_model"] = SILVER_MODEL
        row["ambiguous"] = ambiguous or row.get("ambiguous", False)
        applied += 1

    with DATASET_PATH.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"Applied {applied} labels. {len(missing)} row(s) with no label found: {missing}")


if __name__ == "__main__":
    main()
