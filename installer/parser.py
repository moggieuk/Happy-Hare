import copy
import logging
import re


class Tokenizer(object):
    def __init__(self, buf):
        self.slice = buf[:]
        self.next_token = None
        self.spec = [
            ("comment", re.compile(r"^[ \t]*[#;].*?(?=\{[^}]+\})")),
            ("comment", re.compile(r"^[ \t]*[#;].*")),
            ("whitespace", re.compile(r"^\s+")),
            ("section", re.compile(r"^\[[^]]+\]")),
            ("word", re.compile(r"^\w[\w\d%]*")),
            ("assign_op", re.compile(r"^[:=]")),
            ("placeholder", re.compile(r"^\{(pin_|cfg_|param_)[^%}][^}]+\}")),
            ("unknown", re.compile(r"^\S")),
        ]

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
                return {"type": token_type, "value": match.group(0)}

        raise SyntaxError("Unexpected token '{}'", self.slice[0])

    def peek(self):
        if self.next_token is None:
            try:
                self.next_token = next(self)
            except StopIteration:
                return None
        return self.next_token

    def take(self, token_type):
        token = next(self)
        if token and token["type"] == token_type:
            return token
        else:
            raise SyntaxError("Expected {}, got {}".format(token_type, token["type"]))


class Parser(object):
    def __init__(self, default_assign_op=":", default_comment_ch="#"):
        self.default_assign_op = default_assign_op
        self.default_comment_ch = default_comment_ch

    def parse(self, buffer):
        tokenizer = Tokenizer(buffer)
        return self._post_process(self.parse_document(tokenizer))

    def walk_tree(self, node, callback, context=None):
        def _walk_tree(node, callback, context):
            cont, ctx = callback(node, context)
            if cont is False:
                return ctx

            if node["type"] in ["document", "section", "value", "value_line", "comment"]:
                for item in node["body"]:
                    ctx = _walk_tree(item, callback, ctx)
            elif node["type"] == "option":
                ctx = _walk_tree(node["value"], callback, ctx)
            return ctx

        return _walk_tree(node, callback, context)

    def serialize(self, node, filter=None):
        def print_node(node, buffer):
            if filter and not filter(node):
                return (False, buffer)
            if node["type"] in ["comment_entry", "value_entry", "whitespace"]:
                buffer.append(node["value"])
            elif node["type"] == "placeholder":
                buffer.append("{" + node["value"] + "}")
            elif node["type"] == "section":
                buffer.append("[" + node["name"] + "]")
            elif node["type"] == "option":
                buffer.append(node["name"] + node["trailing_space"] + node["assign_op"])
            return (True, buffer)

        buffer = []
        self.walk_tree(node, print_node, buffer)
        return "".join(buffer)

    def document(self, body=[]):
        return {"type": "document", "body": body}

    def parse_document(self, tokenizer):
        body = []
        peek = tokenizer.peek()
        while peek:
            if peek["type"] == "section":
                body.append(self.parse_section(tokenizer))
            elif peek["type"] == "comment":
                body.append(self.parse_comment(tokenizer))
            elif peek["type"] == "whitespace":
                body.append(self.parse_whitespace(tokenizer))
            elif peek["type"] == "placeholder":
                body.append(self.parse_placeholder(tokenizer))
            else:
                raise SyntaxError("Unexpected token '{}' at:\n {}".format(peek, tokenizer.slice[:20]))
            peek = tokenizer.peek()

        return self.document(body)

    def _post_process(self, document):
        new_body = []
        # Move trailing whitespace/comments from sections a level up to be part of the document body
        for item in document["body"]:
            new_body.append(item)
            if item["type"] == "section":
                # Find the index of the last option in the section
                idx = max([i + 1 for i, n in enumerate(item["body"]) if n["type"] == "option"] or [0])
                if item["body"][idx]["type"] == "whitespace":
                    idx += 1
                new_body += item["body"][idx:]
                item["body"] = item["body"][:idx]

        document["body"] = new_body
        return document

    def section(self, name, body=[]):
        return {"type": "section", "name": name, "body": body}

    def parse_section(self, tokenizer):
        token = tokenizer.take("section")
        body = []

        peek = tokenizer.peek()
        while peek and peek["type"] != "section":
            if peek["type"] == "comment":
                body.append(self.parse_comment(tokenizer))
            elif peek["type"] == "word":
                body.append(self.parse_option(tokenizer))
            elif peek["type"] == "placeholder":
                body.append(self.parse_placeholder(tokenizer))
            elif peek["type"] == "whitespace":
                body.append(self.parse_whitespace(tokenizer))
            else:
                raise SyntaxError("Unexpected token '{}' at:\n {}".format(peek, tokenizer.slice[:20]))
            peek = tokenizer.peek()

        return self.section(token["value"][1:-1], body)

    def option(self, name, value=None, assign_op=None, trailing_space=""):
        return {
            "type": "option",
            "name": name,
            "assign_op": assign_op or self.default_assign_op,
            "value": value,
            "trailing_space": trailing_space,
        }

    def parse_option(self, tokenizer):
        token = tokenizer.take("word")
        trailing_space = ""
        if tokenizer.peek()["type"] == "whitespace":
            trailing_space = tokenizer.take("whitespace")["value"]
        assign = tokenizer.take("assign_op")["value"]
        if token["value"].startswith("gcode"):  # parse gcode options as is so we don't parse it as many placeholders
            value = self.parse_value(tokenizer, as_is=True)
        else:
            value = self.parse_value(tokenizer)

        return self.option(token["value"], value, assign, trailing_space)

    def value(self, body):
        return {"type": "value", "body": body}

    def value_line(self, body):
        return {"type": "value_line", "body": body}

    def value_entry(self, value):
        return {"type": "value_entry", "value": value}

    def parse_value(self, tokenizer, as_is=False):
        body = []
        current_entry = ""
        current_line = []

        peek = tokenizer.peek()
        while peek:
            if peek["type"] == "whitespace":
                if peek["value"].endswith("\n"):  # multi-line value ends with a newline without a tab/space after it
                    break

                token = tokenizer.take("whitespace")
                if len(body) == 0 and len(current_line) == 0 and len(current_entry) == 0:
                    current_line.append(self.whitespace(token["value"]))
                else:
                    current_entry += token["value"]
                idx = current_entry.find("\n")
                while idx != -1:
                    current_line.append(self.value_entry(current_entry[: idx + 1]))
                    current_entry = current_entry[idx + 1 :]
                    body.append(self.value_line(current_line))
                    current_line = []
                    idx = current_entry.find("\n")

            elif not as_is and peek["type"] == "comment":
                if len(current_entry) > 0:
                    current_line.append(self.value_entry(current_entry))
                    current_entry = ""
                current_line.append(self.parse_comment(tokenizer))
            elif not as_is and peek["type"] == "placeholder":
                if len(current_entry) > 0:
                    current_line.append(self.value_entry(current_entry))
                    current_entry = ""
                current_line.append(self.parse_placeholder(tokenizer))
            else:
                current_entry += next(tokenizer)["value"]

            peek = tokenizer.peek()

        if len(current_entry) > 0:
            current_line.append(self.value_entry(current_entry))

        if len(current_line) > 0:
            body.append(self.value_line(current_line))

        return self.value(body)

    def comment(self, body):
        return {"type": "comment", "body": body}

    def simple_comment(self, value):
        return {"type": "comment", "body": [self.comment_entry(value)]}

    def comment_entry(self, value):
        return {"type": "comment_entry", "value": value}

    def parse_comment(self, tokenizer):
        token = tokenizer.take("comment")
        current_comment = token["value"]
        body = []

        peek = tokenizer.peek()
        while peek:
            if peek["type"] == "whitespace":
                if peek["value"].find("\n") != -1:
                    break
                current_comment += tokenizer.take("whitespace")["value"]
            elif peek["type"] == "placeholder":
                if len(current_comment) > 0:
                    body.append(self.comment_entry(current_comment))
                    current_comment = ""
                body.append(self.parse_placeholder(tokenizer))
            else:
                current_comment += next(tokenizer)["value"]

            peek = tokenizer.peek()

        if len(current_comment) > 0:
            body.append(self.comment_entry(current_comment))

        return self.comment(body)

    def parse_placeholder(self, tokenizer):
        placeholder = tokenizer.take("placeholder")
        return {"type": "placeholder", "value": placeholder["value"][1:-1]}

    def whitespace(self, value):
        return {"type": "whitespace", "value": value}

    def parse_whitespace(self, tokenizer):
        token = tokenizer.take("whitespace")
        return self.whitespace(token["value"])


