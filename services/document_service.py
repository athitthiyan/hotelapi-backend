from __future__ import annotations

from io import BytesIO
from typing import Iterable

import models


def invoice_number_for_booking(booking: models.Booking) -> str:
    return f"INV-{booking.booking_ref}"


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_simple_pdf(lines: Iterable[str]) -> bytes:
    content_lines = ["BT", "/F1 11 Tf", "50 790 Td"]
    first = True
    for line in lines:
        if not first:
            content_lines.append("0 -16 Td")
        content_lines.append(f"({_escape_pdf_text(line)}) Tj")
        first = False
    content_lines.append("ET")
    stream_text = "\n".join(content_lines).encode("latin-1", errors="replace")

    objects = []
    objects.append(b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
    objects.append(b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n")
    objects.append(
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n"
    )
    objects.append(b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")
    objects.append(
        f"5 0 obj << /Length {len(stream_text)} >> stream\n".encode("latin-1")
        + stream_text
        + b"\nendstream endobj\n"
    )

    buffer = BytesIO()
    buffer.write(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(buffer.tell())
        buffer.write(obj)

    xref_offset = buffer.tell()
    buffer.write(f"xref\n0 {len(offsets)}\n".encode("latin-1"))
    buffer.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        buffer.write(f"{offset:010d} 00000 n \n".encode("latin-1"))
    buffer.write(
        (
            f"trailer << /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF"
        ).encode("latin-1")
    )
    return buffer.getvalue()


def build_invoice_pdf(booking: models.Booking) -> bytes:
    room = booking.room
    hotel = room.partner_hotel if room else None
    refund_amount = booking.total_amount if booking.payment_status == models.PaymentStatus.REFUNDED else 0.0
    lines = [
        "Stayvora Tax Invoice",
        f"Invoice Number: {invoice_number_for_booking(booking)}",
        f"Booking Reference: {booking.booking_ref}",
        f"Customer: {booking.user_name}",
        f"Customer Email: {booking.email}",
        f"Hotel: {room.hotel_name if room else 'Stayvora Hotel'}",
        f"Room: {room.room_type_name if room and room.room_type_name else room.room_type.value if room else 'Room'}",
        f"Check-in: {booking.check_in.date().isoformat()}",
        f"Check-out: {booking.check_out.date().isoformat()}",
        f"Nights: {booking.nights}",
        f"Room Rate: {booking.room_rate:.2f}",
        f"Taxes: {booking.taxes:.2f}",
        f"Service Fee: {booking.service_fee:.2f}",
        f"Total Paid: {booking.total_amount:.2f}",
        f"Refund Amount: {refund_amount:.2f}",
        f"GST Number: {hotel.gst_number if hotel and hotel.gst_number else 'Not provided'}",
    ]
    return _build_simple_pdf(lines)


def build_voucher_pdf(booking: models.Booking) -> bytes:
    room = booking.room
    hotel = room.partner_hotel if room else None
    lines = [
        "Stayvora Booking Voucher",
        f"Booking Reference: {booking.booking_ref}",
        f"Guest: {booking.user_name}",
        f"Hotel: {room.hotel_name if room else 'Stayvora Hotel'}",
        f"Room: {room.room_type_name if room and room.room_type_name else room.room_type.value if room else 'Room'}",
        f"Check-in: {booking.check_in.date().isoformat()}",
        f"Check-out: {booking.check_out.date().isoformat()}",
        f"Guests: {booking.guests}",
        f"Booking Status: {booking.status.value}",
        f"Payment Status: {booking.payment_status.value}",
        f"Hotel Support: {hotel.support_email if hotel and hotel.support_email else 'support@stayvora.co.in'}",
        f"Hotel City: {room.city if room and room.city else ''}",
    ]
    return _build_simple_pdf(lines)
