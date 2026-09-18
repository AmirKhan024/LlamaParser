# Categorization eval results (FINAL, gold labels)

## rules

### All documents

- n = 133
- Accuracy: 87.2%
- Macro-F1: 0.894
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

### All documents

- n = 133
- Accuracy: 70.7%
- Macro-F1: 0.706
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

### travel_documents_fees -> software_subscriptions (2)
- `synth-travel_documents_fees-00` (synthetic_image, vendor: VisaAssist Services): expected category: travel_documents_fees
- `synth-travel_documents_fees-03` (synthetic_image, vendor: VisaAssist Services): expected category: travel_documents_fees

### travel_documents_fees -> other (2)
- `synth-travel_documents_fees-05` (synthetic_image, vendor: GlobalTravel Docs): expected category: travel_documents_fees
- `synth-travel_documents_fees-07` (synthetic_image, vendor: GlobalTravel Docs): expected category: travel_documents_fees

### local_transport -> other (1)
- `cat-sroie-011` (sroie, vendor: SECURE PARKING CORPORATION S/B): 

### software_subscriptions -> accommodation (1)
- `synth-software_subscriptions-07` (synthetic_image, vendor: CloudSuite Pro): expected category: software_subscriptions

### office_supplies_equipment -> other (1)
- `synth-office_supplies_equipment-02` (synthetic_image, vendor: OfficeMart): expected category: office_supplies_equipment


_Best method by macro-F1 (all documents) among those run: **rules** (0.894)._

## Not yet run

- **llm**: `python scripts/eval_categorization.py --final --methods llm` -- standalone (doesn't touch rules, classifier, hybrid), uses eval/categorization/_predictions_cache/llm.jsonl for any row already predicted (nothing already done gets re-spent), and appends its section into this file via _final_results_state.json without rerunning anything else.
- **hybrid**: `python scripts/eval_categorization.py --final --methods hybrid` -- standalone (doesn't touch rules, llm, classifier), uses eval/categorization/_predictions_cache/hybrid.jsonl for any row already predicted (nothing already done gets re-spent), and appends its section into this file via _final_results_state.json without rerunning anything else.
