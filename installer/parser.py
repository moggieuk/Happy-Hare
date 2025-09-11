# Happy Hare MMU Software
#
# A tiny tokenizer > parser > AST > writer pipeline for Klipper-style .cfg files.
# It reads a config buffer, builds an in-memory tree (sections, options, comments, whitespace, placeholders),
# lets you query/mutate it, then serializes back to text while preserving layout.
#
#  - Layout preservation: whitespace/comments are first-class nodes and retained through round trips
#  - Indented multi-line values: indentation signals continuation; a bare newline without leading space ends the value
#  - Special-case gcode* options: parses their values literally to avoid misinterpreting gcode content
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import sys
import logging
import re

# Note {placeholder} style placeholders are no longer used with jinja templating
CONFIG_SPEC = [
    ("comment", re.compile(r"^[ \t]*[#;].*?(?=\{[^}]+\})")),
    ("comment", re.compile(r"^[ \t]*[#;].*")),
    ("whitespace", re.compile(r"^\s+")),
    ("section", re.compile(r"^\[.+\]")),
    ("word", re.compile(r"^\w[\w%]*")),
    ("assign_op", re.compile(r"^[:=]")),
    ("placeholder", re.compile(r"^\{(?:PIN_|PARAM_)[^%}]+\}")),
    ("unknown", re.compile(r"^\S")),
]

MAGIC_EXCLUSION_COMMENT = re.compile(r"^# EXCLUDE FROM CONFIG BUILDER.*")

if sys.version_info[0] >= 3:
    unicode = str


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

    def unread(self, s):
        if s:
            self.next_token = None
            self.slice = s + self.slice

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
                lines.append(unicode(" " * (depth + 1) * tab + "{}: `{}`").format(k, v))
            return True, lines

        lines = self.walk(_print, []) or []
        return unicode("\n".join(lines))

    def __str__(self):
        return self._pretty_print()

    def walk(self, callback, context=None, depth=0):
        _, context = callback(self, context, depth)
        return context

    def serialize(self):
        return unicode("")


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


class MagicExclusionNode(BodyNode):
    def __init__(self, body):
        # All nodes after the magic comment live here
        BodyNode.__init__(self, "magic_exclusion", body)

def _is_magic_comment_token(peek):
    return (peek is not None
            and peek.type == "comment"
            and MAGIC_EXCLUSION_COMMENT.match(peek.value.lstrip()))


