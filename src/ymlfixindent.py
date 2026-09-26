"""checkindent.py - checks and fixes indentation width in .yml/.ymlinc/
.zml/.zmlinc source files for SkinForge's own YAML dialect.

WHY THIS EXISTS
----------------
yml2xml.py's hand-written YamlParser (see its class docstring-level comments)
is NOT a lenient, self-describing-indentation parser like PyYAML - it
hardcodes every nested block at EXACTLY parent_indent + 2:

  - parseInlineKey(): a "key:" with nothing after the colon recurses via
    self.parseBlock(indent + 2) - the "+2" is a literal constant, not
    "whatever the next line happens to use".
  - parseSequence(): once inside that block, each "- " item is only
    recognized while its own indent is EXACTLY equal (e[0] != indent -
    strict equality, not "indent or deeper") to that expected value.

A file indented consistently at some OTHER width (4 spaces per level is
the one actually seen in the wild - see screenpart_MovieList.ymlinc's real
bug this tool exists to catch) does not error: parseSequence/parseMapping
just see indent values that never equal what they expect, so the block
looks empty and gets silently dropped. No warning, no exception - the
compiled XML just quietly loses that content. This tool exists to catch
and fix exactly that class of file before it ever reaches the compiler.

APPROACH
--------
Reconstruct each file's intended nesting the way any ordinary indentation-
sensitive format is read: whatever is indented further than the previous
line nests under it, whatever matches the previous line's indent is a
sibling, and a dedent closes back to whichever still-open ancestor's own
indent it matches - regardless of the absolute width used, and tolerant of
a file that mixes widths across different branches (as long as each
individual parent/child pair is still deeper-than-parent, which any
human-legible file already is). Re-emit every line at the exact width the
real parser needs: 2 spaces times its reconstructed nesting depth.

Block scalars ("key: |" and everything more-indented under it, per
YamlParser.parseBlockScalar()) are treated as opaque free-form text: shifted
by whatever delta their own opening line's indent changed by, never
restructured - reindenting a scalar body would corrupt it, not fix it.
"""
import argparse
import glob
import os
import re
import sys

from FileUtils import readFile, writeFile

INDENT_UNIT = 2


def tokenizeLines(text):
    """One entry per source line: None for a blank line (preserved as-is),
    otherwise (indent, content, had_tab). Mirrors YamlParser.__init__'s own
    tokenization (rstrip, count leading spaces) closely enough to reindent
    exactly the lines that parser will see."""
    tokens = []
    for raw in text.split("\n"):
        stripped = raw.rstrip()
        if not stripped.strip():
            tokens.append(None)
            continue
        leading = stripped[:len(stripped) - len(stripped.lstrip(" \t"))]
        had_tab = "\t" in leading
        # Tabs have no single well-defined width, and mixing them with
        # space-indented siblings breaks the plain integer indent
        # comparisons this whole algorithm relies on - expand up front
        # (standard 8-per-tab) so a tab-indented file is at least
        # internally comparable, same as the real parser already assumes
        # by treating tab-indentation as an outright error rather than
        # something to interpret.
        expanded = stripped.expandtabs(8)
        indent = len(expanded) - len(expanded.lstrip(" "))
        content = expanded.strip()
        tokens.append((indent, content, had_tab))
    return tokens


BARE_DASH_KEY_RE = re.compile(r"^-\s+\S+:\s*$")


def isBareDashKey(content):
    """True for a sequence item whose entire content is one key with
    nothing after the colon (e.g. "- convert:", "- icon:") - the shape
    parseSequenceItem() hands to parseInlineKey(), which then recurses via
    parseBlock(item_indent + 2). That +2 lands on top of parseSequence()'s
    own +2 for item_indent itself, so this exact line's nested body sits at
    indent + 4, not the usual + 2 every other "key with nothing after the
    colon" gets - two structural steps (entering the list item, then
    entering that key's own value block) compressed onto one visual line.
    A dash item with a real inline value ("- name: X") isn't this case:
    parseInlineKey() just does a plain parseScalar() for it, nothing nests
    under it at all."""
    return bool(BARE_DASH_KEY_RE.match(content))


