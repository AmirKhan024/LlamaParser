# Categorization eval results (FINAL, gold labels)

## Decision rule

Fixed before the `llm` numbers were looked at.

- **PRIMARY metric:** accuracy on the real-source documents only (sroie + cord + real). Synthetic documents are known to inflate scores, so they don't decide. The real slice scores 43 documents: the 44 real documents minus one approval email that is evidence, not an expense, and has no category.
- **SECONDARY metric:** accuracy on all scored documents (n=133).
- If one method has the strictly highest accuracy on BOTH metrics, it becomes the default.
- If the winners are split (or first place is tied on either metric), the default stays `rules` -- zero cost, deterministic, no quota dependency -- and the split is explained explicitly, along with what would change the call.
- Cost and latency are a tiebreaker consideration only, never the deciding metric.

## Comparison

| method | all docs: accuracy | all docs: macro-F1 | real docs: accuracy (95% CI) | real docs: macro-F1† | synthetic: accuracy | parse failures | mean latency / doc | total tokens | est. cost / 1,000 docs |
|---|---|---|---|---|---|---|---|---|---|
| rules | 87.2% (n=133) | 0.894 | 83.7% (n=43; 70%–92%) | 0.805 | 88.9% (n=90) | n/a | 0 ms | n/a | $0.0000 |
| classifier | 70.7% (n=133) | 0.706 | 46.5% (n=43; 33%–61%) | 0.357 | 82.2% (n=90) | n/a | 108 ms | n/a | $0.0000 |
| llm | 90.2% (n=133) | 0.931 | 69.8% (n=43; 55%–81%) | 0.732 | 100.0% (n=90) | 0 | 3684 ms | 210,986 | $0.3142 |
| hybrid | not pursued — decision: two-method comparison was sufficient | | | | | | | | |

† The real-only macro-F1 is averaged over only the 7 categories that slice contains (accommodation, local_transport, office_supplies_equipment, other, own_vehicle_mileage, phone_internet, travel_meals), not all 14. Estimated costs use unverified public Groq rates.

## Decision outcome

- Primary (real-only accuracy): `rules` at 83.7%.
- Secondary (all-documents accuracy): `llm` at 90.2%.
- **The winners are split (or first place is tied) -> the default stays `rules`.**

Real-only accuracy: `rules` 83.7%, `llm` 69.8%, `classifier` 46.5%. With only 43 real documents, one document is worth 2.3 points and `llm`'s real-only 95% interval is 54.9%–81.4% -- differences of a few points on this slice are not distinguishable from noise. What would change the call: `llm` becomes the default if it strictly beats `rules` on both real-only and all-documents accuracy -- most convincingly on a larger real-document set that covers more than the 7 categories this one does, and after checking the labels blind (see limitations).

- Real documents, `rules` vs `llm` (same 43 documents): both right 26, only `rules` right 10, only `llm` right 4, both wrong 3.
- Real documents, `rules` vs `classifier` (same 43 documents): both right 14, only `rules` right 22, only `classifier` right 6, both wrong 1.

Cost and latency (tiebreaker consideration only, not what decided this):
- `rules`: mean 0 ms per doc, ~$0.0000 per 1,000 docs, no API dependency.
- `classifier`: mean 108 ms per doc, ~$0.0000 per 1,000 docs, no API dependency.
- `llm`: mean 3684 ms per doc, ~$0.3142 per 1,000 docs, needs Groq quota and a network call.

## The `llm` method: prompt design and provenance

