# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


# Converts the YAML skin dialect (skin.yml / *.ymlinc, see TVMagazineCockpit's
# skin/default for real examples) back into the plain XML xmlinc/enigma2
# understand. This is a hand-rolled parser for the specific subset of YAML
# that dialect actually uses - block mappings/sequences, flow lists ([a, b]),
# quoted/bare scalars, "|" block scalars, and standalone "#" comments - not a
# general-purpose YAML implementation, on purpose: it mirrors xmlinc.py's own
# hand-rolled XML handling rather than adding a third-party dependency that
# may not exist in the box's python (see SkinForge/README.md).
#
# Mapping (the inverse of what produced the .yml files):
#   - a mapping {tag: {attr: "val", ..., children: [...]}} becomes
#     <tag attr="val" ...>...children...</tag> (self-closed if no children
#     and no text).
#   - {tag: {..., text: "..."}} becomes <tag ...>text</tag>.
#   - a comment on a sequence item is emitted as <!-- comment --> immediately
#     before that item.
#   - SPECIAL CASE: a "convert" node holding a "cell" key (rather than plain
#     text/children) is a TemplatedMultiContent(Ex) template in the domain-
#     level schema documented in SkinForge/README.md and xml2yml.py's module
#     docstring - fromDomainCell() expands its text/icon/progress/raw fields
#     back into the {template, fonts, itemHeight, ...} shape described next,
#     which renderConvertTemplate()/pyCall() below turn into the actual
#     Python dict literal a <convert type="...">...</convert> block needs as
#     its text content. A "convert" node holding template/fonts/itemHeight/
#     selectionEnabled/scrollbarMode/itemWidth/orientation directly (the
#     older, low-level form - still what a "raw" domain field's own call/
#     kwargs use) is accepted the same way, without a "cell" wrapper.
#     Entries are {call: Name, args: [...]} and/or {call: Name, kwargs: {...}}
#     mappings, each rendered back to "Name(*args, **kwargs)" Python source.
#   - a plain <convert type="X">text</convert> (no "cell" or template keys)
#     is just an ordinary text-content element and goes through the normal
#     path.
#
# Scalar typing: a quoted YAML string ("...") and a plain/bare one both parse
# to a Python str - YAML discards the distinction, so it can't be used to
# decide code-vs-data. Instead, a few kwarg keys are hard-coded as "always
# raw source, never string-quoted" because they are the only ones that hold
# Python expressions rather than data in this domain: "call" (a callable
# name), "flags" (RT_*/BT_* constants, possibly OR'd), and "direction" (the
# GRADIENT_VERTICAL/GRADIENT_HORIZONTAL constants taken by the two
# LinearGradient entry types). Every other string value - including plain
# attributes and "text" - renders as a literal.
#
# Every factory in Components/MultiContent.py is supported the same generic
# way - {call: Name, args: [...]} / {call: Name, kwargs: {...}} renders to
# "Name(*args, **kwargs)" regardless of which one it names (Text, Pixmap,
# PixmapAlphaTest, PixmapAlphaBlend, Progress, ProgressPixmap, Rectangle,
# LinearGradient, LinearGradientAlphaBlend) - there's nothing type-specific
# to implement per entry. The one function that needs special support is
# MultiContentTemplateColor(n): unlike the entry types, it's never a
# top-level template item - it's used *nested inside* a color-ish kwarg
# value, e.g. color: {call: MultiContentTemplateColor, args: [24]}. See
# renderValue() below for where a value itself gets checked for "call".


import argparse
import os
import re
import sys
from FileUtils import readFile, writeFile


TEMPLATE_KEYS = (
    "template", "fonts", "itemHeight", "selectionEnabled",
    "scrollbarMode", "itemWidth", "orientation",
)
RAW_KWARG_KEYS = {"call", "flags", "direction"}
INDENT = "\t"
BARE_VAR_RE = re.compile(r"^\$\w+$")
FORMULA_OP_RE = re.compile(r"[+\-*/]")
IMAGE_FILE_RE = re.compile(r"\.(?:png|svg|jpg)$", re.IGNORECASE)

