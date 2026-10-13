# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


# The inverse of yml2xml.py: turns plain XML (skin.xml / *.xmlinc) into the
# YAML dialect (skin.yml / *.ymlinc) yml2xml.py reads. See that file and
# the SkinForge wiki ("Authoring in YAML") for the schema itself; this file only needs to walk it
# in the other direction.
#
# Two different parsers are used, each for the right job:
#   - The outer XML container syntax (tags/attributes/comments/nesting) is a
#     small hand-rolled recursive-descent parser, same rationale as
#     yml2xml.py's hand-rolled YAML reader: no dependency beyond what ships
#     with Python, matching xmlinc.py's own approach to XML.
#   - The Python dict literal inside a <convert type="..."> template block
#     (see below) is parsed with the standard `ast` module. That content
#     really is Python source, and `ast` - part of the interpreter itself,
#     not a third-party package - is the correct tool for parsing Python,
#     the same way the hand-rolled reader is the correct tool for the small
#     YAML subset. Reimplementing a Python-expression parser by hand (for
#     operator precedence, unary minus, nested calls, ...) would just be a
#     worse copy of what `ast` already does. Needs Python 3.9+ (ast.unparse).
#
# This dialect assumes no mixed content: an element either holds only child
# elements (a container) or only text (a leaf), never both - true of every
# real skin.xml in this codebase, and the same assumption yml2xml.py's
# renderer makes on the way back.
#
# xmlinc's eval(formula) wrapper (e.g. position="0,eval($screen_height-48)")
# is compile-time plumbing, not part of the formula, so attribute values are
# unwrapped to just the formula on the way into YAML (see stripEval()).
# yml2xml.py's wrapEval() puts eval(...) back on the way to XML, using a
# heuristic since the YAML no longer carries an explicit marker for it.
#
# A <convert> block's template isn't rendered as a raw mirror of the Python
# call syntax - see toDomainCell()/parseConvertTemplate() below and
# the SkinForge wiki ("Authoring in YAML") for the domain-level "cell"/"fields" schema this
# produces, hiding MultiContentEntry*/RT_*/font-index plumbing for the shapes
# actually seen in this codebase (falls back to a lossless "raw" field, same
# shape as before, for anything that doesn't match one of those shapes).


import argparse
import ast
import os
import re
import sys
import textwrap
from FileUtils import readFile, writeFile


TEMPLATE_KEYS = (
    "template", "fonts", "itemHeight", "selectionEnabled",
    "scrollbarMode", "itemWidth", "orientation",
)
INDENT = "  "


class Commented:
    def __init__(self, value, comment=None):
        self.value = value
        self.comment = comment


class RawExpr(str):
    """A string that must render as bare YAML text (an identifier or Python
    expression), not a quoted YAML string - the reverse of the "raw" concept
    in yml2xml.py, here decided from the Python AST rather than a key name."""


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}


def xmlUnescape(text):
    for name, char in ENTITIES.items():
        text = text.replace(f"&{name};", char)
    return text


class XmlParseError(ValueError):
    """A parse failure with enough context (line/column, a snippet of the
    offending line, a caret under the exact position) to find and fix the
    spot directly - mirrors xmlinc.py's own XmlParseError, see that
    module for the rationale."""


# The elements whose multi-line text is code (the Python of an <applet>, the template of a <convert> that isn't a
# TemplatedMultiContent): it is kept exactly as written - tabs, blank lines, trailing spaces - so that converting to YAML
# and back changes nothing but the whitespace around the body (which xmlpretty sets anyway).
VERBATIM_TAGS = {"applet", "convert"}


def verbatimBody(raw):
    """*raw* without the blank lines before and after it and the trailing whitespace of its last line - nothing else."""
    lines = raw.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines[-1] = lines[-1].rstrip()
    return "\n".join(lines)


