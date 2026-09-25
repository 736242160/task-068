#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
query_tool.py — 自定义查询语言工具（单文件，仅 Python 标准库）

用法:
    python3 query_tool.py                      # 运行内置示例（含错误定位样例）
    python3 query_tool.py 数据文件 查询文件      # 对自定义数据执行自定义查询

查询语法（关键字不区分大小写）:
    SELECT <字段1, 字段2, ... | *>
        [WHERE <条件>]
        [ORDER BY <字段> [ASC | DESC]]
        [LIMIT <N>]

    条件: 字段 = 值 | 字段 > 数值 | 字段 < 数值 | 字段 CONTAINS "子串"
          条件可用 AND / OR 组合，可用括号改变优先级（AND 优先于 OR）。
          字符串值必须用双引号；数值直接书写（支持负数与小数）。

执行顺序（固定）:
    1. WHERE  过滤数据行
    2. ORDER BY 对过滤结果排序
    3. LIMIT  截取前 N 条
    4. SELECT 投影（选择输出列）并输出

错误报告:
    - 语法错误（关键字拼错、条件缺字段名、括号不配对、字符串未闭合等）
      报告出错的第几个字符（从 1 开始计数），并用 ^ 标出位置。
    - 字段名在表中不存在、排序字段缺失值、对非数值字段做大小比较，
      均会报告具体错误。
