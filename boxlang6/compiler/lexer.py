import re
from dataclasses import dataclass
from typing import List, Optional


# ─── Token types ─────────────────────────────────────────────────────────────

class T:
    # Literals
    NUMBER      = "NUMBER"       # 42, 0xFF, 0b1010
    CHAR_LIT    = "CHAR_LIT"     # 'a'
    STRING_LIT  = "STRING_LIT"   # "add ax, %v"

    # Keywords
    BOX         = "BOX"          # box
    OPEN        = "OPEN"         # open
    RET         = "RET"          # ret
    EXIT        = "EXIT"         # exit
    WHILE       = "WHILE"        # while
    FOR         = "FOR"          # for
    NAMESPACE   = "NAMESPACE"    # namespace
    SHELF       = "SHELF"        # shelf
    ASM         = "ASM"          # asm
    INCLUDE     = "INCLUDE"      # $include
    USE         = "USE"          # $use
    IF          = "IF"           # if
    ELSE        = "ELSE"         # else
    LABEL_DEF   = "LABEL_DEF"    # @fs_error:
    LABEL_REF   = "LABEL_REF"    # @fs_error
    SHELVE      = "SHELVE"       # shelve

    # Types
    TYPE        = "TYPE"         # bit, bit2, bit4, char, num16, num32

    # Identifiers
    IDENT       = "IDENT"        # myVar, _start, etc.

    # Punctuation
    LPAREN      = "LPAREN"       # (
    RPAREN      = "RPAREN"       # )
    LBRACKET    = "LBRACKET"     # [
    RBRACKET    = "RBRACKET"     # ]
    LBRACE      = "LBRACE"       # {
    RBRACE      = "RBRACE"       # }
    LANGLE      = "LANGLE"       # <  (для $include <stdlib>)
    RANGLE      = "RANGLE"       # >
    COLON       = "COLON"        # :
    SEMICOLON   = "SEMICOLON"    # ;
    COMMA       = "COMMA"        # ,
    DOT         = "DOT"          # .
    SCOPE       = "SCOPE"        # ::
    DOLLAR      = "DOLLAR"       # $ (часть $include, но парсим как keyword)
    PERCENT     = "PERCENT"      # % (для %v в asm)
    AMP         = "AMP"          # &
    STAR        = "STAR"         # *

    # Operators
    PLUS        = "PLUS"         # +
    MINUS       = "MINUS"        # -
    SLASH       = "SLASH"        # /
    EQ          = "EQ"           # ==
    NEQ         = "NEQ"          # !=
    LT          = "LT"           # <
    GT          = "GT"           # >
    LTE         = "LTE"          # <=
    GTE         = "GTE"          # >=
    ASSIGN      = "ASSIGN"       # = (если понадобится отдельно от :)
    BANG        = "BANG"         # !

    # Special
    EOF         = "EOF"
    NEWLINE     = "NEWLINE"      # для отладки, обычно скипается


# ─── Token ───────────────────────────────────────────────────────────────────

@dataclass
class Token:
    type:  str
    value: str
    line:  int
    col:   int

    def __repr__(self):
        return f"Token({self.type}, {self.value!r}, {self.line}:{self.col})"


# ─── Lexer ───────────────────────────────────────────────────────────────────

KEYWORDS = {
    "box":       T.BOX,
    "open":      T.OPEN,
    "ret":       T.RET,
    "exit":      T.EXIT,
    "while":     T.WHILE,
    "for":       T.FOR,
    "namespace": T.NAMESPACE,
    "shelf":     T.SHELF,
    "asm":       T.ASM,
    "if":        T.IF,
    "else":      T.ELSE,
    "shelve":    T.SHELVE,
}

TYPES = {"bit", "bit2", "bit4", "char", "num16", "num32"}


class LexerError(Exception):
    def __init__(self, msg: str, line: int, col: int):
        super().__init__(f"[Lexer] {msg} at {line}:{col}")
        self.line = line
        self.col  = col


