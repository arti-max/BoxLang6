from __future__ import annotations
import os, re
from typing import Dict, Optional, Tuple


class PreprocessorError(Exception):
    def __init__(self, msg: str, file: str = "", line: int = 0):
        loc = f"{file}:{line}: " if file else ""
        super().__init__(f"[Preprocessor] {loc}{msg}")


class Preprocessor:
    def __init__(self, std_dir: str = ""):
        self.std_dir  = std_dir
        self.defines: Dict[str, Optional[str]] = {}
        self._included: set = set()   # защита от цикличных include

    # ── entry ─────────────────────────────────────────────────────────────────

    def process(self, src: str, current_file: str = "") -> Tuple[str, Dict[str, Optional[str]]]:
        """
        Возвращает (обработанный_текст, словарь_defines).
        """
        result = self._process_text(src, current_file)
        return result, self.defines

    # ── текстовая обработка ───────────────────────────────────────────────────

    def _process_text(self, src: str, current_file: str) -> str:
        lines  = src.splitlines()
        output = []
        i      = 0

        while i < len(lines):
            line    = lines[i]
            stripped = line.strip()

            # $define NAME [value]
            if stripped.startswith("$define "):
                parts = stripped[8:].split(None, 1)
                name  = parts[0]
                value = parts[1] if len(parts) > 1 else None
                self.defines[name] = value
                # директива не попадает в output
                i += 1

            # $include <lib> или $include "file"
            elif stripped.startswith("$include"):
                included_text = self._handle_include(stripped, current_file, i + 1)
                output.append(included_text)
                i += 1

            # $ifdef / $ifndef
            elif stripped.startswith("$ifdef ") or stripped.startswith("$ifndef "):
                block_lines, consumed = self._collect_ifdef_block(lines, i)
                name     = stripped.split()[1]
                inverted = stripped.startswith("$ifndef")
                defined  = name in self.defines
                condition = defined if not inverted else not defined

                chosen_lines = block_lines["then"] if condition else block_lines["else"]
                # рекурсивно обрабатываем выбранный блок
                inner = self._process_text(
                    "\n".join(chosen_lines), current_file
                )
                output.append(inner)
                i += consumed   # пропускаем весь блок включая $endif

            # $use — оставляем, lexer/codegen читает
            elif stripped.startswith("$use "):
                output.append(line)
                i += 1

            # обычная строка — подставляем $define константы
            else:
                output.append(self._substitute_defines(line))
                i += 1

        return "\n".join(output)

    # ── $include ──────────────────────────────────────────────────────────────

    def _handle_include(self, directive: str, current_file: str, lineno: int) -> str:
        # $include <name>
        m_std = re.match(r'\$include\s+<([^>]+)>', directive)
        # $include "file"
        m_file = re.match(r'\$include\s+"([^"]+)"', directive)

        if m_std:
            name = m_std.group(1)
            path = os.path.join(self.std_dir, name + ".box")
            if not os.path.exists(path):
                raise PreprocessorError(
                    f"Standard library '{name}' not found (looked in '{path}')",
                    current_file, lineno
                )
        elif m_file:
            filename = m_file.group(1)
            base     = os.path.dirname(current_file) if current_file else "."
            path     = os.path.normpath(os.path.join(base, filename))
            if not os.path.exists(path):
                raise PreprocessorError(
                    f"File '{filename}' not found (looked in '{path}')",
                    current_file, lineno
                )
        else:
            raise PreprocessorError(
                f"Invalid $include syntax: '{directive}'",
                current_file, lineno
            )

        abs_path = os.path.abspath(path)
        if abs_path in self._included:
            return ""   # уже включён — пропускаем

        self._included.add(abs_path)

        with open(abs_path, encoding="utf-8") as f:
            content = f.read()

        # рекурсивно обрабатываем включённый файл
        return self._process_text(content, abs_path)

    # ── $ifdef блок ───────────────────────────────────────────────────────────

    def _collect_ifdef_block(
        self, lines: list, start: int
    ) -> Tuple[Dict[str, list], int]:
        """
        Собирает then/else ветки и возвращает
        ({"then": [...], "else": [...]}, количество_потреблённых_строк).
        """
        then_lines = []
        else_lines = []
        in_else    = False
        depth      = 0   # вложенные $ifdef
        i          = start + 1
        consumed   = 1

        while i < len(lines):
            s = lines[i].strip()
            consumed += 1

            if s.startswith("$ifdef ") or s.startswith("$ifndef "):
                depth += 1
                (else_lines if in_else else then_lines).append(lines[i])

            elif s == "$endif":
                if depth == 0:
                    break   # наш $endif
                depth -= 1
                (else_lines if in_else else then_lines).append(lines[i])

            elif s == "$else" and depth == 0:
                in_else = True

            else:
                (else_lines if in_else else then_lines).append(lines[i])

            i += 1

        return {"then": then_lines, "else": else_lines}, consumed

    # ── $define substitution ──────────────────────────────────────────────────

    def _substitute_defines(self, line: str) -> str:
        """Заменяет вхождения define-констант в строке."""
        for name, value in self.defines.items():
            if value is not None:
                # заменяем только целые слова
                line = re.sub(rf'\b{re.escape(name)}\b', value, line)
        return line
