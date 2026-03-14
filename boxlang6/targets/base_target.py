import json
import os
from typing import Any, Dict, Optional
from ..compiler.ast_nodes import *

try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


class ArchLoadError(Exception):
    pass


class BaseTarget:
    """
    Базовый класс таргета. Загружает и валидирует arch JSON,
    предоставляет хелперы для работы с регистрами и инструкциями.
    Конкретные таргеты наследуются и реализуют emit().
    """

    ARCH_DIR = os.path.join(os.path.dirname(__file__), "..", "arch")

    def __init__(self, arch_name: str):
        self.arch_name = arch_name
        self.arch: Dict[str, Any] = self._load_arch(arch_name)
        self._regs  = self.arch["registers"]
        self._insns = self.arch["instructions"]
        self._cc    = self.arch["calling_convention"]

    # ── загрузка и валидация ──────────────────────────────────────────────────

    def _load_arch(self, name: str) -> Dict[str, Any]:
        path = os.path.join(self.ARCH_DIR, f"{name}.json")
        if not os.path.exists(path):
            raise ArchLoadError(f"Arch config not found: {path}")

        with open(path, encoding="utf-8") as f:
            config = json.load(f)

        self._validate(config)
        return config

    def _validate(self, config: Dict[str, Any]):
        if not HAS_JSONSCHEMA:
            return  # без jsonschema просто пропускаем

        schema_path = os.path.join(self.ARCH_DIR, "schema.json")
        if not os.path.exists(schema_path):
            return

        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)

        try:
            import jsonschema
            jsonschema.validate(config, schema)
        except jsonschema.ValidationError as e:
            raise ArchLoadError(f"Invalid arch config '{self.arch_name}': {e.message}")

    # ── регистры ──────────────────────────────────────────────────────────────

    def reg_id(self, name: str) -> int:
        """Номер регистра для ModRM / opcode+reg энкодинга."""
        r = self._regs.get(name)
        if r is None:
            raise ArchLoadError(f"Unknown register '{name}' in arch '{self.arch_name}'")
        return r["id"]

    def reg_bits(self, name: str) -> int:
        """Разрядность регистра: 8 или 16."""
        return self._regs[name]["bits"]

    def reg_half(self, name: str) -> Optional[str]:
        """'low' / 'high' / None для полных регистров."""
        return self._regs.get(name, {}).get("half")

    def is_reg(self, name: str) -> bool:
        return name in self._regs

    def return_reg(self) -> str:
        return self._cc["return"]

    def arg_regs(self):
        return self._cc["args"]

    # ── инструкции ────────────────────────────────────────────────────────────

    def get_variant(self, insn: str, variant: str) -> Dict[str, Any]:
        """
        Получить вариант инструкции из JSON.
        Пример: get_variant("mov", "reg16_imm16")
        """
        insn_def = self._insns.get(insn)
        if insn_def is None:
            raise ArchLoadError(f"Unknown instruction '{insn}'")
        variants = insn_def.get("variants", {})
        v = variants.get(variant)
        if v is None:
            raise ArchLoadError(
                f"Unknown variant '{variant}' for instruction '{insn}'. "
                f"Available: {list(variants.keys())}"
            )
        return v

    def pick_variant(self, insn: str, operand_types: list) -> Dict[str, Any]:
        """
        Автоматически выбрать подходящий вариант инструкции
        по типам операндов. Например ['reg16', 'imm16'].
        """
        insn_def = self._insns.get(insn)
        if insn_def is None:
            raise ArchLoadError(f"Unknown instruction '{insn}'")

        key = "_".join(operand_types)
        v   = insn_def.get("variants", {}).get(key)
        if v is None:
            raise ArchLoadError(
                f"No variant for '{insn}' with operands {operand_types}. "
                f"Available: {list(insn_def['variants'].keys())}"
            )
        return v

    # ── syscalls ──────────────────────────────────────────────────────────────

    def get_syscall(self, name: str) -> Dict[str, Any]:
        sc = self.arch.get("syscalls", {}).get(name)
        if sc is None:
            raise ArchLoadError(f"Unknown syscall '{name}'")
        return sc

    # ── prologue / epilogue ───────────────────────────────────────────────────

    def prologue_steps(self):
        return self.arch.get("prologue", [])

    def epilogue_steps(self):
        return self.arch.get("epilogue", [])

    # ── output ────────────────────────────────────────────────────────────────

    def origin(self) -> int:
        return int(self.arch["output"]["origin"], 16)

    def output_format(self) -> str:
        return self.arch["output"]["format"]

    def entry_point(self) -> str:
        return self.arch["output"]["entry"]

    def endian(self) -> str:
        return self.arch["endian"]

    def bits(self) -> int:
        return self.arch["bits"]

    # ── интерфейс для наследников ─────────────────────────────────────────────

    def emit(self, program: Program) -> bytes:
        """
        Главный метод — принимает AST, возвращает бинарник.
        Реализуется в конкретном таргете.
        """
        raise NotImplementedError