YAML_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "r": "\r"}


def unescapeYamlString(text):
    """Inverse of xml2yml.py's yamlScalar(): turns \\\\, \\", \\n, \\t, \\r
    back into their literal characters. Without this, a quoted scalar that
    came from multi-line source text (e.g. a <convert> body xml2yml.py
    couldn't match to the domain schema) round-trips as a single escaped
    line but gets read back with the backslash sequences still literal,
    corrupting the reconstructed Python/XML content."""
    result = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] in YAML_ESCAPES:
            result.append(YAML_ESCAPES[text[i + 1]])
            i += 2
            continue
        result.append(ch)
        i += 1
    return "".join(result)


class HexInt(int):
    """An int parsed from a bare "0x..." YAML scalar - carries that forward
    so pyVal() renders it back as hex source, the inverse of xml2yml.py's
    HexInt (see that file for why: preserve whichever base the original
    Python literal actually used, not a key-name guess)."""


class Commented:
    """Wraps a parsed sequence item together with a standalone comment line
    that preceded it in the source, if any."""

    def __init__(self, value, comment=None):
        self.value = value
        self.comment = comment


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class YamlParser():
    def __init__(self, text):
        self.entries = []
        for raw in text.split("\n"):
            stripped = raw.rstrip()
            if not stripped.strip():
                continue
            if "\t" in stripped[:len(stripped) - len(stripped.lstrip(" \t"))]:
                print("ERROR: tab used for indentation - YAML indentation must be spaces")
            indent = len(stripped) - len(stripped.lstrip(" "))
            content = stripped.strip()
            is_comment = content.startswith("#")
            self.entries.append((indent, content[1:].strip() if is_comment else content, is_comment))
        self.pos = 0

    def peek(self):
        return self.entries[self.pos] if self.pos < len(self.entries) else None

    def parseDocument(self):
        return self.parseBlock(0)

    def parseBlock(self, indent):
        i = self.pos
        while i < len(self.entries) and self.entries[i][2]:  # skip leading comments
            i += 1
        first = self.entries[i] if i < len(self.entries) else None
        if first is None or first[0] < indent:
            return None
        if first[1].startswith("- "):
            return self.parseSequence(indent)
        return self.parseMapping(indent)

    def parseSequence(self, indent):
        result = []
        pending_comment = None
        while True:
            e = self.peek()
            if e is None or e[0] != indent:
                break
            _, content, is_comment = e
            if is_comment:
                pending_comment = content
                self.pos += 1
                continue
            if not content.startswith("- "):
                break
            self.pos += 1
            value = self.parseSequenceItem(content[2:], indent + 2)
            result.append(Commented(value, pending_comment))
            pending_comment = None
        return result

    def parseSequenceItem(self, first_line, item_indent):
        if not first_line:
            return self.parseBlock(item_indent)
        # A quoted string or flow list/dict is always a self-contained
        # scalar, even if its own content happens to contain a literal ":"
        # (e.g. a "var:" binding string like "ih := 70") - only bare,
        # unquoted "key: value" text is actually this item's own inline
        # mapping shorthand.
        if first_line.startswith(('"', "[", "{")) or ":" not in first_line:
            return self.parseScalar(first_line)
        node = {}
        self.parseInlineKey(node, first_line, item_indent)
        while True:
            e = self.peek()
            if e is None or e[0] != item_indent or e[1].startswith("- "):
                break
            if e[2]:
                self.pos += 1  # skip stray comments between an item's own keys
                continue
            self.pos += 1
            self.parseInlineKey(node, e[1], item_indent)
        return node

    def parseMapping(self, indent):
        node = {}
        while True:
            e = self.peek()
            if e is None or e[0] != indent:
                break
            _, content, is_comment = e
            if is_comment:
                self.pos += 1  # standalone comments before/between keys aren't tracked
                continue
            if content.startswith("- "):
                break
            self.pos += 1
            self.parseInlineKey(node, content, indent)
        return node

    def parseInlineKey(self, node, line, indent):
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest == "|":
            node[key] = self.parseBlockScalar(indent + 2)
        elif rest == "":
            node[key] = self.parseBlock(indent + 2)
        else:
            node[key] = self.parseScalar(rest)

    def parseBlockScalar(self, indent):
        # e[1] is that line's content with ALL its own leading whitespace
        # already stripped (same tokenization every other line gets) - e[0]
        # is the leading-space count that got stripped, so re-adding
        # e[0]-indent spaces recovers exactly how much further than the
        # block's own baseline that line was indented, not just the bare
        # dedented text every other parseX() here works with.
        lines = []
        while True:
            e = self.peek()
            if e is None or e[0] < indent:
                break
            self.pos += 1
            lines.append(" " * (e[0] - indent) + e[1])
        return "\n".join(lines)

    def parseScalar(self, text):
        if text.startswith('"') and text.endswith('"'):
            return unescapeYamlString(text[1:-1])
        if text.startswith("[") and text.endswith("]"):
            return [self.parseScalar(item) for item in splitFlowItems(text[1:-1])]
        if text.startswith("{") and text.endswith("}"):
            result = {}
            for item in splitFlowItems(text[1:-1]):
                k, _, v = item.partition(":")
                result[k.strip()] = self.parseScalar(v.strip())
            return result
        low = text.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if low in {"null", "~"}:
            return None
        try:
            return HexInt(text, 16) if low.startswith("0x") else int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            pass
        return text  # bare scalar - stays a plain str, see module docstring


