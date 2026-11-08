# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0

# Reverses xml2svg.py: reads back the <metadata id="xml2svg-source"> JSON
# blob (the complete original parsed tree, one entry per screen) that
# xml2svg.py embeds in its output, matches each <rect data-ref=
# "screen_idx:child_idx"> to the element it was drawn from, and - only
# where a box's current on-canvas position/size actually differs from what
# that element originally resolved to - rewrites that element's
# position=/size= to the new concrete pixel values. An element nothing
# touched keeps its original position=/size= exactly as written (including
# any symbolic e/center/%/eval token), so a straight xml2svg -> svg2xml
# round trip with no edits reproduces the source (formatting aside).
#
# Everything the metadata tree carries but xml2svg.py never draws as a box
# - <colors>, <scrollbarstyle>, a <widget>'s own nested <convert> - passes
# through untouched too, since it's part of the same serialized tree.
#
# A rect's x/y/width/height are read relative to its own screen's group
# (data-screen="N", see xml2svg.py) rather than the SVG root - the same
# screen-local space resolveBox() works in - by walking fresh from that
# group with an identity starting matrix, composing in any transform=
# found along the way down to the rect (including one directly on the rect
# itself). That last part matters in practice: Inkscape (and most other
# SVG editors) commonly records a drag or a selection-tool resize as a
# transform="translate(...)" or transform="matrix(...)" added straight
# onto the shape, rather than rewriting its x/y/width/height - a plain
# collectRects() that only reads those attributes would silently ignore
# such an edit. rotate()/skewX()/skewY() aren't meaningful for this tool's
# axis-aligned boxes and are treated as identity with a warning rather than
# applied. The one thing no purely-in-SVG round trip can guard against is
# an editor that flattens a group's transform away entirely on save (bakes
# it into every child and removes the <g>, losing the data-screen anchor);
# a plain drag-and-save doesn't do this, but it's a known limitation.

import os
import re
import sys
import json
import argparse
from FileUtils import readFile, writeFile
from xmlinc import XmlParser, Element, Comment, renderNode, XmlParseError
from xml2svg import resolveBox

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
TRANSFORM_FUNC_RE = re.compile(r"(\w+)\s*\(([^)]*)\)")


def matMul(outer, inner):
    """Composes two SVG matrix(a,b,c,d,e,f)-convention affine matrices
    (x'=a*x+c*y+e, y'=b*x+d*y+f) so applying the result to a point equals
    outer(inner(point)) - i.e. inner (closer to the content) applies first."""
    a1, b1, c1, d1, e1, f1 = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (
        a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1,
    )


def applyMat(m, x, y):
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def transformBox(m, x, y, w, h):
    """Transforms a rect's 4 corners and returns the axis-aligned bounding
    box of the result - exact for translate/scale, a reasonable
    degradation for anything else (rotation isn't meaningful for a skin
    widget box anyway)."""
    corners = [applyMat(m, x, y), applyMat(m, x + w, y), applyMat(m, x, y + h), applyMat(m, x + w, y + h)]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def parseTransform(transform_str):
    """Parses an SVG transform="..." attribute into one combined 2x3
    affine matrix. Only translate()/scale()/matrix() are meaningful here;
    rotate()/skewX()/skewY() are treated as identity, with a warning
    (silently ignoring an edit would risk a silently wrong position more
    than refusing to guess at it)."""
    m = IDENTITY
    if not transform_str:
        return m
    for func, argstr in TRANSFORM_FUNC_RE.findall(transform_str):
        args = [float(v) for v in re.split(r"[,\s]+", argstr.strip()) if v]
        if func == "translate":
            fm = (1.0, 0.0, 0.0, 1.0, args[0], args[1] if len(args) > 1 else 0.0)
        elif func == "scale":
            sx = args[0]
            sy = args[1] if len(args) > 1 else sx
            fm = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif func == "matrix" and len(args) == 6:
            fm = tuple(args)
        else:
            print(f"WARNING: unsupported/ignored transform '{func}({argstr})' - only translate/scale/matrix "
                  f"are meaningful for this tool's axis-aligned boxes")
            fm = IDENTITY
        m = matMul(m, fm)
    return m


