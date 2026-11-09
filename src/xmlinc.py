# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


import argparse
import atexit
import math
import os
import re
import struct
import shutil
import sys
import tempfile
from FileUtils import readFile, writeFile


# .xmlinc files are generated from their .ymlinc source and not kept in git:
# when the include search finds no .xmlinc, the .ymlinc beside it is
# converted (yml2xml, then xmlpretty - the same two steps ymlcompile used to
# run into the source tree) into a temporary directory that is removed when
# this run ends.
_generated_xmlinc = {}   # source .ymlinc path -> generated temp .xmlinc path
markFullPath = False     # --marks: the _src of an element names the .ymlinc with its whole path (see --redundant)
markSources = False      # xml2svg sets it: the generated .xmlinc carry _src="file:line" attributes (see yml2xml.py)
reportOnlyAttrs = None   # xml2svg sets it: an unknown $var is only an error in these attributes (None: in all of them)
_generated_dir = None


def _removeGeneratedDir():
    if _generated_dir:
        shutil.rmtree(_generated_dir, ignore_errors=True)


def generateXmlinc(xmlinc_path):
    """Return the path of a temporary .xmlinc generated from the .ymlinc that
    belongs to *xmlinc_path*, or None if there is no such .ymlinc."""
    global _generated_dir  # pylint: disable=global-statement
    base, ext = os.path.splitext(xmlinc_path)
    if ext != ".xmlinc":
        return None
    yml_path = base + ".ymlinc"
    if not os.path.isfile(yml_path):
        return None
    key = os.path.normpath(yml_path)
    if key in _generated_xmlinc:
        return _generated_xmlinc[key]

    from yml2xml import yml2xml    # imported here: only needed on demand
    from xmlpretty import process_file

    if _generated_dir is None:
        _generated_dir = tempfile.mkdtemp(prefix="xmlinc_")
        atexit.register(_removeGeneratedDir)
    # one sub-directory per source keeps the real file name (callers look at
    # it, e.g. the "applet_" and "Summary" checks) while same-named files
    # from different dirs (a plugin's override of a Common file) never collide
    out_dir = os.path.join(_generated_dir, str(len(_generated_xmlinc)))
    os.makedirs(out_dir)
    out = os.path.join(out_dir, os.path.basename(base) + ".xmlinc")
    print(f"==> generating {out} from {yml_path}")
    mark = (os.path.normpath(yml_path) if markFullPath else os.path.basename(yml_path)) if markSources else None
    writeFile(out, yml2xml(readFile(yml_path), False, mark))
    process_file(out, out)
    _generated_xmlinc[key] = out
    return out


ANCHOR_RE = re.compile(r"^([ec])([+-]\d+)?$")


def toInt(s):
    try:
        i = int(s)
    except Exception:
        i = s
    return i


def addMix(v1, v2):
    v1 = toInt(v1)
    v2 = toInt(v2)
    if isinstance(v1, str):
        r = v1
    elif isinstance(v2, str):
        r = v2
    else:
        return v1 + v2
    if v1 == 0 or v2 == 0:
        return r  # a zero offset leaves a symbolic position (center, e-58, ...) untouched
    num = v2 if isinstance(v1, str) else v1
    m = ANCHOR_RE.match(r)
    if m and isinstance(num, int):
        # "e" (end of parent) / "c" (center) anchors take an offset as e+N / e-N
        total = int(m.group(2) or 0) + num
        return m.group(1) + (f"{total:+d}" if total else "")
    print(f"==> addMix: ERROR: cannot add non-numeric position values {v1!r} + {v2!r}, keeping {r!r}")
    return r


# <panel position=...> keywords that name a layout slot instead of a
# coordinate pair - an offset must leave them alone ("top" + 0,0 would
# otherwise be written out as "top,0", which enigma2 no longer recognises).
LAYOUT_POSITIONS = {"fill", "top", "bottom", "left", "right"}

# Rendered line height per unit of the skin's font size: the font's own line
# height (ascent+descent+gap, per em) times the "scale" percentage declared in
# fonts.xmlinc (enigma2 renders at size*scale/100). Regular = DejaVuSans @89%,
# Bold = Roboto-Bold @71%; CAID/Fixed/Console are rendered at 100%. Unknown
# families keep the old conservative 4/3.
FONT_LINE_HEIGHT = {"Regular": 1.036, "Bold": 0.832, "CAID": 1.104, "Fixed": 1.125, "Console": 1.201}
FONT_LINE_HEIGHT_DEFAULT = 4.0 / 3.0

# The check below prefers what the skin declares: a font family (<font name=... filename=... scale=.../> in
# fonts.xmlinc) is rendered at size*scale/100 and its line is (ascender - descender + line gap) / unitsPerEm
# of the font file high. That is read from the font file if it can be found (in the skin directories, in
# $SKINFORGE_FONTS, in enigma2's data/fonts next to SkinForge or in /usr/share/fonts); if not, from this table
# of the same numbers (per em), and for an unknown file the conservative default above.
FONT_FILE_LINE_HEIGHT = {
    "DejaVuSans.ttf": 1.1641, "Roboto-Bold.ttf": 1.1719, "OpenSans-Regular.ttf": 1.3618, "OpenSans-Semibold.ttf": 1.3618,
    "OpenSans-Italic.ttf": 1.3618, "DroidSans-Bold.ttf": 1.1699, "andale.ttf": 1.125, "Bignoodletitling.ttf": 1.104,
}
_font_metrics_cache = {}


def readFontLineHeight(path):
    """Line height per em of a TrueType/OpenType font file (hhea table), or None if it can't be read."""
    if path in _font_metrics_cache:
        return _font_metrics_cache[path]
    result = None
    try:
        with open(path, "rb") as f:
            data = f.read()
        count = struct.unpack(">H", data[4:6])[0]
        tables = {}
        for i in range(count):
            tag, _checksum, offset, _length = struct.unpack(">4sIII", data[12 + 16 * i:28 + 16 * i])
            tables[tag] = offset
        units = struct.unpack(">H", data[tables[b"head"] + 18:tables[b"head"] + 20])[0]
        ascender, descender, gap = struct.unpack(">hhh", data[tables[b"hhea"] + 4:tables[b"hhea"] + 10])
        if units:
            result = (ascender - descender + gap) / units
    except Exception:
        result = None
    _font_metrics_cache[path] = result
    return result


class Pos():
    def __init__(self, x, y=0):
        if isinstance(x, str):
            if "," in x:
                pos_string = x.split(",")
                x = pos_string[0]
                y = pos_string[1]
        self.x = toInt(x)
        self.y = toInt(y)

    def __add__(self, other):
        new_x = addMix(self.x, other.x)
        new_y = addMix(self.y, other.y)
        return Pos(new_x, new_y)

    def __str__(self):
        return f"{self.x},{self.y}"


