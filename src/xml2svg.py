# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0

# Renders the first-level elements of every <screen> in a *compiled*
# skin.xml (xmlinc already resolved - no more <xmlinc>/$var/eval() left) as
# labelled boxes in one SVG, so a screen's layout can be eyeballed without
# installing the plugin and opening that screen on a box. Only direct
# children of <screen> are drawn (eLabel/widget/ePixmap/... - not whatever a
# <widget>'s own <convert> template lays out internally), since that's the
# level a skin author actually positions by hand and the level most layout
# bugs (overlap, off-screen, wrong size) live at.
#
# position=/size= on a compiled skin.xml can still contain tokens only the
# real C++ skin engine resolves at runtime - "e" (edge), "center", "%",
# "w"/"h" (font metrics) - since xmlinc.py's own $var/eval() resolution
# (see xmlinc.py's resolveValue()) is a source-level, build-time step and
# deliberately leaves these alone. parseCoordinate() below is a direct port
# of enigma2's skin.py:parseCoordinate() (same eval()-with-bound-locals
# trick) so those tokens resolve to the same pixel values a real box would
# use - minus font metrics, which aren't available offline and degrade to 0
# exactly like skin.py's own "if font in fonts else 0" fallback.

import os
import sys
import json
import argparse
import tempfile
from FileUtils import readFile, writeFile
from xmlinc import XmlParser, Element, Comment, XmlParseError, XMLInclude

# A non-visual tag at screen level: metadata, not something with an on-screen
# box to draw.
SKIPPED_TAGS = {"colors", "scrollbarstyle"}

# Cycled by element index when an element has no backgroundColor of its own
# to show instead - just needs to be readable/distinct, not meaningful.
PALETTE = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]

HEADER_HEIGHT = 28
SCREEN_GAP = 40
PAGE_MARGIN = 20


def parseCoordinate(s, e, size=0):
    """Port of skin.py:parseCoordinate(), scale=(1,1) (a compiled skin.xml
    is already at the target device resolution) and no live font metrics
    (w/h degrade to 0, same as skin.py's own "not in fonts" fallback)."""
    s = s.strip()
    if s.lstrip("-").isdigit():
        return int(s)
    if s == "center":
        return 0 if not size else (e - size) // 2
    if s == "e":
        return e
    if s == "*":
        return 0
    center = (e - size) / 2  # noqa: F841  (bound for eval() below, like skin.py's own)
    c = e / 2  # noqa: F841
    w = 0  # noqa: F841  no offline font metrics - see module docstring
    h = 0  # noqa: F841
    if "w" in s:
        s = s.replace("w", "*w")
    if "h" in s:
        s = s.replace("h", "*h")
    if "%" in s:
        s = s.replace("%", "*e / 100")
    try:
        val = eval(s)  # pylint: disable=eval-used
    except Exception as err:
        print(f"WARNING: coordinate '{s}' could not be evaluated ({err}), using 0")
        val = 0
    return int(val)


def parseValuePair(s, parent_w, parent_h, size_w=0, size_h=0):
    x, y = (part.strip() for part in s.split(","))
    return parseCoordinate(x, parent_w, size_w), parseCoordinate(y, parent_h, size_h)


def resolveBox(attrs, screen_w, screen_h):
    """Returns (x, y, w, h) in pixels for one element, given the screen (=
    parent, since only first-level children are handled) size. size= is
    resolved before position= - "center" needs the already-resolved size,
    same ordering Screen.py enforces for the real thing (it sorts skin
    attributes so position= is always applied last)."""
    size_attr = attrs.get("size")
    if size_attr and size_attr != "fill":
        w, h = parseValuePair(size_attr, screen_w, screen_h)
    else:
        w, h = screen_w, screen_h

    pos_attr = attrs.get("position", "0,0")
    if pos_attr == "fill":
        x, y = 0, 0
        w, h = screen_w, screen_h
    else:
        x, y = parseValuePair(pos_attr, screen_w, screen_h, w, h)
    return x, y, max(0, w), max(0, h)


