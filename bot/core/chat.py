"""Accept messages and invoke registered handlers in top-level or child turns."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from uuid import uuid4

from .storage import Conversation, ConversationStore, Message

logger = logging.getLogger(__name__)


class DispatchError(Exception):
    """A routing or application error whose message can be shown to the caller."""


@dataclass(frozen=True)
class Forward:
    target: str
    text: str


@dataclass(frozen=True)
class DispatchResult:
    target: str
    conversation_id: str | None = None
    reply: Message | None = None
    error: str | None = None


@dataclass(frozen=True)
class Turn:
    conversation: Conversation
    message: Message
    history: tuple[Message, ...]
    _forward: Callable[..., Awaitable[tuple[DispatchResult, ...]]] = field(repr=False)

    async def forward(self, *requests: Forward) -> tuple[DispatchResult, ...]:
        """Run independent child turns concurrently and return results in request order."""
        return await self._forward(*requests)


Handler = Callable[[Turn], Awaitable[str]]


class ChatCore:
    def __init__(self, store: ConversationStore, *, max_depth: int = 4) -> None:
        if max_depth < 0:
            raise ValueError("max_depth must be non-negative")
        self._store = store
        self._max_depth = max_depth
        self._handlers: dict[str, Handler] = {}

    def register(self, name: str, handler: Handler) -> None:
        if not name or name in self._handlers:
            raise ValueError(f"Invalid or duplicate handler name: {name!r}")
        self._handlers[name] = handler

    def open(self, participants: Iterable[str]) -> Conversation:
        members = tuple(dict.fromkeys(participants))
        if not members or any(not member.strip() for member in members):
            raise ValueError("A conversation requires at least one valid participant")
        conversation = Conversation(uuid4().hex, members)
        self._store.create(conversation)
        return conversation

    def history(self, conversation_id: str, actor: str) -> tuple[Message, ...]:
        self._accessible(conversation_id, actor)
        return self._store.history(conversation_id)

    def post(self, conversation_id: str, actor: str, text: str) -> Message:
        conversation = self._accessible(conversation_id, actor)
        return self._post(conversation, actor, text)

    async def submit(self, conversation_id: str, actor: str, target: str, text: str) -> Message:
        conversation = self._accessible(conversation_id, actor)
        handler = self._resolve(target)
        depth = self._depth(conversation)
        message = self._post(conversation, actor, text)
        return await self._invoke(conversation, message, target, handler, depth)

    def _depth(self, conversation: Conversation) -> int:
        depth = 0
        while conversation.parent_id is not None:
            depth += 1
            parent = self._store.get(conversation.parent_id)
            if parent is None or depth > self._max_depth:
                raise DispatchError("Conversation ancestry unavailable or too deep")
            conversation = parent
        return depth

    def _accessible(self, conversation_id: str, actor: str) -> Conversation:
        conversation = self._store.get(conversation_id)
        if conversation is None or actor not in conversation.participants:
            raise DispatchError("Conversation unavailable to this participant")
        return conversation

    def _resolve(self, target: str) -> Handler:
        try:
            return self._handlers[target]
        except KeyError:
            raise DispatchError(f"Unknown handler: {target}") from None

    def _post(self, conversation: Conversation, actor: str, text: str) -> Message:
        if not text.strip():
            raise DispatchError("A message cannot be empty")
        message = Message(uuid4().hex, conversation.id, actor, "input", text)
        self._store.append(message)
        return message

    async def _invoke(
        self, conversation: Conversation, message: Message, target: str, handler: Handler, depth: int,
    ) -> Message:
        async def forward(*requests: Forward) -> tuple[DispatchResult, ...]:
            tasks = (
                self._dispatch(conversation, message, request, depth + 1) for request in requests
            )
            results = await asyncio.gather(*tasks, return_exceptions=True)
            replies = []
            for request, result in zip(requests, results):
                if isinstance(result, asyncio.CancelledError):
                    replies.append(DispatchResult(request.target, error="Handler cancelled"))
                elif isinstance(result, BaseException):
                    raise result
                else:
                    replies.append(result)
            return tuple(replies)

        turn = Turn(conversation, message, self._store.history(conversation.id), forward)
        text = await handler(turn)
        if not isinstance(text, str) or not text.strip():
            raise DispatchError(f"Handler {target} returned an empty or invalid response")
        reply = Message(uuid4().hex, conversation.id, target, "output", text, message.id)
        self._store.append(reply)
        return reply

    async def _dispatch(
        self, parent: Conversation, origin: Message, request: Forward, depth: int,
    ) -> DispatchResult:
        child_id = None
        try:
            if depth > self._max_depth:
                raise DispatchError("Maximum conversation depth reached")
            handler = self._resolve(request.target)
            child = Conversation(uuid4().hex, (origin.sender,), parent.id, origin.id)
            self._store.create(child)
            child_id = child.id
            message = self._post(child, origin.sender, request.text)
            reply = await self._invoke(child, message, request.target, handler, depth)
            return DispatchResult(request.target, child_id, reply)
        except DispatchError as exc:
            return DispatchResult(request.target, child_id, error=str(exc))
        except Exception as exc:
            logger.error("Handler %s failed (%s)", request.target, type(exc).__name__)
            return DispatchResult(request.target, child_id, error=f"Handler {request.target} failed")
