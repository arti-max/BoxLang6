from .ast_nodes import Program


class Optimizer:
    """
    Pass-through оптимизатор.
    TODO: constant folding, dead code elimination, peephole.
    """
    def optimize(self, program: Program) -> Program:
        return program
