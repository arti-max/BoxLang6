from typing import Dict, Optional, List
try:
    from .ast_nodes import *
except ImportError:
    import sys, os
    sys.path.insert(0, os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    ))
    from boxlang6.compiler.ast_nodes import *


class SemanticError(Exception):
    def __init__(self, msg: str, node: Node):
        super().__init__(f"[Semantic] {msg} at {node.line}:{node.col}")
        self.node = node


# ─── Symbol ──────────────────────────────────────────────────────────────────

class Symbol:
    """Запись в таблице символов."""
    def __init__(self, name: str, kind: str, type_ref: Optional[TypeRef] = None):
        self.name     = name
        self.kind     = kind      # "var", "func", "param", "shelf"
        self.type_ref = type_ref

    def __repr__(self):
        return f"Symbol({self.kind} {self.name}: {self.type_ref})"


# ─── Scope ───────────────────────────────────────────────────────────────────

class Scope:
    """
    Одна область видимости.
    parent — родительский скоуп (None у глобального).
    """
    def __init__(self, name: str, parent: Optional["Scope"] = None):
        self.name    = name
        self.parent  = parent
        self.symbols: Dict[str, Symbol] = {}

    def define(self, sym: Symbol, node: Node):
        if sym.name in self.symbols:
            raise SemanticError(
                f"'{sym.name}' already defined in scope '{self.name}'", node
            )
        self.symbols[sym.name] = sym

    def lookup(self, name: str) -> Optional[Symbol]:
        """Ищет символ вверх по цепочке скоупов."""
        if name in self.symbols:
            return self.symbols[name]
        if self.parent:
            return self.parent.lookup(name)
        return None

    def lookup_local(self, name: str) -> Optional[Symbol]:
        return self.symbols.get(name)


# ─── Analyzer ────────────────────────────────────────────────────────────────

VALID_TYPES = {"bit", "bit2", "bit4", "char", "num16", "num32"}