class XmlParser():
    def __init__(self, text):
        self.text = text
        self.pos = 0
        self.n = len(text)

    def fail(self, pos, msg):
        """Raises XmlParseError with a 1-based line/column and a
        single-line snippet of that line with a caret under the exact
        column. The file name itself isn't known here (this class only
        ever sees raw text) - callers that do know it are expected to
        catch and re-raise with that added, same as xmlinc.py's."""
        line = self.text.count("\n", 0, pos) + 1
        line_start = self.text.rfind("\n", 0, pos) + 1
        line_end = self.text.find("\n", pos)
        if line_end == -1:
            line_end = len(self.text)
        snippet = self.text[line_start:line_end]
        col = pos - line_start + 1
        caret = " " * (col - 1) + "^"
        raise XmlParseError(f"{msg} (line {line}, column {col}):\n{snippet}\n{caret}")

    def charAt(self, pos):
        return self.text[pos] if pos < self.n else None

    def skipSpace(self):
        while self.pos < self.n and self.text[self.pos].isspace():
            self.pos += 1

    def parseDocument(self):
        """Returns a single (tag, attrs, children, text) tuple for the
        common case of exactly one top-level element (unchanged from
        before - every existing caller keeps working untouched), or a list
        of Commented-wrapped ones if the source has more than one - true
        XML requires exactly one root, but a .xmlinc *fragment* (spliced
        into a parent document via literal text substitution) can be
        several bare sibling elements with nothing wrapping them, e.g.
        Common's screenpart_BottomBar.xmlinc (two sibling <eLabel/>s) -
        silently parsing and returning only the first one, as this used to,
        is real, silent data loss for any such file."""
        self.skipSpace()
        if self.text.startswith("<?", self.pos):
            end = self.text.find("?>", self.pos)
            if end == -1:
                self.fail(self.pos, "unterminated XML declaration, expected a closing '?>'")
            self.pos = end + 2
        self.skipSpace()
        elements = []
        pending_comment = None
        while self.pos < self.n:
            if self.text.startswith("<!--", self.pos):
                start = self.pos + 4
                end = self.text.find("-->", start)
                if end == -1:
                    self.fail(self.pos, "unterminated comment, expected a closing '-->'")
                pending_comment = self.text[start:end].strip()
                self.pos = end + 3
                self.skipSpace()
                continue
            if self.text[self.pos] != "<":
                break  # stray trailing whitespace/text at the top level
            elements.append(Commented(self.parseElement(), pending_comment))
            pending_comment = None
            self.skipSpace()
        if len(elements) == 1:
            return elements[0].value
        return elements

    def skipComment(self):
        end = self.text.find("-->", self.pos)
        if end == -1:
            self.fail(self.pos, "unterminated comment, expected a closing '-->'")
        self.pos = end + 3

    def parseName(self):
        start = self.pos
        while self.pos < self.n and (self.text[self.pos].isalnum() or self.text[self.pos] in "_:.-"):
            self.pos += 1
        if self.pos == start:
            self.fail(self.pos, f"expected a tag/attribute name, found {self.charAt(self.pos)!r}")
        return self.text[start:self.pos]

    def parseAttrValue(self):
        quote = self.charAt(self.pos)
        if quote not in ('"', "'"):
            self.fail(self.pos, f"expected an attribute value starting with a quote, found {quote!r}")
        start = self.pos
        self.pos += 1
        end = self.text.find(quote, self.pos)
        if end == -1:
            self.fail(start, f"unterminated attribute value, expected a closing {quote!r}")
        value = self.text[self.pos:end]
        self.pos = end + 1
        return xmlUnescape(value)

    def parseElement(self):
        if self.charAt(self.pos) != "<":
            self.fail(self.pos, f"expected '<' to start an element, found {self.charAt(self.pos)!r}")
        start = self.pos
        self.pos += 1
        tag = self.parseName()
        attrs = {}
        while True:
            self.skipSpace()
            if self.pos >= self.n:
                self.fail(start, f"unterminated start tag <{tag}>, ran off the end of the file")
            if self.text.startswith("/>", self.pos):
                self.pos += 2
                return tag, attrs, None, None
            if self.text[self.pos] == ">":
                self.pos += 1
                break
            name = self.parseName()
            self.skipSpace()
            if self.charAt(self.pos) != "=":
                self.fail(self.pos, f"expected '=' after attribute {name!r} of <{tag}>, found {self.charAt(self.pos)!r}")
            self.pos += 1
            self.skipSpace()
            attrs[name] = self.parseAttrValue()

        children, text = self.parseContent()
        if text is not None:
            if attrs.get("nostrip") == "1":
                text = xmlUnescape(text)   # nostrip="1": the leading/trailing whitespace is the value (a separator, ...)
            elif tag in VERBATIM_TAGS and "\n" in text.strip():
                text = xmlUnescape(verbatimBody(text))
            else:
                text = xmlUnescape(text.strip())
        end_tag_pos = self.pos
        end_tag = self.parseName()
        if end_tag != tag:
            self.fail(end_tag_pos, f"mismatched close tag: <{tag}> ... </{end_tag}>")
        self.skipSpace()
        if self.charAt(self.pos) != ">":
            self.fail(self.pos, f"expected '>' to close </{tag}>, found {self.charAt(self.pos)!r}")
        self.pos += 1
        return tag, attrs, children, text

    def parseContent(self):
        children = []
        pending_comment = None
        text_parts = []
        while True:
            if self.pos >= self.n:
                self.fail(self.pos, "unexpected end of file while looking for a closing tag")
            if self.text.startswith("</", self.pos):
                self.pos += 2
                break
            if self.text.startswith("<!--", self.pos):
                start = self.pos + 4
                end = self.text.find("-->", start)
                if end == -1:
                    self.fail(self.pos, "unterminated comment, expected a closing '-->'")
                pending_comment = self.text[start:end].strip()
                self.pos = end + 3
                continue
            if self.text[self.pos] == "<":
                node = self.parseElement()
                children.append(Commented(node, pending_comment))
                pending_comment = None
                continue
            next_lt = self.text.find("<", self.pos)
            if next_lt == -1:
                self.fail(self.pos, "unexpected end of file while looking for a closing tag")
            text_parts.append(self.text[self.pos:next_lt])
            self.pos = next_lt

        if children:
            return children, None
        text = "".join(text_parts)
        return None, (text if text.strip() else None)


