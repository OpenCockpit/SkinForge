# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0

import os
import sys
import argparse
import re
import xml.etree.ElementTree as ET
from xml.dom import minidom
from xml.sax.saxutils import escape
from FileUtils import readFile, writeFile


def add_root(lines):
    lines = ["<root>"] + lines + ["</root>"]
    return lines


def remove_root(ilines):
    olines = []
    for line in ilines:
        if line and not ("<root>" in line or "</root>" in line or "<?xml" in line):
            # minidom indented every one of these lines one level deeper
            # than it should really be, since they're technically <root>'s
            # children now - strip that one synthetic tab back off so a
            # .xmlinc fragment's own top-level element(s) start flush at
            # column 0, matching how the file actually gets spliced into
            # its parent (nothing here is really nested under anything).
            olines.append(line[1:] if line.startswith("\t") else line)
    # print("olines: %s" % olines)
    return olines


def process_file(src_file, dst_file):
    print(f"process_file: {src_file}")
    lines = readFile(src_file).splitlines()
    if os.path.splitext(src_file)[1] == ".xmlinc":
        lines = add_root(lines)
    xml_string = "\n".join(lines)
    tree = ET.ElementTree(ET.fromstring(xml_string))
    root = tree.getroot()
    # An <applet>'s text is python code: its indentation is significant, so it must
    # come out exactly as it went in. The pretty-printing below (and remove_root(),
    # which strips one leading tab from every line) would eat that indentation -
    # set the text aside behind a placeholder and put it back at the very end.
    applet_texts = []
    for applet in root.iter("applet"):
        if applet.text and applet.text.strip():
            applet_texts.append(applet.text)
            applet.text = f"@@APPLET_TEXT_{len(applet_texts) - 1}@@"
    try:
        xml_string = ET.tostring(root, encoding="unicode", method="xml")
        xml_string = minidom.parseString(
            xml_string.encode("utf-8")).toprettyxml(indent="\t")
        xml_string = re.sub(
            r'<(\w+)\s+([^>]*?)/?>',
            lambda m: reorder_attributes_alphabetically(m.group(0)),
            xml_string
        )
        # position=/size= attribute *values* (as opposed to a <convert>
        # block's own pos=/size= tuples, handled separately below) are
        # otherwise never touched at all - a widget written directly in a
        # screen keeps whatever comma-spacing its source happened to use,
        # while one reached through an <xmlinc> only gets its own position=
        # tightened as a side effect of that tool's offset math (size= not
        # even then). Enforce the same tight convention as everywhere else
        # in this codebase, uniformly, regardless of source style or
        # include-routing - this only ever matches a real "key=" attribute
        # (quote right after "="), never text inside a <convert> body,
        # where a size is always written size=(w,h) with parens, not quotes.
        xml_string = re.sub(
            r'\b(position|size)="([^"]*)"',
            lambda m: f'{m.group(1)}="{re.sub(r"\s*,\s*", ",", m.group(2))}"',
            xml_string
        )

        # minidom's toprettyxml keeps a leaf element's own opening/closing
        # tags on separate lines from its siblings, but collapses its text
        # content onto the same line as those tags (e.g. <convert type="...">
        # {...}</convert> all glued together) - true for every <convert>
        # tag, not just the TemplatedMultiContent(Ex) ones the next pass
        # fully reformats the body of. Break those three pieces back onto
        # their own lines first for any dict-bodied convert (starts with
        # "{"), so a type that pass doesn't recognize (e.g. this codebase's
        # "COCTemplatedMultiContentEx") still ends up with </convert> on its
        # own line instead of glued onto the body's closing brace. A plain
        # string-bodied convert (ClockToText, ServiceTime, ...) is always
        # written single-line in this codebase - skip those, or this would
        # introduce diff noise splitting them across three lines instead.
        def fix_convert_newlines(m):
            indent, tag, body, close = m.group(1), m.group(2), m.group(3), m.group(4)
            if not body.startswith("{"):
                return m.group(0)
            # The captured body still carries its own original relative
            # indentation (e.g. 4 spaces per level) but no leading indent of
            # its own - shift every one of its lines under the tag's indent
            # so it actually nests under <convert>, not sits at column 0.
            lines = body.splitlines()
            # A lone closing bracket/brace/paren as the last line matches
            # the opening "{"'s own level, not its sibling keys' one level
            # deeper - force it back to plain `indent` instead of carrying
            # forward whatever (deeper) relative indent it originally had.
            closing = None
            if len(lines) > 1 and lines[-1].strip() in {"}", "]", ")"}:
                closing = lines.pop().strip()
            body = "\n".join(indent + line if line else line for line in lines)
            if closing is not None:
                body += f"\n{indent}{closing}"
            return f'\n{indent}{tag}\n{body}\n{indent}{close}'

        xml_string = re.sub(
            r'\n([ \t]*)(<convert[^>]*>)\s*(.*?)\s*(</convert>)',
            fix_convert_newlines,
            xml_string,
            flags=re.DOTALL
        )
        xml_string = re.sub(
            r'\n([ \t]*)(<convert[^>]*type="[A-Za-z]*TemplatedMultiContent(?:Ex)?"[^>]*>)(.*?)(</convert>)',
            lambda m: (
                "\n" + m.group(1) + m.group(2) + "\n" +
                format_multicontent(m.group(3), m.group(1)) + "\n" +
                m.group(1) + m.group(4)
            ),
            xml_string,
            flags=re.DOTALL
        )
    except Exception as e:
        print(f"failed to process {src_file}: {e}")
        return
    lines = [line for line in xml_string.splitlines() if line.split()]
    if os.path.splitext(src_file)[1] == ".xmlinc":
        lines = remove_root(lines)
    xml_string = "\n".join(lines)
    xml_string = xml_string.replace("&quot;", '"')
    for i, text in enumerate(applet_texts):
        xml_string = xml_string.replace(f"@@APPLET_TEXT_{i}@@", escape(text))
    xml_string += "\n"
    # print(xml_string)
    writeFile(dst_file, xml_string)


