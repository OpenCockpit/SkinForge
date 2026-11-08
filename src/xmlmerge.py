# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


import os
import sys
import argparse
import xml.etree.ElementTree as ET


def merge_files(src1, src2, dst):
    tree1 = ET.parse(src1)
    root1 = tree1.getroot()

    tree2 = ET.parse(src2)
    root2 = tree2.getroot()

    root1.extend(root2)

    tree1.write(dst)


def parseArgs(argv):
    parser = argparse.ArgumentParser(prog="xmlmerge.py")
    parser.add_argument("-i", "--src1", dest="src1", required=True, help="first source file")
    parser.add_argument("-j", "--src2", dest="src2", required=True, help="second source file")
    parser.add_argument("-o", "--dst", dest="dst", required=True, help="destination file")
    return parser.parse_args(argv)


def xmlmerge(argv):
    args = parseArgs(argv)
    src1 = os.path.normpath(args.src1)
    src2 = os.path.normpath(args.src2)
    dst = os.path.normpath(args.dst)

    print("merging skins...")
    print('src1: ' + src1)
    print('src2: ' + src2)
    print('dst: ' + dst)

    merge_files(src1, src2, dst)

    # print("xmlmerge done.")


if __name__ == "__main__":
    xmlmerge(sys.argv[1:])
