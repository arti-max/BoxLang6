#!/usr/bin/env python3
"""
BoxLang6 Compiler — точка входа.
Использование:
    python -m boxlang6 input.box
    python -m boxlang6 input.box -o output.com --arch x16 --debug
"""
import argparse
import os
import sys

from .compiler.lexer      import Lexer
from .compiler.parser     import Parser
from .compiler.semantic   import SemanticAnalyzer
from .compiler.optimizer  import Optimizer
from .targets.binary_target import BinaryTarget, CodeGenError
from .targets.base_target   import ArchLoadError


# ─── ANSI цвета (работают в Windows 10+ и любом unix терминале) ──────────────

def _supports_color() -> bool:
    return sys.stdout.isatty() and os.name != "nt" or os.environ.get("TERM")

RED    = "\033[31m" if _supports_color() else ""
YELLOW = "\033[33m" if _supports_color() else ""
CYAN   = "\033[36m" if _supports_color() else ""
BOLD   = "\033[1m"  if _supports_color() else ""
RESET  = "\033[0m"  if _supports_color() else ""


# ─── Error reporting ──────────────────────────────────────────────────────────

def _report(kind: str, color: str, msg: str, src: str = "", line: int = 0, col: int = 0):
    header = f"{color}{BOLD}{kind}:{RESET} {msg}"
    print(header, file=sys.stderr)
    if src and line:
        lines = src.splitlines()
        if 0 < line <= len(lines):
            print(f"  {CYAN}→ line {line}:{col}{RESET}  {lines[line - 1]}", file=sys.stderr)
            if col > 0:
                print(f"    {' ' * (col - 1)}^", file=sys.stderr)


def _error(msg: str, src: str = "", line: int = 0, col: int = 0) -> None:
    _report("error", RED, msg, src, line, col)
    sys.exit(1)


def _warn(msg: str) -> None:
    _report("warning", YELLOW, msg)


# ─── Pipeline ─────────────────────────────────────────────────────────────────

def compile_file(
    src_path: str,
    out_path: str,
    arch:     str,
    debug:    bool,
    use:      str | None,
) -> None:

    # 1. читаем исходник
    try:
        with open(src_path, encoding="utf-8") as f:
            source = f.read()
    except OSError as e:
        _error(f"Cannot read '{src_path}': {e}")

    print(f"{CYAN}[1/5]{RESET} Lexing  {src_path}")
    try:
        tokens = Lexer(source).tokenize()
    except Exception as e:
        _error(f"Lexer: {e}", source)

    print(f"{CYAN}[2/5]{RESET} Parsing")
    try:
        program = Parser(tokens).parse()
    except Exception as e:
        # пробуем вытащить line/col из сообщения
        _error(f"Parser: {e}", source)

    print(f"{CYAN}[3/5]{RESET} Semantic analysis")
    try:
        SemanticAnalyzer().analyze(program)
    except Exception as e:
        _error(f"Semantic: {e}", source)

    print(f"{CYAN}[3.5/5]{RESET} Optimization (pass-through)")
    program = Optimizer().optimize(program)

    print(f"{CYAN}[4/5]{RESET} Code generation  arch={arch}")
    try:
        target = BinaryTarget(arch)
        binary = target.emit(program)
    except ArchLoadError as e:
        _error(f"Architecture error: {e}")
    except CodeGenError as e:
        _error(f"CodeGen: {e}", source)
    except Exception as e:
        _error(f"Internal codegen error: {e}", source)

    # 5. записываем бинарник
    print(f"{CYAN}[5/5]{RESET} Writing {out_path}  ({len(binary)} bytes)")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(binary)
    except OSError as e:
        _error(f"Cannot write '{out_path}': {e}")

    # debug дамп
    if debug:
        _write_debug(src_path, out_path, binary, arch)

    print(f"\n{BOLD}OK{RESET}  {out_path}  {len(binary)} bytes")


def _write_debug(src_path: str, out_path: str, binary: bytes, arch: str):
    """Пишет .hex дамп и пытается вызвать ndisasm если установлен."""
    base   = os.path.splitext(out_path)[0]
    hex_path = base + ".hex"
    asm_path = base + ".asm.txt"

    # hex дамп
    with open(hex_path, "w") as f:
        for i in range(0, len(binary), 16):
            chunk = binary[i:i+16]
            hex_part = " ".join(f"{b:02X}" for b in chunk)
            asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            f.write(f"{i:04X}:  {hex_part:<48}  {asc_part}\n")
    print(f"  {CYAN}debug{RESET}  hex dump → {hex_path}")

    # пробуем ndisasm
    import shutil, subprocess
    if shutil.which("ndisasm"):
        bits = "16"   # TODO: брать из arch JSON
        try:
            result = subprocess.run(
                ["ndisasm", f"-b{bits}", out_path],
                capture_output=True, text=True
            )
            with open(asm_path, "w") as f:
                f.write(result.stdout)
            print(f"  {CYAN}debug{RESET}  ndisasm  → {asm_path}")
        except Exception as e:
            _warn(f"ndisasm failed: {e}")
    else:
        _warn("ndisasm not found — install NASM for asm debug dump")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog        = "boxlang6",
        description = "BoxLang6 compiler — bit-oriented ISA descriptor",
        formatter_class = argparse.RawDescriptionHelpFormatter,
        epilog = """
examples:
  python -m boxlang6 hello.box
  python -m boxlang6 hello.box -o build/hello.com --arch x16 --debug
  python -m boxlang6 hello.box --use x16pros
        """
    )

    p.add_argument(
        "input",
        metavar = "INPUT",
        help    = "source .box file"
    )
    p.add_argument(
        "-o", "--output",
        metavar = "OUTPUT",
        default = None,
        help    = "output binary (default: same name as input, .com/.bin)"
    )
    p.add_argument(
        "--arch",
        metavar = "ARCH",
        default = "x16",
        help    = "architecture config name from arch/ dir (default: x16)"
    )
    p.add_argument(
        "--use",
        metavar = "SYSTEM",
        default = None,
        help    = "system config ($use directive override)"
    )
    p.add_argument(
        "--debug",
        action  = "store_true",
        help    = "write .hex dump and ndisasm .asm.txt alongside output"
    )
    p.add_argument(
        "--version",
        action  = "version",
        version = "BoxLang6 0.1.0"
    )

    return p


def main():
    parser = build_parser()
    args   = parser.parse_args()

    # определяем output путь
    if args.output:
        out_path = args.output
    else:
        base     = os.path.splitext(args.input)[0]
        out_path = base + ".com"

    compile_file(
        src_path = args.input,
        out_path = out_path,
        arch     = args.arch,
        debug    = args.debug,
        use      = args.use,
    )


if __name__ == "__main__":
    main()
