# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


# Replaces the <screen> blocks of a compiled skin.xml by the ones of another compiled (and pretty-printed) file: the
# result of compiling a single screen (xmlinc.py -s) is put into the skin.xml of the last full build, see
# xmlcompilescreen.
#
# The blocks are found by line, not by parsing: the files are the output of xmlpretty, where a screen is a block that
# starts at "<indent><screen ... name="X" ...>" and ends at the first following "<indent></screen>". The rest of the
# skin.xml stays exactly as it was.


import argparse
import re
import sys
from FileUtils import readFile, writeFile


NAME_RE = re.compile(r'(?<![\w-])name="([^"]*)"')
START_RE = re.compile(r"^(\s*)<screen\b")


def findScreens(lines):
    """Returns [(name, first line, last line)] of the <screen> blocks in lines, in order."""
    screens = []
    i = 0
    while i < len(lines):
        m = START_RE.match(lines[i])
        if not m:
            i += 1
            continue
        indent = m.group(1)
        # the start tag: up to the line it is closed on
        tag_end = i
        while not lines[tag_end].rstrip().endswith(">") and tag_end + 1 < len(lines):
            tag_end += 1
        name_match = NAME_RE.search("\n".join(lines[i:tag_end + 1]))
        end = tag_end
        if not lines[tag_end].rstrip().endswith("/>"):
            end = tag_end + 1
            while end < len(lines) and lines[end].rstrip() != indent + "</screen>":
                end += 1
            if end >= len(lines):
                raise ValueError(f"line {i + 1}: <screen> is not closed")
        if name_match:
            screens.append((name_match.group(1), i, end))
        i = end + 1
    return screens


def spliceScreens(new_lines, old_lines):
    """Returns (the lines of old_lines with the screens of new_lines in place of the ones of the same name, replaced,
    unchanged, added: the names of the screens)."""
    old_screens = {}
    for name, start, end in findScreens(old_lines):
        old_screens.setdefault(name, (start, end))
    result = old_lines[:]
    replacements = []   # (first line, last line, new block) in old_lines
    replaced, unchanged, added = [], [], []
    for name, start, end in findScreens(new_lines):
        block = new_lines[start:end + 1]
        if name in old_screens:
            old_start, old_end = old_screens[name]
            if old_lines[old_start:old_end + 1] == block:
                unchanged.append(name)
            else:
                replacements.append((old_start, old_end, block))
                replaced.append(name)
        else:
            added.append(name)
            replacements.append((None, None, block))
    # last to first, so the line numbers of the blocks before stay valid
    for old_start, old_end, block in sorted((r for r in replacements if r[0] is not None), key=lambda r: r[0], reverse=True):
        result[old_start:old_end + 1] = block
    for _start, _end, block in (r for r in replacements if r[0] is None):
        # not in the skin yet: at the end (the order of the screens is the one of the full compile)
        close = max(i for i, line in enumerate(result) if line.strip() == "</skin>")
        result[close:close] = block
    return result, replaced, unchanged, added


def parseArgs(argv):
    parser = argparse.ArgumentParser(prog="xmlscreen.py")
    parser.add_argument("-i", dest="srcinfile", required=True, help="compiled file with the screens to put in")
    parser.add_argument("-o", dest="srcoutfile", required=True, help="skin.xml to put them in")
    return parser.parse_args(argv)


def xmlscreen(argv):
    args = parseArgs(argv)
    new_lines = readFile(args.srcinfile).split("\n")
    old_text = readFile(args.srcoutfile)
    if not old_text:
        print(f"ERROR: {args.srcoutfile} is missing or empty: compile the skin completely first")
        return 1
    if not findScreens(new_lines):
        print("ERROR: no screen was compiled: is the screen_* file included by the skin.xml of the plugin?")
        return 1

    result, replaced, unchanged, added = spliceScreens(new_lines, old_text.split("\n"))
    for name in replaced:
        print(f"screen {name}: replaced")
    for name in unchanged:
        print(f"screen {name}: unchanged")
    for name in added:
        print(f"screen {name}: not in {args.srcoutfile} yet, added at the end - compile the skin completely for the right order")
    if replaced or added:
        writeFile(args.srcoutfile, "\n".join(result))
    return 0


if __name__ == "__main__":
    sys.exit(xmlscreen(sys.argv[1:]))
