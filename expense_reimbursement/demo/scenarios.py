"""Demo population and receipts (Part A of the Stage 4 brief).

A smoke-test / demo script, NOT an eval set: nothing here states an expected
verdict, clause or category, and none of it reaches the rendered documents or
the manifest. What IS here is what a real submitter would supply: who they are,
which claim a receipt goes into, and what the receipt prints. `edit` records an
action the employee takes on the review page after extraction (this is what
produces Stage 1 corrections for the fraud rules to read).
"""

from datetime import date

EMPLOYEES = [
    # key, name, grade, base city
    ("aarav", "Aarav Mehta", "L2", "Mumbai"),
    ("priya", "Priya Nair", "L3", "Bengaluru"),
    ("rohan", "Rohan Iyer", "L1", "Pune"),
    ("sneha", "Sneha Kulkarni", "L4", "Mumbai"),
    ("vikram", "Vikram Singh", "L5", "Delhi"),
    ("ananya", "Ananya Das", "L2", "Kolkata"),
    ("karthik", "Karthik Reddy", "L3", "Hyderabad"),
    ("meera", "Meera Joshi", "L1", "Jaipur"),
]


def d(month: int, day: int) -> str:
    return date(2026, month, day).isoformat()


# kind: restaurant | hotel | taxi | telecom | generic
# Common: emp, claim (claim key within the employee), vendor, date, city
# restaurant: total, items[], extra[] (lines printed on the bill)
# hotel: nights, rate (per night, pre-tax), extras[(desc, amount)], country, currency
# taxi: total, route
# telecom: total, account
# generic: total, items[]
S = []


def add(emp, claim, kind, vendor, when, city, **kw):
    S.append(dict(emp=emp, claim=claim, kind=kind, vendor=vendor, date=when, city=city, **kw))


# ---- Aarav (L2, Mumbai)
add("aarav", "jun", "taxi", "Mumbai Blue Cabs", d(6, 3), "Mumbai", total=420, route="Andheri to Nariman Point",
    edit={"amount": "640.00", "reason": "Toll and waiting charges were added by the driver."})
add("aarav", "jun", "restaurant", "Cafe Madras", d(6, 12), "Mumbai", total=585, items=["Masala dosa", "Filter coffee", "Idli sambar"])
add("aarav", "jun", "hotel", "Lotus Residency", d(6, 16), "Pune", nights=2, rate=3900, extras=[("Laundry", 350)])
add("aarav", "jun", "telecom", "Vodafone Idea", d(6, 25), "Mumbai", total=720, account="102291733")
add("aarav", "jul", "restaurant", "The Bombay Canteen", d(7, 22), "Mumbai", total=10000, items=["Tasting menu", "Mocktails", "Dessert platter"])
add("aarav", "aug", "hotel", "Marriott Marina Bay", d(8, 6), "Singapore", nights=2, rate=160, currency="USD", country="Singapore", extras=[])

# ---- Priya (L3, Bengaluru)
add("priya", "jun", "taxi", "Namma Yatri Cabs", d(6, 8), "Bengaluru", total=1250, route="Whitefield to Indiranagar")
add("priya", "jul-a", "restaurant", "Toit Brewpub", d(7, 8), "Bengaluru", total=3240, items=["Grilled chicken", "Paneer tikka", "Craft beer"],
    extra=["Client: Meridian Retail", "Guests: 2"])
add("priya", "jul-b", "restaurant", "Toit Brewpub", d(7, 11), "Bengaluru", total=3240, items=["Grilled chicken", "Paneer tikka", "Craft beer"],
    extra=["Client: Meridian Retail", "Guests: 2"])
add("priya", "jul-c", "generic", "Bangalore Tech Summit", d(7, 20), "Bengaluru", total=18500, items=["Conference pass - 2 day"])
add("priya", "jul-c", "telecom", "Airtel", d(7, 24), "Bengaluru", total=1180, account="88120044")
add("priya", "aug", "hotel", "Vivanta Bengaluru", d(8, 5), "Bengaluru", nights=3, rate=6200, extras=[("Minibar", 780)])

# ---- Rohan (L1, Pune)
add("rohan", "jul", "hotel", "Hotel Sayaji", d(7, 7), "Pune", nights=2, rate=4500, extras=[])
add("rohan", "jul", "taxi", "Pune City Taxi", d(7, 14), "Pune", total=380, route="Hinjawadi to Kothrud")
add("rohan", "jul", "restaurant", "Vaishali", d(7, 15), "Pune", total=1350, items=["Thali", "Misal pav", "Lassi"])
add("rohan", "jul", "telecom", "Jio", d(7, 30), "Pune", total=950, account="7700221")

# ---- Sneha (L4, Mumbai)
add("sneha", "jun", "hotel", "Trident Nariman Point", d(6, 20), "Mumbai", nights=2, rate=7400, extras=[("Minibar", 900), ("Laundry", 450)])
add("sneha", "jun", "restaurant", "Wasabi by Morimoto", d(6, 26), "Mumbai", total=14850, items=["Omakase", "Sake flight", "Sushi platter"],
    extra=["Client: Apex Logistics", "Guests: 6"])