def reindentTokens(tokens):
    """Returns (corrected, changed_line_count, had_tabs, ambiguous_dedents).

    corrected is a list matching tokens 1:1: None for a blank line,
    otherwise (new_indent, content)."""
    corrected = []
    changed = 0
    had_tabs = False
    ambiguous = 0

    # Stack of (source_indent, corrected_indent, child_step) for the
    # currently open ancestor chain. child_step is how much deeper this
    # frame's own children land: normally INDENT_UNIT, but doubled when
    # this frame's own line was a bare "- key:" item (see isBareDashKey).
    # The sentinel makes the first real line's indent compare as "deeper
    # than the root", landing it at corrected indent 0.
    stack = [(-1, -INDENT_UNIT, INDENT_UNIT)]

    in_block_scalar = False
    block_scalar_source_base = None
    block_scalar_delta = 0

    for tok in tokens:
        if tok is None:
            corrected.append(None)
            continue
        src_indent, content, had_tab = tok
        had_tabs = had_tabs or had_tab

        if in_block_scalar:
            if src_indent > block_scalar_source_base:
                new_indent = max(0, src_indent + block_scalar_delta)
                if new_indent != src_indent:
                    changed += 1
                corrected.append((new_indent, content))
                continue
            in_block_scalar = False
            # falls through: this line is not part of the scalar body,
            # process it normally below.

        while src_indent < stack[-1][0]:
            stack.pop()

        this_child_step = INDENT_UNIT * 2 if isBareDashKey(content) else INDENT_UNIT

        if src_indent == stack[-1][0]:
            new_indent = stack[-1][1]
            stack[-1] = (src_indent, new_indent, this_child_step)
        elif src_indent > stack[-1][0]:
            new_indent = stack[-1][1] + stack[-1][2]
            stack.append((src_indent, new_indent, this_child_step))
        else:
            # Popped past every open ancestor without an exact match - a
            # dedent that doesn't line up with anything still open. Rare
            # for a file the real parser already accepts today; flagged
            # rather than guessed at.
            ambiguous += 1
            new_indent = 0
            stack = [(-1, -INDENT_UNIT, INDENT_UNIT), (src_indent, 0, this_child_step)]

        if new_indent != src_indent:
            changed += 1
        corrected.append((new_indent, content))

        stripped_content = content.rstrip()
        if stripped_content == "|" or stripped_content.endswith(": |"):
            in_block_scalar = True
            block_scalar_source_base = src_indent
            block_scalar_delta = new_indent - src_indent

    return corrected, changed, had_tabs, ambiguous


def renderCorrected(corrected):
    lines = []
    for entry in corrected:
        if entry is None:
            lines.append("")
        else:
            indent, content = entry
            lines.append(" " * indent + content)
    return "\n".join(lines)


def checkFile(path, fix=False):
    """Returns (changed_line_count, ambiguous_dedent_count, had_tabs).
    Prints one line per file summarizing the result; with fix=True and any
    real (non-ambiguous) change found, rewrites the file in place."""
    text = readFile(path)
    if not text:
        return 0, 0, False

    tokens = tokenizeLines(text)
    corrected, changed, had_tabs, ambiguous = reindentTokens(tokens)

    if had_tabs:
        print(f"{path}: WARNING: tab-indented line(s) found - widths guessed via 8-per-tab expansion, verify manually")
    if ambiguous:
        print(f"{path}: WARNING: {ambiguous} dedent(s) didn't match any open ancestor indent - check manually, not auto-fixed with confidence")

    if changed == 0:
        print(f"{path}: OK ({INDENT_UNIT}-space nesting already correct)")
        return 0, ambiguous, had_tabs

    if fix:
        writeFile(path, renderCorrected(corrected) + ("\n" if text.endswith("\n") else ""))
        print(f"{path}: FIXED - reindented {changed} line(s)")
    else:
        print(f"{path}: WRONG INDENTATION - {changed} line(s) would be reindented (rerun with --fix to apply)")

    return changed, ambiguous, had_tabs


def expandArgsToFiles(args):
    files = []
    for arg in args:
        if os.path.isdir(arg):
            for ext in ("yml", "ymlinc", "zml", "zmlinc"):
                files.extend(sorted(glob.glob(os.path.join(arg, f"*.{ext}"))))
        else:
            matches = sorted(glob.glob(arg)) if any(c in arg for c in "*?[") else [arg]
            files.extend(matches)
    return files


def main(argv):
    parser = argparse.ArgumentParser(prog="checkindent.py")
    parser.add_argument("paths", nargs="+", help=".yml/.ymlinc/.zml/.zmlinc file(s), glob(s), or a directory to scan non-recursively")
    parser.add_argument("--fix", action="store_true", help="rewrite files with wrong indentation in place (default: report only)")
    args = parser.parse_args(argv)

    files = expandArgsToFiles(args.paths)
    if not files:
        print("checkindent.py: no matching files found")
        return 1

    total_changed = 0
    total_ambiguous = 0
    for f in files:
        if not os.path.isfile(f):
            print(f"{f}: ERROR: not a file")
            continue
        changed, ambiguous, _ = checkFile(f, fix=args.fix)
        total_changed += changed
        total_ambiguous += ambiguous

    if total_ambiguous:
        return 2
    return 1 if (total_changed and not args.fix) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
