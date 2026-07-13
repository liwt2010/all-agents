"""
Dataview Engine — Obsidian-Dataview-style query over MultiLinkGraph
PR 1 (P0-1) — implements ARCHITECTURE.md Ch.10

Syntax (EBNF, simplified):
  query     = select_clause from_clause [where_clause] [order_clause] [limit_clause] ";" ;
  select    = "SELECT" (field_list | select_expr {"," select_expr}) ;
  select_expr = field_ref ["AS" IDENT]
              | agg "(" (field_ref | "*") ")" ["FILTER" "(" "WHERE" condition ")"] ;
  agg       = "COUNT" | "AVG" | "SUM" | "MIN" | "MAX" ;
  from      = "FROM" node_type ;
  where     = "WHERE" condition ;
  condition = comparison | in_clause | steps_clause ;
  steps     = [NUMBER] "STEPS" "FROM" node_ref ;
  in_clause = field "IN" "(" query ")" ;
  comparison = field op value ;
  op        = "=" | "!=" | ">" | "<" | ">=" | "<=" ;
  value     = STRING | NUMBER | "true" | "false" | "null" ;

NOT supported (out of scope for PR 1):
  - JOIN, GROUP BY, HAVING
  - Subqueries deeper than one level
  - INSERT/UPDATE/DELETE
  - Aliases in ORDER BY
"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, Field

from agent_system.memory.graph import (
    MultiLinkGraph,
    NodeType,
    GraphNode,
)

logger = logging.getLogger(__name__)


# ─── Query I/O models ─────────────────────────────────────────────

class QueryError(Exception):
    """Query parse or execution error with location."""
    line: int
    column: int
    message: str
    hint: str | None = None

    def __init__(self, message: str, line: int = 0, column: int = 0, hint: str | None = None):
        super().__init__(f"[line {line}, col {column}] {message}" + (f" (hint: {hint})" if hint else ""))
        self.line = line
        self.column = column
        self.message = message
        self.hint = hint

    def __str__(self):
        return f"[line {self.line}, col {self.column}] {self.message}" + (f" — hint: {self.hint}" if self.hint else "")


class QueryRequest(BaseModel):
    """Input: SQL + graph + optional current node for STEPS FROM"""
    sql: str
    graph: Any | None = None  # MultiLinkGraph; injected by query() to avoid Pydantic generics
    current_node: str | None = None


class QueryResult(BaseModel):
    """Output of a Dataview query"""
    rows: list[dict[str, Any]] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    aggregations: dict[str, float] = Field(default_factory=dict)
    steps_executed: int = 0
    duration_ms: float = 0.0
    row_count: int = 0

    class Config:
        arbitrary_types_allowed = True


# ─── Tokenizer ────────────────────────────────────────────────────

class TokenType(str, Enum):
    # Keywords
    SELECT = "SELECT"
    FROM = "FROM"
    WHERE = "WHERE"
    IN = "IN"
    STEPS = "STEPS"
    ORDER = "ORDER"
    BY = "BY"
    ASC = "ASC"
    DESC = "DESC"
    LIMIT = "LIMIT"
    AS = "AS"
    FILTER = "FILTER"
    AND = "AND"
    OR = "OR"
    NOT = "NOT"
    DISTINCT = "DISTINCT"
    ALL = "ALL"
    # Aggregates
    COUNT = "COUNT"
    AVG = "AVG"
    SUM = "SUM"
    MIN = "MIN"
    MAX = "MAX"
    # Operators
    EQ = "="
    NEQ = "!="
    GT = ">"
    LT = "<"
    GTE = ">="
    LTE = "<="
    # Punctuation
    COMMA = ","
    SEMI = ";"
    LPAREN = "("
    RPAREN = ")"
    STAR = "*"
    DOT = "."
    # Literals
    IDENT = "IDENT"
    STRING = "STRING"
    NUMBER = "NUMBER"
    EOF = "EOF"


KEYWORDS = {
    "SELECT", "FROM", "WHERE", "IN", "STEPS", "ORDER", "BY", "ASC",
    "DESC", "LIMIT", "AS", "FILTER", "AND", "OR", "NOT",
    "COUNT", "AVG", "SUM", "MIN", "MAX",
    "DISTINCT", "ALL",
}


@dataclass
class Token:
    type: TokenType
    value: Any
    line: int
    column: int


_TOKEN_REGEX = re.compile(
    r"""
    (?P<ws>\s+) |
    (?P<number>\d+(?:\.\d+)?) |
    (?P<string>'[^']*'|"[^"]*") |
    (?P<ident>[A-Za-z_][A-Za-z0-9_]*) |
    (?P<op>>=|<=|!=|=|<|>) |
    (?P<punc>[,;()*.])
    """,
    re.VERBOSE,
)


def tokenize(sql: str) -> list[Token]:
    """Lex SQL string into tokens. Raises QueryError on lex failure."""
    tokens: list[Token] = []
    pos = 0
    line = 1
    col = 1

    while pos < len(sql):
        m = _TOKEN_REGEX.match(sql, pos)
        if not m:
            raise QueryError(f"Unexpected character: {sql[pos]!r}", line, col)

        last = m.lastgroup
        text = m.group()
        start_line, start_col = line, col

        if last == "ws":
            for ch in text:
                if ch == "\n":
                    line += 1
                    col = 1
                else:
                    col += 1
        elif last == "number":
            tokens.append(Token(TokenType.NUMBER, float(text) if "." in text else int(text), start_line, start_col))
            col += len(text)
        elif last == "string":
            tokens.append(Token(TokenType.STRING, text[1:-1], start_line, start_col))
            col += len(text)
        elif last == "ident":
            # Only treat as keyword if all-uppercase (allows 'count' as identifier alias for COUNT(*))
            if text.isupper() and text in KEYWORDS:
                tokens.append(Token(TokenType(text), text, start_line, start_col))
            else:
                tokens.append(Token(TokenType.IDENT, text, start_line, start_col))
            col += len(text)
        elif last == "op":
            tokens.append(Token(TokenType(text), text, start_line, start_col))
            col += len(text)
        elif last == "punc":
            mapping = {",": TokenType.COMMA, ";": TokenType.SEMI, "(": TokenType.LPAREN, ")": TokenType.RPAREN, "*": TokenType.STAR, ".": TokenType.DOT}
            tokens.append(Token(mapping[text], text, start_line, start_col))
            col += len(text)

        pos = m.end()

    tokens.append(Token(TokenType.EOF, None, line, col))
    return tokens


# ─── AST ──────────────────────────────────────────────────────────

@dataclass
class FieldRef:
    """Reference to a node field, with optional content./metadata. prefix"""
    name: str           # "agent" or "content.status" or "id"
    line: int = 0
    column: int = 0


@dataclass
class AggExpr:
    """Aggregate expression: COUNT/AVG/SUM/MIN/MAX"""
    agg: str            # COUNT/AVG/SUM/MIN/MAX
    field: FieldRef     # field or * for COUNT(*)
    alias: str | None = None
    filter: Optional["Condition"] = None  # FILTER (WHERE ...)
    line: int = 0
    column: int = 0


@dataclass
class FieldExpr:
    """Plain field selection"""
    field: FieldRef
    alias: str | None = None
    line: int = 0
    column: int = 0


@dataclass
class Comparison:
    """field op value"""
    field: FieldRef
    op: str
    value: Any
    line: int = 0
    column: int = 0


@dataclass
class InClause:
    """field IN (subquery)"""
    field: FieldRef
    subquery: "SelectStmt"
    line: int = 0
    column: int = 0


@dataclass
class StepsClause:
    """[N] STEPS FROM node_ref"""
    depth: int          # 0 means "current only", default 1
    node_ref: str       # node id or "current"
    line: int = 0
    column: int = 0


Condition = Union[Comparison, InClause, StepsClause]


@dataclass
class FromClause:
    node_type: str
    line: int = 0
    column: int = 0


@dataclass
class OrderClause:
    field: FieldRef
    descending: bool = False
    line: int = 0
    column: int = 0


@dataclass
class LimitClause:
    count: int
    line: int = 0
    column: int = 0


@dataclass
class SelectStmt:
    select_exprs: list[Union[FieldExpr, AggExpr]]
    from_clause: FromClause
    where: Condition | None = None
    order: OrderClause | None = None
    limit: LimitClause | None = None
    is_aggregation_only: bool = False  # True if all select_exprs are AggExpr


# ─── Parser (recursive descent) ──────────────────────────────────

class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.pos + offset, len(self.tokens) - 1)]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.type != TokenType.EOF:
            self.pos += 1
        return tok

    def expect(self, type: TokenType, hint: str | None = None) -> Token:
        tok = self.peek()
        if tok.type != type:
            raise QueryError(
                f"Expected {type.value}, got {tok.type.value}",
                tok.line, tok.column,
                hint=hint,
            )
        return self.advance()

    def parse(self) -> SelectStmt:
        stmt = self._parse_select_stmt()
        # Optional trailing semicolon, else EOF
        if self.peek().type == TokenType.SEMI:
            self.advance()
        if self.peek().type != TokenType.EOF:
            tok = self.peek()
            raise QueryError(f"Unexpected token {tok.type.value}", tok.line, tok.column)
        return stmt

    def _parse_select_stmt(self) -> SelectStmt:
        self.expect(TokenType.SELECT, hint="queries must start with SELECT")
        # Skip noise keywords like DISTINCT, ALL (not supported as features in PR 1)
        while self.peek().type in (TokenType.DISTINCT, TokenType.ALL):
            self.advance()
        exprs = self._parse_select_exprs()
        from_clause = self._parse_from()
        where = None
        if self.peek().type == TokenType.WHERE:
            self.advance()
            where = self._parse_condition()
        order = None
        if self.peek().type == TokenType.ORDER:
            self.advance()
            order = self._parse_order()
        limit = None
        if self.peek().type == TokenType.LIMIT:
            self.advance()
            n_tok = self.expect(TokenType.NUMBER)
            limit = LimitClause(count=int(n_tok.value), line=n_tok.line, column=n_tok.column)
        is_agg = all(isinstance(e, AggExpr) for e in exprs)
        return SelectStmt(
            select_exprs=exprs,
            from_clause=from_clause,
            where=where,
            order=order,
            limit=limit,
            is_aggregation_only=is_agg,
        )

    def _parse_select_exprs(self) -> list[Union[FieldExpr, AggExpr]]:
        exprs = [self._parse_select_expr()]
        while self.peek().type == TokenType.COMMA:
            self.advance()
            exprs.append(self._parse_select_expr())
        return exprs

    def _parse_select_expr(self) -> Union[FieldExpr, AggExpr]:
        tok = self.peek()
        if tok.type in (TokenType.COUNT, TokenType.AVG, TokenType.SUM, TokenType.MIN, TokenType.MAX):
            return self._parse_agg_expr()
        # Field reference (or STAR)
        if tok.type == TokenType.STAR:
            self.advance()
            # COUNT(*) handled above; bare * in field list not supported
            raise QueryError(
                "Bare '*' is only valid inside COUNT(*)",
                tok.line, tok.column,
                hint="use COUNT(*) or pick a field name",
            )
        field = self._parse_field_ref()
        alias = None
        if self.peek().type == TokenType.AS:
            self.advance()
            alias_tok = self.expect(TokenType.IDENT, hint="alias must be an identifier")
            alias = alias_tok.value
        return FieldExpr(field=field, alias=alias, line=field.line, column=field.column)

    def _parse_agg_expr(self) -> AggExpr:
        agg_tok = self.advance()
        self.expect(TokenType.LPAREN)
        if self.peek().type == TokenType.STAR:
            star_tok = self.advance()
            field = FieldRef(name="*", line=star_tok.line, column=star_tok.column)
            if agg_tok.type != TokenType.COUNT:
                raise QueryError(
                    f"{agg_tok.type.value}(*) not supported; only COUNT(*) allows *",
                    agg_tok.line, agg_tok.column,
                )
        else:
            field = self._parse_field_ref()
        self.expect(TokenType.RPAREN)
        alias = None
        filter_cond = None
        # ORDER matters: AS can come before or after FILTER
        # Pattern 1: agg(field) AS alias FILTER (WHERE ...)
        # Pattern 2: agg(field) FILTER (WHERE ...) AS alias
        if self.peek().type == TokenType.AS:
            self.advance()
            alias_tok = self.expect(TokenType.IDENT)
            alias = alias_tok.value
        if self.peek().type == TokenType.FILTER:
            self.advance()
            self.expect(TokenType.LPAREN)
            self.expect(TokenType.WHERE, hint="FILTER expects (WHERE ...)")
            filter_cond = self._parse_condition()
            self.expect(TokenType.RPAREN)
            # FILTER may be followed by AS
            if self.peek().type == TokenType.AS:
                self.advance()
                alias_tok = self.expect(TokenType.IDENT)
                alias = alias_tok.value
        return AggExpr(
            agg=agg_tok.type.value,
            field=field,
            alias=alias,
            filter=filter_cond,
            line=agg_tok.line,
            column=agg_tok.column,
        )

    def _parse_field_ref(self) -> FieldRef:
        tok = self.expect(TokenType.IDENT, hint="expected a field name")
        name = tok.value
        # Support content.x and metadata.x prefix via DOT token
        while self.peek().type == TokenType.DOT:
            self.advance()
            next_tok = self.expect(TokenType.IDENT, hint="expected field name after '.'")
            name = name + "." + next_tok.value
        return FieldRef(name=name, line=tok.line, column=tok.column)

    def _parse_from(self) -> FromClause:
        tok = self.expect(TokenType.FROM, hint="FROM clause is required")
        type_tok = self.expect(TokenType.IDENT, hint="node type after FROM (e.g. tasks, outputs, failures)")
        return FromClause(node_type=type_tok.value, line=tok.line, column=tok.column)

    def _parse_condition(self) -> Condition:
        # StepsClause has special syntax: [NUMBER] STEPS FROM <ident>
        if (self.peek().type == TokenType.NUMBER
                and self.peek(1).type == TokenType.STEPS):
            return self._parse_steps()
        if self.peek().type == TokenType.STEPS:
            return self._parse_steps()
        # Otherwise it's a comparison or IN clause
        field = self._parse_field_ref()
        if self.peek().type == TokenType.IN:
            in_tok = self.advance()
            self.expect(TokenType.LPAREN)
            subquery = self._parse_select_stmt_inner()
            self.expect(TokenType.RPAREN)
            return InClause(field=field, subquery=subquery, line=in_tok.line, column=in_tok.column)
        # Comparison
        op_tok = self.peek()
        if op_tok.type not in (TokenType.EQ, TokenType.NEQ, TokenType.GT, TokenType.LT, TokenType.GTE, TokenType.LTE):
            raise QueryError(
                f"Expected comparison operator, got {op_tok.type.value}",
                op_tok.line, op_tok.column,
                hint="use =, !=, >, <, >=, <=",
            )
        self.advance()
        value = self._parse_value()
        return Comparison(field=field, op=op_tok.value, value=value, line=op_tok.line, column=op_tok.column)

    def _parse_steps(self) -> StepsClause:
        depth = 1
        line, column = self.peek().line, self.peek().column
        # Support both [N] STEPS FROM and STEPS FROM (depth=1) syntax
        if self.peek().type == TokenType.NUMBER:
            n_tok = self.advance()
            depth = int(n_tok.value)
            self.expect(TokenType.STEPS, hint="expected STEPS after N")
        else:
            self.expect(TokenType.STEPS)
        self.expect(TokenType.FROM)
        node_ref_tok = self.peek()
        if node_ref_tok.type not in (TokenType.IDENT, TokenType.STRING):
            raise QueryError(
                "STEPS FROM expects a node id or 'current'",
                node_ref_tok.line, node_ref_tok.column,
            )
        self.advance()
        node_ref = node_ref_tok.value
        return StepsClause(depth=depth, node_ref=node_ref, line=line, column=column)

    def _parse_value(self) -> Any:
        tok = self.peek()
        if tok.type == TokenType.STRING:
            self.advance()
            return tok.value
        if tok.type == TokenType.NUMBER:
            self.advance()
            return tok.value
        if tok.type == TokenType.IDENT:
            self.advance()
            if tok.value.lower() == "true":
                return True
            if tok.value.lower() == "false":
                return False
            if tok.value.lower() == "null":
                return None
            # Bare ident — treat as identifier reference (for STEPS FROM node_id)
            return tok.value
        raise QueryError(
            f"Expected literal value, got {tok.type.value}",
            tok.line, tok.column,
        )

    def _parse_order(self) -> OrderClause:
        self.expect(TokenType.BY)
        field = self._parse_field_ref()
        descending = False
        if self.peek().type == TokenType.DESC:
            self.advance()
            descending = True
        elif self.peek().type == TokenType.ASC:
            self.advance()
        return OrderClause(field=field, descending=descending, line=field.line, column=field.column)

    def _parse_select_stmt_inner(self) -> SelectStmt:
        """Parse SELECT ... FROM ... ; but tolerate missing semicolon (used inside IN (subquery))"""
        stmt = self._parse_select_stmt_no_semi()
        return stmt

    def _parse_select_stmt_no_semi(self) -> SelectStmt:
        """Like _parse_select_stmt but no semicolon expected at end."""
        self.expect(TokenType.SELECT)
        # Skip noise keywords like DISTINCT, ALL (not supported as features in PR 1)
        while self.peek().type in (TokenType.DISTINCT, TokenType.ALL):
            self.advance()
        exprs = self._parse_select_exprs()
        from_clause = self._parse_from()
        where = None
        if self.peek().type == TokenType.WHERE:
            self.advance()
            where = self._parse_condition()
        order = None
        if self.peek().type == TokenType.ORDER:
            self.advance()
            order = self._parse_order()
        limit = None
        if self.peek().type == TokenType.LIMIT:
            self.advance()
            n_tok = self.expect(TokenType.NUMBER)
            limit = LimitClause(count=int(n_tok.value), line=n_tok.line, column=n_tok.column)
        is_agg = all(isinstance(e, AggExpr) for e in exprs)
        return SelectStmt(
            select_exprs=exprs,
            from_clause=from_clause,
            where=where,
            order=order,
            limit=limit,
            is_aggregation_only=is_agg,
        )


# ─── Executor ─────────────────────────────────────────────────────

def _resolve_field(node: GraphNode, field_ref: FieldRef) -> Any:
    """Resolve a FieldRef against a GraphNode. Supports content.x / metadata.x / id / type / created_at / updated_at."""
    name = field_ref.name
    if name == "*":
        return None
    if name == "id":
        return node.id
    if name == "type":
        return node.type.value if hasattr(node.type, "value") else str(node.type)
    if name == "created_at":
        return node.created_at.isoformat() if node.created_at else None
    if name == "updated_at":
        return node.updated_at.isoformat() if node.updated_at else None
    if name.startswith("content."):
        return node.content.get(name[len("content."):])
    if name.startswith("metadata."):
        return node.metadata.get(name[len("metadata."):])
    # Default: try content first, then metadata
    if name in node.content:
        return node.content[name]
    if name in node.metadata:
        return node.metadata[name]
    return None


def _display_field_name(field: FieldRef) -> str:
    """Return the display name for a FieldRef — strips 'content.'/'metadata.' prefix.
    Used as the default row/column key when no alias is given."""
    name = field.name
    if name.startswith("content."):
        return name[len("content."):]
    if name.startswith("metadata."):
        return name[len("metadata."):]
    return name


def _agg_key(expr: AggExpr) -> str:
    """Default key for an aggregate expression result."""
    if expr.alias:
        return expr.alias
    if expr.field.name == "*":
        return expr.agg.lower()
    return f"{expr.agg.lower()}_{expr.field.name}"


def _node_matches_condition(node: GraphNode, cond: Condition, graph: MultiLinkGraph, current_node: str | None) -> bool:
    if isinstance(cond, Comparison):
        actual = _resolve_field(node, cond.field)
        return _compare(actual, cond.op, cond.value)
    if isinstance(cond, InClause):
        # Run subquery, collect all values from all fields in result rows
        sub_result = execute_query(cond.subquery, graph, current_node)
        values: set = set()
        for row in sub_result.rows:
            for v in row.values():
                if v is not None:
                    values.add(v)
        actual = _resolve_field(node, cond.field)
        return actual in values
    if isinstance(cond, StepsClause):
        target_id = cond.node_ref
        if target_id == "current":
            target_id = current_node
        if not target_id:
            return False
        if target_id == node.id:
            return cond.depth >= 0
        if cond.depth == 0:
            return False
        # Use MultiLinkGraph.neighbors to traverse
        try:
            neighbors = graph.neighbors(target_id, depth=cond.depth, max_depth=cond.depth)
            neighbor_ids = {n.node.id for n in neighbors}
            return node.id in neighbor_ids
        except Exception:
            return False
    return False


def _compare(actual: Any, op: str, expected: Any) -> bool:
    """Compare actual to expected. Handle None carefully."""
    if actual is None:
        if op == "=":
            return expected is None
        if op == "!=":
            return expected is not None
        return False
    try:
        if op == "=":
            return actual == expected
        if op == "!=":
            return actual != expected
        if op == ">":
            return actual > expected
        if op == "<":
            return actual < expected
        if op == ">=":
            return actual >= expected
        if op == "<=":
            return actual <= expected
    except TypeError:
        return False
    return False


def _parse_node_type(type_str: str) -> NodeType:
    """Parse a user-provided node type string into NodeType enum.
    Accepts both singular (task) and plural (tasks) forms — SQL convention.
    Case-insensitive.
    """
    type_lower = type_str.lower().strip()
    # Direct match
    for nt in NodeType:
        if nt.value == type_lower:
            return nt
    # Plural form: strip trailing 's' and try again
    if type_lower.endswith("s") and len(type_lower) > 3:
        singular = type_lower[:-1]
        for nt in NodeType:
            if nt.value == singular:
                return nt
    raise QueryError(
        f"Unknown node type: {type_str!r}",
        hint=f"valid types: {', '.join(nt.value for nt in NodeType)} (singular or plural)",
    )


def execute_query(stmt: SelectStmt, graph: MultiLinkGraph, current_node: str | None = None) -> QueryResult:
    """Execute a parsed SelectStmt against a MultiLinkGraph."""
    start = time.perf_counter()
    steps_executed = 0

    # 1. FROM: get candidate nodes
    try:
        node_type = _parse_node_type(stmt.from_clause.node_type)
    except QueryError:
        raise
    candidates = graph.find_nodes(node_type=node_type)
    steps_executed += 1

    # 2. WHERE: filter
    if stmt.where is not None:
        candidates = [n for n in candidates if _node_matches_condition(n, stmt.where, graph, current_node)]
        steps_executed += 1

    # 3. SELECT: build rows or aggregations
    rows: list[dict[str, Any]] = []
    aggregations: dict[str, float] = {}

    if stmt.is_aggregation_only:
        # Compute aggregates
        for expr in stmt.select_exprs:
            assert isinstance(expr, AggExpr)
            key = _agg_key(expr)
            agg_value = _compute_agg(expr, candidates, graph, current_node)
            aggregations[key] = agg_value
            steps_executed += 1
    else:
        for node in candidates:
            row: dict[str, Any] = {}
            for expr in stmt.select_exprs:
                if isinstance(expr, FieldExpr):
                    val = _resolve_field(node, expr.field)
                    key = expr.alias or _display_field_name(expr.field)
                    row[key] = val
                elif isinstance(expr, AggExpr):
                    # Mixed: compute per-row (won't be useful, but allowed)
                    val = _compute_agg(expr, [node], graph, current_node)
                    key = _agg_key(expr)
                    row[key] = val
            rows.append(row)

    # 4. ORDER
    if stmt.order is not None and rows:
        field_name = stmt.order.field.name
        try:
            rows.sort(key=lambda r: (r.get(field_name) is None, r.get(field_name)), reverse=stmt.order.descending)
        except TypeError:
            # Mixed types — sort by string repr as fallback
            rows.sort(key=lambda r: str(r.get(field_name)), reverse=stmt.order.descending)
        steps_executed += 1

    # 5. LIMIT
    if stmt.limit is not None:
        rows = rows[: stmt.limit.count]
        steps_executed += 1

    duration_ms = round((time.perf_counter() - start) * 1000, 3)

    # Build columns from select_exprs
    columns: list[str] = []
    for expr in stmt.select_exprs:
        if isinstance(expr, FieldExpr):
            columns.append(expr.alias or _display_field_name(expr.field))
        elif isinstance(expr, AggExpr):
            columns.append(_agg_key(expr))

    return QueryResult(
        rows=rows,
        columns=columns,
        aggregations=aggregations,
        steps_executed=steps_executed,
        duration_ms=duration_ms,
        row_count=len(rows),
    )


def _compute_agg(expr: AggExpr, nodes: list[GraphNode], graph: MultiLinkGraph, current_node: str | None) -> float:
    """Compute an aggregate value. FILTER narrows the input set."""
    filtered = nodes
    if expr.filter is not None:
        filtered = [n for n in nodes if _node_matches_condition(n, expr.filter, graph, current_node)]

    if expr.agg == "COUNT":
        if expr.field.name == "*":
            return float(len(filtered))
        # COUNT(field) — count non-null
        return float(sum(1 for n in filtered if _resolve_field(n, expr.field) is not None))

    if expr.field.name == "*":
        raise QueryError(f"{expr.agg}(*) not supported; specify a numeric field")

    values: list[float] = []
    for n in filtered:
        v = _resolve_field(n, expr.field)
        if v is None:
            continue
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            continue

    if not values:
        return 0.0

    if expr.agg == "AVG":
        return sum(values) / len(values)
    if expr.agg == "SUM":
        return sum(values)
    if expr.agg == "MIN":
        return min(values)
    if expr.agg == "MAX":
        return max(values)
    raise QueryError(f"Unknown aggregate: {expr.agg}")


# ─── Top-level API ────────────────────────────────────────────────

def query(sql: str, graph: MultiLinkGraph | None = None, current_node: str | None = None) -> QueryResult:
    """
    Parse and execute a Dataview SQL query.

    Usage:
        from agent_system.core.dataview import query
        result = query("SELECT COUNT(*) FROM tasks;", graph=g)
    """
    if graph is None:
        from agent_system.memory.graph import get_graph
        graph = get_graph()
    tokens = tokenize(sql)
    parser = Parser(tokens)
    stmt = parser.parse()
    return execute_query(stmt, graph, current_node)


# ─── Builder (chainable, type-safe wrapper over query()) ──────────

class Query:
    """
    Thin builder over the SQL engine. Builds a SQL string internally.

    Usage:
        from agent_system.core.dataview import Query
        q = Query(graph).from_("tasks").where(status="completed").count()
        result = q.execute()
    """

    def __init__(self, graph: MultiLinkGraph):
        self._graph = graph
        self._from: str | None = None
        self._where_clauses: list[str] = []
        self._select_parts: list[str] = []
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None

    def from_(self, node_type: str) -> "Query":
        self._from = node_type
        return self

    def where(self, **filters: Any) -> "Query":
        for key, value in filters.items():
            if isinstance(value, str):
                self._where_clauses.append(f"{key} = '{value}'")
            elif isinstance(value, bool):
                self._where_clauses.append(f"{key} = {str(value).lower()}")
            elif value is None:
                self._where_clauses.append(f"{key} = null")
            else:
                self._where_clauses.append(f"{key} = {value}")
        return self

    def count(self, field: str = "*", alias: str = "count") -> "Query":
        self._select_parts.append(f"COUNT({field}) AS {alias}")
        return self

    def avg(self, field: str, alias: str | None = None) -> "Query":
        self._select_parts.append(f"AVG({field})" + (f" AS {alias}" if alias else ""))
        return self

    def sum(self, field: str, alias: str | None = None) -> "Query":
        self._select_parts.append(f"SUM({field})" + (f" AS {alias}" if alias else ""))
        return self

    def select(self, *fields: str) -> "Query":
        self._select_parts.extend(fields)
        return self

    def order_by(self, field: str, descending: bool = False) -> "Query":
        self._order = (field, descending)
        return self

    def limit(self, n: int) -> "Query":
        self._limit = n
        return self

    def steps_from(self, depth: int, node_ref: str = "current") -> "Query":
        self._where_clauses.append(f"{depth} STEPS FROM {node_ref}")
        return self

    def _build_sql(self) -> str:
        if not self._from:
            raise QueryError("FROM clause is required")
        if not self._select_parts:
            raise QueryError("SELECT clause is required (use select/count/avg/sum)")
        sql_parts = ["SELECT", ", ".join(self._select_parts), "FROM", self._from]
        if self._where_clauses:
            sql_parts.append("WHERE")
            sql_parts.append(" AND ".join(self._where_clauses))
        if self._order:
            field, desc = self._order
            sql_parts.append(f"ORDER BY {field} {'DESC' if desc else 'ASC'}")
        if self._limit is not None:
            sql_parts.append(f"LIMIT {self._limit}")
        return " ".join(sql_parts) + ";"

    def execute(self) -> QueryResult:
        return query(self._build_sql(), graph=self._graph)