#!/usr/bin/env python3
"""
BoxLang6 Debugger — FastAPI + WebSocket бэкенд.
Запуск:
    python -m boxlang6.debug path/to/file.box [--arch x16] [--port 8765]
"""
from __future__ import annotations
import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import uvicorn

from ..compiler.lexer       import Lexer
from ..compiler.parser      import Parser
from ..compiler.semantic    import SemanticAnalyzer
from ..compiler.optimizer   import Optimizer
from ..targets.binary_target import BinaryTarget, CodeGenError
from ..targets.base_target   import ArchLoadError
from .session   import DebugSession
from .ast_walker import ast_to_dict


app   = FastAPI(title="BoxLang6 Debugger")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# глобальное состояние сервера
_sessions:  Set[WebSocket] = set()
_session:   DebugSession   = DebugSession()
_src_path:  str            = ""
_arch:      str            = "x16"
_SKIP_OWN_MAP = {"Program", "Namespace", "TypeRef", "Param"}
_CONTAINER_NODES = {"Program", "Namespace", "FunctionDef", "WhileLoop", "ForLoop"}
_NO_CODE_NODES   = {"TypeRef", "Param"}


# ─── HTTP ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/source")
async def source():
    try:
        text = Path(_src_path).read_text(encoding="utf-8")
        return {"source": text, "path": _src_path}
    except OSError:
        return {"source": "", "path": _src_path}


# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _sessions.add(ws)
    try:
        # сразу шлём исходник
        text = Path(_src_path).read_text(encoding="utf-8")
        await ws.send_text(json.dumps({
            "event": "source",
            "data":  {"source": text, "path": _src_path}
        }))

        # слушаем очередь событий и шлём клиенту
        while True:
            try:
                ev = await asyncio.wait_for(_session.next_event(), timeout=0.1)
                await ws.send_text(json.dumps({
                    "event": ev.event,
                    "data":  ev.data,
                }))
                if ev.event == "done":
                    break
            except asyncio.TimeoutError:
                # проверяем не закрылся ли клиент
                try:
                    await ws.send_text(json.dumps({"event": "ping"}))
                except Exception:
                    break

    except WebSocketDisconnect:
        pass
    finally:
        _sessions.discard(ws)


# ─── Компиляция в фоне ───────────────────────────────────────────────────────

def _run_pipeline():
    """Запускается в отдельном потоке, не блокирует event loop."""
    global _session

    try:
        source = Path(_src_path).read_text(encoding="utf-8")
    except OSError as e:
        _session.emit_error(f"Cannot read file: {e}")
        _session.emit_done()
        return

    # Lexer
    try:
        tokens = Lexer(source).tokenize()
    except Exception as e:
        _session.emit_error(f"Lexer: {e}")
        _session.emit_done()
        return

    # Parser
    try:
        program = Parser(tokens).parse()
    except Exception as e:
        _session.emit_error(f"Parser: {e}")
        _session.emit_done()
        return

    # отправляем AST сразу после парсинга
    _session.emit_ast(ast_to_dict(program))

    # Semantic
    try:
        SemanticAnalyzer().analyze(program)
    except Exception as e:
        _session.emit_error(f"Semantic: {e}")
        _session.emit_done()
        return

    # Optimizer
    program = Optimizer().optimize(program)

    # CodeGen — BinaryTarget с debug-хуками
    try:
        target = DebugBinaryTarget(_arch, _session)
        binary = target.emit(program)
        # НЕ вызываем _session.emit_hex здесь — DebugBinaryTarget.emit() уже делает это
    except (ArchLoadError, CodeGenError) as e:
        _session.emit_error(str(e))
        _session.emit_done()
        return

    _session.emit_done()


# ─── DebugBinaryTarget — BinaryTarget с хуками ───────────────────────────────

from ..targets.binary_target import BinaryTarget
from ..compiler.ast_nodes    import (
    FunctionDef, VarDecl, Assignment, WhileLoop,
    ForLoop, ReturnStmt, ExitCall, FunctionCall
)



