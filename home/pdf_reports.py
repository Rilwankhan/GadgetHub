from decimal import Decimal
from html import escape
from io import BytesIO

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


NAVY = colors.HexColor("#14213d")
BLUE = colors.HexColor("#2563eb")
CYAN = colors.HexColor("#06b6d4")
INK = colors.HexColor("#273449")
MUTED = colors.HexColor("#64748b")
LINE = colors.HexColor("#dbe3ee")
PALE = colors.HexColor("#f3f6fb")


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "GadgetHubTitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=25,
            textColor=NAVY,
            alignment=TA_LEFT,
            spaceAfter=3 * mm,
        ),
        "subtitle": ParagraphStyle(
            "GadgetHubSubtitle",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=13,
            textColor=MUTED,
            spaceAfter=5 * mm,
        ),
        "section": ParagraphStyle(
            "GadgetHubSection",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14,
            textColor=NAVY,
            spaceBefore=4 * mm,
            spaceAfter=2 * mm,
        ),
        "body": ParagraphStyle(
            "GadgetHubBody",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8,
            leading=11,
            textColor=INK,
        ),
        "small": ParagraphStyle(
            "GadgetHubSmall",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=7,
            leading=9,
            textColor=INK,
        ),
        "label": ParagraphStyle(
            "GadgetHubLabel",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=7,
            leading=9,
            textColor=MUTED,
        ),
        "table_header": ParagraphStyle(
            "GadgetHubTableHeader",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=7,
            leading=9,
            textColor=colors.white,
        ),
        "money": ParagraphStyle(
            "GadgetHubMoney",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=INK,
            alignment=TA_RIGHT,
        ),
    }


def _paragraph(value, style):
    text = escape(str(value if value not in (None, "") else "-"), quote=True)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
    return Paragraph(text, style)


def _money(value, currency="INR"):
    amount = Decimal(str(value or 0))
    return f"{currency} {amount:,.2f}"


def _date(value):
    if not value:
        return "-"
    return timezone.localtime(value).strftime("%d %b %Y, %I:%M %p %Z")


def _table(rows, widths, *, repeat_rows=1, right_aligned=()):
    table = Table(rows, colWidths=widths, repeatRows=repeat_rows, hAlign="LEFT")
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, BLUE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.35, LINE),
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
    ]
    for row_index in range(2, len(rows), 2):
        commands.append(("BACKGROUND", (0, row_index), (-1, row_index), PALE))
    for column in right_aligned:
        commands.append(("ALIGN", (column, 1), (column, -1), "RIGHT"))
    table.setStyle(TableStyle(commands))
    return table


def _table_header(labels, styles):
    return [_paragraph(label, styles["table_header"]) for label in labels]


def _document(buffer, title, pagesize=A4):
    document = SimpleDocTemplate(
        buffer,
        pagesize=pagesize,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=27 * mm,
        bottomMargin=18 * mm,
        title=title,
        author="GadgetHub",
    )
    return document


def _draw_page(canvas, document):
    canvas.saveState()
    page_width, page_height = document.pagesize
    canvas.setFillColor(NAVY)
    canvas.rect(0, page_height - 18 * mm, page_width, 18 * mm, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(16 * mm, page_height - 11.5 * mm, "GADGETHUB")
    canvas.setFillColor(CYAN)
    canvas.setFont("Helvetica", 7)
    canvas.drawRightString(
        page_width - 16 * mm, page_height - 11 * mm, "SMART TECH. BETTER LIFE."
    )
    canvas.setStrokeColor(LINE)
    canvas.line(16 * mm, 12 * mm, page_width - 16 * mm, 12 * mm)
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 7)
    canvas.drawString(16 * mm, 7 * mm, "GadgetHub - generated document")
    canvas.drawRightString(page_width - 16 * mm, 7 * mm, f"Page {document.page}")
    canvas.restoreState()


def _add_document(story, title, pagesize=A4):
    buffer = BytesIO()
    _document(buffer, title, pagesize).build(
        story, onFirstPage=_draw_page, onLaterPages=_draw_page
    )
    buffer.seek(0)
    return buffer