# ---------------------------------------------------------------------------
# Python-template parsing (the <convert> special case), via `ast`
# ---------------------------------------------------------------------------

FORMULA_OP_RE = re.compile(r"\s*([+\-*/|])\s*")


def compactFormula(text):
    """ast.unparse() always spaces binary operators (e.g. "e - 350",
    "RT_HALIGN_LEFT | RT_VALIGN_CENTER"), but every hand-written
    TemplatedMultiContentEx grid-math formula and flags OR-chain in this
    codebase is written compact ("e-350", "RT_HALIGN_LEFT|RT_VALIGN_CENTER")
    - strip that spacing back out so the round trip doesn't introduce diff
    noise."""
    return FORMULA_OP_RE.sub(r"\1", text)


class HexInt(int):
    """An int literal that was actually written in hex in the source (e.g.
    border_color=0x595959) - a plain int has no memory of which base it was
    written in, so this carries that forward for renderKwargValue()/
    yamlScalar() to render back the same way, rather than guessing from the
    kwarg's name (a color that happened to be written in decimal should stay
    decimal, and a non-color int that happened to be hex should stay hex)."""


def exprToValue(node, text=None):
    if isinstance(node, ast.Call):
        result = {"call": node.func.id}
        if node.args:
            result["args"] = [exprToValue(a, text) for a in node.args]
        if node.keywords:
            result["kwargs"] = {kw.arg: exprToValue(kw.value, text) for kw in node.keywords}
        return result
    if isinstance(node, ast.Constant):
        value = node.value
        if (text is not None and isinstance(value, int) and not isinstance(value, bool)):
            segment = ast.get_source_segment(text, node)
            if segment and segment.strip().lower().startswith("0x"):
                return HexInt(value)
        return value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        return -node.operand.value
    if isinstance(node, (ast.Tuple, ast.List)):
        return [exprToValue(e, text) for e in node.elts]
    if isinstance(node, ast.Name):
        return RawExpr(node.id)
    return RawExpr(compactFormula(ast.unparse(node)))  # e.g. flags OR-chains, "e-485" grid math


