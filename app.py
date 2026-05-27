from datetime import datetime
from copy import copy
from io import BytesIO
import os
import re
import time
import zipfile

from flask import Flask, jsonify, redirect, render_template, request, send_file, send_from_directory, url_for
try:
    from pypdf import PdfReader, PdfWriter, Transformation
    from pypdf._page import PageObject
    from PyPDF2 import PdfMerger
except ImportError:
    from PyPDF2 import PdfMerger, PdfReader, PdfWriter, Transformation
    from PyPDF2._page import PageObject
from reportlab.pdfgen import canvas
from werkzeug.utils import secure_filename
from pdf2image import convert_from_path
from PIL import Image


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
ALLOWED_MODES = {"normal", "duplex"}
ALLOWED_GRID_SIZES = {1, 2, 3, 4}
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
LAYOUT_DPI = 72
A4_WIDTH = 595.275590551
A4_HEIGHT = 841.88976378
GENERATED_FILE_PATTERN = re.compile(r"\d{8}_\d{4,6}")
AUTO_CLEANUP_SECONDS = int(os.environ.get("AUTO_CLEANUP_SECONDS", 24 * 60 * 60))
CLEANUP_INTERVAL_SECONDS = 60 * 60
LAST_CLEANUP = 0

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)


def is_pdf_filename(filename):
    return filename.lower().endswith(".pdf")


def is_image_filename(filename):
    return os.path.splitext(filename.lower())[1] in ALLOWED_IMAGE_EXTENSIONS


def timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def timestamped_filename(filename, stamp):
    safe_name = secure_filename(filename)
    base_name, extension = os.path.splitext(safe_name)

    if not base_name:
        base_name = "uploaded_file"

    return base_name, extension.lower(), f"{base_name}_{stamp}{extension.lower()}"


def save_upload(file, expected_type="pdf", suffix=None):
    if not file or file.filename == "":
        raise ValueError("No file selected")

    if expected_type == "pdf" and not is_pdf_filename(file.filename):
        raise ValueError("Only PDF files allowed")

    if expected_type == "image" and not is_image_filename(file.filename):
        raise ValueError("Only image files allowed")

    stamp = timestamp() if suffix is None else f"{timestamp()}_{suffix}"
    base_name, extension, filename = timestamped_filename(file.filename, stamp)
    input_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(input_path)

    return input_path, base_name, extension


def get_poppler_path():
    configured_path = os.environ.get("POPPLER_PATH")
    if configured_path:
        return configured_path

    windows_path = r"C:\poppler\Library\bin"
    if os.name == "nt" and os.path.isdir(windows_path):
        return windows_path

    return None


def pdf_convert_options(dpi=150):
    options = {"dpi": dpi}
    poppler_path = get_poppler_path()
    if poppler_path:
        options["poppler_path"] = poppler_path
    return options


def parse_bool(value, default=True):
    if value is None:
        return default
    return str(value).lower() in {"1", "true", "yes", "on"}


def parse_int(value, default, minimum, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default

    return max(minimum, min(maximum, number))


def measurement_to_pixels(value, unit, default_px, minimum_px, maximum_px):
    if value is None or str(value).strip() == "":
        return default_px

    try:
        amount = float(value)
    except ValueError as exc:
        raise ValueError("Margin and gap must be numbers") from exc

    if amount < 0:
        raise ValueError("Margin and gap cannot be negative")

    normalized_unit = (unit or "cm").strip().lower()

    if normalized_unit in {"cm", "centimeter", "centimeters"}:
        pixels = amount * LAYOUT_DPI / 2.54
    elif normalized_unit in {"in", "inch", "inches"}:
        pixels = amount * LAYOUT_DPI
    elif normalized_unit in {"px", "pixel", "pixels"}:
        pixels = amount
    else:
        raise ValueError("Unit must be cm or inch")

    return max(minimum_px, min(maximum_px, pixels))


def get_page_size(page):
    box = page.cropbox
    return float(box.width), float(box.height)


def fit_page_transform(page, x, y, width, height):
    page_width, page_height = get_page_size(page)
    if page_width <= 0 or page_height <= 0:
        raise ValueError("Invalid source page size")

    scale = min(width / page_width, height / page_height)
    fitted_width = page_width * scale
    fitted_height = page_height * scale
    tx = x + ((width - fitted_width) / 2)
    ty = y + ((height - fitted_height) / 2)

    return Transformation().scale(scale).translate(tx, ty)


def add_page_to_sheet(sheet, source_page, transform, writer):
    page_copy = copy(source_page)
    page_copy.add_transformation(transform)
    sheet.merge_page(page_copy)


def build_marks_overlay(cells, show_border, show_page_numbers):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(A4_WIDTH, A4_HEIGHT))

    if show_border:
        pdf.setStrokeColorRGB(0, 0, 0)
        pdf.setLineWidth(1)

    if show_page_numbers:
        pdf.setFillColorRGB(1, 0, 0)
        pdf.setFont("Helvetica-Bold", 6)

    for cell in cells:
        x, y, width, height, page_number = cell

        if show_border:
            pdf.rect(x, y, width, height, stroke=1, fill=0)

        if show_page_numbers:
            pdf.drawString(x + 3, y + height - 7, str(page_number))

    pdf.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def grid_position(index, grid_size, mirror_columns=False):
    row = index // grid_size
    col = index % grid_size

    if mirror_columns:
        col = grid_size - 1 - col

    return row, col