def splitFlowItems(inner):
    inner = inner.strip()
    if not inner:
        return []
    items = []
    depth = 0
    in_quote = False
    current = ""
    for ch in inner:
        if ch == '"':
            in_quote = not in_quote
            current += ch
        elif ch in "[{" and not in_quote:
            depth += 1
            current += ch
        elif ch in "]}" and not in_quote:
            depth -= 1
            current += ch
        elif ch == "," and depth == 0 and not in_quote:
            items.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        items.append(current.strip())
    return items


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def xmlEscapeText(value):
    text = str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def xmlEscapeAttr(value):
    return xmlEscapeText(value).replace('"', "&quot;")


def wrapEval(value):
    """Inverse of xml2yml.py's stripEval(): re-wrap a formula segment in
    eval(...) for xmlinc to evaluate at compile time. Splits on "," since
    every real eval() usage in this codebase wraps exactly one comma-segment
    of a position/size pair (e.g. "0,eval($screen_height-48)"), never a
    partial segment. A segment is wrapped only if it references a $var,
    isn't just that bare reference, AND actually contains a formula
    operator (+-*/) - a plain "$FR_big" (substitution, no eval needed),
    TemplatedMultiContentEx's "e"/"c" grid math (no $ at all, evaluated
    separately inside <convert> blocks), and a $var embedded in a literal
    with no arithmetic at all (e.g. name="picon$index", a plain per-cell
    substitution real skins use - see this module's docstring) all pass
    through untouched. Every real eval() in this codebase does contain an
    operator; nothing here is guessed without that evidence.

    A segment ending in an image extension is a file path (e.g.
    "IS_HD:$imagepath/icon_hd.png"), whose "/" is a directory separator,
    not division - never a formula."""
    if not isinstance(value, str) or "$" not in value:
        return value
    segments = value.split(",")
    wrapped = [
        seg if BARE_VAR_RE.match(seg.strip()) or "$" not in seg or not FORMULA_OP_RE.search(seg) or IMAGE_FILE_RE.search(seg.strip())
        else f"eval({seg})"
        for seg in segments
    ]
    return ",".join(wrapped)


