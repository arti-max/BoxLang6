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


from .compiler.lexer         import Lexer
from .compiler.parser        import Parser
from .compiler.semantic      import SemanticAnalyzer
from .compiler.optimizer     import Optimizer
from .compiler.preprocessor  import Preprocessor, PreprocessorError
from .targets.binary_target  import BinaryTarget, CodeGenError
from .targets.base_target    import ArchLoadError


# ─── ANSI цвета ───────────────────────────────────────────────────────────────


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
    defines: list
) -> None:

    std_dir = os.path.join(os.path.dirname(__file__), "std")

    # 0. читаем исходник
    try:
        with open(src_path, encoding="utf-8") as f:
            raw_source = f.read()
    except OSError as e:
        _error(f"Cannot read '{src_path}': {e}")

    # 1. препроцессор — до лексера, на уровне текста
    print(f"{CYAN}[1/6]{RESET} Preprocessing  {src_path}")
    try:
        pp = Preprocessor(std_dir=std_dir)
        for d in defines:
            pp.defines[d] = None
        source, defines = pp.process(raw_source, current_file=src_path)
    except PreprocessorError as e:
        _error(f"Preprocessor: {e}")
    except OSError as e:
        _error(f"Preprocessor I/O: {e}")

    # 2. лексер
    print(f"{CYAN}[2/6]{RESET} Lexing")
    try:
        tokens = Lexer(source).tokenize()
    except Exception as e:
        _error(f"Lexer: {e}", source)

    # 3. парсер
    print(f"{CYAN}[3/6]{RESET} Parsing")
    try:
        program = Parser(tokens).parse()
    except Exception as e:
        _error(f"Parser: {e}", source)

    # 4. семантический анализ
    print(f"{CYAN}[4/6]{RESET} Semantic analysis")
    try:
        SemanticAnalyzer().analyze(program)
    except Exception as e:
        _error(f"Semantic: {e}", source)

    # 4.5. оптимизатор
    print(f"{CYAN}[4.5/6]{RESET} Optimization (pass-through)")
    program = Optimizer().optimize(program)

    # 5. кодогенерация
    print(f"{CYAN}[5/6]{RESET} Code generation  arch={arch}")
    # try:
    target = BinaryTarget(arch)
    binary = target.emit(program)
    # except ArchLoadError as e:
        # _error(f"Architecture error: {e}")
    # except CodeGenError as e:
        # _error(f"CodeGen: {e}", source)
    # except Exception as e:
        # _error(f"Internal codegen error: {e}", source)

    # 6. запись бинарника
    print(f"{CYAN}[6/6]{RESET} Writing {out_path}  ({len(binary)} bytes)")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(binary)
    except OSError as e:
        _error(f"Cannot write '{out_path}': {e}")

    if debug:
        _write_debug(src_path, out_path, binary, arch)

    print(f"\n{BOLD}OK{RESET}  {out_path}  {len(binary)} bytes")


# ─── Debug dump ───────────────────────────────────────────────────────────────


def _write_debug(src_path: str, out_path: str, binary: bytes, arch: str):
    base     = os.path.splitext(out_path)[0]
    hex_path = base + ".hex"
    asm_path = base + ".asm.txt"

    with open(hex_path, "w") as f:
        for i in range(0, len(binary), 16):
            chunk    = binary[i:i+16]
            hex_part = " ".join(f"{b:02X}" for b in chunk)
            asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            f.write(f"{i:04X}:  {hex_part:<48}  {asc_part}\n")
    print(f"  {CYAN}debug{RESET}  hex dump → {hex_path}")

    import shutil, subprocess
    if shutil.which("ndisasm"):
        bits = "16"  # TODO: брать из arch JSON
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
    p.add_argument("input",  metavar="INPUT",  help="source .box file")
    p.add_argument("-o", "--output", metavar="OUTPUT", default=None,
                   help="output binary (default: same name as input, .com/.bin)")
    p.add_argument("--arch",    metavar="ARCH",   default="x16",
                   help="architecture config name from arch/ dir (default: x16)")
    p.add_argument("--use",     metavar="SYSTEM", default=None,
                   help="system config ($use directive override)")
    p.add_argument("--debug",   action="store_true",
                   help="write .hex dump and ndisasm .asm.txt alongside output")
    p.add_argument("--version", action="version", version="BoxLang6 0.1.0")
    p.add_argument("-D", action="append", dest="defines", default=[],
               metavar="NAME", help="define preprocessor symbol (e.g. -D MOS6502)")
    return p


def main():
    parser   = build_parser()
    args     = parser.parse_args()
    out_path = args.output or (os.path.splitext(args.input)[0] + ".com")

    compile_file(
        src_path = args.input,
        out_path = out_path,
        arch     = args.arch,
        debug    = args.debug,
        use      = args.use,
        defines  = args.defines,
    )


if __name__ == "__main__":
    main()