class SectionNode(BodyNode):
    def __init__(self, name, body):
        BodyNode.__init__(self, "section", body)
        self.name = name

    def serialize(self):
        return unicode("[" + self.name + "]")


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

    def filter_tree(self, node, predicate):
        """
        Remove any node (and its subtree) for which predicate(node) is True.
        Operates in-place.
        """
        def _filter_tree(parent, cur):
            # If this node should be removed, prune it from the parent and stop descending.
            if predicate(cur):
                if isinstance(parent, BodyNode):
                    try:
                        parent.body.remove(cur)
                    except ValueError:
                        pass  # already removed or not present
                return

            # Otherwise descend into children if present.
            if isinstance(cur, BodyNode):
                for child in list(cur.body):  # iterate over a snapshot to avoid skipping
                    _filter_tree(cur, child)

        _filter_tree(None, node)

    def parse_document(self, tokenizer):

        def _parse_regular(peek, body):
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

        body = []
        peek = tokenizer.peek()

        while peek:
            # Start exclusion block: the magic comment and then everything to EOF
            if _is_magic_comment_token(peek):
                magic_comment = self.parse_comment(tokenizer)  # Keep the marker as a real comment
                exclusion_body = [magic_comment]

                peek2 = tokenizer.peek()
                while peek2:
                    _parse_regular(peek2, exclusion_body)
                    peek2 = tokenizer.peek()

                body.append(MagicExclusionNode(exclusion_body))
                break  # Everything else is now inside the exclusion node

            _parse_regular(peek, body)
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
            if _is_magic_comment_token(peek):
                break # Do not consume here—let parse_document() handle it

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
                if peek.value.startswith("\n") and peek.value.endswith("\n"):  # multi-line value ends with a newline without a tab/space after it
                    break

                token = tokenizer.take("whitespace")

                # Ensure whitespace for empty options is tokenized in ValueLineNode (only newline is standalone)
                m = re.search(r"\r?\n", token.value)
                if m and m.start() > 0:
                    tokenizer.unread(token.value[m.start():])
                    token.value = token.value[:m.start()]

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
    node.name = ctx
    return ctx


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

    # Dumps a tree view to stdout for debugging
    def pretty_print_document(self):
        def print_node(node, ctx, _):
            logging.debug(node._pretty_print())
            return True, ctx

        print(self.document._pretty_print())

    def write(self):
        def print_node(node, buffer, _):
            buffer += node.serialize()
            return True, buffer

        return self.document.walk(print_node, "")

    def excluded_nodes(self):
        """
        Return a list of all MagicExclusionNode instances in the current document,
        across all files that have been read/merged.
        """
        def collect_magic(node, acc, _depth):
            if isinstance(node, MagicExclusionNode):
                acc.append(node)
            return True, acc  # Keep descending
        return self.document.walk(collect_magic, [])

    def delete_excluded(self):
        """
        Remove every MagicExclusionNode (and all of its children) from the document.
        Returns the number of nodes removed.
        """
        to_remove = len(self.excluded_nodes())
        if to_remove == 0:
            return 0
        # Filter_tree prunes matching nodes and their entire subtree in-place
        self.parser.filter_tree(self.document, lambda n: isinstance(n, MagicExclusionNode))
        return to_remove

    def _iter_section_nodes(self, node, inside_excluded=False):
        """
        Yield (SectionNode, is_excluded) pairs from the tree rooted at `node`.
        Exclusion only flips to True when descending into a MagicExclusionNode.
        """
        if isinstance(node, SectionNode):
            yield node, inside_excluded
            return

        if isinstance(node, MagicExclusionNode):
            for child in node.body:
                # Descend with inside_excluded=True
                for pair in self._iter_section_nodes(child, True):
                    yield pair
            return

        if isinstance(node, DocumentNode):
            for child in node.body:
                # Reset inside_excluded at each top-level sibling (critical!)
                for pair in self._iter_section_nodes(child, False):
                    yield pair
            return

        # Other nodes cannot contain sections, so skip

    def _for_section(self, section_name, callback, ctx=None, scope="all"):
        """
        scope:
          - "included": only before the EXCLUDE marker
          - "excluded": only inside MagicExclusionNode
          - "all":      both
        """
        if scope not in ("all", "included", "excluded"):
            raise ValueError("scope must be 'all', 'included', or 'excluded'")

        out = ctx
        for sec, is_excl in self._iter_section_nodes(self.document, False):
            if section_name and sec.name != section_name:
                continue
            if scope == "included" and is_excl:
                continue
            if scope == "excluded" and not is_excl:
                continue
            out = callback(sec, out)
        return out

    def sections(self, scope="all"):
        return [x.name for x in self._for_section(None, collect, [], scope=scope)]

    def _get_section(self, section_name):
        section = self._for_section(section_name, identity)
        if section:
            return section
        else:
            raise KeyError("Section [{}] not found".format(section_name))

    def sections(self, scope="all"):
        return [x.name for x in self._for_section(None, collect, [], scope=scope)]

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
        self.parser.filter_tree(
            self.document,
            lambda n: isinstance(n, SectionNode) and n.name == section_name,
        )

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
        if value is None:
            logging.warning("{} in section [{}] has no value".format(option_name, section_name))
            value = ""
        value = value.strip()
        idx = value.find("\n")

        if idx != -1:
            value = value[:idx] + re.sub(r"^(?=\S)", r"    ", value[idx:], flags=re.MULTILINE)

        if self.has_option(section_name, option_name):
            option = self._get_option(section_name, option_name)

            if option.body and isinstance(option.body[0].body[0], WhitespaceNode):
                value = option.body[0].body[0].value + value
            else:
                value = " " + value
            value = self.parser.parse_value(Tokenizer(value, CONFIG_SPEC))
            option.body = value

        else:
            value = self.parser.parse_value(Tokenizer(" " + value, CONFIG_SPEC))
            section = self._get_section(section_name)
            section.body.append(OptionNode(option_name, value, self.parser.default_assign_op))
            section.body.append(WhitespaceNode("\n"))
            option = self._get_option(section_name, option_name)
            option.body = value

    def items(self, section_name):
        section = self._get_section(section_name)
        return [(item.name, item.value()) for item in section.body if isinstance(item, OptionNode)]

    def placeholders(self):
        def collect_placeholders(node, ctx, _):
            if isinstance(node, PlaceholderNode):
                ctx.append(node.value)
            return True, ctx

        return self.document.walk(collect_placeholders, [])




