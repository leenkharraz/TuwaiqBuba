"""Generate the synthetic sample documents used by sample mode.

All names, numbers and providers are fictional and every document carries a
"SAMPLE" watermark. The prepared extraction JSON for each document lives next
to it in fixtures/samples/<id>.extraction.json and must match what is drawn
here.

Run (dev dependencies required):  python scripts/generate_fixtures.py
The generated files are committed, so this is only needed to change samples.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    import arabic_reshaper
    from bidi import get_display
except ImportError:  # pragma: no cover
    sys.exit("Install dev dependencies first: pip install -r requirements-dev.txt")

OUT = Path(__file__).resolve().parent.parent / "fixtures" / "samples"
FONT_CANDIDATES = [
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
MONO_CANDIDATES = ["/System/Library/Fonts/Menlo.ttc", "C:/Windows/Fonts/consola.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]

INK = (20, 32, 52)
MUTED = (96, 110, 130)
BLUE = (1, 76, 140)
LINE = (205, 216, 230)


def _font(cands, size):
    for c in cands:
        if Path(c).exists():
            return ImageFont.truetype(c, size)
    return ImageFont.load_default()


def F(size):
    return _font(FONT_CANDIDATES, size)


def M(size):
    return _font(MONO_CANDIDATES, size)


def ar(text: str) -> str:
    """Shape and reorder Arabic so Pillow (without libraqm) draws it correctly."""
    return get_display(arabic_reshaper.reshape(text), base_dir="R")


def has_ar(text: str) -> bool:
    return any("\u0600" <= ch <= "\u06ff" for ch in text)


def txt(d, xy, text, size=26, fill=INK, anchor="la", bold=False, font=None):
    s = ar(text) if has_ar(text) else text
    d.text(xy, s, font=font or F(size), fill=fill, anchor=anchor,
           stroke_width=1 if bold else 0, stroke_fill=fill)


def watermark(img, text="SAMPLE · " + "مستند تجريبي"):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    s = "SAMPLE  ·  " + ar("مستند تجريبي")
    d.text((img.width // 2, img.height // 2), s, font=F(int(img.width / 13)), fill=(200, 60, 60, 38), anchor="mm")
    layer = layer.rotate(28, resample=Image.BICUBIC)
    base = img.convert("RGBA")
    base.alpha_composite(layer)
    return base.convert("RGB")


def footer(d, w, h):
    d.line((60, h - 110, w - 60, h - 110), fill=LINE, width=2)
    txt(d, (w // 2, h - 80), "SAMPLE — synthetic document for the Bupa Discovery prototype. Not a real invoice or medical record.",
        size=18, fill=MUTED, anchor="mm")
    txt(d, (w // 2, h - 50), "مستند اصطناعي للتجربة فقط — ليس فاتورة أو سجلا طبيا حقيقيا", size=18, fill=MUTED, anchor="mm")


def bilingual_row(d, y, en, value, arabic, w, value_size=27, bold_value=True):
    txt(d, (80, y), en, size=22, fill=MUTED)
    txt(d, (w - 80, y), arabic, size=22, fill=MUTED, anchor="ra")
    txt(d, (w // 2, y), value, size=value_size, anchor="ma", bold=bold_value)
    d.line((80, y + 44, w - 80, y + 44), fill=(234, 239, 246), width=1)


def header(d, w, name_ar, name_en, city_ar, city_en, extra=None):
    d.rectangle((0, 0, w, 16), fill=BLUE)
    txt(d, (w - 80, 60), name_ar, size=36, bold=True, fill=BLUE, anchor="ra")
    txt(d, (80, 60), name_en, size=30, bold=True, fill=BLUE)
    txt(d, (w - 80, 112), city_ar, size=22, fill=MUTED, anchor="ra")
    txt(d, (80, 112), city_en, size=22, fill=MUTED)
    if extra:
        txt(d, (80, 148), extra, size=20, fill=MUTED)
    d.line((60, 190, w - 60, 190), fill=LINE, width=2)


def items_table(d, y, w, rows, cur_ar="المبلغ (ر.س)", cur_en="Amount (SAR)"):
    d.rectangle((70, y, w - 70, y + 56), fill=(232, 241, 250))
    txt(d, (w - 90, y + 14), "الوصف", size=22, bold=True, anchor="ra")
    txt(d, (110, y + 14), "Description", size=22, bold=True)
    txt(d, (w // 2 + 40, y + 14), cur_ar + " / " + cur_en if False else cur_en, size=22, bold=True, anchor="ma")
    y += 70
    for ar_desc, en_desc, amount in rows:
        txt(d, (w - 90, y), ar_desc, size=24, anchor="ra")
        txt(d, (110, y + 36), en_desc, size=20, fill=MUTED)
        txt(d, (w // 2 + 40, y + 10), amount, size=26, bold=True, anchor="ma", font=M(26))
        y += 84
        d.line((70, y - 8, w - 70, y - 8), fill=(234, 239, 246), width=1)
    return y


def totals(d, y, w, rows):
    for en, value, arabic, strong in rows:
        txt(d, (w // 2 - 60, y), en, size=22, fill=INK if strong else MUTED, anchor="ra", bold=strong)
        txt(d, (w // 2 + 40, y), value, size=28 if strong else 24, anchor="ma", bold=strong, font=M(28 if strong else 24))
        txt(d, (w - 90, y), arabic, size=22, fill=INK if strong else MUTED, anchor="ra", bold=strong)
        y += 50
    return y


def outpatient_invoice() -> Image.Image:
    w, h = 1240, 1754
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    header(d, w, "مجمع الواحة الطبي التجريبي", "Al Waha Demo Medical Complex",
           "الرياض، المملكة العربية السعودية", "Riyadh, Saudi Arabia", "VAT No. 300000000000003")
    txt(d, (w // 2, 225), "فاتورة ضريبية", size=38, bold=True, anchor="ma")
    txt(d, (w // 2, 278), "Tax Invoice", size=26, fill=MUTED, anchor="ma")
    y = 345
    for en, val, a in [
        ("Invoice No.", "INV-24-10871", "رقم الفاتورة"),
        ("Invoice Date", "2026-11-16", "تاريخ الفاتورة"),
        ("Visit Date", "2026-11-16", "تاريخ الزيارة"),
        ("Patient", "ريم خالد المطيري", "اسم المريض"),
        ("File No.", "0042187", "رقم الملف"),
        ("Clinic", "Internal Medicine", "العيادة: الباطنية"),
        ("Diagnosis", "Acute pharyngitis (J02.9)", "التشخيص"),
    ]:
        bilingual_row(d, y, en, val, a, w)
        y += 58
    y = items_table(d, y + 20, w, [
        ("كشفية استشاري باطنية", "Consultant consultation - Internal Medicine", "300.00"),
        ("تحليل صورة دم كاملة CBC", "Complete blood count (CBC)", "120.00"),
        ("تحليل البروتين التفاعلي CRP", "C-reactive protein (CRP)", "80.00"),
    ])
    y = totals(d, y + 20, w, [
        ("Subtotal", "500.00", "المجموع قبل الضريبة", False),
        ("VAT 15%", "75.00", "ضريبة القيمة المضافة 15%", False),
        ("Total (SAR)", "575.00", "الإجمالي - ر.س", True),
    ])
    txt(d, (w - 90, y + 30), "المبلغ المستحق: 575.00 ريال سعودي", size=24, anchor="ra", bold=True)
    footer(d, w, h)
    return watermark(img)


def pos_receipt(amount: str, rrn: str, approval: str) -> Image.Image:
    w, h = 640, 1180
    img = Image.new("RGB", (w, h), (252, 252, 250))
    d = ImageDraw.Draw(img)
    m = M(26)
    lines = [
        ("AL WAHA DEMO MEDICAL COMPLEX", True), ("RIYADH - KSA", False), ("-" * 30, False),
        ("mada            PURCHASE", True), ("DATE: 16/11/2026  TIME: 10:42", False),
        ("TERMINAL: 60012345", False), ("CARD: **** **** **** 4417", False),
        ("RRN: " + rrn, False), ("REF: INV-24-10871", False), ("-" * 30, False),
        ("AMOUNT        SAR " + amount, True), ("-" * 30, False),
        ("APPROVED  " + approval, True), ("THANK YOU", False), ("", False),
        ("*** SAMPLE - SYNTHETIC ***", True), ("*** NOT A REAL RECEIPT ***", False),
    ]
    y = 60
    for line, bold in lines:
        d.text((w // 2, y), line, font=m, fill=INK, anchor="ma", stroke_width=1 if bold else 0, stroke_fill=INK)
        y += 56
    return watermark(img)


def prescription() -> Image.Image:
    w, h = 1240, 1754
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w, 16), fill=BLUE)
    txt(d, (80, 60), "Al Waha Demo Medical Complex - Outpatient Clinics", size=32, bold=True, fill=BLUE)
    txt(d, (80, 110), "Riyadh, Saudi Arabia", size=22, fill=MUTED)
    d.line((60, 160, w - 60, 160), fill=LINE, width=2)
    txt(d, (80, 200), "PRESCRIPTION", size=40, bold=True)
    y = 290
    for label, val in [("Patient:", "Reem Khalid Almutairi"), ("MRN:", "0042187"), ("Date:", "16 Nov 2026"),
                       ("Clinic:", "Internal Medicine"), ("Diagnosis:", "Acute pharyngitis (J02.9)")]:
        txt(d, (80, y), label, size=26, fill=MUTED)
        txt(d, (300, y), val, size=28, bold=True)
        y += 60
    d.line((60, y + 10, w - 60, y + 10), fill=LINE, width=2)
    txt(d, (80, y + 50), "Rx", size=48, bold=True, fill=BLUE)
    y += 130
    for n, (drug, sig) in enumerate([
        ("Amoxicillin 500 mg capsules", "1 capsule three times daily for 7 days"),
        ("Paracetamol 500 mg tablets", "1 tablet every 8 hours as needed for fever or pain"),
    ], start=1):
        txt(d, (80, y), f"{n}. {drug}", size=30, bold=True)
        txt(d, (120, y + 46), sig, size=24, fill=MUTED)
        y += 120
    txt(d, (80, y + 60), "Physician: Dr. Sample Physician, Internal Medicine", size=24)
    txt(d, (80, y + 100), "License No.: DEMO-000000", size=22, fill=MUTED)
    footer(d, w, h)
    return watermark(img)


def lab_result() -> Image.Image:
    w, h = 1240, 1754
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    header(d, w, "مختبر مجمع الواحة الطبي التجريبي", "Al Waha Demo Medical Complex - Laboratory",
           "الرياض، المملكة العربية السعودية", "Riyadh, Saudi Arabia")
    txt(d, (w // 2, 225), "نتائج التحاليل المخبرية", size=36, bold=True, anchor="ma")
    txt(d, (w // 2, 276), "Laboratory Report", size=26, fill=MUTED, anchor="ma")
    y = 345
    for en, val, a in [("Patient", "ريم خالد المطيري", "اسم المريض"), ("File No.", "0042187", "رقم الملف"),
                       ("Collected", "2026-11-16", "تاريخ سحب العينة"), ("Requested by", "Internal Medicine", "الطبيب المعالج")]:
        bilingual_row(d, y, en, val, a, w)
        y += 58
    y += 30
    d.rectangle((70, y, w - 70, y + 56), fill=(232, 241, 250))
    cols = [110, 520, 720, 900]
    for x, head in zip(cols, ["Test", "Result", "Unit", "Reference"]):
        txt(d, (x, y + 14), head, size=22, bold=True)
    y += 76
    for row in [("WBC", "11.8 H", "10^9/L", "4.0 - 11.0"), ("Hemoglobin", "13.1", "g/dL", "12.0 - 15.5"),
                ("Platelets", "265", "10^9/L", "150 - 400"), ("CRP", "18 H", "mg/L", "< 5")]:
        for x, val in zip(cols, row):
            txt(d, (x, y), val, size=25, bold=x == 520)
        y += 62
        d.line((70, y - 12, w - 70, y - 12), fill=(234, 239, 246), width=1)
    txt(d, (110, y + 30), "Validated by: Demo Lab Specialist", size=22, fill=MUTED)
    footer(d, w, h)
    return watermark(img)


def dental_invoice() -> Image.Image:
    w, h = 1240, 1754
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    header(d, w, "عيادة ابتسامة التجريبية لطب الأسنان", "Ibtisama Demo Dental Clinic",
           "جدة، المملكة العربية السعودية", "Jeddah, Saudi Arabia", "VAT No. 300000000000017")
    txt(d, (w // 2, 225), "فاتورة ضريبية", size=38, bold=True, anchor="ma")
    txt(d, (w // 2, 278), "Tax Invoice", size=26, fill=MUTED, anchor="ma")
    y = 345
    for en, val, a in [("Invoice No.", "DN-7781", "رقم الفاتورة"), ("Invoice Date", "2026-11-09", "تاريخ الفاتورة"),
                       ("Visit Date", "2026-11-09", "تاريخ الزيارة"), ("Patient", "ريم خالد المطيري", "اسم المريض"),
                       ("File No.", "D-5521", "رقم الملف")]:
        bilingual_row(d, y, en, val, a, w)
        y += 58
    y = items_table(d, y + 20, w, [
        ("حشو تجميلي كومبوزيت - سن رقم 36", "Composite filling - tooth #36", "450.00"),
        ("أشعة سينية بسيطة - سن رقم 36", "Periapical x-ray - tooth #36", "60.00"),
    ])
    totals(d, y + 20, w, [
        ("Subtotal", "510.00", "المجموع قبل الضريبة", False),
        ("VAT 15%", "76.50", "ضريبة القيمة المضافة 15%", False),
        ("Total (SAR)", "586.50", "الإجمالي - ر.س", True),
    ])
    footer(d, w, h)
    return watermark(img)


def as_phone_photo(img: Image.Image, angle: float, blur: float, darken: float) -> Image.Image:
    bg = Image.new("RGB", (int(img.width * 1.15), int(img.height * 1.1)), (92, 84, 72))
    doc = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(92, 84, 72))
    bg.paste(doc, ((bg.width - doc.width) // 2, (bg.height - doc.height) // 2))
    if blur:
        bg = bg.filter(ImageFilter.GaussianBlur(blur))
    if darken:
        bg = Image.blend(bg, Image.new("RGB", bg.size, (30, 28, 24)), darken)
    bg.thumbnail((1400, 1800))
    return bg


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    inv = outpatient_invoice()
    inv.save(OUT / "outpatient_invoice_ar.png", optimize=True)
    pos_receipt("575.00", "632011458820", "000318").save(OUT / "payment_receipt_en.png", optimize=True)
    prescription().save(OUT / "prescription_en.pdf", "PDF", resolution=150)
    lab_result().save(OUT / "lab_result_ar.png", optimize=True)
    pos_receipt("500.00", "632011459901", "000327").save(OUT / "receipt_mismatch_en.png", optimize=True)
    dental_invoice().save(OUT / "dental_invoice_ar.png", optimize=True)
    as_phone_photo(pos_receipt("575.00", "632011458820", "000318"), 7, 9, 0.45).save(
        OUT / "blurry_receipt_photo.jpg", quality=70)
    as_phone_photo(inv, -2.5, 0.6, 0.08).save(OUT / "invoice_photo_duplicate.jpg", quality=84)
    for p in sorted(OUT.iterdir()):
        print(f"{p.name:34s} {p.stat().st_size // 1024:5d} KB")


if __name__ == "__main__":
    main()
