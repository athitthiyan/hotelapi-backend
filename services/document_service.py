"""
PDF document generation — invoice & voucher.

Uses reportlab Canvas for professional, well-aligned PDFs with proper
headers, tables, and branding.
"""
from __future__ import annotations

import logging
from io import BytesIO
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import models

logger = logging.getLogger(__name__)

PAGE_WIDTH, PAGE_HEIGHT = A4  # 595.27 x 841.89 points
LEFT_MARGIN = 40
RIGHT_MARGIN = PAGE_WIDTH - 40
CONTENT_WIDTH = RIGHT_MARGIN - LEFT_MARGIN

# Brand colours — Stayvora Premium
BRAND_PRIMARY = colors.HexColor("#0f2033")    # Midnight Navy
BRAND_ACCENT = colors.HexColor("#d6b86b")     # Champagne Gold
BRAND_LIGHT = colors.HexColor("#faf8f2")      # Warm Ivory
BRAND_TEXT = colors.HexColor("#2d3748")        # Rich Dark Text
BRAND_MUTED = colors.HexColor("#718096")      # Soft Slate


def invoice_number_for_booking(booking: models.Booking) -> str:
    return f"INV-{booking.booking_ref}"


def _draw_header(c: canvas.Canvas, title: str, y: float) -> float:
    """Draw the branded header bar. Returns the new y position."""
    # Background bar
    c.setFillColor(BRAND_PRIMARY)
    c.rect(0, y - 10, PAGE_WIDTH, 60, fill=True, stroke=False)

    # Logo text
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(LEFT_MARGIN, y + 15, "Stayvora")

    # Document title
    c.setFont("Helvetica", 12)
    c.drawRightString(RIGHT_MARGIN, y + 15, title)

    return y - 30


def _draw_divider(c: canvas.Canvas, y: float) -> float:
    """Draw a thin horizontal line."""
    c.setStrokeColor(colors.HexColor("#dddddd"))
    c.setLineWidth(0.5)
    c.line(LEFT_MARGIN, y, RIGHT_MARGIN, y)
    return y - 15


def _draw_section_title(c: canvas.Canvas, title: str, y: float) -> float:
    """Draw a bold section heading."""
    c.setFillColor(BRAND_ACCENT)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(LEFT_MARGIN, y, title)
    return y - 18


def _draw_key_value(
    c: canvas.Canvas,
    key: str,
    value: str,
    y: float,
    key_x: float = LEFT_MARGIN,
    val_x: float = LEFT_MARGIN + 140,
) -> float:
    """Draw a label: value pair."""
    c.setFillColor(BRAND_MUTED)
    c.setFont("Helvetica", 9)
    c.drawString(key_x, y, key)
    c.setFillColor(BRAND_TEXT)
    c.setFont("Helvetica", 9)
    c.drawString(val_x, y, str(value))
    return y - 15


def _draw_table_row(
    c: canvas.Canvas,
    cols: list[str],
    col_xs: list[float],
    y: float,
    bold: bool = False,
    bg: Optional[colors.Color] = None,
    align_right: Optional[list[int]] = None,
) -> float:
    """Draw a single table row. Returns the new y position."""
    if bg:
        c.setFillColor(bg)
        c.rect(LEFT_MARGIN - 5, y - 4, CONTENT_WIDTH + 10, 16, fill=True, stroke=False)

    font = "Helvetica-Bold" if bold else "Helvetica"
    c.setFont(font, 9)
    c.setFillColor(BRAND_ACCENT if bold else BRAND_TEXT)

    for i, (col, x) in enumerate(zip(cols, col_xs)):
        if align_right and i in align_right:
            c.drawRightString(x, y, col)
        else:
            c.drawString(x, y, col)
    return y - 18


def _safe_date(dt) -> str:
    """Convert datetime to ISO date string safely."""
    if dt is None:
        return "N/A"
    if hasattr(dt, "date"):
        return dt.date().isoformat()
    return str(dt)


def _safe_currency(amount, symbol: str = "INR") -> str:
    """Format a numeric amount as currency."""
    try:
        return f"{symbol} {float(amount):,.2f}"
    except (TypeError, ValueError):
        return f"{symbol} 0.00"


# ─── Invoice ────────────────────────────────────────────────────────────────


