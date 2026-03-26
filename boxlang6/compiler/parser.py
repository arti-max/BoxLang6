from typing import List, Optional
try:
    from .lexer  import Lexer, Token, T
    from .ast_nodes import *
except ImportError:
    import sys, os
    sys.path.insert(0, os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    ))
    from boxlang6.compiler.lexer import Lexer, Token, T
    from boxlang6.compiler.ast_nodes import *


class ParseError(Exception):
    def __init__(self, msg: str, token: Token):
        super().__init__(f"[Parser] {msg} at {token.line}:{token.col} (got {token.type} {token.value!r})")
        self.token = token


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos    = 0

    # ── helpers ──────────────────────────────────────────────────────────────

    def peek(self, offset: int = 0) -> Token:
        i = self.pos + offset
        if i < len(self.tokens):
            return self.tokens[i]
        return self.tokens[-1]  # EOF

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.type != T.EOF:
            self.pos += 1
        return tok

    def check(self, *types: str) -> bool:
        return self.peek().type in types

    def match(self, *types: str) -> Optional[Token]:
        if self.check(*types):
            return self.advance()
        return None

    def expect(self, type_: str, msg: str = "") -> Token:
        if self.peek().type == type_:
            return self.advance()
        raise ParseError(
            msg or f"Expected {type_}",
            self.peek()
        )

    def loc(self) -> dict:
        """Текущая позиция для передачи в узел."""
        t = self.peek()
        return {"line": t.line, "col": t.col}

    # ── entry point ──────────────────────────────────────────────────────────

    def parse(self) -> Program:
        body = []
        while not self.check(T.EOF):
            body.append(self.parse_top_level())
        return Program(body=body)

    # ── top level ────────────────────────────────────────────────────────────

    def parse_top_level(self) -> Node:
        tok = self.peek()

        if tok.type == T.INCLUDE:
            return self.parse_include()
        if tok.type == T.USE:
            return self.parse_use()
        if tok.type == T.NAMESPACE:
            return self.parse_namespace()
        if tok.type == T.SHELF:
            return self.parse_shelf()
        if tok.type == T.BOX:
            return self.parse_function()

        raise ParseError("Unexpected token at top level", tok)

    # ── $include ─────────────────────────────────────────────────────────────

    def parse_include(self) -> Node:
        tok = self.expect(T.INCLUDE)
        l, c = tok.line, tok.col

        # $include <stdlib>
        if self.match(T.LT):
            name = self.expect(T.IDENT, "Expected library name after <").value
            self.expect(T.GT, "Expected > after library name")
            return IncludeStd(name=name, line=l, col=c)

        # $include "filename.box"
        if self.check(T.STRING_LIT):
            path = self.advance().value
            return IncludeFile(path=path, line=l, col=c)

        raise ParseError("Expected <lib> or \"file\" after $include", self.peek())
    
    def parse_use(self) -> UseDirective:
        tok  = self.expect(T.USE)
        name = self.expect(T.IDENT, "Expected system name after $use").value
        return UseDirective(name=name, line=tok.line, col=tok.col)

    # ── namespace ────────────────────────────────────────────────────────────

    def parse_namespace(self) -> Namespace:
        tok = self.expect(T.NAMESPACE)
        name = self.expect(T.IDENT, "Expected namespace name").value
        self.expect(T.LPAREN, "Expected '(' after namespace name")
        body = self.parse_body()
        self.expect(T.RPAREN, "Expected ')' to close namespace")
        return Namespace(name=name, body=body, line=tok.line, col=tok.col)

    # ── shelf ────────────────────────────────────────────────────────────────

    def parse_shelf(self) -> ShelfDef:
        tok = self.expect(T.SHELF)
        name = self.expect(T.IDENT, "Expected shelf name").value
        self.expect(T.LPAREN, "Expected '(' after shelf name")

        fields = []
        while not self.check(T.RPAREN, T.EOF):
            fields.append(self.parse_field_decl())

        self.expect(T.RPAREN, "Expected ')' to close shelf")
        return ShelfDef(name=name, fields=fields, line=tok.line, col=tok.col)

    def parse_field_decl(self) -> FieldDecl:
        type_tok = self.peek()
        l, c     = type_tok.line, type_tok.col
        type_ref = self.parse_type_ref()
        name     = self.expect(T.IDENT, "Expected field name").value
        default  = None
        if self.match(T.COLON):
            default = self.parse_expr()
        self.expect(T.SEMICOLON, "Expected ';' after field declaration")
        return FieldDecl(type_ref=type_ref, name=name, default=default, line=l, col=c)

    # ── function ─────────────────────────────────────────────────────────────

    def parse_function(self) -> FunctionDef:
        tok = self.expect(T.BOX)
        name = self.expect(T.IDENT, "Expected function name").value

        # params: [char a, num16 b]
        self.expect(T.LBRACKET, "Expected '[' after function name")
        params = []
        if not self.check(T.RBRACKET):
            params = self.parse_params()
        self.expect(T.RBRACKET, "Expected ']' to close params")

        # optional return type: : num16
        return_type = None
        if self.match(T.COLON):
            return_type = self.parse_type_ref()

        self.expect(T.LPAREN, "Expected '(' to open function body")
        body = self.parse_body()
        self.expect(T.RPAREN, "Expected ')' to close function body")

        return FunctionDef(
            name=name, params=params,
            return_type=return_type, body=body,
            line=tok.line, col=tok.col
        )

    def parse_params(self) -> List[Param]:
        params = []
        while True:
            type_tok = self.peek()
            l, c     = type_tok.line, type_tok.col
            type_ref = self.parse_type_ref()
            name     = self.expect(T.IDENT, "Expected parameter name").value
            params.append(Param(type_ref=type_ref, name=name, line=l, col=c))
            if not self.match(T.COMMA):
                break
        return params

    # ── body (список стейтментов) ─────────────────────────────────────────────

    def parse_body(self) -> List[Node]:
        stmts = []
        while not self.check(T.RPAREN, T.EOF):
            stmts.append(self.parse_statement())
        return stmts

    # ── statements ───────────────────────────────────────────────────────────

    def parse_statement(self) -> Node:
        tok = self.peek()

        # вложенные конструкции
        if tok.type == T.BOX:
            return self.parse_function()
        if tok.type == T.SHELF:
            return self.parse_shelf()
        if tok.type == T.NAMESPACE:
            return self.parse_namespace()
        if tok.type == T.WHILE:
            return self.parse_while()
        if tok.type == T.FOR:
            return self.parse_for()
        if tok.type == T.ASM:
            return self.parse_asm()
        if tok.type == T.RET:
            return self.parse_ret()
        if tok.type == T.EXIT:
            return self.parse_exit()
        if tok.type == T.OPEN:
            return self.parse_open()
        if tok.type == T.INCLUDE:
            return self.parse_include()
        if tok.type == T.IF:
            return self.parse_if()
        if tok.type == T.LABEL_DEF:
            self.advance()
            return AsmLabel(name=tok.value, line=tok.line, col=tok.col)

        # объявление переменной: начинается с TYPE
        if tok.type == T.TYPE:
            return self.parse_var_decl()

        # присваивание или выражение-стейтмент
        return self.parse_assign_or_expr_stmt()

    # ── while ────────────────────────────────────────────────────────────────

    def parse_while(self) -> WhileLoop:
        tok = self.expect(T.WHILE)
        self.expect(T.LBRACKET, "Expected '[' after while")
        cond = self.parse_expr()
        self.expect(T.RBRACKET, "Expected ']' to close while condition")
        self.expect(T.LPAREN,   "Expected '(' to open while body")
        body = self.parse_body()
        self.expect(T.RPAREN,   "Expected ')' to close while body")
        return WhileLoop(condition=cond, body=body, line=tok.line, col=tok.col)

    # ── for ──────────────────────────────────────────────────────────────────

    def parse_for(self) -> ForLoop:
        tok = self.expect(T.FOR)
        self.expect(T.LBRACKET, "Expected '[' after for")

        init = cond = step = None

        # init
        if not self.check(T.SEMICOLON):
            if self.peek().type == T.TYPE:
                init = self.parse_var_decl(expect_semi=False)
            else:
                init = self.parse_assign_or_expr(expect_semi=False)
        self.expect(T.SEMICOLON, "Expected ';' after for init")

        # condition
        if not self.check(T.SEMICOLON):
            cond = self.parse_expr()
        self.expect(T.SEMICOLON, "Expected ';' after for condition")

        # step — парсим как присваивание если есть ':'
        if not self.check(T.RBRACKET):
            step = self.parse_assign_or_expr(expect_semi=False)

        self.expect(T.RBRACKET, "Expected ']' to close for header")
        self.expect(T.LPAREN,   "Expected '(' to open for body")
        body = self.parse_body()
        self.expect(T.RPAREN,   "Expected ')' to close for body")

        return ForLoop(init=init, condition=cond, step=step,
                    body=body, line=tok.line, col=tok.col)

    # ── asm ──────────────────────────────────────────────────────────────────

    def parse_asm(self) -> AsmInsert:
        tok = self.expect(T.ASM)
        self.expect(T.LBRACKET, "Expected '[' after asm")
        insn = self.expect(T.STRING_LIT, "Expected instruction name").value

        args = []
        while self.match(T.COMMA):
            args.append(self.parse_expr())

        self.expect(T.RBRACKET, "Expected ']' to close asm")
        self.expect(T.SEMICOLON, "Expected ';' after asm")
        return AsmInsert(insn=insn, args=args, line=tok.line, col=tok.col)

    # ── ret ──────────────────────────────────────────────────────────────────

    def parse_ret(self) -> ReturnStmt:
        tok = self.expect(T.RET)
        value = None
        if not self.check(T.SEMICOLON):
            value = self.parse_expr()
        self.expect(T.SEMICOLON, "Expected ';' after ret")
        return ReturnStmt(value=value, line=tok.line, col=tok.col)

    # ── exit ─────────────────────────────────────────────────────────────────

    def parse_exit(self) -> ExitCall:
        """exit[code];  или  exit[];"""
        tok = self.expect(T.EXIT)
        self.expect(T.LBRACKET, "Expected '[' after exit")
        code = None
        if not self.check(T.RBRACKET):
            code = self.parse_expr()
        self.expect(T.RBRACKET, "Expected ']' to close exit")
        self.expect(T.SEMICOLON, "Expected ';' after exit")
        return ExitCall(code=code, line=tok.line, col=tok.col)

    # ── open (function call) ──────────────────────────────────────────────────

    def parse_open(self) -> FunctionCall:
        """open myFunc[args];  или  open ns::myFunc[args];"""
        tok = self.expect(T.OPEN)

        # имя — IDENT или IDENT::IDENT
        name_tok = self.expect(T.IDENT, "Expected function name after open")
        if self.match(T.SCOPE):
            ns   = name_tok.value
            name = self.expect(T.IDENT, "Expected function name after ::").value
            target = ScopedIdentifier(namespace=ns, name=name,
                                      line=name_tok.line, col=name_tok.col)
        else:
            target = Identifier(name=name_tok.value,
                                line=name_tok.line, col=name_tok.col)

        self.expect(T.LBRACKET, "Expected '[' after function name in open")
        args = []
        if not self.check(T.RBRACKET):
            args.append(self.parse_expr())
            while self.match(T.COMMA):
                args.append(self.parse_expr())
        self.expect(T.RBRACKET, "Expected ']' to close open args")
        self.expect(T.SEMICOLON, "Expected ';' after open")

        return FunctionCall(target=target, args=args, line=tok.line, col=tok.col)

    # ── var decl ──────────────────────────────────────────────────────────────

    def parse_var_decl(self, expect_semi: bool = True) -> VarDecl:
        type_tok = self.peek()
        l, c     = type_tok.line, type_tok.col
        type_ref = self.parse_type_ref()
        name     = self.expect(T.IDENT, "Expected variable name").value

        # массив: num16 arr[5]  — размер после имени
        if self.match(T.LBRACKET):
            count_tok      = self.expect(T.NUMBER, "Expected array size")
            type_ref.array = int(count_tok.value, 0)
            self.expect(T.RBRACKET, "Expected ']' after array size")

        value = None
        if self.match(T.COLON):
            value = self.parse_expr()
        if expect_semi:
            self.expect(T.SEMICOLON, "Expected ';' after variable declaration")
        return VarDecl(type_ref=type_ref, name=name, value=value, line=l, col=c)

    # ── assign or expr stmt ───────────────────────────────────────────────────

    def parse_assign_or_expr_stmt(self) -> Node:
        return self.parse_assign_or_expr(expect_semi=True)

    # ── type ref ─────────────────────────────────────────────────────────────

    def parse_type_ref(self) -> TypeRef:
        tok  = self.expect(T.TYPE, "Expected type")
        base = tok.value
        l, c = tok.line, tok.col
        
        if tok.value == "shelve":
            shelf_name = self.expect(T.IDENT).value
            return TypeRef(base="shelf", pointer=True, array=None, shelf_name=shelf_name)
        
        pointer = False
        if self.match(T.STAR):
            pointer = True

        return TypeRef(base=base, pointer=pointer, array=None, line=l, col=c)

    # ── expressions ──────────────────────────────────────────────────────────
    #
    # Precedence (низкий → высокий):
    #   comparison:  == != < > <= >=
    #   additive:    + -
    #   multiplicative: * /
    #   unary:       - ! * &
    #   postfix:     [index]  .field
    #   primary:     literal, ident, (expr)

    def parse_expr(self) -> Node:
        return self.parse_comparison()

    def parse_comparison(self) -> Node:
        left = self.parse_additive()
        while self.check(T.EQ, T.NEQ, T.LT, T.GT, T.LTE, T.GTE):
            op    = self.advance().value
            right = self.parse_additive()
            left  = BinaryOp(left=left, op=op, right=right)
        return left

    def parse_additive(self) -> Node:
        left = self.parse_multiplicative()
        while self.check(T.PLUS, T.MINUS):
            op    = self.advance().value
            right = self.parse_multiplicative()
            left  = BinaryOp(left=left, op=op, right=right)
        return left

    def parse_multiplicative(self) -> Node:
        left = self.parse_unary()
        while self.check(T.STAR, T.SLASH):
            op    = self.advance().value
            right = self.parse_unary()
            left  = BinaryOp(left=left, op=op, right=right)
        return left

    def parse_unary(self) -> Node:
        # унарные операторы: - ! * &
        if self.check(T.MINUS, T.BANG, T.STAR, T.AMP):
            op_tok  = self.advance()
            operand = self.parse_unary()
            return UnaryOp(
                op      = op_tok.value,
                operand = operand,
                line    = op_tok.line,
                col     = op_tok.col,
            )

        return self.parse_postfix()

    def parse_postfix(self) -> Node:
        node = self.parse_primary()
        while True:
            if self.match(T.LBRACKET):          # arr[i]
                index = self.parse_expr()
                self.expect(T.RBRACKET)
                node = IndexAccess(target=node, index=index)
            elif self.match(T.DOT):             # obj.field
                fname = self.expect(T.IDENT).value
                node = FieldAccess(target=node, field_name=fname, pointer_deref=False)
            elif (                              # obj->field  ← FIX: смотрим вперёд без consume
                self.peek(0).type == T.MINUS
                and self.peek(1).type == T.GT
            ):
                self.advance()  # consume  -
                self.advance()  # consume  >
                fname = self.expect(T.IDENT).value
                node = FieldAccess(target=node, field_name=fname, pointer_deref=True)
            else:
                break
        return node

    def parse_primary(self) -> Node:
        tok = self.peek()

        # number
        if tok.type == T.NUMBER:
            self.advance()
            return Literal(value=int(tok.value, 0), line=tok.line, col=tok.col)

        # char literal
        if tok.type == T.CHAR_LIT:
            self.advance()
            return Literal(value=ord(tok.value), line=tok.line, col=tok.col)

        # string literal (в выражениях — для asm и т.п.)
        if tok.type == T.STRING_LIT:
            self.advance()
            return StringLiteral(value=tok.value, line=tok.line, col=tok.col)
        
        if tok.type == T.LABEL_REF:
            self.advance()
            return AsmLabelRef(name=tok.value, line=tok.line, col=tok.col)

        # identifier — может быть IDENT::IDENT
        if tok.type == T.IDENT:
            self.advance()
            if self.match(T.SCOPE):
                ns   = tok.value
                name = self.expect(T.IDENT, "Expected name after ::").value
                return ScopedIdentifier(namespace=ns, name=name,
                                        line=tok.line, col=tok.col)
            return Identifier(name=tok.value, line=tok.line, col=tok.col)
        
        if tok.type == T.LBRACE:
            self.advance()
            elements = []
            if not self.check(T.RBRACE):
                elements.append(self.parse_expr())
                while self.match(T.COMMA):
                    elements.append(self.parse_expr())
            self.expect(T.RBRACE, "Expected '}' to close array initializer")
            return ArrayInit(elements=elements, line=tok.line, col=tok.col)

        # (expr)
        if tok.type == T.LPAREN:
            self.advance()
            expr = self.parse_expr()
            self.expect(T.RPAREN, "Expected ')' to close grouped expression")
            return expr
        
        if tok.type == T.OPEN:
            self.advance()
            name_tok = self.expect(T.IDENT, "Expected function name after open")
            if self.match(T.SCOPE):
                ns     = name_tok.value
                name   = self.expect(T.IDENT, "Expected function name after ::").value
                target = ScopedIdentifier(namespace=ns, name=name,
                                          line=name_tok.line, col=name_tok.col)
            else:
                target = Identifier(name=name_tok.value,
                                    line=name_tok.line, col=name_tok.col)
            self.expect(T.LBRACKET, "Expected '[' after function name")
            args = []
            if not self.check(T.RBRACKET):
                args.append(self.parse_expr())
                while self.match(T.COMMA):
                    args.append(self.parse_expr())
            self.expect(T.RBRACKET, "Expected ']' to close call args")
            # НЕТ expect SEMICOLON — мы внутри выражения
            return FunctionCall(target=target, args=args,
                                line=tok.line, col=tok.col)

        raise ParseError("Unexpected token in expression", tok)
    
    def parse_assign_or_expr(self, expect_semi: bool = True) -> Node:
        """Как parse_assign_or_expr_stmt, но без обязательного ';'."""
        expr = self.parse_expr()

        if self.match(T.COLON):
            value = self.parse_expr()
            if expect_semi:
                self.expect(T.SEMICOLON, "Expected ';' after assignment")
            return Assignment(target=expr, value=value)

        if expect_semi:
            self.expect(T.SEMICOLON, "Expected ';' after expression")
        return expr
    
    def parse_if(self) -> IfStmt:
        tok = self.expect(T.IF)
        self.expect(T.LBRACKET, "Expected '[' after if")
        cond = self.parse_expr()
        self.expect(T.RBRACKET, "Expected ']' to close if condition")
        self.expect(T.LPAREN,   "Expected '(' to open if body")
        body = self.parse_body()
        self.expect(T.RPAREN,   "Expected ')' to close if body")

        else_ifs  = []
        else_body = None

        while self.check(T.ELSE):
            self.advance()  # consume 'else'

            if self.check(T.IF):
                # else if [cond] ( body )
                ei_tok = self.expect(T.IF)
                self.expect(T.LBRACKET, "Expected '[' after else if")
                ei_cond = self.parse_expr()
                self.expect(T.RBRACKET, "Expected ']' to close else if condition")
                self.expect(T.LPAREN,   "Expected '(' to open else if body")
                ei_body = self.parse_body()
                self.expect(T.RPAREN,   "Expected ')' to close else if body")
                else_ifs.append(IfStmt(
                    condition = ei_cond,
                    body      = ei_body,
                    line      = ei_tok.line,
                    col       = ei_tok.col,
                ))
            else:
                # else ( body )
                self.expect(T.LPAREN, "Expected '(' to open else body")
                else_body = self.parse_body()
                self.expect(T.RPAREN, "Expected ')' to close else body")
                break  # после else ничего быть не может

        return IfStmt(
            condition = cond,
            body      = body,
            else_if   = else_ifs,
            else_body = else_body,
            line      = tok.line,
            col       = tok.col,
        )