# Base color names defined by the device's own GUI skin (e.g. MetrixHD's
# skin_base.xml) that are available to every plugin skin without being
# declared in colors.xmlinc. Kept intentionally small - only
# names actually verified to exist there, so a real typo or a color that
# was never defined anywhere still gets flagged.
KNOWN_BASE_COLORS = {
    "black", "white", "red", "green", "blue", "yellow",
    "grey", "gray", "darkgrey", "darkgray", "orange",
    "transparent", "foreground", "background",
}

VAR_RE = re.compile(r"\$\w+")
# The old form of a default: <default tag="widget" .../>, tag="widget[render=Label]" (only the widgets of that render) or
# tag="widget[itemHeight]" (only the widgets that set that attribute themselves). See scanDefaults for the current form.
DEFAULT_TAG_SELECTOR_RE = re.compile(r"^(?P<tag>\w+)(?:\[(?:render=(?P<render>[^\]=]+)|(?P<has>\w+))\])?$")

# One condition of a default: [not] attribute, attribute=value or attribute!=value
CONDITION_RE = re.compile(r"^(?:(?P<neg>not)\s+)?(?P<attr>\w+)\s*(?:(?P<op>!=|=)\s*(?P<value>.+))?$")


def parseConditions(text):
    """The conditions of a default (the "if" of a <defaults> entry): "render=Listbox and not itemCornerRadius". Returns
    a list of (attribute, op, value), op being "has" (the attribute is there, whatever its value), "absent", "eq" or "ne"
    (value is None for the first two). ValueError for a text that cannot be read."""
    conditions = []
    for part in re.split(r"\s+and\s+", text.strip()):
        m = CONDITION_RE.match(part.strip())
        if m is None:
            raise ValueError(f"cannot read the condition '{part}' (attribute, not attribute, attribute=value or attribute!=value)")
        negated = m.group("neg") is not None
        if m.group("op") is None:
            conditions.append((m.group("attr"), "absent" if negated else "has", None))
        else:
            equal = (m.group("op") == "=") != negated    # "not a=b" is "a!=b"
            conditions.append((m.group("attr"), "eq" if equal else "ne", m.group("value").strip()))
    return conditions


def negateCondition(condition):
    """The condition that is met when this one is not (for the else of an if)."""
    attr, op, value = condition
    return (attr, {"has": "absent", "absent": "has", "eq": "ne", "ne": "eq"}[op], value)


def conditionText(condition):
    attr, op, value = condition
    return {"has": attr, "absent": f"not {attr}", "eq": f"{attr}={value}", "ne": f"{attr}!={value}"}[op]


def conditionHolds(condition, attrs):
    """Whether an element with these attributes meets the condition."""
    attr, op, value = condition
    if op == "has":
        return attr in attrs
    if op == "absent":
        return attr not in attrs
    if op == "eq":
        return attrs.get(attr) == value
    return attrs.get(attr) != value


def isDefaultElement(tag):
    """True for the elements that declare defaults (<defaults> with default widgets in it, and the old <default tag=.../>):
    they are for the compiler only and never appear in the compiled output."""
    return tag in {"defaults", "default"}


# ---------------------------------------------------------------------------
# XML tree parser - same recursive-descent approach as xml2yml.py's
# XmlParser (see that file for the rationale: no third-party dependency),
# adapted for xmlinc's own needs: attribute values and text content are
# captured raw and unescaped-only here - $var substitution and eval() are
# semantic operations applied later, during the tree-walk in XMLInclude,
# not part of parsing. A document with more than one top-level element is
# supported (see parseDocument()) since an xmlinc *source* file, unlike a
# real XML document, isn't required to have exactly one root - it's spliced
# into its parent by element substitution, not loaded standalone.
# ---------------------------------------------------------------------------

class Comment:
    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text


class RawText:
    """An applet_* include's content - spliced in completely unprocessed
    and unescaped (it isn't necessarily even valid XML), wherever in the
    tree the <xmlinc file="applet_..."/> that pulled it in appears - a
    top-level sibling or nested inside another element's children."""
    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text


class Element:
    __slots__ = ("tag", "attrs", "children", "text", "screen_origin")

    def __init__(self, tag, attrs, children, text):
        self.screen_origin = False  # position is relative to the screen, not to a frame (origin="screen")
        self.tag = tag
        self.attrs = attrs          # dict, insertion order = source order
        self.children = children    # list of Element/Comment, or None
        self.text = text            # str, or None (mutually exclusive with children)


class XmlParseError(ValueError):
    """A parse failure with enough context (line/column, a snippet of the
    offending line, a caret under the exact position) to find and fix the
    spot directly, instead of the bare AssertionError this used to raise -
    the position alone was useless without re-deriving line/column and
    re-opening the file by hand."""


class XmlParser:
    def __init__(self, text):
        self.text = text
        self.pos = 0
        self.n = len(text)

    def fail(self, pos, msg):
        """Raises XmlParseError with a 1-based line/column and a
        single-line snippet of that line with a caret under the exact
        column - the file name itself isn't known here (this class only
        ever sees raw text), so callers that do know it (processInclude/
        processFile) are expected to catch and re-raise with that added."""
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
        self.skipSpace()
        if self.text.startswith("<?", self.pos):
            end = self.text.find("?>", self.pos)
            if end == -1:
                self.fail(self.pos, "unterminated XML declaration, expected a closing '?>'")
            self.pos = end + 2
            self.skipSpace()
        nodes = []
        while self.pos < self.n:
            if self.text.startswith("<!--", self.pos):
                nodes.append(self.parseComment())
                self.skipSpace()
                continue
            if self.text[self.pos] != "<":
                break  # stray trailing whitespace/text at the top level
            nodes.append(self.parseElement())
            self.skipSpace()
        if len(nodes) == 1 and isinstance(nodes[0], Element):
            return nodes[0]
        return nodes

    def parseComment(self):
        start = self.pos + 4
        end = self.text.find("-->", start)
        if end == -1:
            self.fail(self.pos, "unterminated comment, expected a closing '-->'")
        comment = Comment(self.text[start:end].strip())
        self.pos = end + 3
        return comment

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
        return value

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
                return Element(tag, attrs, None, None)
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

        children, text = self.parseContent(attrs.get("nostrip") in {"1", "true", "True"})
        if not self.text.startswith("</", self.pos):
            self.fail(start, f"unclosed tag <{tag}>, expected a matching </{tag}>")
        self.pos += 2
        end_tag_pos = self.pos
        end_tag = self.parseName()
        if end_tag != tag:
            self.fail(end_tag_pos, f"mismatched close tag: <{tag}> ... </{end_tag}>")
        self.skipSpace()
        if self.charAt(self.pos) != ">":
            self.fail(self.pos, f"expected '>' to close </{tag}>, found {self.charAt(self.pos)!r}")
        self.pos += 1
        return Element(tag, attrs, children, text)

    def parseContent(self, nostrip=False):
        children = []
        text_parts = []
        while True:
            if self.pos >= self.n:
                self.fail(self.pos, "unexpected end of file while looking for a closing tag")
            if self.text.startswith("</", self.pos):
                break
            if self.text.startswith("<!--", self.pos):
                children.append(self.parseComment())
                continue
            if self.text[self.pos] == "<":
                children.append(self.parseElement())
                continue
            next_lt = self.text.find("<", self.pos)
            if next_lt == -1:
                self.fail(self.pos, "unexpected end of file while looking for a closing tag")
            text_parts.append(self.text[self.pos:next_lt])
            self.pos = next_lt
        if children:
            return children, None
        text = "".join(text_parts)
        if not nostrip:  # enigma2's own nostrip="1": leading/trailing whitespace is significant (e.g. a separator like "  •  ")
            text = text.strip()
        return None, (text if text else None)


