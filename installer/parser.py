import copy
import logging
import re

CONFIG_SPEC = [
    ("comment", re.compile(r"^[ \t]*[#;].*?(?=\{[^}]+\})")),
    ("comment", re.compile(r"^[ \t]*[#;].*")),
    ("whitespace", re.compile(r"^\s+")),
    ("section", re.compile(r"^\[[^]]+\]")),
    ("word", re.compile(r"^\w[\w\d%]*")),
    ("assign_op", re.compile(r"^[:=]")),
    ("placeholder", re.compile(r"^\[[pin_|cfg_|param_)[^%\]][^\]]+\]]")),
    ("unknown", re.compile(r"^\S")),
]


class Token(object):
    def __init__(self, type, value):
        self.type = type
        self.value = value

    def __repr__(self):
        return "{}: {}".format(self.type, self.value)


class Tokenizer(object):
    def __init__(self, buf, spec):
        self.slice = buf[:]
        self.next_token = None
        self.spec = spec

    def __iter__(self):
        return self

    def next(self):
        return self.__next__()

    def __next__(self):
        if self.next_token is not None:
            next_token = self.next_token
            self.next_token = None
            return next_token

        if len(self.slice) == 0:
            raise StopIteration

        for token_type, regex in self.spec:
            match = regex.match(self.slice)
            if match:
                self.slice = self.slice[match.end() :]
                return Token(token_type, match.group(0))

        raise SyntaxError("Unexpected token '{}'".format(self.slice[0]))

    def peek(self):
        if self.next_token is None:
            try:
                self.next_token = next(self)
            except StopIteration:
                return None
        return self.next_token

    def take(self, token_type):
        token = next(self)
        if token and token.type == token_type:
            return token
        else:
            raise SyntaxError("Expected {}, got {}".format(token_type, token.type))

    def consume(self, token_type):
        peek = self.peek()
        if peek and peek.type == token_type:
            self.take(token_type)


class Node(object):
    def __init__(self, type):
        self.type = type

    def _pretty_print(self, tab=4):
        def _print(node, lines, depth):
            lines.append(" " * depth * tab + type(node).__name__)
            for k, v in vars(node).items():
                if k in ["type", "body"]:
                    continue
                lines.append(" " * (depth + 1) * tab + "{}: `{}`".format(k, v))
            return True, lines

        lines = self.walk(_print, [])
        return "\n".join(lines)

    def __str__(self):
        return self._pretty_print()

    def walk(self, callback, context=None, depth=0):
        _, context = callback(self, context, depth)
        return context

    def serialize(self):
        return str("")


class BodyNode(Node):
    def __init__(self, type, body):
        super(BodyNode, self).__init__(type)
        self.body = body

    def walk(self, callback, context=None, depth=0):
        cont, context = callback(self, context, depth)
        if cont:
            for item in self.body:
                context = item.walk(callback, context, depth + 1)
        return context


class DocumentNode(BodyNode):
    def __init__(self, body=None):
        super(DocumentNode, self).__init__("document", body or [])


class SectionNode(BodyNode):
    def __init__(self, name, body):
        BodyNode.__init__(self, "section", body)
        self.name = name

    def serialize(self):
        return "[" + self.name + "]"


class OptionNode(BodyNode):
    def __init__(self, name, value, assign_op, trailing_ws=""):
        BodyNode.__init__(self, "option", value)
        self.name = name
        self.assign_op = assign_op
        self.trailing_ws = trailing_ws

    def serialize(self):
        return self.name + self.trailing_ws + self.assign_op

    def value(self, default=None, with_comments=False):
        if len(self.body) == 0:
            return default

        def serialize(node, buf, _):
            if not with_comments and isinstance(node, CommentNode):
                return False, buf
            return True, buf + node.serialize()

        value = ""
        for value_line in self.body:
            value = value_line.walk(serialize, value)
        return value.strip()


class ValueLineNode(BodyNode):
    def __init__(self, body):
        BodyNode.__init__(self, "value_line", body)


class ValueEntryNode(Node):
    def __init__(self, value):
        Node.__init__(self, "value_entry")
        self.value = value

    def serialize(self):
        return self.value