def parse_page_range(page_range, total_pages):
    if not page_range or not page_range.strip():
        return list(range(total_pages))

    selected = []
    seen = set()

    for raw_part in page_range.split(","):
        part = raw_part.strip()
        if not part:
            continue

        if "-" in part:
            raw_start, raw_end = part.split("-", 1)
            start = int(raw_start) if raw_start.strip() else 1
            end = int(raw_end) if raw_end.strip() else total_pages
        else:
            start = end = int(part)

        if start < 1 or end < 1 or start > total_pages or end > total_pages or start > end:
            raise ValueError(f"Invalid page range: {part}")

        for page_number in range(start, end + 1):
            index = page_number - 1
            if index not in seen:
                selected.append(index)
                seen.add(index)

    if not selected:
        raise ValueError("No pages selected")

    return selected


def output_path_for(filename):
    return os.path.join(OUTPUT_FOLDER, filename)


def send_output(path, filename, mimetype):
    return send_file(
        path,
        as_attachment=True,
        download_name=filename,
        mimetype=mimetype
    )


def cleanup_folder(folder, max_age_seconds):
    now = time.time()
    removed = 0

    for filename in os.listdir(folder):
        if filename == ".gitkeep":
            continue

        if not GENERATED_FILE_PATTERN.search(filename):
            continue

        path = os.path.join(folder, filename)
        if not os.path.isfile(path):
            continue

        if now - os.path.getmtime(path) > max_age_seconds:
            os.remove(path)
            removed += 1

    return removed


def run_auto_cleanup():
    global LAST_CLEANUP

    now = time.time()
    if now - LAST_CLEANUP < CLEANUP_INTERVAL_SECONDS:
        return

    LAST_CLEANUP = now
    cleanup_folder(UPLOAD_FOLDER, AUTO_CLEANUP_SECONDS)
    cleanup_folder(OUTPUT_FOLDER, AUTO_CLEANUP_SECONDS)


@app.before_request
def before_request_cleanup():
    run_auto_cleanup()


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/convert-page")
def convert_page():
    return render_template("convert.html")


@app.route("/batch-page")
def batch_page():
    return render_template("batch.html")


@app.route("/merge-page")
def merge_page():
    return render_template("merge.html")


@app.route("/split-page")
def split_page():
    return render_template("split.html")


@app.route("/compress-page")
def compress_page():
    return render_template("compress.html")


@app.route("/pdf-to-images-page")
def pdf_to_images_page():
    return render_template("pdf_to_images.html")


@app.route("/images-to-pdf-page")
def images_to_pdf_page():
    return render_template("images_to_pdf.html")


@app.route("/protect-page")
def protect_page():
    return render_template("protect.html")


@app.route("/recent-page")
def recent_page():
    return render_template("recent.html")


@app.route("/cleanup-page")
def cleanup_page():
    return render_template("cleanup.html")


@app.route("/download/<path:filename>")
def download_output(filename):
    safe_name = secure_filename(filename)
    return send_from_directory(
        OUTPUT_FOLDER,
        safe_name,
        as_attachment=True,
        download_name=safe_name
    )


@app.route("/recent")
def recent_outputs():
    files = []

    for filename in os.listdir(OUTPUT_FOLDER):
        path = os.path.join(OUTPUT_FOLDER, filename)
        if filename == ".gitkeep" or not os.path.isfile(path):
            continue

        files.append({
            "name": filename,
            "size": os.path.getsize(path),
            "created": datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S"),
            "url": url_for("download_output", filename=filename)
        })

    files.sort(key=lambda item: item["created"], reverse=True)
    return jsonify(files[:20])