def renderAttrs(attrs):
    return "".join(f' {k}="{v}"' for k, v in attrs.items())


def renderNode(node, lines):
    if isinstance(node, RawText):
        lines.extend(node.text.splitlines())
        return
    if isinstance(node, Comment):
        lines.append(f"<!-- {node.text} -->")
        return
    attr_str = renderAttrs(node.attrs)
    if node.children:
        lines.append(f"<{node.tag}{attr_str}>")
        for child in node.children:
            renderNode(child, lines)
        lines.append(f"</{node.tag}>")
    elif node.text is not None:
        lines.append(f"<{node.tag}{attr_str}>{node.text}</{node.tag}>")
    else:
        lines.append(f"<{node.tag}{attr_str}/>")


# ---------------------------------------------------------------------------
# xmlinc: walks the parsed tree, resolving <xmlinc>/<global>/<screen>/
# <layout> and every $var/eval()/position offset - the semantic layer that
# used to be entangled with parsing itself in the old line/token version.
# ---------------------------------------------------------------------------

class XMLInclude:
    def __init__(self, srcdir, dstdir, cmndir):
        self.srcdir = srcdir
        self.dstdir = dstdir
        self.cmndir = cmndir
        self.globals = {}
        self.locals = []         # one {"$name": value} per file being processed (innermost last), see <local>
        self.layouts = []
        self.colors = {}
        self.font_aliases = {}   # <alias name=... font=... size=.../> declarations: name -> (face, size)
        self.font_defs = {}      # <font name=... filename=... scale=.../> declarations: name -> (filename, scale in %)
        # the default widgets (<defaults>): tag -> {conditions: {"values", "order", "conditions"}}, see scanDefaults
        self.default_rules = {}
        self.default_order = 0
        self.default_filled = {}   # (tag, rule, attribute) -> how many elements got the attribute from that rule
        # --redundant: (_src, attribute) -> [how often the element was compiled with it, how often the attribute was the same as the
        # default that would have been filled in], see applyDefaults. None: not asked for.
        self.redundant_report = None
        self.current_screen = "?"
        self.current_file = ""
        self.output_size = None  # (xres, yres) of the <resolution> declared by the skin, if any
        # the output the skins are designed for, until a <resolution> says otherwise: a plugin skin has none of its
        # own (it is merged into the skin of the box) but its windows are sized from it, see frames.ymlinc
        self.globals["$output_width"], self.globals["$output_height"] = "1920", "1080"
        self.frame_origin = None  # Pos of the frame (see processInclude) placed by what was just processed
        self.last_font_var = None
        # Only for debugging one screen (xmlcompilescreen): the stems (file name without extension) of the screen_*
        # files that are compiled. The other screen_* includes of the skin are not (their .ymlinc is not even
        # converted), see skipsScreen(). None: every screen is compiled.
        self.screen_filter = None
        self.screen_include_depth = 0  # how many screen_* files are being processed: their includes are never skipped
        self.target_screens = set()    # id() of the <screen> elements that came from the files of screen_filter

    def inSkin(self):
        """True while the skin itself is being processed, not a screen_* file it includes."""
        return bool(self.screen_filter) and not self.screen_include_depth

    def skipsScreen(self, inc_filename):
        """True for a screen_* include of the skin itself that is not one of the screens to compile. A screen_* file
        included by a screen (a panel) is never skipped: it belongs to the screen that includes it."""
        if not self.inSkin():
            return False
        stem = os.path.splitext(os.path.basename(inc_filename))[0]
        return stem.startswith("screen_") and stem not in self.screen_filter

    def getIncFilePath(self, inc_filename):
        if not os.path.splitext(inc_filename)[1]:
            inc_filename += ".xmlinc"
        print(f"inc_filename: {inc_filename}")

        # Same search order as always: the source dir, its parent, the common
        # dir, its parent. In each one a real .xmlinc wins; failing that, a
        # .ymlinc next to where it would be is converted on demand (the
        # .xmlinc files are generated build intermediates, not kept in git).
        search_dirs = (self.srcdir, os.path.dirname(self.srcdir),
                       self.cmndir, os.path.dirname(self.cmndir))
        for i, directory in enumerate(search_dirs, 1):
            inc_file = os.path.join(directory, inc_filename)
            print(f"inc_file {i}: {inc_file}")
            if os.path.exists(inc_file):
                return inc_file
            generated = generateXmlinc(inc_file)
            if generated:
                return generated

        print(f"ERROR: inc file: {inc_file} not found.")
        return inc_file

    def localVars(self):
        """The <local> variables of the file being processed: nothing else sees them, not the files it includes
        and not the file that included it."""
        return self.locals[-1] if self.locals else {}

    def resolveVar(self, var, attr=None):
        local_vars = self.localVars()
        if var in local_vars:
            return local_vars[var]
        if var in self.colors:
            return self.colors[var]
        if var in self.globals:
            value = self.globals[var]
            if var.startswith(("$FR_", "$FB_")):
                self.last_font_var = var
            return value
        try:
            return str(int(var.strip("$")))
        except Exception:
            if reportOnlyAttrs is None or attr in reportOnlyAttrs:
                print(f"ERROR: global {var} not found.")
            return var

    def substituteKnownVars(self, text):
        """Replaces $name by its value, but only for names the skin actually defines (a global
        or a color): python code may legitimately contain a "$" (a regex, a format string), so
        an unknown $name is left alone instead of being reported as an error. No eval()
        handling either - eval(...) in python code is the builtin."""
        if not text or "$" not in text:
            return text

        local_vars = self.localVars()

        def sub(m):
            var = m.group(0)
            if var in local_vars:
                return local_vars[var]
            if var in self.colors:
                return self.colors[var]
            if var in self.globals:
                return self.globals[var]
            return var

        return VAR_RE.sub(sub, text)

    def normalizeText(self, text):
        """Tightens "kwarg = value" to "kwarg=value" (and "; " to ";") in
        element text content (a <convert> block's Python-source body,
        mainly) - the original tool's clean() step did this as a side
        effect of also flattening the whole file to one line per tag, which
        this rewrite's real XML parsing makes unnecessary for structure,
        but a hand-typed convert body written with that looser "=" spacing
        still needs tightening to match this codebase's convention.

        Deliberately does NOT touch newlines/indentation: a source fragment
        that's already nicely multi-line (e.g. after an earlier xmlpretty
        run) should stay that way once spliced into the compiled skin.xml -
        xmlpretty's own reformatting handles either already-multi-line or
        single-line input fine for a recognized TemplatedMultiContent(Ex)
        body (it rebuilds structure from scratch), but for a convert type
        it doesn't recognize (raw passthrough) it can only re-indent
        existing lines, not rebuild missing structure - so flattening here
        would strip formatting nothing downstream can restore."""
        return text.replace(" = ", "=").replace("; ", ";")

    def resolveInOutputSize(self, value):
        """resolveValue() with $screen_width/$screen_height set to the output size."""
        saved = (self.globals.get("$screen_width"), self.globals.get("$screen_height"))
        self.globals["$screen_width"], self.globals["$screen_height"] = self.output_size
        try:
            return self.resolveValue(value, "position")
        finally:
            for var, val in zip(("$screen_width", "$screen_height"), saved):
                if val is None:
                    self.globals.pop(var, None)
                else:
                    self.globals[var] = val

    def substituteScreenCenter(self, value):
        """$screen_center is the centre of the screen along the axis the value is on: in an "x,y"
        pair the first one is $screen_width/2, the second $screen_height/2."""
        segments = value.split(",")
        if len(segments) != 2 or "$screen_width" not in self.globals or "$screen_height" not in self.globals:
            print(f"ERROR: $screen_center needs an x,y pair and a known screen size, found {value!r}")
            return value
        centers = [str(int(self.globals["$screen_width"]) // 2), str(int(self.globals["$screen_height"]) // 2)]
        return ",".join(re.sub(r"\$screen_center\b", centers[i], seg) for i, seg in enumerate(segments))

    def resolveValue(self, value, attr=None):
        """$var substitution, then eval(...) evaluation, in that order (a
        formula can reference a global by name) - applied to one attribute
        value or text-content string at a time, each fully self-contained
        (an XML-parsed attribute value's boundaries are never ambiguous the
        way they were for the old token-position approach)."""
        if "$screen_center" in value:
            value = self.substituteScreenCenter(value)
        if "$" in value:
            value = VAR_RE.sub(lambda m: self.resolveVar(m.group(0), attr), value)
        if "eval" in value:
            value = self.evaluateFormulas(value)
        return value

    def evaluateFormulas(self, text):
        out = []
        i = 0
        n = len(text)
        while i < n:
            if text.startswith("eval", i) and (i + 4 >= n or not (text[i + 4].isalnum() or text[i + 4] == "_")):
                j = i + 4
                while j < n and text[j] != "(":
                    j += 1
                start = j
                level = 0
                while True:
                    if text[j] == "(":
                        level += 1
                    elif text[j] == ")":
                        level -= 1
                        if level == 0:
                            j += 1
                            break
                    j += 1
                formula = text[start:j]
                formula = re.sub(r'(?<!/)/(?!/)', '//', formula)
                try:
                    eval_result = eval(formula)  # pylint: disable=eval-used
                    if isinstance(eval_result, float):
                        if eval_result >= 0:
                            result = int(math.floor(eval_result + 0.5))
                        else:
                            result = int(math.ceil(eval_result - 0.5))
                    else:
                        result = int(eval_result)
                    out.append(str(result))
                except Exception as e:
                    # most likely a $var that is not defined (reported before): say so and go on with the
                    # formula as it is, like an undefined $var itself, instead of ending the whole run
                    print(f"ERROR: cannot evaluate {formula} in {self.current_file or '?'}: {type(e).__name__}")
                    out.append(formula)
                i = j
            else:
                out.append(text[i])
                i += 1
        return "".join(out)

    def findFontFile(self, filename):
        """The path of a declared font file in the places a font is kept, or None."""
        dirs = [d for d in os.environ.get("SKINFORGE_FONTS", "").split(os.pathsep) if d]
        for base in (self.srcdir, self.dstdir, self.cmndir):
            for d in (base, os.path.dirname(base)):
                dirs += [d, os.path.join(d, "fonts")]
        dirs += [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "enigma2", "data", "fonts"),
                 "/usr/share/fonts"]
        for d in dirs:
            path = os.path.join(d, filename)
            if os.path.isfile(path):
                return path
        return None

    def fontLineHeight(self, family):
        """The height a line of the font family needs per unit of font size: the scale of the family (fonts.xmlinc)
        times the line height of its font file per em. None if the family is not declared."""
        declared = self.font_defs.get(family)
        if not declared:
            return None
        filename, scale = declared
        per_em = None
        path = self.findFontFile(filename)
        if path:
            per_em = readFontLineHeight(path)
        if per_em is None:
            per_em = FONT_FILE_LINE_HEIGHT.get(os.path.basename(filename), FONT_LINE_HEIGHT_DEFAULT)
        return per_em * scale / 100.0

    def checkFonts(self, attrs):
        if "font" not in attrs or "size" not in attrs:
            return
        font_parts = attrs["font"].split(";")
        alias_name = None
        if len(font_parts) <= 1:
            # a font alias name (<alias name=... font=... size=.../>) instead of "Face;Size"
            alias = self.font_aliases.get(attrs["font"].strip())
            if not alias:
                return
            alias_name = attrs["font"].strip()
            font_parts = [alias[0], alias[1]]
        font_size = font_parts[1]
        try:
            size_height = attrs["size"].split(",")[1]
            if float(size_height) == 0:
                return  # 0 = sized at runtime by the screen's code (or a hidden placeholder)
            family = font_parts[0].strip()
            factor = self.fontLineHeight(family) or FONT_LINE_HEIGHT.get(family, FONT_LINE_HEIGHT_DEFAULT)
            required = math.ceil(float(font_size) * factor)
            if float(size_height) < required:
                widget = attrs.get("name") or attrs.get("source") or "?"
                fontvar = alias_name or self.last_font_var or "(literal)"
                screen_h = self.globals.get("$screen_height", "?")
                print(f"WARNING: screen={self.current_screen} screen_h={screen_h} widget={widget} font={fontvar} size: {size_height} < font: {required}")
        except Exception:
            pass

    def checkColor(self, key, value):
        if not key.endswith("Color") or key == "spacingColor":  # spacingColor is a pixel gap, not a color
            return
        if value.startswith("#") or value.startswith("$") or value == "":
            return
        if "$" + value not in self.colors and value not in self.colors and value not in KNOWN_BASE_COLORS:
            print(f"ERROR: color {value} not defined")

    def scanColors(self, node):
        """Pre-scans an about-to-be-included file's own tree for <color
        name=... value=.../> declarations, feeding self.colors - mirrors
        the old parseColors()'s independent scan, run before the file is
        otherwise processed, so a color it declares is already resolvable
        by anything (including itself) once real processing starts."""
        nodes = node if isinstance(node, list) else [node]
        for n in nodes:
            if isinstance(n, Comment):
                continue
            if n.tag == "color" and "name" in n.attrs and "value" in n.attrs:
                self.colors["$" + n.attrs["name"]] = n.attrs["value"]
            if n.tag == "alias" and "name" in n.attrs and "font" in n.attrs and "size" in n.attrs:
                self.font_aliases[n.attrs["name"]] = (n.attrs["font"], n.attrs["size"])
            if n.tag == "font" and "name" in n.attrs and "filename" in n.attrs:
                try:
                    scale = float(self.resolveValue(n.attrs.get("scale", "100")))
                except ValueError:
                    scale = 100.0
                self.font_defs[n.attrs["name"]] = (n.attrs["filename"], scale)
            if n.children:
                self.scanColors(n.children)

    def addDefaultRule(self, tag, conditions, values):
        """Adds the default values of one default widget (or screen, ...): tag is the element they are for, conditions a
        tuple of (attribute, op, value) that the element must meet (see parseConditions).
        A later declaration with the same tag and conditions (in another file, say) adds to the earlier one: the same
        attribute is overridden, the others stay."""
        rules = self.default_rules.setdefault(tag, {})
        rule = rules.setdefault(conditions, {"values": {}, "order": 0, "conditions": conditions})
        rule["values"].update(values)
        rule["order"] = self.default_order    # declared last = wins between rules that are equally specific
        self.default_order += 1

    def scanDefaults(self, node):
        """Pre-scans an about-to-be-included file's own tree for the declarations of defaults, feeding
        self.default_rules - same timing and rationale as scanColors() above (a default is in force from the file that
        declares it being reached by an include on).

        <defaults> holds the default widgets (and default screens, ...), each written like the element it is a default
        for, with the attributes it fills in when an element does not set them itself:
        <defaults><widget zPosition="1" transparent="1"/></defaults> for every widget. An if="..." on it says when it
        applies (see parseConditions): <widget if="render=Listbox and not itemCornerRadius" .../>. The conditions are
        about what the element itself sets, not about what other defaults fill in. All the other attributes are the
        values. Between the defaults that apply to one element the more specific one (more conditions) wins, between
        equally specific ones the one declared last.

        The old form, <default tag="widget[render=Label]" .../>, is read as the same thing."""
        nodes = node if isinstance(node, list) else [node]
        for n in nodes:
            if isinstance(n, Comment):
                continue
            if n.tag == "default" and "tag" in n.attrs:
                m = DEFAULT_TAG_SELECTOR_RE.match(n.attrs["tag"])
                if m is None:
                    print(f"ERROR: default tag={n.attrs['tag']}: not a tag, tag[render=value] or tag[attribute]")
                else:
                    conditions = ()
                    if m.group("render"):
                        conditions = (("render", "eq", m.group("render")),)
                    elif m.group("has"):
                        conditions = ((m.group("has"), "has", None),)
                    self.addDefaultRule(m.group("tag"), conditions, {k: v for k, v in n.attrs.items() if k != "tag"})
            elif n.tag == "defaults":
                for child in n.children or []:
                    if not isinstance(child, Element):
                        continue
                    conditions = ()
                    if "if" in child.attrs:
                        try:
                            conditions = tuple(sorted(parseConditions(child.attrs["if"]), key=str))
                        except ValueError as e:
                            print(f"ERROR: defaults: {child.tag} if=\"{child.attrs['if']}\": {e}")
                            continue
                    self.addDefaultRule(child.tag, conditions, {k: v for k, v in child.attrs.items() if k != "if"})
                continue
            if n.children:
                self.scanDefaults(n.children)

    @staticmethod
    def defaultLabel(tag, conditions):
        """widget, widget[render=Listbox], widget[itemCornerRadius]: the name of a default in the log."""
        return tag + "".join(f"[{conditionText(condition)}]" for condition in conditions)

    def noteSourceAttrs(self, raw_attrs, redundant):
        """For --redundant: counts the attributes that an element sets in its source (raw_attrs, with the _src that says
        where), and of these the ones that are redundant (the same as what the defaults would fill in)."""
        source = raw_attrs.get("_src")
        if source is None:
            return
        for key in raw_attrs:
            if key != "_src":
                counts = self.redundant_report.setdefault((source, key), [0, 0])
                counts[0] += 1
                counts[1] += key in redundant

    def conditionAttrs(self):
        """The attributes that any default has a condition on: they have to stay in the source, a default may depend on them."""
        return {condition[0] for rules in self.default_rules.values() for rule in rules.values() for condition in rule["conditions"]}

    def matchingDefaults(self, tag, attrs):
        """The defaults that apply to an element with these attributes, the one that wins first."""
        matching = [rule for rule in (self.default_rules.get(tag) or {}).values()
                    if all(conditionHolds(condition, attrs) for condition in rule["conditions"])]
        matching.sort(key=lambda rule: (len(rule["conditions"]), rule["order"]), reverse=True)
        return matching

    def defaultValue(self, tag, key, attrs):
        """What the defaults say for an attribute of an element with these attributes (as written), or None. For what the
        compiler itself reads from the source of an element (the resolution of a screen), which then is the same as
        when the screen says it."""
        for rule in self.matchingDefaults(tag, attrs):
            if key in rule["values"]:
                return rule["values"][key]
        return None

    def applyDefaults(self, tag, attrs, raw_attrs=None):
        """Fills in the attributes that the element does not set itself (attrs, resolved) from the defaults that apply to
        it. What applies is decided from the attributes the element has in its source, before anything is filled in.
        raw_attrs: the attributes as written, only for --redundant."""
        matching = self.matchingDefaults(tag, attrs)
        if self.redundant_report is not None and raw_attrs is not None:
            # An attribute is redundant if what is written is exactly what the default that wins for it says (the text, not the
            # value it comes to: the text means the same in every skin the part is compiled into), and no default depends on it.
            winner = {}
            for rule in matching:
                for key, value in rule["values"].items():
                    winner.setdefault(key, value)
            conditions = self.conditionAttrs()
            self.noteSourceAttrs(raw_attrs, {k for k, v in winner.items() if raw_attrs.get(k) == v and k not in conditions})
        for rule in matching:
            label = self.defaultLabel(tag, rule["conditions"])
            for key, value in rule["values"].items():
                if key in attrs:
                    continue
                value = self.resolveValue(value, key)
                self.checkColor(key, value)
                attrs[key] = value
                self.default_filled[(tag, label, key)] = self.default_filled.get((tag, label, key), 0) + 1

    def printDefaultsReport(self):
        """What the defaults filled in, for the log: one line per default and attribute."""
        if not self.default_filled:
            return
        print("==> defaults: what the default widgets filled in (rule, attribute: elements)")
        for (_tag, label, key), count in sorted(self.default_filled.items()):
            print(f"==> defaults: {label} {key}: {count}")

    def offsetPositions(self, node, pos):
        """Applies a position=(x,y) offset to every element anywhere in
        this subtree that has its own position= attribute - not just
        direct children - since an <xmlinc position=.../> shifts
        everything nested under it, arbitrarily deep through further
        nested includes."""
        nodes = node if isinstance(node, list) else [node]
        for n in nodes:
            if not isinstance(n, Element):
                continue
            if "position" in n.attrs and n.attrs["position"] not in LAYOUT_POSITIONS:
                n.attrs["position"] = str(pos + Pos(n.attrs["position"]))
            if n.children:
                self.offsetPositions(n.children, pos)

    def computeBoundingBox(self, node):
        """Returns (max_x, max_y) - the furthest right/bottom edge among
        every element anywhere in this subtree that has both a plain
        numeric position= and size= (arbitrarily deep, same reach as
        offsetPositions) - or None if nothing measurable was found. A
        "fill"/"center,center" position or a non-numeric size (e.g. still
        containing an unresolved eval()/$var, which shouldn't happen by
        the time this runs but is defensive) is skipped rather than
        guessed at, so a stray unmeasurable widget just doesn't
        contribute instead of poisoning the whole result with a bogus
        number."""
        nodes = node if isinstance(node, list) else [node]
        max_x = max_y = None
        for n in nodes:
            if not isinstance(n, Element):
                continue
            pos_attr = n.attrs.get("position")
            size_attr = n.attrs.get("size")
            if pos_attr and size_attr and pos_attr != "fill":
                try:
                    x, y = (int(v) for v in pos_attr.split(","))
                    w, h = (int(v) for v in size_attr.split(","))
                    max_x = x + w if max_x is None else max(max_x, x + w)
                    max_y = y + h if max_y is None else max(max_y, y + h)
                except ValueError:
                    pass
            if n.children:
                child_box = self.computeBoundingBox(n.children)
                if child_box:
                    cx, cy = child_box
                    max_x = cx if max_x is None else max(max_x, cx)
                    max_y = cy if max_y is None else max(max_y, cy)
        return None if max_x is None else (max_x, max_y)

    @staticmethod
    def scanParams(doc):
        """The parameters that an included file declares for itself, as {name: default or None}, or None if it declares
        none: <param name="x" default="y"/> (no default: the includer has to give it), as siblings at the top of the file
        or as direct children of its one root element."""
        nodes = doc if isinstance(doc, list) else [doc]
        if len(nodes) == 1 and isinstance(nodes[0], Element) and nodes[0].children:
            nodes = nodes + [c for c in nodes[0].children if isinstance(c, Element)]
        params = {n.attrs["name"]: n.attrs.get("default") for n in nodes
                  if isinstance(n, Element) and n.tag == "param" and "name" in n.attrs}
        return params or None

    @staticmethod
    def checkIncludeParams(inc_filename, attrs, params):
        """Reports what an <xmlinc> gives to a file that declares its parameters wrongly: a parameter that is missing
        and has no default, and an attribute that is no parameter (a typo, most likely)."""
        for name, default in params.items():
            if default is None and name not in attrs:
                print(f"ERROR: include {inc_filename}: parameter {name} is required")
        for key in attrs:
            if key not in params and key not in {"file", "position", "size"}:
                print(f"WARNING: include {inc_filename}: {key} is not a parameter of it (parameters: {', '.join(params)})")

    def exposeIncludeAttrs(self, attrs, params=None):
        """Registers the attributes of an <xmlinc> other than file and position as $vars (size also as
        $width/$height) and checks the fonts. params: the parameters that the included file declares (scanParams):
        one that the <xmlinc> does not give gets its default - a required one that is not given is not set at all, not
        even to a global of the same name, so the error that it is missing is the one the file gets."""
        resolved_attrs = {}
        for key, value in attrs.items():
            if key in {"file", "position"}:
                continue
            value = self.resolveValue(value, key)
            resolved_attrs[key] = value
            if key == "size":
                w, h = value.split(",")
                self.globals["$width"] = w
                self.globals["$height"] = h
            self.globals["$" + key] = value
        for name, default in (params or {}).items():
            if name not in attrs:
                if default is None:
                    self.globals.pop("$" + name, None)
                else:
                    self.globals["$" + name] = self.resolveValue(default, name)
        self.checkFonts(resolved_attrs)

    def restoreSize(self, saved_size):
        for var, val in zip(("$width", "$height"), saved_size):
            if val is None:
                self.globals.pop(var, None)
            else:
                self.globals[var] = val

    def processInclude(self, level, node, pos):
        """Resolves one <xmlinc> element: locates the file, registers its
        other attributes as $vars, recurses into it (or splices it in
        completely unprocessed for an applet_* file), and returns the
        replacement node(s) to substitute in its place."""
        attrs = node.attrs
        inc_filename = self.resolveValue(attrs["file"])

        if self.skipsScreen(inc_filename):
            # Decided before the file is looked up, as that would convert its .ymlinc. What the full compile would
            # have left behind for the includes after this one is kept: the attributes of the <xmlinc> are globals
            # from here on (e.g. imagepath) - the size only for this include.
            print(f"==> skipping screen: {inc_filename}")
            saved_size = (self.globals.get("$width"), self.globals.get("$height"))
            self.exposeIncludeAttrs(attrs)
            if "size" in attrs:
                self.restoreSize(saved_size)
            self.frame_origin = None
            return None

        inc_file = self.getIncFilePath(inc_filename)

        if os.path.basename(inc_file).startswith("applet_"):
            print(f"==> processApplet: >{level}, {inc_file}")
            return RawText(readFile(inc_file))

        try:
            inc_doc = XmlParser(readFile(inc_file)).parseDocument()
        except XmlParseError as e:
            raise XmlParseError(f"{inc_file}: {e}") from None
        self.scanColors(inc_doc)
        self.scanDefaults(inc_doc)

        # size= exposes $width/$height only for the duration of this include:
        # they are put back afterwards, so the enclosing file's own
        # $width/$height (and anything resolved against them, like this
        # include's position=) are not clobbered by a nested include's size.
        saved_size = (self.globals.get("$width"), self.globals.get("$height"))
        # The parameters that the file declares (<param>) are its own: they are put back after the include, so one that
        # an include gives does not stay behind for the includes that follow. The attributes of an <xmlinc> to a file
        # that declares none are globals from here on, as they always were (a screen can pass dialog_width to its
        # frame and use it for the rest of the screen).
        params = self.scanParams(inc_doc)
        saved_params = {}
        if params:
            self.checkIncludeParams(inc_filename, attrs, params)
            saved_params = {"$" + name: self.globals.get("$" + name) for name in params}
        self.exposeIncludeAttrs(attrs, params)

        # A file whose root <screen> carries frame="..." is a frame (a window drawn on the screen). The
        # content of a screen that places a frame is positioned relative to the frame's origin, see
        # processElement().
        is_frame = isinstance(inc_doc, Element) and inc_doc.tag == "screen" and "frame" in inc_doc.attrs
        self.frame_origin = None
        is_screen_file = os.path.basename(inc_file).startswith("screen_")
        is_target = self.inSkin() and os.path.splitext(os.path.basename(inc_filename))[0] in self.screen_filter
        self.screen_include_depth += is_screen_file
        try:
            result = self.processFile(level + 1, inc_file)
        finally:
            self.screen_include_depth -= is_screen_file
        inner_origin = self.frame_origin
        if is_target:
            self.target_screens.update(id(r) for r in (result if isinstance(result, list) else [result])
                                       if isinstance(r, Element) and r.tag == "screen")

        if "size" in attrs:
            self.restoreSize(saved_size)
        for var, value in saved_params.items():
            if value is None:
                self.globals.pop(var, None)
            else:
                self.globals[var] = value

        # Auto-expose the included content's own intrinsic bounding box as
        # $child_width/$child_height, so a parent can size/position things
        # around an include without hand-maintaining the number (see
        # the wiki's "The XML+ compiler" page). Measured against the
        # file's own local 0,0-based coordinates - this is meant to be a
        # stable property of the fragment itself ("how big is this
        # content"), not wherever a particular include happens to place
        # it. Fixed name, not per-file: simpler to use, but it means each
        # xmlinc overwrites the previous one's value - only ever reliable
        # for the include that was just processed, not an earlier sibling.
        box = self.computeBoundingBox(result) if result is not None else None
        if box:
            self.globals["$child_width"] = str(box[0])
            self.globals["$child_height"] = str(box[1])

        # position= is resolved only now - after the file above has been
        # fully processed and its own $child_width/$child_height set - so
        # an <xmlinc> can reference its own just-measured size to
        # center/place itself, e.g.
        # position="eval(($screen_width-$child_width)/2),0". Resolving it
        # any earlier (as this used to, before the file was even read)
        # would only ever see whatever the *previous* include left
        # behind, never this one's own size.
        pos2 = pos
        if "position" in attrs:
            pos2 = pos + Pos(self.centerPosition(self.resolveValue(attrs["position"], "position"), box))

        # the origin of the frame this include placed, for the screen around it
        if is_frame:
            self.frame_origin = pos2
        elif inner_origin is not None:
            self.frame_origin = inner_origin + pos2
        else:
            self.frame_origin = None

        if isinstance(result, list):
            for r in result:
                if isinstance(r, Element):
                    self.offsetPositions(r, pos2)
        elif isinstance(result, Element):
            self.offsetPositions(result, pos2)
        return result

    def centerPosition(self, value, box):
        """Replaces a "center" component of an include's position by the offset that centres the include's
        bounding box (box = (width, height), see computeBoundingBox) on the screen."""
        if "center" not in value:
            return value
        if value.strip() == "center":
            value = "center,center"
        segments = value.split(",")
        if len(segments) != 2 or not box or "$screen_width" not in self.globals or "$screen_height" not in self.globals:
            print(f"ERROR: cannot center an include with position {value!r}, box {box}")
            return value
        screen = (int(self.globals["$screen_width"]), int(self.globals["$screen_height"]))
        return ",".join(str((screen[i] - box[i]) // 2) if seg.strip() == "center" else seg for i, seg in enumerate(segments))

    @staticmethod
    def unwrapScreens(result):
        """Replace every <screen> element in an include's result (one element
        or a list) by that screen's own children."""
        items = result if isinstance(result, list) else [result]
        out = []
        for item in items:
            if isinstance(item, Element) and item.tag == "screen":
                out.extend(item.children or [])
            else:
                out.append(item)
        return out

    def processElement(self, level, node):
        """Returns the processed replacement for one element - a single
        Element/Comment, a list of them (an <xmlinc> can expand to more
        than one top-level sibling), or None (an element that's build-time
        only and never appears in the compiled output, e.g. <global>)."""
        if isinstance(node, Comment):
            return node

        self.last_font_var = None

        if node.tag == "xmlinc":
            # An include's own position= is always relative to its own
            # file's local coordinate system (Pos(0,0)) - accumulation
            # across nesting levels happens entirely through the bubble-up
            # offsetPositions() call below, once per enclosing level, not
            # by threading a running total down into each recursive call.
            return self.processInclude(level, node, Pos(0, 0))

        if node.tag == "screenfamily" and self.inSkin():
            return None   # compiling only the screens of some screen_* files: this is none of them

        if node.tag == "screenfamily":
            # screens that only differ in name (and title): <screenfamily body="part.xmlinc" ...screen attributes>
            # with one <screen name=... title=.../> per member. Each becomes a complete screen that includes the
            # body (at bodyPosition, default 0,0).
            attrs = dict(node.attrs)
            body = attrs.pop("body")
            body_position = attrs.pop("bodyPosition", "0,0")
            screens = []
            for member in node.children or []:
                if not isinstance(member, Element):
                    continue
                merged = dict(attrs)
                merged.update(member.attrs)
                # a member can hand a value to the body: content="..." is not a screen attribute but the $content of the
                # body (the name, without extension, of the part a frame body like screenpart_ScreenLarge includes)
                include_attrs = {"file": body, "position": body_position}
                if "content" in merged:
                    include_attrs["content"] = merged.pop("content")
                body_include = Element("xmlinc", include_attrs, None, None)
                screens.append(self.processElement(level, Element("screen", dict(sorted(merged.items())), [body_include], None)))
            return screens

        if node.tag == "global":
            self.globals["$" + node.attrs["name"]] = self.resolveValue(node.attrs["value"])
            return None

        if node.tag == "param":
            # The declaration of a parameter of the file, read by processInclude() before the file is processed.
            return None

        if node.tag == "local":
            # Like <global>, but only for the rest of this file: processFile() drops the file's variables when it is
            # done, and an include of another file never sees them (hand a value on as an attribute of the <xmlinc>).
            # Shadows a global of the same name.
            self.localVars()["$" + node.attrs["name"]] = self.resolveValue(node.attrs["value"])
            return None

        if isDefaultElement(node.tag):
            # Already picked up by scanDefaults() before this file's
            # elements were processed - a <default> (or <defaults>) is
            # build-time only, like <global>, and never appears in the
            # compiled output.
            return None

        if node.tag == "screen":
            # resolution= is the "designed for" fallback enigma2 itself
            # uses for scaling (see skin.py's parseScreen) when a screen
            # has no explicit size= - same fallback xml2svg.py's
            # size_attr lookup already applies. This only feeds
            # $screen_width/$screen_height below; it never writes a size=
            # onto the compiled <screen> element itself; a screen whose
            # position= isn't "fill" still needs a real size= in its own
            # source (or position="fill" instead) - enigma2's own
            # collectAttributes()/context.parse() (skin.py) requires an
            # actual size= there, and papering over a missing one here
            # would just hide that source error instead of surfacing it.
            size_attr = node.attrs.get("size") or node.attrs.get("resolution") or self.defaultValue("screen", "resolution", node.attrs)
            if size_attr:
                w, h = self.resolveValue(size_attr, "size").split(",")
                self.globals["$screen_width"] = w
                self.globals["$screen_height"] = h
            elif self.output_size:
                # a screen without a size (position="fill") covers the whole output: without this it
                # would keep the size of whichever earlier screen happened to declare one
                self.globals["$screen_width"], self.globals["$screen_height"] = self.output_size
            self.current_screen = node.attrs.get("name", "?")

        if node.tag == "layout":
            if node.children is None and node.text is None:
                # self-closing <layout name="x"/> - a reference, not a
                # declaration: just validate and pass through unchanged,
                # same as the original (this doesn't actually splice
                # anything - see the module's design notes).
                name = node.attrs.get("name")
                if name not in self.layouts:
                    print(f"==> processFile: >{level}, ERROR: layout {name} not defined")
                else:
                    print(f"==> processFile: >{level}, >>> including layout {name}")
            else:
                self.layouts.append(node.attrs.get("name"))

        if node.tag == "resolution" and "xres" in node.attrs and "yres" in node.attrs:
            self.output_size = (self.resolveValue(node.attrs["xres"]), self.resolveValue(node.attrs["yres"]))
            # the size of the output is known to the skin from here on: a window that fills it (Window_Full) is
            # sized from these, as no screen is being processed yet when the globals of the frames are defined
            self.globals["$output_width"], self.globals["$output_height"] = self.output_size

        new_attrs = {}
        for key, value in node.attrs.items():
            if node.tag == "screen" and key == "position" and self.output_size:
                # a screen is placed on the output, so e/c-style formulas in its own position
                # refer to the output size, not to the size of the screen being placed
                value = self.resolveInOutputSize(value)
            else:
                value = self.resolveValue(value, key)
            self.checkColor(key, value)
            new_attrs[key] = value
        if "Summary" not in self.current_file:
            self.applyDefaults(node.tag, new_attrs, node.attrs)
            self.checkFonts(new_attrs)
        elif self.redundant_report is not None:
            self.noteSourceAttrs(node.attrs, ())    # no defaults in a Summary: nothing in it is redundant

        new_children = None
        placed_origin = None
        if node.children:
            new_children = []
            own_children = []  # (start, end) in new_children of what is not a frame, or a screen placing one
            for child in node.children:
                self.frame_origin = None
                result = self.processElement(level, child)
                child_origin = self.frame_origin
                if result is None:
                    continue
                if isinstance(child, Element) and child.tag == "xmlinc" and node.tag != "skin":
                    # An include inside another element is a panel-style
                    # reference (<panel name="X"/> in the original skin): when
                    # the included file is a whole <screen name="X"> definition,
                    # its content - not the <screen> wrapper - belongs here.
                    result = self.unwrapScreens(result)
                start = len(new_children)
                if isinstance(result, list):
                    new_children.extend(result)
                else:
                    new_children.append(result)
                if child_origin is None:
                    own_children.append((start, len(new_children)))
                elif placed_origin is None:
                    placed_origin = child_origin
            if placed_origin is not None and node.tag == "screen":
                # the screen places a frame: everything else in it is relative to the frame's origin
                for start, end in own_children:
                    for item in new_children[start:end]:
                        if isinstance(item, Element) and not item.screen_origin:
                            self.offsetPositions(item, placed_origin)
        self.frame_origin = placed_origin if node.tag == "screen" else None

        if node.tag == "applet":
            # python code: left exactly as written except for $variables the skin defines
            new_text = self.substituteKnownVars(node.text)
        else:
            new_text = self.resolveValue(self.normalizeText(node.text)) if node.text is not None else None
        element = Element(node.tag, new_attrs, new_children, new_text)
        # origin="screen" is for the compiler only: the element is not relative to the frame of its screen
        if new_attrs.get("origin") == "screen":
            del new_attrs["origin"]
            element.screen_origin = True
        return element

    def processFile(self, level, afile):
        print(f"==> processFile: >{level}, {afile}")
        # Restored on the way back out - checkFonts()'s "Summary" exemption
        # needs to see *this* file's own path while processing its
        # elements, then the enclosing file's path again once a nested
        # include here has been fully processed and control returns to it.
        prev_file = self.current_file
        self.current_file = afile
        self.locals.append({})
        try:
            try:
                doc = XmlParser(readFile(afile)).parseDocument()
            except XmlParseError as e:
                raise XmlParseError(f"{afile}: {e}") from None
            nodes = doc if isinstance(doc, list) else [doc]
            results = []
            for n in nodes:
                result = self.processElement(level, n)
                if result is None:
                    continue
                if isinstance(result, list):
                    results.extend(result)
                else:
                    results.append(result)
            return results if len(results) != 1 else results[0]
        finally:
            self.locals.pop()
            self.current_file = prev_file


def parseArgs(argv):
    parser = argparse.ArgumentParser(prog="xmlinc.py")
    parser.add_argument("-i", dest="srcinfile", required=True, help="source file")
    parser.add_argument("-o", dest="srcoutfile", required=True, help="destination file")
    parser.add_argument("-d", dest="dstdir", required=True, help="destination dir")
    parser.add_argument("-c", dest="cmndir", required=True, help="common dir")
    parser.add_argument("--marks", dest="marks", action="store_true",
                        help="the .ymlinc that are converted on the way give every element an _src=\"path:line\" (for --redundant)")
    parser.add_argument("--redundant", dest="redundant", default=None, metavar="FILE",
                        help="write to FILE which attributes of the sources are the same as the defaults (needs --marks): "
                             "one line per source element and attribute: path:line, attribute, how often compiled, how often redundant")
    parser.add_argument("-s", dest="screens", action="append", default=None, metavar="SCREEN",
                        help="compile only this screen_* file (name with or without extension, repeatable); the "
                             "other screen_* includes of the source file are skipped - see xmlcompilescreen")
    return parser.parse_args(argv)


def xmlinc(argv):
    args = parseArgs(argv)
    srcinfile = os.path.normpath(args.srcinfile)
    srcoutfile = os.path.normpath(args.srcoutfile)
    dstdir = os.path.normpath(args.dstdir)
    cmndir = os.path.normpath(args.cmndir)

    print("processing xml...")
    print("src in file: " + srcinfile)
    srcdir = os.path.dirname(srcinfile)
    srcfn = os.path.basename(srcinfile)
    print("src in file name: " + srcfn)
    print("src out file: " + srcoutfile)
    print("src dir: " + srcdir)
    print("dst dir: " + dstdir)
    print("cmn dir: " + cmndir)

    global markSources, markFullPath    # pylint: disable=global-statement
    if args.redundant or args.marks:
        markSources = markFullPath = True
    inc = XMLInclude(srcdir, dstdir, cmndir)
    if args.redundant:
        inc.redundant_report = {}
    if args.screens:
        inc.screen_filter = {os.path.splitext(os.path.basename(s))[0] for s in args.screens}
    result = inc.processFile(0, srcinfile)

    lines = []
    nodes = result if isinstance(result, list) else [result]
    if inc.screen_filter:
        # only the screens of the files asked for: the other files of the skin that define screens (not named
        # screen_*) were compiled too, as their content is not known before
        for node in nodes:
            if isinstance(node, Element) and node.children:
                node.children = [c for c in node.children
                                 if not (isinstance(c, Element) and c.tag == "screen" and id(c) not in inc.target_screens)]
    for node in nodes:
        renderNode(node, lines)
    output = "\n".join(lines) + "\n"
    writeFile(srcoutfile, output)
    inc.printDefaultsReport()
    if args.redundant:
        lines = [f"{source}\t{key}\t{counts[0]}\t{counts[1]}" for (source, key), counts in sorted(inc.redundant_report.items())]
        writeFile(args.redundant, "\n".join(lines) + "\n")

    # print("xmlinc done.")


if __name__ == "__main__":
    xmlinc(sys.argv[1:])
