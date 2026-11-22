"""Payslip HTML (Jinja templates) -> PDF (xhtml2pdf), optionally password
protected (pypdf), and a ZIP of a whole run."""
import base64
import io
import re
import zipfile
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.models.payroll_settings import PAYSLIP_TEMPLATES

HERE = Path(__file__).resolve().parent
DEFAULT_LOGO = HERE / "assets" / "staunch_logo.png"
LOGO_MIMES = {"image/png", "image/jpeg", "image/gif"}
MAX_LOGO_BYTES = 1024 * 1024

_env = Environment(loader=FileSystemLoader(str(HERE / "templates")), autoescape=select_autoescape(["html"]))


class PayslipRenderError(RuntimeError):
    pass


def logo_bytes(settings) -> tuple[bytes, str]:
    if getattr(settings, "logo", None):
        return settings.logo, settings.logo_mime or "image/png"
    return DEFAULT_LOGO.read_bytes(), "image/png"


def logo_data_uri(settings) -> str:
    data, mime = logo_bytes(settings)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def render_html(payslip: dict, template: str) -> str:
    name = template if template in PAYSLIP_TEMPLATES else "CLASSIC"
    return _env.get_template(f"{name.lower()}.html").render(p=payslip)


def html_to_pdf(html: str) -> bytes:
    from xhtml2pdf import pisa

    out = io.BytesIO()
    result = pisa.CreatePDF(html, dest=out, encoding="utf-8")
    if result.err:
        raise PayslipRenderError("The payslip PDF could not be generated.")
    return out.getvalue()


def protect(pdf: bytes, password: Optional[str]) -> bytes:
    if not password:
        return pdf
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    writer.encrypt(user_password=password, owner_password=None, algorithm="AES-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def render_pdf(payslip: dict, template: str, password: Optional[str] = None) -> bytes:
    return protect(html_to_pdf(render_html(payslip, template)), password)


def safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "payslip"


def payslip_filename(payslip: dict) -> str:
    emp = payslip["employee"]
    return safe_filename(f"Payslip_{emp.get('code') or ''}_{payslip['month_label'].replace(' ', '_')}") + ".pdf"


def zip_files(files: list[tuple[str, bytes]]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        seen: set[str] = set()
        for name, data in files:
            unique, n = name, 1
            while unique in seen:
                n += 1
                unique = name.replace(".pdf", f"_{n}.pdf")
            seen.add(unique)
            archive.writestr(unique, data)
    return out.getvalue()
