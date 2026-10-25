# Oyster Pocket session report

> **SIMULATED DATA: some or all evidence in this report comes from the software simulator. It validates orchestration, policy evaluation and reporting only; it does not validate radio drivers, pairing interoperability or actual BLE permissions.**

| Field | Value |
| --- | --- |
| Session | `sess-39bd564a-001` |
| Source modes | simulated |
| Linked baseline | none |
| Generated (UTC) | 2026-10-24T20:38:56Z |
| Report version | 1.0 |

## Findings

No findings recorded.

## AI runs

### `run-ad4e46ee0ecb913c` - succeeded

- Provider: fake  (fake provider)
- Question: Inspect my enrolled BLE sensor and read its public value
- Model explanation (not verified; classifications above come from the evaluator, links, images and HTML removed, URLs defanged):

~~~text
Simulated assistant (FakeProvider, no language model). 2 check(s) ran in this run. See the cited evidence for details.
~~~

- Cited evidence: `evt-000000`, `evt-000001`, `evt-000002`, `evt-000003`
- Model-stated limitation (not verified): Produced by the FakeProvider (tests/simulation only); no language model was used.
- Model-stated limitation (not verified): Evidence in this run is simulated (simulated device or fixture), not live radio measurements.

## Evidence index

| Event | Session | Operation | Status | Source | Target |
| --- | --- | --- | --- | --- | --- |
| `evt-000000` | `sess-39bd564a-001` | ble_inspect_gatt | success | simulated **[SIMULATED]** | target-lab-01 |
| `evt-000001` | `sess-39bd564a-001` | ble_inspect_gatt | success | simulated **[SIMULATED]** | target-lab-01 |
| `evt-000002` | `sess-39bd564a-001` | ble_inspect_gatt | success | simulated **[SIMULATED]** | target-lab-01 |
| `evt-000003` | `sess-39bd564a-001` | ble_read_profile_value | success | simulated **[SIMULATED]** | target-lab-01 |

## Limitations

- SIMULATED DATA: some or all evidence in this report comes from the software simulator. It validates orchestration, policy evaluation and reporting only; it does not validate radio drivers, pairing interoperability or actual BLE permissions.
- Classifications come from deterministic evaluators against the documented lab profile; AI text explains results but never sets a classification.
- Severity is contextual to the documented profile; a readable attribute alone does not establish impact.