def collect(node, ctx):
    ctx.append(node)
    return ctx


def identity(node, _):
    return node


def rename(node, ctx):
    node["name"] = ctx


class ConfigBuilder(object):
    def __init__(self, filemame=None, parser=Parser()):
        self.filename = filemame
        self.parser = parser
        if self.filename:
            with open(self.filename, "r") as f:
                self.document = self.parser.parse(f.read())
        else:
            self.document = self.parser.document()

    def read(self, filename):
        with open(filename, "r") as f:
            document = self.parser.parse(f.read())
            self.document["body"] += document["body"]

    def write(self):
        return self.parser.serialize(self.document)

    def _for_section(self, section_name, callback, ctx=None):
        def for_section(node, ctx):
            if node["type"] == "section" and (not section_name or node["name"] == section_name):
                ctx = callback(node, ctx)
            return (node["type"] == "document", ctx)

        return self.parser.walk_tree(self.document, for_section, ctx)

    def _sections(self):
        return self._for_section(None, collect, [])

    def _get_section(self, section_name):
        section = self._for_section(section_name, identity)
        if section:
            return section
        else:
            raise KeyError("Section '{}' not found".format(section_name))

    def sections(self):
        return [x["name"] for x in self._sections()]

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
                self.parser.simple_comment("# " + comment),
                self.parser.whitespace("\n" if extra_newline else ""),
            ]
        else:
            section_body = [self.parser.whitespace("\n" if extra_newline else "")]

        document_body = [
            self.parser.whitespace("\n"),
            self.parser.section(section_name, section_body),
        ]
        if at_top:
            self.document["body"][0:0] = document_body
        else:
            self.document["body"] += document_body

    def remove_section(self, section_name):
        for i, item in enumerate(self.document["body"]):
            if item["type"] == "section" and item["name"] == section_name:
                self.document["body"].pop(i)

    def rename_section(self, section_name, new_section_name):
        self._for_section(section_name, rename, new_section_name)

    def _for_option(self, section_name, option_name, callback, ctx=None):
        def for_option(node, ctx):
            if node["type"] == "option" and (not option_name or node["name"] == option_name):
                ctx = callback(node, ctx)
            return (node["type"] == "section", ctx)

        section = self._get_section(section_name)
        return self.parser.walk_tree(section, for_option, ctx)

    def _options(self, section_name):
        return self._for_option(section_name, None, collect, [])

    def _get_option(self, section_name, option_name):
        option = self._for_option(section_name, option_name, identity)
        if option:
            return option
        else:
            raise KeyError("Option '{}' not found".format(option_name))

    def options(self, section_name):
        return [x["name"] for x in self._options(section_name)]

    def has_option(self, section_name, option_name):
        try:
            return self._for_option(section_name, option_name, identity) is not None
        except KeyError:
            return False

    def remove_option(self, section_name, option_name):
        try:
            section = self._get_section(section_name)
            for i, item in enumerate(section["body"]):
                if item["type"] == "option" and item["name"] == option_name:
                    section["body"].pop(i)
        except KeyError:
            return

    def rename_option(self, section_name, option_name, new_option_name):
        self._for_option(section_name, option_name, rename, new_option_name)

    def get(self, section_name, option_name, default=None, with_comments=False):
        def filter(node):
            return with_comments or node["type"] != "comment"

        try:
            option = self._get_option(section_name, option_name)
            if option["value"]["body"] == []:
                return default
            return self.parser.serialize(option["value"], filter).strip()
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
            if option["value"]["body"][0] and option["value"]["body"][0]["body"][0]["type"] == "whitespace":
                value = option["value"]["body"][0]["body"][0]["value"] + value
            value = self.parser.parse_value(Tokenizer(value))
            option["value"] = value
        else:
            value = self.parser.parse_value(Tokenizer(" " + value))
            section = self._get_section(section_name)
            section["body"].append(
                self.parser.option(
                    option_name,
                    value,
                )
            )
            section["body"].append(self.parser.whitespace("\n"))
        option = self._get_option(section_name, option_name)

    def items(self, section_name):
        section = self._get_section(section_name)
        return [
            (item["name"], self.parser.serialize(item["value"])) for item in section["body"] if item["type"] == "option"
        ]

    def comment_out_section(self, section_name):
        for i, item in enumerate(self.document["body"]):
            if item["type"] == "section" and item["name"] == section_name:
                print(i, item)
                comment = self.parser.serialize(item, "")
                comment = re.sub(r"^", "#", comment, flags=re.MULTILINE)
                self.document["body"][i] = self.parser.parse_comment(Tokenizer(comment))

    def comment_out_option(self, section_name, option_name):
        section = self._get_section(section_name)
        for i, item in enumerate(section["body"]):
            if item["type"] == "option" and item["name"] == option_name:
                section["body"][i:i] = [self.parser.whitespace("#")]
                return
        # comment = self.parser.serialize(option, "")
        # comment = re.sub(r"^", "#", comment, flags=re.MULTILINE)
        # self._for_option(section_name, option_name, lambda node, _: self.parser.parse_comment(Tokenizer

    def replace_placeholder(self, placeholder, value):
        tmp = self.parser.serialize(self.document)
        tmp = tmp.replace("{" + placeholder + "}", value)
        self.document = self.parser.parse(tmp)

    def remove_placeholder(self, placeholder):
        tmp = self.parser.serialize(self.document)
        if placeholder.startswith("cfg_"):
            tmp = re.sub(r"^[ \t]*\{{{}}}[ \t]*$\n?".format(placeholder), "", tmp, flags=re.MULTILINE)
        else:
            tmp = tmp.replace("{" + placeholder + "}", "")
        self.document = self.parser.parse(tmp)

    def use_config(self, config):
        if self.filename is None:
            raise ValueError("Cannot use config without an initial file")

        logging.debug("Using config {}.{}".format(self.filename, config))
        with open(self.filename + "." + config, "r") as f:
            self.replace_placeholder("cfg_" + config, f.read())

    def placeholders(self):
        def collect_placeholders(node, ctx):
            if node["type"] == "placeholder":
                ctx.append(node["value"])
            return True, ctx

        return self.parser.walk_tree(self.document, collect_placeholders, [])

    def _expand(self, root_node, test, count, start_idx, whitespace="\n"):
        def expand(node, ctx):
            node.update(
                [
                    (attr.replace("%", str(ctx)), value.replace("%", str(ctx)) if isinstance(value, str) else value)
                    for attr, value in node.items()
                ]
            )
            return True, ctx

        for i, node in enumerate(root_node["body"]):
            if test(node):
                expanded = []
                for cnt in range(start_idx, start_idx + count):
                    to_expand = copy.deepcopy(node)
                    self.parser.walk_tree(to_expand, expand, cnt)
                    expanded.append(to_expand)
                    expanded.append(self.parser.whitespace(whitespace))
                root_node["body"][i : i + 1] = expanded

    def expand_section(self, section_name, count, start_idx=0, newline=False):
        logging.debug("Expanding section [{}]".format(section_name))

        def test(node):
            return node["type"] == "section" and node["name"] == section_name

        if not self.has_section(section_name):
            raise KeyError("Section '{}' not found".format(section_name))
        self._expand(self.document, test, count, start_idx, whitespace="\n" if newline else "")

    def expand_option(self, section_name, option_name, count, start_idx=0):
        logging.debug("Expanding option [{}] {}".format(section_name, option_name))
        regexp = re.compile(option_name)

        def test(node):
            return node["type"] == "option" and regexp.search(node["name"])

        section = self._get_section(section_name)
        self._expand(section, test, count, start_idx)

    def expand_value_line(self, section_name, option_name, value, count, start_idx=0):
        logging.debug("Expanding line in [{}] {} matching '{}'".format(section_name, option_name, value))
        regexp = re.compile(value)

        def test(node):
            return node["type"] == "value_line" and any(
                [regexp.search(entry["value"]) for entry in node["body"] if entry["type"] == "value_entry"]
            )

        option = self._get_option(section_name, option_name)
        self._expand(option["value"], test, count, start_idx, whitespace="")