- **Model / settings:** Groq `openai/gpt-oss-120b`, temperature 0, reasoning effort left at the provider default. One request per document; a Groq JSON-validation rejection is retried once, then recorded as a parse failure; 429s back off exponentially.
- **Prompt:** few-shot. The system prompt lists all 14 categories with the one-line definition from `categories.py`, plus the tie-break rules from `categories.py`. Three worked examples follow as user/assistant turns.
- **Where the few-shot examples came from:** hand-written for this prompt (an airline e-ticket, a client dinner, a hotel folio with minibar/laundry lines), using fictional vendors, clients and people. **None is a row of the 134-document eval set** -- `tests/test_llm_categorizer.py` checks that none of their names appear in `dataset.jsonl`, and none of the eval's documents were used to tune the prompt. The prompt was run once against the eval set; it was not iterated on the results.
- **What the model sees:** the Stage 1 extracted fields only -- `document_type`, vendor, date, amount + currency, line-item names, and `additional_fields` -- and never the OCR markdown. `additional_fields` is included on purpose: for the synthetic client/team dinners the distinguishing signal (client name, attendee count, "Team Dinner") lives only there, so a literal vendor/date/amount/line-items input would make `client_entertainment`, `team_events` and `travel_meals` indistinguishable by construction. **`rules` and `classifier` are not given identical information:** they read a 2,000-character OCR markdown excerpt. So an `llm`-vs-`rules` difference reflects input as well as method.
- **Output validation:** strict JSON `{category, confidence, reason}`; the category must equal one of the 14 ids exactly. Anything else (not JSON, no category, `null`, an id not on the list, wrong case) is a **parse failure**: stored as no answer, scored as a miss, and counted separately -- never coerced to the nearest category.
- **Caching:** every request/response is in `eval/categorization/_llm_raw_cache/<doc id>.json`; predictions are checkpointed per document in `_predictions_cache/llm.jsonl` with a config fingerprint so a changed prompt can never reuse stale rows.

## rules

- Mean latency per doc: 0 ms

### All documents

- n = 133
- Accuracy: 87.2%
- Macro-F1: 0.894
- Top confusions (gold → predicted): travel_meals → other (6); training_conferences → other (4); travel_documents_fees → other (2); travel_documents_fees → software_subscriptions (2); local_transport → other (1)
- Latency p50: 0 ms
- Estimated cost per 1,000 docs: $0.0000

| category | precision | recall | support |
|---|---|---|---|
| accommodation | 0.89 | 1.00 | 8 |
| client_entertainment | 1.00 | 1.00 | 8 |
| fuel | 1.00 | 1.00 | 8 |
| intercity_travel | 1.00 | 1.00 | 8 |
| local_transport | 1.00 | 0.89 | 9 |
| office_supplies_equipment | 1.00 | 0.88 | 8 |
| other | 0.56 | 1.00 | 18 |
| own_vehicle_mileage | 1.00 | 1.00 | 8 |
| phone_internet | 1.00 | 1.00 | 8 |
| software_subscriptions | 0.78 | 0.88 | 8 |
| team_events | 1.00 | 1.00 | 6 |
| training_conferences | 1.00 | 0.50 | 8 |
| travel_documents_fees | 1.00 | 0.50 | 8 |
| travel_meals | 1.00 | 0.70 | 20 |

Confusion matrix:

| true \ pred | accommodation | client_entertainment | fuel | intercity_travel | local_transport | office_supplies_equipment | other | own_vehicle_mileage | phone_internet | software_subscriptions | team_events | training_conferences | travel_documents_fees | travel_meals |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| accommodation | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| client_entertainment | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| fuel | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| intercity_travel | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| local_transport | 0 | 0 | 0 | 0 | 8 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| office_supplies_equipment | 0 | 0 | 0 | 0 | 0 | 7 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 0 | 0 | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| own_vehicle_mileage | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 |
| phone_internet | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 |
| software_subscriptions | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 7 | 0 | 0 | 0 | 0 |
| team_events | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 0 | 0 | 0 |
| training_conferences | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 0 | 0 | 0 | 0 | 4 | 0 | 0 |
| travel_documents_fees | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 2 | 0 | 0 | 4 | 0 |
| travel_meals | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 14 |

### Real-source only (sroie + cord + real)

- n = 43
- Accuracy: 83.7%
- Macro-F1: 0.805
- **Categories covered: 7/14** (accommodation, local_transport, office_supplies_equipment, other, own_vehicle_mileage, phone_internet, travel_meals) -- macro-F1 above is only averaged over these categories, not all 14. Treat it as a read on this slice's categories, not overall category coverage.
- Top confusions (gold → predicted): travel_meals → other (6); local_transport → other (1)
- Latency p50: 0 ms
- Estimated cost per 1,000 docs: $0.0000