@app.route("/cleanup", methods=["POST"])
def cleanup_outputs():
    hours = parse_int(request.form.get("hours"), 24, 1, 24 * 30)
    max_age_seconds = hours * 60 * 60
    removed = cleanup_folder(UPLOAD_FOLDER, max_age_seconds)
    removed += cleanup_folder(OUTPUT_FOLDER, max_age_seconds)
    return jsonify({"removed": removed})


@app.route("/convert", methods=["POST"])
def convert_pdf():
    try:
        if "file" not in request.files:
            return "No file uploaded", 400

        mode, grid_size, options = get_9up_options()
        file = request.files["file"]
        input_path, base_name, _ = save_upload(file, "pdf")

        stamp = timestamp()
        output_filename = f"{base_name}_{mode}_{grid_size}x{grid_size}_{stamp}.pdf"
        output_path = output_path_for(output_filename)

        create_9up_pdf(input_path, output_path, grid_size, mode, **options)

        return send_output(output_path, output_filename, "application/pdf")

    except ValueError as e:
        return str(e), 400
    except Exception as e:
        return f"Error: {str(e)}", 500


@app.route("/preview-convert", methods=["POST"])
def preview_convert_pdf():
    try:
        if "file" not in request.files:
            return "No file uploaded", 400

        mode, grid_size, options = get_9up_options()
        file = request.files["file"]
        input_path, base_name, _ = save_upload(file, "pdf", "preview")

        stamp = timestamp()
        preview_pdf = output_path_for(f"{base_name}_{mode}_{grid_size}x{grid_size}_preview_{stamp}.pdf")
        preview_png_name = f"{base_name}_{mode}_{grid_size}x{grid_size}_preview_{stamp}.png"
        preview_png = output_path_for(preview_png_name)

        create_9up_pdf(input_path, preview_pdf, grid_size, mode, **options)
        preview_images = convert_from_path(
            preview_pdf,
            first_page=1,
            last_page=1,
            **pdf_convert_options(dpi=150)
        )

        if not preview_images:
            raise ValueError("Preview create nahi hua")

        preview_images[0].save(preview_png, "PNG")
        return send_file(preview_png, mimetype="image/png")

    except ValueError as e:
        return str(e), 400
    except Exception as e:
        return f"Error: {str(e)}", 500


@app.route("/batch-convert", methods=["POST"])
def batch_convert_pdf():
    try:
        files = request.files.getlist("files")
        if not files:
            return "No files uploaded", 400

        mode, grid_size, options = get_9up_options()
        stamp = timestamp()
        zip_filename = f"batch_9up_{mode}_{grid_size}x{grid_size}_{stamp}.zip"
        zip_path = output_path_for(zip_filename)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
            converted = 0

            for index, file in enumerate(files, start=1):
                if file.filename == "":
                    continue

                input_path, base_name, _ = save_upload(file, "pdf", f"batch_{index}")
                output_filename = f"{base_name}_{mode}_{grid_size}x{grid_size}_{stamp}.pdf"
                output_path = output_path_for(output_filename)
                create_9up_pdf(input_path, output_path, grid_size, mode, **options)
                zip_file.write(output_path, output_filename)
                converted += 1

        if converted == 0:
            return "No valid PDF files uploaded", 400

        return send_output(zip_path, zip_filename, "application/zip")

    except ValueError as e:
        return str(e), 400
    except Exception as e:
        return f"Error: {str(e)}", 500


@app.route("/merge", methods=["POST"])
def merge_pdf():
    if "files" not in request.files:
        return "No files uploaded", 400

    files = request.files.getlist("files")

    if len(files) < 2:
        return "Please upload at least 2 PDF files", 400

    merger = PdfMerger()
    stamp = timestamp()
    output_filename = f"merged_pdf_{stamp}.pdf"
    output_path = output_path_for(output_filename)

    try:
        saved_paths = []

        for index, file in enumerate(files, start=1):
            if file.filename == "":
                continue

            input_path, _, _ = save_upload(file, "pdf", index)
            saved_paths.append(input_path)

        if len(saved_paths) < 2:
            return "Please upload at least 2 valid PDF files", 400

        for input_path in saved_paths:
            merger.append(input_path)

        merger.write(output_path)

        return send_output(output_path, output_filename, "application/pdf")

    except ValueError as e:
        return str(e), 400
    except Exception as e:
        return f"Error: {str(e)}", 500
    finally:
        merger.close()


