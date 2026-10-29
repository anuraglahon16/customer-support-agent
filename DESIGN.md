# DESIGN

> Completed retrospectively from the implementation and saved evaluation artifacts on 2026-10-29. These are the design choices reflected in code; they should be revised if a local run or trace demonstrates something different.

## Components

- **PostgreSQL** stores seeded users, customer orders, and the append-only action log. It runs via Docker Compose. The app does not issue SQL directly from Python; database operations are exposed through MCP Toolbox.
- **MCP Toolbox** serves the Postgres tools on port 5001 by default (configurable with `TOOLBOX_PORT`). It exposes order-status lookup, customer-order lookup, action logging, and login verification. The support agent gets only the support-agent toolset.
- **Support application / pipeline** runs in the Python process. `support/pipeline.py` owns the event sequence and run logs; `support/cli.py` renders it in a terminal; `support/web.py` exposes FastAPI endpoints on port 8000 and streams the same events as NDJSON.
- **Security Judge** is a separate A2A service on port 10002. It checks known attack patterns and then obtains a structured model verdict when needed. Isolation gives it a clear protocol boundary and lets the pipeline report service errors explicitly.
- **Data Masker** is a separate A2A service on port 10003. It masks personal data in the response after the support agent has finished, and reports which categories changed.
- **Guardrail** runs in-process, using a fresh ADK session per check. It decides whether a request belongs to the online shop support domain; it is separate logically, but does not need another network service.
- **Mem0** stores customer-scoped memories in its service. Recall runs before the agent and save runs after masking, only on unblocked turns.
- **Phoenix** runs as a separate persistent local process on port 6006. The application exports OpenTelemetry spans to it so traces survive restarts of the CLI or web process.

The Judge and Masker are separate A2A services because the assignment requires independent guard services and explicit protocol/error boundaries. The Guardrail is an in-process step because it does not need a separate network boundary. A separate service can still fail or time out, so the caller must fail loudly rather than treating a missing verdict as permission.

## Responsibilities

| Rule / decision | Enforcement | Why |
|---|---|---|
| Customer owns the order being queried | **Code / SQL** | `get-order-status` filters on both order ID and the session-bound customer email; `find-customer-orders` filters by the bound email. Prompt instructions alone are not authorization. |
| Customer identity passed to tools | **Code** | The application binds the email from the logged-in session; the model does not choose the identity used by the query. |
| Agent cannot change orders | **Tool configuration / database** | The support toolset has no order-update or delete tool. Requests are recorded in `actions_log` instead. |
| Action types and ownership | **Database / SQL** | A CHECK constraint limits action types, and the action-log query checks ownership for order-specific requests. |
| Judge verdict | **Code and model** | Pattern matching blocks known attack forms; the model returns an explicit `allow` or `block` plus a reason for other messages. Parsing and transport errors are surfaced. |
| Guardrail domain | **Prompt plus code** | The prompt describes the shop's support scope; a fresh session is used for each check and unparseable output is an error. |
| PII masking | **Code and model/service boundary** | The Masker service applies email, phone, and payment-card rules and reports what it changed. The pipeline validates the returned shape. |
| Memory policy | **Code** | Recall happens before the agent; score and length cutoffs are applied; save stores the user's message only and does not run after a block. |
| Turn budgets | **Code** | Tool-call, token, and wall-clock caps are pipeline decisions, not prompt requests. |
| Trace shape and event order | **Code** | The shared pipeline owns spans and emitted events; CLI and web render the same event stream. |
| Evaluation values | **Code / runner** | `eval/run.py` computes metrics from run logs and traces and writes `reports/eval.json`; numbers must not be manually fabricated. |

The saved evaluation reports no secret in the repository, no mutating tool loaded, no cross-user leak, and no silent fail-open. Those are the four recorded red-line checks; they do not replace checking local environment files or reviewing the actual traces.

## Communication