class SemanticAnalyzer:
    def __init__(self):
        self.global_scope  = Scope("global")
        self.current_scope = self.global_scope
        # namespace name → Scope
        self.namespaces: Dict[str, Scope] = {}
        # shelf name → ShelfDef
        self.shelves:    Dict[str, ShelfDef] = {}
        # текущая функция (для проверки ret)
        self.current_func: Optional[FunctionDef] = None

    # ── scope helpers ─────────────────────────────────────────────────────────

    def push_scope(self, name: str) -> Scope:
        s = Scope(name, parent=self.current_scope)
        self.current_scope = s
        return s

    def pop_scope(self):
        self.current_scope = self.current_scope.parent

    # ── entry ─────────────────────────────────────────────────────────────────

    def analyze(self, program: Program):
        # Первый проход — собираем все функции и shelf на верхнем уровне
        # чтобы forward-reference работал
        self._hoist(program.body, self.global_scope)
        # Второй проход — полный анализ
        for node in program.body:
            self.visit(node)

    def _hoist(self, body: List[Node], scope: Scope):
        """Регистрируем имена функций и shelf до их анализа (forward decl)."""
        for node in body:
            if isinstance(node, FunctionDef):
                sym = Symbol(node.name, "func", node.return_type)
                scope.define(sym, node)
            elif isinstance(node, ShelfDef):
                self.shelves[node.name] = node
                sym = Symbol(node.name, "shelf")
                scope.define(sym, node)
            elif isinstance(node, Namespace):
                ns_scope = Scope(node.name, parent=scope)
                self.namespaces[node.name] = ns_scope
                self._hoist(node.body, ns_scope)

    # ── visitor ───────────────────────────────────────────────────────────────

    def visit(self, node: Node):
        method = f"visit_{type(node).__name__}"
        visitor = getattr(self, method, self.visit_default)
        return visitor(node)

    def visit_default(self, node: Node):
        pass  # узлы без специфической проверки просто пропускаем

    # ── top level ─────────────────────────────────────────────────────────────

    def visit_Program(self, node: Program):
        for n in node.body:
            self.visit(n)

    def visit_IncludeStd(self, node: IncludeStd):
        pass  # резолвится позже в загрузчике

    def visit_IncludeFile(self, node: IncludeFile):
        pass

    def visit_UseDirective(self, node: UseDirective):
        pass   # резолвится в загрузчике arch конфига
    
    def visit_Namespace(self, node: Namespace):
        ns_scope = self.namespaces.get(node.name)
        if ns_scope is None:
            raise SemanticError(f"Namespace '{node.name}' not hoisted", node)

        prev = self.current_scope
        self.current_scope = ns_scope
        # _hoist здесь НЕ вызываем — он уже был вызван в analyze()
        for n in node.body:
            self.visit(n)
        self.current_scope = prev

    def visit_ShelfDef(self, node: ShelfDef):
        for field in node.fields:
            self._check_type(field.type_ref, field)
            if field.default:
                self.visit(field.default)

    # ── function ──────────────────────────────────────────────────────────────

    def visit_FunctionDef(self, node: FunctionDef):
        if node.return_type:
            self._check_type(node.return_type, node)

        prev_func = self.current_func
        self.current_func = node
        self.push_scope(f"func:{node.name}")

        # регистрируем параметры
        for param in node.params:
            self._check_type(param.type_ref, param)
            self.current_scope.define(
                Symbol(param.name, "param", param.type_ref), param
            )

        # hoist вложенных функций
        self._hoist(node.body, self.current_scope)

        for stmt in node.body:
            self.visit(stmt)

        self.pop_scope()
        self.current_func = prev_func
        
    def visit_ArrayInit(self, node: ArrayInit):
        for el in node.elements:
            self.visit(el)

    # ── statements ────────────────────────────────────────────────────────────

    def visit_VarDecl(self, node: VarDecl):
        self._check_type(node.type_ref, node)
        
        
        if node.value:
            self.visit(node.value)
            self._check_value_fits(node.type_ref, node.value, node)
            # проверка строки: длина не может превышать размер массива
            if node.type_ref.array is not None and isinstance(node.value, StringLiteral):
                if len(node.value.value) > node.type_ref.array:
                    raise SemanticError(
                        f"String length {len(node.value.value)} exceeds "
                        f"array size {node.type_ref.array}", node
                    )
            
            if node.type_ref.base == "shelf":
                shelf_name = getattr(node.type_ref, 'shelf_name', None)
                if shelf_name is None:
                    raise SemanticError(f"shelf type requires name (shelve Vec2)", node)
                if shelf_name not in self.shelves:
                    raise SemanticError(f"No shelf '{shelf_name}' defined", node)        
            
            # проверка ArrayInit
            if node.type_ref.array is not None and isinstance(node.value, ArrayInit):
                if len(node.value.elements) > node.type_ref.array:
                    raise SemanticError(
                        f"Array initializer has {len(node.value.elements)} elements "
                        f"but array size is {node.type_ref.array}", node
                    )
        self.current_scope.define(
            Symbol(node.name, "var", node.type_ref), node
        )
        
    def _check_value_fits(self, type_ref: TypeRef, value: Node, node: Node):
        """Проверяет что литерал помещается в тип. Для массивов проверяет каждый элемент."""
        if type_ref.array is not None:
            # ArrayInit — проверяем каждый элемент
            if isinstance(value, ArrayInit):
                elem_ref = TypeRef(base=type_ref.base, pointer=type_ref.pointer, array=None)
                for el in value.elements:
                    self._check_value_fits(elem_ref, el, node)
            # StringLiteral — char уже гарантированно 0..127, пропускаем
            return

        if not isinstance(value, Literal) or not isinstance(value.value, int):
            return

        v = value.value
        limits = {
            "bit":   (0, 1),
            "bit2":  (0, 3),
            "bit4":  (0, 15),
            "char":  (0, 255),
            "num16": (0, 65535),
            "num32": (0, 4294967295),
        }
        lo, hi = limits.get(type_ref.base, (None, None))
        if lo is None:
            return
        if not (lo <= v <= hi):
            raise SemanticError(
                f"Value {v} overflows type '{type_ref.base}' "
                f"(valid range {lo}..{hi})",
                node
            )

    def visit_Assignment(self, node: Assignment):
        self.visit(node.target)
        self.visit(node.value)

    def visit_ReturnStmt(self, node: ReturnStmt):
        if self.current_func is None:
            raise SemanticError("'ret' outside of function", node)
        if node.value:
            self.visit(node.value)

    def visit_ExitCall(self, node: ExitCall):
        if node.code:
            self.visit(node.code)

    def visit_AsmInsert(self, node: AsmInsert):
        # Новый синтаксис: asm["insn", arg1, arg2]
        # Просто проверяем что аргументы валидны
        for arg in node.args:
            # регистры (ax, bx...) — это Identifier, но их нет в таблице символов
            # поэтому пропускаем Identifier если это регистр — codegen разберётся
            if isinstance(arg, Identifier):
                continue   # регистр или переменная — проверит codegen
            self.visit(arg)

    def visit_FunctionCall(self, node: FunctionCall):
        if isinstance(node.target, ScopedIdentifier):
            ns_scope = self.namespaces.get(node.target.namespace)
            if ns_scope is None:
                raise SemanticError(
                    f"Unknown namespace '{node.target.namespace}'", node
                )
            sym = ns_scope.lookup(node.target.name)
            if sym is None:
                raise SemanticError(
                    f"'{node.target.name}' not defined in namespace "
                    f"'{node.target.namespace}'",
                    node
                )
        else:
            name = node.target.name
            sym  = self.current_scope.lookup(name)
            if sym is None:
                raise SemanticError(f"Undefined function '{name}'", node)
            if sym.kind not in ("func",):
                raise SemanticError(f"'{name}' is not a function", node)

        for arg in node.args:
            self.visit(arg)

    def visit_WhileLoop(self, node: WhileLoop):
        self.visit(node.condition)
        self.push_scope("while")
        for stmt in node.body:
            self.visit(stmt)
        self.pop_scope()

    def visit_ForLoop(self, node: ForLoop):
        self.push_scope("for")
        if node.init:
            self.visit(node.init)
        if node.condition:
            self.visit(node.condition)
        if node.step:
            self.visit(node.step)
        for stmt in node.body:
            self.visit(stmt)
        self.pop_scope()
        
    def visit_IfStmt(self, node: IfStmt):
        self.visit(node.condition)
        self.push_scope("if")
        for stmt in node.body:
            self.visit(stmt)
        self.pop_scope()

        for ei in node.else_if:
            self.visit(ei.condition)
            self.push_scope("elif")
            for stmt in ei.body:
                self.visit(stmt)
            self.pop_scope()

        if node.else_body:
            self.push_scope("else")
            for stmt in node.else_body:
                self.visit(stmt)
            self.pop_scope()

    # ── expressions ───────────────────────────────────────────────────────────

    def visit_Identifier(self, node: Identifier):
        sym = self.current_scope.lookup(node.name)
        if sym is None:
            raise SemanticError(f"Undefined identifier '{node.name}'", node)
        return sym

    def visit_ScopedIdentifier(self, node: ScopedIdentifier):
        ns_scope = self.namespaces.get(node.namespace)
        if ns_scope is None:
            raise SemanticError(f"Unknown namespace '{node.namespace}'", node)
        sym = ns_scope.lookup(node.name)
        if sym is None:
            raise SemanticError(
                f"'{node.name}' not defined in '{node.namespace}'", node
            )
        return sym

    def visit_BinaryOp(self, node: BinaryOp):
        self.visit(node.left)
        self.visit(node.right)

    def visit_UnaryOp(self, node: UnaryOp):
        if node.op == "&":
            # &funcName или &varName — имя может быть функцией
            # семантик не проверяет, codegen разберётся (var_offsets vs labels)
            return
        if node.op == "*":
            # разыменование — проверяем операнд
            self.visit(node.operand)
            return
        self.visit(node.operand)

    def visit_IndexAccess(self, node: IndexAccess):
        self.visit(node.target)
        self.visit(node.index)

    def visit_FieldAccess(self, node: FieldAccess):
        # arr.length — compile-time константа, target должен быть массивом
        if node.field_name == "length":
            if isinstance(node.target, Identifier):
                sym = self.current_scope.lookup(node.target.name)
                if sym and sym.type_ref and sym.type_ref.array is None:
                    raise SemanticError(
                        f"'{node.target.name}' is not an array, "
                        f"cannot access .length", node
                    )
            return

        self.visit(node.target)
        
        # резолвим shelf_name из target
        shelf_name = self._resolve_shelf_name(node.target)
        if shelf_name:
            shelf_def = self.shelves.get(shelf_name)
            if shelf_def is None:
                raise SemanticError(f"No shelf '{shelf_name}'", node)
            
            field = next((f for f in shelf_def.fields if f.name == node.field_name), None)
            if field is None:
                raise SemanticError(f"No field '{node.field_name}' in shelf '{shelf_name}'", node)
            return
        
        # fallback для других случаев
        self.visit(node.target)

    def visit_Literal(self, node: Literal): # type: ignore
        pass  # литерал всегда валиден
    
    def visit_StringLiteral(self, node: StringLiteral):
        pass  # всегда валидна

    # ── helpers ───────────────────────────────────────────────────────────────

    def _check_type(self, type_ref: TypeRef, node: Node):
        base = type_ref.base
        
        if base == "shelf":
            shelf_name = getattr(type_ref, 'shelf_name', None)
            if shelf_name is None:
                raise SemanticError("shelf requires name: 'shelve Vec2' or 'shelf Vec2'", node)
            if shelf_name not in self.shelves:
                raise SemanticError(f"No shelf '{shelf_name}' defined", node)
            return  # ✅ shelf OK
        
        if base not in VALID_TYPES:
            raise SemanticError(f"Unknown type '{base}'", node)