def parseVarTuple(node):
    """"var": (ih := 70, hspace := 10, ...) - a tuple of walrus-bound local
    variables some plugins (e.g. TimeshiftCockpit) declare ahead of
    "template" and then reference throughout its pos=/size= expressions.
    Returns an ordered list of "name := expr" strings (order matters - a
    later binding can reference an earlier one by name) if `node` matches
    that exact shape, else None so the caller falls back to raw text rather
    than force-fitting something else. ast.unparse() on just elt.value (not
    the whole NamedExpr) reproduces the original expression with no
    spurious wrapping parens - verified separately against this exact
    real-world tuple."""
    if not isinstance(node, ast.Tuple):
        return None
    result = []
    for elt in node.elts:
        if not isinstance(elt, ast.NamedExpr) or not isinstance(elt.target, ast.Name):
            return None
        result.append(f"{elt.target.id} := {compactFormula(ast.unparse(elt.value))}")
    return result


def parseConvertTemplate(text):
    """Returns a dict body (var/fonts/template/itemHeight/...) if `text` is
    a TemplatedMultiContent(Ex) dict literal, else None (plain text
    convert)."""
    text = text.strip()   # a body kept as written starts with its own indentation
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return None
    if not isinstance(tree.body, ast.Dict):
        return None
    top = {}
    for key_node, value_node in zip(tree.body.keys, tree.body.values):
        if not isinstance(key_node, ast.Constant):
            return None
        key = key_node.value
        if key == "var":
            var_list = parseVarTuple(value_node)
            if var_list is None:
                return None  # unexpected "var" shape - leave as plain text
            top["var"] = var_list
            continue
        if key not in TEMPLATE_KEYS:
            return None  # not our template shape - leave as plain text
        top[key] = exprToValue(value_node, text)
    if "template" not in top:
        return None
    return top


# ---------------------------------------------------------------------------
# Domain-level cell schema: hides MultiContentEntry*/RT_*/font-index plumbing
# for the shapes actually seen in this codebase (see the SkinForge wiki, "Authoring in YAML").
# Anything that doesn't match one of these exact shapes falls back to a "raw"
# field carrying the untouched {call, args?, kwargs?} - never lossy, just not
# upgraded. Resolving a tuple-index (value: 0) to a semantic name (startHM)
# is explicitly out of scope: that needs a per-plugin mapping (e.g.
# TVMagazineCockpit/Index.py's `idx` dict) this tool has no way to discover.
# ---------------------------------------------------------------------------

TEXT_REQUIRED_KWARGS = {"pos", "size", "flags", "text"}
TEXT_OPTIONAL_KWARGS = {"font", "color", "color_sel"}
TEXT_ALLOWED_KWARGS = TEXT_REQUIRED_KWARGS | TEXT_OPTIONAL_KWARGS
ICON_REQUIRED_KWARGS = {"pos", "size", "png", "flags"}
ICON_ALLOWED_KWARGS = ICON_REQUIRED_KWARGS | {"backcolor"}
# MultiContentEntryPixmapAlphaTest is otherwise identical to the far more
# common ...AlphaBlend - the value here is the "variant:" a field carries to
# tell them back apart on the way to XML, None for the common case (left
# unmarked in the domain field, matching e.g. border_width only ever being
# present when relevant).
ICON_CALLS = {"MultiContentEntryPixmapAlphaBlend": None, "MultiContentEntryPixmapAlphaTest": "AlphaTest"}
PROGRESS_REQUIRED_KWARGS = {"pos", "size", "percent"}
PROGRESS_ALLOWED_KWARGS = PROGRESS_REQUIRED_KWARGS | {
    "borderWidth", "foreColor", "foreColorSelected", "backColor"}
BORDER_TEXT_KWARGS = {"pos", "size", "font", "flags", "text", "border_width", "border_color"}


def resolveFontIndex(fonts, index):
    """Returns "Family;Size" for a fonts[index] that's a plain two-arg
    gFont(family, size) call, else None - some plugins (e.g. TheraphosaCockpit)
    use parseFont("Family;Size") or other single-arg helpers instead, which
    this domain-schema shape has no way to represent; callers must fall back
    to "raw" rather than assume the two-arg shape and crash."""
    entry = fonts[index]
    args = entry.get("args", [])
    if entry.get("call") != "gFont" or len(args) != 2:
        return None
    family, size = args
    return f"{family};{size}"


