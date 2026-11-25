# Data contract v1

Structured JSON/CSV/Markdown files use UTF-8 and decimal GB/s. Raw stdout/stderr are retained byte-for-byte and hashed. This is the shared executable contract. Unknown fields stay null; unavailable measurements never become zero. Record evidence paths relative to their owning run/observation directory. Do not store credentials.

## Topology (`topology.json`)

```json
{"schema_version":"1.0","host":"example-host","collected_at":"ISO8601 UTC","cpu":{"vendor":"GenuineIntel","family":6,"model":173,"architecture":"x86_64","model_name":"...","sockets":2},"numa_nodes":[{"id":0,"cpus":[0],"socket":0,"memory_total_kib":0}],"devices":{"0000:03:00.0":{"bdf":"0000:03:00.0","parent_bdf":"0000:02:00.0","root_bus":"0000:00","numa_node":0,"cpu_socket":0,"kind":"npu","role":"endpoint","name":"Ascend","class_code":"0x120000","vendor_id":"0x19e5","device_id":"0xd806","dsn":null,"link":{"current_speed_gt_s":32,"current_width":16,"max_speed_gt_s":32,"max_width":16},"evidence_refs":[]}},"roots":[{"root_bus":"0000:00","cpu_socket":0,"numa_node":0}],"npu_devices":[{"logical_id":0,"physical_id":0,"bdf":"0000:03:00.0","mapping_verified":true,"mapping_source":"npu-smi"}],"io_mapping":[{"bdf":"0000:03:00.0","socket":0,"unit_kind":"IIO","unit_id":4,"source":"registers","confidence":"observed"}],"commands":[],"warnings":[]}
```

`kind` = npu/nic/nvme/bridge/host/other. `role` = endpoint/root_port/switch_upstream/switch_downstream/bridge/host/other. Parent is the actual immediate PCI ancestor, never guessed from BDF arithmetic. Root-bus and root-port are distinct. DSNs do not merge graph nodes. Include every PCI function, not only NPU ancestors. Multiple logical NPU mappings may require an explicit mapping file if discovery cannot verify them.

`commands` is a list `{id,argv,returncode,stdout_path,stderr_path,started_at,duration_s,sha256,status}`; `sha256` contains separate `stdout`/`stderr` digests. Append command results to `commands.jsonl`. Unknown returncode is null; command status retains unavailable/permission/timeout failures. `evidence_refs` refer to these IDs or source files. CPU socket null is permitted; one socket may have multiple NUMA nodes. NIC names/speeds and NVMe models may be extra fields.

Collector library API: `collect(out: Path, *, sysfs_root: Path = Path('/sys'), host: str|None = None, device_map: list|None = None) -> dict`. Writes raw evidence and topology.json. `discover_inventory(text: str, mapping_text: str='') -> list`. `link_ceiling_gbs(speed_gt_s,width) -> float|None` returns encoded one-direction capacity (Gen1–5 only).

PMU API: `discover(topology: dict, sysfs_root=Path('/sys')) -> dict` returns supported/probe/event sets/units/model restrictions; `start(out:Path, topology:dict, label:str) -> handle` starts optional host-side observation; `stop(handle) -> dict` only stops its own process; `summarize(path:Path) -> dict` preserves units, scaling, running time and timing uncertainty. Unsupported does not imply zero. No unconditional raw event codes on unknown CPU models.

## Case

```json
{"id":"single-0-pinned-H2D","cards":[0],"mode":"solo","purpose":"baseline","memory":"pinned","directions":{"0":"H2D"},"backend":"torch","mib":256,"iters":32,"warmup":3,"repeats":5,"cpu_node":null,"memory_node":null,"event_timing":false}
```

Directions H2D/D2H/HD; HD uses distinct H2D/D2H host buffers and distinct H2D/D2H device buffers, with H+D bytes counted once each. Cards use runtime logical IDs with a verified BDF mapping from supported discovery or an explicit device map. A solo case contains exactly one card; concurrent can contain any verified subset. All selected cards meet a repeated barrier. Allocation, first touch, warmup and verification are outside timed loops. Same repeat sample bytes = MiB*2^20*iters*(2 if HD else 1). Pageability is verified, not just requested. Optional `start_at_epoch_s` and controller-owned `control` metadata support fleet admission/release. No cross-host aggregation from unsynchronized monotonic clocks.

Copy protocol is explicit, not inferred from `streams=2`:

| Memory / direction | `protocol.name` | Host concurrency and API |
| --- | --- | --- |
| Pinned, H2D/D2H/HD | `device_async_streams` | One host submission thread; `non_blocking=true`. Torch uses `copy_`; ACL uses `aclrtMemcpyAsync`, attached to the directional stream. HD has two directional streams and independent buffers. |
| Pageable, H2D/D2H | `host_sync` | One host thread; `non_blocking=false`. Torch uses blocking `copy_`; ACL uses `aclrtMemcpy`. |
| Pageable, HD | `host_concurrent_sync` | Two initialized host threads, released through their own barrier, execute blocking H2D and D2H loops concurrently and join before returning; `non_blocking=false`. |

Both implementations create two stream objects for HD. Torch copies execute under the corresponding current stream; ACL synchronous pageable memcpy does not attach to those streams (`device_stream_attachment=false`). Therefore two stream objects alone do not prove asynchronous DMA or hardware overlap. `hardware_overlap_verified` remains null. Torch pageable HD records optional Events inside each directional host thread; ACL pageable Event timing is unsupported and is never substituted with invented device timings.