| category | precision | recall | support |
|---|---|---|---|
| accommodation | 1.00 | 1.00 | 1 |
| local_transport | 0.00 | 0.00 | 1 |
| office_supplies_equipment | 1.00 | 1.00 | 3 |
| other | 0.72 | 1.00 | 18 |
| own_vehicle_mileage | 1.00 | 1.00 | 1 |
| phone_internet | 1.00 | 1.00 | 1 |
| travel_meals | 1.00 | 0.67 | 18 |

Confusion matrix:

| true \ pred | accommodation | local_transport | office_supplies_equipment | other | own_vehicle_mileage | phone_internet | travel_meals |
|---|---|---|---|---|---|---|---|
| accommodation | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| local_transport | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| office_supplies_equipment | 0 | 0 | 3 | 0 | 0 | 0 | 0 |
| other | 0 | 0 | 0 | 18 | 0 | 0 | 0 |
| own_vehicle_mileage | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| phone_internet | 0 | 0 | 0 | 0 | 0 | 1 | 0 |
| travel_meals | 0 | 0 | 0 | 6 | 0 | 0 | 12 |

### Synthetic-only

- n = 90
- Accuracy: 88.9%
- Macro-F1: 0.856
- Top confusions (gold → predicted): training_conferences → other (4); travel_documents_fees → other (2); travel_documents_fees → software_subscriptions (2); office_supplies_equipment → other (1); software_subscriptions → accommodation (1)
- Latency p50: 0 ms
- Estimated cost per 1,000 docs: $0.0000

| category | precision | recall | support |
|---|---|---|---|
| accommodation | 0.88 | 1.00 | 7 |
| client_entertainment | 1.00 | 1.00 | 8 |
| fuel | 1.00 | 1.00 | 8 |
| intercity_travel | 1.00 | 1.00 | 8 |
| local_transport | 1.00 | 1.00 | 8 |
| office_supplies_equipment | 1.00 | 0.80 | 5 |
| other | 0.00 | 0.00 | 0 |
| own_vehicle_mileage | 1.00 | 1.00 | 7 |
| phone_internet | 1.00 | 1.00 | 7 |
| software_subscriptions | 0.78 | 0.88 | 8 |
| team_events | 1.00 | 1.00 | 6 |
| training_conferences | 1.00 | 0.50 | 8 |
| travel_documents_fees | 1.00 | 0.50 | 8 |
| travel_meals | 1.00 | 1.00 | 2 |

Confusion matrix:

| true \ pred | accommodation | client_entertainment | fuel | intercity_travel | local_transport | office_supplies_equipment | other | own_vehicle_mileage | phone_internet | software_subscriptions | team_events | training_conferences | travel_documents_fees | travel_meals |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| accommodation | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| client_entertainment | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| fuel | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| intercity_travel | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| local_transport | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| office_supplies_equipment | 0 | 0 | 0 | 0 | 0 | 4 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| own_vehicle_mileage | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| phone_internet | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 |
| software_subscriptions | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 7 | 0 | 0 | 0 | 0 |
| team_events | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 0 | 0 | 0 |
| training_conferences | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 0 | 0 | 0 | 0 | 4 | 0 | 0 |
| travel_documents_fees | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 2 | 0 | 0 | 4 | 0 |
| travel_meals | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2 |

## classifier

- Mean latency per doc: 108 ms

### All documents

- n = 133
- Accuracy: 70.7%
- Macro-F1: 0.706
- Top confusions (gold → predicted): client_entertainment → travel_meals (6); team_events → travel_meals (6); local_transport → own_vehicle_mileage (4); travel_meals → other (4); office_supplies_equipment → other (3)
- Latency p50: 16 ms
- Estimated cost per 1,000 docs: $0.0000

