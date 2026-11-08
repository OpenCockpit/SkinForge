# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


import os
import sys
import argparse
import xml.etree.ElementTree as ET
from FileUtils import readFile, writeFile


def render_xmlinc_ref(node):
    # Already a reference to an external file, not inline content to
    # extract - preserve it exactly as written (file=, position=, any other
    # attribute) rather than trying to "split" it. An xmlinc reference has
    # a "file" attribute, never a "type"/"name" one, so this check has to
    # run wherever we're about to assume a child is an inline definition to
    # extract - not just among skin.xml's own top-level children, but also
    # one level down inside <components>/<layouts>, since a shared
    # component or layout (e.g. one already living in Common) can just as
    # easily be an <xmlinc file="..."/> reference there instead of an
    # inline definition.
    attrs = "".join(f' {k}="{v}"' for k, v in node.attrib.items())
    return f'<xmlinc{attrs}/>'


def save_element_to_file(element, filename, srcdir):
    # No pretty-printing here - xmlpretty.py already does this far more
    # thoroughly (comma-spacing, <convert>-block reformatting, position=/
    # size= tightening, ...) than this tool's own old minidom-based pass
    # ever did; the wrapper script runs the real xmlpretty on every file
    # this generates afterward instead.
    filepath = os.path.join(srcdir, filename + ".xmlinc")
    tree = ET.ElementTree(element)
    tree.write(filepath)
    print(f"GENERATED: {filepath}")


def xmlsplit(src):
    srcdir = os.path.dirname(src)
    xml_string = readFile(src)
    tree = ET.ElementTree(ET.fromstring(xml_string))
    root = tree.getroot()
    skinlines = ["<skin>"]
    for node in root:
        print(node.tag)
        if node.tag == "xmlinc":
            skinlines.append(render_xmlinc_ref(node))
        elif node.tag == "components":
            skinlines.append("<components>")
            for comp in node:
                if comp.tag == "xmlinc":
                    skinlines.append(render_xmlinc_ref(comp))
                    continue
                filename = comp.tag + "_" + comp.attrib["type"]
                save_element_to_file(comp, filename, srcdir)
                skinlines.append(f'<xmlinc file="{filename}.xmlinc"/>')
            skinlines.append("</components>")
        elif node.tag == "layouts":
            skinlines.append("<layouts>")
            for comp in node:
                if comp.tag == "xmlinc":
                    skinlines.append(render_xmlinc_ref(comp))
                    continue
                filename = comp.tag + "_" + comp.attrib["name"]
                save_element_to_file(comp, filename, srcdir)
                skinlines.append(f'<xmlinc file="{filename}.xmlinc"/>')
            skinlines.append("</layouts>")
        elif node.tag in {"windowstyle", "windowstylescrollbar"}:
            filename = node.tag + "_" + node.attrib["id"]
            save_element_to_file(node, filename, srcdir)
            skinlines.append(f'<xmlinc file="{filename}.xmlinc"/>')
        else:
            if "name" in node.attrib:
                filename = node.tag + "_" + node.attrib["name"]
            else:
                filename = node.tag
            save_element_to_file(node, filename, srcdir)
            skinlines.append(f'<xmlinc file="{filename}.xmlinc"/>')
    skinlines.append("</skin>")
    skin = "\n".join(skinlines)
    writeFile(src, skin)
    print(f"GENERATED: {src}")


def parseArgs(argv):
    parser = argparse.ArgumentParser(prog="xmlsplit.py")
    parser.add_argument("-i", dest="src", required=True, help="source file")
    return parser.parse_args(argv)


def main(argv):
    args = parseArgs(argv)
    print("src: " + args.src)
    xmlsplit(args.src)


if __name__ == "__main__":
    main(sys.argv[1:])
    sys.exit()
