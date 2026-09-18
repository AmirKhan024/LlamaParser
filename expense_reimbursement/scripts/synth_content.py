"""Fictional content generators for the synthetic eval-set receipts
(scripts/generate_synthetic_eval_images.py). Pure Python + Faker --
deliberately no LLM call here, so this costs no Groq quota and produces
exactly the categories/counts asked for. Every vendor/person name is
fictional; a few Indian-context Faker locales are mixed with the default
for variety.
"""

import random
from dataclasses import dataclass, field
from decimal import Decimal

from faker import Faker

fake_en = Faker("en_IN")
fake_intl = Faker("en_US")

FICTIONAL_AIRLINES = ["AeroVista Airlines", "SkyBridge Air", "Meridian Wings", "Northwind Airways"]
FICTIONAL_HOTELS = ["The Sahyadri Grand", "Zenith Business Suites", "Harborview Residency", "Palm Court Hotel"]
FICTIONAL_CAB_APPS = ["GoCab", "RideNow", "QuickHail", "CityGo"]
FICTIONAL_FUEL_STATIONS = ["Suryodaya Fuel Station", "Highway Energy Point", "Metro Petro Station"]
FICTIONAL_TELECOM = ["Tarang Telecom", "NetSpeed Broadband", "Signal Plus Mobile"]
FICTIONAL_SAAS = ["CloudSuite Pro", "TaskFlow SaaS", "DevOps Nexus", "InsightBoard Analytics"]
FICTIONAL_TRAINING = ["TechConf India", "Skillbridge Academy", "CloudCert Institute", "DataMinds Workshop"]
FICTIONAL_VISA_AGENTS = ["VisaAssist Services", "GlobalTravel Docs", "SafeTrip Insurance"]
FICTIONAL_RESTAURANTS = ["Spice Route", "Copper Kettle", "The Old Mill Kitchen", "Bay Leaf Diner", "Urban Tandoor"]
FICTIONAL_OFFICE_SHOPS = ["OfficeMart", "DeskWorks Supplies", "PaperTrail Stationery"]
FICTIONAL_COURIERS = ["QuickShip Courier", "SwiftPost Logistics"]
FICTIONAL_GIFT_SHOPS = ["Elegant Hampers Co", "The Gift Nook"]
FICTIONAL_CLIENT_COMPANIES = ["Meridian Retail", "Bluepeak Logistics", "Harrow Financial", "Novagen Pharma", "Coastline Bank"]
INDIAN_CITIES = ["Mumbai", "Bengaluru", "Pune", "Delhi NCR", "Chennai", "Hyderabad", "Kolkata"]
CURRENCIES = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


@dataclass
class SyntheticDoc:
    doc_id: str
    category: str
    layout: str  # "receipt" | "invoice" | "ticket" | "utility" | "fee"
    vendor: str
    lines: list[str] = field(default_factory=list)
    line_items: list[tuple] = field(default_factory=list)  # (desc, qty, rate, amount)
    total_label: str = "Total"
    total_amount: str = "0.00"
    currency: str = "INR"
    ambiguous: bool = False
    notes: str = ""


def _money(amount) -> str:
    return f"{Decimal(amount):.2f}"


def _rand_amount(low, high) -> Decimal:
    return Decimal(random.randint(low * 100, high * 100)) / 100


def gen_intercity_travel(doc_id: str) -> SyntheticDoc:
    airline = random.choice(FICTIONAL_AIRLINES)
    origin, dest = random.sample(INDIAN_CITIES, 2)
    fare = _rand_amount(3500, 12000)
    return SyntheticDoc(
        doc_id=doc_id, category="intercity_travel", layout="ticket", vendor=airline,
        lines=[
            f"E-Ticket / Boarding Pass",
            f"Passenger: {fake_en.name()}",
            f"PNR: {fake_en.bothify('??####').upper()}",
            f"Route: {origin} -> {dest}",
            f"Date: {fake_en.date_this_year()}",
            f"Flight: {airline} {random.randint(100,999)}",
        ],
        line_items=[("Base fare", "1", _money(fare * Decimal('0.8')), _money(fare * Decimal('0.8'))),
                    ("Taxes & fees", "1", _money(fare * Decimal('0.2')), _money(fare * Decimal('0.2')))],
        total_amount=_money(fare), currency="INR",
    )