class CommentNode(BodyNode):
    def __init__(self, body):
        BodyNode.__init__(self, "comment", body)


class CommentEntryNode(Node):
    def __init__(self, value):
        Node.__init__(self, "comment_entry")
        self.value = value

    def serialize(self):
        return self.value


class PlaceholderNode(Node):
    def __init__(self, value):
        Node.__init__(self, "placeholder")
        self.value = value

    def serialize(self):
        return "{" + self.value + "}"


class WhitespaceNode(Node):
    def __init__(self, value):
        Node.__init__(self, "whitespace")
        self.value = value

    def serialize(self):
        return self.value


class Parser(object):
    def __init__(self, default_assign_op=":", default_comment_ch="#"):
        self.default_assign_op = default_assign_op
        self.default_comment_ch = default_comment_ch

    def parse(self, buffer):
        tokenizer = Tokenizer(buffer, CONFIG_SPEC)
        return self._post_process(self.parse_document(tokenizer))

    def filter_tree(self, node, filter):
        def _filter_tree(parent, node, filter):
            if filter(node):
                if parent and parent["body"]:
                    parent["body"].remove(node)
            elif node["body"]:
                for item in node["body"]:
                    _filter_tree(node, item, filter)

        _filter_tree(None, node, filter)

    def parse_document(self, tokenizer):
        body = []
        peek = tokenizer.peek()
        while peek:
            if peek.type == "section":
                body.append(self.parse_section(tokenizer))
            elif peek.type == "comment":
                body.append(self.parse_comment(tokenizer))
            elif peek.type == "whitespace":
                body.append(self.parse_whitespace(tokenizer))
            elif peek.type == "placeholder":
                body.append(self.parse_placeholder(tokenizer))
            else:
                raise SyntaxError("Unexpected token '{}' at:\n {}".format(peek, tokenizer.slice[:20]))
            peek = tokenizer.peek()

        return DocumentNode(body)

    def _post_process(self, document):
        """Move trailing whitespace/comments from sections a level up to be part of the document body"""
        new_body = []
        for item in document.body:
            new_body.append(item)
            if isinstance(item, SectionNode):
                # Find the index of the next item after last option in the section
                idx = max([i + 1 for i, n in enumerate(item.body) if isinstance(n, OptionNode)] or [0])
                if len(item.body) <= idx:
                    continue
                # If the next item is whitespace, still keep it in the section
                if isinstance(item.body[idx], WhitespaceNode):
                    idx += 1
                # All remaining items get moved up a level
                if len(item.body) > idx:
                    new_body += item.body[idx:]
                    item.body = item.body[:idx]

        document.body = new_body
        return document

    def parse_section(self, tokenizer):
        token = tokenizer.take("section")
        body = []

        peek = tokenizer.peek()
        while peek and peek.type != "section":
            if peek.type == "comment":
                body.append(self.parse_comment(tokenizer))
            elif peek.type == "word":
                body.append(self.parse_option(tokenizer))
            elif peek.type == "placeholder":
                body.append(self.parse_placeholder(tokenizer))
            elif peek.type == "whitespace":
                body.append(self.parse_whitespace(tokenizer))
            else:
                raise SyntaxError("Unexpected token '{}' at:\n {}".format(peek, tokenizer.slice[:20]))
            peek = tokenizer.peek()

        return SectionNode(token.value[1:-1], body)

    def parse_option(self, tokenizer):
        token = tokenizer.take("word")
        trailing_ws = ""
        if tokenizer.peek().type == "whitespace":
            trailing_ws = tokenizer.take("whitespace").value
        assign_op = tokenizer.take("assign_op").value
        if token.value.startswith("gcode"):  # parse gcode options as-is, so we don't parse gcode as structure'
            value = self.parse_value(tokenizer, as_is=True)
        else:
            value = self.parse_value(tokenizer)

        return OptionNode(token.value, value, assign_op, trailing_ws)

    def parse_value(self, tokenizer, as_is=False):
        body = []
        current_entry = ""
        current_line = []

        peek = tokenizer.peek()
        while peek:
            if peek.type == "whitespace":
                if peek.value.endswith("\n"):  # multi-line value ends with a newline without a tab/space after it
                    break

                token = tokenizer.take("whitespace")
                if len(body) == 0 and len(current_line) == 0 and len(current_entry) == 0:
                    current_line.append(WhitespaceNode(token.value))
                else:
                    current_entry += token.value
                idx = current_entry.find("\n")
                while idx != -1:
                    current_line.append(ValueEntryNode(current_entry[: idx + 1]))
                    current_entry = current_entry[idx + 1 :]
                    body.append(ValueLineNode(current_line))
                    current_line = []
                    idx = current_entry.find("\n")

            elif not as_is and peek.type == "comment":
                if len(current_entry) > 0:
                    current_line.append(ValueEntryNode(current_entry))
                    current_entry = ""
                current_line.append(self.parse_comment(tokenizer))
            elif not as_is and peek.type == "placeholder":
                if len(current_entry) > 0:
                    current_line.append(ValueEntryNode(current_entry))
                    current_entry = ""
                current_line.append(self.parse_placeholder(tokenizer))
            else:
                current_entry += next(tokenizer).value

            peek = tokenizer.peek()

        if len(current_entry) > 0:
            current_line.append(ValueEntryNode(current_entry))

        if len(current_line) > 0:
            body.append(ValueLineNode(current_line))

        return body

    def parse_comment(self, tokenizer):
        token = tokenizer.take("comment")
        current_comment = token.value
        body = []

        peek = tokenizer.peek()
        while peek:
            if peek.type == "whitespace":
                if peek.value.find("\n") != -1:
                    break
                current_comment += tokenizer.take("whitespace").value
            elif peek.type == "placeholder":
                if len(current_comment) > 0:
                    body.append(CommentEntryNode(current_comment))
                    current_comment = ""
                body.append(self.parse_placeholder(tokenizer))
            else:
                current_comment += next(tokenizer).value

            peek = tokenizer.peek()

        if len(current_comment) > 0:
            body.append(CommentEntryNode(current_comment))

        return CommentNode(body)

    def parse_placeholder(self, tokenizer):
        placeholder = tokenizer.take("placeholder")
        return PlaceholderNode(placeholder.value[1:-1])

    def parse_whitespace(self, tokenizer):
        token = tokenizer.take("whitespace")
        return WhitespaceNode(token.value)