| category | precision | recall | support |
|---|---|---|---|
| accommodation | 0.80 | 1.00 | 8 |
| client_entertainment | 1.00 | 0.25 | 8 |
| fuel | 0.73 | 1.00 | 8 |
| intercity_travel | 1.00 | 1.00 | 8 |
| local_transport | 1.00 | 0.56 | 9 |
| office_supplies_equipment | 0.71 | 0.62 | 8 |
| other | 0.38 | 0.28 | 18 |
| own_vehicle_mileage | 0.62 | 1.00 | 8 |
| phone_internet | 0.88 | 0.88 | 8 |
| software_subscriptions | 0.73 | 1.00 | 8 |
| team_events | 0.00 | 0.00 | 6 |
| training_conferences | 1.00 | 1.00 | 8 |
| travel_documents_fees | 1.00 | 1.00 | 8 |
| travel_meals | 0.48 | 0.70 | 20 |

Confusion matrix:

| true \ pred | accommodation | client_entertainment | fuel | intercity_travel | local_transport | office_supplies_equipment | other | own_vehicle_mileage | phone_internet | software_subscriptions | team_events | training_conferences | travel_documents_fees | travel_meals |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| accommodation | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| client_entertainment | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 6 |
| fuel | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| intercity_travel | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| local_transport | 0 | 0 | 0 | 0 | 5 | 0 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 |
| office_supplies_equipment | 0 | 0 | 0 | 0 | 0 | 5 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| other | 2 | 0 | 2 | 0 | 0 | 2 | 5 | 0 | 1 | 3 | 0 | 0 | 0 | 3 |
| own_vehicle_mileage | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 |
| phone_internet | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 7 | 0 | 0 | 0 | 0 | 0 |
| software_subscriptions | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 |
| team_events | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 6 |
| training_conferences | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 |
| travel_documents_fees | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 |
| travel_meals | 0 | 0 | 1 | 0 | 0 | 0 | 4 | 1 | 0 | 0 | 0 | 0 | 0 | 14 |

### Real-source only (sroie + cord + real)

- n = 43
- Accuracy: 46.5%
- Macro-F1: 0.357
- **Categories covered: 7/14** (accommodation, local_transport, office_supplies_equipment, other, own_vehicle_mileage, phone_internet, travel_meals) -- macro-F1 above is only averaged over these categories, not all 14. Treat it as a read on this slice's categories, not overall category coverage.
- Top confusions (gold → predicted): travel_meals → other (4); office_supplies_equipment → other (3); other → software_subscriptions (3); other → travel_meals (3); other → accommodation (2)
- Latency p50: 30 ms
- Estimated cost per 1,000 docs: $0.0000

| category | precision | recall | support |
|---|---|---|---|
| accommodation | 0.33 | 1.00 | 1 |
| fuel | 0.00 | 0.00 | 0 |
| local_transport | 1.00 | 1.00 | 1 |
| office_supplies_equipment | 0.00 | 0.00 | 3 |
| other | 0.38 | 0.28 | 18 |
| own_vehicle_mileage | 0.50 | 1.00 | 1 |
| phone_internet | 0.00 | 0.00 | 1 |
| software_subscriptions | 0.00 | 0.00 | 0 |
| travel_meals | 0.80 | 0.67 | 18 |

Confusion matrix:

| true \ pred | accommodation | fuel | local_transport | office_supplies_equipment | other | own_vehicle_mileage | phone_internet | software_subscriptions | travel_meals |
|---|---|---|---|---|---|---|---|---|---|
| accommodation | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| fuel | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| local_transport | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| office_supplies_equipment | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 | 0 |
| other | 2 | 2 | 0 | 2 | 5 | 0 | 1 | 3 | 3 |
| own_vehicle_mileage | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| phone_internet | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 |
| software_subscriptions | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| travel_meals | 0 | 1 | 0 | 0 | 4 | 1 | 0 | 0 | 12 |

### Synthetic-only

- n = 90
- Accuracy: 82.2%
- Macro-F1: 0.776
- Top confusions (gold → predicted): client_entertainment → travel_meals (6); team_events → travel_meals (6); local_transport → own_vehicle_mileage (4)
- Latency p50: 15 ms
- Estimated cost per 1,000 docs: $0.0000

