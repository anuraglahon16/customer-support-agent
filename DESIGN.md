# DESIGN

## Components

| Part | Separate process? | Port |
|---|---|---|
| Postgres 16 (users, orders, actions log) | yes, Docker container | 5432 |
| MCP Toolbox (the three SQL tools) | yes | 5001 |
| Security Judge (pattern scan + model, A2A) | yes | 10002 |
| Masker (PII masking, A2A) | yes | 10003 |
| Phoenix (traces, localhost only) | yes | 6006 |
| Web app (FastAPI) | yes | 8000 |
| Pipeline, Sanitizer, Guardrail, support agent | no, inside the app | — |
| CLI | no, runs the same pipeline in-process | — |
| Mem0 (long-term memory) | hosted service, reached over HTTPS | — |

The Judge and Masker enforce rules any product would need: no prompt injection in, no other people's PII out. In a company a security team would own them and share them across products, so they run as their own services behind a fixed A2A contract, and a product change can't quietly weaken them. The Guardrail answers a different question, "is this something our shop helps with?", which the product team owns and changes whenever the product does. So it lives in the app next to the agent, which also saves a network hop.

## Responsibilities

**Ownership** is checked by Postgres, not the model. Each of the three tools in `mcp_toolbox/tools.yaml` puts the customer's email into its SQL:

- `get-order-status`: `WHERE order_id = $1 AND customer_email = $2`
- `find-customer-orders`: `WHERE customer_email = $1`
- `action-log`: an `EXISTS (… AND customer_email = $1)` check before the insert, so an action can be logged only on the customer's own order

Together these are why `G-ACCESS` passes. The email is bound at login and filled in by code. The model never supplies it, so no message can change whose orders are read or written. Someone else's order comes back as zero rows, and the reply is the same as for an order that doesn't exist.

**API keys** live in `.env` only. No prompt, span, run file or tool argument contains them.

**Budgets** are enforced by code in `support/pipeline.py`: at most 6 tool calls, 30k tokens and 30 s per turn. When a cap is hit, the turn stops. The model is never asked to stop itself.

| Rule | What it says | Where it's enforced | Code / prompt / both | Why there |
|---|---|---|---|---|
| R-1 | A customer sees only their own orders | the SQL of all three tools, email bound at login; plus a prompt rule from P08: look the order up before logging an action | both (code is the control) | A model can be talked out of an instruction, not out of a WHERE clause. P08 showed why the prompt layer sits on top: the agent called `action-log` for another customer's order, the SQL refused it, but the attempt alone failed T-LEAK, so the prompt now keeps it from trying. |
| R-2 | Must-happen-every-turn things are pipeline steps, not prompt instructions | recall and save are steps in `support/pipeline.py`; the agent has no memory tool | code | An instruction gets followed on most turns; a pipeline step runs on every turn, and an agent with no memory tool can't skip it or call it at the wrong moment. |
| R-3 | The toolset excludes mutating tools; forbidding a loaded tool in the prompt isn't a control | toolset `support-agent` has 3 tools; the `toolbox` DB role has no `UPDATE`/`DELETE` | code | A loaded tool will eventually get called, however the prompt forbids it; if it isn't loaded and the role can't write, there's nothing to talk the model into. |
| R-4 | A guard that errors fails the turn loudly | `guard_client` raises `GuardError`; the pipeline's `errored()` ends the turn with an `error` event; retries are bounded and reported | code | A guard that fails silently looks exactly like one that passed, and a model told to "stop if the guard fails" never even sees the failure. |
| R-5 | Save only what the user said | `memory.save(session.email, message)` sends the user's message only, never the reply; extraction instructions tell Mem0 what to keep | both (code + an instruction to Mem0) | Code decides what is sent, so the agent's own words never come back later as "facts"; the instruction decides what Mem0 extracts from it, which code can't, as the order questions saved at 0.43 showed. |
| R-6 | The Guardrail prompt uses this product's scope and examples | the Guardrail instruction: shop scope, domain examples, none taken from the gold sets | prompt | Whether a message is on-topic is a judgement about meaning, which only a model can make, and examples kept out of the gold sets keep the eval honest. One piece around it is code: rewording the prompt didn't stop the Guardrail answering messages instead of judging them (eval runs 3–5), and wrapping the input in `<message>` tags did, with 0/30 false blocks three runs in a row. |
| R-7 | Reject over-long memories | `T_MEM_MAXCHARS = 500` in `support/memory.py` | code | A length limit is a number, which code checks exactly and a model only estimates; a long memory is also where injected instructions would ride into future prompts. |
| R-8 | A guard changes only what it exists to change | regex substitution in the Masker; text with nothing to mask comes back byte-for-byte | code | A model asked to mask would also rephrase, trim or "fix" the reply; a regex touches only what matches, and byte-for-byte output is something a test can check. |
| R-9 | A verdict is explicit, never inferred from an echo | the Judge's `Verdict` schema (`allow`/`block` + reason); `guard_client` rejects anything else | code | If "allow" were read off any reply that doesn't say "block", a garbled answer or a model repeating the attack back would count as a pass; a schema turns an unclear answer into an error. |
| R-10 | Traces outlive the process | `phoenix serve` as its own process; storage in `.phoenix/` | code | Traces kept inside the app die with it, exactly when you need them to see why it died; all 8 spans of `63b20305…` survived a full `stop`/`up`. |
| R-11 | Enumerations enforced by the schema | the `CHECK` constraint on `action_type`, plus `allowedValues` in `tools.yaml` and the tool description | both (schema + description) | The database rejects a sixth action type whatever the model sends; the allowed values and description are there so the model gets it right first time instead of hitting the error. |

