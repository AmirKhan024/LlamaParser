# Demo run report

Smoke test / demo script, not an eval: no expected verdicts or clauses exist, and no accuracy number is reported.

## Stage 1 (extraction)

- receipts rendered: 48; uploaded: 48; extracted OK: 48; confirmed: 48; claims submitted: 23 of 23

| document status | n |
|---|---|
| confirmed | 48 |

Extracted document types:

| type | n |
|---|---|
| taxi_receipt | 13 |
| restaurant_bill | 12 |
| hotel_invoice | 10 |
| telecom_bill | 9 |
| generic_receipt | 4 |

Stage 2 categories assigned:

| category | n |
|---|---|
| local_transport | 13 |
| accommodation | 10 |
| travel_meals | 9 |
| phone_internet | 9 |
| training_conferences | 2 |
| team_events | 2 |
| client_entertainment | 1 |
| other | 1 |
| office_supplies_equipment | 1 |

Failing Stage 1 arithmetic checks (AI version):

| check | n |
|---|---|
| (none) | 0 |

Extraction sanity -- amount read differs from the total printed on the receipt:

- none

Oddities / crashes logged by the runner:

- vikram/jul: evaluate 503: {"detail":"The policy model is unavailable; nothing was stored. RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gp
- vikram/jul: evaluate 503: {"detail":"The policy model is unavailable; nothing was stored. RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gp
- vikram/jul: evaluate 503: {"detail":"The policy model is unavailable; nothing was stored. RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gp
- vikram/jul: evaluate 503: {"detail":"The policy model is unavailable; nothing was stored. RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gp
- vikram/jul: evaluate 503: {"detail":"The policy model is unavailable; nothing was stored. RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gp
- vikram/jul: evaluate 503: {"detail":"The policy model is unavailable; nothing was stored. RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gp
- vikram/aug: evaluate 503: {"detail":"The policy model is unavailable; nothing was stored. RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gp
- vikram/aug: evaluate 503: {"detail":"The policy model is unavailable; nothing was stored. RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gp
- vikram/aug: evaluate 503: {"detail":"The policy model is unavailable; nothing was stored. RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gp
- vikram/aug: evaluate 503: {"detail":"The policy model is unavailable; nothing was stored. RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gp
- vikram/aug: evaluate 503: {"detail":"The policy model is unavailable; nothing was stored. RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gp
- vikram/aug: evaluate 503: {"detail":"The policy model is unavailable; nothing was stored. RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model `openai/gp

## Stage 3 (policy decisions, latest run per claim)

- units: 42 across 17 claims; model: ['openai/gpt-oss-120b', 'openai/gpt-oss-20b']

Verdict distribution:

| verdict | n |
|---|---|
| insufficient_information | 18 |
| compliant | 14 |
| violation | 10 |

Verdict by category:

| category / verdict | n |
|---|---|
| travel_meals / insufficient_information | 8 |
| accommodation / violation | 7 |
| phone_internet / compliant | 6 |
| accommodation / insufficient_information | 6 |
| accommodation / compliant | 3 |
| client_entertainment / compliant | 2 |
| local_transport / compliant | 2 |
| team_events / insufficient_information | 2 |
| local_transport / insufficient_information | 1 |
| local_transport / violation | 1 |
| travel_meals / violation | 1 |
| training_conferences / compliant | 1 |
| phone_internet / violation | 1 |
| training_conferences / insufficient_information | 1 |

Clauses selected:

| clause | n |
|---|---|
| 8.1 | 8 |
| 9.2 | 7 |
| 12.1 | 7 |
| 5.3 | 3 |
| 8.5 | 3 |
| 8.4 | 3 |
| 9.3 | 2 |
| 8.2 | 1 |
| 10.2 | 1 |
| 10.3 | 1 |
| 15.2 | 1 |
| (none) | 1 |
| 19.2 | 1 |
| 11.1 | 1 |
| 15.1 | 1 |
| 5.3.1 | 1 |

Top blocking missing_fields (insufficient_information units):

| field | n |
|---|---|
| trip_days | 9 |
| city_tier | 3 |
| line_item_names | 3 |
| stay_nights | 1 |
| system:bad_partition | 1 |
| occasion | 1 |
| city tier (required to select the correct per‑day cap) | 1 |
| document_amount | 1 |

Hard failures (system:*): 1

  - None: the document has 3 line items (0..2); the units cover [0] -- every line item must be covered exactly once

## Stage 4 (fraud rules, latest assessment per claim)

- claims assessed: 23

Risk bands:

| band | n |
|---|---|
| low | 15 |
| high | 4 |
| medium | 4 |

| rule | fired | clear | not_applicable |
|---|---|---|---|
| duplicate_same_employee | 4 | 19 | 0 |
| duplicate_cross_employee | 2 | 21 | 0 |
| threshold_gaming | 3 | 20 | 0 |
| velocity | 2 | 21 | 0 |
| round_number | 5 | 17 | 1 |
| weekend_business | 1 | 22 | 0 |
| policy_repeat_violation | 0 | 14 | 9 |
| correction_upward | 2 | 21 | 0 |
| correction_guardrail | 2 | 21 | 0 |

Mean assessable signals: 8.6 of 9
Narratives: {'ok': 14, 'not_needed': 9}