class DebugBinaryTarget(BinaryTarget):

    def __init__(self, arch_name: str, session):
        super().__init__(arch_name)
        self._dbg        = session
        self._offset_map: dict[str, int] = {}

    # ─── утилита записи диапазона ────────────────────────────────────────────

    def _record(self, node, fn, overwrite=True):
        start = self._pos()
        fn()
        end  = self._pos()
        line = getattr(node, "line", 0)
        typ  = type(node).__name__
        # print(f"    _record {typ} line={line} [{start}:{end}] overwrite={overwrite}")
        # print(f"    map before write: { {k:v for k,v in self._offset_map.items() if start <= int(k) < end} }")
        if line and end > start:
            for i in range(start, end):
                key = str(i)
                if overwrite or key not in self._offset_map:
                    self._offset_map[key] = line


    def _emit_FunctionDef(self, node):
        self._dbg.emit_call(node.name)
        parent = super()
        # НЕ перезаписываем — дочерние узлы запишутся внутри fn() первыми
        # и останутся нетронутыми
        self._record(node, lambda: parent._emit_FunctionDef(node), overwrite=False)
        self._dbg.emit_ret()

    def _emit_VarDecl(self, node):
        parent = super()
        self._record(node, lambda: parent._emit_VarDecl(node))  # overwrite=True
        from ..compiler.ast_nodes import Literal
        from .session import VarState
        if node.value and isinstance(node.value, Literal):
            self._dbg.emit_var(VarState(
                name   = node.name,
                value  = node.value.value,
                type_  = node.type_ref.base,
                offset = self._var_offsets.get(node.name, 0),
                reg    = self._var_regs.get(node.name, ""),
            ))
            
    def _emit_IfStmt(self, node):
        start = self._pos()
        BinaryTarget._emit_IfStmt(self, node)
        end  = self._pos()
        line = getattr(node, "line", 0)
        if line and end > start:
            for i in range(start, end):
                key = str(i)
                if key not in self._offset_map:  # контейнер — не перезаписываем
                    self._offset_map[key] = line

    def _emit_ExitCall(self, node):
        parent = super()
        self._record(node, lambda: parent._emit_ExitCall(node))

    def _emit_Assignment(self, node):
        parent = super()
        self._record(node, lambda: parent._emit_Assignment(node))

    def _emit_ReturnStmt(self, node):
        parent = super()
        self._record(node, lambda: parent._emit_ReturnStmt(node))

    def _emit_WhileLoop(self, node):
        parent = super()
        self._record(node, lambda: parent._emit_WhileLoop(node), overwrite=False)

    def _emit_ForLoop(self, node):
        parent = super()
        self._record(node, lambda: parent._emit_ForLoop(node), overwrite=False)

    # ─── emit ────────────────────────────────────────────────────────────────

    def emit(self, program):
        binary = super().emit(program)

        # debug print
        by_line = {}
        for offset, line in self._offset_map.items():
            by_line.setdefault(line, []).append(int(offset))
        for line, offsets in sorted(by_line.items()):
            offsets.sort()
            # print(f"  line {line}: bytes {min(offsets)}..{max(offsets)}  ({len(offsets)} bytes)")

        self._dbg.emit_hex(binary, cursor=-1, offset_map=self._offset_map)
        return binary


# ─── Entry point ──────────────────────────────────────────────────────────────

def run_server(src: str, arch: str = "x16", port: int = 8765):
    global _src_path, _arch, _session
    _src_path = os.path.abspath(src)
    _arch     = arch
    _session  = DebugSession()

    # запускаем компиляцию в фоновом потоке
    # (стартует через 1 сек чтобы сервер успел подняться)
    def _delayed():
        import time
        time.sleep(1.0)
        _run_pipeline()

    t = threading.Thread(target=_delayed, daemon=True)
    t.start()

    print(f"  BoxLang6 Debugger")
    print(f"  → http://localhost:{port}")
    print(f"  → file: {_src_path}")
    print(f"  → arch: {_arch}")
    print(f"  Ctrl+C to stop\n")

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