"""

import csv
import io
import sys


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------

class QueryError(Exception):
    """查询错误。pos 为出错字符位置（1 起始），语义错误可为 None。"""

    def __init__(self, message, pos=None):
        super().__init__(message)
        self.message = message
        self.pos = pos


# ---------------------------------------------------------------------------
# 词法分析
# ---------------------------------------------------------------------------

class Token:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind, value, pos):
        self.kind = kind      # IDENT/NUMBER/STRING/OP/COMMA/LPAREN/RPAREN/STAR/EOF
        self.value = value
        self.pos = pos        # 1 起始的字符位置

    def __repr__(self):
        return "Token(%s, %r, %d)" % (self.kind, self.value, self.pos)


def tokenize(text):
    tokens = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                buf.append(text[j])
                j += 1
            if j >= n:
                raise QueryError("字符串未闭合（缺少结束双引号）", i + 1)
            tokens.append(Token("STRING", "".join(buf), i + 1))
            i = j + 1
            continue
        if ch.isdigit() or (ch == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i + (1 if ch == "-" else 0)
            while j < n and (text[j].isdigit() or text[j] == "."):
                j += 1
            try:
                value = float(text[i:j])
            except ValueError:
                raise QueryError("非法的数值 %r" % text[i:j], i + 1)
            tokens.append(Token("NUMBER", value, i + 1))
            i = j
            continue
        if ch in "=><":
            tokens.append(Token("OP", ch, i + 1))
            i += 1
            continue
        if ch == ",":
            tokens.append(Token("COMMA", ch, i + 1))
            i += 1
            continue
        if ch == "(":
            tokens.append(Token("LPAREN", ch, i + 1))
            i += 1
            continue
        if ch == ")":
            tokens.append(Token("RPAREN", ch, i + 1))
            i += 1
            continue
        if ch == "*":
            tokens.append(Token("STAR", ch, i + 1))
            i += 1
            continue
        if ch.isalpha() or ch == "_" or ord(ch) > 127:
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_" or ord(text[j]) > 127):
                j += 1
            tokens.append(Token("IDENT", text[i:j], i + 1))
            i = j
            continue
        raise QueryError("无法识别的字符 %r" % ch, i + 1)
    tokens.append(Token("EOF", None, n + 1))
    return tokens


# ---------------------------------------------------------------------------
# 语法分析（递归下降）
# ---------------------------------------------------------------------------

KEYWORDS = {"SELECT", "WHERE", "AND", "OR", "CONTAINS",
            "ORDER", "BY", "ASC", "DESC", "LIMIT"}


def _describe(tok):
    if tok.kind == "EOF":
        return "语句结束"
    return repr(tok.value)


class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.i = 0

    def peek(self):
        return self.tokens[self.i]

    def advance(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def is_keyword(self, tok, word):
        return tok.kind == "IDENT" and tok.value.upper() == word

    def expect_keyword(self, word):
        tok = self.peek()
        if self.is_keyword(tok, word):
            self.advance()
            return tok
        raise QueryError(
            "期望关键字 %s，但发现 %s（请检查关键字拼写）" % (word, _describe(tok)),
            tok.pos)

    def expect_field(self, what):
        tok = self.peek()
        if tok.kind == "IDENT" and tok.value.upper() not in KEYWORDS:
            self.advance()
            return tok.value
        raise QueryError("%s缺少字段名，但发现 %s" % (what, _describe(tok)), tok.pos)


def parse_query(text):
    """解析查询文本，返回查询字典；语法错误抛出带字符位置的 QueryError。"""
    parser = Parser(tokenize(text))
    parser.expect_keyword("SELECT")

    # 选择列表
    if parser.peek().kind == "STAR":
        parser.advance()
        fields = "*"
    else:
        fields = [parser.expect_field("SELECT 后")]
        while parser.peek().kind == "COMMA":
            parser.advance()
            fields.append(parser.expect_field("逗号后"))

    # WHERE
    cond = None
    if parser.is_keyword(parser.peek(), "WHERE"):
        parser.advance()
        cond = parse_or(parser)

    # ORDER BY
    order = None
    if parser.is_keyword(parser.peek(), "ORDER"):
        parser.advance()
        parser.expect_keyword("BY")
        field = parser.expect_field("ORDER BY 后")
        descending = False
        tok = parser.peek()
        if parser.is_keyword(tok, "ASC"):
            parser.advance()
        elif parser.is_keyword(tok, "DESC"):
            parser.advance()
            descending = True
        order = (field, descending)

    # LIMIT
    limit = None
    if parser.is_keyword(parser.peek(), "LIMIT"):
        parser.advance()
        tok = parser.peek()
        if tok.kind != "NUMBER" or not tok.value.is_integer() or tok.value < 0:
            shown = "%g" % tok.value if tok.kind == "NUMBER" else _describe(tok)
            raise QueryError("LIMIT 后应为非负整数，但发现 %s" % shown, tok.pos)
        parser.advance()
        limit = int(tok.value)

    # 语句应到此结束
    tok = parser.peek()
    if tok.kind == "RPAREN":
        raise QueryError("括号不配对：多余的右括号 ')'", tok.pos)
    if tok.kind != "EOF":
        raise QueryError("无法识别的内容 %s（请检查关键字拼写或语句结构）"
                         % _describe(tok), tok.pos)

    return {"fields": fields, "cond": cond, "order": order, "limit": limit}


def parse_or(parser):
    node = parse_and(parser)
    while parser.is_keyword(parser.peek(), "OR"):
        parser.advance()
        node = ("or", node, parse_and(parser))
    return node


def parse_and(parser):
    node = parse_primary(parser)
    while parser.is_keyword(parser.peek(), "AND"):
        parser.advance()
        node = ("and", node, parse_primary(parser))
    return node


def parse_primary(parser):
    tok = parser.peek()
    if tok.kind == "LPAREN":
        parser.advance()
        node = parse_or(parser)
        closing = parser.peek()
        if closing.kind != "RPAREN":
            raise QueryError("括号不配对：缺少右括号 ')'", closing.pos)
        parser.advance()
        return node
    return parse_comparison(parser)


def parse_comparison(parser):
    field = parser.expect_field("条件")
    tok = parser.peek()
    if tok.kind == "OP":
        op = parser.advance().value
    elif parser.is_keyword(tok, "CONTAINS"):
        parser.advance()
        op = "CONTAINS"
    else:
        raise QueryError(
            "条件缺少比较运算符（=、>、< 或 CONTAINS），但发现 %s" % _describe(tok),
            tok.pos)
    tok = parser.peek()
    if tok.kind == "NUMBER":
        value = parser.advance().value
    elif tok.kind == "STRING":
        value = parser.advance().value
    else:
        raise QueryError("条件缺少比较值，但发现 %s" % _describe(tok), tok.pos)
    return ("cmp", field, op, value)


# ---------------------------------------------------------------------------
# 语义检查与执行
# ---------------------------------------------------------------------------

def _cond_fields(node, acc):
    if node[0] == "cmp":
        acc.append(node[1])
    else:
        _cond_fields(node[1], acc)
        _cond_fields(node[2], acc)


def check_fields(query, header):
    """检查查询中引用的字段是否都存在于表头。"""
    referenced = []
    if query["fields"] != "*":
        referenced.extend(query["fields"])
    if query["cond"] is not None:
        _cond_fields(query["cond"], referenced)
    if query["order"] is not None:
        referenced.append(query["order"][0])
    for name in referenced:
        if name not in header:
            raise QueryError("字段 '%s' 在表中不存在（可用字段: %s）"
                             % (name, ", ".join(header)))


def _to_number(raw, field):
    try:
        return float(raw)
    except ValueError:
        raise QueryError("字段 '%s' 的值 %r 不是数值，无法进行大小比较" % (field, raw))


def eval_cond(node, row):
    kind = node[0]
    if kind == "and":
        return eval_cond(node[1], row) and eval_cond(node[2], row)
    if kind == "or":
        return eval_cond(node[1], row) or eval_cond(node[2], row)
    _, field, op, value = node
    raw = row[field]
    if op == "=":
        if isinstance(value, str):
            return raw == value
        return _to_number(raw, field) == value
    if op in (">", "<"):
        if isinstance(value, str):
            raise QueryError("运算符 '%s' 需要数值，不能比较字符串 %r" % (op, value))
        number = _to_number(raw, field)
        return number > value if op == ">" else number < value
    # CONTAINS
    if not isinstance(value, str):
        raise QueryError("CONTAINS 需要字符串操作数（请用双引号），不能用数值")
    return value in raw


def execute(query, header, rows):
    """按固定顺序执行：WHERE 过滤 -> ORDER BY 排序 -> LIMIT 截取 -> SELECT 投影。"""
    check_fields(query, header)

    # 1. 过滤
    result = rows
    if query["cond"] is not None:
        result = [row for row in result if eval_cond(query["cond"], row)]

    # 2. 排序
    if query["order"] is not None:
        field, descending = query["order"]
        for index, row in enumerate(result, 1):
            if row[field] == "":
                raise QueryError("排序字段 '%s' 在过滤结果第 %d 行缺失值" % (field, index))
        try:
            keys = [float(row[field]) for row in result]
            numeric = True
        except ValueError:
            numeric = False
        if numeric:
            result.sort(key=lambda row: float(row[field]), reverse=descending)
        else:
            result.sort(key=lambda row: row[field], reverse=descending)

    # 3. 截取
    if query["limit"] is not None:
        result = result[: query["limit"]]

    # 4. 投影
    fields = header if query["fields"] == "*" else query["fields"]
    return fields, [[row[field] for field in fields] for row in result]


# ---------------------------------------------------------------------------
# 数据读取与结果格式化
# ---------------------------------------------------------------------------

def parse_table(text):
    """解析数据表文本（首行为表头，字段可带双引号），返回 (表头, 行字典列表)。"""
    reader = csv.reader(io.StringIO(text.strip()))
    records = [record for record in reader if record]
    if not records:
        raise QueryError("数据表为空")
    header = records[0]
    rows = []
    for record in records[1:]:
        record = record + [""] * (len(header) - len(record))
        rows.append(dict(zip(header, record)))
    return header, rows


def format_table(fields, rows):
    if not rows:
        return "(无匹配行)"
    widths = [len(field) for field in fields]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = [" | ".join(field.ljust(widths[i]) for i, field in enumerate(fields))]
    lines.append("-+-".join("-" * width for width in widths))
    for row in rows:
        lines.append(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)


def run_query(query_text, table_text):
    """解析并执行查询，返回格式化结果字符串；出错抛出 QueryError。"""
    query = parse_query(query_text)
    header, rows = parse_table(table_text)
    fields, result = execute(query, header, rows)
    return format_table(fields, result)


def show_error(query_text, error):
    print("错误: %s" % error.message)
    if error.pos is not None:
        print("  " + query_text)
        print("  " + " " * (error.pos - 1) + "^ 第 %d 个字符" % error.pos)


# ---------------------------------------------------------------------------
# 内置示例
# ---------------------------------------------------------------------------

DEMO_DATA = '''name,age,city,score
"张三",28,"北京",86.5
"李四",35,"上海,浦东",92
"王五",22,"北京",78
"赵六",41,"广州",88
"钱七",31,"上海,徐汇",95.5
"孙八",19,"北京",60'''

DEMO_DATA_MISSING = '''name,age,city
"张三",28,"北京"
"李四",,"上海"
"王五",22,"广州"'''

DEMO_OK = [
    'SELECT name, age, city WHERE age > 25 AND city CONTAINS "北京" ORDER BY age DESC LIMIT 2',
    'SELECT name, score WHERE (city CONTAINS "上海" OR score > 90) AND age < 40 ORDER BY score ASC',
    'SELECT * WHERE city = "广州" LIMIT 5',
]

DEMO_BAD = [
    ('SELCT name, age WHERE age > 20', DEMO_DATA),                       # 关键字拼错
    ('SELECT name WHERE = 5', DEMO_DATA),                                # 条件缺字段名
    ('SELECT name WHERE (age > 20 AND city = "北京"', DEMO_DATA),        # 括号不配对
    ('SELECT name WHERE city = "北京', DEMO_DATA),                       # 字符串未闭合
    ('SELECT salary WHERE age > 20', DEMO_DATA),                         # 字段不存在
    ('SELECT name WHERE name > 3', DEMO_DATA),                           # 非数值字段比较
    ('SELECT name, age ORDER BY age', DEMO_DATA_MISSING),                # 排序字段缺失值
]


def run_demo():
    print("=" * 72)
    print("数据表:")
    print(DEMO_DATA)
    print("=" * 72)
    print("执行顺序（固定）: WHERE 过滤 -> ORDER BY 排序 -> LIMIT 截取 -> SELECT 投影")
    print("=" * 72)

    print("\n【正常查询示例】")
    for query_text in DEMO_OK:
        print("\n查询: %s" % query_text)
        try:
            print(run_query(query_text, DEMO_DATA))
        except QueryError as error:
            show_error(query_text, error)

    print("\n【错误定位示例】")
    for query_text, data in DEMO_BAD:
        print("\n查询: %s" % query_text)
        try:
            print(run_query(query_text, data))
        except QueryError as error:
            show_error(query_text, error)


def main(argv):
    if len(argv) == 1:
        run_demo()
        return 0
    if len(argv) == 3:
        with open(argv[1], encoding="utf-8") as f:
            table_text = f.read()
        with open(argv[2], encoding="utf-8") as f:
            query_text = f.read().strip()
        try:
            print(run_query(query_text, table_text))
            return 0
        except QueryError as error:
            show_error(query_text, error)
            return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
