# Bounded Tool Recovery

Research code for **Evidence Computation and Completion Confirmation in Bounded Tool Recovery** (TMLR working manuscript).

The project studies recovery after ambiguous tool outcomes under a truthful settlement and read-lag bound. It separates final state, mutation history, and the controller's completion declaration. The experiments compare four evidence presentations for one API model with deterministic recovery rules and a later budget and representation diagnostic. The results are limited to known synthetic SQL task families and one model; they do not establish a general exactly-once mechanism or model superiority over strong rules.

## Start without an API key

Python 3.11 or newer is required. From the repository root:

```bash
python3.11 -m pip install -e .
python3.11 -m unittest discover -s tests -p 'test_*.py'
python3.11 scripts/run_offline.py --scope smoke --output results/smoke.jsonl
```

`--scope smoke` runs two deterministic instances across seven fault schedules, four rules, and three budgets (168 episodes). `--scope full` runs all 24 instances (2,016 episodes). Both modes write records and a summary to the requested output path. Neither contacts a model provider.

## Repository map

| Location | Purpose |
| --- | --- |
| `src/drc/sqlite_contracts.py` | Disk-backed tool environment and fault schedules |
| `src/drc/flash_recovery.py` | Task generator, public evidence representations, original rules, and scoring |
| `src/drc/flash_revision.py` | Per-item rule, budget interventions, declaration-time audit, and panel construction |
| `src/drc/flash_api.py`, `api_budget.py` | DeepSeek client and durable spending guard for reproducing model calls |
| `tests/` | Contract, boundary, budget, and revision checks |
| `reports/` | Aggregate results from the completed study |
| `docs/` | Formal model, frozen diagnostic protocol, and contribution boundary |

The aggregate reports are included to make the published counts easy to inspect. They are not the full episode journals. The separate research artifact retains historical raw episodes, API request journals, hashes, and offline replay tools; contact the maintainers for that archive until it is published separately. Regenerating model responses requires a DeepSeek account, a locally supplied key, and a new expenditure budget. Exact responses are not guaranteed by an API model alias.

## Scientific scope

The original study recorded 2,856 formal episodes. The post-review diagnostic replayed those episodes and added 2,016 deterministic and 560 model episodes. In the dependency extension, the wait and verification rules rose from 32/56 to 56/56 SCTS with one additional declaration opportunity. In the equal-byte evidence panel comparison, computed evidence scored 112/112 and restated evidence 110/112; the conditional difference interval includes zero. All started failures were retained. The new diagnostics were specified after inspecting the original outcomes and are not an independent preregistered replication.

See [`docs/FLASH_REVIEW_REVISION_PROTOCOL.md`](docs/FLASH_REVIEW_REVISION_PROTOCOL.md) for the intervention definitions and [`docs/FORMAL_MODEL_V2.md`](docs/FORMAL_MODEL_V2.md) for assumptions. This repository is released under the MIT License. The manuscript and third-party materials have separate rights; they are not covered by this code license.