def build_payment_receipt(order, payment):
    styles = _styles()
    story = [
        _paragraph("Payment receipt", styles["title"]),
        _paragraph(
            f"Receipt for order #{order.pk} | Paid on {_date(payment.updated_at)}",
            styles["subtitle"],
        ),
        _paragraph("Customer and delivery", styles["section"]),
    ]
    details = [
        [
            _paragraph("Customer", styles["label"]),
            _paragraph(order.full_name, styles["body"]),
            _paragraph("Email", styles["label"]),
            _paragraph(order.email, styles["body"]),
        ],
        [
            _paragraph("Mobile", styles["label"]),
            _paragraph(order.phone, styles["body"]),
            _paragraph("Delivery address", styles["label"]),
            _paragraph(order.address, styles["body"]),
        ],
    ]
    detail_table = Table(
        details,
        colWidths=[22 * mm, 48 * mm, 25 * mm, 71 * mm],
        hAlign="LEFT",
    )
    detail_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALE),
                ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.extend([detail_table, _paragraph("Items paid", styles["section"])])

    item_rows = [_table_header(["Product", "Qty", "Unit price", "Line total"], styles)]
    for item in order.items.all():
        item_rows.append(
            [
                _paragraph(item.product_name, styles["body"]),
                _paragraph(item.quantity, styles["body"]),
                _paragraph(_money(item.unit_price), styles["money"]),
                _paragraph(_money(item.unit_price * item.quantity), styles["money"]),
            ]
        )
    story.append(
        _table(
            item_rows,
            [76 * mm, 15 * mm, 39 * mm, 36 * mm],
            right_aligned=(1, 2, 3),
        )
    )
    story.extend(
        [
            Spacer(1, 4 * mm),
            _paragraph(f"Order total: {_money(order.total)}", styles["money"]),
            _paragraph("Payment details", styles["section"]),
        ]
    )
    payment_rows = [
        [
            _paragraph("Payment status", styles["label"]),
            _paragraph("Paid", styles["body"]),
            _paragraph("Payment method", styles["label"]),
            _paragraph("Razorpay", styles["body"]),
        ],
        [
            _paragraph("Razorpay payment ID", styles["label"]),
            _paragraph(payment.razorpay_payment_id, styles["body"]),
            _paragraph("Razorpay order ID", styles["label"]),
            _paragraph(payment.razorpay_order_id, styles["body"]),
        ],
        [
            _paragraph("Transaction amount", styles["label"]),
            _paragraph(_money(payment.amount, payment.currency), styles["body"]),
            _paragraph("Receipt reference", styles["label"]),
            _paragraph(f"GH-{order.pk}-{payment.pk}", styles["body"]),
        ],
    ]
    payment_table = Table(
        payment_rows,
        colWidths=[31 * mm, 42 * mm, 31 * mm, 62 * mm],
        hAlign="LEFT",
    )
    payment_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALE),
                ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.extend([payment_table, Spacer(1, 4 * mm)])
    story.append(
        _paragraph(
            "This receipt confirms a payment recorded by GadgetHub. Keep it for your records.",
            styles["small"],
        )
    )
    return _add_document(story, f"GadgetHub receipt - order {order.pk}")


def build_order_details_report(order):
    styles = _styles()
    paid_transaction = next(
        (
            payment
            for payment in order.payment_transactions.all()
            if payment.status == "paid"
        ),
        None,
    )
    is_paid = order.payment_status == "paid"
    if not is_paid:
        amount_paid = Decimal("0.00")
    elif paid_transaction:
        amount_paid = paid_transaction.amount
    else:
        amount_paid = order.total
    amount_currency = paid_transaction.currency if paid_transaction else "INR"
    if order.payment_method == "cash_on_delivery":
        payment_method = "COD"
    elif order.payment_method == "razorpay":
        payment_method = "Online Payment (Razorpay)"
    else:
        payment_method = order.get_payment_method_display()
    if order.payment_status in ("unpaid", "pending"):
        payment_status = "Pending"
    else:
        payment_status = order.get_payment_status_display()

    story = [
        _paragraph("Order details", styles["title"]),
        _paragraph(
            f"Order #{order.pk} | Booked {_date(order.created_at)}",
            styles["subtitle"],
        ),
        _paragraph("Customer and delivery", styles["section"]),
    ]
    customer_rows = [
        [
            _paragraph("Customer name", styles["label"]),
            _paragraph(order.full_name, styles["body"]),
        ],
        [
            _paragraph("Email", styles["label"]),
            _paragraph(order.email, styles["body"]),
        ],
        [
            _paragraph("Mobile number", styles["label"]),
            _paragraph(order.phone, styles["body"]),
        ],
        [
            _paragraph("Complete delivery address", styles["label"]),
            _paragraph(order.address, styles["body"]),
        ],
    ]
    customer_table = Table(
        customer_rows,
        colWidths=[43 * mm, 135 * mm],
        hAlign="LEFT",
    )
    customer_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALE),
                ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.extend(
        [
            customer_table,
            _paragraph("Order and payment summary", styles["section"]),
        ]
    )

    summary_rows = [
        [
            _paragraph("Order ID", styles["label"]),
            _paragraph(f"#{order.pk}", styles["body"]),
            _paragraph("Booking date and time", styles["label"]),
            _paragraph(_date(order.created_at), styles["body"]),
        ],
        [
            _paragraph("Tracking status", styles["label"]),
            _paragraph(order.get_status_display(), styles["body"]),
            _paragraph("Payment method", styles["label"]),
            _paragraph(payment_method, styles["body"]),
        ],
        [
            _paragraph("Payment status", styles["label"]),
            _paragraph(payment_status, styles["body"]),
            _paragraph("Amount paid", styles["label"]),
            _paragraph(_money(amount_paid, amount_currency), styles["body"]),
        ],
    ]
    summary_table = Table(
        summary_rows,
        colWidths=[33 * mm, 55 * mm, 36 * mm, 54 * mm],
        hAlign="LEFT",
    )
    summary_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALE),
                ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.extend(
        [
            summary_table,
            _paragraph("Products", styles["section"]),
        ]
    )

    item_rows = [
        _table_header(["Product name", "Qty", "Unit price", "Line total"], styles)
    ]
    for item in order.items.all():
        item_rows.append(
            [
                _paragraph(item.product_name, styles["body"]),
                _paragraph(item.quantity, styles["body"]),
                _paragraph(_money(item.unit_price), styles["money"]),
                _paragraph(_money(item.unit_price * item.quantity), styles["money"]),
            ]
        )
    if len(item_rows) == 1:
        item_rows.append(
            [
                _paragraph("No products recorded.", styles["body"]),
                _paragraph("-", styles["body"]),
                _paragraph("-", styles["body"]),
                _paragraph("-", styles["body"]),
            ]
        )
    story.extend(
        [
            _table(
                item_rows,
                [82 * mm, 18 * mm, 38 * mm, 40 * mm],
                right_aligned=(1, 2, 3),
            ),
            Spacer(1, 4 * mm),
            _paragraph(f"Order total: {_money(order.total)}", styles["money"]),
        ]
    )
    return _add_document(story, f"GadgetHub order details - {order.pk}")


