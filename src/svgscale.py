# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


import os
import subprocess
import sys
import argparse
import xml.etree.ElementTree as ET
from Version import VERSION


def get_width(filename):
    print(f"get_width: {filename}")
    with open(filename, 'r', encoding='utf-8') as svg_file:
        tree = ET.parse(svg_file)
    root = tree.getroot()
    width = root.get('width')
    try:
        float(width)
    except Exception:
        width = 0
    print(f"get_width: {width}")
    return width


def scale_file(width, src, dst):
    subprocess.run(["rsvg-convert", "-f", "svg", "-w", str(width), src, "-o", dst], check=True)


def process_file(scale, src, dst):
    width = get_width(src)
    if width:
        width = int(float(scale) * float(width))
        scale_file(width, src, dst)
    else:
        print(f"process_file: no width in {src}")


def parseArgs(argv):
    parser = argparse.ArgumentParser(prog="svgscale.py")
    parser.add_argument("-s", "--scale", dest="scale", required=True, help="scaling factor")
    parser.add_argument("-i", "--src", dest="src", required=True, help="source file")
    parser.add_argument("-o", "--dst", dest="dst", help="destination file (defaults to source)")
    return parser.parse_args(argv)


def scale_svg(argv):
    print(f"svgscale {VERSION}")
    args = parseArgs(argv)
    scale = args.scale
    src = os.path.normpath(args.src)
    dst = os.path.normpath(args.dst) if args.dst else src

    print("scaling svg...")
    print('scale: ' + scale)
    print('src: ' + src)
    print('dst: ' + dst)

    process_file(scale, src, dst)

    # print("svgscale done.")


if __name__ == "__main__":
    scale_svg(sys.argv[1:])