| category | precision | recall | support |
|---|---|---|---|
| accommodation | 1.00 | 1.00 | 7 |
| client_entertainment | 1.00 | 0.25 | 8 |
| fuel | 1.00 | 1.00 | 8 |
| intercity_travel | 1.00 | 1.00 | 8 |
| local_transport | 1.00 | 0.50 | 8 |
| office_supplies_equipment | 1.00 | 1.00 | 5 |
| own_vehicle_mileage | 0.64 | 1.00 | 7 |
| phone_internet | 1.00 | 1.00 | 7 |
| software_subscriptions | 1.00 | 1.00 | 8 |
| team_events | 0.00 | 0.00 | 6 |
| training_conferences | 1.00 | 1.00 | 8 |
| travel_documents_fees | 1.00 | 1.00 | 8 |
| travel_meals | 0.14 | 1.00 | 2 |

Confusion matrix:

| true \ pred | accommodation | client_entertainment | fuel | intercity_travel | local_transport | office_supplies_equipment | own_vehicle_mileage | phone_internet | software_subscriptions | team_events | training_conferences | travel_documents_fees | travel_meals |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| accommodation | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| client_entertainment | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 6 |
| fuel | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| intercity_travel | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| local_transport | 0 | 0 | 0 | 0 | 4 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 |
| office_supplies_equipment | 0 | 0 | 0 | 0 | 0 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| own_vehicle_mileage | 0 | 0 | 0 | 0 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| phone_internet | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 |
| software_subscriptions | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 |
| team_events | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 6 |
| training_conferences | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 |
| travel_documents_fees | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 |
| travel_meals | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2 |

## llm

- Model: `openai/gpt-oss-120b`, temperature 0, reasoning effort: provider default; prompt/config fingerprint `12ce646ee78331fc`
- **Parse failures / invalid category returns: 0** of 133 scored documents
- Mean latency per doc: 3684 ms
- Total tokens for the run: 210,986 (est. $0.0418, unverified Groq rates)

### All documents

- n = 133
- Accuracy: 90.2%
- Macro-F1: 0.931
- Top confusions (gold → predicted): other → office_supplies_equipment (10); travel_meals → other (2); travel_meals → team_events (1)
- Latency p50: 984 ms
- Estimated cost per 1,000 docs: $0.3142

| category | precision | recall | support |
|---|---|---|---|
| accommodation | 1.00 | 1.00 | 8 |
| client_entertainment | 1.00 | 1.00 | 8 |
| fuel | 1.00 | 1.00 | 8 |
| intercity_travel | 1.00 | 1.00 | 8 |
| local_transport | 1.00 | 1.00 | 9 |
| office_supplies_equipment | 0.44 | 1.00 | 8 |
| other | 0.80 | 0.44 | 18 |
| own_vehicle_mileage | 1.00 | 1.00 | 8 |
| phone_internet | 1.00 | 1.00 | 8 |
| software_subscriptions | 1.00 | 1.00 | 8 |
| team_events | 0.86 | 1.00 | 6 |
| training_conferences | 1.00 | 1.00 | 8 |
| travel_documents_fees | 1.00 | 1.00 | 8 |
| travel_meals | 1.00 | 0.85 | 20 |

Confusion matrix:

| true \ pred | accommodation | client_entertainment | fuel | intercity_travel | local_transport | office_supplies_equipment | other | own_vehicle_mileage | phone_internet | software_subscriptions | team_events | training_conferences | travel_documents_fees | travel_meals |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| accommodation | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| client_entertainment | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| fuel | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| intercity_travel | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| local_transport | 0 | 0 | 0 | 0 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| office_supplies_equipment | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 0 | 10 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| own_vehicle_mileage | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 |
| phone_internet | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 |
| software_subscriptions | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 |
| team_events | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 0 | 0 | 0 |
| training_conferences | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 |
| travel_documents_fees | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 |
| travel_meals | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 1 | 0 | 0 | 17 |

### Real-source only (sroie + cord + real)

- n = 43
- Accuracy: 69.8%
- Macro-F1: 0.732
- **Categories covered: 7/14** (accommodation, local_transport, office_supplies_equipment, other, own_vehicle_mileage, phone_internet, travel_meals) -- macro-F1 above is only averaged over these categories, not all 14. Treat it as a read on this slice's categories, not overall category coverage.
- Top confusions (gold → predicted): other → office_supplies_equipment (10); travel_meals → other (2); travel_meals → team_events (1)
- Latency p50: 4407 ms
- Estimated cost per 1,000 docs: $0.3748

