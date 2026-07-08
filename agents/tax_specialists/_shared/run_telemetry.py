"""Specialist run telemetry helper.

Every tax specialist runner imports this module to record its run in the
``specialist_run`` table via the HTTP API.

Usage (context-manager style — recommended)::

    from agents.tax_specialists._shared.run_telemetry import RunTelemetry

    with RunTelemetry(
        base_url="http://localhost:8000",
        service_token="Bearer …",
        workspace_id="…",
        entity_id="…",
        tax_year=2023,
        specialist_id="tax-1120-preparer",
        backend="anthropic",
        model="claude-sonnet-4-6",
        endpoint_url="http://localhost:8000/mcp",
        input_summary={"args": [...]},
    ) as t:
        # your LLM call here
        response = client.messages.create(...)
        t.record_tool_call("get_form_1120_data")
        t.set_output_summary({"output_file": str(output_path), ...})

The context manager automatically calls ``finish()`` with status='completed'
on a clean exit, or status='failed' with the exception message on any
``Exception``. Cancellation (``KeyboardInterrupt``, ``SystemExit``) also
records status='cancelled'.

Alternatively call ``start()``, ``record_tool_call()``, and ``finish()``
manually from a try/except/finally block.

Environment override:
    ACCTOPS_BASE_URL   Override the backend base URL (default: http://localhost:8000).

Network errors in ``start()`` or ``finish()`` are NOT re-raised by default —
telemetry failures must never abort a real specialist run. Set
``strict=True`` to make them raise (useful in tests).
"""

from __future__ import annotations

import os
import time
import traceback
from typing import Any

import httpx

_DEFAULT_BASE_URL = "http://localhost:8000"