@app.route("/split", methods=["POST"])
def split_pdf():
    try:
        if "file" not in request.files:
            return "No file uploaded", 400

        file = request.files["file"]
        page_range = request.form.get("page_range", "")
        input_path, base_name, _ = save_upload(file, "pdf")

        reader = PdfReader(input_path)
        pages = parse_page_range(page_range, len(reader.pages))
        writer = PdfWriter()

        for page_index in pages:
            writer.add_page(reader.pages[page_index])

        output_filename = f"{base_name}_split_{timestamp()}.pdf"
        output_path = output_path_for(output_filename)

        with open(output_path, "wb") as output_file:
            writer.write(output_file)

        return send_output(output_path, output_filename, "application/pdf")

    except ValueError as e:
        return str(e), 400
    except Exception as e:
        return f"Error: {str(e)}", 500


@app.route("/compress", methods=["POST"])
def compress_pdf():
    try:
        if "file" not in request.files:
            return "No file uploaded", 400

        file = request.files["file"]
        input_path, base_name, _ = save_upload(file, "pdf")

        reader = PdfReader(input_path)
        writer = PdfWriter()

        for page in reader.pages:
            writer.add_page(page)
            output_page = writer.pages[-1]
            if hasattr(output_page, "compress_content_streams"):
                output_page.compress_content_streams()

        output_filename = f"{base_name}_compressed_{timestamp()}.pdf"
        output_path = output_path_for(output_filename)

        with open(output_path, "wb") as output_file:
            writer.write(output_file)

        return send_output(output_path, output_filename, "application/pdf")

    except ValueError as e:
        return str(e), 400
    except Exception as e:
        return f"Error: {str(e)}", 500


@app.route("/pdf-to-images", methods=["POST"])
def pdf_to_images():
    try:
        if "file" not in request.files:
            return "No file uploaded", 400

        file = request.files["file"]
        image_format = request.form.get("image_format", "png").lower()
        if image_format not in {"png", "jpg"}:
            return "Image format must be png or jpg", 400

        page_range = request.form.get("page_range", "")
        input_path, base_name, _ = save_upload(file, "pdf")
        reader = PdfReader(input_path)
        selected_pages = parse_page_range(page_range, len(reader.pages))

        images = convert_from_path(input_path, **pdf_convert_options(dpi=150))
        stamp = timestamp()
        zip_filename = f"{base_name}_images_{stamp}.zip"
        zip_path = output_path_for(zip_filename)
        pil_format = "JPEG" if image_format == "jpg" else "PNG"

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for page_index in selected_pages:
                image = images[page_index]
                if image_format == "jpg":
                    image = image.convert("RGB")

                image_filename = f"{base_name}_page_{page_index + 1}.{image_format}"
                image_path = output_path_for(f"{base_name}_{stamp}_page_{page_index + 1}.{image_format}")
                image.save(image_path, pil_format)
                zip_file.write(image_path, image_filename)

        return send_output(zip_path, zip_filename, "application/zip")

    except ValueError as e:
        return str(e), 400
    except Exception as e:
        return f"Error: {str(e)}", 500


@app.route("/images-to-pdf", methods=["POST"])
def images_to_pdf():
    try:
        files = request.files.getlist("files")
        if not files:
            return "No images uploaded", 400

        images = []
        base_name = "images"

        for index, file in enumerate(files, start=1):
            if file.filename == "":
                continue

            input_path, uploaded_base_name, _ = save_upload(file, "image", index)
            if index == 1:
                base_name = uploaded_base_name

            image = Image.open(input_path).convert("RGB")
            image.load()
            images.append(image)

        if not images:
            return "No valid images uploaded", 400

        output_filename = f"{base_name}_images_pdf_{timestamp()}.pdf"
        output_path = output_path_for(output_filename)
        images[0].save(output_path, save_all=True, append_images=images[1:])

        return send_output(output_path, output_filename, "application/pdf")

    except ValueError as e:
        return str(e), 400
    except Exception as e:
        return f"Error: {str(e)}", 500


@app.route("/protect", methods=["POST"])
def protect_pdf():
    try:
        if "file" not in request.files:
            return "No file uploaded", 400

        password = request.form.get("password", "").strip()
        if not password:
            return "Password required", 400

        file = request.files["file"]
        input_path, base_name, _ = save_upload(file, "pdf")

        reader = PdfReader(input_path)
        writer = PdfWriter()

        for page in reader.pages:
            writer.add_page(page)

        writer.encrypt(password)

        output_filename = f"{base_name}_protected_{timestamp()}.pdf"
        output_path = output_path_for(output_filename)

        with open(output_path, "wb") as output_file:
            writer.write(output_file)

        return send_output(output_path, output_filename, "application/pdf")

    except ValueError as e:
        return str(e), 400
    except Exception as e:
        return f"Error: {str(e)}", 500