Benchmark API: `run_case(case:dict, topology:dict, output:Path) -> dict` creates a new JSONL; `validate_result(path:Path, expected_case:dict|None=None) -> dict` independently rederives timings, verified card/repeat coverage and the summary. Parent imports no NPU framework before process spawning. Unsupported backend/copy mode must be explicit, never substituted silently.

Current hardware JSONL records:

- `meta`: schema_version, `measurement_kind="hardware"`, case, host, started_at, `clock_anchor={epoch_s,monotonic_s}`, device_map, topology hash, visibility environment and timing protocol.
- Optional `worker_identity`: rank/BDF, PID/PGID, `/proc` start ticks, PID namespace and `token_sha256` (never the run token). Native guards accept only same-namespace identities captured through their own worker channel; container PIDs are never guessed as host PIDs. An independent host `/proc` observer may additionally cache actual host PID/start/PGID identities confirmed by the unique task token; this can explain a container worker’s stale host PID without guessing container IDs. Unknown vanished PIDs remain unconfirmed.
- `worker_meta`: rank, bdf, physical_id, direction, pinned, affinity, binding, versions, streams, **protocol**, **buffer_placement**, **device_buffers**. The last two dictionaries have keys `H2D`/`D2H` for the active directions; each device entry has positive integer `address` and matching `nbytes`. Host entries additionally include pageability verification and NUMA evidence. HD requires distinct addresses in both dictionaries. Missing/aliased device buffers fail current result validation.
- `sample`: rank, bdf, direction, repeat, bytes, start_s, end_s, wall_s, GBs, enqueue/sync wall and CPU times, optional h2d_event_ms/d2h_event_ms, and release_epoch_s. For synchronous pageable copies, `enqueue_wall_s` includes transfer completion and the two HD host threads joining; it is not a pure asynchronous submission overhead. `process_cpu_s` sums CPU time across worker threads and can exceed wall time.
- `verified`: rank, bdf, verified=true. `group_trial`: repeat, start_s, end_s, bytes, total_GBs, start_skew_ms.
- Final `summary`: case_id, verified=true, cards, card_GBs (dict keyed string rank), total_GBs, group_trial_GBs; optional derived `timing_profile[str(rank)]` with median wall/enqueue/final_sync/process_cpu seconds and H/D Event milliseconds. Event spans include waits.

Host pageability and NUMA placement are separate claims. `buffer_placement` retains `address,nbytes,pinned,verification,page_query,numa_maps,requested_node,binding_verified`. With no requested memory binding, binding_verified is null. With a requested binding, it must have successful exact-page evidence, correct page coverage and no page errors; driver EFAULT or only VMA/affinity evidence cannot satisfy it.

Controller statuses include complete/unsupported/failed/busy/interrupted, and only complete+validated cases feed causal conclusions.

## Historical replay

`replay` currently accepts the explicit `ascend-dma-legacy-redacted-v0` importer format. `examples/pcie-gen5-pairs.json` contains redacted **real historical measurements**, not synthetic rates and not a fresh run on the current hardware. Original numerical observations and legacy JSONL fields are retained. The legacy validator independently reconciles card/repeat coverage, content-verification records, bytes/timings and historical per-card/group summaries.

Normalized replay summaries/results/receipts carry `measurement_kind="historical_replay"`; topology provenance sets `replay_is_new_measurement=false`. Legacy raw records can use `run_meta`, older field names and different worker metadata; do not fabricate new `device_buffers` or relabel them as current-schema hardware records. The public redacted input withholds original verbatim system stdout and private execution receipts, and the report must retain this evidence boundary. Reproduction of a replay is another offline import, never an automatic execution of historical DMA.

## Analysis and report

Analysis API (root owner): `analyze(topology:dict, results:list, *, evidence:list|None=None) -> dict`; `plan_followups(topology:dict, results:list, budget:int=12) -> list[case]`. Results list items `{case,status,summary,raw_path,receipt_path,software_context}`. `software_context` fingerprints only recorded version facts; known same-backend version conflicts cannot become matched controls, and missing versions remain unknown. Returns `schema_version,host,findings,hypotheses,proposals,comparisons,limitations`. Each hypothesis includes id,suspicion,prediction_if_true,prediction_if_false,experiment_ids,observed,result,next_step,evidence_refs. Findings include id,title,evidence_type (measured/derived/inferred/unknown),status (confirmed/supported/refuted/inconclusive),claim,evidence_refs,scope and limitations. A visible shared path is observed topology; its causal role requires matched experiment contrasts.

Report API: `render(out:Path, topology:dict, results:list, analysis:dict, *, fleet:dict|None=None) -> dict[path]`. Output report.html (self-contained offline), topology.svg + optional detail SVGs, bandwidth.csv/md, topology-evidence.md, commands.md and reproduction metadata. Every PCI function must appear in the full graph, and the overview must disclose grouping with links to exact membership. Preserve measured/derived/inferred/unknown distinctions, edge provenance, all nodes and actual parent-child ancestry. Show NIC/NVMe too. No fake physical ASIC merges from DSN. Report hypothesis→experiment→result→decision→next step and missing coverage. Escape all host/command/source strings. No CDN dependence.