def buildColorMap(colorsNode, base=None):
    cmap = dict(base) if base else {}
    if colorsNode and colorsNode.children:
        for child in colorsNode.children:
            if isinstance(child, Element) and child.tag == "color" and "name" in child.attrs and "value" in child.attrs:
                cmap[child.attrs["name"]] = child.attrs["value"]
    return cmap


def resolveColor(value, colormap):
    if not value:
        return None
    if not value.startswith("#"):
        value = colormap.get(value, value)  # unknown name: pass through - SVG understands CSS keywords like "black"/"orange" too
    if value.startswith("#") and len(value) == 9:
        # Enigma2's 8-digit colors are #AARRGGBB (alpha byte first, and
        # inverted: 0x00=opaque..0xFF=transparent) - SVG's 8-digit form is
        # #RRGGBBAA, so passing this straight through would render the
        # wrong color entirely. Drop the alpha byte and keep just the RGB;
        # box visibility is handled uniformly by fill-opacity below
        # regardless of a widget's real runtime alpha, since the point
        # here is staying visible as a box, not replicating actual
        # on-screen transparency.
        value = "#" + value[3:]
    return value


def describeElement(node):
    a = node.attrs
    label = a.get("name") or a.get("source") or node.tag
    if "source" in a and "render" in a and label == a["source"]:
        label = f"{label} ({a['render']})"
    return label


