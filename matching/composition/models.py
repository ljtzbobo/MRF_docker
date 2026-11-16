from dataclasses import dataclass,field


@dataclass(frozen=True)
class Signal:
    format: str
    rate_hz: float | None
    length: int | None


@dataclass(frozen=True)
class Function:
    id: str
    capability: str
    predecessors: tuple[str,...]=()
    parameters: dict=field(default_factory=dict)
    input_format: str | None=None
    output_format: str | None=None
    port_dependencies: dict=field(default_factory=dict)


@dataclass(frozen=True)
class Operator:
    id: str
    cid: str
    capability: str
    inputs: dict[str,tuple[str,...]]
    outputs: tuple[str,...]
    parameters: dict=field(default_factory=dict)
    kind: str='declared'
    automatic: bool=False
    cost: float=1.0


@dataclass(frozen=True)
class Token:
    id: str
    signal: Signal
    completed: frozenset[str]=frozenset()
    producer: str | None=None
    source: str='input'


@dataclass(frozen=True)
class Step:
    id: str
    function_id: str | None
    operator: Operator
    node: object
    binding: object
    inputs: dict[str,str]
    output: Token


@dataclass(frozen=True)
class State:
    tokens: tuple[Token,...]
    steps: tuple[Step,...]=()
    assignments: tuple[tuple[str,str],...]=()
    cost: float=0.0


@dataclass
class SearchOutcome:
    solutions: list
    statistics: dict