# Simple built-in command line round-trip test for the config parser. Use
# "--check" option to perform byte-by-btye comparison
# "--print" option to pretty print node tree
# 
# Usage:
#   python parser.py path/to/printer.cfg <in.cfg>
#   python parser.py path/to/printer.cfg --out <path/to/out.cfg> <in.cfg>
#   python parser.py path/to/printer.cfg --check <in.cfg>
#
if __name__ == "__main__":
    import argparse
    import difflib
    import sys
    from pathlib import Path

    def _decode_with_fallback(data: bytes, forced_encoding: str | None = None):
        """
        Return (text, used_encoding). Prefers utf-8/utf-8-sig, falls back to latin-1.
        Preserves UTF-8 BOM by switching to 'utf-8-sig' when present.
        """
        if forced_encoding:
            return data.decode(forced_encoding), forced_encoding

        # Detect UTF-8 BOM
        if data.startswith(b"\xef\xbb\xbf"):
            return data.decode("utf-8-sig"), "utf-8-sig"

        # Try UTF-8, then latin-1
        try:
            return data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            return data.decode("latin-1"), "latin-1"

    def _encode_with_bom_awareness(text: str, used_encoding: str) -> bytes:
        if used_encoding == "utf-8-sig":
            # Prepend BOM when encoding (utf-8-sig handles it automatically)
            return text.encode("utf-8-sig")
        return text.encode(used_encoding)

    def _make_parser():
        ap = argparse.ArgumentParser(
            description="Parse a Klipper-style config and write it back unchanged."
        )
        ap.add_argument("cfg", type=Path, help="Input config file")
        ap.add_argument(
            "--out",
            type=Path,
            help="Path to write round-tripped output (default: <cfg>.roundtrip)",
        )
        ap.add_argument(
            "--check",
            action="store_true",
            help="Exit 0 if input == output bytes, else 1 and print unified diff.",
        )
        ap.add_argument(
            "--encoding",
            help="Force encoding for read/write (default: auto-detect utf-8/utf-8-sig, fallback latin-1)",
        )
        ap.add_argument(
            "--print",
            action="store_true",
            help="Dump a pretty print of node tree for debugging",
        )
        return ap

    def _main():
        args = _make_parser().parse_args()

        # Read original bytes (no newline normalization)
        try:
            original_bytes = args.cfg.read_bytes()
        except Exception as e:
            sys.stderr.write("[ERROR] Failed to read '{}': {}\n".format(args.cfg, e))
            sys.exit(2)

        # Decode with basic BOM/encoding handling
        try:
            original_text, used_encoding = _decode_with_fallback(
                original_bytes, forced_encoding=args.encoding
            )
        except Exception as e:
            sys.stderr.write("[ERROR] Decoding failed: {}\n".format(e))
            sys.exit(2)

        # Parse > serialize
        try:
            parser = Parser()
            builder = ConfigBuilder(parser=parser)
            builder.read_buf(original_text)
            roundtripped_text = builder.write()
        except Exception as e:
            sys.stderr.write("[ERROR] Parsing/serialization failed: {}\n".format(e))
            sys.exit(2)

        # Encode output bytes (preserve BOM if used)
        try:
            out_bytes = _encode_with_bom_awareness(roundtripped_text, used_encoding)
        except Exception as e:
            sys.stderr.write("[ERROR] Encoding failed: {}\n".format(e))
            sys.exit(2)

        out_path = args.out or args.cfg.with_suffix(args.cfg.suffix + ".roundtrip")

        # Write exact bytes (no newline normalization)
        try:
            out_path.write_bytes(out_bytes)
        except Exception as e:
            sys.stderr.write("[ERROR] Failed writing '{}': {}\n".format(out_path, e))
            sys.exit(2)

        # Pretty print node structure for debugging
        if args.print:
            builder.pretty_print_document()

        if args.check:
            if original_bytes == out_bytes:
                print("OK: Round-trip is byte-for-byte identical.")
                sys.exit(0)
            else:
                print("MISMATCH: Round-trip differs. Unified diff:\n")
                # Decode both for human-friendly diff display
                # Use the same encoding we used to write (minus BOM for display)
                diff_from_enc = "utf-8" if used_encoding == "utf-8-sig" else used_encoding
                try:
                    lhs = original_bytes.decode(diff_from_enc, errors="replace")
                except Exception:
                    lhs = original_bytes.decode("latin-1", errors="replace")
                try:
                    rhs = out_bytes.decode(diff_from_enc, errors="replace")
                except Exception:
                    rhs = out_bytes.decode("latin-1", errors="replace")

                diff = difflib.unified_diff(
                    lhs.splitlines(keepends=True),
                    rhs.splitlines(keepends=True),
                    fromfile=str(args.cfg),
                    tofile=str(out_path),
                    n=3,
                )
                sys.stdout.writelines(diff)
                sys.exit(1)
        else:
            print("Wrote round-tripped file to: {}".format(out_path))

    _main()
