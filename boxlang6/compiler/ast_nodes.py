from dataclasses import dataclass, field
from typing import Optional, List, Any


# ─── Base ───────────────────────────────────────────────────────────────────

@dataclass
class Node:
    """Базовый узел AST. Все узлы наследуются от него."""
    line: int = 0
    col:  int = 0

    def __repr__(self):
        return f"{self.__class__.__name__}(line={self.line})"


# ─── Program ─────────────────────────────────────────────────────────────────

@dataclass
class Program(Node):
    """Корень AST — весь файл."""
    body: List[Node] = field(default_factory=list)


# ─── Includes ────────────────────────────────────────────────────────────────

@dataclass
class IncludeStd(Node):
    """$include <stdlib>"""
    name: str = ""          # "stdlib", "io", etc.

@dataclass
class IncludeFile(Node):
    """$include "filename.box" """
    path: str = ""
    
@dataclass
class UseDirective(Node):
    """$use x16pros"""
    name: str = ""

# ─── Types ───────────────────────────────────────────────────────────────────

@dataclass
class TypeRef(Node):
    """
    Ссылка на тип переменной.
    base    — базовый тип: bit, bit2, bit4, char, num16, num32
    pointer — является ли указателем (type*)
    array   — размер массива, если type[N], иначе None
    """
    base:    str            = "num16"
    pointer: bool           = False
    array:   Optional[int]  = None

    def __str__(self):
        s = self.base
        if self.array is not None:
            s += f"[{self.array}]"
        if self.pointer:
            s += "*"
        return s
    

@dataclass
class LabelRef(Node):
    """
    Адрес функции или метки как значение.
    &func_name → LabelRef("func_name")
    Используется в: box* fp: &myFunc;  и  asm["call", &myFunc];
    """
    name: str = ""

@dataclass
class AsmLabel(Node):
    """Объявление метки внутри функции: @fs_error:"""
    name: str = ""

@dataclass
class AsmLabelRef(Node):
    """Ссылка на метку как аргумент: @fs_error"""
    name: str = ""

# ─── Expressions ─────────────────────────────────────────────────────────────

@dataclass
class Literal(Node):
    """Числовой или символьный литерал. Например: 16, 'a', 0xFF"""
    value: Any = 0
    
@dataclass
class StringLiteral(Node):
    """Строковый литерал: "Hello, World!" """
    value: str = ""


@dataclass
class ArrayInit(Node):
    """Инициализатор массива: {1, 2, 3}"""
    elements: List[Node] = field(default_factory=list)

@dataclass
class Identifier(Node):
    """Имя переменной или функции."""
    name: str = ""

@dataclass
class ScopedIdentifier(Node):
    """namespace::funcName"""
    namespace: str = ""
    name:      str = ""

@dataclass
class BinaryOp(Node):
    """
    Бинарная операция: a + b, a < 5, etc.
    op — строка оператора: '+', '-', '*', '/', '<', '>', '==', '!=', '<=', '>='
    """
    left:  Node = field(default_factory=Node)
    op:    str  = "+"
    right: Node = field(default_factory=Node)

@dataclass
class UnaryOp(Node):
    """Унарная операция: -x, !x, *ptr, &var"""
    op:      str  = "-"
    operand: Node = field(default_factory=Node)

@dataclass
class IndexAccess(Node):
    """Доступ к элементу массива: arr[i]"""
    target: Node = field(default_factory=Node)
    index:  Node = field(default_factory=Node)

@dataclass
class FieldAccess(Node):
    """Доступ к полю shelf: myShelf.varA"""
    target: Node = field(default_factory=Node)
    field_name: str = ""


# ─── Statements ──────────────────────────────────────────────────────────────

@dataclass
class VarDecl(Node):
    """
    Объявление переменной.
    num16 myVar: 16;
    """
    type_ref: TypeRef   = field(default_factory=TypeRef)
    name:     str       = ""
    value:    Optional[Node] = None     # инициализатор (может отсутствовать)

@dataclass
class Assignment(Node):
    """myVar: expr;  или  arr[i]: expr;"""
    target: Node = field(default_factory=Node)
    value:  Node = field(default_factory=Node)

@dataclass
class ReturnStmt(Node):
    """ret expr;"""
    value: Optional[Node] = None

@dataclass
class ExitCall(Node):
    """
    exit[code] — завершение программы.
    code кладётся в регистр, обозначенный как 'ax' в arch JSON (return register).
    Если code не передан — по умолчанию 0.
    """
    code: Optional[Node] = None   # exit[0], exit[myVar], exit[ax + 1] — всё ок

@dataclass
class AsmInsert(Node):
    """
    asm["int", 0x22];           -> insn="int",      args=[Literal(0x22)]
    asm["mov.reg16_imm16", ax, 5] -> insn="mov.reg16_imm16", args=[...]
    """
    insn: str        = ""        # было template
    args: List[Node] = field(default_factory=list)

@dataclass
class FunctionCall(Node):
    """
    open myFunc[args];
    open namespace::myFunc[args];
    target — Identifier или ScopedIdentifier
    """
    target: Node        = field(default_factory=Node)
    args:   List[Node]  = field(default_factory=list)


# ─── Control Flow ─────────────────────────────────────────────────────────────

@dataclass
class WhileLoop(Node):
    """
    while [condition] ( body )
    """
    condition: Node       = field(default_factory=Node)
    body:      List[Node] = field(default_factory=list)

@dataclass
class ForLoop(Node):
    """
    for [init; condition; step] ( body )
    """
    init:      Optional[Node] = None
    condition: Optional[Node] = None
    step:      Optional[Node] = None
    body:      List[Node]     = field(default_factory=list)
    
@dataclass
class IfStmt(Node):
    """
    if [condition] ( body )
    else if [condition] ( body )
    else ( body )
    """
    condition: Node           = field(default_factory=Node)
    body:      List[Node]     = field(default_factory=list)
    else_if:   List["IfStmt"] = field(default_factory=list)  # else if ветки
    else_body: Optional[List[Node]] = None                   # else ветка


# ─── Functions ────────────────────────────────────────────────────────────────

@dataclass
class Param(Node):
    """Параметр функции: char a"""
    type_ref: TypeRef = field(default_factory=TypeRef)
    name:     str     = ""

@dataclass
class FunctionDef(Node):
    """
    box _start[] : num16 ( body )
    box myFunc[char a, num16 b] ( body )
    return_type — None если нет возвращаемого типа
    """
    name:        str            = ""
    params:      List[Param]    = field(default_factory=list)
    return_type: Optional[TypeRef] = None
    body:        List[Node]     = field(default_factory=list)


# ─── Namespace ────────────────────────────────────────────────────────────────

@dataclass
class Namespace(Node):
    """
    namespace MySpace ( ... )
    """
    name: str       = ""
    body: List[Node] = field(default_factory=list)


# ─── Shelf (struct) ───────────────────────────────────────────────────────────

@dataclass
class FieldDecl(Node):
    """Поле shelf: num16 varA: 0;"""
    type_ref: TypeRef        = field(default_factory=TypeRef)
    name:     str            = ""
    default:  Optional[Node] = None

@dataclass
class ShelfDef(Node):
    """
    shelf MyShelf ( fields )
    """
    name:   str             = ""
    fields: List[FieldDecl] = field(default_factory=list)

