"""Tracing: every turn is one trace in Phoenix (SPEC §10).

Phoenix runs as its own process with persistent storage (O-1, `./run.sh phoenix`); this
module only exports to it. `setup()` must run before any agent is built so ADK's spans
(invoke_agent, call_llm, execute_tool) use this provider.

Decision (stage 5): spans record full content: the message, the reply, model prompts and
tool results (O-2, O-3). Phoenix listens on 127.0.0.1 only and stores data in .phoenix/
(gitignored). Fine for seeded demo data; not acceptable for real customers (see DESIGN.md).
"""
from __future__ import annotations

import os

import httpx
from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
from openinference.semconv.resource import ResourceAttributes
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

PHOENIX_URL = f"http://127.0.0.1:{os.environ.get('PHOENIX_PORT', '6006')}"
PROJECT = os.environ.get("PHOENIX_PROJECT", "customer-support")

KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND
CHAIN = OpenInferenceSpanKindValues.CHAIN.value
GUARDRAIL = OpenInferenceSpanKindValues.GUARDRAIL.value
RETRIEVER = OpenInferenceSpanKindValues.RETRIEVER.value
TOOL = OpenInferenceSpanKindValues.TOOL.value

# ADK names its spans but gives them no OpenInference kind; SPEC §10 requires one.
_ADK_KINDS = {
    "invoke_workflow": CHAIN,          # ADK 2.x wrapper around invoke_agent
    "invoke_agent": OpenInferenceSpanKindValues.AGENT.value,
    "call_llm": OpenInferenceSpanKindValues.LLM.value,
    "execute_tool": TOOL,
}


class _AdkSpanKinds(SpanProcessor):
    def on_start(self, span, parent_context=None):
        # ADK 2.x calls its model-call span "generate_content <model>"; SPEC §10 names it call_llm
        if span.name.startswith("generate_content "):
            span.update_name("call_llm")
        if span.attributes and KIND in span.attributes:
            return
        kind = _ADK_KINDS.get(span.name.split(" ", 1)[0])
        if kind:
            span.set_attribute(KIND, kind)


_provider: TracerProvider | None = None
_project_id: str | None = None


def setup() -> TracerProvider:
    global _provider
    if _provider is None:
        _provider = TracerProvider(resource=Resource.create({
            "service.name": "customer-support",
            ResourceAttributes.PROJECT_NAME: PROJECT,
        }))
        _provider.add_span_processor(_AdkSpanKinds())
        _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{PHOENIX_URL}/v1/traces")))
        trace.set_tracer_provider(_provider)
        # model calls carry prompts, outputs and token counts (O-3), whichever provider runs
        GoogleGenAIInstrumentor().instrument(tracer_provider=_provider)
        try:
            from openinference.instrumentation.anthropic import AnthropicInstrumentor
            AnthropicInstrumentor().instrument(tracer_provider=_provider)
        except ImportError:
            pass
    return _provider


tracer = trace.get_tracer("support.pipeline")


def flush() -> None:
    if _provider is not None:
        _provider.force_flush(timeout_millis=5000)


def check_phoenix() -> str:
    """'ok', or why Phoenix is unusable (for C-4 and /health)."""
    try:
        httpx.get(f"{PHOENIX_URL}/v1/projects", timeout=2).raise_for_status()
        return "ok"
    except Exception as exc:
        return f"Phoenix unreachable at {PHOENIX_URL}: {exc}"


def _ensure_project_id() -> str | None:
    """Phoenix trace URLs use the project's id; create the project if no span has yet."""
    global _project_id
    if _project_id:
        return _project_id
    try:
        projects = httpx.get(f"{PHOENIX_URL}/v1/projects", timeout=2).json().get("data", [])
        found = next((p for p in projects if p.get("name") == PROJECT), None)
        if found is None:
            found = httpx.post(f"{PHOENIX_URL}/v1/projects", json={"name": PROJECT}, timeout=2).json()["data"]
        _project_id = found["id"]
    except Exception:
        return None
    return _project_id


def trace_url(trace_id: str) -> str:
    project_id = _ensure_project_id()
    if project_id is None:
        return f"{PHOENIX_URL}/projects"
    return f"{PHOENIX_URL}/projects/{project_id}/traces/{trace_id}"