def collect(node, ctx):
    ctx.append(node)
    return ctx


def identity(node, _):
    return node


def rename(node, ctx):
    node["name"] = ctx


class ConfigBuilder(object):
    def __init__(self, filename=None, parser=Parser()):
        self.filename = filename
        self.parser = parser
        self.document = DocumentNode()
        if self.filename:
            with open(self.filename, "r") as f:
                self.document = self.parser.parse(f.read())

    def read(self, filename):
        with open(filename, "r") as f:
            doc = self.parser.parse(f.read())
            self.document.body += doc.body

    def read_buf(self, buf):
        doc = self.parser.parse(buf)
        self.document.body += doc.body

    def read_test(self, b):
        self.document.body = b

    def write(self):
        def print_node(node, buffer, _):
            buffer += node.serialize()
            return True, buffer

        return self.document.walk(print_node, "")

    def _for_section(self, section_name, callback, ctx=None):
        def for_section(node, ctx, _):
            if isinstance(node, SectionNode) and (not section_name or node.name == section_name):
                ctx = callback(node, ctx)
            return (isinstance(node, DocumentNode), ctx)

        return self.document.walk(for_section, ctx)

    def _sections(self):
        return self._for_section(None, collect, [])

    def _get_section(self, section_name):
        section = self._for_section(section_name, identity)
        if section:
            return section
        else:
            raise KeyError("Section '{}' not found".format(section_name))

    def sections(self):
        return [x.name for x in self._sections()]

    def has_section(self, section_name):
        try:
            return self._get_section(section_name) is not None
        except KeyError:
            return False

    def add_section(self, section_name, comment=None, at_top=False, extra_newline=True):
        if self.has_section(section_name):
            return

        if comment:
            section_body = [
                CommentNode([CommentEntryNode("# " + comment)]),
                WhitespaceNode("\n" if extra_newline else ""),
            ]
        else:
            section_body = [WhitespaceNode("\n" if extra_newline else "")]

        document_body = [
            WhitespaceNode("\n"),
            SectionNode(section_name, section_body),
        ]
        if at_top:
            self.document.body[0:0] = document_body
        else:
            self.document.body += document_body

    def remove_section(self, section_name):
        for i, item in enumerate(self.document.body):
            if isinstance(item, SectionNode) and item.name == section_name:
                self.document.body.pop(i)

    def rename_section(self, section_name, new_section_name):
        self._for_section(section_name, rename, new_section_name)

    def _for_option(self, section_name, option_name, callback, ctx=None):
        def for_option(node, ctx, _):
            if isinstance(node, OptionNode) and (not option_name or node.name == option_name):
                ctx = callback(node, ctx)
            return (isinstance(node, SectionNode), ctx)

        section = self._get_section(section_name)
        return section.walk(for_option, ctx)

    def _options(self, section_name):
        return self._for_option(section_name, None, collect, [])

    def _get_option(self, section_name, option_name):
        option = self._for_option(section_name, option_name, identity)
        if option:
            return option
        else:
            raise KeyError("Option '{}' not found".format(option_name))

    def options(self, section_name):
        return [x.name for x in self._options(section_name)]

    def has_option(self, section_name, option_name):
        try:
            return self._for_option(section_name, option_name, identity) is not None
        except KeyError:
            return False

    def remove_option(self, section_name, option_name):
        try:
            section = self._get_section(section_name)
            for i, item in enumerate(section.body):
                if isinstance(item, OptionNode) and item.name == option_name:
                    section.body.pop(i)
        except KeyError:
            return

    def rename_option(self, section_name, option_name, new_option_name):
        self._for_option(section_name, option_name, rename, new_option_name)

    def get(self, section_name, option_name, default=None, with_comments=False):
        def serialize(node, buf, _):
            if not with_comments and isinstance(node, CommentNode):
                return False, buf
            return True, buf + node.serialize()

        try:
            option = self._get_option(section_name, option_name)
            if len(option.body) == 0:
                return default
            value = ""
            for value_line in option.body:
                value = value_line.walk(serialize, value)
            return value.strip()
        except KeyError:
            return default

    def getint(self, section_name, option_name, default=None):
        value = self.get(section_name, option_name)
        if value:
            return int(value.strip())
        return default

    def getfloat(self, section_name, option_name, default=None):
        value = self.get(section_name, option_name)
        if value:
            return float(value.strip())
        return default

    def getboolean(self, section_name, option_name, default=None):
        value = self.get(section_name, option_name)
        if value:
            return value.strip().lower() in ["1", "true", "yes", "on"]
        return default

    def set(self, section_name, option_name, value):
        value = str(value).strip()
        idx = value.find("\n")
        if idx != -1:
            value = value[:idx] + re.sub(r"^(?=\S)", r"    ", value[idx:], flags=re.MULTILINE)
        if self.has_option(section_name, option_name):
            option = self._get_option(section_name, option_name)
            if option.body and isinstance(option.body[0].body[0].type, WhitespaceNode):
                value = option.body[0].body[0].value + value
            value = self.parser.parse_value(Tokenizer(value, CONFIG_SPEC))
            option.value = value
        else:
            value = self.parser.parse_value(Tokenizer(" " + value, CONFIG_SPEC))
            section = self._get_section(section_name)
            section.body.append(OptionNode(option_name, value, self.parser.default_assign_op))
            section.body.append(WhitespaceNode("\n"))
        option = self._get_option(section_name, option_name)

    def items(self, section_name):
        section = self._get_section(section_name)
        return [(item.name, item.value()) for item in section.body if isinstance(item, OptionNode)]

    def placeholders(self):
        def collect_placeholders(node, ctx, _):
            if isinstance(node, PlaceholderNode):
                ctx.append(node.value)
            return True, ctx

        return self.document.walk(collect_placeholders, [])
