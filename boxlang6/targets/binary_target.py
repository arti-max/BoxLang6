from __future__ import annotations
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from .base_target import BaseTarget, ArchLoadError
from ..compiler.ast_nodes import *


class CodeGenError(Exception):
    def __init__(self, msg: str, node: Optional[Node] = None):
        loc = f" at {node.line}:{node.col}" if node else ""
        super().__init__(f"[CodeGen]{loc} {msg}")


# ─── Relocation ───────────────────────────────────────────────────────────────

@dataclass
class Relocation:
    buf_offset: int       # байтовое смещение начала инструкции в буфере
    bit_hi:     int       # старший бит поля внутри инструкции
    bit_lo:     int       # младший бит поля
    label:      str       # имя метки
    label_type: str       # "rel8", "rel16", "abs4", "abs8", "abs16"
    insn_end:   int       # buf_offset + size_bits//8
    insn_size:  int       # размер инструкции в байтах
    
@dataclass
class VarAddr:
    """Адрес переменной в address-mode. Отличается от голого int."""
    addr: int

# ─── BitEncoder ───────────────────────────────────────────────────────────────

class BitEncoder:
    """
    Универсальный упаковщик битовых полей.
    Читает variant из arch JSON и кодирует инструкцию в байты.
    """

    def __init__(self, target: "BinaryTarget"):
        self.t = target

    def encode(self, insn, variant, args):
        v          = self.t.get_variant(insn, variant)
        size_bits  = v["size_bits"]
        size_bytes = size_bits // 8   # ← здесь есть
        buf_off    = self.t._pos()
        insn_end   = buf_off + size_bytes

        result = 0
        relocs = []

        for f in v["fields"]:
            hi, lo = self._parse_bits(f["bits"])
            source = f["source"]
            value, reloc = self._resolve_source(
                source, args, hi, lo, buf_off, insn_end, size_bytes  # ← добавить
            )
            if reloc:
                relocs.append(reloc)
            width  = hi - lo + 1
            mask   = (1 << width) - 1
            result |= (value & mask) << lo

        raw = self._pack(result, size_bits)
        return raw, relocs

    # ── source resolver ───────────────────────────────────────────────────────

    def _resolve_source(
        self,
        source:   str,
        args:     list,
        bit_hi:   int,
        bit_lo:   int,
        buf_off:  int,
        insn_end: int,
        size_bytes: int=0,
    ) -> Tuple[int, Optional[Relocation]]:

        # const:VALUE
        if source.startswith("const:"):
            raw = source[6:]
            return (int(raw, 16) if raw.startswith("0x") else int(raw), None)

        # arg:N:TYPE
        if source.startswith("arg:"):
            parts = source.split(":")   # ["arg", "N", "TYPE"]
            n     = int(parts[1])
            typ   = parts[2]
            if n >= len(args):
                raise CodeGenError(
                    f"Instruction needs arg:{n} but only {len(args)} args given"
                )
            return (self._resolve_arg(args[n], typ), None)

        # label:TYPE
        if source.startswith("label:"):
            ltype = source[6:]
            # имя метки берётся из args[0] для label-инструкций
            label = args[0] if args else ""
            r = Relocation(
                buf_offset = buf_off,
                bit_hi     = bit_hi,
                bit_lo     = bit_lo,
                label      = label,
                label_type = ltype,
                insn_end   = insn_end,
                insn_size  = size_bytes,
            )
            return (0, r)

        raise CodeGenError(f"Unknown source format: '{source}'")

    def _resolve_arg(self, arg: Any, typ: str) -> int:
        if isinstance(arg, VarAddr):
            return arg.addr

        if typ == "reg.id":
            return self.t.reg_id(str(arg))

        if typ == "reg.bits":
            return self.t.reg_bits(str(arg))

        if typ in ("var.offset", "var.addr"):
            return int(arg)

        v = int(arg) if not isinstance(arg, int) else arg

        if typ == "imm":      return v
        if typ == "imm.lo8":  return v & 0xFF
        if typ == "imm.hi8":  return (v >> 8) & 0xFF
        if typ == "imm.lo4":  return v & 0xF
        if typ == "imm.hi4":  return (v >> 4) & 0xF

        raise CodeGenError(f"Unknown arg type '{typ}'")

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_bits(s: str) -> Tuple[int, int]:
        hi, lo = s.split(":")
        return int(hi), int(lo)

    def _pack(self, value: int, size_bits: int) -> bytes:
        endian = self.t.arch.get("endian", "little")
        result = value.to_bytes(size_bits // 8, endian)
        # print(f"    _pack value=0x{value:X} size={size_bits}bit endian={endian} → {result.hex()}")
        return result


# ─── BinaryTarget ─────────────────────────────────────────────────────────────

class BinaryTarget(BaseTarget):

    def __init__(self, arch_name: str):
        super().__init__(arch_name)
        self._buf:         bytearray            = bytearray()
        self._labels:      Dict[str, int]       = {}
        self._relocs:      List[Relocation]     = []
        self._var_offsets: Dict[str, int]       = {}
        self._var_regs:    Dict[str, str]       = {}
        self._var_sizes:   Dict[str, int]       = {}   # ← новое
        self._stack_off:   int                  = 0
        self._addr_off:    int                  = 0
        self._free_regs:   List[str]            = []
        self._encoder:     BitEncoder           = BitEncoder(self)
        self._lbl_counter: int                  = 0
        self._namespace_prefix: str             = ""
        self._var_elem_sizes: Dict[str, int]    = {}

    # ── public entry ──────────────────────────────────────────────────────────

    def emit(self, program: Program) -> bytes:
        self._buf.clear()
        self._labels.clear()
        self._relocs.clear()

        for node in program.body:
            self._emit_node(node)

        self._resolve_relocs()
        return bytes(self._buf)

    # ── buffer helpers ────────────────────────────────────────────────────────

    def _pos(self) -> int:
        return len(self._buf)

    def _write(self, data: bytes, relocs: List[Relocation]):
        for r in relocs:
            self._relocs.append(r)
        self._buf.extend(data)

    # ── encode helpers ────────────────────────────────────────────────────────
    
    def _storage_mode(self) -> str:
        """
        "stack"     — переменные на стеке (x86)
        "registers" — переменные в регистрах
        всё остальное ("zeropage", "absolute", ...) → "address"
        """
        mode = self.arch.get("var_storage", "stack")
        if mode in ("stack", "registers"):
            return mode
        return "address"

    def _addr_base(self) -> int:
        """Базовый адрес для address-mode. Читается из arch JSON."""
        raw = self.arch.get("var_addr_base", "0x00")
        return int(raw, 16) if isinstance(raw, str) else int(raw)


    def _insn(self, insn: str, variant: str, args: list):
        """Закодировать инструкцию и записать в буфер."""
        raw, relocs = self._encoder.encode(insn, variant, args)
        self._write(raw, relocs)

    def _op(self, op_name: str, bindings: Dict[str, Any]):
        """
        Выполнить абстрактную операцию из секции ops.
        bindings: {"$value": 42, "$label": "foo", ...}
        """
        ops = self.arch.get("ops", {})
        op  = ops.get(op_name)
        if op is None:
            raise CodeGenError(
                f"Operation '{op_name}' not supported by arch '{self.arch_name}'"
            )
        for step in op["steps"]:
            args = self._resolve_step_args(step["args"], bindings)
            self._insn(step["insn"], step["variant"], args)

    def _resolve_step_args(self, raw_args: list, bindings: Dict[str, Any]) -> list:
        result = []
        for a in raw_args:
            if a.startswith("$"):
                val = bindings.get(a)
                if val is None:
                    raise CodeGenError(f"Unresolved binding '{a}'")
                result.append(val)
            elif a.startswith("const:"):
                raw = a[6:]
                result.append(int(raw, 16) if raw.startswith("0x") else int(raw))
            else:
                # регистр или число
                try:
                    result.append(int(a, 16) if a.startswith("0x") else int(a))
                except ValueError:
                    result.append(a)   # строка-регистр
        return result

    # ── labels ────────────────────────────────────────────────────────────────

    def _def_label(self, name: str):
        self._labels[name] = self._pos()

    def _unique_label(self, prefix: str) -> str:
        self._lbl_counter += 1
        return f"__{prefix}_{self._lbl_counter}"

    # ── relocation resolver ───────────────────────────────────────────────────

    def _resolve_relocs(self):
        origin = self.origin()
        # print(f"\n  === resolving {len(self._relocs)} relocs ===")
        for r in self._relocs:
            if r.label not in self._labels:
                raise CodeGenError(f"Undefined label '{r.label}'")
            target = self._labels[r.label]
            # print(f"  reloc: label={r.label!r} buf_off={r.buf_offset} "
                # f"insn_end={r.insn_end} target_pos={target} "
                # f"bits={r.bit_hi}:{r.bit_lo} type={r.label_type}")
            ltype  = r.label_type

            if ltype == "rel8":
                delta = target - r.insn_end
                if not (-128 <= delta <= 127):
                    raise CodeGenError(f"rel8 overflow for '{r.label}': delta={delta}")
                value = delta & 0xFF

            elif ltype == "rel16":
                delta = target - r.insn_end
                if not (-32768 <= delta <= 32767):
                    raise CodeGenError(f"rel16 overflow for '{r.label}': delta={delta}")
                value = delta & 0xFFFF

            elif ltype == "rel16.lo8":
                delta = target - r.insn_end
                if not (-32768 <= delta <= 32767):
                    raise CodeGenError(f"rel16 overflow for '{r.label}': delta={delta}")
                value = delta & 0xFF

            elif ltype == "rel16.hi8":
                delta = target - r.insn_end
                value = (delta >> 8) & 0xFF

            elif ltype == "abs4":
                value = (target + origin) & 0xF

            elif ltype == "abs8":
                value = (target + origin) & 0xFF

            elif ltype == "abs16":
                value = (target + origin) & 0xFFFF

            elif ltype == "abs16.lo8":
                value = (target + origin) & 0xFF

            elif ltype == "abs16.hi8":
                value = ((target + origin) >> 8) & 0xFF

            else:
                raise CodeGenError(f"Unknown label_type '{ltype}'")

            self._patch_bits(r.buf_offset, r.bit_hi, r.bit_lo, value, r.insn_size)

    def _patch_bits(self, buf_off: int, bit_hi: int, bit_lo: int,value: int, insn_size: int = 0):
        endian = self.arch.get("endian", "little")
        width  = bit_hi - bit_lo + 1
        mask   = (1 << width) - 1
        value  = value & mask

        if endian == "big" and insn_size > 0:
            total_bits  = insn_size * 8
            # бит N в big-endian → байт (total_bits - 1 - N) // 8
            byte_of_hi  = (total_bits - 1 - bit_hi) // 8
            byte_of_lo  = (total_bits - 1 - bit_lo) // 8
            n_bytes     = byte_of_lo - byte_of_hi + 1
            buf_start   = buf_off + byte_of_hi
            # сдвиг внутри байта — бит bit_lo попадает в позицию (bit_lo % 8)
            shift       = bit_lo % 8
        else:
            byte_of_lo  = bit_lo // 8
            byte_of_hi  = bit_hi // 8
            n_bytes     = byte_of_hi - byte_of_lo + 1
            buf_start   = buf_off + byte_of_lo
            shift       = bit_lo - byte_of_lo * 8

        chunk  = int.from_bytes(self._buf[buf_start : buf_start + n_bytes], "big")
        chunk &= ~(mask << shift)
        chunk |=  (value << shift)
        self._buf[buf_start : buf_start + n_bytes] = chunk.to_bytes(n_bytes, "big")

    # ── prologue / epilogue ───────────────────────────────────────────────────

    def _emit_prologue(self):
        for step in self.prologue_steps():
            args = self._resolve_step_args(step.get("args", []), {})
            self._insn(step["insn"], step["variant"], args)

    def _emit_epilogue(self):
        for step in self.epilogue_steps():
            args = self._resolve_step_args(step.get("args", []), {})
            self._insn(step["insn"], step["variant"], args)

    # ── variable storage ──────────────────────────────────────────────────────

    def _alloc_var(self, name: str, type_ref: TypeRef):
        arch_bits = self.arch.get("bits", 16)
        size      = self._type_size(type_ref)

        # проверяем размер одного элемента, не всего массива
        elem_ref  = TypeRef(base=type_ref.base, pointer=type_ref.pointer, array=None)
        elem_size = self._type_size(elem_ref)
        elem_bits = elem_size * 8
        if elem_bits > arch_bits:
            raise CodeGenError(
                f"Type '{type_ref.base}' is {elem_bits}-bit but arch "
                f"'{self.arch_name}' is only {arch_bits}-bit"
            )
        
        if type_ref.array:
            self._var_elem_sizes[name] = self._type_size(TypeRef(base=type_ref.base))

        mode = self._storage_mode()

        if mode == "stack":
            self._stack_off -= size
            self._var_offsets[name] = self._stack_off
            self._var_sizes[name]   = size
            alloc = self.arch["var_alloc"]
            args  = self._resolve_step_args(alloc["args"], {"$size": size})
            self._insn(alloc["insn"], alloc["variant"], args)

        elif mode == "registers":
            if not self._free_regs:
                raise CodeGenError(f"No free registers to allocate variable '{name}'")
            reg = self._free_regs.pop(0)
            self._var_regs[name]  = reg
            self._var_sizes[name] = size

        else:
            addr     = self._addr_base() + self._addr_off
            max_addr = (1 << arch_bits) - 1
            if addr > max_addr:
                raise CodeGenError(
                    f"Address space overflow allocating '{name}' "
                    f"at 0x{addr:X} (max 0x{max_addr:X})"
                )
            self._var_offsets[name] = addr
            self._var_sizes[name]   = size
            self._addr_off         += size
            alloc = self.arch.get("var_alloc")
            if alloc and alloc.get("insn") != "nop":
                args = self._resolve_step_args(
                    alloc["args"], {"$var_addr": addr, "$size": size}
                )
                self._insn(alloc["insn"], alloc["variant"], args)

    def _load_var_to_work(self, name: str, node: Node):
        mode = self._storage_mode()

        if mode == "stack":
            off  = self._var_offsets.get(name)
            if off is None:
                raise CodeGenError(f"Unknown variable '{name}'", node)
            size = self._var_sizes.get(name, 2)
            if size == 1:
                self._op("load_var8", {"$bp_offset": off})
            else:
                self._op("load_var",  {"$bp_offset": off, "$reg": self.return_reg()})

        elif mode == "registers":
            reg = self._var_regs.get(name)
            if reg is None:
                raise CodeGenError(f"Unknown variable '{name}'", node)
            if reg != self.return_reg():
                self._op("copy_reg", {"$src": reg, "$dst": self.return_reg()})

        else:
            addr = self._var_offsets.get(name)
            if addr is None:
                raise CodeGenError(f"Unknown variable '{name}'", node)
            size = self._var_sizes.get(name, 1)
            if size == 1:
                self._op("load_var8", {"$var_addr": addr})
            else:
                self._op("load_var",  {"$var_addr": addr})

    def _store_work_to_var(self, name: str, node: Node):
        mode = self._storage_mode()

        if mode == "stack":
            off  = self._var_offsets.get(name)
            if off is None:
                raise CodeGenError(f"Unknown variable '{name}'", node)
            size = self._var_sizes.get(name, 2)
            if size == 1:
                self._op("store_var8", {"$bp_offset": off})
            else:
                self._op("store_var",  {"$bp_offset": off, "$reg": self.return_reg()})

        elif mode == "registers":
            reg = self._var_regs.get(name)
            if reg is None:
                raise CodeGenError(f"Unknown variable '{name}'", node)
            if reg != self.return_reg():
                self._op("copy_reg", {"$src": self.return_reg(), "$dst": reg})

        else:
            addr = self._var_offsets.get(name)
            if addr is None:
                raise CodeGenError(f"Unknown variable '{name}'", node)
            size = self._var_sizes.get(name, 1)
            if size == 1:
                self._op("store_var8", {"$var_addr": addr})
            else:
                self._op("store_var",  {"$var_addr": addr})

    # ── node dispatcher ───────────────────────────────────────────────────────

    def _emit_node(self, node: Node):
        method  = f"_emit_{type(node).__name__}"
        emitter = getattr(self, method, None)
        if emitter is None:
            raise CodeGenError(f"No emitter for {type(node).__name__}", node)
        emitter(node)

    # ── top level ─────────────────────────────────────────────────────────────

    def _emit_Program(self, node: Program):
        for n in node.body:
            self._emit_node(n)

    def _emit_Namespace(self, node: Namespace):
        prev = self._namespace_prefix
        self._namespace_prefix = node.name
        for n in node.body:
            self._emit_node(n)
        self._namespace_prefix = prev

    def _emit_IncludeStd(self,  node): pass
    def _emit_IncludeFile(self, node): pass
    def _emit_ShelfDef(self,    node): pass
    def _emit_UseDirective(self, node): pass
    def _emit_StringLiteral(self, node): pass
    def _emit_ArrayInit(self, node):     pass

    # ── function ──────────────────────────────────────────────────────────────

    def _emit_FunctionDef(self, node: FunctionDef):
        prev_offsets = self._var_offsets.copy()
        prev_regs    = self._var_regs.copy()
        prev_sizes   = self._var_sizes.copy()
        prev_stack   = self._stack_off
        prev_addr    = self._addr_off          # ← новое
        prev_free    = self._free_regs.copy()
        prev_elem_sizes = self._var_elem_sizes.copy()

        self._var_offsets = {}
        self._var_regs    = {}
        self._var_sizes   = {}
        self._stack_off   = 0
        self._addr_off    = 0                  # ← сброс
        self._free_regs   = list(self.arch.get("var_regs", []))

        label = self._scoped_label(node)
        self._def_label(label)
        self._emit_prologue()

        mode        = self._storage_mode()
        arg_regs    = self.arg_regs()
        arg_passing = self.arch["calling_convention"].get("arg_passing", "registers")
        base_off    = self.arch["calling_convention"].get("arg_base_offset", 4)

        for i, param in enumerate(node.params):
            if arg_passing == "stack":
                stack_align = self.arch["calling_convention"].get("stack_align", 2)
                off = base_off + i * stack_align
                self._var_offsets[param.name] = off
                self._var_sizes[param.name]   = self._type_size(param.type_ref)
            else:
                if mode == "stack":
                    self._alloc_var(param.name, param.type_ref)
                    if i < len(arg_regs):
                        src = arg_regs[i]
                        if src != self.return_reg():
                            self._op("copy_reg", {"$src": src, "$dst": self.return_reg()})
                        self._store_work_to_var(param.name, param)
                elif mode == "registers":
                    if i < len(arg_regs):
                        self._var_regs[param.name] = arg_regs[i]
                else:
                    # address mode: аллоцируем и копируем из arg reg
                    self._alloc_var(param.name, param.type_ref)
                    if i < len(arg_regs):
                        src = arg_regs[i]
                        if src != self.return_reg():
                            self._op("copy_reg", {"$src": src, "$dst": self.return_reg()})
                        self._store_work_to_var(param.name, param)

        has_explicit_ret = False
        for stmt in node.body:
            self._emit_node(stmt)
            if isinstance(stmt, (ReturnStmt, ExitCall)):
                has_explicit_ret = True

        if not has_explicit_ret:
            self._emit_epilogue()

        self._var_offsets = prev_offsets
        self._var_regs    = prev_regs
        self._var_sizes   = prev_sizes
        self._stack_off   = prev_stack
        self._addr_off    = prev_addr           # ← восстановить
        self._free_regs   = prev_free
        self._var_elem_sizes = prev_elem_sizes

    def _scoped_label(self, node: FunctionDef) -> str:
        if self._namespace_prefix:
            return f"{self._namespace_prefix}__{node.name}"
        return node.name
    
    def _emit_array_init(self, node: VarDecl):
        base_addr = self._var_offsets[node.name]
        elem_size = self._type_size(TypeRef(base=node.type_ref.base))
        mode      = self._storage_mode()

        if isinstance(node.value, StringLiteral):
            elements = [Literal(value=ord(c)) for c in node.value.value]
        elif isinstance(node.value, ArrayInit):
            elements = node.value.elements
        else:
            raise CodeGenError("Array initializer must be string or {}", node)

        for i, elem in enumerate(elements):
            self._emit_expr_to_work(elem)
            offset = i * elem_size

            if mode == "stack":
                elem_off = base_addr - offset   # стек растёт вниз
                if elem_size == 1:
                    self._op("store_var8", {"$bp_offset": elem_off})
                else:
                    self._op("store_var", {"$bp_offset": elem_off, "$reg": self.return_reg()})
            else:
                elem_addr = base_addr + offset
                if elem_size == 1:
                    self._op("store_var8", {"$var_addr": elem_addr})
                else:
                    self._op("store_var", {"$var_addr": elem_addr})

    # ── statements ────────────────────────────────────────────────────────────

    def _emit_VarDecl(self, node: VarDecl):
        self._alloc_var(node.name, node.type_ref)
        if node.value is None:
            return

        if node.type_ref.array is not None:
            self._emit_array_init(node)
        else:
            self._emit_expr_to_work(node.value)
            self._store_work_to_var(node.name, node)

    def _emit_Assignment(self, node):
        if isinstance(node.target, Identifier):
            self._emit_expr_to_work(node.value)
            self._store_work_to_var(node.target.name, node)

        elif isinstance(node.target, UnaryOp) and node.target.op == "*":
            ops        = self.arch.get("ops", {})
            ptr_steps  = ops.get("ptr_store", {}).get("steps", [])
            ptr_uses_stack = any(s.get("insn") == "pla" for s in ptr_steps)

            if ptr_uses_stack:
                # 6502: push addr, value в A, ptr_store делает PLA
                self._emit_expr_to_work(node.target.operand)
                self._op("push_reg", {"$reg": self.return_reg()})
                self._emit_expr_to_work(node.value)
                self._op("ptr_store", {})

            elif self.arch.get("tmp_regs"):
                # x86: push value, вычислить addr, pop value в tmp, ptr_store
                self._emit_expr_to_work(node.value)
                self._op("push_reg", {"$reg": self.return_reg()})
                self._emit_expr_to_work(node.target.operand)
                val_reg = self._pick_tmp_reg()
                self._op("pop_reg",  {"$reg": val_reg})
                self._op("ptr_store", {})

            else:
                raise CodeGenError(
                    "ptr_store: arch must define either 'pla' in ptr_store steps "
                    "or 'tmp_regs'", node
                )

        elif isinstance(node.target, IndexAccess):
            self._emit_index_store(node)

        else:
            raise CodeGenError(
                f"Unsupported assignment target: {type(node.target).__name__}", node
            )

    def _emit_ReturnStmt(self, node: ReturnStmt):
        if node.value:
            self._emit_expr_to_work(node.value)
        self._emit_epilogue()

    def _emit_ExitCall(self, node: ExitCall):
        if node.code is None:
            code = 0
        elif isinstance(node.code, Literal):
            code = node.code.value
        else:
            self._emit_expr_to_work(node.code)
            self._op("syscall_exit", {
                "$code":       self.return_reg(),
                "$halt_label": "__halt"
            })
            self._emit_halt_trampoline()
            return
        self._op("syscall_exit", {"$code": code, "$halt_label": "__halt"})
        self._emit_halt_trampoline()
        
    def _emit_halt_trampoline(self):
        """
        Если arch использует $halt_label в syscall_exit —
        генерируем __halt: jmp __halt один раз.
        Для x86 (INT 21h) $halt_label не используется → ничего не делаем.
        """
        ops      = self.arch.get("ops", {})
        exit_op  = ops.get("syscall_exit", {})
        uses_halt = any(
            "$halt_label" in step.get("args", [])
            for step in exit_op.get("steps", [])
        )
        if uses_halt and "__halt" not in self._labels:
            self._def_label("__halt")
            self._op("jump", {"$label": "__halt"})

    def _emit_FunctionCall(self, node: FunctionCall):
        cc          = self.arch["calling_convention"]
        arg_passing = cc.get("arg_passing", "registers")
        arg_regs    = self.arg_regs()

        if len(node.args) > len(arg_regs) and arg_passing == "registers":
            raise CodeGenError(f"Too many arguments (max {len(arg_regs)})", node)

        if arg_passing == "registers":
            for arg in node.args:
                self._emit_expr_to_work(arg)
                self._op("push_reg", {"$reg": self.return_reg()})
            for i in reversed(range(len(node.args))):
                self._op("pop_reg", {"$reg": arg_regs[i]})

        elif arg_passing == "stack":
            for arg in reversed(node.args):
                self._emit_expr_to_work(arg)
                self._op("push_reg", {"$reg": self.return_reg()})

        if isinstance(node.target, Identifier):
            label = node.target.name
        elif isinstance(node.target, ScopedIdentifier):
            label = f"{node.target.namespace}__{node.target.name}"
        else:
            raise CodeGenError("Unsupported call target", node)

        self._op("call", {"$label": label})

        if arg_passing == "stack" and node.args:
            stack_align = cc.get("stack_align", 2)
            self._op("stack_free", {"$size": len(node.args) * stack_align})

    def _emit_WhileLoop(self, node: WhileLoop):
        lbl_start = self._unique_label("while_start")
        lbl_end   = self._unique_label("while_end")

        self._def_label(lbl_start)
        self._emit_cond_jump(node.condition, lbl_end, invert=True)

        for stmt in node.body:
            self._emit_node(stmt)

        self._op("jump", {"$label": lbl_start})
        self._def_label(lbl_end)

    def _emit_ForLoop(self, node: ForLoop):
        lbl_start = self._unique_label("for_start")
        lbl_end   = self._unique_label("for_end")

        if node.init:
            self._emit_node(node.init)

        self._def_label(lbl_start)

        if node.condition:
            self._emit_cond_jump(node.condition, lbl_end, invert=True)

        for stmt in node.body:
            self._emit_node(stmt)

        if node.step:
            self._emit_node(node.step)

        self._op("jump", {"$label": lbl_start})
        self._def_label(lbl_end)

    def _emit_AsmInsert(self, node: AsmInsert):
        if "." in node.insn:
            insn_name, variant = node.insn.split(".", 1)
        else:
            insn_name = node.insn
            variant   = None

        # определяем dst_var — переменная-назначение (первый аргумент)
        dst_var = None
        dst_reg = None
        if node.args:
            first = node.args[0]
            if isinstance(first, Literal) and isinstance(first.value, str):
                dst_reg = first.value
            elif isinstance(first, Identifier) and \
                (first.name in self._var_offsets or first.name in self._var_regs) and \
                len(node.args) > 1:
                # переменная на первом месте + есть src → это запись в переменную
                dst_var = first.name

        # если dst_var — сначала резолвим src аргументы, потом store
        if dst_var is not None:
            # резолвим второй аргумент в work регистр
            src = node.args[1]
            if isinstance(src, Literal) and isinstance(src.value, str):
                # регистр → copy_reg в work если не совпадает
                src_reg = src.value
                if src_reg != self.return_reg():
                    self._op("copy_reg", {"$src": src_reg, "$dst": self.return_reg()})
            elif isinstance(src, Literal):
                self._op("load_imm", {"$value": src.value})
            elif isinstance(src, Identifier):
                if src.name in self._var_offsets or src.name in self._var_regs:
                    self._load_var_to_work(src.name, node)
                elif self.is_reg(src.name):
                    if src.name != self.return_reg():
                        self._op("copy_reg", {"$src": src.name, "$dst": self.return_reg()})
                else:
                    raise CodeGenError(f"Unknown src '{src.name}' in asm", node)
            else:
                self._emit_expr_to_work(src)

            self._store_work_to_var(dst_var, node)
            return

        # обычный путь — читаем все аргументы
        resolved = []
        for i, arg in enumerate(node.args):
            if isinstance(arg, Literal) and isinstance(arg.value, str):
                resolved.append(arg.value)

            elif isinstance(arg, Literal):
                resolved.append(arg.value)

            elif isinstance(arg, Identifier):
                if arg.name in self._var_offsets or arg.name in self._var_regs:
                    mode = self._storage_mode()
                    if mode == "stack":
                        self._load_var_to_work(arg.name, node)
                        work = self.return_reg()
                        if dst_reg and dst_reg != work and i > 0:
                            self._op("copy_reg", {"$src": work, "$dst": dst_reg})
                            resolved.append(dst_reg)
                        else:
                            resolved.append(work)
                    else:
                        resolved.append(VarAddr(self._var_offsets[arg.name]))

                elif self.is_reg(arg.name):
                    resolved.append(arg.name)

                else:
                    raise CodeGenError(f"Unknown variable or register '{arg.name}' in asm", node)

            elif isinstance(arg, LabelRef):
                resolved.append(arg.name)

            else:
                self._emit_expr_to_work(arg)
                resolved.append(self.return_reg())

        if variant is None:
            arg_types = [self._classify_arg(a) for a in resolved]
            variant   = self._match_variant(insn_name, arg_types, node)

        self._insn(insn_name, variant, resolved)

    def _classify_arg(self, arg) -> str:
        if isinstance(arg, str) and self.is_reg(arg):
            return f"reg{self.reg_bits(arg)}"
        if isinstance(arg, VarAddr):
            return "addr"
        if isinstance(arg, int):
            if arg <= 0xFF:   return "imm8"
            if arg <= 0xFFFF: return "imm16"
        return "imm"


    def _match_variant(self, insn: str, arg_types: list, node: Node) -> str:
        insn_def = self._insns.get(insn)
        if insn_def is None:
            raise CodeGenError(f"Unknown instruction '{insn}'", node)
        variants = insn_def.get("variants", {})

        # 1. нет аргументов → берём "plain" или единственный вариант
        if not arg_types:
            if "plain" in variants:
                return "plain"
            if len(variants) == 1:
                return next(iter(variants))
            raise CodeGenError(
                f"Instruction '{insn}' has no args but no 'plain' variant. "
                f"Available: {list(variants.keys())}",
                node
            )

        # 2. прямое совпадение по ключу
        key = "_".join(arg_types)
        if key in variants:
            return key

        # 3. совпадение по полю "operands"
        for vname, vdata in variants.items():
            if vdata.get("operands") == arg_types:
                return vname

        # 4. нечёткий матчинг: imm8/imm16/imm взаимозаменяемы, reg* совместимы
        def types_compatible(expected: list, actual: list) -> bool:
            if len(expected) != len(actual):
                return False
            IMM = {"imm8", "imm16", "imm"}
            for e, a in zip(expected, actual):
                if e == a:
                    continue
                if e in IMM and a in IMM:
                    continue
                if e.startswith("reg") and a.startswith("reg"):
                    continue
                return False
            return True

        for vname, vdata in variants.items():
            operands = vdata.get("operands")
            if operands is not None and types_compatible(operands, arg_types):
                return vname

        # 5. промоут imm8↔imm16 в ключе
        PROMOTE = {"imm8": "imm16", "imm16": "imm8", "imm": "imm8"}
        promoted_key = "_".join(PROMOTE.get(t, t) for t in arg_types)
        if promoted_key in variants:
            return promoted_key

        # 6. единственный вариант — берём его (инструкции с одним режимом)
        if len(variants) == 1:
            return next(iter(variants))

        raise CodeGenError(
            f"No variant of '{insn}' matches {arg_types}. "
            f"Available: {list(variants.keys())}",
            node
        )
        
    def _emit_IfStmt(self, node: IfStmt):
        end_label  = self._unique_label("if_end")
        next_label = self._unique_label("if_next")

        # основная ветка
        self._emit_cond_jump(node.condition, next_label, invert=True)
        for stmt in node.body:
            self._emit_node(stmt)
        self._op("jump", {"$label": end_label})
        self._def_label(next_label)

        # else if ветки
        for ei in node.else_if:
            next_ei = self._unique_label("elif_next")
            self._emit_cond_jump(ei.condition, next_ei, invert=True)
            for stmt in ei.body:
                self._emit_node(stmt)
            self._op("jump", {"$label": end_label})
            self._def_label(next_ei)

        # else ветка
        if node.else_body:
            for stmt in node.else_body:
                self._emit_node(stmt)

        self._def_label(end_label)

    # ── expressions → work register ───────────────────────────────────────────

    def _emit_expr_to_work(self, node: Node):
        """Вычислить выражение. Результат в рабочем регистре (return reg)."""

        if isinstance(node, Literal):
            self._op("load_imm", {"$value": node.value})

        elif isinstance(node, Identifier):
            self._load_var_to_work(node.name, node)

        elif isinstance(node, BinaryOp):
            if node.op in ("<", ">", "==", "!=", "<=", ">="):
                # булевы выражения — не вычисляем как число,
                # только через _emit_cond_jump
                raise CodeGenError(
                    "Boolean expression used as value — use in while/for/if only",
                    node
                )

            # левый → work, push; правый → work, pop left → op
            self._emit_expr_to_work(node.left)
            self._op("push_reg", {"$reg": self.return_reg()})
            self._emit_expr_to_work(node.right)

            # правый сейчас в work, кладём во второй регистр
            right_tmp = self._pick_tmp_reg()
            self._op("copy_reg", {"$src": self.return_reg(), "$dst": right_tmp})

            # восстанавливаем левый
            self._op("pop_reg", {"$reg": self.return_reg()})

            op_map = {
                "+": "add",
                "-": "sub",
                "*": "mul",
                "/": "div",
            }
            op_name = op_map.get(node.op)
            if op_name is None:
                raise CodeGenError(f"Unknown operator '{node.op}'", node)

            self._op(op_name, {"$right": right_tmp})

        elif isinstance(node, UnaryOp):
            if node.op == "-":
                self._emit_expr_to_work(node.operand)
                self._op("neg", {})

            elif node.op == "&":
                name = node.operand.name
                if name in self._var_offsets:
                    off = self._var_offsets[name]
                    self._op("load_addr", {"$offset": off})
                else:
                    self._op("load_label_addr", {"$label": name})

            elif node.op == "*":
                self._emit_expr_to_work(node.operand)
                self._op("ptr_load", {"$ptr_reg": self.return_reg()})

            else:
                raise CodeGenError(f"Unary op '{node.op}' not supported", node)

        elif isinstance(node, FunctionCall):
            # вызов как выражение — результат уже в return reg
            self._emit_FunctionCall(node)
            
        elif isinstance(node, IndexAccess):
            if not isinstance(node.target, Identifier):
                raise CodeGenError("Index access: array must be identifier", node)
            arr_name  = node.target.name
            elem_size = self._var_elem_sizes.get(arr_name, 1)
            self._emit_array_addr(arr_name, node.index, elem_size)
            self._op("ptr_load", {"$ptr_reg": self.return_reg()})

        elif isinstance(node, FieldAccess):
            if node.field_name == "length":
                if not isinstance(node.target, Identifier):
                    raise CodeGenError(".length: must be identifier", node)
                arr_name = node.target.name
                size      = self._var_sizes.get(arr_name, 0)
                elem_size = self._var_elem_sizes.get(arr_name, 1)
                length    = size // elem_size
                self._op("load_imm", {"$value": length})
            else:
                raise CodeGenError(f"Field '{node.field_name}' not supported yet", node)

        else:
            raise CodeGenError(
                f"Cannot emit expr {type(node).__name__} to work register", node
            )
            
    def _emit_index_store(self, node: Assignment):
        # target = arr[i], value = expr
        arr   = node.target.target
        index = node.target.index
        if not isinstance(arr, Identifier):
            raise CodeGenError("Index assignment: array must be identifier", node)

        base_addr = self._var_offsets.get(arr.name)
        if base_addr is None:
            raise CodeGenError(f"Unknown array '{arr.name}'", node)

        # elem_size из _var_sizes / type_ref не хранится напрямую — считаем
        # через полный размер / длину массива. Лучше хранить _var_elem_sizes
        elem_size = self._var_elem_sizes.get(arr.name, 1)

        ops      = self.arch.get("ops", {})
        ptr_steps = ops.get("ptr_store", {}).get("steps", [])
        ptr_uses_stack = any(s.get("insn") == "pla" for s in ptr_steps)

        if ptr_uses_stack:
            # 6502: вычислить addr = base + i*elem_size → push, value → A
            self._emit_array_addr(arr.name, index, elem_size)
            self._op("push_reg", {"$reg": self.return_reg()})
            self._emit_expr_to_work(node.value)
            self._op("ptr_store", {})
        else:
            # x86: push value, вычислить addr, pop value в tmp
            self._emit_expr_to_work(node.value)
            self._op("push_reg", {"$reg": self.return_reg()})
            self._emit_array_addr(arr.name, index, elem_size)
            val_reg = self._pick_tmp_reg()
            self._op("pop_reg", {"$reg": val_reg})
            self._op("ptr_store", {})
            
    def _emit_array_addr(self, arr_name: str, index: Node, elem_size: int):
        base_addr = self._var_offsets[arr_name]
        mode      = self._storage_mode()

        # загружаем базовый адрес для ВСЕХ режимов
        if mode == "stack":
            self._op("load_addr", {"$offset": base_addr})
        else:  # address / zeropage
            self._op("load_addr", {"$offset": base_addr})

        # при index == 0 просто возвращаем базовый адрес
        if isinstance(index, Literal) and index.value == 0:
            return

        self._op("push_reg", {"$reg": self.return_reg()})
        self._emit_expr_to_work(index)

        if elem_size > 1:
            self._op("push_reg", {"$reg": self.return_reg()})
            self._op("load_imm",  {"$value": elem_size})
            right_tmp = self._pick_tmp_reg()
            self._op("copy_reg", {"$src": self.return_reg(), "$dst": right_tmp})
            self._op("pop_reg",  {"$reg": self.return_reg()})
            self._op("mul",      {"$right": right_tmp})

        right_tmp = self._pick_tmp_reg()
        self._op("copy_reg", {"$src": self.return_reg(), "$dst": right_tmp})
        self._op("pop_reg",  {"$reg": self.return_reg()})
        self._op("add",      {"$right": right_tmp})  # всегда ADD, не SUB

    # ── conditional jumps ─────────────────────────────────────────────────────

    # таблица: op → (прямой прыжок, инвертированный)
    _JMP_TABLE: Dict[str, Tuple[str, str]] = {
        "==": ("jump_if_zero",       "jump_if_not_zero"  ),
        "!=": ("jump_if_not_zero",   "jump_if_zero"      ),
        "<":  ("jump_if_less",       "jump_if_greater_eq"),
        ">":  ("jump_if_greater",    "jump_if_less_eq"   ),
        "<=": ("jump_if_less_eq",    "jump_if_greater"   ),
        ">=": ("jump_if_greater_eq", "jump_if_less"      ),
    }

    def _emit_cond_jump(self, cond: Node, label: str, invert: bool = False):
        """
        Генерирует сравнение + условный прыжок.
        invert=True — прыгать если условие ЛОЖНО (для while/for).
        """
        if isinstance(cond, BinaryOp) and cond.op in self._JMP_TABLE:
            # левый → work, push; правый → work, tmp; pop left; cmp
            self._emit_expr_to_work(cond.left)
            self._op("push_reg", {"$reg": self.return_reg()})
            self._emit_expr_to_work(cond.right)

            right_tmp = self._pick_tmp_reg()
            self._op("copy_reg",
                     {"$src": self.return_reg(), "$dst": right_tmp})
            self._op("pop_reg", {"$reg": self.return_reg()})
            self._op("cmp", {"$right": right_tmp})

            pair    = self._JMP_TABLE[cond.op]
            op_name = pair[1] if invert else pair[0]
        else:
            # нет явного оператора — cmp work, 0
            self._emit_expr_to_work(cond)
            self._op("cmp", {"$right": 0})
            op_name = "jump_if_zero" if invert else "jump_if_not_zero"

        self._op(op_name, {"$label": label})

    # ── helpers ───────────────────────────────────────────────────────────────

    def _pick_tmp_reg(self) -> str:
        work = self.return_reg()
        candidates = self.arch.get("tmp_regs", [])
        for r in candidates:
            if r != work:
                return r
        raise CodeGenError("Cannot find tmp register for expression")

    def _type_size(self, type_ref: TypeRef) -> int:
        sizes = {
            "bit":   1,
            "bit2":  1,
            "bit4":  1,
            "char":  1,
            "num16": 2,
            "num32": 4,
        }
        base = sizes.get(type_ref.base, 2)
        if type_ref.array:
            return base * type_ref.array
        return base


# ─── Test ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import traceback, sys, os
    sys.path.insert(0, os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    ))
    from boxlang6.compiler.lexer    import Lexer
    from boxlang6.compiler.parser   import Parser
    from boxlang6.compiler.semantic import SemanticAnalyzer
    from boxlang6.targets.binary_target import BinaryTarget

    def ok(label: str, extra: str = ""):
        print(f"  [OK] {label}" + (f"  →  {extra}" if extra else ""))

    def fail(label: str, e: Exception):
        print(f"  [FAIL] {label}: {e}")
        traceback.print_exc()

    def compile_src(src: str, arch: str = "x16") -> bytes:
        tokens  = Lexer(src).tokenize()
        program = Parser(tokens).parse()
        SemanticAnalyzer().analyze(program)
        return BinaryTarget(arch).emit(program)

    print("=== binary_target.py self-test ===\n")

    # exit[0]
    try:
        b = compile_src("box _start[] : num16 ( exit[0]; )")
        assert len(b) > 0
        ok("exit[0]", f"{len(b)} bytes: {b.hex()}")
    except Exception as e:
        fail("exit[0]", e)

    # ret 42
    try:
        b = compile_src("box _start[] : num16 ( ret 42; )")
        ok("ret 42", b.hex())
    except Exception as e:
        fail("ret 42", e)

    # VarDecl + Assignment
    try:
        b = compile_src("""
        box _start[] : num16 (
            num16 x: 5;
            x: x + 1;
            exit[0];
        )
        """)
        ok("VarDecl + Assignment + exit", f"{len(b)} bytes")
    except Exception as e:
        fail("VarDecl + Assignment", e)

    # while loop
    try:
        b = compile_src("""
        box _start[] : num16 (
            num16 i: 0;
            while [i < 10] (
                i: i + 1;
            )
            exit[0];
        )
        """)
        ok("while loop", f"{len(b)} bytes")
    except Exception as e:
        fail("while loop", e)

    # for loop
    try:
        b = compile_src("""
        box _start[] : num16 (
            for [num16 i: 0; i < 5; i + 1] (
                num16 x: i;
            )
            exit[0];
        )
        """)
        ok("for loop", f"{len(b)} bytes")
    except Exception as e:
        fail("for loop", e)

    # function call
    try:
        b = compile_src("""
        box add[num16 a, num16 b] : num16 (
            ret a + b;
        )
        box _start[] : num16 (
            open add[1, 2];
            exit[0];
        )
        """)
        ok("function call", f"{len(b)} bytes")
    except Exception as e:
        fail("function call", e)

    # namespace scoped call
    try:
        b = compile_src("""
        namespace Math (
            box square[num16 n] : num16 (
                ret n * n;
            )
        )
        box _start[] : num16 (
            open Math::square[4];
            exit[0];
        )
        """)
        ok("namespace scoped call", f"{len(b)} bytes")
    except Exception as e:
        fail("namespace scoped call", e)

    print("\n=== done ===")
