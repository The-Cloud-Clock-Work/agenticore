"""ProgressSink ABC — the one interface every connector implements.

A connector (Telegram, Slack, Teams, Discord, custom HTTP relay, …) receives
canonical progress events from AgentExecutor by subclassing ProgressSink and
overriding whichever callbacks it cares about. Default implementations are
no-ops, so a connector only implements what it renders.

The contract is the same across every transport. Whether you're editing a
Telegram message in place, posting to a Slack thread, or rendering an
Adaptive Card in Teams, the events you receive are identical — visibility is
already filtered against the agent's stream_config before events reach this
sink.

See docs/design/progress-sink.md for the canonical event vocabulary and
connector authoring guide.
"""

from __future__ import annotations

import abc
from typing import Optional


class ProgressSink(abc.ABC):
    """Abstract base for any downstream consumer of agent progress events.

    All callbacks are coroutines. Default implementations are no-ops, so a
    connector overrides only what it wants to render. Every callback is
    fired in emission order; callbacks must not block (use background tasks
    for slow I/O like rate-limited edits).
    """

    async def on_narration(self, text: str) -> None:
        """Interleaved assistant text emitted between tool calls.

        Appears as the agent works ("checking logs…", "found the issue,
        patching now…"). A connector typically renders this as a live-
        updating message or progress indicator and then REPLACES it with
        the final answer when on_final fires.
        """
        return None

    async def on_final(self, text: str) -> None:
        """The final assistant answer — the authoritative reply.

        Emitted exactly once per turn, just before on_usage. A connector
        typically sends this as a new message (Telegram), a thread reply
        (Slack), or the authoritative card update (Teams).
        """
        return None

    async def on_tool_call(self, name: str, args: dict, tool_use_id: str) -> None:
        """A tool invocation — name + parsed input arguments.

        Connectors that show tool activity render a compact chip or a
        collapsible panel. Connectors that don't care (voice-first UIs,
        minimal chat surfaces) leave this as no-op.
        """
        return None

    async def on_tool_result(self, tool_use_id: str, content: str, is_error: bool) -> None:
        """Tool output paired with its tool_use_id.

        ``content`` is always a string — list-shaped tool outputs are
        joined with newlines before delivery. ``is_error`` distinguishes
        a successful result from a tool error.
        """
        return None

    async def on_thinking(self, text: str) -> None:
        """Extended-thinking content (reasoning deltas).

        Gated by ``show_thinking`` — most connectors leave this as no-op
        since raw thinking is noisy. Debug surfaces (the `/debug` channel
        in a dev chat) might subclass to render it.
        """
        return None

    async def on_usage(self, usage: dict) -> None:
        """Token counts and stop metadata, emitted once at turn end.

        Keys: ``input_tokens``, ``output_tokens``, ``cost_usd``,
        ``duration_ms``, ``stop_reason``. Connectors that display cost
        budgets render this as a footer; others no-op.
        """
        return None

    async def on_error(self, message: str, code: Optional[str] = None) -> None:
        """Subprocess failure, parse error, or upstream error event.

        Fires at most once per turn and guarantees the turn is over — no
        further callbacks will arrive after on_error. Connectors should
        surface this to the user (don't swallow).
        """
        return None


class NullSink(ProgressSink):
    """No-op sink used as the default when no sink is supplied.

    Exists so AgentExecutor code paths can unconditionally dispatch events
    without checking for None.
    """


__all__ = ["ProgressSink", "NullSink"]
