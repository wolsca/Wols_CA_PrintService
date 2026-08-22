import os
from pypdf import PdfReader, PdfWriter, PageObject, Transformation
import config

def validate_pdf(input_pdf_path):
    """Fails fast with a clear message on encrypted, empty or corrupt PDFs."""
    try:
        reader = PdfReader(input_pdf_path)
    except Exception as e:
        raise ValueError(f"Corrupt or unreadable PDF file: {e}")

    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                raise ValueError("This PDF is encrypted and cannot be printed.")
        except ValueError:
            raise
        except Exception:
            raise ValueError("This PDF is encrypted and cannot be printed.")

    try:
        page_count = len(reader.pages)
    except Exception as e:
        raise ValueError(f"Corrupt or unreadable PDF file: {e}")

    if page_count == 0:
        raise ValueError("The PDF has no pages.")
    return page_count

def generate_booklet_pdfs(input_pdf_path):
    """Imposes pages into an A5 booklet format split over front and back sheets."""
    try:
        reader = PdfReader(input_pdf_path)
        page_count = len(reader.pages)

        a4_width, a4_height = 842.0, 595.0
        a5_width, a5_height = a4_width / 2.0, a4_height

        total_booklet_pages = ((page_count + 3) // 4) * 4
        total_sheets = total_booklet_pages // 4

        front_writer = PdfWriter()
        back_writer = PdfWriter()

        for sheet in range(total_sheets):
            left_page_front_idx = total_booklet_pages - 1 - (2 * sheet)
            right_page_front_idx = 2 * sheet
            left_page_back_idx = (2 * sheet) + 1
            right_page_back_idx = total_booklet_pages - 2 - (2 * sheet)

            # Create Front Sheet
            front_sheet = PageObject.create_blank_page(width=a4_width, height=a4_height)
            if left_page_front_idx < page_count:
                lp_f = reader.pages[left_page_front_idx]
                scale = min(a5_width / float(lp_f.mediabox.width), a5_height / float(lp_f.mediabox.height))
                lp_f.add_transformation(Transformation().scale(scale, scale))
                front_sheet.merge_page(lp_f)

            if right_page_front_idx < page_count:
                rp_f = reader.pages[right_page_front_idx]
                scale = min(a5_width / float(rp_f.mediabox.width), a5_height / float(rp_f.mediabox.height))
                rp_f.add_transformation(Transformation().scale(scale, scale).translate(tx=a5_width, ty=0))
                front_sheet.merge_page(rp_f)
            front_writer.add_page(front_sheet)

            # Create Back Sheet
            back_sheet = PageObject.create_blank_page(width=a4_width, height=a4_height)
            if left_page_back_idx < page_count:
                lp_b = reader.pages[left_page_back_idx]
                scale = min(a5_width / float(lp_b.mediabox.width), a5_height / float(lp_b.mediabox.height))
                lp_b.add_transformation(Transformation().scale(scale, scale))
                back_sheet.merge_page(lp_b)

            if right_page_back_idx < page_count:
                rp_b = reader.pages[right_page_back_idx]
                scale = min(a5_width / float(rp_b.mediabox.width), a5_height / float(rp_b.mediabox.height))
                rp_b.add_transformation(Transformation().scale(scale, scale).translate(tx=a5_width, ty=0))
                back_sheet.merge_page(rp_b)
            back_writer.add_page(back_sheet)

        base_name = os.path.basename(input_pdf_path)
        front_pdf_path = os.path.join(config.TEMP_DIR, f"front_{base_name}")
        back_pdf_path = os.path.join(config.TEMP_DIR, f"back_{base_name}")

        with open(front_pdf_path, "wb") as f_out:
            front_writer.write(f_out)
        with open(back_pdf_path, "wb") as b_out:
            back_writer.write(b_out)

        return front_pdf_path, back_pdf_path, page_count

    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Corrupt or unreadable PDF file: {str(e)}")

def generate_duplex_booklet_pdf(front_pdf_path, back_pdf_path, base_name):
    """Interleaves the imposed sides so a duplex printer needs no manual flip."""
    front_reader = PdfReader(front_pdf_path)
    back_reader = PdfReader(back_pdf_path)
    writer = PdfWriter()
    for index in range(len(front_reader.pages)):
        writer.add_page(front_reader.pages[index])
        if index < len(back_reader.pages):
            writer.add_page(back_reader.pages[index])

    duplex_path = os.path.join(config.TEMP_DIR, f"duplex_{base_name}")
    with open(duplex_path, "wb") as out:
        writer.write(out)
    return duplex_path

def generate_blank_front_pdf(input_pdf_path):
    """Puts a blank front side in front of the document.

    Used for a single page in DoubleSided mode: the printer prints the blank
    front, asks to put the sheet back and only then prints the page. That pause
    is what makes it possible to load special paper for exactly this one page,
    without the risk of another job using it.
    """
    reader = PdfReader(input_pdf_path)
    first = reader.pages[0]
    writer = PdfWriter()
    writer.add_page(PageObject.create_blank_page(width=float(first.mediabox.width),
                                                height=float(first.mediabox.height)))
    for page in reader.pages:
        writer.add_page(page)

    path = os.path.join(config.TEMP_DIR, f"blankfront_{os.path.basename(input_pdf_path)}")
    with open(path, "wb") as out:
        writer.write(out)
    return path

def generate_two_sided_pdfs(input_pdf_path):
    """
    Splits a document into odd and even pages for the 'Duplex' mode on a printer
    without a duplex unit.
    """
    reader = PdfReader(input_pdf_path)
    page_count = len(reader.pages)
    base_name = os.path.basename(input_pdf_path)

    front_writer = PdfWriter()
    back_writer = PdfWriter()
    for index in range(page_count):
        (front_writer if index % 2 == 0 else back_writer).add_page(reader.pages[index])

    front_path = os.path.join(config.TEMP_DIR, f"front_{base_name}")
    back_path = os.path.join(config.TEMP_DIR, f"back_{base_name}")
    with open(front_path, "wb") as out:
        front_writer.write(out)
    with open(back_path, "wb") as out:
        back_writer.write(out)
    return front_path, back_path, page_count