"""消息提交、任务查询和回复确认。"""

from typing import Any

from aiohttp import web

from wintermute.events import DuplicateEventError, EventError, OutputRecord
from wintermute.logging import log_context

from .common import HTTP_INTERFACE, ClientId, HTTPError, HTTPInput, NonblankText, read_input

routes = web.RouteTableDef()


class _MessageInput(HTTPInput):
    message_id: ClientId
    text: NonblankText


def _output_data(output: OutputRecord) -> dict[str, Any]:
    # 投递异常可能包含外部请求细节；对外仅提供结果和投递状态。
    return output.model_dump(mode="json", exclude={"error", "user_id"})


@routes.post("/v1/messages")
async def submit(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    message = await read_input(request, _MessageInput, "invalid_message")
    with log_context(
        terminal_id=binding.terminal_id,
        user_id=binding.user_id,
        conversation_id=binding.conversation_id,
    ):
        try:
            receipt = await interface.gateway.conversation.connect(
                binding.terminal_id, binding.conversation_id
            ).send(message.text, producer_event_id=message.message_id)
        except DuplicateEventError:
            raise HTTPError(409, "message_id_conflict") from None
        except EventError:
            raise HTTPError(400, "event_rejected") from None
    return web.json_response(
        {
            "event_id": receipt.event.event_id,
            "job_id": receipt.job_id,
            "duplicate": receipt.duplicate,
        },
        status=202,
    )


@routes.get("/v1/jobs/{job_id}")
async def job(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    try:
        job = interface.gateway.store.get_job(request.match_info["job_id"], user_id=binding.user_id)
        event = interface.gateway.store.get_event(job.event_id, user_id=binding.user_id)
    except EventError:
        raise HTTPError(404, "not_found") from None
    if job.conversation_id != binding.conversation_id or binding.terminal_id not in (
        event.terminal_id,
        event.reply_terminal_id,
    ):
        raise HTTPError(404, "not_found")
    outputs = interface.gateway.store.list_outputs(
        user_id=binding.user_id,
        conversation_id=binding.conversation_id,
        terminal_id=binding.terminal_id,
        job_id=job.job_id,
    )
    return web.json_response(
        {
            "job_id": job.job_id,
            "event_id": job.event_id,
            "conversation_id": job.conversation_id,
            "layer": job.layer,
            "status": job.status,
            "error": "agent_run_failed" if job.error is not None else None,
            "outputs": [_output_data(output) for output in outputs],
        }
    )


@routes.get("/v1/outputs")
async def outputs(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    if set(request.query) - {"limit"} or len(request.query.getall("limit", [])) > 1:
        raise HTTPError(400, "invalid_query")
    try:
        limit = int(request.query.get("limit", "100"))
    except ValueError:
        raise HTTPError(400, "invalid_limit") from None
    if not 1 <= limit <= 1000:
        raise HTTPError(400, "invalid_limit")
    outputs = interface.gateway.store.list_outputs(
        user_id=binding.user_id,
        conversation_id=binding.conversation_id,
        terminal_id=binding.terminal_id,
        status="pending",
        limit=limit,
    )
    return web.json_response({"outputs": [_output_data(output) for output in outputs]})


@routes.post("/v1/outputs/{output_id}/ack")
async def ack(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    try:
        output = interface.gateway.store.get_output(
            request.match_info["output_id"], user_id=binding.user_id
        )
    except EventError:
        raise HTTPError(404, "not_found") from None
    if (
        output.conversation_id != binding.conversation_id
        or output.terminal_id != binding.terminal_id
    ):
        raise HTTPError(404, "not_found")
    interface.gateway.store.mark_output_delivered(output.output_id)
    return web.json_response({"output_id": output.output_id, "status": "delivered"})