def posSizeFields(kwargs):
    """Returns {"position": "x,y", "size": "w,h"} from a MultiContentEntry*
    call's pos=/size= kwargs - same "x,y" string convention position=/size=
    use on every other widget in this codebase, rather than a domain-only
    [x, y, w, h] array shape."""
    pos, size = kwargs["pos"], kwargs["size"]
    return {"position": f"{pos[0]},{pos[1]}", "size": f"{size[0]},{size[1]}"}


def toDomainField(entry, fonts):
    """Returns (kind, field) for one template entry - kind is text/icon/
    progress when the call matches one of those exact known shapes, else
    "raw" (field is then the original {call, args?, kwargs?} untouched)."""
    call = entry["call"]
    kwargs = entry.get("kwargs", {})
    keys = set(kwargs.keys())
    has_args = "args" in entry

    if (not has_args and call == "MultiContentEntryText"
            and TEXT_REQUIRED_KWARGS <= keys <= TEXT_ALLOWED_KWARGS):
        # font= is itself optional in the source (enigma2 defaults it when
        # omitted, e.g. TimeshiftCockpit's screenpart_TimeshiftOverviewList)
        # - only resolve/require it when the call actually has one; an
        # unresolvable font (present but not a plain gFont(family, size))
        # still falls back to raw, same as before.
        font = resolveFontIndex(fonts, kwargs["font"]) if "font" in kwargs else None
        if "font" not in kwargs or font is not None:
            field = posSizeFields(kwargs)
            if font is not None:
                field["font"] = font
            field["flags"] = kwargs["flags"]
            field["value"] = kwargs["text"]
            for k in ("color", "color_sel"):
                if k in kwargs:
                    field[k] = kwargs[k]
            return "text", field

    if (not has_args and call in ICON_CALLS
            and ICON_REQUIRED_KWARGS <= keys <= ICON_ALLOWED_KWARGS):
        field = {**posSizeFields(kwargs), "flags": kwargs["flags"], "value": kwargs["png"]}
        variant = ICON_CALLS[call]
        if variant:
            field["variant"] = variant
        if "backcolor" in kwargs:
            field["backcolor"] = kwargs["backcolor"]
        return "icon", field

    if (not has_args and call == "MultiContentEntryProgress"
            and PROGRESS_REQUIRED_KWARGS <= keys <= PROGRESS_ALLOWED_KWARGS):
        field = {**posSizeFields(kwargs), "value": kwargs["percent"]}
        for k in ("borderWidth", "foreColor", "foreColorSelected", "backColor"):
            if k in kwargs:
                field[k] = kwargs[k]
        return "progress", field

    # A raw entry's "font" kwarg is a plain index into the enclosing
    # template's fonts list - meaningless on its own once separated from
    # that list. Inline the actual font-call definition it points to so the
    # raw entry is fully self-contained (renderKwargValue/renderFlow already
    # know how to render a call-shaped kwarg value); toDomainCell() also
    # keeps the fonts list itself around verbatim, so this is purely a
    # readability convenience for the raw field, not that field's only tie
    # back to its font.
    font_idx = kwargs.get("font")
    if isinstance(font_idx, int) and not isinstance(font_idx, bool) and 0 <= font_idx < len(fonts):
        entry = {**entry, "kwargs": {**kwargs, "font": fonts[font_idx]}}
    return "raw", entry


def detectBorder(entries, fonts):
    """If entries[0] is the empty-text border-box trick (a full MultiContent
    Text entry whose only purpose is drawing a border, since text=""  never
    renders anything), pulls it out as (border_dict, width, remaining). Match
    must be exact - anything that deviates stays a normal field instead of
    being force-fit, so this can never lose information."""
    if not entries:
        return None, None, entries
    first = entries[0]
    kwargs = first.get("kwargs", {})
    if (first["call"] != "MultiContentEntryText" or "args" in first
            or set(kwargs.keys()) != BORDER_TEXT_KWARGS
            or kwargs.get("text") != ""
            or list(kwargs.get("pos", [])) != [0, 0]):
        return None, None, entries
    font = resolveFontIndex(fonts, kwargs["font"])
    if font is None:
        return None, None, entries
    border = {
        "width": kwargs["border_width"],
        "color": kwargs["border_color"],
        "font": font,
        "flags": kwargs["flags"],
    }
    return border, kwargs["size"][0], entries[1:]


