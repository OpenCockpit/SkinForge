# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


import os
import sys
import re
import argparse
from fractions import Fraction
from FileUtils import readFile, writeFile
from xmlinc import XmlParser, Comment, renderNode

ending_values = ["Width", "Height", "Font", "Offset", "Margin", "HPos"]
key_values = ["xres", "yres", "lineSpacing", "value", "textX", "textY", "pixmapX", "pixmapY", "separation", "rcheight",
              "rcheighthalf", "font", "position", "size", "size2", "offset", "margin", "cornerDia", "selectionDia", "width", "cornerRadius"]
sep1 = (" ", '"', "{", "[", "(", ",", ";", ":", "=", "}", "]", ")")
sep2 = ("+", "-", "*", "/", "%")


def split(s, sep):
    # print("split: s: %s" % s)
    pattern = f"({'|'.join(re.escape(c) for c in sep)})"
    o = re.split(pattern, s)
    o = [item for item in o if item != '']
    # print("split: o: %s" % o)
    return o


def scaleFormula(scale, value):
    # print("scaleFormula: %s" % value)
    words = split(value, sep1 + sep2)
    formula = ""
    for word in words:
        try:
            value = scaleValue(scale, int(word))
        except Exception:  # as e:
            # print("exception: %s" % e)
            value = word
        formula += value
    # print("formula: %s" % formula)
    return formula


def scaleValue(scale, value):
    # print("scaleValue: %s, %s" % (scale, value))
    if value in {'(', ','}:
        raise ValueError(f"invalid value: {value}")
    if isinstance(value, str):
        if value.startswith("eval("):
            value = scaleFormula(scale, value)
    try:
        if int(value) > 1:
            # Add 0.5 and truncate to get "round half up" behavior
            scaled_value = int(value) * scale
            value = int(scaled_value +
                        0.5) if scaled_value >= 0 else int(scaled_value - 0.5)
    except Exception:  # as e:
        # print("exception: %s" % e)
        pass
    # print("scaleValue result: %s" % value)
    return str(value)


class Template():
    def __init__(self):
        self.sep = ("{", "[", "(", ",", ":", "=", "}", "]", ")")

    def clean(self, i):
        o = i.replace("\n", " ").replace("\t", " ")
        o = " ".join(o.split())
        return o

    def parse(self, scale, i):
        # print("parse: %s" % i)
        o = []
        n = 0
        while n < len(i):
            k = i[n].strip()
            if k in {"pos", "size"}:
                o.append(f"{k}=({scaleValue(scale, i[n + 3])},{scaleValue(scale, i[n + 5])})")
                n += 7
            elif k in {"gFont"}:
                o.append(f"{k}({scaleValue(scale, i[n + 2])},{scaleValue(scale, i[n + 4])})")
                n += 6
            elif k in {'"itemHeight"', '"itemWidth"'}:
                o.append(f"{k}:{scaleValue(scale, i[n + 2])}")
                n += 3
            elif k in {":"}:
                if i[n + 1] == "(":
                    snum = i[n + 2]
                    try:
                        int(snum)
                        o.append(f"{k}({scaleValue(scale, i[n + 2])}")
                        n += 3
                    except Exception:
                        o.append(k)
                        n += 1
                else:
                    o.append(k)
                    n += 1
            else:
                o.append(k)
                n += 1
        return o

    def split(self, s, sep):
        # print("split: s: %s" % s)
        pattern = f"({'|'.join(re.escape(c) for c in sep)})"
        # print("pattern: %s" % pattern)
        r = re.split(pattern, s)
        r = [item for item in r if item != '']
        # print("split: r: %s" % r)
        return r

    def joinFormulas(self, words):
        # print("eval: words: %s" % words)
        output = []
        i = 0
        while i < len(words):
            if words[i] == "eval":
                formula = ""
                i += 1
                level = 0
                while level > 0 or not formula:
                    if words[i] in {"(", ")"}:
                        level += 1 if words[i] == "(" else -1
                    formula += words[i]
                    i += 1
                # result = int(eval(formula))
                # output.append(str(result))
                output.append("eval" + formula)
            else:
                output.append(words[i])
                i += 1
        # print("output: %s" % output)
        return output

    def scaleTemplate(self, scale, i):
        # print("scaleTemplate: %s: %s" % (scale, i))
        result = self.clean(i)
        # print("clean:\n%s" % result)
        result = self.split(result, self.sep)
        # print("split:\n%s" % result)
        result = self.joinFormulas(result)
        # print("joinFormulas:\n%s" % result)
        result = self.parse(scale, result)
        # print("parse:\n%s" % result)
        result = "".join(result)
        # print("join:\n%s" % result)
        return result


