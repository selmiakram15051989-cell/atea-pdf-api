import base64
import io
import json
import re
import traceback
from http.server import BaseHTTPRequestHandler

import fitz  # PyMuPDF


# ---------------------------------------------------------------------------
# Multipart parser
# ---------------------------------------------------------------------------

def _parse_multipart(body: bytes, content_type: str) -> dict:
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
    raw_parts = body.split(delimiter)

    for raw_part in raw_parts[1:]:
        if raw_part.strip() in (b"", b"--", b"--\r\n", b"\r\n--"):
            continue
        if raw_part.startswith(b"--"):
            continue
        if b"\r\n\r\n" in raw_part:
            header_section, content = raw_part.split(b"\r\n\r\n", 1)
        elif b"\n\n" in raw_part:
            header_section, content = raw_part.split(b"\n\n", 1)
        else:
            continue
        content = content.rstrip(b"\r\n")
        header_text = header_section.decode("utf-8", errors="replace")
        name_match = re.search(r'name="([^"]+)"', header_text)
        if not name_match:
            continue
        fields[name_match.group(1)] = content

    return fields


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _first(pattern, text, group=1, flags=re.IGNORECASE):
    m = re.search(pattern, text, flags)
    return m.group(group).strip() if m else ""


def _clean_number(s: str) -> str:
    """Remove stray spaces inside numbers: '16 209' → '16209'."""
    return re.sub(r"(\d)\s+(\d)", r"\1\2", s.strip())