## Communication

One turn, in order:

1. **Browser → app:** HTTP POST. The reply streams back as NDJSON, one event per line (`trace`, `stage`, `llm`, `tool_call`, `tool_result`, `step`, `final`). The CLI calls the same pipeline in-process and prints the same events.
2. **Pipeline → Sanitizer:** in-process; checks length and characters only.
3. **Pipeline → Judge:** A2A JSON-RPC, `message/send`.
4. **Pipeline → Guardrail:** in-process ADK agent, fresh session per check.
5. **Pipeline → Mem0 (recall):** HTTPS.
6. **Pipeline → support agent:** in-process ADK agent.
7. **Agent → Toolbox:** MCP.
8. **Toolbox → Postgres:** SQL, as the limited `toolbox` role.
9. **Pipeline → Masker:** A2A, on the agent's reply.
10. **Pipeline → Mem0 (save):** HTTPS.

Each guard answers in its own shape:

```json
Judge:     {"verdict":"block","reason":"…","source":"pattern"}
Guardrail: {"decision":"unsafe","reasoning":"…"}
Masker:    {"masked_text":"…","changes":{"email":0,"phone":1,"card":0},"detail":"…"}
```

The Judge's `source` says whether a pattern or the model made the call. The Guardrail's `decision` is `safe` or `unsafe`. The Masker never blocks; it returns the reply with PII replaced and a count of what it masked, per type. A block ends the turn at that hop: a message the Judge blocks never reaches the Guardrail, memory or agent, and is never saved (X01 was neither recalled nor stored).

## State

| What | Where | How long | Customer data? | Who can read it |
|---|---|---|---|---|
| ADK sessions (chat history) | the app's memory | until logout or restart. Guardrail sessions are thrown away after each check | yes, the conversation | the agent, within that session |
| Mem0 memories | hosted Mem0, per customer | until deleted. The eval runner deletes them and waits until they're gone | yes, preferences like "leave it at the back door" | the app, through the key in `.env`, and Mem0 as provider |
| Phoenix traces | `.phoenix/` on this machine | until `.phoenix/` is deleted. Survives `stop`/`up` | yes, every span records arguments and results | anyone on this machine (Phoenix listens on localhost only) |
| `runs/` | `runs/<turn_id>.json`, `runs/failing/` | until deleted | yes, the full event stream including tool results | anyone with the project folder. Must not be committed |
| `actions_log` | Postgres | until the next `./run.sh reset` | yes, the customer's email and action | the app through the `toolbox` role, scoped by the bound email |

**A memory says one thing, but the order changed later. Why do the tools still win?** A memory is what the customer told us once, found again by a similarity score. Nobody checks it against the database or updates it when the order moves. A tool reads the database at the moment of the question, filtered by the logged-in email. So the database is the only source for order facts, and memory is for preferences that no table holds.

