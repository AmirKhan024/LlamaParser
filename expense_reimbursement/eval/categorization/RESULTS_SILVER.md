# Categorization eval results (SILVER -- interim, not final)

**These numbers use silver labels (Claude Code's own judgment against categories.py's definitions and tie-break rules -- not the categorizer being evaluated, and not human-reviewed gold labels). Treat as directional only until RESULTS.md exists.**

**llm, hybrid could not be evaluated this run -- see each method's section below for why.**

## rules

- n = 133
- Accuracy: 87.2%
- Macro-F1: 0.894
- Latency p50: 0 ms
- Estimated cost per 1,000 docs: $0.0000

### Per-class precision/recall
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

### Confusion matrix
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

### By source
| source | n | accuracy | macro-F1 |
|---|---|---|---|
| real (sroie+cord+real) | 43 | 83.7% | 0.805 |
| synthetic_image | 90 | 88.9% | 0.856 |

## llm

_Unavailable this run: 132/134 calls failed (see the per-row `error` field in eval/categorization/_predictions_cache/llm.jsonl) -- most likely every usable Groq model's daily quota was exhausted while this eval set was being built. Re-run once quota resets._

## classifier

- n = 133
- Accuracy: 70.7%
- Macro-F1: 0.706
- Latency p50: 16 ms
- Estimated cost per 1,000 docs: $0.0000

### Per-class precision/recall
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

### Confusion matrix
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

### By source
| source | n | accuracy | macro-F1 |
|---|---|---|---|
| real (sroie+cord+real) | 43 | 46.5% | 0.357 |
| synthetic_image | 90 | 82.2% | 0.776 |

## hybrid

_Unavailable this run: 57/134 calls failed (see the per-row `error` field in eval/categorization/_predictions_cache/hybrid.jsonl) -- most likely every usable Groq model's daily quota was exhausted while this eval set was being built. Re-run once quota resets._
