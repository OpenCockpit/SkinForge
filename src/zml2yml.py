# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


# Expands ZAML's "for" loop construct into plain SkinForge YAML (.yml /
# .ymlinc), the same dialect yml2xml.py consumes. This is a line-based
# text expander, not a general-purpose YAML implementation, deliberately -
# it only ever needs to find one specific block shape and copy/substitute
# its lines, so it doesn't need (and shouldn't pull in) a real parser.
#
# Syntax:
#   - for:
#       var: "$i"
#       range: [0, 5]
#       body:
#         - xmlinc:
#             file: "screenpart_PrimeCell.ymlinc"
#             index: "$i"
#             position: "$i*$screen_width/6,0"
#
# "var" names the token exactly as it's written in body ("$i", with the
# $), rather than a separate bare name you'd have to mentally re-prefix -
# what you declare is what you'll see used below it. The $/quotes are
# optional (plain "var: i" still works) so existing files aren't broken.
#
# A loop iterates over either "range: [start, end]" (inclusive, numeric)
# or "values: [a, b, c]" (a literal list, iterated in order, duplicates
# allowed) - exactly one of the two, never both. Values may be quoted
# ("text1") or bare (text1); quotes are stripped, same as "var".
#
# expands into one copy of body per iteration, spliced in place of the
# single "for" list item at the same indentation, with every occurrence
# of $i in each copy replaced by that iteration's value (the range
# number, or the literal value text) as plain text - the same "$var
# embeds in a literal" convention xmlinc itself uses (e.g. "list$i"), so
# nothing downstream needs to know a loop produced the result. Nesting
# works: an inner "for" inside a "body" is left untouched during the
# outer loop's own pass (its lines are just copied and $-substituted
# like any other body line) and gets expanded on the next pass over the
# whole file.
#
# A malformed "for" item (missing var/range/body) is left in the output
# untouched rather than silently dropped, so a typo fails loudly further
# down the pipeline (yml2xml/xmlinc won't recognize "for:") instead of
# quietly vanishing.
#
# An <xmlinc file="..."> reference is free to point at another .zmlinc
# fragment (one that itself uses "for", say) exactly like it would point
# at a .ymlinc one - rewriteIncludeRefs() below retargets any
# file: "X.zmlinc" to file: "X.ymlinc" in the output, the same way
# yml2xml.py's toXmlIncFile() retargets .ymlinc to .xmlinc one layer
# down. This runs after "for" expansion, so it also catches references
# that only exist inside an expanded loop body. It's a text rewrite, not
# a recursive fetch: the referenced .zmlinc file still has to be expanded
# in its own right (zml2ymldomain does this for every .zmlinc file it
# finds, independent of who references it) - this just makes sure
# whichever file references it points at the right compiled name once
# that's done.


import argparse
import os
import re
import sys
from FileUtils import readFile, writeFile


FOR_ITEM_RE = re.compile(r'^(?P<indent>[ \t]*)-\s+for:\s*$')
VAR_LINE_RE = re.compile(r'^[ \t]+var:\s*"?\$?(\w+)"?\s*$')
RANGE_LINE_RE = re.compile(r'^[ \t]+range:\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\s*$')
VALUES_LINE_RE = re.compile(r'^[ \t]+values:\s*\[(?P<items>.*)\]\s*$')
BODY_LINE_RE = re.compile(r'^[ \t]+body:\s*$')
FILE_REF_RE = re.compile(r'(file:\s*"[^"]*)\.zmlinc(")')


def indentLen(line):
    return len(line) - len(line.lstrip(" \t"))


def parseValuesList(itemsText):
    """Splits a "values: [a, "b", c]" payload into ["a", "b", "c"],
    stripping surrounding quotes per item (same convention as "var")."""
    values = []
    for part in itemsText.split(","):
        part = part.strip()
        if not part:
            continue
        if len(part) >= 2 and part[0] == part[-1] and part[0] in "'\"":
            part = part[1:-1]
        values.append(part)
    return values


