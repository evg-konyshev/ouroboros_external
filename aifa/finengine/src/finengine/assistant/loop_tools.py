"""Изменяющие инструменты цикла `aifa_move` и `aifa_finish` (CONTRACTS.md §3.1, ADR-0042, ADR-0060)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from finengine.assistant import ouro_view
from finengine.assistant.goal_tools import reply_of
from finengine.assistant.jobs import JobBoard
from finengine.assistant.operations import Operation, claim_operation
from finengine.assistant.loop_desk import LoopDesk, OpenLoop
from finengine.assistant.ouro_view import Refusal
from finengine.assistant.subtask_waits import SubtaskWaits
from finengine.assistant.tool_call import BoundCall
from finengine.config import settings
from finengine.ingestion.document_store import get_store
from finengine.skills.run_orchestrator import emit_run_batch
from finengine.skills.production_runtime import publish_result
from finengine.skills.spec.dynamic_run import Rejection, decision_from_data

FINISH_OBJECTIVE = "Ответить пользователю"
FINISH_REASON = "Цикл завершает ouroboros"


def no_loop(why: str = "") -> str:
    return ouro_view.refusal(Refusal.NO_LOOP, why or "Цикл не открыт: его открывает aifa_open.")


def _document_exists(doc_id: str) -> bool:
    return get_store().get_document(doc_id) is not None


async def document_loop(desk: LoopDesk, call: BoundCall) -> OpenLoop | str | None:
    doc_id = call.args.get("doc_id")
    if "doc_id" in call.args and (not isinstance(doc_id, str) or not doc_id):
        return ouro_view.refusal(Refusal.BAD_ARGUMENTS, "doc_id — непустой id документа.")
    if doc_id is None:
        opened = await desk.report_ids_of(call)
        if len(opened) > 1:
            return ouro_view.refusal(Refusal.BAD_ARGUMENTS, f"Укажи doc_id открытого цикла: {', '.join(opened)}.")
        doc_id = opened[0] if opened else None
    if doc_id is not None and (
        doc_id not in call.record.doc_ids or not await asyncio.to_thread(_document_exists, doc_id)
    ):
        return ouro_view.refusal(Refusal.NO_DOCUMENT, "Документ отсутствует в ходе или удалён.")
    return await desk.open_loop_of(call, doc_id)


class ExclusiveTool:
    """Вызов, меняющий `LoopSession`: идёт по одному на ход, ожидание очереди входит в срок вызова (ADR-0043 п. 4)."""

    name = ""
    root_only = False
    finish_turn = False

    def __init__(self, board: JobBoard, desk: LoopDesk) -> None:
        self._board = board
        self._desk = desk

    async def run(self, call: BoundCall) -> str:
        operation = None
        if call.ctx.operation_id:
            try:
                operation = await asyncio.to_thread(claim_operation, call, self.name, uuid.uuid4().hex)
            except ValueError as exc:
                return ouro_view.refusal(Refusal.BAD_ARGUMENTS, str(exc))
            if not operation.created:
                if operation.result is not None:
                    return operation.result
                job = self._board.find(call.ctx.run_id, operation.job_id)
                if job is None:
                    job = self._board.start(call.ctx.run_id, self.name, operation.wait(), job_id=operation.job_id)
                return await reply_of(self._board, job, settings.ouroboros_goal_wait_sec)
        job = self._board.start(
            call.ctx.run_id, self.name, self._queued(call, operation),
            job_id=operation.job_id if operation else None,
        )
        return await reply_of(self._board, job, settings.ouroboros_goal_wait_sec)

    async def _queued(self, call: BoundCall, operation: Operation | None = None) -> str:
        async with self._desk.queue_of(call.ctx.run_id):
            try:
                text = await self._resolve_and_execute(call)
            except Exception as exc:
                # Без записи итога повтор этого operation_id ждал бы результат, которого не будет.
                if operation is not None:
                    await asyncio.to_thread(operation.finish, ouro_view.service_fault(type(exc).__name__))
                raise
            if operation is not None:
                await asyncio.to_thread(operation.finish, text)
            return text

    async def _resolve_and_execute(self, call: BoundCall) -> str:
        if not self.finish_turn and await asyncio.to_thread(
            self._desk.reports_closed, call.ctx.run_id, call.record.user_id,
        ):
            return ouro_view.refusal(Refusal.LOOP_CLOSED, "Циклы закрыты aifa_finish.")
        loop = await self._desk.open_loop_of(call) if self.finish_turn else await document_loop(self._desk, call)
        if isinstance(loop, str):
            return loop
        if self.finish_turn and loop is None:
            loop = next(iter(await self._desk.loops_of(call)), None)
        if loop is None:
            return no_loop()
        return await self._execute_in_queue(loop, call)

    async def _execute_in_queue(self, loop: OpenLoop, call: BoundCall) -> str:
        return await asyncio.to_thread(self._execute_live, loop, call.args)

    def _execute_live(self, loop: OpenLoop, args: dict[str, Any]) -> str:
        if loop.doc_id is not None and not _document_exists(loop.doc_id):
            return ouro_view.refusal(Refusal.NO_DOCUMENT, "Документ удалён.")
        return self._execute(loop, args)

    def _execute(self, loop: OpenLoop, args: dict[str, Any]) -> str:
        raise NotImplementedError

    def _rejected(self, loop: OpenLoop, reason: Rejection) -> str:
        """Отказ хода не тратит, но идёт в счёт отказов подряд: три подряд — `ЛИМИТ` (ADR-0060 п. 5)."""
        loop.session.reject(reason, feedback=f"Твой вызов {self.name} не принят: {reason}.", spends_move=False)
        self._desk.save(loop.run_id, loop.doc_id)
        return ouro_view.refusal(Refusal(reason.code), reason)


class MoveTool(ExclusiveTool):
    name = "aifa_move"
    root_only = True    # ходы тратят общие пределы цикла: подзадача их не ведёт (ADR-0060 п. 5)

    def _execute(self, loop: OpenLoop, args: dict[str, Any]) -> str:
        session = loop.session
        if session.closed:
            return ouro_view.refusal(Refusal.LOOP_CLOSED, "Цикл закрыт aifa_finish: ходить больше нельзя.")
        reason = session.limit()
        if reason is not None:
            return ouro_view.limited(reason)
        try:
            decision = decision_from_data(
                {**args, "action": "execute"}, scenarios_enabled=False,
                goals_enabled=session.run_goals is not None, raw_query=session.goal.raw_query,
            )
        except ValueError as problem:
            return self._rejected(loop, Rejection(str(problem)))
        checked = session.check(decision)
        if isinstance(checked, Rejection):
            return self._rejected(loop, checked)
        if session.goal.intent_kind == "market_digest":
            with session.state_lock:
                for clause in decision.new_goals:
                    session.market_windows.setdefault(clause.goal_ref, datetime.now(timezone.utc).isoformat())
                self._desk.save(loop.run_id, loop.doc_id)
        known = ouro_view.shown_ids(session)
        outcome = session.move(checked)
        self._desk.save(loop.run_id, loop.doc_id)
        if outcome.refusal:
            return ouro_view.refusal(Refusal(outcome.refusal), outcome.details)
        return ouro_view.moved(
            session, outcome, known, limit=settings.ouroboros_result_limit, exhausted=session.limit(),
        )


class FinishTool(ExclusiveTool):
    name = "aifa_finish"
    root_only = True
    finish_turn = True

    def __init__(self, board: JobBoard, desk: LoopDesk, subtasks: SubtaskWaits) -> None:
        super().__init__(board, desk)
        self._subtasks = subtasks

    async def _resolve_and_execute(self, call: BoundCall) -> str:
        run_id, user_id = call.ctx.run_id, call.record.user_id
        pending = await asyncio.to_thread(self._desk.pending_finish, run_id, user_id)
        if pending is not None:
            return await self._publish_staged(run_id, user_id, pending)
        return await super()._resolve_and_execute(call)

    async def _execute_in_queue(self, loop: OpenLoop, call: BoundCall) -> str:
        report_ids = await self._desk.report_ids_of(call)
        result = await asyncio.to_thread(self._finish, loop, call.args, report_ids)
        if isinstance(result, str):
            return result
        return await self._publish_staged(loop.run_id, loop.user_id, result)

    async def _publish_staged(self, run_id: str, user_id: int | str, result: dict[str, Any]) -> str:
        await asyncio.to_thread(self._desk.close_reports, run_id, user_id, tuple(result["doc_ids"]))
        await emit_run_batch(run_id, "aifa_finish", result["events"])
        await asyncio.to_thread(self._desk.finish_committed, run_id, user_id)
        return str(result["reply"])

    def _finish(self, loop: OpenLoop, args: dict[str, Any], report_ids: tuple[str, ...]) -> str | dict[str, Any]:
        session = loop.session
        if session.closed:
            return ouro_view.refusal(Refusal.LOOP_CLOSED, "Цикл уже закрыт: aifa_finish принят раньше.")
        try:
            decision = decision_from_data(
                {
                    "objective": FINISH_OBJECTIVE, "reason": FINISH_REASON, "action": args.get("action"),
                    "deliverables": args.get("deliverables", []), "evidence_ids": args.get("evidence_ids", []),
                },
                scenarios_enabled=False, answer_in_chat=True,
            )
        except ValueError as problem:
            return self._rejected(loop, Rejection(str(problem)))
        last_word = session.limit() is not None
        checked = session.check(decision, last_word=last_word)
        if isinstance(checked, Rejection) and not last_word:
            return self._rejected(loop, checked)
        sink = session.event_sink
        events: list[dict[str, Any]] = []
        if report_ids:
            session.event_sink = events.append
        rejected = session.rejected
        try:
            if isinstance(checked, Rejection):
                result, refused = session.stopped(checked), str(checked)
            else:
                result, refused = session.finish(checked), ""
            filenames = self._desk.report_names(loop.run_id, loop.user_id, report_ids)
            reply = ouro_view.accepted(
                result, reason=refused, unfinished=self._subtasks.unfinished(loop.run_id), filenames=filenames,
            )
            if not report_ids:
                self._desk.save(loop.run_id)
            publish_result(
                result, session.event_sink, case_id=session.case_id, document_id=loop.origin.document_id or "",
                document_ids=report_ids,
            )
            if not report_ids:
                return reply
            pending = {"doc_ids": list(report_ids), "reply": reply, "events": events}
            self._desk.stage_finish(loop.run_id, loop.user_id, pending)
            return pending
        except Exception:
            session.closed = False
            session.rejected = rejected
            raise
        finally:
            session.event_sink = sink