def gen_local_transport(doc_id: str) -> SyntheticDoc:
    app = random.choice(FICTIONAL_CAB_APPS)
    fare = _rand_amount(80, 650)
    airport = random.random() < 0.3
    return SyntheticDoc(
        doc_id=doc_id, category="local_transport", layout="receipt", vendor=app,
        lines=[
            "Trip Receipt",
            f"Pickup: {fake_en.street_name()}",
            f"Drop: {'Airport Terminal' if airport else fake_en.street_name()}",
            f"Date: {fake_en.date_this_year()}",
            f"Driver: {fake_en.first_name()}",
        ],
        line_items=[("Fare", "1", _money(fare), _money(fare))],
        total_amount=_money(fare), currency="INR",
    )


def gen_own_vehicle_mileage(doc_id: str) -> SyntheticDoc:
    km = random.randint(20, 180)
    rate = Decimal("9")
    amount = km * rate
    return SyntheticDoc(
        doc_id=doc_id, category="own_vehicle_mileage", layout="invoice", vendor="Local Conveyance Claim",
        lines=[
            "Local Conveyance / Mileage Claim Form",
            f"Employee: {fake_en.name()}",
            f"Vehicle: Four-wheeler (own)",
            f"Date: {fake_en.date_this_year()}",
            f"Purpose: Client visit",
            f"Client: {random.choice(FICTIONAL_CLIENT_COMPANIES)}",
        ],
        line_items=[(f"{km} km @ Rs {rate}/km", str(km), str(rate), _money(amount))],
        total_amount=_money(amount), currency="INR",
    )


def gen_fuel(doc_id: str) -> SyntheticDoc:
    station = random.choice(FICTIONAL_FUEL_STATIONS)
    litres = round(random.uniform(4, 35), 2)
    rate = round(random.uniform(95, 108), 2)
    amount = round(litres * rate, 2)
    return SyntheticDoc(
        doc_id=doc_id, category="fuel", layout="receipt", vendor=station,
        lines=["Fuel Receipt", f"Date: {fake_en.date_this_year()}", "Fuel: Petrol"],
        line_items=[(f"Petrol {litres} L @ Rs {rate}", str(litres), str(rate), _money(amount))],
        total_amount=_money(amount), currency="INR",
    )


def gen_accommodation(doc_id: str) -> SyntheticDoc:
    hotel = random.choice(FICTIONAL_HOTELS)
    nights = random.randint(1, 4)
    is_intl = random.random() < 0.3
    currency = random.choice(["USD", "EUR"]) if is_intl else "INR"
    nightly = _rand_amount(120, 300) if is_intl else _rand_amount(3000, 12000)
    room_total = nightly * nights
    extras = []
    extra_total = Decimal("0")
    if random.random() < 0.5:
        minibar = _rand_amount(10, 40) if is_intl else _rand_amount(300, 1200)
        extras.append(("Minibar", "1", _money(minibar), _money(minibar)))
        extra_total += minibar
    total = room_total + extra_total
    return SyntheticDoc(
        doc_id=doc_id, category="accommodation", layout="invoice", vendor=hotel,
        lines=[
            "Hotel Folio / Tax Invoice",
            f"Guest: {fake_en.name() if not is_intl else fake_intl.name()}",
            f"Check-in: {fake_en.date_this_year()}",
            f"Nights: {nights}",
        ],
        line_items=[(f"Room charge x {nights} night(s)", str(nights), _money(nightly), _money(room_total))] + extras,
        total_amount=_money(total), currency=currency,
    )