def build_invoice_pdf(booking: models.Booking) -> bytes:
    """
    Generate a professional tax invoice PDF for a booking.

    Includes:
      - Branded header
      - Invoice + booking metadata
      - Customer and hotel details
      - Line-item pricing table
      - Totals with taxes
      - Footer with GST info
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Stayvora Tax Invoice")
    c.setAuthor("Stayvora")
    c.setSubject(f"Invoice for {booking.booking_ref}")

    room = booking.room
    hotel = room.partner_hotel if room else None
    refund_amount = (
        booking.total_amount
        if booking.payment_status == models.PaymentStatus.REFUNDED
        else 0.0
    )

    # ── Header ──────────────────────────────────────────────────────
    y = PAGE_HEIGHT - 50
    y = _draw_header(c, "TAX INVOICE", y)
    y -= 20

    # ── Invoice metadata (two-column) ───────────────────────────────
    mid_x = PAGE_WIDTH / 2 + 20
    y = _draw_section_title(c, "Invoice Details", y)

    y = _draw_key_value(c, "Invoice No.", invoice_number_for_booking(booking), y)
    _draw_key_value(c, "Booking Ref", booking.booking_ref, y + 15, key_x=mid_x, val_x=mid_x + 100)

    y = _draw_key_value(c, "Invoice Date", _safe_date(booking.created_at), y)
    _draw_key_value(c, "Payment Status", (booking.payment_status.value if booking.payment_status else "N/A").upper(), y + 15, key_x=mid_x, val_x=mid_x + 100)

    y -= 5
    y = _draw_divider(c, y)

    # ── Customer details ─────────────────────────────────────────────
    y = _draw_section_title(c, "Bill To", y)
    y = _draw_key_value(c, "Guest Name", booking.user_name, y)
    y = _draw_key_value(c, "Email", booking.email, y)
    if booking.phone:
        y = _draw_key_value(c, "Phone", booking.phone, y)
    y -= 5
    y = _draw_divider(c, y)

    # ── Hotel & Stay details ─────────────────────────────────────────
    y = _draw_section_title(c, "Stay Details", y)
    hotel_name = room.hotel_name if room else "Stayvora Hotel"
    room_name = (
        room.room_type_name
        if room and room.room_type_name
        else (room.room_type.value if room else "Room")
    )
    y = _draw_key_value(c, "Hotel", hotel_name, y)
    y = _draw_key_value(c, "Room Type", room_name, y)
    y = _draw_key_value(c, "Check-in", _safe_date(booking.check_in), y)
    y = _draw_key_value(c, "Check-out", _safe_date(booking.check_out), y)
    y = _draw_key_value(c, "Nights", str(booking.nights), y)
    y = _draw_key_value(c, "Guests", str(booking.guests or 1), y)
    y -= 5
    y = _draw_divider(c, y)

    # ── Pricing table ────────────────────────────────────────────────
    y = _draw_section_title(c, "Pricing Breakdown", y)

    col_xs = [LEFT_MARGIN, LEFT_MARGIN + 250, RIGHT_MARGIN]
    y = _draw_table_row(
        c, ["Description", "Details", "Amount"],
        col_xs, y, bold=True, bg=BRAND_LIGHT,
        align_right=[2],
    )

    y = _draw_table_row(
        c, ["Room Rate", f"{booking.nights} night(s)", _safe_currency(booking.room_rate)],
        col_xs, y, align_right=[2],
    )
    y = _draw_table_row(
        c, ["Taxes (12%)", "", _safe_currency(booking.taxes)],
        col_xs, y, align_right=[2],
    )
    y = _draw_table_row(
        c, ["Service Fee (5%)", "", _safe_currency(booking.service_fee)],
        col_xs, y, align_right=[2],
    )

    # Total row
    y -= 2
    c.setStrokeColor(BRAND_PRIMARY)
    c.setLineWidth(1)
    c.line(LEFT_MARGIN, y + 12, RIGHT_MARGIN, y + 12)
    y = _draw_table_row(
        c, ["", "Total Paid", _safe_currency(booking.total_amount)],
        col_xs, y, bold=True, align_right=[2],
    )

    if refund_amount > 0:
        y = _draw_table_row(
            c, ["", "Refund Amount", f"- {_safe_currency(refund_amount)}"],
            col_xs, y, bold=True, align_right=[2],
        )

    y -= 10
    y = _draw_divider(c, y)

    # ── GST info ─────────────────────────────────────────────────────
    gst = hotel.gst_number if hotel and hotel.gst_number else None
    if gst:
        y = _draw_key_value(c, "Hotel GST No.", gst, y)
    else:
        c.setFillColor(BRAND_MUTED)
        c.setFont("Helvetica", 8)
        c.drawString(LEFT_MARGIN, y, "GST Number: Not provided by property")
        y -= 15

    # ── Footer ───────────────────────────────────────────────────────
    c.setFillColor(BRAND_MUTED)
    c.setFont("Helvetica", 7)
    c.drawCentredString(
        PAGE_WIDTH / 2, 40,
        "This is a computer-generated invoice. No signature required.",
    )
    c.drawCentredString(
        PAGE_WIDTH / 2, 30,
        "Stayvora | support@stayvora.co.in | www.stayvora.co.in",
    )

    c.showPage()
    c.save()
    return buf.getvalue()


# ─── Voucher ────────────────────────────────────────────────────────────────


def build_voucher_pdf(booking: models.Booking) -> bytes:
    """
    Generate a professional booking voucher PDF.

    Includes:
      - Branded header
      - Booking reference and guest details
      - Hotel and room information
      - Check-in / Check-out details
      - Booking & payment status
      - Hotel contact information
      - Important notes for the guest
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)

    room = booking.room
    hotel = room.partner_hotel if room else None

    # ── Header ──────────────────────────────────────────────────────
    y = PAGE_HEIGHT - 50
    y = _draw_header(c, "BOOKING VOUCHER", y)
    y -= 20

    # ── Accent bar with booking ref ─────────────────────────────────
    c.setFillColor(BRAND_ACCENT)
    c.rect(LEFT_MARGIN, y - 5, CONTENT_WIDTH, 28, fill=True, stroke=False)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(PAGE_WIDTH / 2, y + 3, f"Booking Ref: {booking.booking_ref}")
    y -= 35

    # ── Guest details ────────────────────────────────────────────────
    y = _draw_section_title(c, "Guest Information", y)
    y = _draw_key_value(c, "Guest Name", booking.user_name, y)
    y = _draw_key_value(c, "Email", booking.email, y)
    if booking.phone:
        y = _draw_key_value(c, "Phone", booking.phone, y)
    y = _draw_key_value(c, "No. of Guests", str(booking.guests or 1), y)
    y -= 5
    y = _draw_divider(c, y)

    # ── Hotel & Room ─────────────────────────────────────────────────
    y = _draw_section_title(c, "Hotel & Room Details", y)
    hotel_name = room.hotel_name if room else "Stayvora Hotel"
    room_name = (
        room.room_type_name
        if room and room.room_type_name
        else (room.room_type.value if room else "Room")
    )
    y = _draw_key_value(c, "Hotel", hotel_name, y)
    y = _draw_key_value(c, "Room Type", room_name, y)

    hotel_city = room.city if room and room.city else ""
    hotel_country = room.country if room and room.country else ""
    location = ", ".join(filter(None, [hotel_city, hotel_country]))
    if location:
        y = _draw_key_value(c, "Location", location, y)
    y -= 5
    y = _draw_divider(c, y)

    # ── Stay dates ───────────────────────────────────────────────────
    y = _draw_section_title(c, "Stay Dates", y)
    y = _draw_key_value(c, "Check-in", _safe_date(booking.check_in), y)
    y = _draw_key_value(c, "Check-out", _safe_date(booking.check_out), y)
    y = _draw_key_value(c, "Duration", f"{booking.nights} night(s)", y)

    # Check-in/out times from partner hotel
    check_in_time = hotel.check_in_time if hotel and hotel.check_in_time else "14:00"
    check_out_time = hotel.check_out_time if hotel and hotel.check_out_time else "11:00"
    y = _draw_key_value(c, "Check-in Time", check_in_time, y)
    y = _draw_key_value(c, "Check-out Time", check_out_time, y)
    y -= 5
    y = _draw_divider(c, y)

    # ── Status ───────────────────────────────────────────────────────
    y = _draw_section_title(c, "Booking Status", y)
    booking_status = booking.status.value if booking.status else "N/A"
    payment_status = booking.payment_status.value if booking.payment_status else "N/A"
    y = _draw_key_value(c, "Booking Status", booking_status.upper(), y)
    y = _draw_key_value(c, "Payment Status", payment_status.upper(), y)
    y -= 5
    y = _draw_divider(c, y)

    # ── Hotel contact ────────────────────────────────────────────────
    y = _draw_section_title(c, "Hotel Contact", y)
    support_email = (
        hotel.support_email
        if hotel and hotel.support_email
        else "support@stayvora.co.in"
    )
    y = _draw_key_value(c, "Support Email", support_email, y)
    if hotel and hotel.support_phone:
        y = _draw_key_value(c, "Support Phone", hotel.support_phone, y)
    y -= 5
    y = _draw_divider(c, y)

    # ── Special requests ─────────────────────────────────────────────
    if booking.special_requests:
        y = _draw_section_title(c, "Special Requests", y)
        c.setFillColor(BRAND_TEXT)
        c.setFont("Helvetica", 9)
        # Wrap long text
        text = booking.special_requests[:300]
        c.drawString(LEFT_MARGIN, y, text)
        y -= 20
        y = _draw_divider(c, y)

    # ── Important notes ──────────────────────────────────────────────
    y = _draw_section_title(c, "Important Notes", y)
    c.setFillColor(BRAND_TEXT)
    c.setFont("Helvetica", 8)
    notes = [
        "Please present this voucher at the hotel reception during check-in.",
        "A valid government-issued photo ID is required at check-in.",
        "Early check-in and late check-out are subject to availability.",
        "For cancellation or changes, please contact Stayvora support.",
    ]
    for note in notes:
        c.drawString(LEFT_MARGIN + 10, y, f"•  {note}")
        y -= 13

    # ── Footer ───────────────────────────────────────────────────────
    c.setFillColor(BRAND_MUTED)
    c.setFont("Helvetica", 7)
    c.drawCentredString(
        PAGE_WIDTH / 2, 40,
        "This voucher is electronically generated and valid without signature.",
    )
    c.drawCentredString(
        PAGE_WIDTH / 2, 30,
        "Stayvora | support@stayvora.co.in | www.stayvora.co.in",
    )

    c.showPage()
    c.save()
    return buf.getvalue()