def escapeXml(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def collectScreens(root):
    """Yields (screenElement, screen_w, screen_h) for every <screen> found
    directly under the document root - a real skin.xml has exactly one
    <skin> root with one or more <screen> children; nothing deeper is a
    screen in this dialect."""
    children = root.children or []
    for node in children:
        if isinstance(node, Element) and node.tag == "screen":
            size_attr = node.attrs.get("size") or node.attrs.get("resolution", "1920,1080")
            try:
                w, h = (int(v) for v in size_attr.split(","))
            except ValueError:
                w, h = 1920, 1080
                print(f"WARNING: screen '{node.attrs.get('name', '?')}' has non-numeric size='{size_attr}', assuming {w}x{h}")
            yield node, w, h


def renderScreenBoxes(screen, screen_w, screen_h, globalColors, screen_idx):
    """Returns the list of SVG element strings (rects+labels) for one
    screen's first-level children, in screen-local coordinates (the caller
    wraps this in a <g transform="translate(...)"> for placement) - and
    draws in zPosition order (default 0, so an unset zPosition sorts with
    the others written in source order) so later/higher elements visually
    sit on top, same as the real renderer.

    Each rect carries data-ref="{screen_idx}:{child_idx}" - child_idx is
    this element's index in screen.children (the *unfiltered* list, so a
    skipped tag like <colors> still "uses up" an index, keeping numbering
    stable) - so svg2xml.py can match a possibly-edited box back to the
    exact element it came from, via the <metadata> tree it also embeds."""
    colorsNode = next((c for c in (screen.children or []) if isinstance(c, Element) and c.tag == "colors"), None)
    colormap = buildColorMap(colorsNode, globalColors)

    boxes = []
    for child_idx, node in enumerate(screen.children or []):
        if not isinstance(node, Element) or node.tag in SKIPPED_TAGS:
            continue
        try:
            z = int(node.attrs.get("zPosition", "0"))
        except ValueError:
            z = 0
        boxes.append((z, child_idx, node))
    boxes.sort(key=lambda t: (t[0], t[1]))

    out = []
    for _z, child_idx, node in boxes:
        x, y, w, h = resolveBox(node.attrs, screen_w, screen_h)
        fill = resolveColor(node.attrs.get("backgroundColor"), colormap) or PALETTE[child_idx % len(PALETTE)]
        label = describeElement(node)
        full = " ".join(f'{k}="{v}"' for k, v in node.attrs.items())
        out.append(
            f'<g>'
            f'<rect data-ref="{screen_idx}:{child_idx}" x="{x}" y="{y}" width="{w}" height="{h}" '
            f'fill="{fill}" fill-opacity="0.45" stroke="#000000" stroke-opacity="0.6" stroke-width="1">'
            f'<title>{escapeXml(f"<{node.tag}> {full}")}</title>'
            f'</rect>'
            f'<text x="{x + 4}" y="{y + 14}" font-family="monospace" font-size="12" fill="#000000">'
            f'{escapeXml(label)}'
            f'</text>'
            f'</g>'
        )
    return out


def elementToDict(node):
    """Serializes a parsed Element/Comment tree to plain dicts/lists so it
    can round-trip through json.dumps/loads - used to embed the *complete*
    original source (not just what gets drawn as a box: <colors>, a
    <widget>'s nested <convert>, everything) in the SVG's <metadata>, so
    svg2xml.py can reconstruct it losslessly and patch in only what an
    editor actually changed."""
    if isinstance(node, Comment):
        return {"comment": node.text}
    return {
        "tag": node.tag,
        "attrs": node.attrs,
        "children": [elementToDict(c) for c in node.children] if node.children else None,
        "text": node.text,
    }


def firstLevelBoundingBox(nodes):
    """Like resolveBox(), but restricted to elements with a fully numeric
    position=/size= (no e/center/%/eval token, which can't be resolved
    without already knowing the canvas size) - used to size a bare
    screenpart fragment (no enclosing <screen>) when no explicit --size is
    given. Mirrors xmlinc.py's own computeBoundingBox(), but first-level
    only, matching what this tool actually draws. Returns None if nothing
    measurable was found."""
    max_x = max_y = 0
    found = False
    for node in nodes:
        if not isinstance(node, Element) or node.tag in SKIPPED_TAGS:
            continue
        pos_attr = node.attrs.get("position")
        size_attr = node.attrs.get("size")
        if not pos_attr or not size_attr or pos_attr in ("fill",) or size_attr in ("fill",):
            continue
        try:
            x, y = (int(v) for v in pos_attr.split(","))
            w, h = (int(v) for v in size_attr.split(","))
        except ValueError:
            continue
        max_x = max(max_x, x + w)
        max_y = max(max_y, y + h)
        found = True
    return (max_x, max_y) if found else None


def scanColorsRecursive(nodes, cmap):
    """Collects every <color name=... value=.../> anywhere in the given
    tree (unlike buildColorMap(), which only looks at one element's direct
    children) - for loading colors from a separate file, e.g. a shared
    screenpart_colors.xmlinc, that a bare fragment doesn't carry itself."""
    for n in nodes:
        if not isinstance(n, Element):
            continue
        if n.tag == "color" and "name" in n.attrs and "value" in n.attrs:
            cmap[n.attrs["name"]] = n.attrs["value"]
        if n.children:
            scanColorsRecursive(n.children, cmap)


def wrapFragmentAsScreen(nodes, name, explicitSize):
    """Turns a bare screenpart's top-level element list (no enclosing
    <screen>, e.g. Common/src/skin/screenpart_*.xmlinc) into a synthetic
    <screen> so the rest of the pipeline (collectScreens() et al) doesn't
    need to know the difference. A single already-<screen> node is passed
    through as-is (it already carries its own size)."""
    if len(nodes) == 1 and isinstance(nodes[0], Element) and nodes[0].tag == "screen":
        screen = nodes[0]
        if explicitSize and not (screen.attrs.get("size") or screen.attrs.get("resolution")):
            screen.attrs["size"] = ",".join(explicitSize.lower().split("x"))
        return screen
    if explicitSize:
        w, h = (int(v) for v in explicitSize.lower().split("x"))
    else:
        box = firstLevelBoundingBox(nodes)
        if box:
            w, h = box
            print(f"no --size given, using computed bounding box: {w}x{h}")
        else:
            w, h = 1920, 1080
            print(f"WARNING: no --size given and no numeric position/size found to infer one, defaulting to {w}x{h}")
    return Element("screen", {"name": name, "size": f"{w},{h}"}, nodes, None)


def renderSvg(root, extraColors=None, fragment=False):
    screens = list(collectScreens(root))
    if not screens:
        print("WARNING: no <screen> elements found - nothing to draw")
        return None

    globalColors = buildColorMap(next((c for c in (root.children or []) if isinstance(c, Element) and c.tag == "colors"), None), extraColors)

    total_w = max(w for _s, w, _h in screens)
    total_h = sum(h + HEADER_HEIGHT + SCREEN_GAP for _s, _w, h in screens) - SCREEN_GAP

    # The complete original tree, one entry per screen (in the same order
    # as the data-ref="{screen_idx}:..." rects below reference) - lets
    # svg2xml.py reconstruct the source losslessly and patch in only what
    # actually moved, without needing to trust/recompute layout constants
    # like PAGE_MARGIN across tool versions (each screen's own <g
    # transform="translate(...)"> below is what a rect's coordinates are
    # actually relative to, and that's read back live from the SVG itself).
    metadata = {"fragment": fragment, "screens": [elementToDict(s) for s, _w, _h in screens]}
    metadata_text = escapeXml(json.dumps(metadata))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w + 2 * PAGE_MARGIN}" '
        f'height="{total_h + 2 * PAGE_MARGIN}" viewBox="0 0 {total_w + 2 * PAGE_MARGIN} {total_h + 2 * PAGE_MARGIN}">',
        f'<metadata id="xml2svg-source">{metadata_text}</metadata>',
        f'<rect x="0" y="0" width="{total_w + 2 * PAGE_MARGIN}" height="{total_h + 2 * PAGE_MARGIN}" fill="#f4f4f4"/>',
    ]

    y = PAGE_MARGIN
    for screen_idx, (screen, w, h) in enumerate(screens):
        name = screen.attrs.get("name", "?")
        parts.append(
            f'<text x="{PAGE_MARGIN}" y="{y + 18}" font-family="sans-serif" font-size="16" '
            f'font-weight="bold" fill="#111111">{escapeXml(name)} ({w}x{h})</text>'
        )
        content_y = y + HEADER_HEIGHT
        parts.append(f'<g data-screen="{screen_idx}" transform="translate({PAGE_MARGIN},{content_y})">')
        parts.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="#ffffff" stroke="#999999" stroke-width="1"/>')
        parts.extend(renderScreenBoxes(screen, w, h, globalColors, screen_idx))
        parts.append('</g>')
        y = content_y + h + SCREEN_GAP

    parts.append('</svg>')
    return "\n".join(parts) + "\n"