def get_9up_options():
    mode = request.form.get("mode", "duplex")
    if mode not in ALLOWED_MODES:
        raise ValueError("Invalid printing mode")

    try:
        grid_size = int(request.form.get("grid_size", 3))
    except ValueError as exc:
        raise ValueError("Invalid grid size") from exc

    if grid_size not in ALLOWED_GRID_SIZES:
        raise ValueError("Grid size must be 1, 2, 3, or 4")

    if request.form.get("margin_value") is None:
        margin = parse_int(request.form.get("margin"), 25, 0, 600)
    else:
        margin = measurement_to_pixels(
            request.form.get("margin_value"),
            request.form.get("margin_unit", "cm"),
            25,
            0,
            600
        )

    if request.form.get("gap_value") is None:
        border = parse_int(request.form.get("border"), 12, 0, 300)
    else:
        border = measurement_to_pixels(
            request.form.get("gap_value"),
            request.form.get("gap_unit", "cm"),
            12,
            0,
            300
        )

    options = {
        "page_range": request.form.get("page_range", ""),
        "show_page_numbers": parse_bool(request.form.get("page_numbers"), True),
        "show_border": parse_bool(request.form.get("borders"), True),
        "margin": margin,
        "border": border
    }

    return mode, grid_size, options


def create_9up_pdf(
    input_path,
    output_path,
    grid_size=3,
    mode="duplex",
    page_range="",
    show_page_numbers=True,
    show_border=True,
    margin=25,
    border=12
):
    reader = PdfReader(input_path)
    if not reader.pages:
        raise Exception("PDF pages load nahi hui")

    selected_indices = parse_page_range(page_range, len(reader.pages))
    selected_pages = [(index + 1, reader.pages[index]) for index in selected_indices]

    cell_width = (A4_WIDTH - (2 * margin)) / grid_size
    cell_height = (A4_HEIGHT - (2 * margin)) / grid_size

    if cell_width <= border * 2 or cell_height <= border * 2:
        raise ValueError("Margin or border is too large for this grid")

    writer = PdfWriter()

    if mode == "normal":
        for i in range(0, len(selected_pages), grid_size * grid_size):
            sheet = PageObject.create_blank_page(width=A4_WIDTH, height=A4_HEIGHT)
            batch = selected_pages[i:i + (grid_size * grid_size)]
            marks = []

            for idx, (page_number, page) in enumerate(batch):
                row, col = grid_position(idx, grid_size)

                x = margin + (col * cell_width)
                y = A4_HEIGHT - margin - ((row + 1) * cell_height)
                content_x = x + border
                content_y = y + border
                content_width = cell_width - (border * 2)
                content_height = cell_height - (border * 2)

                transform = fit_page_transform(
                    page,
                    content_x,
                    content_y,
                    content_width,
                    content_height
                )
                add_page_to_sheet(sheet, page, transform, writer)
                marks.append((x, y, cell_width, cell_height, page_number))

            if show_border or show_page_numbers:
                sheet.merge_page(build_marks_overlay(marks, show_border, show_page_numbers))

            writer.add_page(sheet)

    else:
        odd_pages = [page for page in selected_pages if page[0] % 2 == 1]
        even_pages = [page for page in selected_pages if page[0] % 2 == 0]

        max_pages = max(len(odd_pages), len(even_pages))

        for i in range(0, max_pages, grid_size * grid_size):
            for is_odd in [True, False]:
                batch = (
                    odd_pages[i:i + grid_size * grid_size]
                    if is_odd
                    else even_pages[i:i + grid_size * grid_size]
                )

                if not batch:
                    continue

                sheet = PageObject.create_blank_page(width=A4_WIDTH, height=A4_HEIGHT)
                marks = []

                for idx, (page_number, page) in enumerate(batch):
                    row, col = grid_position(
                        idx,
                        grid_size,
                        mirror_columns=not is_odd
                    )

                    x = margin + (col * cell_width)
                    y = A4_HEIGHT - margin - ((row + 1) * cell_height)
                    content_x = x + border
                    content_y = y + border
                    content_width = cell_width - (border * 2)
                    content_height = cell_height - (border * 2)

                    transform = fit_page_transform(
                        page,
                        content_x,
                        content_y,
                        content_width,
                        content_height
                    )
                    add_page_to_sheet(sheet, page, transform, writer)
                    marks.append((x, y, cell_width, cell_height, page_number))

                if show_border or show_page_numbers:
                    sheet.merge_page(build_marks_overlay(marks, show_border, show_page_numbers))

                writer.add_page(sheet)

    if not writer.pages:
        raise Exception("Output PDF create nahi hua")

    with open(output_path, "wb") as output_file:
        writer.write(output_file)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