def build_monthly_sales_report(month, orders):
    styles = _styles()
    order_list = list(orders)
    active_orders = [order for order in order_list if order.status != order.Status.CANCELLED]
    cancelled_orders = [order for order in order_list if order.status == order.Status.CANCELLED]
    refunded_orders = [
        order
        for order in active_orders
        if order.payment_status == order.PaymentStatus.REFUNDED
    ]
    paid_orders = [
        order
        for order in active_orders
        if order.payment_status == order.PaymentStatus.PAID
    ]
    unpaid_orders = [
        order
        for order in active_orders
        if order.payment_status
        not in (order.PaymentStatus.PAID, order.PaymentStatus.REFUNDED)
    ]
    total = lambda selected: sum((order.total for order in selected), Decimal("0.00"))

    story = [
        _paragraph("Monthly sales report", styles["title"]),
        _paragraph(
            f"Orders created in {month.strftime('%B %Y')} | Generated {_date(timezone.now())}",
            styles["subtitle"],
        ),
        _paragraph(
            "Order value includes all orders placed in the selected month. Paid, outstanding, cancelled, and refunded totals are shown separately.",
            styles["body"],
        ),
        Spacer(1, 3 * mm),
    ]
    summary = [
        ["Orders placed", str(len(order_list))],
        ["Order value", _money(total(order_list))],
        ["Paid order value", _money(total(paid_orders))],
        ["Outstanding order value", _money(total(unpaid_orders))],
        ["Cancelled order value", _money(total(cancelled_orders))],
        ["Refunded order value", _money(total(refunded_orders))],
    ]
    summary_rows = [
        [
            _paragraph(label, styles["label"]),
            _paragraph(value, styles["body"]),
        ]
        for label, value in summary
    ]
    summary_table = Table(
        summary_rows,
        colWidths=[51 * mm, 42 * mm],
        hAlign="LEFT",
    )
    summary_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALE),
                ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.extend([_paragraph("Monthly totals", styles["section"]), summary_table])
    story.append(_paragraph("Order details", styles["section"]))
    detail_rows = [
        _table_header(
            ["Order / date", "Customer", "Items", "Delivery address", "Status / payment", "Total"],
            styles,
        )
    ]
    for order in order_list:
        items = "<br/>".join(
            escape(f"{item.quantity} x {item.product_name}", quote=True)
            for item in order.items.all()
        ) or "-"
        detail_rows.append(
            [
                _paragraph(f"#{order.pk}<br/>{_date(order.created_at)}", styles["small"]),
                _paragraph(
                    f"{order.full_name}<br/>{order.email}<br/>{order.phone}",
                    styles["small"],
                ),
                Paragraph(items, styles["small"]),
                _paragraph(order.address, styles["small"]),
                _paragraph(
                    f"{order.get_status_display()}<br/>{order.get_payment_method_display()}<br/>{order.get_payment_status_display()}",
                    styles["small"],
                ),
                _paragraph(_money(order.total), styles["money"]),
            ]
        )
    if len(detail_rows) == 1:
        story.append(_paragraph("No orders were placed in this month.", styles["body"]))
    else:
        story.append(
            _table(
                detail_rows,
                [29 * mm, 47 * mm, 42 * mm, 60 * mm, 53 * mm, 26 * mm],
                right_aligned=(5,),
            )
        )
    return _add_document(
        story,
        f"GadgetHub monthly sales - {month.strftime('%Y-%m')}",
        pagesize=landscape(A4),
    )


