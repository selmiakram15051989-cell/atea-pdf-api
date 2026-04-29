import io
import json
import re
import traceback
from http.server import BaseHTTPRequestHandler

import fitz  # PyMuPDF
import requests

# ---------------------------------------------------------------------------
# ATEA brand colours
# ---------------------------------------------------------------------------
ATEA_BLUE = (0 / 255, 74 / 255, 173 / 255)      # #004AAD
ATEA_YELLOW = (246 / 255, 178 / 255, 27 / 255)   # #F6B21B
WHITE = (1.0, 1.0, 1.0)
BLACK = (0.0, 0.0, 0.0)

LOGO_URL = "https://atea.corsica/wp-content/uploads/2026/01/LOGO-ATEA.png"

# SolarEdge → ATEA colour mapping (RGB 0-1 float, tolerance ±0.15)
COLOUR_MAP = [
    # Orange SolarEdge  →  Jaune ATEA
    {"src": (0.9, 0.4, 0.1), "dst": ATEA_YELLOW},
    # Bleu clair SolarEdge  →  Bleu ATEA
    {"src": (0.2, 0.5, 0.8), "dst": ATEA_BLUE},
]
COLOUR_TOL = 0.15

SOLAREDGE_REPLACEMENTS = [
    ("SolarEdge", "ATEA"),
    ("solaredge", "atea"),
    ("SOLAREDGE", "ATEA"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _colours_close(c1, c2, tol=COLOUR_TOL):
    """Return True when two RGB triples are within *tol* of each other."""
    if c1 is None or c2 is None:
        return False
    return all(abs(a - b) <= tol for a, b in zip(c1[:3], c2[:3]))


def _download_logo():
    """Download ATEA logo and return raw PNG bytes, or None on failure."""
    try:
        resp = requests.get(LOGO_URL, timeout=10)
        resp.raise_for_status()
        print(f"[logo] Downloaded ATEA logo ({len(resp.content)} bytes)")
        return resp.content
    except Exception as exc:
        print(f"[logo] WARNING – could not download logo: {exc}")
        return None


def _extract_client_info(first_page):
    """
    Attempt to extract client name, address and date from the first PDF page.
    Returns a formatted string, or an empty string if nothing useful is found.
    """
    text = first_page.get_text("text")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    client_name = ""
    address = ""
    date_str = ""

    # Look for a date pattern like DD/MM/YYYY or Month YYYY
    date_pattern = re.compile(
        r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{1,2}\s+\w+\s+\d{4})"
    )
    for line in lines:
        m = date_pattern.search(line)
        if m and not date_str:
            date_str = m.group(0)

    # Heuristic: the client name is often the first capitalised line that is
    # not a SolarEdge header keyword.
    skip_keywords = {"solaredge", "rapport", "designer", "site", "page", "atea"}
    for line in lines:
        if len(line) > 3 and not any(kw in line.lower() for kw in skip_keywords):
            if line[0].isupper() and not client_name:
                client_name = line
            elif client_name and not address and len(line) > 5:
                address = line
                break

    parts = [p for p in [client_name, address, date_str] if p]
    return "   |   ".join(parts)


def _recolour_drawings(page):
    """Replace SolarEdge brand colours with ATEA equivalents on *page*."""
    drawings = page.get_drawings()
    if not drawings:
        return

    for draw in drawings:
        changed = False

        new_color = draw.get("color")
        new_fill = draw.get("fill")

        for mapping in COLOUR_MAP:
            src, dst = mapping["src"], mapping["dst"]
            if _colours_close(draw.get("color"), src):
                new_color = dst
                changed = True
            if _colours_close(draw.get("fill"), src):
                new_fill = dst
                changed = True

        if not changed:
            continue

        # Re-draw the path with ATEA colours
        shape = page.new_shape()
        for item in draw.get("items", []):
            kind = item[0]
            if kind == "l":          # line
                shape.draw_line(item[1], item[2])
            elif kind == "re":       # rectangle
                shape.draw_rect(item[1])
            elif kind == "c":        # cubic bezier
                shape.draw_bezier(item[1], item[2], item[3], item[4])
            elif kind == "qu":       # quad
                shape.draw_quad(item[1])

        lw = draw.get("width") or 1
        shape.finish(
            color=new_color,
            fill=new_fill,
            width=lw,
            closePath=draw.get("closePath", False),
        )
        shape.commit()


def _replace_solaredge_text(page):
    """Redact all SolarEdge brand text and rewrite with ATEA equivalent."""
    for old_text, new_text in SOLAREDGE_REPLACEMENTS:
        hits = page.search_for(old_text)
        for rect in hits:
            # Sample font size from the surrounding text block
            font_size = 10
            blocks = page.get_text("dict", clip=rect).get("blocks", [])
            for block in blocks:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        font_size = span.get("size", 10)
                        break

            page.add_redact_annot(rect, fill=WHITE)

        if hits:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # Re-insert the replacement text at every original position
        hits_after = page.search_for(old_text)  # should be empty now
        _ = hits_after  # kept for clarity

        # We need to find where we just blanked – re-search is not possible
        # after redaction; instead we keep the rects from the first search.
        for rect in hits:
            page.insert_text(
                rect.tl,
                new_text,
                fontname="helv",
                fontsize=font_size,
                color=BLACK,
            )


def _draw_header(page, logo_bytes, client_info, page_width):
    """
    Draw the ATEA header on *page*:
      • white background rectangle (full width, 80 px tall)
      • "VOTRE PROJET, " (black) + "SUR-MESURE." (ATEA blue) — 24 pt bold
      • ATEA logo (right-aligned, 60 px tall)
      • client info line (helv 9 pt, black)
    """
    header_h = 80
    header_rect = fitz.Rect(0, 0, page_width, header_h)

    shape = page.new_shape()
    shape.draw_rect(header_rect)
    shape.finish(color=WHITE, fill=WHITE, width=0)
    shape.commit()

    # ---- Tagline ----
    tag_y = 30  # baseline y for the tagline text
    x_cursor = 10

    part1 = "VOTRE PROJET, "
    part2 = "SUR-MESURE."

    # Measure part1 width so we can place part2 right after it
    tw1 = fitz.get_text_length(part1, fontname="helv", fontsize=24)

    page.insert_text(
        fitz.Point(x_cursor, tag_y),
        part1,
        fontname="helv",
        fontsize=24,
        color=BLACK,
    )
    page.insert_text(
        fitz.Point(x_cursor + tw1, tag_y),
        part2,
        fontname="helv",
        fontsize=24,
        color=ATEA_BLUE,
    )

    # ---- Logo (right side) ----
    if logo_bytes:
        try:
            logo_h = 60
            logo_img = fitz.open("png", logo_bytes)
            logo_page = logo_img[0]
            aspect = logo_page.rect.width / logo_page.rect.height
            logo_w = logo_h * aspect
            logo_rect = fitz.Rect(
                page_width - logo_w - 10,
                5,
                page_width - 10,
                5 + logo_h,
            )
            page.insert_image(logo_rect, stream=logo_bytes)
            print("[header] Logo inserted")
        except Exception as exc:
            print(f"[header] Could not insert logo: {exc}")

    # ---- Client info line ----
    if client_info:
        page.insert_text(
            fitz.Point(10, 55),
            client_info,
            fontname="helv",
            fontsize=9,
            color=BLACK,
        )


def _draw_footer(page, page_width, page_height):
    """
    Draw the ATEA footer on *page*:
      • solid blue rectangle (full width, 30 px, bottom of page)
      • left: disclaimer text in white 7 pt
      • right: "ATEA.CORSICA" in white 8 pt bold
    """
    footer_h = 30
    footer_rect = fitz.Rect(0, page_height - footer_h, page_width, page_height)

    shape = page.new_shape()
    shape.draw_rect(footer_rect)
    shape.finish(color=ATEA_BLUE, fill=ATEA_BLUE, width=0)
    shape.commit()

    disclaimer = (
        "VOTRE PROJET EST SUR-MESURE ET A ÉTÉ RÉALISÉ GRATUITEMENT PAR NOTRE BUREAU D'ÉTUDES INTERNE. "
        "POUR TOUTE INFORMATION COMPLÉMENTAIRE, CONTACTEZ VOTRE CHARGÉ D'AFFAIRES."
    )
    # Baseline: centre of the footer rectangle
    text_y = page_height - footer_h + 11

    page.insert_text(
        fitz.Point(8, text_y),
        disclaimer,
        fontname="helv",
        fontsize=7,
        color=WHITE,
    )

    domain = "ATEA.CORSICA"
    domain_w = fitz.get_text_length(domain, fontname="helv", fontsize=8)
    page.insert_text(
        fitz.Point(page_width - domain_w - 8, text_y),
        domain,
        fontname="helv",
        fontsize=8,
        color=WHITE,
    )


def _erase_solaredge_header(page, page_width, page_height):
    """Cover the top 8 % of the page with a white rectangle."""
    erase_h = page_height * 0.08
    erase_rect = fitz.Rect(0, 0, page_width, erase_h)
    shape = page.new_shape()
    shape.draw_rect(erase_rect)
    shape.finish(color=WHITE, fill=WHITE, width=0)
    shape.commit()


# ---------------------------------------------------------------------------
# Core transformation
# ---------------------------------------------------------------------------

def transform_pdf(pdf_bytes: bytes) -> bytes:
    """
    Apply all ATEA branding transformations to *pdf_bytes* and return the
    resulting PDF as bytes.
    """
    logo_bytes = _download_logo()

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    print(f"[transform] Opened PDF with {len(doc)} page(s)")

    # Extract client info from page 1 before any modifications
    client_info = ""
    if len(doc) > 0:
        client_info = _extract_client_info(doc[0])
        print(f"[transform] Client info: {client_info!r}")

    for page_num, page in enumerate(doc):
        page_rect = page.rect
        page_width = page_rect.width
        page_height = page_rect.height
        print(f"[transform] Processing page {page_num + 1} ({page_width:.0f}×{page_height:.0f})")

        # 1. Erase SolarEdge header
        _erase_solaredge_header(page, page_width, page_height)

        # 2. Replace SolarEdge text occurrences
        _replace_solaredge_text(page)

        # 3. Recolour SolarEdge brand colours
        _recolour_drawings(page)

        # 4. Draw ATEA header (on top of everything)
        _draw_header(page, logo_bytes, client_info, page_width)

        # 5. Draw ATEA footer
        _draw_footer(page, page_width, page_height)

    output = io.BytesIO()
    doc.save(output, garbage=4, deflate=True)
    doc.close()
    result = output.getvalue()
    print(f"[transform] Done – output size {len(result)} bytes")
    return result


# ---------------------------------------------------------------------------
# Vercel / WSGI entry point
# ---------------------------------------------------------------------------

def _parse_multipart(body: bytes, content_type: str):
    """
    Minimal multipart/form-data parser.
    Returns a dict {field_name: bytes}.
    """
    # Extract boundary
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip().strip('"')
            break

    if not boundary:
        raise ValueError("No boundary found in Content-Type header")

    fields = {}
    delimiter = ("--" + boundary).encode()
    # Split on delimiter lines
    raw_parts = body.split(delimiter)

    for raw_part in raw_parts[1:]:  # skip preamble
        if raw_part.strip() in (b"", b"--", b"--\r\n", b"\r\n--"):
            continue
        if raw_part.startswith(b"--"):
            continue

        # Separate headers from body (double CRLF)
        if b"\r\n\r\n" in raw_part:
            header_section, content = raw_part.split(b"\r\n\r\n", 1)
        elif b"\n\n" in raw_part:
            header_section, content = raw_part.split(b"\n\n", 1)
        else:
            continue

        # Strip trailing boundary delimiter
        content = content.rstrip(b"\r\n")

        header_text = header_section.decode("utf-8", errors="replace")
        name_match = re.search(r'name="([^"]+)"', header_text)
        if not name_match:
            continue

        field_name = name_match.group(1)
        fields[field_name] = content

    return fields


class handler(BaseHTTPRequestHandler):
    """Vercel serverless handler (Python runtime)."""

    def log_message(self, fmt, *args):  # silence default access log noise
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            content_type = self.headers.get("Content-Type", "")

            print(f"[handler] POST received – Content-Length={content_length}, Content-Type={content_type}")

            if "multipart/form-data" not in content_type:
                self._error(400, "Expected multipart/form-data")
                return

            fields = _parse_multipart(body, content_type)
            if "pdf" not in fields:
                self._error(400, "Missing 'pdf' field in form data")
                return

            pdf_bytes = fields["pdf"]
            print(f"[handler] PDF field size: {len(pdf_bytes)} bytes")

            result_pdf = transform_pdf(pdf_bytes)

            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/pdf")
            self.send_header(
                "Content-Disposition", 'attachment; filename="ATEA_Rapport.pdf"'
            )
            self.send_header("Content-Length", str(len(result_pdf)))
            self.end_headers()
            self.wfile.write(result_pdf)

        except Exception:
            tb = traceback.format_exc()
            print(f"[handler] ERROR:\n{tb}")
            self._error(500, str(tb))

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _error(self, status: int, message: str):
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
