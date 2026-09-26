#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
miniql.py —— 单文件自定义查询语言工具（仅使用 Python 标准库）

用法：
    python3 miniql.py "查询语句" 数据文件.csv
    python3 miniql.py "查询语句" < 数据文件.csv
    python3 miniql.py --demo            # 运行内置样例（含错误定位样例）

查询语法（关键字不区分大小写）：
    SELECT 字段1, 字段2 ... | *
    [WHERE 条件 [AND|OR 条件 ...]]        支持括号，AND 优先级高于 OR
    [ORDER BY 字段 [ASC|DESC]]            缺省 ASC
    [LIMIT N]

条件：字段 = 值 | 字段 > 数 | 字段 < 数 | 字段 CONTAINS "子串"
值：双引号字符串、数字、或不加引号的裸词

执行顺序固定为：过滤(WHERE) -> 排序(ORDER BY) -> 截取(LIMIT)。
即先按条件筛掉不满足的行，再对剩余行排序，最后取前 N 条；
若颠倒顺序（如先截取再过滤）结果集会不同，因此顺序不可交换。
"""

import csv
import sys
from dataclasses import dataclass

KEYWORDS = {"SELECT", "WHERE", "AND", "OR", "ORDER", "BY",
            "ASC", "DESC", "LIMIT", "CONTAINS"}


class QueryError(Exception):
    """查询相关错误。pos 为查询语句中的字符下标（从 0 开始），None 表示与位置无关。"""

    def __init__(self, message, pos=None):
        super().__init__(message)
        self.message = message
        self.pos = pos


# ---------------------------------------------------------------- 词法分析

@dataclass
class Token:
    kind: str   # SELECT/WHERE/.../IDENT/STRING/NUMBER/OP/COMMA/LPAREN/RPAREN/EOF
    value: str
    pos: int    # 在查询语句中的下标（0 起）

    def display(self):
        if self.kind == "EOF":
            return "语句末尾"
        return repr(self.value)


def tokenize(text):
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == ",":
            tokens.append(Token("COMMA", c, i)); i += 1
        elif c == "(":
            tokens.append(Token("LPAREN", c, i)); i += 1
        elif c == ")":
            tokens.append(Token("RPAREN", c, i)); i += 1
        elif c in "=><":
            tokens.append(Token("OP", c, i)); i += 1
        elif c == "*":
            tokens.append(Token("STAR", c, i)); i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                buf.append(text[j]); j += 1
            if j >= n:
                raise QueryError("字符串未闭合：缺少结束双引号", i)
            tokens.append(Token("STRING", "".join(buf), i))
            i = j + 1
        elif c.isdigit() or (c == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i + 1
            while j < n and (text[j].isdigit() or text[j] == "."):
                j += 1
            word = text[i:j]
            try:
                float(word)
            except ValueError:
                raise QueryError("非法的数字 %r" % word, i)
            tokens.append(Token("NUMBER", word, i))
            i = j
        elif c.isalpha() or c == "_" or ord(c) > 127:
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_" or ord(text[j]) > 127):
                j += 1
            word = text[i:j]
            upper = word.upper()
            if upper in KEYWORDS:
                tokens.append(Token(upper, upper, i))
            else:
                tokens.append(Token("IDENT", word, i))
            i = j
        else:
            raise QueryError("无法识别的字符 %r" % c, i)
    tokens.append(Token("EOF", "", n))
    return tokens


# ---------------------------------------------------------------- 语法分析

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

    def expect(self, kind, what=None):
        tok = self.peek()
        if tok.kind != kind:
            raise QueryError("期望 %s，却得到 %s" % (what or kind, tok.display()), tok.pos)
        return self.advance()

    def parse(self):
        query = self.parse_query()
        tok = self.peek()
        if tok.kind != "EOF":
            raise QueryError("语句结束后有多余内容 %s" % tok.display(), tok.pos)
        return query

    def parse_query(self):
        self.expect("SELECT", "关键字 SELECT（请检查关键字拼写）")
        fields = []
        if self.peek().kind == "STAR":
            self.advance()
            fields = "*"
        else:
            fields = [self.parse_field_name("选择列表")]
            while self.peek().kind == "COMMA":
                self.advance()
                fields.append(self.parse_field_name("选择列表"))

        where = None
        if self.peek().kind == "WHERE":
            self.advance()
            where = self.parse_or()

        order = None
        if self.peek().kind == "ORDER":
            self.advance()
            self.expect("BY", "关键字 BY（ORDER 后应跟 BY）")
            name_tok = self.expect("IDENT", "排序字段名")
            desc = False
            if self.peek().kind in ("ASC", "DESC"):
                desc = self.advance().kind == "DESC"
            order = (name_tok.value, desc, name_tok.pos)

        limit = None
        if self.peek().kind == "LIMIT":
            self.advance()
            num_tok = self.expect("NUMBER", "LIMIT 后的整数")
            limit = float(num_tok.value)
            if limit != int(limit) or limit < 0:
                raise QueryError("LIMIT 必须是非负整数，得到 %r" % num_tok.value, num_tok.pos)
            limit = int(limit)

        return {"select": fields, "where": where, "order": order, "limit": limit}

    def parse_field_name(self, where_desc):
        tok = self.expect("IDENT", "%s中的字段名" % where_desc)
        return (tok.value, tok.pos)

    # 条件：or -> and -> primary，AND 优先级高于 OR
    def parse_or(self):
        node = self.parse_and()
        while self.peek().kind == "OR":
            self.advance()
            node = ("or", node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_primary()
        while self.peek().kind == "AND":
            self.advance()
            node = ("and", node, self.parse_primary())
        return node

    def parse_primary(self):
        tok = self.peek()
        if tok.kind == "LPAREN":
            self.advance()
            node = self.parse_or()
            if self.peek().kind != "RPAREN":
                raise QueryError(
                    "括号不配对：第 %d 个字符的 '(' 没有匹配的 ')'" % (tok.pos + 1),
                    self.peek().pos)
            self.advance()
            return node
        return self.parse_comparison()

    def parse_comparison(self):
        field_tok = self.expect("IDENT", "条件中的字段名")
        tok = self.peek()
        if tok.kind == "OP":
            op = self.advance().value
        elif tok.kind == "CONTAINS":
            self.advance()
            op = "CONTAINS"
        else:
            raise QueryError("条件中期望运算符 =、>、< 或 CONTAINS，却得到 %s"
                             % tok.display(), tok.pos)
        val_tok = self.peek()
        if val_tok.kind == "STRING":
            self.advance()
            value, is_number = val_tok.value, False
        elif val_tok.kind == "NUMBER":
            self.advance()
            value, is_number = float(val_tok.value), True
        elif val_tok.kind == "IDENT":
            self.advance()
            value, is_number = val_tok.value, False
        else:
            raise QueryError("条件中期望比较值（字符串或数字），却得到 %s"
                             % val_tok.display(), val_tok.pos)
        return ("cmp", field_tok.value, field_tok.pos, op, value, is_number)


# ---------------------------------------------------------------- 执行

def to_number(text):
    try:
        return float(text)
    except (ValueError, TypeError):
        return None


def collect_fields(node, out):
    """收集条件语法树中引用的字段名及其位置。"""
    if node is None:
        return
    if node[0] == "cmp":
        out.append((node[1], node[2]))
    else:
        collect_fields(node[1], out)
        collect_fields(node[2], out)


def eval_condition(node, header, row, row_no):
    if node[0] == "and":
        return eval_condition(node[1], header, row, row_no) and \
               eval_condition(node[2], header, row, row_no)
    if node[0] == "or":
        return eval_condition(node[1], header, row, row_no) or \
               eval_condition(node[2], header, row, row_no)
    _, field, _, op, value, is_number = node
    cell = row[header.index(field)]
    if op == "CONTAINS":
        if is_number:
            raise QueryError("CONTAINS 的右侧应为字符串，得到数字 %r" % value)
        return str(value) in cell
    if op == "=":
        if is_number:
            num = to_number(cell)
            return num is not None and num == value
        return cell == value
    # > 和 < 必须两边都是数值
    if not is_number:
        raise QueryError("运算符 %s 的右侧应为数字，得到 %r" % (op, value))
    num = to_number(cell)
    if num is None:
        raise QueryError("第 %d 行字段 '%s' 的值 %r 不是数值，无法用 %s 比较"
                         % (row_no, field, cell, op))
    return num > value if op == ">" else num < value


def run_query(query_text, data_text):
    """解析并执行查询，返回 (输出表头, 输出行列表)。出错抛 QueryError。"""
    ast = Parser(tokenize(query_text)).parse()

    reader = csv.reader(data_text.splitlines())
    rows = [r for r in reader if r]
    if not rows:
        raise QueryError("数据表为空（至少需要一行表头）")
    header = [h.strip() for h in rows[0]]
    data = [r + [""] * (len(header) - len(r)) for r in rows[1:]]

    # 字段存在性检查：选择列表 / 条件 / 排序
    if ast["select"] == "*":
        select_names = header
    else:
        select_names = [name for name, _ in ast["select"]]
        for name, pos in ast["select"]:
            if name not in header:
                raise QueryError("字段 '%s' 在数据表中不存在" % name, pos)
    cond_fields = []
    collect_fields(ast["where"], cond_fields)
    for name, pos in cond_fields:
        if name not in header:
            raise QueryError("字段 '%s' 在数据表中不存在" % name, pos)
    if ast["order"] is not None:
        name, _, pos = ast["order"]
        if name not in header:
            raise QueryError("排序字段 '%s' 在数据表中不存在" % name, pos)

    # 第 1 步：过滤
    result = []
    for idx, row in enumerate(data, start=1):
        if ast["where"] is None or eval_condition(ast["where"], header, row, idx):
            result.append(row)

    # 第 2 步：排序
    if ast["order"] is not None:
        field, desc, _ = ast["order"]
        col = header.index(field)
        keys = []
        for idx, row in enumerate(result, start=1):
            cell = row[col]
            if cell == "":
                raise QueryError("排序字段 '%s' 在第 %d 行缺失值，无法排序" % (field, idx))
            keys.append(to_number(cell))
        numeric = all(k is not None for k in keys)
        result.sort(key=lambda r: to_number(r[col]) if numeric else r[col],
                    reverse=desc)

    # 第 3 步：截取
    if ast["limit"] is not None:
        result = result[:ast["limit"]]

    idxs = [header.index(name) for name in select_names]
    return select_names, [[row[i] for i in idxs] for row in result]


# ---------------------------------------------------------------- 输出与错误定位

def format_error(err, query_text):
    lines = ["错误：%s" % err.message]
    if err.pos is not None:
        lines.append("  " + query_text)
        lines.append("  " + " " * err.pos + "^ 第 %d 个字符" % (err.pos + 1))
    return "\n".join(lines)


def print_csv(out_header, out_rows, file=sys.stdout):
    writer = csv.writer(file)
    writer.writerow(out_header)
    writer.writerows(out_rows)


# ---------------------------------------------------------------- 内置样例

DEMO_DATA = """\
name,age,city,salary
张三,28,北京,12000
李四,35,上海,18000
王五,42,上海,25000
赵六,31,北京,15000
孙七,26,广州,9000
周八,38,上海,21000
"""

DEMO_DATA_WITH_MISSING = """\
name,age,city
张三,28,北京
李四,35,
王五,42,上海
"""

DEMOS = [
    ("【样例 1】条件过滤 + 排序 + 截取", DEMO_DATA,
     'SELECT name, city, salary WHERE city = "上海" AND age > 30 ORDER BY salary DESC LIMIT 2'),
    ("【样例 2】CONTAINS + OR + 括号", DEMO_DATA,
     'SELECT name, age WHERE (name CONTAINS "张" OR age < 28) AND salary > 5000'),
    ("【错误 1】关键字拼错（SELEC）", DEMO_DATA,
     'SELEC name WHERE age > 30'),
    ("【错误 2】条件缺字段名", DEMO_DATA,
     'SELECT name WHERE = 30'),
    ("【错误 3】括号不配对", DEMO_DATA,
     'SELECT name WHERE (age > 30 AND city = "上海"'),
    ("【错误 4】字符串未闭合", DEMO_DATA,
     'SELECT name WHERE city = "上海'),
    ("【错误 5】字段名不存在", DEMO_DATA,
     'SELECT name, salaryy WHERE age > 30'),
    ("【错误 6】非数值字段做大小比较", DEMO_DATA,
     'SELECT name WHERE name > 30'),
    ("【错误 7】排序字段存在缺失值", DEMO_DATA_WITH_MISSING,
     'SELECT name, city ORDER BY city'),
]


def run_demo():
    for title, data, query in DEMOS:
        print("=" * 60)
        print(title)
        print("查询：%s" % query)
        try:
            out_header, out_rows = run_query(query, data)
            print("结果：")
            print_csv(out_header, out_rows)
        except QueryError as err:
            print(format_error(err, query))
        print()


# ---------------------------------------------------------------- 命令行入口

USAGE = __doc__


def main(argv):
    args = argv[1:]
    if not args or "-h" in args or "--help" in args:
        sys.stdout.write(USAGE)
        return 0
    if args[0] == "--demo":
        run_demo()
        return 0
    query = args[0]
    if len(args) > 1:
        with open(args[1], "r", encoding="utf-8") as f:
            data = f.read()
    else:
        data = sys.stdin.read()
    try:
        out_header, out_rows = run_query(query, data)
    except QueryError as err:
        print(format_error(err, query), file=sys.stderr)
        return 1
    print_csv(out_header, out_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