| category | precision | recall | support |
|---|---|---|---|
| accommodation | 1.00 | 1.00 | 1 |
| local_transport | 1.00 | 1.00 | 1 |
| office_supplies_equipment | 0.23 | 1.00 | 3 |
| other | 0.80 | 0.44 | 18 |
| own_vehicle_mileage | 1.00 | 1.00 | 1 |
| phone_internet | 1.00 | 1.00 | 1 |
| team_events | 0.00 | 0.00 | 0 |
| travel_meals | 1.00 | 0.83 | 18 |

Confusion matrix:

| true \ pred | accommodation | local_transport | office_supplies_equipment | other | own_vehicle_mileage | phone_internet | team_events | travel_meals |
|---|---|---|---|---|---|---|---|---|
| accommodation | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| local_transport | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| office_supplies_equipment | 0 | 0 | 3 | 0 | 0 | 0 | 0 | 0 |
| other | 0 | 0 | 10 | 8 | 0 | 0 | 0 | 0 |
| own_vehicle_mileage | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| phone_internet | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| team_events | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| travel_meals | 0 | 0 | 0 | 2 | 0 | 0 | 1 | 15 |

### Synthetic-only

- n = 90
- Accuracy: 100.0%
- Macro-F1: 1.000
- Latency p50: 734 ms
- Estimated cost per 1,000 docs: $0.2853

| category | precision | recall | support |
|---|---|---|---|
| accommodation | 1.00 | 1.00 | 7 |
| client_entertainment | 1.00 | 1.00 | 8 |
| fuel | 1.00 | 1.00 | 8 |
| intercity_travel | 1.00 | 1.00 | 8 |
| local_transport | 1.00 | 1.00 | 8 |
| office_supplies_equipment | 1.00 | 1.00 | 5 |
| own_vehicle_mileage | 1.00 | 1.00 | 7 |
| phone_internet | 1.00 | 1.00 | 7 |
| software_subscriptions | 1.00 | 1.00 | 8 |
| team_events | 1.00 | 1.00 | 6 |
| training_conferences | 1.00 | 1.00 | 8 |
| travel_documents_fees | 1.00 | 1.00 | 8 |
| travel_meals | 1.00 | 1.00 | 2 |

Confusion matrix:

| true \ pred | accommodation | client_entertainment | fuel | intercity_travel | local_transport | office_supplies_equipment | own_vehicle_mileage | phone_internet | software_subscriptions | team_events | training_conferences | travel_documents_fees | travel_meals |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| accommodation | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| client_entertainment | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| fuel | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| intercity_travel | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| local_transport | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| office_supplies_equipment | 0 | 0 | 0 | 0 | 0 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| own_vehicle_mileage | 0 | 0 | 0 | 0 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| phone_internet | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 |
| software_subscriptions | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 |
| team_events | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 0 | 0 | 0 |
| training_conferences | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 |
| travel_documents_fees | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 |
| travel_meals | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2 |


## Error analysis (rules)

17 misclassified row(s), grouped by (gold -> predicted):

### travel_meals -> other (6)
- `cat-cord-002` (cord, vendor: None): 
- `cat-cord-003` (cord, vendor: Dynamic Bakery & Cake Natural): 
- `cat-cord-004` (cord, vendor: None): 
- `cat-cord-007` (cord, vendor: None): 
- `cat-cord-008` (cord, vendor: None): 
- `cat-cord-011` (cord, vendor: None): 

### training_conferences -> other (4)
- `synth-training_conferences-02` (synthetic_image, vendor: Skillbridge Academy): expected category: training_conferences
- `synth-training_conferences-04` (synthetic_image, vendor: TechConf India): expected category: training_conferences
- `synth-training_conferences-05` (synthetic_image, vendor: Skillbridge Academy): expected category: training_conferences
- `synth-training_conferences-06` (synthetic_image, vendor: TechConf India): expected category: training_conferences