1. The CLI or browser sends a customer message to the shared pipeline. The browser uses HTTP endpoints on the FastAPI server; `POST /api/chat` returns a stream of newline-delimited JSON events with media type `application/x-ndjson`.
2. The pipeline sanitizes the message and calls the Security Judge over HTTP using A2A JSON-RPC `message/send`. The Judge replies with a data part containing an explicit `verdict` and `reason`.
3. The in-process Guardrail evaluates shop-domain scope. If allowed, the pipeline recalls customer-scoped memories from Mem0 and invokes the ADK support agent.
4. The agent calls MCP Toolbox tools over the Toolbox protocol. Toolbox executes parameterized Postgres queries; customer identity is bound by the application session, not selected by the model.
5. After the agent response, the pipeline calls the Data Masker over A2A JSON-RPC `message/send`. The Masker returns `masked_text`, a `changes` object, and `detail`.
6. For a non-blocked turn, the pipeline saves the user's message to Mem0, writes a per-turn JSON run record, and emits the terminal event. Blocked turns end immediately and do not save memory. A guard transport, timeout, or parsing error produces an explicit error event rather than an allow.
7. The pipeline exports a root `agent.turn` trace and child spans to Phoenix over OTLP HTTP. The emitted trace event includes a trace ID and a URL to inspect it.

## State

| State | Location / lifetime | Data and access considerations |
|---|---|---|
| Login/session | Application / ADK in-memory session | Contains the customer identity and conversation context. The demo login is not production authentication. |
| Orders and users | PostgreSQL | Customer profiles and order data; ownership is enforced in SQL. Seed passwords are for the assignment only and must not be reused as a real authentication design. |
| Requested actions | PostgreSQL `actions_log` | Records intent for downstream staff/system; the agent cannot mutate orders. |
| Memories | Mem0 cloud service, customer-scoped | Stores user-provided preferences. Recall is capped to top 5, minimum score 0.25 and maximum length 500 characters. Save only the user's message after a non-blocked turn. A stale memory can conflict with a later order update, so order facts must still come from the database rather than memory. |
| Run logs | `runs/<turn_id>.json`; deliberately failing examples under `runs/failing/` | Record termination, steps, tool calls, token counts, latency and trace ID. Normal trajectory evaluation reads `runs/*.json`; preserved failing examples live in the subfolder. |
| Evaluation report | `reports/eval.json` plus captured terminal output | Derived from the evaluation runner; do not hand-edit metrics. The current saved report is dated 2026-10-29 and has a trajectory-gate failure on M01 despite passing numeric metric rows. |
| Traces | Local persistent Phoenix storage under `.phoenix/` | Spans can contain full messages, prompts, tool results and customer information. Phoenix binds to localhost; do not expose it publicly and treat its data as sensitive. |
| Secrets | Local `.env` | API keys must never be committed. `.env` is intended to be ignored by Git. |

If a customer's order changes after a preference has been saved, the order database remains authoritative for status, price, address and other order facts. Memory is for standing preferences such as delivery instructions, not a replacement for a live order lookup. A production design would need an explicit policy to correct or expire preferences that become obsolete.

## Trade-offs

- **Latency:** The saved evaluation measured order-turn p50 at 5,839 ms and p95 at 6,569.25 ms (12 order cases); both met the 8,000 ms and 15,000 ms targets. Blocked attack p95 was 5 ms across 30 attack cases, under the 5,000 ms target. The full report lists 76 completed turns with p50 5,932 ms and p95 8,287.25 ms, 51 blocked turns, and one error at about 15 seconds.
- **Judge:** Deterministic patterns cheaply reject known attack strings before model inference. The model adds coverage for less obvious attempts but costs latency and can time out. A Judge timeout must stop the turn with an error; converting it to allow would be a security defect.
- **Guardrail:** A domain-specific prompt with a fresh session improves separation between shop support and unrelated requests, but model checks add latency. The recorded evaluation blocked 15/15 off-topic cases and falsely blocked 0/30 legitimate cases.
- **Masker:** A separate post-agent step catches PII that may appear in generated output, but adds a network hop. It should preserve case and whitespace and must report its changes so the UI can explain the result.
- **Memory cutoff:** The implementation keeps the declared minimum relevance score of 0.25 and 500-character maximum. The saved memory set recalled 9/10 planted keywords (90%), but M01's “back door” memory did not appear in recall and its plant turn had an error in the run output. That failure should be investigated, not fixed by lowering the threshold without evidence.
- **Failure handling:** The pipeline fails closed on guard failures and records errors. The saved run has one M01 error (0.78% of 128 turns), within the numeric 2% error target, but the separate trajectory gate still fails because the required memory plant ended in error. Numeric averages do not override a failed gate.
- **Current completion status:** The saved report passes all 15 numeric metric rows and all four red-line checks, but gate 3 (trajectory) failed on M01 and gate 5 requires a human to open and inspect the successful and failing traces end-to-end. The saved run exited with status 2. The assignment should not be described as fully passing until M01 is fixed and rerun and the manual trace review is completed.