That rule is enforced by a **prompt**, not code. The agent's instruction says: "for any order fact, the tools win: never state an order's status from a memory." Nothing in code stops the agent repeating a stale memory, so this is a place where I rely on the model following an instruction. The risk is limited because order facts rarely reach memory in the first place. Saving reinforces this: after Mem0 started storing order questions as memories (scoring 0.43), I added extraction instructions so order state isn't saved at all.

## Trade-offs

Timings from the order-3 trace `4f4939ba…`, which took 5.4 s end to end:

| Step | Cost | What it bought | Worth it? |
|---|---|---|---|
| Sanitizer | 0 ms | rejects oversized or malformed input before anything else runs | yes, free |
| Judge | ~950 ms | 30/30 attacks blocked in run 9. Patterns alone catch 29/30 in milliseconds (blocked p95 5 ms), and the model caught X30 | yes, but it's the biggest single guard cost on every legitimate message |
| Guardrail | ~1 s | 15/15 off-topic blocked, 0/30 false blocks at v3 | yes, but it waits for the Judge (see below) |
| Memory recall | 420 ms | 9/10 memory recall in run 9 | yes for returning customers. Wasted for messages that don't need memory |
| Agent | 2.7 s | the answer | — |
| Masker | ~11 ms | other people's emails, phones and card numbers can't leave | yes, deterministic and nearly free |
| Memory save | 255 ms | the next session remembers | yes. It could run after the reply is sent |

The guards before the agent add about 2 s, and the agent itself is 2.7 s of the 5.4 s.

**Running the guards side by side.** Starting the Judge and Guardrail together would save about 1 s on every legitimate message. But it collides with P-3, "a block ends the turn; nothing after it runs." If the Judge blocks X01 by pattern in a few milliseconds, the Guardrail's model call is already in flight. I'd cancel it, but a cancelled call has still started, has probably been billed, and leaves a span in the trace, so by the letter of P-3 something ran after the block. A version that keeps P-3 runs the pattern scan alone first, since that's where nearly every block comes from, and only then starts the Judge's model and the Guardrail together. That still leaves one gap, a model-level Judge block like X30, where the Guardrail call would have to be cancelled. Rather than bend P-3, I've kept the guards in sequence and paid the ~1 s.

**Fail closed.** Every guard that errors blocks the turn rather than letting the message through. That was right for the Judge: when Gemini returned 429s, the turn stopped with `[error] judge` and no answer (trace `93e4955f…`), instead of skipping the security check. The cost showed up when Mem0 hit its monthly limit (1000/1000) in runs 14–15. Every turn that reached memory recall ended in the Mem0 error. That included "What is the status of order 3?", which needs nothing but the database. Only turns blocked earlier still got an answer. Failing closed suits the security guards. Memory isn't a security control, so if I changed one thing, recall would fail open, but visibly. The recall step would report its error as an event, mark its span as an error and show `[error] memory` in the CLI, and the turn would continue without memories. A silent skip, where the customer just gets an answer that ignores their saved instructions and nothing records why, is what R-4 warns about, so that's not what I mean.

**Memory cutoff: 0.25 → 0.20 → 0.17.** The planted delivery instruction M01 scored 0.24. At 0.25 it was dropped, and a new session said there were no saved instructions (`b0a67ad3…`). At 0.20 it came back: "left at the back door" (`1177455d…`). In eval run 1, M02 and M07 also scored under 0.20, so the cutoff went to 0.17. A lower cutoff lets more loosely related memories through. The cutoff couldn't fix bad memories anyway: the order questions Mem0 saved scored 0.43, above any cutoff. That was fixed at save time instead.

**Gemini → Claude.** The order-3 turn went from 8.2 s to 4.4 s and the Judge from 5.9 s to 1.7 s. Gemini also cost reliability: `gemini-2.5-flash` was closed to new users, `gemini-3-flash-preview` returned 503s and allowed 20 free requests a day, and daily-quota 429s stopped my manual testing for a day. Every eval run in the report ran on Claude. The price is a paid API: Sonnet 5.5 for the agent, Haiku 4.5 for the guards.