def unescapeXml(text):
    return (text.replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&amp;", "&"))


def dictToNode(d):
    if "comment" in d:
        return Comment(d["comment"])
    children = [dictToNode(c) for c in d["children"]] if d.get("children") else None
    return Element(d["tag"], dict(d["attrs"]), children, d.get("text"))


def findMetadata(svgRoot):
    for node in svgRoot.children or []:
        if isinstance(node, Element) and node.tag == "metadata" and node.attrs.get("id") == "xml2svg-source":
            return json.loads(unescapeXml(node.text or "{}"))
    return None


def findScreenGroups(node, out):
    """Finds each <g data-screen="N"> anywhere in the tree (xml2svg.py
    writes exactly one per screen), keyed by N - searched by this marker
    rather than assumed to be the SVG's direct children, so this still
    works if an editor wrapped/reordered things around it."""
    if not isinstance(node, Element):
        return
    if node.tag == "g" and "data-screen" in node.attrs:
        out[node.attrs["data-screen"]] = node
    for child in node.children or []:
        findScreenGroups(child, out)


def walkForRects(node, m, out):
    """Recurses from (typically) a screen group's own children, composing
    in every transform= found along the way (any nested <g>, and/or the
    rect's own attribute - see module docstring for why the latter
    matters), and for each <rect data-ref=...> found records its box -
    already resolved into the *caller's* starting coordinate space -
    keyed by its data-ref."""
    if not isinstance(node, Element):
        return
    m2 = matMul(m, parseTransform(node.attrs.get("transform"))) if "transform" in node.attrs else m
    if node.tag == "rect" and "data-ref" in node.attrs:
        try:
            x = float(node.attrs.get("x", 0))
            y = float(node.attrs.get("y", 0))
            w = float(node.attrs.get("width", 0))
            h = float(node.attrs.get("height", 0))
        except ValueError:
            x = y = w = h = None
        if x is not None:
            bx, by, bw, bh = transformBox(m2, x, y, w, h)
            out[node.attrs["data-ref"]] = (round(bx), round(by), round(bw), round(bh))
    for child in node.children or []:
        walkForRects(child, m2, out)


def collectRects(svgRoot):
    """Returns {data-ref: (x, y, w, h)} for every box, each resolved in its
    own screen's local coordinate space - starting the walk fresh (identity
    matrix) from each <g data-screen="N">'s children deliberately excludes
    that group's *own* transform= (the screen's page placement, not part
    of what "local" means for its contents) from the accumulation."""
    screenGroups = {}
    findScreenGroups(svgRoot, screenGroups)
    rects = {}
    for groupNode in screenGroups.values():
        for child in groupNode.children or []:
            walkForRects(child, IDENTITY, rects)
    return rects


def applyEdits(screenTree, screen_w, screen_h, rects, screen_idx):
    """Patches position=/size= on screenTree's first-level children in
    place, wherever the matching rect's current box differs from what that
    element originally resolved to (via the same resolveBox() xml2svg.py
    used to draw it) - so an element nobody touched keeps its original
    (possibly symbolic) position=/size= untouched. Returns how many
    elements were actually changed."""
    changed = 0
    for child_idx, node in enumerate(screenTree.children or []):
        if not isinstance(node, Element):
            continue
        ref = f"{screen_idx}:{child_idx}"
        if ref not in rects:
            continue
        orig = resolveBox(node.attrs, screen_w, screen_h)
        new = rects[ref]
        if new == orig:
            continue
        x, y, w, h = new
        node.attrs["position"] = f"{x},{y}"
        node.attrs["size"] = f"{w},{h}"
        changed += 1
    return changed


def parseArgs(argv):
    parser = argparse.ArgumentParser(prog="svg2xml.py")
    parser.add_argument("-i", dest="src", required=True, help="SVG previously produced by xml2svg.py (possibly hand-edited)")
    parser.add_argument("-o", dest="dst", required=True, help="output .xml/.xmlinc file")
    return parser.parse_args(argv)


def svg2xml(argv):
    args = parseArgs(argv)
    src = os.path.normpath(args.src)
    dst = os.path.normpath(args.dst)

    print(f"reading: {src}")
    try:
        svgRoot = XmlParser(readFile(src)).parseDocument()
    except XmlParseError as e:
        raise XmlParseError(f"{src}: {e}") from None
    if isinstance(svgRoot, list):
        svgRoot = next((n for n in svgRoot if isinstance(n, Element) and n.tag == "svg"), None)
    if not isinstance(svgRoot, Element):
        print("ERROR: no <svg> root found")
        return

    metadata = findMetadata(svgRoot)
    if metadata is None:
        print('ERROR: no <metadata id="xml2svg-source"> found - this SVG wasn\'t produced by xml2svg.py '
              '(or the metadata was stripped by whatever last saved it)')
        return

    rects = collectRects(svgRoot)

    screens = [dictToNode(s) for s in metadata["screens"]]
    total_changed = 0
    for screen_idx, screenTree in enumerate(screens):
        size_attr = screenTree.attrs.get("size") or screenTree.attrs.get("resolution", "1920,1080")   # as xml2svg.py draws it
        w, h = (int(v) for v in size_attr.split(","))
        total_changed += applyEdits(screenTree, w, h, rects, screen_idx)
    print(f"{total_changed} element(s) repositioned/resized")

    lines = []
    if metadata.get("fragment"):
        # A bare screenpart had no <screen> wrapper in the source - and
        # xml2svg.py only ever synthesizes exactly one for that case - so
        # unwrap back down to just its children, matching what it was
        # originally given.
        for child in screens[0].children or []:
            renderNode(child, lines)
    else:
        skinRoot = Element("skin", {}, screens, None)
        renderNode(skinRoot, lines)

    output = "\n".join(lines) + "\n"
    writeFile(dst, output)
    print(f"wrote: {dst}")


if __name__ == "__main__":
    svg2xml(sys.argv[1:])
