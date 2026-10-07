# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


import argparse
import atexit
import math
import os
import re
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
    writeFile(out, yml2xml(readFile(yml_path), False))
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
# declared in screenpart_colors.xmlinc. Kept intentionally small - only
# names actually verified to exist there, so a real typo or a color that
# was never defined anywhere still gets flagged.
KNOWN_BASE_COLORS = {
    "black", "white", "red", "green", "blue", "yellow",
    "grey", "gray", "darkgrey", "darkgray", "orange",
    "transparent", "foreground", "background",
}

VAR_RE = re.compile(r"\$\w+")
DEFAULT_TAG_SELECTOR_RE = re.compile(r"^(?P<tag>\w+)(?:\[render=(?P<render>[^\]]+)\])?$")


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
    __slots__ = ("tag", "attrs", "children", "text")

    def __init__(self, tag, attrs, children, text):
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

        children, text = self.parseContent(attrs.get("nostrip") in ("1", "true", "True"))
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
        self.layouts = []
        self.colors = {}
        self.defaults = {}
        self.current_screen = "?"
        self.current_file = ""
        self.last_font_var = None

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

    def resolveVar(self, var):
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
            print(f"ERROR: global {var} not found.")
            return var

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

    def resolveValue(self, value):
        """$var substitution, then eval(...) evaluation, in that order (a
        formula can reference a global by name) - applied to one attribute
        value or text-content string at a time, each fully self-contained
        (an XML-parsed attribute value's boundaries are never ambiguous the
        way they were for the old token-position approach)."""
        if "$" in value:
            value = VAR_RE.sub(lambda m: self.resolveVar(m.group(0)), value)
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
                eval_result = eval(formula)  # pylint: disable=eval-used
                if isinstance(eval_result, float):
                    if eval_result >= 0:
                        result = int(math.floor(eval_result + 0.5))
                    else:
                        result = int(math.ceil(eval_result - 0.5))
                else:
                    result = int(eval_result)
                out.append(str(result))
                i = j
            else:
                out.append(text[i])
                i += 1
        return "".join(out)

    def checkFonts(self, attrs):
        if "font" not in attrs or "size" not in attrs:
            return
        font_parts = attrs["font"].split(";")
        if len(font_parts) <= 1:
            return
        font_size = font_parts[1]
        try:
            size_height = attrs["size"].split(",")[1]
            if float(size_height) == 0:
                return  # 0 = sized at runtime by the screen's code (or a hidden placeholder)
            factor = FONT_LINE_HEIGHT.get(font_parts[0].strip(), FONT_LINE_HEIGHT_DEFAULT)
            required = math.ceil(float(font_size) * factor)
            if float(size_height) < required:
                widget = attrs.get("name") or attrs.get("source") or "?"
                fontvar = self.last_font_var or "(literal)"
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
            if n.children:
                self.scanColors(n.children)

    def scanDefaults(self, node):
        """Pre-scans an about-to-be-included file's own tree for <default
        tag=... .../> declarations, feeding self.defaults - same timing
        and rationale as scanColors() above. Every attribute on a
        <default> other than "tag" itself is a default value for that
        tag; a later declaration for the same tag+attr overrides an
        earlier one (last-scanned wins), the same as any other $var/color
        redeclaration in this toolset.

        "tag" may optionally narrow the match to one render variant of
        that tag, e.g. tag="widget[render=Label]" - self.defaults is then
        keyed self.defaults[base_tag][render_value], with the unqualified
        tag="widget" form stored under the "*" key (matches any render,
        and any element with no render attribute at all). A malformed
        selector (anything not matching DEFAULT_TAG_SELECTOR_RE) is
        stored verbatim as its own base tag under "*", same as before -
        it just won't match any real element, rather than crashing."""
        nodes = node if isinstance(node, list) else [node]
        for n in nodes:
            if isinstance(n, Comment):
                continue
            if n.tag == "default" and "tag" in n.attrs:
                m = DEFAULT_TAG_SELECTOR_RE.match(n.attrs["tag"])
                base_tag = m.group("tag") if m else n.attrs["tag"]
                render_value = m.group("render") if m else None
                target = self.defaults.setdefault(base_tag, {}).setdefault(render_value or "*", {})
                target.update({k: v for k, v in n.attrs.items() if k != "tag"})
            if n.children:
                self.scanDefaults(n.children)

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

    def processInclude(self, level, node, pos):
        """Resolves one <xmlinc> element: locates the file, registers its
        other attributes as $vars, recurses into it (or splices it in
        completely unprocessed for an applet_* file), and returns the
        replacement node(s) to substitute in its place."""
        attrs = node.attrs
        inc_filename = self.resolveValue(attrs["file"])
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

        resolved_attrs = {}
        for key, value in attrs.items():
            if key in {"file", "position"}:
                continue
            value = self.resolveValue(value)
            resolved_attrs[key] = value
            if key == "size":
                w, h = value.split(",")
                self.globals["$width"] = w
                self.globals["$height"] = h
            self.globals["$" + key] = value
        self.checkFonts(resolved_attrs)

        result = self.processFile(level + 1, inc_file)

        if "size" in attrs:
            for var, val in zip(("$width", "$height"), saved_size):
                if val is None:
                    self.globals.pop(var, None)
                else:
                    self.globals[var] = val

        # Auto-expose the included content's own intrinsic bounding box as
        # $child_width/$child_height, so a parent can size/position things
        # around an include without hand-maintaining the number (see
        # README's "Relative positioning" section). Measured against the
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
            pos2 = pos + Pos(self.resolveValue(attrs["position"]))

        if isinstance(result, list):
            for r in result:
                if isinstance(r, Element):
                    self.offsetPositions(r, pos2)
        elif isinstance(result, Element):
            self.offsetPositions(result, pos2)
        return result

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

        if node.tag == "global":
            self.globals["$" + node.attrs["name"]] = self.resolveValue(node.attrs["value"])
            return None

        if node.tag == "default":
            # Already picked up by scanDefaults() before this file's
            # elements were processed - a <default> is build-time only,
            # like <global>, and never appears in the compiled output.
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
            size_attr = node.attrs.get("size") or node.attrs.get("resolution")
            if size_attr:
                w, h = self.resolveValue(size_attr).split(",")
                self.globals["$screen_width"] = w
                self.globals["$screen_height"] = h
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

        new_attrs = {}
        for key, value in node.attrs.items():
            value = self.resolveValue(value)
            self.checkColor(key, value)
            new_attrs[key] = value
        if "Summary" not in self.current_file:
            tag_defaults = self.defaults.get(node.tag, {})
            render_value = new_attrs.get("render")
            default_dicts = []
            if render_value is not None and render_value in tag_defaults:
                default_dicts.append(tag_defaults[render_value])
            if "*" in tag_defaults:
                default_dicts.append(tag_defaults["*"])
            for defaults in default_dicts:
                for key, value in defaults.items():
                    if key in new_attrs:
                        continue
                    value = self.resolveValue(value)
                    self.checkColor(key, value)
                    new_attrs[key] = value
            self.checkFonts(new_attrs)

        new_children = None
        if node.children:
            new_children = []
            for child in node.children:
                result = self.processElement(level, child)
                if result is None:
                    continue
                if child.tag == "xmlinc" and node.tag != "skin":
                    # An include inside another element is a panel-style
                    # reference (<panel name="X"/> in the original skin): when
                    # the included file is a whole <screen name="X"> definition,
                    # its content - not the <screen> wrapper - belongs here.
                    result = self.unwrapScreens(result)
                if isinstance(result, list):
                    new_children.extend(result)
                else:
                    new_children.append(result)

        if node.tag == "applet":
            new_text = node.text   # python code: leave it exactly as written
        else:
            new_text = self.resolveValue(self.normalizeText(node.text)) if node.text is not None else None
        return Element(node.tag, new_attrs, new_children, new_text)

    def processFile(self, level, afile):
        print(f"==> processFile: >{level}, {afile}")
        # Restored on the way back out - checkFonts()'s "Summary" exemption
        # needs to see *this* file's own path while processing its
        # elements, then the enclosing file's path again once a nested
        # include here has been fully processed and control returns to it.
        prev_file = self.current_file
        self.current_file = afile
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
            self.current_file = prev_file


def parseArgs(argv):
    parser = argparse.ArgumentParser(prog="xmlinc.py")
    parser.add_argument("-i", dest="srcinfile", required=True, help="source file")
    parser.add_argument("-o", dest="srcoutfile", required=True, help="destination file")
    parser.add_argument("-d", dest="dstdir", required=True, help="destination dir")
    parser.add_argument("-c", dest="cmndir", required=True, help="common dir")
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

    inc = XMLInclude(srcdir, dstdir, cmndir)
    result = inc.processFile(0, srcinfile)

    lines = []
    nodes = result if isinstance(result, list) else [result]
    for node in nodes:
        renderNode(node, lines)
    output = "\n".join(lines) + "\n"
    writeFile(srcoutfile, output)

    # print("xmlinc done.")


if __name__ == "__main__":
    xmlinc(sys.argv[1:])