def _find_after(keyword_pattern: str, text: str, value_pattern: str) -> str:
    """Find *value_pattern* in the text that follows *keyword_pattern*."""
    m = re.search(keyword_pattern, text, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    tail = text[m.end():]
    v = re.search(value_pattern, tail[:500])
    return _clean_number(v.group(1).strip()) if v else ""


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------

def _extract_client(full_text: str) -> dict:
    nom = _first(r"(PV\s+(?:PART|PRO)[^\n]*)", full_text)
    adresse = _first(r"([^\n]*\d{5}[^\n]*)", full_text)
    date = _first(
        r"(\d{1,2}\s+(?:jan|fév|mar|avr|mai|juin|juil|août|sep|oct|nov|déc)[a-z.]*\.?\s+\d{4}"
        r"|\d{1,2}/\d{2}/\d{4})",
        full_text,
        flags=re.IGNORECASE,
    )
    return {"nom": nom, "adresse": adresse, "date": date}


def _extract_systeme(full_text: str) -> dict:
    modules = _first(r"(\d+)\s*[Mm]odules?\s*PV", full_text)
    onduleurs = _first(r"(\d+)\s*[Oo]nduleur", full_text)
    optimiseurs = _first(r"(\d+)\s*[Oo]ptimiseur", full_text)

    # First kWc occurrence → puissance DC
    m_dc = re.search(r"([\d]+[\.,]?\d*)\s*kWc", full_text, re.IGNORECASE)
    puissance_dc = (m_dc.group(1).strip() + " kWc") if m_dc else ""

    # kW after DC → puissance AC (second distinct occurrence)
    m_ac = re.search(
        r"([\d]+[\.,]\d+)\s*kW(?!c)", full_text[m_dc.end() if m_dc else 0:], re.IGNORECASE
    )
    puissance_ac = (m_ac.group(1).strip() + " kW") if m_ac else ""

    # Production annuelle: number before kWh in a "Production" context
    prod = _first(r"(\d[\d\s]*)\s*kWh", full_text)
    prod = _clean_number(prod)

    co2 = _first(r"([\d]+[\.,]\d+)\s*kg", full_text)
    arbres = _first(r"(\d+)\s*[Aa]rbre", full_text)

    return {
        "modules": modules,
        "onduleurs": onduleurs,
        "optimiseurs": optimiseurs,
        "puissance_dc": puissance_dc,
        "puissance_ac": puissance_ac,
        "production_annuelle": prod + " kWh" if prod else "",
        "co2_economise": co2 + " kg" if co2 else "",
        "arbres": arbres,
    }


def _extract_financier(full_text: str) -> dict:
    def _euro(keyword: str) -> str:
        return _find_after(keyword, full_text, r"€?\s*([\d\s]+[\.,]?\d*)\s*€?")

    def _pct(keyword: str) -> str:
        return _find_after(keyword, full_text, r"([\d]+[\.,]\d+)\s*%")

    def _years(keyword: str) -> str:
        return _find_after(keyword, full_text, r"(\d+)\s*ann[ée]e")

    paiements = _euro("Paiements nets")
    economies_duree = _euro(r"[Éé]conomies sur la facture")
    van = _euro(r"[Bb][ée]n[ée]fice du syst[eè]me")
    tri = _pct(r"rentabilit[ée]")
    roi = _years(r"[Rr]etour sur investissement|[Rr]ecuperation")
    prix = _euro(r"Prix du syst[eè]me")
    aides = _euro(r"Montant des aides")

    retour_pct = _first(r"([\d]+[\.,]\d+)\s*%\s*de\s*retour", full_text)
    # €/kWh: the euro sign may be rendered as U+00B7 (·) by some PDF fonts
    cout_kwh = _first(r"([\d]+[\.,]\d+)\s*[€·]/kWh", full_text)

    # Currency symbol may be €, ·, or absent — capture the decimal number
    _cur = r"[€·]?"
    facture = _first(
        r"[Ff]acture\s*(?:mensuelle|actuelle)[^\n]*?([\d]+[\.,]\d+)\s*" + _cur, full_text
    )
    facture_atea = _first(
        r"(?:[Ff]acture\s*avec\s*ATEA|[Ff]acture\s*solaire)[^\n]*?([\d]+[\.,]\d+)\s*" + _cur,
        full_text,
    )
    economies_men = _first(
        r"[Éé]conomies\s*mensuelle[^\n]*?([\d]+[\.,]\d+)\s*" + _cur, full_text
    )
    compensation = _first(
        r"[Cc]ompensation[^\n]*?([\d]+[\.,]\d+)\s*" + _cur, full_text
    )

    return {
        "paiements_nets": _clean_number(paiements),
        "economies_duree": _clean_number(economies_duree),
        "van": _clean_number(van),
        "tri": tri,
        "retour_investissement": roi,
        "prix_systeme": _clean_number(prix),
        "montant_aides": _clean_number(aides),
        "retour_pct": retour_pct,
        "cout_kwh": cout_kwh,
        "facture_mensuelle": facture,
        "facture_avec_atea": facture_atea,
        "economies_mensuelles": economies_men,
        "compensation": compensation,
    }


def _extract_production(full_text: str) -> dict:
    dom = _first(r"(\d+)\s*%\s*[Vv]ers\s*(?:le\s*)?domicile", full_text)
    res = _first(r"(\d+)\s*%\s*[Vv]ers\s*(?:le\s*)?r[ée]seau", full_text)
    pv = _first(r"(\d+)\s*%\s*[Dd]epuis\s*(?:le\s*)?PV|(\d+)\s*%.*autoconsomm", full_text)
    grid = _first(r"(\d+)\s*%\s*[Dd]epuis\s*(?:le\s*)?r[ée]seau", full_text)

    def _int(s):
        try:
            return int(s)
        except (ValueError, TypeError):
            return None

    return {
        "vers_domicile_pct": _int(dom),
        "vers_reseau_pct": _int(res),
        "conso_depuis_pv_pct": _int(pv),
        "conso_depuis_reseau_pct": _int(grid),
    }


def _extract_modules_detail(full_text: str) -> dict:
    nombre = _first(r"(\d+)\s*[Mm]odules?\s*PV", full_text)
    # Model name follows "DMEGC", "Hengdian", or "modèle :" with optional space
    modele = _first(
        r"(?:DMEGC|Hengdian|[Mm]od[eè]le\s*:?)\s+([A-Za-z0-9][^\n]{5,80})", full_text
    )
    puissance = _first(r"([\d]+[\.,]?\d*\s*kWc)", full_text)
    # Number comes AFTER the keyword in SolarEdge PDFs
    azimut = _first(r"azimut\s*([\d]+°?)", full_text)
    inclinaison = _first(r"inclinaison\s*([\d]+°?)", full_text)

    return {
        "nombre": nombre,
        "modele": modele,
        "puissance": puissance,
        "azimut": azimut,
        "inclinaison": inclinaison,
    }


def _extract_cashflow(full_text: str) -> list:
    """
    Try to find a sequence of signed integers/floats that looks like a
    year-by-year cash-flow table (at least 10 values).
    """
    # Look for runs of numbers (possibly negative) separated by whitespace
    blocks = re.findall(
        r"(-?\d[\d\s]*(?:[\.,]\d+)?(?:\s+-?\d[\d\s]*(?:[\.,]\d+)?){9,})",
        full_text,
    )
    for block in blocks:
        tokens = re.findall(r"-?\d[\d\.,]*", block)
        values = []
        for t in tokens:
            try:
                values.append(int(float(t.replace(",", ".").replace(" ", ""))))
            except ValueError:
                pass
        if len(values) >= 10:
            return values
    return []


# ---------------------------------------------------------------------------
# Image extraction
# ---------------------------------------------------------------------------

def _extract_photos(doc: fitz.Document, min_bytes: int = 50000, max_photos: int = 3) -> list:
    photos = []
    seen_xrefs = set()

    for page_num in range(len(doc)):
        if len(photos) >= max_photos:
            break
        page = doc[page_num]
        for img_info in page.get_images(full=True):
            if len(photos) >= max_photos:
                break
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            try:
                img_dict = doc.extract_image(xref)
                img_bytes = img_dict.get("image", b"")
                ext = img_dict.get("ext", "jpeg")
                if len(img_bytes) < min_bytes:
                    print(f"[photos] xref={xref} skipped ({len(img_bytes)} bytes < {min_bytes})")
                    continue
                b64 = base64.b64encode(img_bytes).decode("ascii")
                mime = "image/png" if ext == "png" else "image/jpeg"
                photos.append(f"data:{mime};base64,{b64}")
                print(f"[photos] xref={xref} extracted ({len(img_bytes)} bytes, {ext})")
            except Exception as exc:
                print(f"[photos] xref={xref} error: {exc}")

    return photos


# ---------------------------------------------------------------------------
# Main extraction pipeline
# ---------------------------------------------------------------------------

def extract_pdf(pdf_bytes: bytes) -> dict:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    print(f"[extract] Opened PDF – {len(doc)} page(s)")

    # Concatenate text from all pages
    pages_text = []
    for i, page in enumerate(doc):
        t = page.get_text("text")
        pages_text.append(t)
        print(f"[extract] Page {i+1}: {len(t)} chars")

    full_text = "\n".join(pages_text)

    result = {
        "client": _extract_client(full_text),
        "systeme": _extract_systeme(full_text),
        "financier": _extract_financier(full_text),
        "production": _extract_production(full_text),
        "modules_detail": _extract_modules_detail(full_text),
        "cashflow": _extract_cashflow(full_text),
        "photos": [],
    }

    try:
        result["photos"] = _extract_photos(doc)
        print(f"[extract] {len(result['photos'])} photo(s) extracted")
    except Exception as exc:
        print(f"[extract] Photo extraction failed: {exc}")

    doc.close()
    return result


# ---------------------------------------------------------------------------
# Vercel handler
# ---------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            content_type = self.headers.get("Content-Type", "")

            print(f"[handler] POST Content-Length={content_length} Content-Type={content_type}")

            if "multipart/form-data" not in content_type:
                self._json_response(400, {"error": "Expected multipart/form-data"})
                return

            fields = _parse_multipart(body, content_type)
            if "pdf" not in fields:
                self._json_response(400, {"error": "Missing 'pdf' field in form data"})
                return

            pdf_bytes = fields["pdf"]
            print(f"[handler] PDF field: {len(pdf_bytes)} bytes")

            data = extract_pdf(pdf_bytes)
            self._json_response(200, data)

        except Exception:
            tb = traceback.format_exc()
            print(f"[handler] ERROR:\n{tb}")
            self._json_response(500, {"error": tb})

    def _json_response(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