def toDomainCell(body):
    fonts = body.get("fonts", [])
    entries = body["template"]

    cell = {}
    if "var" in body:
        cell["vars"] = body["var"]
    if "itemHeight" in body:
        cell["itemHeight"] = body["itemHeight"]
    for key in ("itemWidth", "selectionEnabled", "scrollbarMode", "orientation"):
        if key in body:
            cell[key] = body[key]

    border, width, entries = detectBorder(entries, fonts)
    if border is not None:
        cell["width"] = width
        cell["border"] = border

    cell["fields"] = [
        {kind: field} for kind, field in (toDomainField(entry, fonts) for entry in entries)
    ]

    # fonts[] is kept verbatim, in its original order, regardless of which
    # (if any) entries end up referencing each slot - font=N in a template
    # is a POSITIONAL reference, so silently dropping or renumbering an
    # entry (even one nothing currently points to) would be a real change,
    # not just a cosmetic one: anything that referenced it by that number
    # from outside this one <convert> block would now point at the wrong
    # font, or a font that no longer exists.
    if fonts:
        cell["fonts"] = fonts

    return cell


# ---------------------------------------------------------------------------
# YAML rendering
# ---------------------------------------------------------------------------

def stripEval(value):
    """Replace every eval(<formula>) in an attribute value with just
    <formula> - xmlinc's eval() wrapper is compile-time plumbing, not part
    of the formula itself, so it doesn't need to clutter the YAML. Scans by
    matching parens (not a regex) so a formula containing its own parens,
    e.g. eval(($width-100)/2), still unwraps correctly. yml2xml.py's
    wrapEval() puts the eval(...) back on the way to XML."""
    out = []
    i = 0
    n = len(value)
    while i < n:
        if value.startswith("eval(", i):
            depth = 1
            j = i + 5
            while j < n and depth > 0:
                if value[j] == "(":
                    depth += 1
                elif value[j] == ")":
                    depth -= 1
                j += 1
            out.append(value[i + 5:j - 1])
            i = j
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def yamlScalar(value):
    if isinstance(value, RawExpr):
        return str(value)
    if value is None:
        return "null"
    if isinstance(value, HexInt):
        return f"0x{value:06x}"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r"))
        return '"' + escaped + '"'
    return str(value)


def renderFlow(value):
    if isinstance(value, list):
        return "[" + ", ".join(renderFlow(v) for v in value) + "]"
    if isinstance(value, dict) and "call" in value:
        return renderCallFlow(value)
    return yamlScalar(value)


def renderCallFlow(node):
    parts = [f"call: {node['call']}"]
    if "args" in node:
        parts.append(f"args: {renderFlow(node['args'])}")
    if "kwargs" in node:
        kw = ", ".join(f"{k}: {renderFlow(v)}" for k, v in node["kwargs"].items())
        parts.append(f"kwargs: {{{kw}}}")
    return "{" + ", ".join(parts) + "}"


def renderCallBlock(node, indent, lines):
    prefix = INDENT * indent
    lines.append(f"{prefix}- call: {node['call']}")
    if "args" in node:
        lines.append(f"{prefix}{INDENT}args: {renderFlow(node['args'])}")
    if "kwargs" in node:
        lines.append(f"{prefix}{INDENT}kwargs:")
        for k, v in node["kwargs"].items():
            lines.append(f"{prefix}{INDENT}{INDENT}{k}: {renderKwargValue(k, v)}")


def renderKwargValue(_key, value):
    if isinstance(value, list):
        return renderFlow(value)
    if isinstance(value, dict) and "call" in value:
        return renderCallFlow(value)
    return yamlScalar(value)


def renderConvertBody(body, indent, lines):
    prefix = INDENT * indent
    lines.append(f"{prefix}template:")
    for entry in body["template"]:
        renderCallBlock(entry, indent + 1, lines)
    if "fonts" in body:
        lines.append(f"{prefix}fonts:")
        for font in body["fonts"]:
            renderCallBlock(font, indent + 1, lines)
    for key in ("itemHeight", "itemWidth", "selectionEnabled", "scrollbarMode", "orientation"):
        if key in body:
            lines.append(f"{prefix}{key}: {yamlScalar(body[key])}")