POS_SIZE_OPEN_RE = re.compile(r'\b(pos|size)=\(')
GFONT_RE = re.compile(r'''\bgFont\((["'][^"']*["'])\s*,\s*(-?[\w.]+)\)''')


def protect_pos_size(text):
    """Finds every pos=(...)/size=(...) tuple via balanced-paren scanning
    (not a fixed-shape regex like TUPLE_ELEM used to be - real grid-math
    elements can have their own nested parens and "//", e.g. pos=(hspace//2,
    (ih-thumb_h)//2), which no simple element pattern can cover) and
    replaces its own top-level comma(s) with a placeholder, protecting them
    from the general comma-spacer below (this must *normalize* an already-
    spaced source too, e.g. hand-authored pos=(90, 6), not just protect an
    already-tight one - the placeholder swap achieves both: whatever
    whitespace was around the comma is discarded, restored as tight "," at
    the end)."""
    out = []
    i, n = 0, len(text)
    while i < n:
        m = POS_SIZE_OPEN_RE.match(text, i)
        if not m:
            out.append(text[i])
            i += 1
            continue
        out.append(text[i:m.end()])
        i = m.end()
        depth = 1
        start = i
        while depth > 0:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        inner = text[start:i - 1]
        depth2 = 0
        protected = []
        for ch in inner:
            if ch == "(":
                depth2 += 1
            elif ch == ")":
                depth2 -= 1
            protected.append("\x00" if ch == "," and depth2 == 0 else ch)
        # The placeholder swap alone only stops the general comma-spacer
        # below from *adding* a space - any whitespace the source already
        # had directly around the comma (e.g. hand-authored "hspace//2, (...)")
        # is still sitting right there in `protected` and needs stripping
        # too, or the restored comma stays loose instead of tight.
        inner_protected = re.sub(r'\s*\x00\s*', '\x00', "".join(protected))
        out.append(inner_protected + ")")
    return "".join(out)