def gen_travel_meals(doc_id: str) -> SyntheticDoc:
    restaurant = random.choice(FICTIONAL_RESTAURANTS)
    amount = _rand_amount(180, 900)
    return SyntheticDoc(
        doc_id=doc_id, category="travel_meals", layout="invoice", vendor=restaurant,
        lines=["Tax Invoice", f"Date: {fake_en.date_this_year()}", f"Table for 1", f"Server: {fake_en.first_name()}"],
        line_items=[("Meal", "1", _money(amount * Decimal('0.9')), _money(amount * Decimal('0.9'))),
                    ("GST", "1", _money(amount * Decimal('0.1')), _money(amount * Decimal('0.1')))],
        total_amount=_money(amount), currency="INR",
    )


def gen_client_entertainment(doc_id: str) -> SyntheticDoc:
    restaurant = random.choice(FICTIONAL_RESTAURANTS)
    client = random.choice(FICTIONAL_CLIENT_COMPANIES)
    attendees = random.randint(2, 6)
    amount = _rand_amount(1500, 6000)
    return SyntheticDoc(
        doc_id=doc_id, category="client_entertainment", layout="invoice", vendor=restaurant,
        lines=[
            "Tax Invoice", f"Date: {fake_en.date_this_year()}",
            f"Client: {client}", f"Attendees: {attendees}",
        ],
        line_items=[("Food & beverages (incl. wine)", str(attendees), _money(amount / attendees), _money(amount))],
        total_amount=_money(amount), currency="INR",
    )


def gen_team_events(doc_id: str, ambiguous: bool = False) -> SyntheticDoc:
    restaurant = random.choice(FICTIONAL_RESTAURANTS)
    attendees = random.randint(2, 10) if not ambiguous else 2
    amount = _rand_amount(600, 4000)
    lines = ["Tax Invoice", f"Date: {fake_en.date_this_year()}"]
    if ambiguous:
        lines.append(f"Guests: {attendees}")  # deliberately no "team" or "client" wording
    else:
        lines.append(f"Team Dinner - {attendees} colleagues")
    return SyntheticDoc(
        doc_id=doc_id, category="team_events", layout="invoice", vendor=restaurant,
        lines=lines,
        line_items=[("Food & beverages", str(attendees), _money(amount / attendees), _money(amount))],
        total_amount=_money(amount), currency="INR",
        ambiguous=ambiguous,
        notes="2 diners, no client or 'team' wording -- could read as travel_meals instead" if ambiguous else "",
    )


def gen_phone_internet(doc_id: str) -> SyntheticDoc:
    telecom = random.choice(FICTIONAL_TELECOM)
    amount = _rand_amount(400, 2200)
    return SyntheticDoc(
        doc_id=doc_id, category="phone_internet", layout="utility", vendor=telecom,
        lines=[
            "Monthly Bill", f"Account: {fake_en.bothify('##########')}",
            f"Billing period: {fake_en.month_name()} 2026",
        ],
        line_items=[("Plan charges", "1", _money(amount * Decimal('0.85')), _money(amount * Decimal('0.85'))),
                    ("Taxes", "1", _money(amount * Decimal('0.15')), _money(amount * Decimal('0.15')))],
        total_amount=_money(amount), currency="INR",
    )


def gen_software_subscriptions(doc_id: str) -> SyntheticDoc:
    vendor = random.choice(FICTIONAL_SAAS)
    is_intl = random.random() < 0.6
    currency = "USD" if is_intl else "INR"
    amount = _rand_amount(15, 250) if is_intl else _rand_amount(1200, 15000)
    plan = random.choice(["Monthly subscription", "Annual subscription"])
    return SyntheticDoc(
        doc_id=doc_id, category="software_subscriptions", layout="invoice", vendor=vendor,
        lines=["Invoice", f"Billed to: Konkan Digital Systems Pvt Ltd", f"Invoice date: {fake_en.date_this_year()}"],
        line_items=[(plan, "1", _money(amount), _money(amount))],
        total_amount=_money(amount), currency=currency,
    )