def renderDomainField(kind, field, indent, lines):
    prefix = INDENT * indent
    if kind == "raw":
        # field is an untouched {call, args?, kwargs?} - same shape/rendering
        # renderCallBlock() uses, just without its own leading "- call:" line
        # since the fields-list loop below already emitted the dash+kind line.
        lines.append(f"{prefix}call: {field['call']}")
        if "args" in field:
            lines.append(f"{prefix}args: {renderFlow(field['args'])}")
        if "kwargs" in field:
            lines.append(f"{prefix}kwargs:")
            for k, v in field["kwargs"].items():
                lines.append(f"{prefix}{INDENT}{k}: {renderKwargValue(k, v)}")
        return
    for key, value in field.items():
        lines.append(f"{prefix}{key}: {renderKwargValue(key, value)}")


def renderDomainCell(cell, indent, lines):
    prefix = INDENT * indent
    if "vars" in cell:
        lines.append(f"{prefix}vars:")
        for v in cell["vars"]:
            lines.append(f'{INDENT * (indent + 1)}- {yamlScalar(v)}')
    for key in ("itemHeight", "itemWidth", "width", "selectionEnabled", "scrollbarMode", "orientation"):
        if key in cell:
            lines.append(f"{prefix}{key}: {renderKwargValue(key, cell[key])}")
    if "border" in cell:
        lines.append(f"{prefix}border:")
        for key, value in cell["border"].items():
            lines.append(f"{prefix}{INDENT}{key}: {renderKwargValue(key, value)}")
    if "fonts" in cell:
        lines.append(f"{prefix}fonts:")
        for font in cell["fonts"]:
            # A plain two-arg gFont(family, size) renders as the same
            # "Family;Size" string every font: reference elsewhere in this
            # schema already uses - call:/args: is only needed as a lossless
            # fallback for anything else (e.g. parseFont(...)).
            if font.get("call") == "gFont" and len(font.get("args", [])) == 2:
                family, size = font["args"]
                lines.append(f'{INDENT * (indent + 1)}- "{family};{size}"')
            else:
                renderCallBlock(font, indent + 1, lines)
    lines.append(f"{prefix}fields:")
    dash_indent = indent + 1
    for item in cell["fields"]:
        (kind, field), = item.items()
        lines.append(f"{INDENT * dash_indent}- {kind}:")
        renderDomainField(kind, field, dash_indent + 2, lines)


def toYmlIncFile(filename):
    """An <xmlinc file="..."> reference is written against the XML dialect
    (xmlinc.py's own default-extension logic assumes ".xmlinc"), but once
    spliced into the YAML side it needs to point at that fragment's .yml
    counterpart instead, or a tool walking the YAML tree (e.g. tools/
    retrieveymlincs.py) would go looking for a file that was never
    generated. Mirrors deriveOutFile()'s whole-file mapping, just applied to
    one attribute value instead of the file being converted as a whole."""
    if filename.endswith(".xmlinc"):
        return filename[: -len(".xmlinc")] + ".ymlinc"
    return filename  # bare name (no extension) - xmlinc.py's own default applies either way


def renderAttrs(tag, attrs, indent, lines):
    prefix = INDENT * indent
    for k, v in attrs.items():
        if tag == "xmlinc" and k == "file":
            v = toYmlIncFile(v)
        lines.append(f'{prefix}{k}: {yamlScalar(stripEval(v))}')