class RunTelemetry:
    """Context manager / helper for recording a specialist run lifecycle."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        service_token: str,
        workspace_id: str,
        entity_id: str,
        tax_year: int,
        specialist_id: str,
        backend: str,
        model: str,
        endpoint_url: str | None = None,
        input_summary: dict[str, Any] | None = None,
        strict: bool = False,
    ) -> None:
        self._base_url = (
            base_url
            or os.environ.get("ACCTOPS_BASE_URL")
            or _DEFAULT_BASE_URL
        ).rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {service_token}",
            "Content-Type": "application/json",
        }
        self._workspace_id = workspace_id
        self._entity_id = entity_id
        self._tax_year = tax_year
        self._specialist_id = specialist_id
        self._backend = backend
        self._model = model
        self._endpoint_url = endpoint_url
        self._input_summary = input_summary
        self._strict = strict

        # Mutable state accumulated during the run
        self._run_id: str | None = None
        self._started_wall: float | None = None
        self._tool_calls: dict[str, int] = {}  # tool_name -> count
        self._output_summary: dict[str, Any] | None = None
        self._input_tokens: int = 0
        self._output_tokens: int = 0

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    @property
    def run_id(self) -> str | None:
        """The specialist_run UUID assigned by the server, or None if not started."""
        return self._run_id

    def start(self) -> None:
        """POST /specialist-runs to create a running record.

        Idempotent: calling start() more than once is a no-op after the first
        successful call (run_id is already set).
        """
        if self._run_id is not None:
            return

        self._started_wall = time.monotonic()

        payload: dict[str, Any] = {
            "entity_id": self._entity_id,
            "tax_year": self._tax_year,
            "specialist_id": self._specialist_id,
            "backend": self._backend,
            "model": self._model,
        }
        if self._endpoint_url:
            payload["endpoint_url"] = self._endpoint_url
        if self._input_summary:
            payload["input_summary"] = self._input_summary

        url = f"{self._base_url}/api/workspaces/{self._workspace_id}/specialist-runs"
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(url, json=payload, headers=self._headers)
                resp.raise_for_status()
                self._run_id = resp.json()["id"]
        except Exception as exc:
            if self._strict:
                raise
            # Non-strict: log and continue — telemetry must not kill the run.
            print(f"[telemetry] WARNING: start() failed — {exc}")

    def record_tool_call(self, tool_name: str, count: int = 1) -> None:
        """Increment the per-tool call counter for this run."""
        self._tool_calls[tool_name] = self._tool_calls.get(tool_name, 0) + count

    def record_tokens(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Accumulate token counts from an SDK usage object."""
        self._input_tokens += input_tokens
        self._output_tokens += output_tokens

    def record_usage(self, usage: Any) -> None:
        """Extract input/output tokens from an Anthropic/OpenAI usage object.

        Accepts any object with ``.input_tokens`` / ``.output_tokens`` attributes,
        or a dict with the same keys.
        """
        if usage is None:
            return
        if isinstance(usage, dict):
            self.record_tokens(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )
        else:
            self.record_tokens(
                input_tokens=getattr(usage, "input_tokens", 0),
                output_tokens=getattr(usage, "output_tokens", 0),
            )

    def set_output_summary(self, summary: dict[str, Any]) -> None:
        """Set the output summary (call after the LLM run succeeds)."""
        self._output_summary = summary

    def finish(
        self,
        status: str = "completed",
        output_summary: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        """PATCH /specialist-runs/{id}/finish with final status + metrics.

        ``status`` must be 'completed', 'failed', or 'cancelled'.
        ``output_summary`` overrides any value set via set_output_summary().
        ``error`` populates error_summary when provided (or when status='failed').
        """
        if self._run_id is None:
            # start() was never called or failed — nothing to patch.
            return

        # Compute duration
        duration_ms: int | None = None
        if self._started_wall is not None:
            duration_ms = int((time.monotonic() - self._started_wall) * 1000)

        # Build metrics
        total_tool_calls = sum(self._tool_calls.values())
        metrics: dict[str, Any] = {
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "total_tool_calls": total_tool_calls,
        }
        if self._tool_calls:
            metrics["mcp_tool_breakdown"] = dict(self._tool_calls)

        # Error summary
        error_summary: str | None = None
        if error is not None:
            error_summary = f"{type(error).__name__}: {error}"
        elif status == "failed":
            error_summary = "Run failed (no exception provided)"

        final_output = output_summary or self._output_summary

        payload: dict[str, Any] = {
            "status": status,
            "duration_ms": duration_ms,
            "metrics": metrics,
        }
        if final_output:
            payload["output_summary"] = final_output
        if error_summary:
            payload["error_summary"] = error_summary

        url = (
            f"{self._base_url}/api/workspaces/{self._workspace_id}"
            f"/specialist-runs/{self._run_id}/finish"
        )
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.patch(url, json=payload, headers=self._headers)
                resp.raise_for_status()
        except Exception as exc:
            if self._strict:
                raise
            print(f"[telemetry] WARNING: finish() failed — {exc}")

    # -----------------------------------------------------------------------
    # Context manager protocol
    # -----------------------------------------------------------------------

    def __enter__(self) -> "RunTelemetry":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        if exc_type is None:
            # Clean exit
            self.finish(status="completed")
        elif issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            # Cancellation — record but do NOT suppress
            self.finish(status="cancelled")
            return False
        else:
            # Exception — record as failed, do NOT suppress
            self.finish(
                status="failed",
                error=exc_val if isinstance(exc_val, Exception) else None,
            )
            return False
        return False  # never suppress exceptions

    # -----------------------------------------------------------------------
    # Convenience: extract tool-use events from an Anthropic response
    # -----------------------------------------------------------------------

    def ingest_anthropic_response(self, response: Any) -> None:
        """Parse an Anthropic Messages response and accumulate telemetry.

        Extracts:
          - tool_use blocks -> record_tool_call(tool_name)
          - response.usage  -> record_usage(usage)

        Pass the raw response object from ``client.beta.messages.create()``.
        """
        if response is None:
            return
        # Tool calls
        for block in getattr(response, "content", []):
            if getattr(block, "type", None) == "tool_use":
                self.record_tool_call(block.name)
            elif getattr(block, "type", None) == "server_tool_use":
                # MCP server-side tool calls exposed in newer SDK versions
                self.record_tool_call(getattr(block, "name", "server_tool"))
        # Token usage
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.record_usage(usage)