class XML(Template):
    def __init__(self):
        Template.__init__(self)

    def scaleColumn(self, scale, _key, value):
        # print("key: %s, value: %s" % (key, value))
        out = value
        if "," in value:
            numbers = value.split(",")
            out = f"{scaleValue(scale, numbers[0])},{scaleValue(scale, numbers[1])},{numbers[2]},{numbers[3]},{scaleValue(scale, numbers[4])},{numbers[5]},{numbers[6]}"
        # print("scale_column: %s" % out)
        return out

    def scaleNumber(self, scale, key, value):
        # print("key: %s, value: %s" % (key, value))
        out = str(value)
        if key in key_values or any(key.endswith(end) for end in ending_values):
            if "," in value:
                numbers = value.split(",")
                out = f"{scaleValue(scale, numbers[0])},{scaleValue(scale, numbers[1])}"
            elif ";" in value:
                numbers = value.split(";")
                out = f"{numbers[0]};{scaleValue(scale, numbers[1])}"
            else:
                out = scaleValue(scale, value)
        # print("out: %s" % out)
        return str(out)

    def scalePointer(self, scale, pointer):
        # print("scale_pointer: %s, %s" % (scale, pointer))
        elems = pointer.split(":")
        pic = elems[0]
        dims = elems[1]
        dim = dims.split(",")
        pointer = f"{pic}:{scaleValue(scale, dim[0])},{scaleValue(scale, dim[1])}"
        # print(pointer)
        return pointer

    def scaleElement(self, scale, node):
        if isinstance(node, Comment):
            return
        # A <convert type="TemplatedMultiContent"> only ever appears inside
        # a <widget> in this dialect, so its own tag+type is enough to spot
        # it without needing to check the parent too.
        if node.tag == "convert" and node.attrs.get("type") == "TemplatedMultiContent":
            if node.text:
                node.text = self.scaleTemplate(scale, node.text)
        else:
            for key in node.attrs:
                if key.startswith("column"):
                    node.attrs[key] = self.scaleColumn(scale, key, node.attrs[key])
                elif key == "pointer":
                    node.attrs[key] = self.scalePointer(scale, node.attrs[key])
                elif key in {"backgroundPixmap", "selectionPixmap"}:
                    pass
                else:
                    node.attrs[key] = self.scaleNumber(scale, key, node.attrs[key])
            # Reorder attributes alphabetically for consistent output -
            # renderNode() (xmlinc.py) renders in dict order, so sorting
            # the dict itself is all that's needed, no post-hoc string pass.
            node.attrs = dict(sorted(node.attrs.items()))
        if node.children:
            for child in node.children:
                self.scaleElement(scale, child)

    def processFile(self, scale, src, dst):
        # print("process_file: scale: %s, src: %s, dst: %s" % (scale, src, dst))
        root = XmlParser(readFile(src)).parseDocument()
        # A .xmlinc fragment can have more than one top-level element -
        # parseDocument() already returns a plain list for that case, no
        # synthetic <root> wrapper (and matching unwrap afterward) needed.
        nodes = root if isinstance(root, list) else [root]

        for node in nodes:
            self.scaleElement(scale, node)

        lines = []
        for node in nodes:
            renderNode(node, lines)
        xml_string = "\n".join(lines) + "\n"
        writeFile(dst, xml_string)


def parseArgs(argv):
    parser = argparse.ArgumentParser(prog="xmlscale.py")
    parser.add_argument("-s", dest="scale", required=True, help="scaling factor")
    parser.add_argument("-i", dest="src", required=True, help="source file")
    parser.add_argument("-o", dest="dst", help="destination file (defaults to source)")
    return parser.parse_args(argv)


def scaleSkin(argv):
    args = parseArgs(argv)
    scale = args.scale
    src = os.path.normpath(args.src)
    dst = os.path.normpath(args.dst) if args.dst else src

    print("processing skin...")
    scale = float(Fraction(scale))
    print('scale: ' + str(scale))
    print('src: ' + src)
    print('dst: ' + dst)

    if os.path.splitext(src)[1] == ".tpl":
        writeFile(dst, Template().scaleTemplate(scale, readFile(src)))
    else:
        XML().processFile(scale, src, dst)

    # print("xmlscale done.")


if __name__ == "__main__":
    scaleSkin(sys.argv[1:])
