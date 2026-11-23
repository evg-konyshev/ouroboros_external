## Context

See `proposal.md` (Why) and the two delta specs for required behaviour. Observed state of the
service (`aifa/finengine/src/finengine/`):

- **The turn already carries a set.**
  - The owner check is done for the whole set at the door (`api/main.py` `_docs_of_the_turn`,
    `_chat_turn_docs`).
  - `_chat_turn_docs` appends `active_doc_id` to the candidates (`api/main.py:1286-1287`).
  - `TurnRecord.doc_ids` is frozen in the coordination cache, and `aifa_doc` lists and reads every
    document in it (`assistant/read_tools.py:146-163`). The listing reads every full text.
- **Case and workspace come from `project_id`.**
  - A chat in a folder sends `project_id = folder_id` (`aifa/web/.../use-chat-run.ts:950`), and
    `case_id = project_id or session_id` (`api/main.py:1564`). So a folder's chats share a Case,
    and the request never carries the chat's own id.
  - The frontend always sends all chat `doc_ids` (`use-chat-run.ts:840-842, 947`).
- **The Case is multi-document.**
  - Several documents can be active in one Case (`store/cases.py:369-411`).
  - Evidence ids resolve against the whole Case (`skills/spec/dynamic_run.py:1199-1237`), and so
    does `aifa_state(artifact_ids)`.
  - `_bind_target_to_case` accepts two loops, because each target has its own
    `document_version`.
  - `closed` is persisted (`skills/spec/loop_state.py:59, 78`).
- **The loop is single-document.** `prepare_production_context` classifies one text and freezes
  one passport and registry; `profile_id` → (standard, company_type, language) is in
  `skills/atoms/run_passport.py:36-50`.
- **Loop identity is the run.**
  - Keys: `LoopDesk._open[run_id]`, state `LOOP_SESSION key=run_id`, the claim
    `loop_opening key=run_id`, the job kind `OPEN_KIND`.
  - The "after aifa_open" check in `read_tools.py:83, 141` matches the job kind exactly
    (`jobs.py:53-58`).
  - `OpenLoop.queue` is per loop.
  - `_sweep` deletes only `key=run_id` (`loop_desk.py:129-134`), and the state store has no TTL
    (`store/state.py:76-79`).
  - `CaseRuntime.register_document` takes no `case_lock` (`skills/case_runtime.py:204-217`).