# ─── Test ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import traceback
    import sys
    import os

    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    from boxlang6.compiler.lexer     import Lexer
    from boxlang6.compiler.ast_nodes import *


    def ok(label: str):
        print(f"  [OK] {label}")

    def fail(label: str, e: Exception):
        print(f"  [FAIL] {label}: {e}")
        traceback.print_exc()

    def parse(src: str) -> Program:
        tokens = Lexer(src).tokenize()
        return Parser(tokens).parse()

    print("=== parser.py self-test ===\n")

    # $include
    try:
        p = parse('$include <stdlib>')
        assert isinstance(p.body[0], IncludeStd)
        assert p.body[0].name == "stdlib"
        ok("$include <stdlib>")
    except Exception as e:
        fail("$include <stdlib>", e)

    try:
        p = parse('$include "myfile.box"')
        assert isinstance(p.body[0], IncludeFile)
        assert p.body[0].path == "myfile.box"
        ok('$include "myfile.box"')
    except Exception as e:
        fail('$include "myfile.box"', e)

    # shelf
    try:
        p = parse("""
        shelf MyShelf (
            num16 varA: 0;
            char b;
        )
        """)
        s = p.body[0]
        assert isinstance(s, ShelfDef)
        assert s.name == "MyShelf"
        assert len(s.fields) == 2
        assert s.fields[0].default is not None
        assert s.fields[1].default is None
        ok("shelf")
    except Exception as e:
        fail("shelf", e)

    # box без параметров
    try:
        p = parse("box _start[] : num16 ( ret 0; )")
        fn = p.body[0]
        assert isinstance(fn, FunctionDef)
        assert fn.name == "_start"
        assert str(fn.return_type) == "num16"
        assert isinstance(fn.body[0], ReturnStmt)
        ok("box _start[] : num16 ( ret 0; )")
    except Exception as e:
        fail("box _start[]", e)

    # box с параметрами
    try:
        p = parse("box myFunc[char a, num16 b] ( ret a; )")
        fn = p.body[0]
        assert len(fn.params) == 2
        assert fn.params[0].name == "a"
        assert fn.params[1].type_ref.base == "num16"
        ok("box myFunc[char a, num16 b]")
    except Exception as e:
        fail("box with params", e)

    # exit[]
    try:
        p = parse("box f[] ( exit[]; )")
        ex = p.body[0].body[0]
        assert isinstance(ex, ExitCall)
        assert ex.code is None
        ok("exit[]")
    except Exception as e:
        fail("exit[]", e)

    # exit[0]
    try:
        p = parse("box f[] ( exit[0]; )")
        ex = p.body[0].body[0]
        assert isinstance(ex, ExitCall)
        assert ex.code.value == 0
        ok("exit[0]")
    except Exception as e:
        fail("exit[0]", e)

    # while
    try:
        p = parse("box f[] ( while [a < 5] ( ret 0; ) )")
        w = p.body[0].body[0]
        assert isinstance(w, WhileLoop)
        assert w.condition.op == "<"
        ok("while [a < 5]")
    except Exception as e:
        fail("while", e)

    # for
    try:
        p = parse("box f[] ( for [num16 i: 0; i < 10; i + 1] ( ret 0; ) )")
        f = p.body[0].body[0]
        assert isinstance(f, ForLoop)
        assert isinstance(f.init, VarDecl)
        assert f.condition.op == "<"
        ok("for [num16 i: 0; i < 10; i + 1]")
    except Exception as e:
        fail("for", e)

    # asm insert
    try:
        p = parse('box f[] ( asm["mov", ax, myVar]; )')
        a = p.body[0].body[0]
        assert isinstance(a, AsmInsert)
        assert a.insn == "mov"
        assert len(a.args) == 2                          # ax и myVar
        assert isinstance(a.args[0], Identifier)
        assert a.args[0].name == "ax"
        assert isinstance(a.args[1], Identifier)
        assert a.args[1].name == "myVar"
        ok('asm["mov", ax, myVar]')
    except Exception as e:
        fail("asm insert", e)
        
    try:
        p = parse('box f[] ( asm["int.imm8", 0x22]; )')
        a = p.body[0].body[0]
        assert a.insn == "int.imm8"
        assert isinstance(a.args[0], Literal)
        assert a.args[0].value == 0x22
        ok('asm["int.imm8", 0x22]')
    except Exception as e:
        fail("asm insert 2", e)

    # open simple
    try:
        p = parse("box f[] ( open myFunc[a, b]; )")
        c = p.body[0].body[0]
        assert isinstance(c, FunctionCall)
        assert isinstance(c.target, Identifier)
        assert c.target.name == "myFunc"
        ok("open myFunc[a, b]")
    except Exception as e:
        fail("open myFunc", e)

    # open scoped
    try:
        p = parse("box f[] ( open MySpace::myFunc[a]; )")
        c = p.body[0].body[0]
        assert isinstance(c.target, ScopedIdentifier)
        assert c.target.namespace == "MySpace"
        ok("open MySpace::myFunc[a]")
    except Exception as e:
        fail("open scoped", e)

    # namespace
    try:
        p = parse("""
        namespace MySpace (
            box myFunc[char a] (
                ret a;
            )
        )
        """)
        ns = p.body[0]
        assert isinstance(ns, Namespace)
        assert ns.name == "MySpace"
        assert isinstance(ns.body[0], FunctionDef)
        ok("namespace")
    except Exception as e:
        fail("namespace", e)

    # var decl + assignment
    try:
        p = parse("box f[] ( num16 x: 5; x: x + 1; )")
        decl   = p.body[0].body[0]
        assign = p.body[0].body[1]
        assert isinstance(decl, VarDecl)
        assert isinstance(assign, Assignment)
        assert assign.value.op == "+"
        ok("VarDecl + Assignment")
    except Exception as e:
        fail("VarDecl + Assignment", e)

    # указатель и массив в типе
    try:
        p = parse("box f[] ( char* ptr: 0; bit[8] arr: 0; )")
        assert p.body[0].body[0].type_ref.pointer == True
        assert p.body[0].body[1].type_ref.array   == 8
        ok("TypeRef pointer / array")
    except Exception as e:
        fail("TypeRef pointer / array", e)

    print("\n=== done ===")