class Lexer:
    def __init__(self, source: str):
        self.source = source
        self.pos    = 0
        self.line   = 1
        self.col    = 1
        self.tokens: List[Token] = []

    # ── helpers ──────────────────────────────────────────────────────────────

    def peek(self, offset: int = 0) -> Optional[str]:
        i = self.pos + offset
        return self.source[i] if i < len(self.source) else None

    def advance(self) -> str:
        ch = self.source[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def match(self, expected: str) -> bool:
        """Consume next char if it matches expected."""
        if self.peek() == expected:
            self.advance()
            return True
        return False

    def add(self, type_: str, value: str, line: int, col: int):
        self.tokens.append(Token(type_, value, line, col))

    # ── skip ─────────────────────────────────────────────────────────────────

    def skip_whitespace(self):
        while self.peek() in (" ", "\t", "\r", "\n"):
            self.advance()

    def skip_line_comment(self):
        """// comment"""
        while self.peek() and self.peek() != "\n":
            self.advance()

    def skip_block_comment(self):
        """/* comment */  - на будущее"""
        while self.peek():
            if self.peek() == "*" and self.peek(1) == "/":
                self.advance(); self.advance()
                return
            self.advance()
        raise LexerError("Unclosed block comment", self.line, self.col)

    # ── scanners ─────────────────────────────────────────────────────────────
    
    def read_label(self, line: int, col: int) -> Token:
        """@name или @name: - метка или ссылка на метку."""
        buf = ""
        while self.peek() and (self.peek().isalnum() or self.peek() == "_"):
            buf += self.advance()
        if not buf:
            raise LexerError("Expected label name after '@'", line, col)
        # смотрим — есть ли ':' после имени
        if self.peek() == ":":
            self.advance()
            return Token(T.LABEL_DEF, buf, line, col)
        return Token(T.LABEL_REF, buf, line, col)

    def read_number(self, line: int, col: int) -> Token:
        """
        Поддерживает: 42  0xFF  0b1010  0o77
        """
        start = self.pos - 1        # первый символ уже advance()-нут
        buf   = self.source[start]

        if buf == "0" and self.peek() in ("x", "X", "b", "B", "o", "O"):
            buf += self.advance()   # prefix
            while self.peek() and (self.peek() in "0123456789abcdefABCDEF_"):
                buf += self.advance()
        else:
            while self.peek() and self.peek().isdigit():
                buf += self.advance()

        return Token(T.NUMBER, buf, line, col)

    def read_char_lit(self, line: int, col: int) -> Token:
        """'a'  — один символ, эскейпы пока не поддерживаем."""
        ch = self.advance()
        if self.peek() != "'":
            raise LexerError(f"Expected closing ' after char literal", line, col)
        self.advance()  # closing '
        return Token(T.CHAR_LIT, ch, line, col)

    def read_string_lit(self, line: int, col: int) -> Token:
        buf = ""
        while self.peek() and self.peek() != '"':
            ch = self.advance()
            if ch == "\\":
                esc = self.advance()
                if esc == "x":
                    # \xNN — два hex символа
                    h1 = self.advance()
                    h2 = self.advance()
                    buf += chr(int(h1 + h2, 16))
                else:
                    buf += {"n": "\n", "t": "\t", "\\": "\\", "r": "\r", "0": "\x00"}.get(esc, esc)
            else:
                buf += ch
        if not self.peek():
            raise LexerError("Unclosed string literal", line, col)
        self.advance()  # closing "
        return Token(T.STRING_LIT, buf, line, col)

    def read_ident_or_keyword(self, first: str, line: int, col: int) -> Token:
        buf = first
        while self.peek() and (self.peek().isalnum() or self.peek() == "_"):
            buf += self.advance()

        if buf in KEYWORDS:
            return Token(KEYWORDS[buf], buf, line, col)
        if buf in TYPES:
            return Token(T.TYPE, buf, line, col)
        return Token(T.IDENT, buf, line, col)

    def read_include(self, line: int, col: int) -> Token:
        buf = "$"
        while self.peek() and self.peek().isalpha():
            buf += self.advance()
        
        if buf == "$include":
            return Token(T.INCLUDE, buf, line, col)
        if buf == "$use":
            return Token(T.USE, buf, line, col)    # ← добавить
        
        raise LexerError(f"Unknown directive {buf!r}", line, col)

    # ── main tokenize ─────────────────────────────────────────────────────────

    def tokenize(self) -> List[Token]:
        while self.pos < len(self.source):
            self.skip_whitespace()
            if self.pos >= len(self.source):
                break

            line, col = self.line, self.col
            ch = self.advance()

            # Comments
            if ch == "/" and self.peek() == "/":
                self.skip_line_comment()
                continue
            if ch == "/" and self.peek() == "*":
                self.advance()
                self.skip_block_comment()
                continue

            # Directive
            if ch == "$":
                tok = self.read_include(line, col)
                self.tokens.append(tok)
                continue

            # String / Char
            if ch == '"':
                self.tokens.append(self.read_string_lit(line, col))
                continue
            if ch == "'":
                self.tokens.append(self.read_char_lit(line, col))
                continue

            # Number
            if ch.isdigit():
                self.tokens.append(self.read_number(line, col))
                continue
            
            if ch == "@":
                self.tokens.append(self.read_label(line, col))
                continue

            # Ident / keyword / type
            if ch.isalpha() or ch == "_":
                self.tokens.append(self.read_ident_or_keyword(ch, line, col))
                continue

            # Two-char operators first
            if ch == ":" and self.peek() == ":":
                self.advance()
                self.add(T.SCOPE, "::", line, col)
                continue
            if ch == "=" and self.peek() == "=":
                self.advance()
                self.add(T.EQ, "==", line, col)
                continue
            if ch == "!" and self.peek() == "=":
                self.advance()
                self.add(T.NEQ, "!=", line, col)
                continue
            if ch == "<" and self.peek() == "=":
                self.advance()
                self.add(T.LTE, "<=", line, col)
                continue
            if ch == ">" and self.peek() == "=":
                self.advance()
                self.add(T.GTE, ">=", line, col)
                continue

            # Single-char
            single = {
                "(": T.LPAREN,  ")": T.RPAREN,
                "[": T.LBRACKET,"]": T.RBRACKET,
                "<": T.LT,      ">": T.GT,
                ":": T.COLON,   ";": T.SEMICOLON,
                ",": T.COMMA,   ".": T.DOT,
                "+": T.PLUS,    "-": T.MINUS,
                "*": T.STAR,    "/": T.SLASH,
                "&": T.AMP,     "!": T.BANG,
                "%": T.PERCENT,
                "{": T.LBRACE, "}": T.RBRACE,
            }
            if ch in single:
                self.add(single[ch], ch, line, col)
                continue

            raise LexerError(f"Unexpected character {ch!r}", line, col)

        self.add(T.EOF, "", self.line, self.col)
        return self.tokens



#test
if __name__ == "__main__":
    import traceback
    import sys, os
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    from boxlang6.compiler.lexer import Lexer, Token, T, LexerError

    def ok(label: str):
        print(f"  [OK] {label}")

    def fail(label: str, e: Exception):
        print(f"  [FAIL] {label}: {e}")
        traceback.print_exc()

    def lex(src: str) -> List[Token]:
        return Lexer(src).tokenize()

    def types(src: str) -> List[str]:
        return [t.type for t in lex(src) if t.type != T.EOF]

    print("=== lexer.py self-test ===\n")

    # Keywords
    try:
        toks = types("box open ret exit while for namespace shelf asm")
        assert toks == [T.BOX, T.OPEN, T.RET, T.EXIT,
                        T.WHILE, T.FOR, T.NAMESPACE, T.SHELF, T.ASM]
        ok("Keywords")
    except Exception as e:
        fail("Keywords", e)

    # Types
    try:
        toks = types("bit bit2 bit4 char num16 num32")
        assert all(t == T.TYPE for t in toks)
        ok("Type keywords")
    except Exception as e:
        fail("Type keywords", e)

    # Numbers
    try:
        toks = lex("42 0xFF 0b1010 0o77")
        vals = [t.value for t in toks if t.type == T.NUMBER]
        assert vals == ["42", "0xFF", "0b1010", "0o77"]
        ok("Numbers (dec / hex / bin / oct)")
    except Exception as e:
        fail("Numbers", e)

    # Char literal
    try:
        toks = lex("'a'")
        assert toks[0].type  == T.CHAR_LIT
        assert toks[0].value == "a"
        ok("Char literal")
    except Exception as e:
        fail("Char literal", e)

    # String literal — убрать %v, оставить просто строку
    try:
        toks = lex('"mov"')
        assert toks[0].type  == T.STRING_LIT
        assert toks[0].value == "mov"
        ok("String literal")
    except Exception as e:
        fail("String literal", e)
        
    # $use
    try:
        toks = lex("$use x16pros")
        assert toks[0].type  == T.USE
        assert toks[1].type  == T.IDENT
        assert toks[1].value == "x16pros"
        ok("$use directive")
    except Exception as e:
        fail("$use", e)
        
    # & амперсанд
    try:
        toks = types("&myFunc")
        assert toks == [T.AMP, T.IDENT]
        ok("& address-of")
    except Exception as e:
        fail("& address-of", e)


    # Scope operator ::
    try:
        toks = types("MySpace::myFunc")
        assert toks == [T.IDENT, T.SCOPE, T.IDENT]
        ok("Scope operator ::")
    except Exception as e:
        fail("Scope ::", e)

    # Two-char operators
    try:
        toks = types("== != <= >=")
        assert toks == [T.EQ, T.NEQ, T.LTE, T.GTE]
        ok("Two-char operators")
    except Exception as e:
        fail("Two-char operators", e)

    # $include
    try:
        toks = lex("$include")
        assert toks[0].type == T.INCLUDE
        ok("$include directive")
    except Exception as e:
        fail("$include", e)

    # Line comment skip
    try:
        toks = types("num16 // this is a comment\nchar")
        assert toks == [T.TYPE, T.TYPE]
        ok("Line comment skip")
    except Exception as e:
        fail("Line comment skip", e)

    # Full snippet: box _start[] : num16 ( exit[0]; )
    try:
        src = "box _start[] : num16 (\n    exit[0];\n)"
        toks = lex(src)
        tt   = [t.type for t in toks]
        assert tt == [
            T.BOX, T.IDENT,
            T.LBRACKET, T.RBRACKET,
            T.COLON, T.TYPE,
            T.LPAREN,
            T.EXIT, T.LBRACKET, T.NUMBER, T.RBRACKET, T.SEMICOLON,
            T.RPAREN,
            T.EOF,
        ]
        ok("Full snippet: box _start[] : num16 ( exit[0]; )")
    except Exception as e:
        fail("Full snippet", e)

    # ExitCall с переменной: exit[myVar]
    try:
        toks = types("exit[myVar]")
        assert toks == [T.EXIT, T.LBRACKET, T.IDENT, T.RBRACKET]
        ok("exit[myVar]")
    except Exception as e:
        fail("exit[myVar]", e)

    # Unexpected char → LexerError
    try:
        lex("num16 @bad")
        fail("LexerError on @", Exception("No error raised"))
    except LexerError:
        ok("LexerError on unknown char '@'")
    except Exception as e:
        fail("LexerError on @", e)

    print("\n=== done ===")