def renderBody(tag, attrs, children, text, body_indent, lines):
    """Renders an element's body (everything after its own "tag:" or
    "- tag:" line, which the caller already emitted) at `body_indent` units.

    A child list item is always a single-key wrapper {tag: {...}} (see
    module docstring), so its OWN nested content sits two units past the
    dash, not one: the dash's "- " prefix is exactly one INDENT-width wide,
    so "tag:" already reads as one unit deeper than the dash itself, and
    that key's own value-mapping is one further unit past that. Contrast
    renderCallBlock() below, where "call"/"args"/"kwargs" are sibling keys
    of the SAME mapping (call's value is inline) and so sit one unit past
    the dash, not two - verified against real files both ways rather than
    derived from first principles alone.
    """
    if tag == "convert" and text:
        template = parseConvertTemplate(text)
        if template is not None:
            lines.append(f'{INDENT * body_indent}type: {yamlScalar(attrs.get("type", ""))}')
            lines.append(f"{INDENT * body_indent}cell:")
            renderDomainCell(toDomainCell(template), body_indent + 1, lines)
            return

    renderAttrs(tag, attrs, body_indent, lines)
    if text is not None:
        value = text
        if "\n" in value:
            # A literal block scalar, not yamlScalar()'s single-line
            # \n/\t-escaped quoted form - that form is unreadable at any
            # real size (this codebase's own raw-preserved convert bodies,
            # e.g. "COCTemplatedMultiContentEx", can run to 20+ lines) and
            # needs no escaping at all here since "|" preserves content
            # literally, quotes included.
            lines.append(f"{INDENT * body_indent}text: |")
            block_prefix = INDENT * (body_indent + 1)
            for line in value.split("\n"):
                lines.append(f"{block_prefix}{line}" if line else "")
        else:
            lines.append(f'{INDENT * body_indent}text: {yamlScalar(value)}')
    elif children:
        lines.append(f"{INDENT * body_indent}children:")
        dash_indent = body_indent + 1
        for item in children:
            if item.comment:
                lines.append(f"{INDENT * dash_indent}# {item.comment}")
            child_tag, child_attrs, child_children, child_text = item.value
            lines.append(f"{INDENT * dash_indent}- {child_tag}:")
            renderBody(child_tag, child_attrs, child_children, child_text, dash_indent + 2, lines)


def xml2yml(text):
    result = XmlParser(text).parseDocument()
    if isinstance(result, list):
        # More than one top-level sibling (see parseDocument()'s docstring)
        # - same "- tag:" dash-list shape renderBody already uses for a
        # element's own <children>, just at the document's own top level
        # instead of under a "children:" key, so there's nothing above the
        # dashes to indent relative to.
        lines = []
        for item in result:
            if item.comment:
                lines.append(f"# {item.comment}")
            tag, attrs, children, text_content = item.value
            lines.append(f"- {tag}:")
            renderBody(tag, attrs, children, text_content, 2, lines)
        return "\n".join(lines) + "\n"
    tag, attrs, children, text_content = result
    lines = [f"{tag}:"]
    renderBody(tag, attrs, children, text_content, 1, lines)
    return "\n".join(lines) + "\n"


def deriveOutFile(srcinfile):
    if srcinfile.endswith(".xmlinc"):
        return srcinfile[: -len(".xmlinc")] + ".ymlinc"
    if srcinfile.endswith(".xml"):
        return srcinfile[: -len(".xml")] + ".yml"
    raise ValueError(f"cannot derive output filename from: {srcinfile}")


def parseArgs(argv):
    parser = argparse.ArgumentParser(prog="xml2yml.py")
    parser.add_argument("-i", dest="srcinfile", required=True, help="source .xml/.xmlinc file")
    parser.add_argument("-o", dest="srcoutfile", default=None,
                        help="destination .yml/.ymlinc file (default: input filename with opposite extension)")
    return parser.parse_args(argv)


def main(argv):
    args = parseArgs(argv)
    srcoutfile = args.srcoutfile or deriveOutFile(args.srcinfile)
    print("src in file: " + args.srcinfile)
    print("src out file: " + srcoutfile)

    if not os.path.isfile(args.srcinfile):
        # readFile() already prints the OS-level reason (e.g. "No such file
        # or directory") and returns "" rather than raising, so without this
        # check xml2yml() would go on to parse empty text and fail with a
        # confusing IndexError instead of stopping here.
        print(f"ERROR: source file not found: {args.srcinfile}")
        sys.exit(1)

    try:
        output = xml2yml(readFile(args.srcinfile))
    except XmlParseError as e:
        print(f"ERROR: {args.srcinfile}: {e}")
        sys.exit(1)
    writeFile(srcoutfile, output)

    # print("xml2yml done.")


if __name__ == "__main__":
    main(sys.argv[1:])