add("sneha", "jul", "restaurant", "Pizza Express Bandra", d(7, 17), "Mumbai", total=4950, items=["Pizzas", "Pasta", "Soft drinks"],
    extra=["Team dinner", "Guests: 8"])
add("sneha", "jul", "telecom", "Airtel", d(7, 28), "Mumbai", total=1400, account="99120311")
add("sneha", "jul", "telecom", "ACT Fibernet", d(7, 29), "Mumbai", total=1499, account="ACT7712")
add("sneha", "aug", "hotel", "Taj Meridian", d(8, 12), "Mumbai", nights=1, rate=None, total=12400, extras=[])

# ---- Vikram (L5, Delhi)
add("vikram", "jun", "hotel", "The Leela Palace", d(6, 10), "Delhi", nights=3, rate=10500, extras=[("Minibar", 650), ("Spa", 2000)])
add("vikram", "jul", "restaurant", "Indian Accent", d(7, 4), "Delhi", total=9800, items=["Tasting menu", "Wine pairing"],
    extra=["Client: Zenith Bank", "Guests: 4"])
add("vikram", "jul", "telecom", "Airtel", d(7, 27), "Delhi", total=2100, account="55120988")
add("vikram", "aug", "taxi", "Delhi Prime Cabs", d(8, 1), "Delhi", total=610, route="Gurugram to Connaught Place")
add("vikram", "aug", "hotel", "Taj Meridian", d(8, 13), "Mumbai", nights=1, rate=None, total=12400, extras=[])

# ---- Ananya (L2, Kolkata)
add("ananya", "jun", "taxi", "Kolkata Yellow Cabs", d(6, 5), "Kolkata", total=495, route="Salt Lake to Park Street")
add("ananya", "jun", "taxi", "Kolkata Yellow Cabs", d(6, 19), "Kolkata", total=495, route="Howrah to Salt Lake")
add("ananya", "jul", "taxi", "Kolkata Yellow Cabs", d(7, 2), "Kolkata", total=495, route="Park Street to Salt Lake")
add("ananya", "jul", "telecom", "BSNL", d(7, 26), "Kolkata", total=810, account="33441122")
add("ananya", "aug", "generic", "Kolkata Stationers", d(8, 3), "Kolkata", total=1850, items=["Notebooks", "Pens", "Folders", "Marker set"])
add("ananya", "aug", "generic", "Croma Electronics", d(8, 20), "Kolkata", total=2400, items=["Wireless headset"])

# ---- Karthik (L3, Hyderabad): a burst in one week, plus round amounts
add("karthik", "wk", "taxi", "Hyd Cabs", d(8, 10), "Hyderabad", total=310, route="HITEC City to Banjara Hills")
add("karthik", "wk", "taxi", "Hyd Cabs", d(8, 10), "Hyderabad", total=280, route="Banjara Hills to Gachibowli")
add("karthik", "wk", "restaurant", "Paradise Biryani", d(8, 11), "Hyderabad", total=450, items=["Chicken biryani", "Raita", "Soft drink"])
add("karthik", "wk", "taxi", "Hyd Cabs", d(8, 11), "Hyderabad", total=360, route="Gachibowli to Secunderabad")
add("karthik", "wk", "taxi", "Hyd Cabs", d(8, 12), "Hyderabad", total=330, route="Secunderabad to HITEC City",
    edit={"amount": "530.00", "reason": "Airport surcharge and parking were extra."})
add("karthik", "wk", "restaurant", "Ohri's", d(8, 13), "Hyderabad", total=520, items=["Paneer meal", "Naan", "Lassi"])
add("karthik", "wk", "taxi", "Hyd Cabs", d(8, 14), "Hyderabad", total=300, route="HITEC City to Kondapur")
add("karthik", "jul", "telecom", "Jio", d(7, 28), "Hyderabad", total=1000, account="7711900")
add("karthik", "aug", "restaurant", "Barbeque Nation", d(8, 20), "Hyderabad", total=5000, items=["Buffet", "Beverages"], extra=["Team dinner", "Guests: 5"])
add("karthik", "aug", "hotel", "Novotel Hyderabad", d(8, 18), "Hyderabad", nights=6, rate=4100, extras=[("Laundry", 1800)])

# ---- Meera (L1, Jaipur)
add("meera", "jun", "hotel", "Clarks Amer", d(6, 15), "Jaipur", nights=2, rate=2800, extras=[])
add("meera", "jun", "restaurant", "Rawat Misthan", d(6, 16), "Jaipur", total=890, items=["Kachori", "Lassi", "Thali"])
add("meera", "jul", "generic", "Coursera Enterprise", d(7, 9), "Jaipur", total=58000, items=["Professional certificate programme"])
add("meera", "jul", "taxi", "Jaipur Auto Cabs", d(7, 20), "Jaipur", total=2000, route="Airport to Malviya Nagar")
add("meera", "jul", "telecom", "Jio", d(7, 29), "Jaipur", total=800, account="7799001")