def parseArgs(argv):
    parser = argparse.ArgumentParser(prog="xml2svg.py")
    parser.add_argument("-i", dest="src", required=True,
                         help="compiled skin.xml (xmlinc already resolved), or a bare screenpart "
                              "fragment (e.g. Common/src/skin/screenpart_*.xmlinc) with no <screen> wrapper")
    parser.add_argument("-o", dest="dst", required=True, help="output .svg file")
    parser.add_argument("--size", dest="size", help="WxH (e.g. 615x740) to use as the canvas for a bare "
                                                      "fragment - defaults to the computed bounding box of its own elements")
    parser.add_argument("-c", dest="common", help="common dir for <xmlinc> resolution (source input only) - defaults to the input's own dir")
    parser.add_argument("--include", dest="includes", action="append", default=[],
                         help="source file(s) to process first, so their <global>/<color> defs (e.g. fonts.xmlinc, "
                              "colors.xmlinc) are known while resolving the input - repeatable")
    parser.add_argument("--compile", dest="compile", action="store_true",
                         help="resolve <xmlinc>/$var/eval() first even if the input doesn't look like a source "
                              "file (always done automatically for *.xmlinc and for any input containing <xmlinc>/<global>)")
    parser.add_argument("--colors", dest="colors", action="append", default=[],
                         help="file(s) to scan for <color name=... value=.../> defs (e.g. screenpart_colors.xmlinc) "
                              "to resolve named colors a bare fragment doesn't carry itself - repeatable")
    return parser.parse_args(argv)