### travel_documents_fees -> other (2)
- `synth-travel_documents_fees-05` (synthetic_image, vendor: GlobalTravel Docs): expected category: travel_documents_fees
- `synth-travel_documents_fees-07` (synthetic_image, vendor: GlobalTravel Docs): expected category: travel_documents_fees

### travel_documents_fees -> software_subscriptions (2)
- `synth-travel_documents_fees-00` (synthetic_image, vendor: VisaAssist Services): expected category: travel_documents_fees
- `synth-travel_documents_fees-03` (synthetic_image, vendor: VisaAssist Services): expected category: travel_documents_fees

### local_transport -> other (1)
- `cat-sroie-011` (sroie, vendor: SECURE PARKING CORPORATION S/B): 

### office_supplies_equipment -> other (1)
- `synth-office_supplies_equipment-02` (synthetic_image, vendor: OfficeMart): expected category: office_supplies_equipment

### software_subscriptions -> accommodation (1)
- `synth-software_subscriptions-07` (synthetic_image, vendor: CloudSuite Pro): expected category: software_subscriptions


## Error analysis (llm)

13 misclassified row(s), grouped by (gold -> predicted):

### other -> office_supplies_equipment (10)
- `cat-sroie-000` (sroie, vendor: OJC MARKETING SDN BHD): model said: "The receipt is for safety shoes, a small physical purchase for work, fitting the office supplies/equipment category."
- `cat-sroie-001` (sroie, vendor: OJC MARKETING SDN BHD): model said: "The receipt is for safety shoes, a physical office supply/equipment purchase."
- `cat-sroie-002` (sroie, vendor: GOGIANT ENGINEERING (M) SDN BHD): model said: "The receipt lists welding consumables and tools, which are physical supplies for work, fitting the office supplies/equipment category."
- `cat-sroie-006` (sroie, vendor: CROSS CHANNEL NETWORK SDN. BHD.): model said: "The receipt is for a hardware item (switch socket outlet) purchased for work, fitting the office supplies/equipment category."
- `cat-sroie-007` (sroie, vendor: CROSS CHANNEL NETWORK SDN. BHD.): model said: "The receipt lists PVC conduit, connectors, and cable clips, which are physical supplies for work, fitting the office supplies/equipment category."
- `cat-sroie-008` (sroie, vendor: SWC ENTERPRISE SDN BHD): model said: "A generic receipt for a small physical item purchase fits the office supplies/equipment category."
- `cat-sroie-009` (sroie, vendor: LIGHTROOM GALLERY SDN BHD): model said: "The receipt is for LED lighting hardware and accessories, a physical purchase for work, which falls under office supplies/equipment."
- `cat-sroie-012` (sroie, vendor: LIGHTROOM GALLERY SDN BHD): model said: "The receipt is for physical lighting equipment and accessories, which are small physical purchases for work, fitting the office_supplies_equipment category."
- `cat-sroie-018` (sroie, vendor: SWC ENTERPRISE SDN BHD): model said: "A generic small receipt with no travel or service context likely represents a purchase of office supplies or equipment."
- `cat-sroie-022` (sroie, vendor: YONG TAT HARDWARE TRADING): model said: "A receipt from a hardware trading store for a small purchase of physical items fits the office supplies/equipment category."

### travel_meals -> other (2)
- `cat-cord-003` (cord, vendor: Dynamic Bakery & Cake Natural): model said: "A bakery receipt for food items does not match any specific travel or software category, so it falls under other business-related purchases."
- `cat-cord-007` (cord, vendor: None): model said: "A generic food receipt without travel or client context does not fit specific categories, so it falls under 'other'."

### travel_meals -> team_events (1)
- `cat-cord-011` (cord, vendor: None): model said: "A cake purchase without client reference is likely for a team celebration, fitting the team_events category."

## What-if: how much the primary metric leans on one label boundary (NOT applied)

