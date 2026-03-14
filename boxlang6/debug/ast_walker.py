from __future__ import annotations
from typing import Any
from ..compiler.ast_nodes import *


def ast_to_dict(node: Any) -> Any:
    """
    Рекурсивно превращает AST в JSON-сериализуемый словарь.
    Используется для отправки дерева клиенту.
    """
    if isinstance(node, list):
        return [ast_to_dict(n) for n in node]

    if not isinstance(node, Node):
        return node

    result = {
        "_type": type(node).__name__,
        "_line": getattr(node, "line", 0),
        "_col":  getattr(node, "col",  0),
    }

    for key, val in vars(node).items():
        if key in ("line", "col"):
            continue
        result[key] = ast_to_dict(val)

    return result