SOURCE_TAGS = {"xmlinc", "global", "default", "layout"}


def containsSourceTags(nodes):
    """True if the tree still has <xmlinc>/<global>/... - i.e. it's an
    uncompiled source file that needs xmlinc.py's resolution first."""
    for n in nodes if isinstance(nodes, list) else [nodes]:
        if not isinstance(n, Element):
            continue
        if n.tag in SOURCE_TAGS or (n.children and containsSourceTags(n.children)):
            return True
    return False


def compileSource(src, common, includes, size):
    """Runs a source file (skin.xml / *.xmlinc, uncompiled) through xmlinc.py's
    own resolver and returns (parsed result, {color name: value}) - the same
    result a real build would splice in. size ("WxH") seeds $width/$height
    and $screen_width/$screen_height, which a bare screenpart inherits from
    whatever includes it on a real build."""
    srcdir = os.path.dirname(src)
    with tempfile.TemporaryDirectory() as tmp:
        inc = XMLInclude(srcdir, tmp, os.path.normpath(common) if common else srcdir)
        for inc_file in includes:
            inc_file = os.path.normpath(inc_file)
            doc = loadDocument(inc_file)
            inc.scanColors(doc)  # normally done when a file is pulled in via <xmlinc>
            inc.scanDefaults(doc)
            inc.processFile(0, inc_file)
        if not size:
            size = "1920x1080"
            print(f"no --size given, resolving $width/$height as {size} (use --size WxH to change)")
        if size:
            w, h = size.lower().split("x")
            inc.globals.update({"$width": w, "$height": h, "$screen_width": w, "$screen_height": h})
        result = inc.processFile(0, src)
    colors = {k.lstrip("$"): v for k, v in inc.colors.items()}
    return result, colors


def loadDocument(path):
    try:
        return XmlParser(readFile(path)).parseDocument()
    except XmlParseError as e:
        raise XmlParseError(f"{path}: {e}") from None


def xml2svg(argv):
    args = parseArgs(argv)
    src = os.path.normpath(args.src)
    dst = os.path.normpath(args.dst)

    print(f"reading: {src}")
    parsed = loadDocument(src)

    extraColors = {}
    if args.compile or src.lower().endswith(".xmlinc") or containsSourceTags(parsed):
        print("source input (<xmlinc>/$var/eval) - resolving with xmlinc first")
        parsed, extraColors = compileSource(src, args.common, args.includes, args.size)
    for colorFile in args.colors:
        scanColorsRecursive([parsed_doc] if isinstance(parsed_doc := loadDocument(colorFile), Element) else parsed_doc, extraColors)

    fragment = not (isinstance(parsed, Element) and parsed.tag == "skin")
    if not fragment:
        root = parsed
    else:
        nodes = parsed if isinstance(parsed, list) else [parsed]
        nodes = [n for n in nodes if not isinstance(n, Comment)]
        if not nodes:
            print("ERROR: input has no root element")
            return
        name = os.path.splitext(os.path.basename(src))[0]
        screenNode = wrapFragmentAsScreen(nodes, name, args.size)
        root = Element("skin", {}, [screenNode], None)

    svg = renderSvg(root, extraColors, fragment)
    if svg is None:
        return
    writeFile(dst, svg)
    print(f"wrote: {dst}")


if __name__ == "__main__":
    xml2svg(sys.argv[1:])
