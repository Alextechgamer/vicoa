"""One Antigravity conversation, driven over ``agy``'s stream-json stdio.

Owns the child process, the turn lifecycle, the event routing and every push
to vicoa-server. The runner above it owns the WebSocket, the message queue
and registration.

The process model is the part worth reading first, because it differs from
the other native wrappers:

* One ``agy`` process serves many turns (``--input-format stream-json``), but
  **an interrupt kills it**: SIGINT makes the CLI emit
  ``result{status: ERROR, error: "interrupted"}`` and exit 1 within ~0.2s
  (verified 1.2.2). Closing stdin is not an interrupt either — the running
  turn finishes first. So Stop is SIGINT, and the *next* prompt respawns the
  binary with ``--conversation <id>``, which restores the transcript. The
  conversation id is captured from ``init`` and persisted on the instance so
  a Vicoa-level resume can do the same after a daemon restart.
* ``init`` is emitted eagerly (~2s after spawn, before any prompt), so
  bring-up waits for it: an auth or flag failure exits before ``init`` and is
  reported from stderr instead of surfacing as a hung first turn.
* The stream carries no permission requests and no questions — headless
  ``agy`` auto-denies the former and settles the latter itself — so there is
  no dialog machinery here at all. See ``events.denied_actions_notice``.
* ``--model`` and the permission mode are launch flags with no runtime RPC,
  but because a respawn on the same conversation is already the normal path
  after an interrupt, a mid-session switch is honoured the same way: the new
  value is recorded and the binary is restarted (gracefully, stdin close)
  before the next turn. The change costs one ~2s bring-up, not the session.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import signal
from typing import Any, Deque, Dict, List, Optional

from integrations.headless.antigravity import spec
from integrations.headless.antigravity.events import EventMapper, TurnResult
from integrations.headless.jsonl_stream import (
    STREAM_READ_AHEAD_BYTES,
    JsonlLineReader,
    OversizedFrameError,
)
from integrations.headless.session_lifecycle import (
    WRAPPER_STOP_STATUSES as _WRAPPER_STOP_STATUSES,
)
from integrations.headless.usage import (
    UsageState,
    antigravity_context_window_for_model,
)
from protocol.system_prompt import format_prompt_prefix
from vicoa.attachments import (
    AttachmentRef,
    attachment_note,
    attachments_dir,
    save_attachment,
    unavailable_note,
)


logger = logging.getLogger(__name__)


_STATUS_ACTIVE = "ACTIVE"
_STATUS_AWAITING_INPUT = "AWAITING_INPUT"

#: How long to wait for ``init`` after spawn. A cold start loads plugins and
#: the model catalog; bounded because a hang here looks like a dead session.
INIT_TIMEOUT_SECONDS = 60.0
#: After SIGINT the CLI exits almost immediately; past this we escalate.
_INTERRUPT_EXIT_TIMEOUT = 5.0
_TERMINATE_TIMEOUT = 5.0

#: Ceiling on one physical stdout line. agy publishes no frame limit and a
#: ``result`` carries the whole closing response, so this is a runaway guard
#: far above anything observed, not a protocol rule.
_MAX_LINE_BYTES = 16 * 1024 * 1024

_STDERR_TAIL_MAX_LINES = 200
_STDERR_TAIL_MAX_CHARS = 4000

#: Silence watchdog, ported from the Claude/Codex/Pi wrappers: a turn with no
#: output at all for this long is settled so the row stops spinning. The
#: process is left alone — later output re-activates the session.
_STATUS_SETTLE_IDLE_SECONDS = 600.0
_STATUS_WATCHDOG_INTERVAL = 30.0


class AntigravityStartupError(RuntimeError):
    """``agy`` exited (or never spoke) before its ``init`` frame.

    Raised with a message already fit for the chat surface.
    """


def startup_failure_message(*, stderr_tail: str, reason: str = "") -> str:
    """Human-readable explanation for a failed bring-up.

    Same shape and tone as the Pi/ACP equivalents. The auth case is worded
    conditionally: ``agy`` keeps its Google login in the OS keyring, and an
    agent that works in a terminal can still fail when the *daemon* spawns it
    with launchd's reduced environment.
    """
    lowered = stderr_tail.lower()
    if (
        "authentication required" in lowered
        or "not authenticated" in lowered
        or "sign in" in lowered
    ):
        return (
            f"{spec.DISPLAY_NAME} couldn't start: not signed in. Run `agy` on "
            f"this machine and complete the Google sign-in, then retry. If it "
            f"works when you run it directly, the daemon may not see the same "
            f"environment — let us know at hi@vicoa.ai."
        )
    detail = reason or "it exited before emitting its init event"
    tail = f"\n\n```\n{stderr_tail.strip()}\n```" if stderr_tail.strip() else ""
    return (
        f"{spec.DISPLAY_NAME} couldn't start: {detail}.{tail}\n\n"
        f"If this looks like a Vicoa bug, please report it to hi@vicoa.ai."
    )


class AntigravitySession:
    """Drives one ``agy`` conversation for one Vicoa agent instance."""

    def __init__(
        self,
        *,
        vicoa_client: Any,
        instance_id: str,
        cwd: str,
        binary: str,
        agent_type: str,
        model: Optional[str] = None,
        permission_mode: Optional[str] = None,
        system_prompt: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        self.vicoa_client = vicoa_client
        self.instance_id = instance_id
        self.cwd = cwd
        self.binary = binary
        self.agent_type = agent_type
        self.model = model
        self.permission_mode = permission_mode
        self.system_prompt = system_prompt
        #: The agent's own conversation handle. Supplied on a Vicoa resume,
        #: otherwise learned from the first ``init`` and persisted.
        self.conversation_id = conversation_id

        self.status = "starting"
        #: Set by the runner when the session was closed from another client,
        #: so a racing in-flight turn can't re-open the row.
        self.stopping = False
        self._closed = False

        self._mapper = EventMapper(agent_type=agent_type)
        self._usage = UsageState()
        self._usage_last_core: Optional[dict] = None

        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional["asyncio.Task[None]"] = None
        self._stderr_task: Optional["asyncio.Task[None]"] = None
        self._stderr_lines: Deque[str] = collections.deque(
            maxlen=_STDERR_TAIL_MAX_LINES
        )
        self._init_seen: Optional["asyncio.Future[None]"] = None

        #: Resolves with the turn's ``TurnResult``, or ``None`` when the
        #: process went away without one.
        self._turn_done: Optional["asyncio.Future[Optional[TurnResult]]"] = None
        self._turn_active = False
        self._interrupting = False
        #: A launch flag changed while the process was up; restart it before
        #: the next turn so the change takes effect on the same conversation.
        self._restart_pending = False
        self.last_activity = 0.0

        self._watchdog_task: Optional["asyncio.Task[None]"] = None
        self._models_task: Optional["asyncio.Task[None]"] = None
        self.available_models: List[Dict[str, str]] = []
        self.current_model: Optional[str] = None
        #: agy's configured default model (``-p /model``), learned at bring-up.
        self._configured_default_model: Optional[str] = None

    # ------------------------------------------------------------------
    # Bring-up
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn, wait for ``init``, then publish models in the background."""
        if self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(self._run_status_watchdog())
        await self._spawn()
        self._models_task = asyncio.create_task(self._report_models())
        await self._set_status(_STATUS_AWAITING_INPUT)

    @property
    def process_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def turn_active(self) -> bool:
        return self._turn_active

    def stderr_tail(self) -> str:
        if not self._stderr_lines:
            return ""
        return "\n".join(self._stderr_lines)[-_STDERR_TAIL_MAX_CHARS:]

    def build_command(self) -> List[str]:
        extra_dirs = (str(attachments_dir(self.instance_id)),)
        return spec.build_command(
            binary=self.binary,
            cwd=self.cwd,
            model=self.model,
            permission_mode=self.permission_mode,
            conversation_id=self.conversation_id,
            extra_dirs=extra_dirs,
        )

    async def _spawn(self) -> None:
        """Start ``agy`` and wait for its ``init`` frame.

        Raises :class:`AntigravityStartupError` (chat-ready message) when the
        process exits first or never emits ``init``.
        """
        command = self.build_command()
        logger.info("antigravity: launching %s", " ".join(command))
        self._stderr_lines.clear()
        self._mapper.reset_turn()
        loop = asyncio.get_running_loop()
        self._init_seen = loop.create_future()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.cwd,
            env=dict(os.environ),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Read-ahead window, not a frame cap — see jsonl_stream.
            limit=STREAM_READ_AHEAD_BYTES,
            # Own process group so a stop can reach helpers agy spawns.
            start_new_session=os.name != "nt",
        )
        if process.stdin is None or process.stdout is None:
            raise AntigravityStartupError(
                startup_failure_message(
                    stderr_tail="", reason="stdio pipes did not materialize"
                )
            )
        self._process = process
        self.last_activity = loop.time()
        if process.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))
        self._reader_task = asyncio.create_task(self._read_loop(process))

        exit_waiter = asyncio.ensure_future(process.wait())
        try:
            done, _ = await asyncio.wait(
                {self._init_seen, exit_waiter},
                timeout=INIT_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not exit_waiter.done():
                exit_waiter.cancel()
        if self._init_seen in done and not self._init_seen.cancelled():
            return
        # Give the reader a moment to flush a ``result`` error frame the CLI
        # writes just before exiting (invalid --model, unknown conversation).
        await asyncio.sleep(0.2)
        pending = self._mapper.take_result()
        reason = ""
        if exit_waiter in done:
            code = process.returncode
            reason = f"it exited with code {code} before its init event"
            if pending is not None and pending.error:
                reason = pending.error
        else:
            reason = f"no init event within {int(INIT_TIMEOUT_SECONDS)}s"
            await self._terminate()
        await self._reap()
        raise AntigravityStartupError(
            startup_failure_message(stderr_tail=self.stderr_tail(), reason=reason)
        )

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def _read_loop(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        reader = JsonlLineReader(
            process.stdout, max_line_bytes=_MAX_LINE_BYTES, label="antigravity"
        )
        try:
            while True:
                try:
                    line = await reader.readline()
                except OversizedFrameError as exc:
                    logger.warning("antigravity: %s", exc)
                    continue
                if not line:
                    break
                await self._handle_line(line)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("antigravity: stdout reader failed")
        finally:
            if self._process is process:
                self._on_process_exit()

    async def _handle_line(self, line: bytes) -> None:
        text = line.decode("utf-8", "replace").strip()
        if not text:
            return
        try:
            frame = json.loads(text)
        except ValueError:
            logger.debug("antigravity: non-JSON stdout line: %s", text[:200])
            return
        if not isinstance(frame, dict):
            return
        self.last_activity = asyncio.get_running_loop().time()
        emissions = self._mapper.handle(frame)
        kind = frame.get("event")
        if kind == "init":
            if self._init_seen is not None and not self._init_seen.done():
                self._init_seen.set_result(None)
            if (
                self._mapper.conversation_id
                and self._mapper.conversation_id != self.conversation_id
            ):
                self.conversation_id = self._mapper.conversation_id
                await self._persist_conversation_id()
            self._apply_context_window()
        for emission in emissions:
            await self._post(emission.content, emission.metadata)
        if self._mapper.context_used_tokens is not None:
            self._usage.set_context_usage(self._mapper.context_used_tokens)
        if kind == "result":
            self._finish_turn(self._mapper.take_result())

    def _on_process_exit(self) -> None:
        """The child is gone (interrupt, timeout, crash). Unpark any turn."""
        process = self._process
        code = process.returncode if process is not None else None
        logger.info("antigravity: process exited (code=%s)", code)
        self._finish_turn(None)

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        """Read stderr into the tail buffer until EOF.

        Must keep draining — an unread PIPE eventually blocks the child — so
        an overlong line is skipped rather than allowed to end the drain.
        """
        while True:
            try:
                line = await stream.readline()
            except asyncio.CancelledError:
                raise
            except ValueError:
                continue
            except Exception:
                return
            if not line:
                return
            decoded = line.decode("utf-8", "replace").rstrip("\n")
            self._stderr_lines.append(decoded)
            logger.debug("antigravity stderr: %s", decoded)

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    async def deliver_user_message(
        self, text: str, attachments: "tuple[AttachmentRef, ...]" = ()
    ) -> None:
        """Run one turn. Serialization and coalescing live in the runner."""
        body = await self._build_prompt_text(text, attachments)
        if not body.strip():
            return

        if self._restart_pending and self.process_alive:
            await self._stop_gracefully()
        self._restart_pending = False
        if not self.process_alive:
            # After an interrupt (or a per-turn timeout) the binary is gone;
            # bring it back on the same conversation.
            try:
                await self._spawn()
            except AntigravityStartupError as exc:
                await self._post(str(exc))
                await self._set_status(_STATUS_AWAITING_INPUT)
                return

        process = self._process
        assert process is not None and process.stdin is not None
        loop = asyncio.get_running_loop()
        done: "asyncio.Future[Optional[TurnResult]]" = loop.create_future()
        self._turn_done = done
        self._turn_active = True
        self._interrupting = False
        self.last_activity = loop.time()
        await self._set_status(_STATUS_ACTIVE)

        payload = json.dumps({"event": "user", "message": {"content": body}}) + "\n"
        try:
            process.stdin.write(payload.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            logger.warning("antigravity: stdin write failed: %s", exc)
            self._finish_turn(None)
            await self._post(f"⚠️ **{self.agent_type} is not accepting input**\n\n{exc}")
            self._turn_active = False
            self._turn_done = None
            await self._set_status(_STATUS_AWAITING_INPUT)
            return

        try:
            result = await done
        finally:
            self._turn_active = False
            self._turn_done = None

        if self._interrupting:
            # The SIGINT'd process emits its final ``result`` ~0.2s *before*
            # it exits. Wait for the exit here so the next queued turn sees a
            # dead process and respawns, rather than writing into a closing
            # stdin. ``interrupt`` escalates to SIGTERM/SIGKILL, so this ends.
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=_INTERRUPT_EXIT_TIMEOUT + _TERMINATE_TIMEOUT + 1.0,
                )
            except asyncio.TimeoutError:
                logger.warning("antigravity: interrupted process still running")

        if result is None and not self._interrupting:
            code = process.returncode
            tail = self.stderr_tail()
            detail = f"\n\n```\n{tail}\n```" if tail else ""
            await self._post(
                f"⚠️ **{self.agent_type} exited during the turn** "
                f"(code {code}).{detail}\n\nSend another message to continue the "
                f"same conversation."
            )
        self._interrupting = False
        await self._flush_usage()
        await self._set_status(_STATUS_AWAITING_INPUT)

    async def interrupt(self) -> None:
        """Stop the running turn — by stopping the process (see module doc).

        A Stop with no turn running still settles the row, because the
        dashboard may be showing a stale ACTIVE only this path can clear.
        """
        if not self._turn_active or not self.process_alive:
            await self._set_status(_STATUS_AWAITING_INPUT)
            return
        self._interrupting = True
        process = self._process
        assert process is not None
        try:
            process.send_signal(signal.SIGINT)
        except (ProcessLookupError, OSError):
            self._finish_turn(None)
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=_INTERRUPT_EXIT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                "antigravity: no exit %.0fs after SIGINT; terminating",
                _INTERRUPT_EXIT_TIMEOUT,
            )
            await self._terminate()
        # The reader's EOF path resolves the turn; this is the backstop for a
        # reader that is still draining a large final frame.
        self._finish_turn(None)

    def _finish_turn(self, result: Optional[TurnResult]) -> None:
        future = self._turn_done
        if future is not None and not future.done():
            future.set_result(result)

    # ------------------------------------------------------------------
    # Launch-flag settings (model, permission mode)
    # ------------------------------------------------------------------

    async def set_model(self, model_id: str) -> bool:
        """Switch models for the rest of the conversation.

        Takes effect at the next turn through a restart on the same
        conversation id (see the module docstring). Returns False for an id
        that isn't in the live list, so the runner can say so instead of
        letting the next spawn fail with agy's "invalid model selection".
        """
        wanted = (model_id or "").strip()
        if not wanted:
            return False
        if wanted in spec.DEFAULT_MODEL_SENTINELS:
            wanted = ""
        elif self.available_models and all(
            m["id"] != wanted for m in self.available_models
        ):
            return False
        self.model = wanted or None
        self._schedule_restart()
        await self._patch_session_config(
            {"agent": spec.CATALOG_ID, "model": self.model, "current_model": self.model}
        )
        if self._apply_context_window():
            await self._flush_usage()
        return True

    async def set_permission_mode(self, mode: str) -> bool:
        """Switch permission modes for the rest of the conversation (same mechanism)."""
        wanted = (mode or "").strip()
        if wanted not in spec.PERMISSION_FLAGS:
            return False
        self.permission_mode = wanted
        self._schedule_restart()
        await self._patch_session_config(
            {"agent": spec.CATALOG_ID, "permission_mode": wanted}
        )
        return True

    def _schedule_restart(self) -> None:
        # A running turn keeps its process; the restart happens before the
        # next one. An idle process is restarted lazily too — it is cheaper
        # to pay the bring-up when a prompt arrives than to hold two flags'
        # worth of "is it stale" bookkeeping here.
        self._restart_pending = True

    async def _stop_gracefully(self) -> None:
        """End an idle process the way the docs describe: close stdin, wait, escalate."""
        process = self._process
        if process is None or process.returncode is not None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=_TERMINATE_TIMEOUT)
        except asyncio.TimeoutError:
            await self._terminate()
        await self._reap()

    async def _build_prompt_text(
        self, text: str, attachments: "tuple[AttachmentRef, ...]"
    ) -> str:
        """Prompt text with the profile prefix and attachment notes.

        The stream-json input accepts text blocks only, so every attachment is
        saved next to the session and referenced by path; the attachments
        folder is on the ``--add-dir`` list so ``view_file`` can open it under
        ``request-review``.
        """
        notes: List[str] = []
        for ref in attachments:
            try:
                data, mime_type = await self.vicoa_client.download_attachment(ref.id)
                local = save_attachment(
                    attachments_dir(self.instance_id), ref, data, mime_type
                )
                notes.append(attachment_note(local))
            except Exception:
                logger.exception(
                    "antigravity: failed to download attachment %s", ref.id
                )
                notes.append(unavailable_note(ref))
        body = "\n".join(part for part in [text, *notes] if part)
        prefix = (self.system_prompt or "").strip()
        if prefix and body:
            body = format_prompt_prefix(prefix) + body
        return body

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------

    async def _report_models(self) -> None:
        """PATCH the machine's live model list (and the running model) onto ``session_config``.

        ``agy models`` is a network call, so it runs off the event loop and
        is served from the on-disk cache when fresh; the servers process
        upserts ``machine_agent_models`` from the same PATCH, which is what
        the pre-launch picker reads.
        """
        try:
            models = await asyncio.to_thread(spec.fetch_models, self.binary)
        except Exception:
            logger.debug("antigravity: model discovery failed", exc_info=True)
            models = []
        # agy's own configured default is always fetched (cheap, quota-free):
        # it is what runs when the picker is on "Default" — including after a
        # mid-session switch back to it — and the context-window seed needs it.
        try:
            configured = await asyncio.to_thread(spec.fetch_current_model, self.binary)
        except Exception:
            logger.debug("antigravity: current-model lookup failed", exc_info=True)
            configured = None
        if configured:
            self._configured_default_model = configured["id"]
            if models and all(m["id"] != configured["id"] for m in models):
                models.append(configured)
        current = self._effective_model()
        delta: Dict[str, Any] = {}
        if models:
            self.available_models = models
            delta["available_models"] = models
        if current:
            self.current_model = current
            delta["current_model"] = current
        if delta:
            await self._patch_session_config(delta)
        if self._apply_context_window():
            await self._flush_usage()

    def _effective_model(self) -> Optional[str]:
        """The model the next request runs on: the explicit pick, else agy's configured default.

        ``init.model`` is deliberately not consulted: it is only present when
        ``--model`` was passed, so after a switch back to "Default" it would
        keep naming the model the *previous* process was launched with.
        """
        requested = (self.model or "").strip()
        if requested and requested not in spec.DEFAULT_MODEL_SENTINELS:
            return requested
        return self._configured_default_model

    def _apply_context_window(self) -> bool:
        """Seed the context-window size from the static per-model table.

        agy never reports a window on the wire, so this is the only source;
        the composer ring needs a max to draw a percentage at all. Returns
        True when the size changed (the caller decides whether to flush).
        """
        return self._usage.set_context_max(
            antigravity_context_window_for_model(self._effective_model())
        )

    # ------------------------------------------------------------------
    # Watchdog and pushes
    # ------------------------------------------------------------------

    async def _run_status_watchdog(self) -> None:
        while not self._closed:
            try:
                await asyncio.sleep(_STATUS_WATCHDOG_INTERVAL)
            except asyncio.CancelledError:
                return
            if self._closed or self.stopping or not self._turn_active:
                continue
            idle = asyncio.get_running_loop().time() - self.last_activity
            if idle < _STATUS_SETTLE_IDLE_SECONDS:
                continue
            logger.warning(
                "antigravity: no output for %.0fs with a turn open; settling", idle
            )
            await self._set_status(_STATUS_AWAITING_INPUT)

    async def _post(self, content: str, metadata: Optional[dict] = None) -> None:
        if not content:
            return
        try:
            await self.vicoa_client.send_message(
                content=content,
                agent_type=self.agent_type,
                agent_instance_id=self.instance_id,
                message_metadata=metadata,
            )
        except Exception:
            logger.exception(
                "antigravity: send_message failed (%d chars)", len(content)
            )

    async def _set_status(self, new_status: str) -> None:
        self.status = new_status
        if self.stopping and new_status.upper() not in _WRAPPER_STOP_STATUSES:
            return
        try:
            await self.vicoa_client.update_agent_instance_status(
                self.instance_id, new_status
            )
        except Exception:
            logger.warning("antigravity: failed to push status=%s", new_status)

    async def _patch_session_config(self, delta: dict) -> None:
        try:
            await self.vicoa_client.patch_agent_instance(
                self.instance_id, session_config=delta
            )
        except Exception:
            logger.debug("antigravity: PATCH session_config failed", exc_info=True)

    async def _persist_conversation_id(self) -> None:
        """Record the conversation handle so a later launch can ``--conversation`` it."""
        try:
            await self.vicoa_client.patch_agent_instance(
                self.instance_id,
                instance_metadata={"antigravity_conversation_id": self.conversation_id},
            )
        except Exception:
            logger.debug(
                "antigravity: failed to persist conversation id", exc_info=True
            )

    async def _flush_usage(self) -> None:
        """Stamp the context fill onto ``instance_metadata.usage`` when it changed."""
        core = self._usage.core()
        if core is None or core == self._usage_last_core:
            return
        blob = self._usage.blob()
        if blob is None:
            return
        try:
            await self.vicoa_client.patch_agent_instance(
                self.instance_id, instance_metadata={"usage": blob}
            )
            self._usage_last_core = core
        except Exception:
            logger.debug("antigravity: failed to flush usage", exc_info=True)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def _terminate(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=_TERMINATE_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("antigravity: child did not exit after SIGTERM; SIGKILL")
            try:
                process.kill()
            except ProcessLookupError:
                return
            await process.wait()

    async def _reap(self) -> None:
        """Cancel the per-process tasks once the process is gone."""
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._reader_task = None
        self._stderr_task = None

    async def aclose(self) -> None:
        self._closed = True
        for task in (self._watchdog_task, self._models_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._finish_turn(None)
        process = self._process
        if process is not None and process.returncode is None:
            # Close stdin so a running turn can finish and the CLI exits 0 on
            # its own; escalate only if it doesn't.
            if process.stdin is not None and not process.stdin.is_closing():
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                await self._terminate()
        await self._reap()


__all__ = [
    "AntigravitySession",
    "AntigravityStartupError",
    "INIT_TIMEOUT_SECONDS",
    "startup_failure_message",
]
