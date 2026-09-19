"""Stage 4 configuration: every weight, band and threshold of the fraud rule
engine lives in this one file. The score is a plain weighted sum of FIRED
rules computed in code (fraud_rules.score); no model ever produces or adjusts
it. Change a value here and bump RULESET_VERSION so old assessments stay
attributable to the ruleset that produced them.
"""

RULESET_VERSION = "v1"
NARRATIVE_PROMPT_VERSION = "v1"

# Severity weight added to the risk score when a rule fires.
WEIGHTS: dict[str, int] = {
    "duplicate_same_employee": 30,
    "duplicate_cross_employee": 25,
    "threshold_gaming": 20,
    "velocity": 15,
    "round_number": 8,
    "weekend_business": 15,
    "policy_repeat_violation": 15,
    "correction_upward": 20,
    "correction_guardrail": 35,
}

# score >= BAND_MEDIUM -> medium, >= BAND_HIGH -> high (score is capped at 100).
BAND_MEDIUM = 25
BAND_HIGH = 50

# With fewer assessable rules than this and nothing fired, the band is
# "unassessable" -- "could not assess" is never reported as "low risk".
MIN_ASSESSABLE_SIGNALS = 3

# --- duplicate / near-duplicate: same vendor + same amount within N days
DUP_WINDOW_DAYS_SAME_EMPLOYEE = 14
DUP_WINDOW_DAYS_CROSS_EMPLOYEE = 14

# --- threshold gaming: amount within this % just BELOW a policy threshold
THRESHOLD_MARGIN_PCT = 5

# --- velocity: per employee per ISO week
VELOCITY_MAX_DOCS_PER_WEEK = 5
VELOCITY_MAX_TOTAL_PER_WEEK = {"INR": 25000}     # currencies not listed are not totalled

# --- round numbers: multiple of STEP and at least MIN (per currency)
ROUND_STEP = {"INR": 500}
ROUND_MIN = {"INR": 1000}

# --- weekend: categories whose implied business activity makes Sat/Sun suspicious.
# (Out-of-hours needs a time of day, which Stage 1 does not extract -- so it is not assessed.)
WEEKEND_CATEGORIES = {"client_entertainment", "team_events"}

# --- policy signal: this many violations of the same clause by one employee
POLICY_REPEAT_MIN_VIOLATIONS = 2

# --- correction signal: Stage 1 corrections.field_path leaves that are money
MONEY_FIELD_KEYS = {
    "amount", "total", "subtotal", "tax", "cgst", "sgst", "grand_total",
    "total_conveyance_amount", "daily_allowance_amount",
    "vehicle_maintenance_amount", "mobile_allowance_amount", "total_claimed", "unit_price",
}