`llm`'s largest real-document disagreement is gold `other` predicted as `office_supplies_equipment` on 10 of 43 real documents (`cat-sroie-000`, `cat-sroie-001`, `cat-sroie-002`, `cat-sroie-006`, `cat-sroie-007`, `cat-sroie-008`, `cat-sroie-009`, `cat-sroie-012`, `cat-sroie-018`, `cat-sroie-022`). `categories.py` defines `office_supplies_equipment` as "Stationery, small peripherals, and other small physical purchases for work.", and the model's stated reasons apply that wording literally. Whether these documents are `other` or `office_supplies_equipment` is a label-boundary judgment; the gold label was `other`, approved in review with the label visible (see the anchoring limitation).

**The gold labels were not changed and the decision above stands.** For scale only: real-only accuracy if those 10 documents had been labeled `office_supplies_equipment` instead:

| method | real-only accuracy as recorded | real-only accuracy under the what-if |
|---|---|---|
| rules | 83.7% (36/43) | 60.5% (26/43) |
| classifier | 46.5% (20/43) | 46.5% (20/43) |
| llm | 69.8% (30/43) | 93.0% (40/43) |

`rules` scores these documents as correct only because its fallback for any generic receipt its keywords don't match is `other`, not because it recognises them; the ranking on the real slice therefore depends on where this one boundary is drawn. Fixing the wording in `categories.py` and re-running `llm` (a new prompt fingerprint, so a fresh run) would be a legitimate next step, but it is tuning on these results and was deliberately not done here.

## Limitations

- **Mostly synthetic eval set.** 90 of 134 documents are synthetic, generated and labeled in the same session as the categorizers being measured; all-documents numbers are likely optimistic. That is why real-only accuracy is the primary metric.
- **The real slice covers only 7 of the 14 categories** (43 scored documents; 18 are `other`, 18 `travel_meals`). Real-only numbers say nothing about the other 7 categories, and a single document moves real-only accuracy by 2.3 points.
- **Label anchoring.** The project owner reviewed silver labels with the label visible and agreed with all 134 (100%). That is not independent confirmation; a blind relabel of a sample is the proper check and has not been done.
- **`rules` keywords were written in the same session as the eval set**, so `rules` may be partly tuned to the documents it is scored on.
- **`team_events` has 6 examples, not 8** (two deliberately ambiguous synthetic documents were relabeled `travel_meals` on review).
- **One category per document.** A hotel folio with a minibar line is filed entirely under `accommodation`.
- **Unequal inputs.** `rules` and `classifier` read a 2,000-character OCR markdown excerpt; `llm` reads only the Stage 1 extracted fields (including `additional_fields`). Any `llm`-vs-`rules` gap mixes model and input effects.
- **Single run, no variance estimate.** `llm` was run once at temperature 0; gpt-oss is not perfectly deterministic even then (noted in Stage 1's reliability numbers), and no repeat runs were done.
- **Same-author prompt.** The category definitions, tie-break rules, few-shot examples, rules keywords and eval labels were all written by the same author in the same project; the prompt was not tuned on eval results, but it was written knowing the shape of the eval.
- **Synthetic inflation is visible in the `llm` numbers.** It scored 100.0% on the 90 synthetic documents but 69.8% on the 43 real ones -- a gap of 30 points. The synthetic receipts are clean template renders whose vendor names, line items and additional fields make their category easy to read, so the all-documents figure mostly measures how easy they are.
- **10 of `llm`'s 13 real-document misses are a single label boundary** (gold `other` vs predicted `office_supplies_equipment`), caused by the wording of `office_supplies_equipment`'s definition in `categories.py`, not by random error. The ranking on the primary metric is fragile to how that boundary is drawn -- see the what-if above.
- **`rules`' real-only score is propped up by its `other` fallback.** 18 of the 43 real documents are `other`, and `rules` predicts `other` for any generic receipt its keywords don't match (recall 100%, precision 72%), so part of that score is the default rather than recognition.
- **Cost and quota.** One `llm` pass over the 133 scored documents used 210,986 tokens (the previous key's daily limit on this model was 200,000; about 1.5-1.9k tokens per document, mostly the fixed few-shot prompt) and averaged 3.7 s per document on the shared tier, so an `llm` default would make the pipeline depend on Groq quota and latency for every upload.