def toXmlIncFile(filename):
    """Inverse of xml2yml.py's toYmlIncFile(): an <xmlinc file="..."> value
    read back from the YAML side points at a .ymlinc fragment, but the
    compiled XML needs the actual .xmlinc file xmlinc.py will look for on
    disk."""
    if not isinstance(filename, str):
        return filename
    if filename.endswith(".ymlinc"):
        return filename[: -len(".ymlinc")] + ".xmlinc"
    return filename  # bare name (no extension) - xmlinc.py's own default applies either way


def renderAttrs(tag, attrs):
    return "".join(
        f' {k}="{xmlEscapeAttr(wrapEval(toXmlIncFile(v) if tag == "xmlinc" and k == "file" else v))}"'
        for k, v in attrs.items()
    )


def pyStr(value):
    """repr(value) but always double-quoted, matching this codebase's
    hand-written convention (e.g. gFont("Regular", 30)) - Python's own
    repr() prefers single quotes unless the string itself contains one,
    which doesn't match real skin.xml sources here."""
    r = repr(value)
    if r.startswith("'") and r.endswith("'"):
        inner = r[1:-1].replace("\\'", "'").replace('"', '\\"')
        return '"' + inner + '"'
    return r


def pyVal(value, raw=False):
    if isinstance(value, dict) and "call" in value:
        return pyCall(value)  # nested call as a value, e.g. MultiContentTemplateColor(24)
    if isinstance(value, list):
        # pos=/size= tuples: every MultiContentEntry* factory takes numeric
        # pairs here, so a string element is always a TemplatedMultiContentEx
        # "e"/"c" expression (e.g. "e - 485"), never literal string data -
        # always raw, unlike a bare kwarg value where raw is key-dependent.
        # No space after the comma: unlike the kwargs-level comma spacing
        # (which does match hand-written sources like TVMagazineCockpit's
        # screenpart_EventCell), pos=/size= tuples across the codebase are
        # overwhelmingly written tight - pos=(10,0), not pos=(10, 0).
        return "(" + ",".join(pyVal(v, raw=True) for v in value) + ")"
    if isinstance(value, HexInt):
        return f"0x{value:06x}"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, str):
        return value if raw else pyStr(value)
    return repr(value)


def pyCall(node):
    # Space after the comma between kwargs/args, matching this codebase's
    # hand-written call style (e.g. MultiContentEntryText(pos=(5,0),
    # size=(360,40), font=0, ...)) - except gFont(...), which like
    # pos=/size= is written tight (gFont("Regular",32), not with a space).
    parts = [pyVal(a) for a in node.get("args", [])]
    parts += [f"{k}={pyVal(v, raw=(k in RAW_KWARG_KEYS))}"
              for k, v in node.get("kwargs", {}).items()]
    sep = "," if node["call"] == "gFont" else ", "
    return f"{node['call']}({sep.join(parts)})"


def unwrap(item):
    return item.value if isinstance(item, Commented) else item


VAR_BINDING_RE = re.compile(r"^(\w+)\s*(?::=|=)\s*(.*)$")


def parseVarBinding(v):
    """A cell.vars entry accepts either "name := expr" (matching the real
    walrus operator the compiled Python needs) or the more natural-looking
    "name = expr" - :=  is tried first so it always wins over the plain-=
    alternative when both would otherwise match. Always emitted back out as
    := (renderConvertTemplate below), since that's the only valid syntax
    inside the "var": (...) tuple literal itself - a bare = there would be
    a real Python SyntaxError, not just a style choice."""
    m = VAR_BINDING_RE.match(v)
    if not m:
        raise ValueError(f"cell.vars entry isn't a \"name := expr\" or \"name = expr\" binding: {v!r}")
    return m.group(1), m.group(2)


