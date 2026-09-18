"""Expense category definitions -- the single source of truth for what an
expense was FOR (as opposed to `document_type` in schemas.py, which is what
KIND OF PAPER it is: a restaurant_bill document can be travel_meals,
client_entertainment, or team_events depending on who ate). Every
categorizer in categorize.py, the DB column, and the UI dropdown all read
from CATEGORIES here -- add or rename a category in exactly one place.

Each category's `policy_clauses` names section numbers in
policy/expense_policy.md; tests/test_categories.py checks both directions:
every category maps to a clause that exists, and every clause is actually
referenced by some category.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    definition: str
    include_examples: list[str]
    # (example, correct_category_id) -- a near-miss that belongs elsewhere,
    # naming where it actually belongs rather than just what it isn't.
    exclude_examples: list[tuple[str, str]]
    policy_clauses: list[str] = field(default_factory=list)


CATEGORIES: dict[str, Category] = {
    "intercity_travel": Category(
        id="intercity_travel",
        label="Intercity Travel",
        definition="Getting between cities: flights, trains, intercity buses, and the "
        "fees attached directly to that ticket.",
        include_examples=[
            "A domestic flight e-ticket, Mumbai to Bengaluru",
            "An international business-class upgrade fee on an airline invoice",
            "A Rajdhani Express AC 2-Tier train ticket",
            "A flight change/cancellation fee charged by the airline",
        ],
        exclude_examples=[
            ("A cab ride to the airport before the flight", "local_transport"),
            ("A hotel stay at the destination city", "accommodation"),
            ("Excess-baggage or seat-selection fee is fine here, but a per-km mileage claim for a self-driven trip", "own_vehicle_mileage"),
        ],
        policy_clauses=["3", "4"],
    ),
    "local_transport": Category(
        id="local_transport",
        label="Local Transport",
        definition="Getting around within a city: cabs, ride-hailing, auto-rickshaw, "
        "metro/bus, airport transfers, parking, and tolls.",
        include_examples=[
            "An Uber/Ola trip receipt within Bengaluru",
            "An auto-rickshaw fare",
            "A cab receipt to the airport before a flight",
            "A parking receipt from a client-site visit",
        ],
        exclude_examples=[
            ("The flight itself, once at the airport", "intercity_travel"),
            ("A trip in the employee's own car claimed by the kilometre", "own_vehicle_mileage"),
            ("The employee's daily home-to-office commute", "other"),
        ],
        policy_clauses=["5"],
    ),
    "own_vehicle_mileage": Category(
        id="own_vehicle_mileage",
        label="Own-Vehicle Mileage",
        definition="Per-kilometre claims for using the employee's own two-wheeler or "
        "four-wheeler for business travel, backed by a trip log (a local "
        "conveyance / mileage claim form).",
        include_examples=[
            "A filled local conveyance form with date/place/purpose/client/km rows",
            "A monthly mileage claim for a two-wheeler used for client visits",
            "A trip log showing total km and a per-km rate calculation",
        ],
        exclude_examples=[
            ("A fuel station receipt for the same vehicle", "fuel"),
            ("A cab or ride-hailing receipt", "local_transport"),
            ("A flight or train ticket", "intercity_travel"),
        ],
        policy_clauses=["6"],
    ),
    "fuel": Category(
        id="fuel",
        label="Fuel",
        definition="Fuel station receipts -- petrol, diesel, or CNG -- for a company-leased "
        "vehicle or an eligible grade's car-policy vehicle.",
        include_examples=[
            "A petrol pump receipt showing litres and amount",
            "A diesel receipt for a company-leased car",
            "A CNG fill-up receipt",
        ],
        exclude_examples=[
            ("A per-km trip log for the employee's own vehicle", "own_vehicle_mileage"),
            ("A cab fare", "local_transport"),
            ("A car service/repair invoice", "other"),
        ],
        policy_clauses=["7"],
    ),
    "accommodation": Category(
        id="accommodation",
        label="Accommodation",
        definition="Hotel or serviced-apartment stays and everything billed on the same "
        "folio (room charge, taxes, minibar, laundry) -- the category doesn't "
        "decide what's reimbursable, the policy does.",
        include_examples=[
            "A hotel invoice/folio for a multi-night business trip",
            "A serviced-apartment bill for an extended assignment",
            "A hotel bill that also lists room service and minibar line items",
        ],
        exclude_examples=[
            ("A restaurant bill from a meal away from the hotel", "travel_meals"),
            ("A flight or train ticket to reach the city", "intercity_travel"),
            ("A cab from the airport to the hotel", "local_transport"),
        ],
        policy_clauses=["8"],
    ),
    "travel_meals": Category(
        id="travel_meals",
        label="Travel Meals",
        definition="The employee's own meals while travelling or working -- a solo or "
        "small working meal, no client and no team celebration involved.",
        include_examples=[
            "A solo dinner receipt while on a business trip",
            "A breakfast bill at the hotel during travel",
            "A quick lunch receipt near a client site with no attendees listed",
        ],
        exclude_examples=[
            ("A restaurant bill that lists a client company name and attendees", "client_entertainment"),
            ("A team dinner with several colleagues and no client present", "team_events"),
            ("Alcohol on a travel-meal bill is not reimbursable, but is still filed here, not as client_entertainment", "travel_meals"),
        ],
        policy_clauses=["9"],
    ),
    "client_entertainment": Category(
        id="client_entertainment",
        label="Client Entertainment",
        definition="Meals or events with clients or prospects -- the bill or claim names "
        "the client company and states how many people attended.",
        include_examples=[
            "A dinner bill noting 'Client: Meridian Retail, 4 attendees'",
            "A lunch receipt with a client company name and attendee count",
            "An event/venue invoice for hosting a client, with alcohol on the bill",
        ],
        exclude_examples=[
            ("The same kind of restaurant bill with no client named", "travel_meals"),
            ("A team-only dinner, no client present", "team_events"),
            ("A business gift given to the same client", "other"),
        ],
        policy_clauses=["10"],
    ),
    "team_events": Category(
        id="team_events",
        label="Team Events",
        definition="Team meals, celebrations, and offsites for Konkan Digital employees "
        "only -- several colleagues, no client.",
        include_examples=[
            "A team lunch bill for 6 colleagues, no client attendees",
            "A birthday or festival celebration bill for the team",
            "A team offsite venue/catering invoice",
        ],
        exclude_examples=[
            ("The same bill if a client is also listed as an attendee", "client_entertainment"),
            ("A single employee's own meal while travelling", "travel_meals"),
            ("A gift bought for the team instead of a shared meal", "other"),
        ],
        policy_clauses=["11"],
    ),
    "phone_internet": Category(
        id="phone_internet",
        label="Phone and Internet",
        definition="Mobile bills, home broadband, data packs, and international roaming "
        "charges.",
        include_examples=[
            "A monthly postpaid mobile bill",
            "A home broadband invoice",
            "An international roaming pack charge during business travel",
        ],
        exclude_examples=[
            ("A SaaS app or online subscription", "software_subscriptions"),
            ("A courier or postage receipt", "other"),
            ("A bill in a family member's or flatmate's name, not the employee's or the company's", "other"),
        ],
        policy_clauses=["12"],
    ),
    "software_subscriptions": Category(
        id="software_subscriptions",
        label="Software and Subscriptions",
        definition="SaaS tools, apps, licences, and other online services billed "
        "monthly or annually, for business use.",
        include_examples=[
            "A SaaS subscription invoice (e.g. a project-management tool)",
            "An annual software licence renewal, billed via an app store",
            "A cloud-service usage invoice for a work tool",
        ],
        exclude_examples=[
            ("A mobile phone or broadband bill", "phone_internet"),
            ("A physical piece of equipment or a peripheral", "office_supplies_equipment"),
            ("A personal streaming or personal cloud-storage subscription -- never reimbursable at all", "other"),
        ],
        policy_clauses=["13"],
    ),
    "office_supplies_equipment": Category(
        id="office_supplies_equipment",
        label="Office Supplies and Equipment",
        definition="Stationery, small peripherals, and other small physical purchases "
        "for work.",
        include_examples=[
            "A stationery store receipt (pens, notebooks, folders)",
            "A wireless mouse or headset purchase receipt",
            "A small desk accessory purchase for a home-office setup",
        ],
        exclude_examples=[
            ("A SaaS or software licence purchase", "software_subscriptions"),
            ("A purchase of ₹2,000 or more, which is capital equipment via procurement, not an expense claim", "other"),
            ("A training-related book bought for a specific certification", "training_conferences"),
        ],
        policy_clauses=["14"],
    ),
    "training_conferences": Category(
        id="training_conferences",
        label="Training and Conferences",
        definition="Courses, certifications, certification exams, conference passes, and "
        "work-related books/materials.",
        include_examples=[
            "A certification exam fee receipt",
            "An online course enrolment invoice",
            "A conference registration/pass invoice",
        ],
        exclude_examples=[
            ("Travel or hotel booked to attend the same conference", "intercity_travel"),
            ("A visa fee for the same trip", "travel_documents_fees"),
            ("A general business/work book with no specific course or exam attached", "office_supplies_equipment"),
        ],
        policy_clauses=["15"],
    ),
    "travel_documents_fees": Category(
        id="travel_documents_fees",
        label="Travel Documents and Fees",
        definition="Visa fees, passport fees for an approved business trip, travel "
        "insurance, and forex conversion fees.",
        include_examples=[
            "A visa application fee receipt",
            "A travel insurance policy invoice for an international trip",
            "A forex card loading fee or currency conversion fee receipt",
        ],
        exclude_examples=[
            ("The flight ticket itself", "intercity_travel"),
            ("A hotel booking fee", "accommodation"),
            ("A conference registration fee for the same trip", "training_conferences"),
        ],
        policy_clauses=["16"],
    ),
    "other": Category(
        id="other",
        label="Other",
        definition="Business gifts, courier/postage, printing, and anything else "
        "business-related that doesn't fit a more specific category. Approval "
        "emails and other supporting documents are evidence, not expenses, "
        "and get no category at all (see NON_EXPENSE_DOCUMENT_TYPES).",
        include_examples=[
            "A business gift receipt for a client",
            "A courier/postage receipt",
            "A printing shop receipt for business materials",
        ],
        exclude_examples=[
            ("An approval email forwarding a claim for sign-off -- not an expense at all", "no category"),
            ("A restaurant bill (has its own categories)", "travel_meals"),
            ("A hotel folio (has its own category)", "accommodation"),
        ],
        policy_clauses=["17", "18"],
    ),
}

CATEGORY_IDS: list[str] = list(CATEGORIES.keys())

# document_type -> default category for the `rules` categorizer, and the
# starting point every other categorizer's prompt/training data is built
# from. None means "this document type is evidence, not an expense" -- no
# category should ever be assigned (see server.py's pipeline hook).
DOCUMENT_TYPE_DEFAULT_CATEGORY: dict[str, Optional[str]] = {
    "telecom_bill": "phone_internet",
    "restaurant_bill": "travel_meals",
    "taxi_receipt": "local_transport",
    "local_conveyance_form": "own_vehicle_mileage",
    "hotel_invoice": "accommodation",
    "fuel_receipt": "fuel",
    "generic_receipt": "other",
    "unstructured_proof": "other",
    "approval_correspondence": None,
}

# Document types that are supporting evidence, not an expense in their own
# right -- never assigned a category, never shown a category in the UI.
NON_EXPENSE_DOCUMENT_TYPES: set[str] = {
    dt for dt, cat in DOCUMENT_TYPE_DEFAULT_CATEGORY.items() if cat is None
}

# Explicit tie-break rules for ambiguous cases -- the `llm` categorizer's
# prompt includes these verbatim; the `rules` categorizer implements them
# as keyword checks (see categorize.py). Kept as plain English here so
# there's exactly one place these judgment calls are written down.
TIE_BREAK_RULES: list[str] = [
    "A restaurant bill defaults to travel_meals. If the document names a "
    "client company or lists client attendees, it's client_entertainment "
    "instead. If it's several colleagues (no client) -- e.g. a team dinner "
    "-- it's team_events instead.",
    "A hotel bill, including any room-service/minibar/laundry line items "
    "printed on the same folio, is accommodation. The category doesn't "
    "decide what's reimbursable -- the policy (§8.4) does that separately.",
    "A fuel station receipt (litres, price/litre, pump) is fuel. A "
    "kilometre-based trip log or conveyance form is own_vehicle_mileage. "
    "They are never the same document.",
    "A cab/ride-hailing receipt to or from the airport is local_transport. "
    "The flight itself is intercity_travel -- always two separate expenses.",
    "An annual or monthly software plan billed by an app store or SaaS "
    "vendor is software_subscriptions, even if the receipt looks like a "
    "generic 'invoice' with no software-specific wording beyond the vendor "
    "name.",
    "Approval emails, forwarding messages, and other correspondence ABOUT "
    "a claim are evidence, not an expense -- they get no category at all, "
    "matching document_type == approval_correspondence.",
    "A visa, passport, or travel-insurance fee is travel_documents_fees, "
    "not intercity_travel, even though it's only incurred because of a "
    "trip.",
    "A business gift purchase is 'other', not client_entertainment -- "
    "entertainment is a shared meal/event; a gift is a physical item.",
]


def category_for_document_type(document_type: str) -> Optional[str]:
    return DOCUMENT_TYPE_DEFAULT_CATEGORY.get(document_type, "other")


def is_categorizable(document_type: str) -> bool:
    return document_type not in NON_EXPENSE_DOCUMENT_TYPES