def expandOnce(text):
    """One pass over the file: expands every "for" item found at any
    indentation level, leaving everything else untouched. Returns
    (new_text, changed)."""
    lines = text.split("\n")
    out = []
    changed = False
    i = 0
    n = len(lines)
    while i < n:
        m = FOR_ITEM_RE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue

        item_indent = m.group("indent")
        var_name = None
        var_range = None
        var_values = None
        body_lines = []
        in_body = False
        j = i + 1
        while j < n:
            raw = lines[j]
            if raw.strip() == "":
                if in_body:
                    body_lines.append(raw)
                j += 1
                continue
            if indentLen(raw) <= len(item_indent):
                break
            if not in_body:
                mv = VAR_LINE_RE.match(raw)
                mr = RANGE_LINE_RE.match(raw)
                mvals = VALUES_LINE_RE.match(raw)
                mb = BODY_LINE_RE.match(raw)
                if mv:
                    var_name = mv.group(1)
                elif mr:
                    var_range = (int(mr.group(1)), int(mr.group(2)))
                elif mvals:
                    var_values = parseValuesList(mvals.group("items"))
                elif mb:
                    in_body = True
                j += 1
                continue
            body_lines.append(raw)
            j += 1

        if var_name is None or not body_lines or (var_range is None and not var_values):
            out.append(lines[i])
            i += 1
            continue

        changed = True
        first_content = next(bl for bl in body_lines if bl.strip() != "")
        shift = indentLen(first_content) - len(item_indent)
        var_re = re.compile(r'\$' + re.escape(var_name) + r'(?![A-Za-z0-9_])')

        if var_values is not None:
            iter_values = var_values
        else:
            start, end = var_range
            iter_values = [str(v) for v in range(start, end + 1)]

        for value in iter_values:
            for bl in body_lines:
                if bl.strip() == "":
                    out.append("")
                    continue
                dedented = bl[shift:] if 0 < shift <= len(bl) else bl
                out.append(var_re.sub(lambda _m, v=value: v, dedented))

        i = j

    return "\n".join(out), changed


def rewriteIncludeRefs(text):
    """Mirrors yml2xml.py's toXmlIncFile(): an <xmlinc file="..."> value
    written against a .zmlinc fragment has to point at the compiled
    .ymlinc sibling instead, since only that sibling exists on disk by
    the time yml2xml/xmlinc run - they have no idea what a .zmlinc file
    is. A file: "X.ymlinc" reference is left alone (already correct)."""
    return FILE_REF_RE.sub(r'\1.ymlinc\2', text)


def zml2yml(text):
    changed = True
    while changed:
        text, changed = expandOnce(text)
    return rewriteIncludeRefs(text)


def deriveOutFile(srcinfile):
    if srcinfile.endswith(".zmlinc"):
        return srcinfile[: -len(".zmlinc")] + ".ymlinc"
    if srcinfile.endswith(".zml"):
        return srcinfile[: -len(".zml")] + ".yml"
    raise ValueError(f"cannot derive output filename from: {srcinfile}")


def parseArgs(argv):
    parser = argparse.ArgumentParser(prog="zml2yml.py")
    parser.add_argument("-i", dest="srcinfile", required=True, help="source .zml/.zmlinc file")
    parser.add_argument("-o", dest="srcoutfile", default=None,
                        help="destination .yml/.ymlinc file (default: input filename with opposite extension)")
    return parser.parse_args(argv)


def main(argv):
    args = parseArgs(argv)
    srcoutfile = args.srcoutfile or deriveOutFile(args.srcinfile)

    if not os.path.isfile(args.srcinfile):
        print(f"ERROR: source file not found: {args.srcinfile}")
        sys.exit(1)

    output = zml2yml(readFile(args.srcinfile))
    writeFile(srcoutfile, output)


if __name__ == "__main__":
    main(sys.argv[1:])