def renderConvertTemplate(body):
    # Real nested indentation (matching hand-written templates like
    # TVMagazineCockpit's screenpart_EventCell), not a flat block where the
    # caller applies one uniform prefix to every line: "{"/"}" sit at the
    # <convert> tag's own level, "template":/"fonts":/... one unit deeper,
    # and individual entries two units deeper. The caller (renderNode) adds
    # only the base prefix on top of what's built here.
    lines = ["{"]
    if "var" in body:
        lines.append(f'{INDENT}"var": (')
        for v in body["var"]:
            name, expr = parseVarBinding(v)
            lines.append(f"{INDENT * 2}{name} := {expr},")
        lines.append(f"{INDENT}),")
    lines.append(f'{INDENT}"template": [')
    for entry in body.get("template", []):
        lines.append(f"{INDENT * 2}{pyCall(unwrap(entry))},")
    lines.append(f"{INDENT}],")

    pairs = []
    if "fonts" in body:
        # pyVal, not pyCall: a fonts[] entry is either a gFont-call dict
        # (pyVal dispatches that to pyCall(), same as before) or a plain
        # string - either an already-resolved "Family;Size" literal, or an
        # unresolved "$var" symbolic reference (see internFonts()) that
        # needs to stay a quoted Python string literal so xmlinc.py's later
        # textual $-substitution can replace it in place without breaking
        # the surrounding list syntax. pyCall() alone would crash on a bare
        # string (it indexes ["call"] unconditionally).
        fonts = ", ".join(pyVal(unwrap(f)) for f in body["fonts"])
        pairs.append(f'"fonts": [{fonts}]')
    for key in ("itemHeight", "itemWidth"):
        if key in body:
            pairs.append(f'"{key}": {body[key]}')
    for key in ("selectionEnabled", "scrollbarMode", "orientation"):
        if key in body:
            pairs.append(f'"{key}": {pyVal(body[key])}')
    for i, pair in enumerate(pairs):
        suffix = "," if i < len(pairs) - 1 else ""
        lines.append(f"{INDENT}{pair}{suffix}")

    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Domain-level "cell" schema (the inverse of xml2yml.py's toDomainCell()) -
# see that file's module docstring for the schema itself. Reconstructs the
# same {template, fonts, itemHeight, ...} intermediate shape renderConvert
# Template()/pyCall() above already know how to turn into Python source, so
# nothing downstream of fromDomainCell() needs to change for this schema.
# ---------------------------------------------------------------------------

def unwrapField(item):
    (kind, field), = unwrap(item).items()
    return kind, field


def toIntOrExpr(s):
    """A pos=/size= element is either a plain int or (TemplatedMultiContentEx
    grid math / a cell.vars-bound name, e.g. "hspace//2") a raw Python
    expression - same distinction pyVal(v, raw=True) already renders
    (plain repr vs. verbatim text), so parse the same way in reverse:
    only actual integers become one, everything else stays the literal
    expression string."""
    try:
        return int(s)
    except ValueError:
        return s


def parsePosSize(field):
    """Inverse of xml2yml.py's posSizeFields(): "x,y"/"w,h" strings back to
    the [x, y]/[w, h] list shape MultiContentEntry*'s pos=/size= kwargs
    need."""
    def pair(s):
        a, b = s.split(",")
        return [toIntOrExpr(a), toIntOrExpr(b)]
    return pair(field["position"]), pair(field["size"])


def fontKey(font_val):
    """A font is either a domain-resolved "Family;Size" string (from a
    text/border field) or a raw {call, args} node (e.g. parseFont(...),
    inlined by xml2yml.py's toDomainField for a font it couldn't resolve to
    that string form) - dedup key is the string itself, or its repr, so
    identical raw font calls still collapse to one shared fonts[] entry
    instead of one per reference."""
    return font_val if isinstance(font_val, str) else repr(font_val)


def internFonts(cell):
    """Returns (fonts_list, font_index) - font_index maps a fontKey() (a
    domain-resolved "Family;Size" string, or a raw {call, args} node's own
    repr) to that font's slot in fonts_list.

    If the cell carries an explicit fonts: list (xml2yml.py's toDomainCell()
    always preserves the source template's fonts[] there, verbatim, in its
    original order - see that function's comment), fonts_list IS that list,
    unchanged: font=N is a POSITIONAL reference, possibly relied on by
    something outside this one <convert> block, so a round trip must never
    renumber or drop an entry - referenced or not - even though an
    unreferenced one has no visible effect on its own. Each original slot is
    registered under every key form a field might use to name it (both the
    resolved-string form a text/border field's font uses, when that slot is
    a plain two-arg gFont() call, and the raw dict's own repr a raw field's
    inlined font kwarg uses) so either lookup style finds the same original
    index; first occurrence wins if two slots would share a key.

    Falls back to the old collect-by-first-use behavior only when the cell
    predates this key (e.g. an older hand-written .ymlinc that inlines font
    strings directly on its fields without ever declaring a fonts: list)."""
    if "fonts" in cell:
        # cell["fonts"] is a YAML sequence, so its items arrive Commented-
        # wrapped (same as "fields"/"template" elsewhere) - unwrap before
        # inspecting. A plain two-arg gFont() entry is written as its
        # resolved "Family;Size" string (xml2yml.py's renderDomainCell) -
        # normalize it back to the {call, args} form every other downstream
        # consumer (pyCall, via renderConvertTemplate) expects; anything
        # else is already that raw dict form, unchanged.
        fonts = []
        font_index = {}
        for i, raw_entry in enumerate(cell["fonts"]):
            font_entry = unwrap(raw_entry)
            if isinstance(font_entry, str):
                if ";" in font_entry:
                    family, size = font_entry.split(";")
                    font_index.setdefault(font_entry, i)
                    font_entry = {"call": "gFont", "args": [family, int(size)]}
                    # A raw field with kwargs outside the domain "text" shape
                    # keeps its own font inlined as this same {call, args} dict
                    # (toDomainField's font-inlining) rather than the "Family;
                    # Size" string, and looks it up by that dict's repr -
                    # register it too so that lookup still finds this slot.
                    font_index.setdefault(repr(font_entry), i)
                else:
                    # Symbolic reference (e.g. "$F_medium", a <global> name
                    # declared elsewhere, like fonts.ymlinc) - yml2xml.py
                    # never sees <global> values (xmlinc.py resolves those
                    # later, as a generic textual $-substitution pass over
                    # the compiled XML - see its resolveValue()), so there's
                    # no "Family;Size" to split here. Leave it as the bare
                    # string, same as any other $var elsewhere in this file
                    # (wrapEval/BARE_VAR_RE): xmlinc.py's substitution turns
                    # it into the real "Family;Size" string in place. The
                    # runtime needs a gFont object in fonts[], not a bare
                    # string, so the entry is wrapped in parseFont(), which
                    # TemplatedMultiContent's eval namespace provides and
                    # which turns that string into a gFont. The fields keep
                    # referring to it by the symbolic name.
                    font_index.setdefault(font_entry, i)
                    font_entry = {"call": "parseFont", "args": [font_entry]}
            else:
                if font_entry.get("call") == "gFont" and len(font_entry.get("args", [])) == 2:
                    family, size = font_entry["args"]
                    font_index.setdefault(f"{family};{size}", i)
                font_index.setdefault(repr(font_entry), i)
            fonts.append(font_entry)
        return fonts, font_index

    order = []
    index = {}

    def register(font_val):
        key = fontKey(font_val)
        if key not in index:
            index[key] = len(order)
            order.append(font_val)
        return index[key]

    if "border" in cell and "font" in cell["border"]:
        register(cell["border"]["font"])
    for item in cell.get("fields", []):
        kind, field = unwrapField(item)
        if kind == "text" and "font" in field:
            register(field["font"])
        elif kind == "raw":
            font_val = field.get("kwargs", {}).get("font")
            if isinstance(font_val, dict):
                register(font_val)

    fonts = []
    for font_val in order:
        if isinstance(font_val, str):
            family, size = font_val.split(";")
            fonts.append({"call": "gFont", "args": [family, int(size)]})
        else:
            fonts.append(font_val)
    return fonts, index


def fromDomainField(kind, field, font_index):
    if kind == "raw":
        kwargs = field.get("kwargs")
        font_val = kwargs.get("font") if kwargs else None
        if isinstance(font_val, dict):
            # Inverse of toDomainField's font inlining: swap the raw font
            # call back for the integer index MultiContentEntry* actually
            # expects, now that it's registered in the shared fonts list.
            field = {**field, "kwargs": {**kwargs, "font": font_index[fontKey(font_val)]}}
        return field

    pos, size = parsePosSize(field)

    if kind == "text":
        kwargs = {"pos": pos, "size": size}
        if "font" in field:
            kwargs["font"] = font_index[field["font"]]
        kwargs["flags"] = field["flags"]
        kwargs["text"] = field["value"]
        for k in ("color", "color_sel"):
            if k in field:
                kwargs[k] = field[k]
        return {"call": "MultiContentEntryText", "kwargs": kwargs}

    if kind == "icon":
        call = "MultiContentEntryPixmapAlphaTest" if field.get("variant") == "AlphaTest" \
            else "MultiContentEntryPixmapAlphaBlend"
        kwargs = {"pos": pos, "size": size, "png": field["value"]}
        if "backcolor" in field:
            kwargs["backcolor"] = field["backcolor"]
        kwargs["flags"] = field["flags"]
        return {"call": call, "kwargs": kwargs}

    if kind == "progress":
        kwargs = {"pos": pos, "size": size, "percent": field["value"]}
        for k in ("borderWidth", "foreColor", "foreColorSelected", "backColor"):
            if k in field:
                kwargs[k] = field[k]
        return {"call": "MultiContentEntryProgress", "kwargs": kwargs}

    raise ValueError(f"unknown domain field kind: {kind}")


def fromDomainCell(cell):
    fonts, font_index = internFonts(cell)

    template = []
    if "border" in cell:
        b = cell["border"]
        template.append({"call": "MultiContentEntryText", "kwargs": {
            "pos": [0, 0], "size": [cell.get("width", 0), cell.get("itemHeight", 0)],
            "font": font_index[b["font"]], "flags": b["flags"], "text": "",
            "border_width": b["width"], "border_color": b["color"],
        }})

    for item in cell.get("fields", []):
        kind, field = unwrapField(item)
        template.append(fromDomainField(kind, field, font_index))

    body = {"template": template}
    if "vars" in cell:
        body["var"] = [unwrap(v) for v in cell["vars"]]
    if fonts:
        body["fonts"] = fonts
    for key in ("itemHeight", "itemWidth", "selectionEnabled", "scrollbarMode", "orientation"):
        if key in cell:
            body[key] = cell[key]
    return body


def renderNode(tag, body, level, lines):
    body = body or {}
    prefix = INDENT * level

    if tag == "convert" and ("cell" in body or any(k in body for k in TEMPLATE_KEYS)):
        attr_str = renderAttrs(tag, {"type": body.get("type", "")})
        lines.append(f"{prefix}<convert{attr_str}>")
        template_body = fromDomainCell(body["cell"]) if "cell" in body else body
        for line in renderConvertTemplate(template_body).split("\n"):
            lines.append(f"{prefix}{line}" if line else "")
        lines.append(f"{prefix}</convert>")
        return

    children = body.get("children")
    if children:
        # A "text" key alongside real children can only be a same-named
        # attribute (e.g. <widget text="UHD">...</widget>) - the special
        # element-own-text-content meaning only applies when there are no
        # children, mirroring xml2yml.py's mutually exclusive if/elif.
        text = None
        attrs = {k: v for k, v in body.items() if k != "children"}
    else:
        text = body.get("text")
        attrs = {k: v for k, v in body.items() if k not in ("children", "text")}
    attr_str = renderAttrs(tag, attrs)

    if children:
        lines.append(f"{prefix}<{tag}{attr_str}>")
        for item in children:
            if isinstance(item, Commented) and item.comment:
                lines.append(f"{prefix}{INDENT}<!-- {item.comment} -->")
            (child_tag, child_body), = unwrap(item).items()
            renderNode(child_tag, child_body, level + 1, lines)
        lines.append(f"{prefix}</{tag}>")
    elif tag == "convert" and text is not None and "\n" in text:
        # A convert type parseConvertTemplate() didn't recognize (e.g. this
        # codebase's "COCTemplatedMultiContentEx", which has its own "var"
        # key) falls all the way back to preserving its original body as one
        # opaque text blob - but that body is itself multi-line, so it needs
        # the same tag/body/closing-tag line-splitting the domain-schema
        # branch above gets, not glued onto <convert>/</convert> like a
        # single-line convert's (ClockToText, ServiceTime, ...) text is.
        lines.append(f"{prefix}<{tag}{attr_str}>")
        for line in xmlEscapeText(text).split("\n"):
            lines.append(f"{prefix}{line}" if line else "")
        lines.append(f"{prefix}</{tag}>")
    elif text is not None:
        lines.append(f"{prefix}<{tag}{attr_str}>{xmlEscapeText(text)}</{tag}>")
    else:
        lines.append(f"{prefix}<{tag}{attr_str}/>")


def yml2xml(text, is_full_document):
    root = YamlParser(text).parseDocument()
    lines = []
    if is_full_document:
        # Matches xmlpretty.py's minidom.toprettyxml() output exactly (no
        # encoding attribute - that's what every real compiled skin.xml has,
        # since xmlpretty is the actual last step that (re)writes this line).
        lines.append('<?xml version="1.0" ?>')
    if isinstance(root, list):
        # More than one top-level sibling (xml2yml.py's own docstring on
        # this - a bare "- tag:" dash list, the same shape a "children:"
        # list already uses) - a .xmlinc fragment can genuinely have more
        # than one root element with nothing wrapping them.
        for item in root:
            if isinstance(item, Commented) and item.comment:
                lines.append(f"<!-- {item.comment} -->")
            (tag, body), = unwrap(item).items()
            renderNode(tag, body, 0, lines)
    else:
        root_items = list(root.items())
        assert len(root_items) == 1, "document root must have exactly one top-level key"
        tag, body = root_items[0]
        renderNode(tag, body, 0, lines)
    return "\n".join(lines) + "\n"


def deriveOutFile(srcinfile):
    if srcinfile.endswith(".ymlinc"):
        return srcinfile[: -len(".ymlinc")] + ".xmlinc"
    if srcinfile.endswith(".yml"):
        return srcinfile[: -len(".yml")] + ".xml"
    raise ValueError(f"cannot derive output filename from: {srcinfile}")


def parseArgs(argv):
    parser = argparse.ArgumentParser(prog="yml2xml.py")
    parser.add_argument("-i", dest="srcinfile", required=True, help="source .yml/.ymlinc file")
    parser.add_argument("-o", dest="srcoutfile", default=None,
                        help="destination .xml/.xmlinc file (default: input filename with opposite extension)")
    return parser.parse_args(argv)


def main(argv):
    args = parseArgs(argv)
    srcoutfile = args.srcoutfile or deriveOutFile(args.srcinfile)
    # print("src in file: " + args.srcinfile)
    # print("src out file: " + srcoutfile)

    if not os.path.isfile(args.srcinfile):
        # readFile() already prints the OS-level reason (e.g. "No such file
        # or directory") and returns "" rather than raising, so without this
        # check yml2xml() would go on to parse empty text and fail with a
        # confusing AttributeError instead of stopping here.
        print(f"ERROR: source file not found: {args.srcinfile}")
        sys.exit(1)

    is_full_document = args.srcinfile.endswith(".yml")
    output = yml2xml(readFile(args.srcinfile), is_full_document)
    writeFile(srcoutfile, output)

    # print("yml2xml done.")


if __name__ == "__main__":
    main(sys.argv[1:])
