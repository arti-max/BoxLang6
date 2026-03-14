from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from ..compiler.ast_nodes import Node


@dataclass
class VarState:
    name:   str
    value:  Any
    type_:  str
    offset: int   # bp_offset (stack) или 0 (register)
    reg:    str = ""


@dataclass
class DebugEvent:
    event: str
    data:  Dict[str, Any]


class DebugSession:
    """
    Хранит состояние одной отладочной сессии.
    Компилятор вызывает методы emit_* по ходу обхода AST.
    server.py читает очередь событий и шлёт их клиенту.
    """

    def __init__(self):
        self._queue:     asyncio.Queue[DebugEvent] = asyncio.Queue()
        self._vars:      Dict[str, VarState]       = {}
        self._callstack: List[str]                 = []
        self._hex:       bytes                     = b""
        self._cursor:    int                       = 0   # текущий байт в hex

    # ── API для компилятора ───────────────────────────────────────────────────

    def emit_ast(self, tree: dict):
        self._put("ast", {"tree": tree})

    def emit_step(self, node: Node, label: str = ""):
        self._put("step", {
            "line":  getattr(node, "line", 0),
            "col":   getattr(node, "col",  0),
            "node":  type(node).__name__,
            "label": label,
        })

    def emit_var(self, var: VarState):
        self._vars[var.name] = var
        self._put("var", {
            "name":   var.name,
            "value":  var.value,
            "type":   var.type_,
            "offset": var.offset,
            "reg":    var.reg,
        })

    def emit_call(self, func_name: str):
        self._callstack.append(func_name)
        self._put("call", {
            "stack": list(self._callstack),
        })

    def emit_ret(self):
        if self._callstack:
            self._callstack.pop()
        self._put("ret", {
            "stack": list(self._callstack),
        })

    def emit_hex(self, binary: bytes, cursor: int = 0, offset_map: dict = None):
        self._hex    = binary
        self._cursor = cursor
        self._put("hex", {
            "bytes":      binary.hex(" ").upper(),
            "cursor":     cursor,
            "size":       len(binary),
            "offset_map": offset_map or {},   # { "0": 3, "1": 3, "3": 5, ... }
        })

    def emit_done(self):
        self._put("done", {})

    def emit_error(self, msg: str, line: int = 0):
        self._put("error", {"msg": msg, "line": line})

    # ── очередь ───────────────────────────────────────────────────────────────

    def _put(self, event: str, data: dict):
        self._queue.put_nowait(DebugEvent(event=event, data=data))

    async def next_event(self) -> DebugEvent:
        return await self._queue.get()

    def has_events(self) -> bool:
        return not self._queue.empty()