KEY_COLON_RE = re.compile(
    r'"(template|fonts|itemHeight|itemWidth|selectionEnabled|scrollbarMode|orientation)":(?!\s)')
SINGLE_QUOTED_RE = re.compile(r"'((?:[^'\\]|\\.)*)'")


def singleToDoubleQuoted(m):
    # This codebase's own convention is always "..." - swap the quote chars
    # and re-escape so the result stays a valid string either way: a \' no
    # longer needs escaping once it's inside "...", and a bare " now does.
    inner = m.group(1).replace("\\'", "'").replace('"', '\\"')
    return f'"{inner}"'


VAR_OPEN_RE = re.compile(r'^\{\s*"var":\s*\(')
TEMPLATE_OPEN_RE = re.compile(r'\s*"template":\s*\[\s*')


def format_var_section(text, i1, i2):
    """Reformats a leading "var": (name := expr, ...) tuple -
    TemplatedMultiContentEx's local-variable feature, e.g. this codebase's
    "COCTemplatedMultiContentEx" convert type - into one binding per line,
    matching this codebase's own convention (also what yml2xml.py's
    renderConvertTemplate produces): "{"/the closing "),"/"template": ["
    all sit at the tag's own level +1, each binding one further unit deep.
    Uses balanced-paren scanning, not a naive comma-split, since a
    binding's own arithmetic can contain nested parens (e.g. (e-x1)//4).
    A no-op (returns text unchanged) when there's no leading "var" section
    at all, or its shape isn't exactly this (immediately followed by
    "template": [ once the tuple closes) - so an ordinary
    TemplatedMultiContent(Ex) body without one is entirely untouched here,
    left to the caller's own "{"..."template": [ handling instead."""
    m = VAR_OPEN_RE.match(text)
    if not m:
        return text
    pos = m.end()
    depth = 1
    while depth > 0:
        if text[pos] == "(":
            depth += 1
        elif text[pos] == ")":
            depth -= 1
        pos += 1
    inner = text[m.end():pos - 1]
    bindings = []
    depth = 0
    current = ""
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            if current.strip():
                bindings.append(" ".join(current.split()))
            current = ""
        else:
            current += ch
    if current.strip():
        bindings.append(" ".join(current.split()))
    rest = text[pos:].lstrip()
    if rest.startswith(","):
        rest = rest[1:].lstrip()
    template_match = TEMPLATE_OPEN_RE.match(rest)
    if not template_match:
        return text
    rest = rest[template_match.end():]
    formatted_bindings = "".join(f"{i2}{b},\n" for b in bindings)
    return f'{{\n{i1}"var": (\n{formatted_bindings}{i1}),\n{i1}"template": [\n{i2}{rest}'