# ─── Test ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import traceback, sys, os
    
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    
    from boxlang6.compiler.lexer     import Lexer
    from boxlang6.compiler.parser    import Parser    # ← это было только в первом блоке
    from boxlang6.compiler.ast_nodes import *
    from boxlang6.compiler.semantic  import SemanticAnalyzer, SemanticError  # ← и это

    def ok(label: str):
        print(f"  [OK] {label}")

    def fail(label: str, e: Exception):
        print(f"  [FAIL] {label}: {e}")
        traceback.print_exc()

    def analyze(src: str):
        tokens  = Lexer(src).tokenize()
        program = Parser(tokens).parse()
        SemanticAnalyzer().analyze(program)
        return program

    def should_fail(src: str):
        try:
            analyze(src)
            raise AssertionError("Expected error but got none")
        except SemanticError as e:
            return e
        except Exception as e:
            return e   # ParseError тоже считается "правильным" падением

    print("=== semantic.py self-test ===\n")

    # Базовая функция
    try:
        analyze("box _start[] : num16 ( ret 0; )")
        ok("box _start[] : num16 ( ret 0; )")
    except Exception as e:
        fail("basic function", e)

    # VarDecl + использование
    try:
        analyze("box f[] ( num16 x: 5; ret x; )")
        ok("VarDecl + ret x")
    except Exception as e:
        fail("VarDecl + ret x", e)

    # Undefined variable
    try:
        e = should_fail("box f[] ( ret unknownVar; )")
        assert "unknownVar" in str(e)
        ok("Undefined variable → SemanticError")
    except Exception as e:
        fail("Undefined variable", e)

    # Double declaration
    try:
        e = should_fail("box f[] ( num16 x: 0; num16 x: 1; )")
        assert "already defined" in str(e)
        ok("Double declaration → SemanticError")
    except Exception as e:
        fail("Double declaration", e)

    # Invalid type
    try:
        e = should_fail("box f[] ( num999 x: 0; )")
        # ParseError или SemanticError — оба валидны
        assert "num999" in str(e) or "Expected" in str(e)
        ok("Invalid type → Error")
    except Exception as e:
        fail("Invalid type", e)

    # asm %v count mismatch
    try:
        analyze("""
        box f[] (
            num16 x: 0;
            asm["mov", ax, x];
        )
        """)
        ok('asm["mov", ax, x]')
    except Exception as e:
        fail('asm insert', e)

    # asm явный вариант
    try:
        analyze('box f[] ( asm["int.imm8", 0x22]; )')
        ok('asm["int.imm8", 0x22]')
    except Exception as e:
        fail('asm explicit variant', e)

    # & address-of функции
    try:
        analyze("""
        box target[] ( ret 0; )
        box f[] (
            num16* fp: &target;
        )
        """)
        ok("& address-of function")
    except Exception as e:
        fail("& address-of", e)

    # exit[0]
    try:
        analyze("box f[] ( exit[0]; )")
        ok("exit[0]")
    except Exception as e:
        fail("exit[0]", e)

    # namespace + scoped call
    try:
        analyze("""
        namespace MySpace (
            box myFunc[char a] ( ret a; )
        )
        box main[] (
            open MySpace::myFunc['x'];
        )
        """)
        ok("namespace + scoped call")
    except Exception as e:
        fail("namespace + scoped call", e)

    # shelf
    try:
        analyze("""
        shelf MyShelf (
            num16 varA: 0;
            char b;
        )
        box f[] ( ret 0; )
        """)
        ok("shelf definition")
    except Exception as e:
        fail("shelf", e)

    # while + for
    try:
        analyze("""
        box f[] (
            num16 i: 0;
            while [i < 10] (
                i: i + 1;
            )
            for [num16 j: 0; j < 5; j + 1] (
                ret j;
            )
        )
        """)
        ok("while + for")
    except Exception as e:
        fail("while + for", e)

    # ret outside function → но у нас ret всегда внутри box, проверим
    # вызов несуществующей функции
    try:
        e = should_fail("box f[] ( open ghost[]; )")
        assert "ghost" in str(e)
        ok("Undefined function call → SemanticError")
    except Exception as e:
        fail("Undefined function call", e)

    # forward reference (f вызывает g, g определена после)
    try:
        analyze("""
        box f[] ( open g[]; )
        box g[] ( ret 0; )
        """)
        ok("Forward reference f → g")
    except Exception as e:
        fail("Forward reference", e)

    print("\n=== done ===")
