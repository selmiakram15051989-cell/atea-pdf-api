import fitz  # PyMuPDF
import io
import json
import re
import requests
from http.server import BaseHTTPRequestHandler
from typing import Dict, Optional

# ─── ATEA brand colours ──────────────────────────────────────────────────────
ATEA_BLUE = (0 / 255, 74 / 255, 173 / 255)        # #004AAD
ATEA_YELLOW = (246 / 255, 178 / 255, 27 / 255)     # #F6B21B
WHITE = (1.0, 1.0, 1.0)
BLACK = (0.0, 0.0, 0.0)

# ─── SolarEdge colours to replace (approximate RGB 0-1 range) ────────────────
# Orange SolarEdge  ≈ (0.9, 0.4, 0.1)
# Blue SolarEdge    ≈ (0.2, 0.5, 0.8)
SOLAREDGE_ORANGE = (0.9, 0.4, 0.1)
SOLAREDGE_BLUE = (0.2, 0.5, 0.8)
COLOR_TOLERANCE = 0.15   # per-channel tolerance for colour matching

LOGO_URL = "https://atea.corsica/wp-content/uploads/2026/01/LOGO-ATEA.png"

FOOTER_TEXT = (
    "VOTRE PROJET EST SUR-MESURE ET A ÉTÉ RÉALISÉ GRATUITEMENT PAR NOTRE BUREAU "
    "D'ÉTUDES INTERNE. POUR TOUTE INFORMATION COMPLÉMENTAIRE, CONTACTEZ VOTRE "
    "CHARGÉ D'AFFAIRES."
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _colour_near(c, target, tol=COLOR_TOLERANCE):
    """Return True when colour tuple c is within tol of target (per channel)."""
    if c is None or len(c) < 3:
        return False
    return all(abs(c[i] - target[i]) <= tol for i in range(3))


def _fetch_logo() -> Optional[bytes]:
    """Download the ATEA logo; return raw PNG bytes or None on failure."""
    try:
        resp = requests.get(LOGO_URL, timeout=10)
        resp.raise_for_status()
        print(f"[logo] Downloaded ATEA logo ({len(resp.content)} bytes)")
        return resp.content
    except Exception as exc:
        print(f"[logo] Could not download ATEA logo: {exc}")
        return None


def _extract_client_info(page: fitz.Page) -> str:
    """
    Extract client name, address and date from page 1.
    Returns a formatted single-line string.
    Falls back to empty string if nothing useful is found.
    """
    text = page.get_text("text")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    name = ""
    address = ""
    date_str = ""

    date_pattern = re.compile(
        r"\b(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}|\d{4}[/\-\.]\d{2}[/\-\.]\d{2})\b"
    )

    for i, line in enumerate(lines):
        m = date_pattern.search(line)
        if m and not date_str:
            date_str = m.group()

        # Heuristic: lines that look like an address often contain digits
        if re.search(r"\d{4,}", line) and not address and len(line) < 80:
            address = line

        # First non-keyword, non-empty line of reasonable length → client name
        skip_words = {"solaredge", "atea", "rapport", "designer", "site", "page"}
        if (
            not name
            and len(line) > 3
            and len(line) < 50
            and not any(w in line.lower() for w in skip_words)
            and not date_pattern.search(line)
        ):
            name = line

    parts = [p for p in [name, address, date_str] if p]
    return "  |  ".join(parts)


def _insert_header(page: fitz.Page, logo_bytes: Optional[bytes], client_info: str) -> None:
    """Draw the ATEA header on the given page."""
    w = page.rect.width
    header_h = 80

    # 1. White background rectangle
    page.draw_rect(fitz.Rect(0, 0, w, header_h), color=WHITE, fill=WHITE)

    # 2. Two-tone title text
    #    "VOTRE PROJET, " in black  +  "SUR-MESURE." in ATEA blue
    title_y = 30
    font_size = 24
    part1 = "VOTRE PROJET, "
    part2 = "SUR-MESURE."

    page.insert_text(
        fitz.Point(10, title_y),
        part1,
        fontname="helv",
        fontsize=font_size,
        color=BLACK,
    )
    # Approximate width of part1 to position part2
    part1_width = fitz.get_text_length(part1, fontname="helv", fontsize=font_size)
    page.insert_text(
        fitz.Point(10 + part1_width, title_y),
        part2,
        fontname="helv",
        fontsize=font_size,
        color=ATEA_BLUE,
    )

    # 3. Client info line
    if client_info:
        page.insert_text(
            fitz.Point(10, title_y + 20),
            client_info,
            fontname="helv",
            fontsize=9,
            color=BLACK,
        )

    # 4. ATEA logo (top-right, height 60 px)
    if logo_bytes:
        try:
            logo_h = 60
            logo_rect = fitz.Rect(w - 120, 10, w - 10, 10 + logo_h)
            page.insert_image(logo_rect, stream=logo_bytes)
        except Exception as exc:
            print(f"[header] Logo insertion failed: {exc}")


def _erase_solaredge_header(page: fitz.Page) -> None:
    """Paint a white rectangle over the top 8 % of the page."""
    w = page.rect.width
    h = page.rect.height
    erase_h = h * 0.08
    page.draw_rect(fitz.Rect(0, 0, w, erase_h), color=WHITE, fill=WHITE)


def _replace_solaredge_text(page: fitz.Page) -> None:
    """Redact all SolarEdge brand mentions and rewrite as 'ATEA'."""
    variants = [
        ("SolarEdge", "ATEA"),
        ("solaredge", "atea"),
        ("SOLAREDGE", "ATEA"),
    ]
    for search_term, replacement in variants:
        hits = page.search_for(search_term)
        for rect in hits:
            # Capture font info to keep size consistent
            words = page.get_text("words", clip=rect)
            font_size = 11  # safe default
            if words:
                # words: (x0,y0,x1,y1,word,block,line,word_idx)
                text_h = abs(rect.y1 - rect.y0)
                font_size = max(6, round(text_h * 0.85))

            page.add_redact_annot(rect, fill=WHITE)
            page.apply_redactions()
            page.insert_text(
                fitz.Point(rect.x0, rect.y1 - 2),
                replacement,
                fontname="helv",
                fontsize=font_size,
                color=BLACK,
            )


def _recolour_drawings(page: fitz.Page) -> None:
    """
    Replace SolarEdge orange → ATEA yellow and SolarEdge blue → ATEA blue
    in all vector drawings on the page.
    """
    drawings = page.get_drawings()
    if not drawings:
        return

    for path in drawings:
        changed = False

        stroke = path.get("color")
        fill = path.get("fill")

        new_stroke = stroke
        new_fill = fill

        if _colour_near(stroke, SOLAREDGE_ORANGE):
            new_stroke = ATEA_YELLOW
            changed = True
        elif _colour_near(stroke, SOLAREDGE_BLUE):
            new_stroke = ATEA_BLUE
            changed = True

        if _colour_near(fill, SOLAREDGE_ORANGE):
            new_fill = ATEA_YELLOW
            changed = True
        elif _colour_near(fill, SOLAREDGE_BLUE):
            new_fill = ATEA_BLUE
            changed = True

        if not changed:
            continue

        # Redraw the path with updated colours
        shape = page.new_shape()
        for item in path.get("items", []):
            kind = item[0]
            if kind == "l":           # line
                shape.draw_line(item[1], item[2])
            elif kind == "re":        # rectangle
                shape.draw_rect(item[1])
            elif kind == "qu":        # quad
                shape.draw_quad(item[1])
            elif kind == "c":         # curve
                shape.draw_bezier(item[1], item[2], item[3], item[4])

        width = path.get("width") or 1
        shape.finish(
            color=new_stroke,
            fill=new_fill,
            width=width,
            closePath=path.get("closePath", False),
        )
        shape.commit()


def _insert_footer(page: fitz.Page) -> None:
    """Draw the ATEA blue footer bar at the bottom of the page."""
    w = page.rect.width
    h = page.rect.height
    footer_h = 30
    y0 = h - footer_h

    page.draw_rect(
        fitz.Rect(0, y0, w, h),
        color=ATEA_BLUE,
        fill=ATEA_BLUE,
    )

    # Left-aligned disclaimer text
    page.insert_text(
        fitz.Point(8, y0 + 11),
        FOOTER_TEXT,
        fontname="helv",
        fontsize=7,
        color=WHITE,
    )

    # Right-aligned "ATEA.CORSICA"
    brand = "ATEA.CORSICA"
    brand_width = fitz.get_text_length(brand, fontname="helv", fontsize=8)
    page.insert_text(
        fitz.Point(w - brand_width - 8, y0 + 20),
        brand,
        fontname="helv",
        fontsize=8,
        color=WHITE,
    )


# ─── Multipart parser ────────────────────────────────────────────────────────

def _parse_multipart(body: bytes, content_type: str) -> Dict[str, bytes]:
    """
    Minimal multipart/form-data parser.
    Returns a dict mapping field name → raw bytes value.
    """
    boundary_match = re.search(r"boundary=([^\s;]+)", content_type)
    if not boundary_match:
        raise ValueError("No boundary found in Content-Type")

    boundary = boundary_match.group(1).encode()
    delimiter = b"--" + boundary
    parts = body.split(delimiter)
    fields: Dict[str, bytes] = {}

    for part in parts[1:]:  # skip preamble
        if part in (b"--\r\n", b"--", b""):
            continue
        # Split headers from body at first blank line
        if b"\r\n\r\n" in part:
            raw_headers, data = part.split(b"\r\n\r\n", 1)
        elif b"\n\n" in part:
            raw_headers, data = part.split(b"\n\n", 1)
        else:
            continue

        # Strip trailing CRLF from data
        data = data.rstrip(b"\r\n")

        headers_text = raw_headers.decode("utf-8", errors="replace")
        cd_match = re.search(
            r'Content-Disposition:[^\n]+name="([^"]+)"', headers_text, re.IGNORECASE
        )
        if cd_match:
            fields[cd_match.group(1)] = data

    return fields


# ─── Core transformation ─────────────────────────────────────────────────────

def transform_pdf(pdf_bytes: bytes) -> bytes:
    """Apply all ATEA branding transformations to a SolarEdge PDF."""
    print(f"[transform] Input PDF size: {len(pdf_bytes)} bytes")
    logo_bytes = _fetch_logo()

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    print(f"[transform] PDF has {doc.page_count} page(s)")

    # Extract client info from first page before any modifications
    client_info = ""
    if doc.page_count > 0:
        client_info = _extract_client_info(doc[0])
        print(f"[transform] Client info: {client_info!r}")

    for page_num, page in enumerate(doc):
        print(f"[transform] Processing page {page_num + 1}/{doc.page_count}")

        # Step 1: Erase SolarEdge header
        _erase_solaredge_header(page)

        # Step 2: Recolour vector drawings (before header/footer to avoid overlap)
        _recolour_drawings(page)

        # Step 3: Replace SolarEdge text occurrences
        _replace_solaredge_text(page)

        # Step 4: Insert ATEA header
        _insert_header(page, logo_bytes, client_info)

        # Step 5: Insert ATEA footer
        _insert_footer(page)

    output = io.BytesIO()
    doc.save(output, garbage=4, deflate=True)
    doc.close()
    result = output.getvalue()
    print(f"[transform] Output PDF size: {len(result)} bytes")
    return result


# ─── Vercel serverless handler ───────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._set_cors_headers()
        self.end_headers()

    def do_POST(self):
        try:
            content_type = self.headers.get("Content-Type", "")
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)

            fields = _parse_multipart(body, content_type)

            if "pdf" not in fields:
                self._json_error(400, "Missing 'pdf' field in multipart form data")
                return

            pdf_bytes = fields["pdf"]
            print(f"[handler] Received PDF ({len(pdf_bytes)} bytes)")

            transformed = transform_pdf(pdf_bytes)

            self.send_response(200)
            self._set_cors_headers()
            self.send_header("Content-Type", "application/pdf")
            self.send_header(
                "Content-Disposition", 'attachment; filename="ATEA_Rapport.pdf"'
            )
            self.send_header("Content-Length", str(len(transformed)))
            self.end_headers()
            self.wfile.write(transformed)

        except Exception as exc:
            print(f"[handler] Unhandled error: {exc}")
            self._json_error(500, str(exc))

    def _set_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json_error(self, status: int, message: str):
        body = json.dumps({"error": message}).encode()
        self.send_response(status)
        self._set_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[http] {fmt % args}")