def format_multicontent(text, base_indent=""):
    i1 = base_indent + "\t"
    i2 = base_indent + "\t\t"
    # Any of these can trail "template": one per line, in whatever order/
    # subset they actually appear in - not just "fonts" then "itemHeight",
    # which left e.g. "selectionEnabled" glued onto the same line as
    # whatever preceded it since nothing broke a line before it.
    trailing_key = r'"(?:fonts|itemHeight|itemWidth|selectionEnabled|scrollbarMode|orientation)"'
    text = text.strip()
    # Handle an optional leading "var" section (and the "template": [ that
    # follows it) first, before anything below - once formatted, its
    # commas are already followed by a newline, so the general comma-
    # spacer a few lines down leaves them alone rather than double-
    # processing them, and the "{"..."template": [ regex further down
    # naturally becomes a no-op here (nothing left for it to match).
    text = format_var_section(text, i1, i2)
    # Normalize every single-quoted string literal (e.g. gFont('Regular', 32),
    # foreColor='#bababa') to double-quoted, first - everything below then
    # only ever has to deal with one quote style.
    text = SINGLE_QUOTED_RE.sub(singleToDoubleQuoted, text)
    # Ensure a space after the colon of these object keys (e.g. "itemHeight":
    # 65, never "itemHeight":65) - scoped to just these keys, not colons in
    # general, so a string value that happens to contain one (e.g. a clock
    # format "%H:%M") is never touched.
    text = KEY_COLON_RE.sub(lambda m: f'"{m.group(1)}": ', text)
    # Ensure a space after every comma between elements (kwargs, list items)
    # - but not the comma inside a pos=(x,y)/size=(w,h) tuple or a
    # gFont(family,size) call, which stay tight (e.g. pos=(10,0),
    # gFont("Regular",32), never pos=(10, 0)/gFont("Regular", 32)) per this
    # codebase's own convention there. Protect those specific commas with a
    # placeholder, space every other comma, then restore.
    text = protect_pos_size(text)
    text = GFONT_RE.sub(lambda m: f'gFont({m.group(1)}\x00{m.group(2)})', text)
    text = re.sub(r',(?!\s)', ', ', text)
    text = text.replace('\x00', ',')
    # flags OR-chains (e.g. RT_HALIGN_LEFT|RT_VALIGN_CENTER) stay tight too,
    # like pos=/size=/gFont(...) above - never RT_HALIGN_LEFT | RT_VALIGN_CENTER.
    text = re.sub(r'\s*\|\s*', '|', text)
    text = re.sub(r',\s*(MultiContent\w+)', f',\n{i2}\\1', text)
    text = re.sub(r'\{\s*"template":\s*\[\s*', f'{{\n{i1}"template": [\n{i2}', text)
    text = re.sub(r'\s*\]\s*,', f'\n{i1}],', text, count=1)
    text = re.sub(rf',\s*(?={trailing_key})', f',\n{i1}', text)
    text = re.sub(r'\s*\}\s*$', f'\n{base_indent}}}', text)
    return base_indent + text


def reorder_attributes_alphabetically(element_tag):
    # Handle both self-closing tags and opening tags
    self_closing = element_tag.endswith('/>')

    # Extract tag name and attributes
    if self_closing:
        tag_match = re.match(r'<(\w+)\s+([^>]*?)/>', element_tag)
    else:
        tag_match = re.match(r'<(\w+)\s+([^>]*?)>', element_tag)

    if not tag_match:
        return element_tag

    tag_name = tag_match.group(1)
    attrs_string = tag_match.group(2) if tag_match.group(2) else ""

    # Extract all attributes
    attrs = {}
    attr_pattern = r'(\w+)="([^"]*)"'

    for match in re.finditer(attr_pattern, attrs_string):
        attr_name, attr_value = match.groups()
        attrs[attr_name] = attr_value

    # Build the reordered tag with alphabetically sorted attributes
    result = f'<{tag_name}'

    for attr in sorted(attrs.keys()):
        result += f' {attr}="{attrs[attr]}"'

    # Preserve self-closing format
    if self_closing:
        result += '/>'
    else:
        result += '>'

    return result


def parseArgs(argv):
    parser = argparse.ArgumentParser(prog="xmlpretty.py")
    parser.add_argument("-i", "--src", dest="src", required=True, help="source file")
    parser.add_argument("-o", "--dst", dest="dst", help="destination file (defaults to source)")
    return parser.parse_args(argv)


def xmlpretty(argv):
    args = parseArgs(argv)
    src = os.path.normpath(args.src)
    dst = os.path.normpath(args.dst) if args.dst else src

    print('xmlpretty: ' + src + " > " + dst)

    process_file(src, dst)

    # print("xmlpretty done.")


if __name__ == "__main__":
    xmlpretty(sys.argv[1:])
