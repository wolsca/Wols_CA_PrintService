#!/usr/bin/env python3
"""Creates the test document for the automated print test.

A booklet only shows its real behaviour with more than one page: three pages
means two sheets, a front side, a flip and a back side. This script writes a
plain multi-page A4 PDF without any dependency, so the pipeline never needs an
external file (pass one with -TestDocument if you have your own).

Usage:
    python tools/make_test_pdf.py build/printtest/in/booklet-test.pdf --pages 3
"""

import argparse
import os
import sys

PAGE_WIDTH = 595   # A4 in PostScript points
PAGE_HEIGHT = 842


def page_content(number, total, title):
    lines = [
        (120, 700, 28, f"{title}"),
        (120, 650, 18, f"Page {number} of {total}"),
        (120, 610, 12, "Automated print test of the Wols CA Print Service."),
        (120, 590, 12, "This page is written to a PDF file by the virtual printer;"),
        (120, 570, 12, "no paper is used."),
        (120, 300, 60, f"{number}"),
    ]
    parts = ["0 0 0 RG", "2 w", "40 40 515 762 re S"]
    for x, y, size, text in lines:
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        parts.append(f"BT /F1 {size} Tf {x} {y} Td ({escaped}) Tj ET")
    return "\n".join(parts)


def build_pdf(pages, title):
    objects = []          # 1-based list of object bodies
    page_ids = []

    # 1 = catalogue, 2 = page tree, 3 = font, then one page + one stream each.
    objects.append(None)  # catalogue, filled in below
    objects.append(None)  # page tree
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for index in range(1, pages + 1):
        stream = page_content(index, pages, title)
        content_id = len(objects) + 2
        page_id = len(objects) + 1
        page_ids.append(page_id)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>")
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[0] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>"

    out = ["%PDF-1.4\n"]
    offsets = []
    position = len(out[0])
    for number, body in enumerate(objects, start=1):
        chunk = f"{number} 0 obj\n{body}\nendobj\n"
        offsets.append(position)
        out.append(chunk)
        position += len(chunk)

    xref_position = position
    xref = [f"xref\n0 {len(objects) + 1}\n", "0000000000 65535 f \n"]
    for offset in offsets:
        xref.append(f"{offset:010d} 00000 n \n")
    out.extend(xref)
    out.append(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
               f"startxref\n{xref_position}\n%%EOF\n")
    return "".join(out).encode("latin-1")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write a multi-page test PDF.")
    parser.add_argument("output", help="path of the PDF to write")
    parser.add_argument("--pages", type=int, default=3, help="number of pages (default 3)")
    parser.add_argument("--title", default="Wols CA booklet test", help="title on every page")
    args = parser.parse_args(argv)

    if args.pages < 1:
        print("[test-pdf] --pages must be at least 1", file=sys.stderr)
        return 1

    directory = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(directory, exist_ok=True)
    with open(args.output, "wb") as handle:
        handle.write(build_pdf(args.pages, args.title))
    print(f"[test-pdf] {args.output} written with {args.pages} page(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
