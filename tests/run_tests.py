#!/usr/bin/env python3
"""
Запуск всех тестов BoxLang6.
python tests/run_tests.py
"""
import sys, os, traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from boxlang6.compiler.lexer        import Lexer
from boxlang6.compiler.parser       import Parser
from boxlang6.compiler.semantic     import SemanticAnalyzer
from boxlang6.compiler.optimizer    import Optimizer
from boxlang6.targets.binary_target import BinaryTarget, CodeGenError
from boxlang6.targets.base_target   import ArchLoadError

TESTS_DIR  = Path(__file__).parent
BUILD_DIR  = TESTS_DIR.parent / "build"
ARCH       = "x16"

BUILD_DIR.mkdir(exist_ok=True)

passed = 0
failed = 0


def run_test(path: Path):
    global passed, failed
    src = path.read_text(encoding="utf-8")
    try:
        tokens  = Lexer(src).tokenize()
        program = Parser(tokens).parse()
        SemanticAnalyzer().analyze(program)
        program = Optimizer().optimize(program)
        binary  = BinaryTarget(ARCH).emit(program)
        assert len(binary) > 0, "Empty output"

        out_path = BUILD_DIR / (path.stem + ".com")
        out_path.write_bytes(binary)

        print(f"  [OK]   {path.name:<30} {len(binary):>4} bytes  →  build/{out_path.name}")
        passed += 1

    except (CodeGenError, ArchLoadError) as e:
        print(f"  [FAIL] {path.name:<30} CodeGen: {e}")
        failed += 1

    except Exception as e:
        print(f"  [FAIL] {path.name:<30} {type(e).__name__}: {e}")
        traceback.print_exc()
        failed += 1


print(f"=== BoxLang6 Tests (arch={ARCH}) ===")
print(f"    output → {BUILD_DIR}\n")

for test_file in sorted(TESTS_DIR.glob("*.box")):
    run_test(test_file)

print(f"\n{'='*50}")
print(f"  Passed: {passed}  Failed: {failed}  Total: {passed + failed}")

if failed == 0:
    print("  ALL TESTS PASSED ✓")
else:
    print("  SOME TESTS FAILED ✗")
    sys.exit(1)