- **Loop-bound tools** (all via `open_loop_of`):
  - `aifa_move`, `aifa_finish`, `aifa_register_atom` (`ExclusiveTool`);
  - `aifa_report` (`atom_tools.py:86`), `aifa_atoms` (`atoms_tool.py:45`, the registry is per
    report);
  - `aifa_state`, `aifa_doc`;
  - `aifa_wait(task_id)` (`goal_tools.py:156-157`, the loop's deadline).

  The subtask skill `aifa-atom-author` calls `aifa_register_atom`/`aifa_report`.
- **Result publication.** `publish_result` emits only `typed_result` and `file` events
  (`skills/production_runtime.py:878-905`), and no `analysis_card` exists on this path.
- **Document metadata.**
  - v2 uploads leave `company_name`/`period`/`doc_type` empty (`use-doc-upload.ts:339-347`). The
    meta card holds `doc_kind`/`issuer`/`period`, and `doc_kind` does not distinguish IFRS from
    RSBU.
  - `total_pages` is 0 for txt/md, and the physical page count after cloud OCR.
  - Every write of `meta` replaces the whole JSON: the ingestion upserts at `api/main.py:580-606`
    run after the text is already published at line 575; `update_document_meta`; and the
    doc-type patch at `api/main.py:1390`.
- **F4.** After `aifa-document-search`, the ouroboros budget is 10,000 and the target is 9,000.
  Ouroboros drops a schema that does not fit, without an error (`ouroboros/agent/task_graph.py:185`).
- **The queue is part of the contract.** CONTRACTS §3.1 lists `aifa_open` as out of queue, and
  ADR-0092 says "очередь `aifa_move` по одному на `LoopSession`" (ADR-0043 p. 4).
- **RFA's universe for `all_workspace` is READY documents only** (RFA §7).
- **Ouroboros side of the turn** (observed; the core is not changed):
  - A subtask inherits its parent's `transport` (`ouroboros/tools/service/orchestration.py:135-167`).
    The plugin takes `run_id` from `transport.source_message_id` (`skills/aifa-engine/plugin.py:308`),
    so every subtask of a turn reaches the same `TurnRecord` and loops. The plugin also sends
    `task_id` and `root_task_id`, so the service can tell a subtask from the root.
  - A parent holds at most 10 outstanding subtasks (`orchestration.py:33`).
  - The plan-author subtask reads `aifa_state` and `aifa_atoms` itself (ADR-0084). Its plan is
    stored by the core tool `task_plan` (ADR-0061), whose steps are free text from the service's
    point of view.
  - The next turn's prompt holds only the chat's message tail (`openspec/specs/dialogue-context-assembly`),
    not the tool results of earlier turns. What the next turn knows about this one is the root's
    reply text and what the service keeps.
  - `OUROBOROS_HARD_TIMEOUT_SEC` (1800 s) bounds each ouroboros task (`supervisor/queue.py:84-108`),
    while a loop's wall limit is `ouroboros_loop_max_wall_sec` = 1200 s from its own open
    (`config.py:79`, `goal_tools.py:157`). A loop opened 700 s into the turn would outlive the root
    task, which is then killed with `hard_timeout` and gives no answer.
- **The Case persists across turns of a chat.**
  - `register_document` keys by `logical_name = document_id` (`skills/production_runtime.py:168`).
  - A document stays active until a new version of the same document replaces it, so the facts of
    earlier turns remain citable.
- **The Case head holds one profile.**
  - `run_passport` and `run_passport_document_version` live in the Case head (`contracts/case.py:105`).
  - A plan built without an explicit passport takes the head's one when the versions match
    (`skills/case_runtime.py:426-431, 461-465, 526-531`).
  - A run that discovers a passport writes it to the head (`case_runtime.py:705-712`).
  - The dynamic run passes its own passport (`skills/spec/dynamic_run.py:1029`). Whether every
    goal channel does the same with two loops is not verified; task 4.4 checks it.
- **Governance.**
  - The ouroboros core is frozen (F1).
  - The tool set, the refusal list and the ouroboros env change only by ADR.
  - F13 counts each step branch from `merge-base dev`, so each branch that touches snapshots or
    governed docs carries its own ADR.

## Goals / Non-Goals

**Goals:**
- Several documents in one turn, with the loop machinery reused per document unchanged.
- The single-document path keeps its calls and loop answers (contract tests C3.x stay green
  without edits).
- Presets follow RFA's policy and cite Case facts.

**Non-Goals** (RFA parts deliberately not ported):
- **No Document state machine, lease or worker pools.** "Ready" means `document_text_exists`.
- **No Workspace table or search projection here.** They come from `aifa-workspaces` and
  `aifa-document-search`; this change only consumes them.
- **No automatic narrowing of the set** (RFA §7 resolver). The planner ranks with `aifa_doc(query)`
  and chooses which loops to open.
- **No service-side context resolver or slot deltas.** Ouroboros holds the dialogue (user
  decision; ADR-0060/0061).
- **No AnalysisRun table, idempotency key or clarification run.** The `run_orchestrator` run and
  the tool calls' `operation_id` already cover this.
- **No llm_balancer, and no currency conversion.**
- **No parsing triggered by `all_workspace`.** Never-parsed documents are listed as not ready
  (user decision). This deliberately departs from RFA's READY-only universe, and ADR A records it:
  the planner sees that a folder document exists but cannot be read, and can tell the user.
- **No cross-loop use of the `set.assemble`/`calc.series` atoms.**
- **No ouroboros core change.** Subtask wiring, `task_plan`, prompt assembly and the hard timeout
  stay as they are. The integration uses what ouroboros already gives: inherited `transport`,
  `task_id`/`root_task_id` in the tool context, the message tail, and free-text plan steps
  (D11–D14).

## Decisions

### D1. One loop per document, not one loop over many documents

A loop is keyed by its document. Everything inside a loop stays as it is: passport, registry, node
keys, gates.

- **Alternative: one loop holding several texts.** The passport and registry are per report, so
  merging them rewrites the engine.
- **Alternative: one active loop, switched by `doc_id`.** It hides which loop is live, and D3
  makes parallel loops cost no more.

### D2. Keys by the resolved document

`aifa_open` resolves the document first (`doc_id` → only document → active → refuse), then:
- the key is `f"{run_id}/{doc_id}"` for every report loop, whichever way it was resolved;
- the market loop keeps `run_id`;
- the opening claim key and the job kind (`aifa_open:<doc_id>`) follow the same rule;
- the "after aifa_open" check in `read_tools.py` (and `aifa_search`) changes from an exact job kind
  to "any job of kind `aifa_open` or `aifa_open:*` exists, or a loop of this turn is open";
- `state.LOOP_INDEX key=run_id → [doc_id, …]` lists the opened report loops in order;
- `_sweep` deletes the index and every `run_id/<doc_id>` loop state with `run_id`'s own key. The
  store has no TTL, so without this the state would pile up.

**Alternative: keep `run_id` for the implicit open.** Then `aifa_open()` followed by
`aifa_open(doc_id=A)` opens two loops on A, and A drops out of the index (the review's B4).

### D3. One queue per turn, and report opening runs in it

- `OpenLoop.queue` moves to `LoopDesk.queue_of(run_id)` (one `asyncio.Lock` per run, in-process as
  now).
- Changing calls and report-loop opening take it. Opening keeps its job and `ЖДИ` semantics: the
  job waits for the queue inside its 240 s budget.
- This serialises the `LOOP_INDEX` read-modify-write and the six-loop count. It also keeps
  `register_document` (no `case_lock`) from bumping the Case revision during another loop's move
  (`update_case_head(expected_revision=…)` would raise `CaseConflictError`).
- **This changes the contract and is recorded as such.**
  - CONTRACTS §3.1 lists `aifa_open` as out of queue, and ADR-0092 / ADR-0043 p. 4 say one queue
    per `LoopSession`.
  - ADR A replaces both wordings with "одна очередь на ход: `aifa_move`, `aifa_register_atom`,
    `aifa_finish` и открытие отчётного цикла".
  - The `aifa_open` line of CONTRACTS §3.1 is edited in the same branch.
  - The single-loop behaviour is unchanged, because there one loop is one turn.
- **Consequence: moves on different documents run one after another.**
  - Peer comparison of five banks is at least five sequential moves.
  - Parallelism stays inside a move (ADR-0088/0092), but one move works on one document.
  - The risk to `OUROBOROS_HARD_TIMEOUT_SEC` (1800 s) is in Risks, and the live check measures it.
  - Revisit: parallel loops once Case head writes tolerate concurrent loops (per-loop queues plus
    `case_lock` on `register_document` and `update_case_head`).
- **Alternative: `case_lock` around `register_document` with per-loop queues.** It covers the
  Case race at registration but not the index race, nor concurrent `update_case_head` of two
  moves.

### D4. Six report loops per turn; refusals before a loop is chosen touch no loop

- Six is a constant in `loop_desk.py`. A breach is refused with the new code `лимит_циклов`.
- A refusal for a missing `doc_id` is returned before any loop is selected, so it calls no
  `session.reject`: it spends no move and counts in no streak.
- `aifa_wait(task_id)` takes the turn's deadline (D11).

### D5. Finish runs on the first loop, closes the rest, and announces the documents

- `aifa_finish` (no `doc_id`) runs today's `check`/`finish` on the first loop in `LOOP_INDEX`.
  Evidence resolves Case-wide.
- It then marks every other loop closed (persisted `closed`) without publishing.
- `publish_result` gets `document_ids` and emits one new additive event
  `{"type": "turn_documents", "document_ids": [...]}` before the `typed_result` events. The
  shared SSE fixtures (C1.2, ADR-0045) and `CONTRACTS.md` §1 are updated.
- `last_word` uses the first loop's limits.

**Alternative: a field on `typed_result`.** There are N of those per answer and none per turn.

### D6. Listing from the registry, with stored page counts and ranked order

- The listing reads `get_document` metadata (the meta card before registry fields for company,
  type and period) and `document_text_exists`. It does not load texts.
- Page count: a new column `document.text_pages`.
  - Right after `save_document_text` in `_parse_doc_in_background`,
    `set_text_pages(doc_id, len(pages_of(text)))` writes it with a single-column UPDATE.
  - The column is added by the start-up DDL from `aifa-workspaces` W3. It is not a `meta` key,
    because the upserts at lines 580-606 that follow would overwrite it.
  - For documents published earlier, the listing computes it once from the text and stores it,
    so the single-document answer keeps today's number.
- Paging: `offset` counts entries (unlike `registry_offset`, which counts characters).
- `aifa_doc(query=…)` without `doc_id` calls
  `search.select_documents(record.doc_ids, query, workspace_id=…)`, passing the workspace only
  for `all_workspace`. It renders ranked documents first, then the rest in set order. On a search
  failure it renders the plain listing with a note.

### D7. Scope in the request; workspace derived from `project_id`; frozen in `TurnRecord`

- `QueryRequest.scope: Literal["conversation", "all_workspace"] = "conversation"`. Pydantic gives
  the 422.
- Workspace: the id is `project_id` only if both hold, else `owner:<id>`:
  - `get_workspace(project_id)` is owned by the caller;
  - the caller's stored dialogs list a folder with that id.

  The second check keeps a stray workspace created by an upload with a chat's id from changing
  that chat's scope. The request has no field that picks the workspace.
- `_chat_turn_docs` for `all_workspace`: `list_documents(workspace_id=…)` (every document, ready
  or not), narrowed by a non-empty `doc_ids`, then `_docs_of_the_turn`. `active_doc_id` is kept
  only if it is already in that set.
- `TurnRecord` gains `scope` and `workspace_id`, and `recall` tolerates records without them.
- The `pinned_tool` path ignores `scope`.
- Frontend: with the toggle on, the body carries `scope: "all_workspace"` and `doc_ids: []`.

### D8. Deleted documents and restore

- Each loop-bound call checks `get_document(doc_id)` before running. If the document is gone, it
  answers `нет_документа`.
- `LoopDesk._restore` refuses to restore a loop whose document text is missing, instead of
  opening it with empty text and registering a new version.

### D9. `aifa_compare`: a read-only tool, with RFA's policy ported

- `assistant/compare_tool.py` handles arguments, Case rows and fact → document lookup by
  `document_version` against `active_documents`.
- `skills/cross_document.py` holds pure functions: a port of RFA `analysis/policy.py`
  (`comparison_kind`, `spread`, `material`, thresholds 5% / 1% / 1 p.p.) and of the preset rules
  in `analysis/presets.py`:
  - retrospective comparisons: one series per company and metric, with the period shape
    following the latest point, the previous/year-ago windows 45–120 / 320–410 days, and CAGR
    where allowed;
  - peer comparisons: rows per period, gaps grouped by company (missing ≠ zero; no refusal for
    different periods), and the standard, currency, scale and basis check;
  - `agreements` (an identical value and basis in at least two documents) and `disagreements`
    (one `spread` between the smallest and largest value per company, metric, period and basis,
    with the benchmark for the largest row). Verification statuses follow; there is no winner and
    no pairwise matrix;
  - the materiality benchmark from `TOTAL_ASSETS`/`SALES_REVENUE` of the same company, basis and
    period. The concept ids exist in our registry.
- Attributes:
  - company and source type come from the meta card, then registry fields;
  - `company_key` comes from `search.card.company_key`, so «ПАО Сбербанк» = «Сбербанк»;
  - standard and company type come from a new column `document.profile_id`, which
    `open_agent_loop` writes with a single-column UPDATE when a loop opens. Every fact comes from
    a loop, so every fact's document has it. It is not a `meta` key: a loop can open between the
    text publish (line 575) and the ingestion upserts (580-606), which would erase it.
- Order independence: groups are keyed by (unit, scale, basis), the largest group wins, and ties
  are broken by unit name.
- It runs out of queue, like `aifa_state`.
- **Alternative: run presets as atoms inside a loop.** Their inputs would have to cross loops.
- **Alternative: port `presets.py` whole.** It is bound to RFA's `facts` rows. Only its rules are
  ported, with RFA's tests where they apply.

### D11. One deadline per turn, set by the first loop

- The first report loop's `opened_at + max_wall_seconds` is written into `LOOP_INDEX` as the turn's
  deadline.
- Every later loop takes it as its own end: its `limits.max_wall_seconds` is shortened to
  `deadline − opened_at`. `ЛИМИТ` then comes from the existing limit check, with no new code path.
- The service never sees `OUROBOROS_HARD_TIMEOUT_SEC`. With one deadline, the turn ends with
  `ЛИМИТ` and an answer while the root task is alive: 1200 s from the first open is less than
  1800 s for the root.
- The restore keeps the shortened limit, because it is part of the saved loop state.
- **Alternative: the latest deadline over the loops** (the first draft of D4). A loop opened at
  700 s ends at 1900 s, after ouroboros has killed the root.
- **Alternative: a service setting mirroring the ouroboros timeout.** It duplicates an ouroboros
  setting across the boundary (F9) and drifts.

### D12. `aifa_state()` among several loops is a turn overview

- The plan-author subtask reads state itself (ADR-0084). With several loops, a refusal would make
  it guess documents, so the no-argument call answers with an overview (`ouro_view.turn_overview`):
  - per loop: `doc_id`, file name, company, period, `profile_id`, moves used and left;
  - documents of the set without a loop;
  - the deadline.
- Then `aifa_state(doc_id=…)` gives one loop's registry and artifacts, as today.
- It reads `LOOP_INDEX`, loop states and registry metadata only, so it stays out of queue like
  `aifa_state`.
- It is not a new tool, so the F4 schema budget does not change.
- The plan-author's plan names a `doc_id` for each move step in its text. `task_plan` is not
  changed: its steps are free text, and a field there would be an ouroboros core change (F1).

### D13. Subtasks address loops by `doc_id`, like the root

- A subtask reaches the turn through the inherited `transport` (Context), with no new wiring.
- The service treats it as today: `aifa_open`/`aifa_move`/`aifa_finish` are root-only
  (`не_та_роль`, ADR-0060 p. 5). With several loops, `aifa_register_atom`/`aifa_report`/
  `aifa_atoms` need `doc_id`, with the same refusal listing documents.
- The root puts the `doc_id` into the subtask's task text (skill text 5.1). `aifa-atom-author` and
  `aifa-plan-author` copy it into their calls.
- **Alternative: pin a subtask to the first document it names** (`task_id → doc_id` in
  `LOOP_INDEX`). It saves one argument, but adds state written out of queue by `aifa_report`, and is
  lost on restore. Rejected; revisit if live traces show subtasks dropping `doc_id`.

### D14. Follow-up turns reuse the Case, not the dialogue

- The next turn's prompt does not hold this turn's tool results (Context). What carries over:
  - the Case: facts of earlier turns stay active and citable (Context);
  - the root's reply: `ПРИНЯТО` names the files used (spec), and the skill text says to name them
    in the reply, so the message tail holds them;
  - the listing: each entry states how many metric facts of the document's active version the Case
    holds. It is counted from the Case artifact index by `document_version`, in one query per
    listing page, without texts.
- The skill text says: on a follow-up, read the listing first. Cite facts already in the Case
  through `aifa_compare` and `aifa_finish`. Open a loop only for a document that is missing facts.
  This also cuts the sequential-move time (D3 risk).
- `aifa_compare` needs no open loop. `aifa_finish` still needs one, so a pure follow-up comparison
  opens one loop: the cheapest is the document the question names.
- **Alternative: inject the previous turn's tool results into the prompt.** It is an ouroboros core
  change (prompt assembly, F1) and duplicates the Case.

### D15. A loop plans with its own profile

- Two loops in one Case share one head `run_passport` (Context). A path that plans without an
  explicit passport takes the head's one only when the document versions match, so another loop's
  profile cannot be picked up. The head write at `case_runtime.py:705-712` still flips between
  documents.
- Rule: every call from a loop passes the loop's frozen passport explicitly. The head passport is
  only a fallback for the single-loop path.
- Task 4.4 proves it with alternating moves on an IFRS bank and an RSBU company. If some path
  passes `None`, the fix is to pass the loop's passport at that call site, not to make the head
  per-document.

### D16. Startup side-effects touched (rule R2.1)

D11–D15 add no start-up side-effect: the overview is a view of existing state, and the deadline
lives in `LOOP_INDEX`.

| Side-effect at start | Change | Preserved by |
|---|---|---|
| `composition.tool_api()` builds `ToolApi` once (`@cache`) | adds `CompareTool` | `tools.snapshot.json` test |
| Plugin `register(api)` | `doc_id`/`offset` arguments; `aifa_compare` | `test_schema_budget.py` (target 9,000 < limit 10,000 from the snapshot, exact set) |
| `SqlDocumentStore.__init__` start-up DDL | `ADD COLUMN IF NOT EXISTS text_pages`, `profile_id` | test: existing table without the columns → the store starts and both exist |
| Ouroboros reads `OUROBOROS_ENABLED_TOOLS` | `+ aifa-engine.aifa_compare` | F9 snapshot + `test_dev_config.py` |
| `tool_names.py` map | `aifa_compare → read_source` | `tool_names.json` test |
| `LoopDesk()` created once; lazy restore | new keys, `LOOP_INDEX`, extended `_sweep` | restore and sweep tests |
| DB schema (tables) | none; state docs via `store/state.py` | — |

## Risks / Trade-offs

- **[Planner picks the wrong documents]** → Ranked selection includes chunk owners. The listing
  shows the file name, and the user can narrow with `doc_ids`.
- **[Six loops cost six budgets]** → All loops share one deadline (D11), so six loops cost one wall
  budget. The skill text says to open only what the question needs.
- **[Sequential moves across documents run out of time]** Five documents need at least five moves
  in a row (D3). Past the shared deadline the turn gets `ЛИМИТ` and answers with what it has, not a
  `hard_timeout` without an answer (D11). → Mitigations:
  - the skill text: one move per document, up to three goals in it (ADR-0089), and presets over
    facts already in the Case instead of re-extracting (D14);
  - the live check measures a five-document turn against the 1200 s deadline;
- **[Subtask drops `doc_id`]** → It gets a refusal that lists the documents and spends nothing
  (D13). Pinning is the recorded revisit.
- **[Case head profile flips between loops]** → Every loop passes its own passport (D15), and the
  alternating-moves test in 4.4 guards it.
  - if it is too slow, the recorded revisit is parallel loops (D3).
- **[Schema budget]** The new arguments and tool must stay under 9,000. → `test_schema_budget.py`
  fails before ouroboros would silently drop a tool.
- **[Opening waits for a running move]** → Opening keeps its `ЖДИ` job semantics, and the planner
  polls with `aifa_wait`, as today.
- **[No size benchmark among the named facts]** → The status falls to `minor_difference` with a
  note (RFA behaviour), and the skill text tells the planner to include
  `TOTAL_ASSETS`/`SALES_REVENUE`.
- **[Meta card empty for some documents]** → Those facts go without a company and are excluded from
  peer and retrospective grouping, with a reason. They are never mixed in.
- **[Frontend change vs ADR-0015]** → The ADR records the toggle and `turn_documents` as additive.
- **[Rollback with new `TurnRecord` fields in the cache]** The old `recall` raises on unknown
  fields. → The rollback step deletes `ouro_turn:*` keys, which ends in-flight turns, as a restart
  does.

## Migration Plan

1. Deploy `engine`, `ouroboros` (plugin schema, enabled tools) and `frontend` on aifa-sym at the
   owner's signal. Report loops in flight lose their loop (new keys); the Case artifacts survive.
2. Rollback: redeploy the previous images, then delete the coordination-cache keys `ouro_turn:*`.
   Leftover `run_id/<doc>` state documents are inert, and the old `_sweep` ignores them. A
   one-line cleanup query is in the ADR.