def gen_office_supplies_equipment(doc_id: str) -> SyntheticDoc:
    shop = random.choice(FICTIONAL_OFFICE_SHOPS)
    items = random.sample([
        ("Notebook pack (5)", "180"), ("Wireless mouse", "650"), ("Stapler", "120"),
        ("Printer cartridge", "1450"), ("Sticky notes", "90"), ("Desk organizer", "340"),
    ], k=random.randint(1, 3))
    line_items = [(name, "1", price, price) for name, price in items]
    total = sum(Decimal(p) for _, p in items)
    return SyntheticDoc(
        doc_id=doc_id, category="office_supplies_equipment", layout="receipt", vendor=shop,
        lines=["Retail Invoice", f"Date: {fake_en.date_this_year()}"],
        line_items=line_items, total_amount=_money(total), currency="INR",
    )


def gen_training_conferences(doc_id: str) -> SyntheticDoc:
    vendor = random.choice(FICTIONAL_TRAINING)
    amount = _rand_amount(2500, 45000)
    course = random.choice(["Cloud Architecture Certification", "Advanced Data Engineering Course",
                             "Annual Tech Conference Pass", "Project Management Certification Exam"])
    return SyntheticDoc(
        doc_id=doc_id, category="training_conferences", layout="invoice", vendor=vendor,
        lines=["Invoice", f"Participant: {fake_en.name()}", f"Date: {fake_en.date_this_year()}"],
        line_items=[(course, "1", _money(amount), _money(amount))],
        total_amount=_money(amount), currency="INR",
    )


def gen_travel_documents_fees(doc_id: str) -> SyntheticDoc:
    vendor = random.choice(FICTIONAL_VISA_AGENTS)
    kind = random.choice(["Visa application fee", "Travel insurance premium", "Forex card loading fee"])
    is_intl = "insurance" in kind.lower() or random.random() < 0.5
    currency = "USD" if is_intl else "INR"
    amount = _rand_amount(20, 150) if is_intl else _rand_amount(1500, 9000)
    return SyntheticDoc(
        doc_id=doc_id, category="travel_documents_fees", layout="fee", vendor=vendor,
        lines=["Fee Receipt", f"Applicant: {fake_en.name()}", f"Date: {fake_en.date_this_year()}"],
        line_items=[(kind, "1", _money(amount), _money(amount))],
        total_amount=_money(amount), currency=currency,
    )


def gen_other_gift(doc_id: str) -> SyntheticDoc:
    shop = random.choice(FICTIONAL_GIFT_SHOPS)
    amount = _rand_amount(800, 2500)
    return SyntheticDoc(
        doc_id=doc_id, category="other", layout="receipt", vendor=shop,
        lines=["Retail Invoice", f"Date: {fake_en.date_this_year()}",
               f"For: {random.choice(FICTIONAL_CLIENT_COMPANIES)} (client gift)"],
        line_items=[("Gift hamper", "1", _money(amount), _money(amount))],
        total_amount=_money(amount), currency="INR",
    )


def gen_other_courier(doc_id: str) -> SyntheticDoc:
    vendor = random.choice(FICTIONAL_COURIERS)
    amount = _rand_amount(80, 600)
    return SyntheticDoc(
        doc_id=doc_id, category="other", layout="receipt", vendor=vendor,
        lines=["Courier Receipt", f"Date: {fake_en.date_this_year()}", f"Tracking: {fake_en.bothify('CR#########')}"],
        line_items=[("Courier charge", "1", _money(amount), _money(amount))],
        total_amount=_money(amount), currency="INR",
    )


GENERATORS = {
    "intercity_travel": [gen_intercity_travel],
    "local_transport": [gen_local_transport],
    "own_vehicle_mileage": [gen_own_vehicle_mileage],
    "fuel": [gen_fuel],
    "accommodation": [gen_accommodation],
    "travel_meals": [gen_travel_meals],
    "client_entertainment": [gen_client_entertainment],
    "team_events": [gen_team_events],
    "phone_internet": [gen_phone_internet],
    "software_subscriptions": [gen_software_subscriptions],
    "office_supplies_equipment": [gen_office_supplies_equipment],
    "training_conferences": [gen_training_conferences],
    "travel_documents_fees": [gen_travel_documents_fees],
    "other": [gen_other_gift, gen_other_courier],
}
