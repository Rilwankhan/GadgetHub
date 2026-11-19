import re
from datetime import datetime
from decimal import Decimal

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.http import (
    FileResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotAllowed,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.html import format_html, format_html_join
from django.urls import path, reverse
from django.utils import timezone

from .models import (
    Category,
    CustomerProfile,
    Order,
    OrderItem,
    OrderTrackingEvent,
    PaymentTransaction,
    Product,
)
from .pdf_reports import build_monthly_sales_report, build_order_details_report


def can_view_order_data(user):
    return user.has_perm("home.view_order") or user.has_perm("home.change_order")


def record_order_status_change(order, updated_by):
    return OrderTrackingEvent.objects.create(
        order=order,
        status=order.status,
        description=f"Your order is now {order.get_status_display().lower()}.",
        updated_by=updated_by,
    )


class GadgetHubAdminSite(admin.AdminSite):
    site_header = "GadgetHub Administration"
    site_title = "GadgetHub Admin"
    index_title = "Store management"
    index_template = "home/admin/index.html"
    login_template = "home/login.html"

    def each_context(self, request):
        context = super().each_context(request)
        context["admin_path"] = request.path
        context["dashboard_section"] = request.GET.get("section", "home")
        return context

    def get_urls(self):
        custom_urls = [
            path(
                "order-updates/",
                self.admin_view(self.order_updates),
                name="order_updates",
            ),
            path(
                "order-updates/<int:order_id>/advance/",
                self.admin_view(self.advance_order),
                name="order_advance",
            ),
            path(
                "reports/monthly-sales/",
                self.admin_view(self.monthly_sales_report),
                name="monthly_sales_report",
            ),
            path(
                "orders/<int:order_id>/download-pdf/",
                self.admin_view(self.order_details_pdf),
                name="order_details_pdf",
            ),
        ]
        return custom_urls + super().get_urls()

    def index(self, request, extra_context=None):
        dashboard_orders = Order.objects.exclude(
            status=Order.Status.CANCELLED
        ).filter(
            Q(payment_method=Order.PaymentMethod.CASH_ON_DELIVERY)
            | Q(payment_status=Order.PaymentStatus.PAID)
        )
        store_summary = {
            "user_count": get_user_model().objects.count(),
            "order_count": dashboard_orders.count(),
            "pending_order_count": dashboard_orders.filter(
                status=Order.Status.PENDING
            ).count(),
            "product_count": Product.objects.count(),
            "active_product_count": Product.objects.filter(is_active=True).count(),
            "category_count": Category.objects.count(),
            "paid_total": dashboard_orders.filter(
                payment_status=Order.PaymentStatus.PAID
            ).aggregate(total=Sum("total"))["total"] or Decimal("0.00"),
            "outstanding_total": dashboard_orders.filter(
                payment_status__in=[
                    Order.PaymentStatus.UNPAID,
                    Order.PaymentStatus.PENDING,
                ]
            ).aggregate(total=Sum("total"))["total"] or Decimal("0.00"),
            "razorpay_payment_count": PaymentTransaction.objects.count(),
            "failed_payment_count": PaymentTransaction.objects.filter(
                status=PaymentTransaction.Status.FAILED
            ).count(),
            "successful_payment_count": PaymentTransaction.objects.filter(
                status=PaymentTransaction.Status.PAID
            ).count(),
            "pending_payment_count": PaymentTransaction.objects.filter(
                status=PaymentTransaction.Status.CREATED
            ).count(),
            "refunded_order_count": Order.objects.filter(
                payment_status=Order.PaymentStatus.REFUNDED
            ).count(),
            "current_month": timezone.localdate().strftime("%Y-%m"),
        }
        context = {
            **(extra_context or {}),
            "store_summary": store_summary,
            "can_view_orders": can_view_order_data(request.user),
            "dashboard_section": request.GET.get("section", "home"),
        }
        return super().index(request, extra_context=context)

    def monthly_sales_report(self, request):
        if not can_view_order_data(request.user):
            return HttpResponseForbidden(
                "You do not have permission to view order reports."
            )

        month_value = request.GET.get(
            "month", timezone.localdate().strftime("%Y-%m")
        )
        if not re.fullmatch(r"\d{4}-\d{2}", month_value):
            return HttpResponseBadRequest("Month must use the YYYY-MM format.")
        try:
            month = datetime.strptime(month_value, "%Y-%m")
        except ValueError:
            return HttpResponseBadRequest("Month must use the YYYY-MM format.")

        start = timezone.make_aware(month.replace(day=1))
        if month.month == 12:
            next_month = month.replace(year=month.year + 1, month=1, day=1)
        else:
            next_month = month.replace(month=month.month + 1, day=1)
        end = timezone.make_aware(next_month)
        orders = (
            Order.objects.filter(created_at__gte=start, created_at__lt=end)
            .select_related("user")
            .prefetch_related("items", "payment_transactions")
            .order_by("created_at", "pk")
        )
        report = build_monthly_sales_report(month, orders)
        return FileResponse(
            report,
            as_attachment=True,
            filename=f"gadgethub-monthly-sales-{month_value}.pdf",
            content_type="application/pdf",
        )

    def order_details_pdf(self, request, order_id):
        if not can_view_order_data(request.user):
            return HttpResponseForbidden(
                "You do not have permission to view order details."
            )

        order = get_object_or_404(
            Order.objects.select_related("user").prefetch_related(
                "items", "payment_transactions", "tracking_events"
            ),
            pk=order_id,
        )
        report = build_order_details_report(order)
        return FileResponse(
            report,
            as_attachment=True,
            filename=f"gadgethub-order-{order.pk}.pdf",
            content_type="application/pdf",
        )

    def order_updates(self, request):
        if not can_view_order_data(request.user):
            return HttpResponseForbidden(
                "You do not have permission to view orders."
            )

        orders = (
            Order.objects.select_related("user")
            .prefetch_related("items")
            .exclude(status=Order.Status.CANCELLED)
            .filter(
                Q(payment_method=Order.PaymentMethod.CASH_ON_DELIVERY)
                | Q(payment_status=Order.PaymentStatus.PAID)
            )
        )
        context = {
            **self.each_context(request),
            "title": "Order updates",
            "orders": orders,
        }
        return render(request, "home/admin/order_updates.html", context)

    def advance_order(self, request, order_id):
        if not request.user.has_perm("home.change_order"):
            return HttpResponseForbidden(
                "You do not have permission to update orders."
            )
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])

        expected_status = request.POST.get("expected_status")
        with transaction.atomic():
            order = get_object_or_404(
                Order.objects.select_for_update(),
                pk=order_id,
            )
            if expected_status != order.status:
                messages.error(
                    request,
                    f"Order #{order.pk} changed. Refresh the page before updating it.",
                )
                return redirect("admin:order_updates")

            next_status = order.next_status
            if next_status is None:
                messages.error(
                    request,
                    f"Order #{order.pk} has no further delivery status.",
                )
                return redirect("admin:order_updates")

            order.status = next_status
            order.save(update_fields=["status"])
            record_order_status_change(order, request.user)

        messages.success(
            request,
            f"Order #{order.pk} is now {order.get_status_display().lower()}.",
        )
        return redirect("admin:order_updates")


gadgethub_admin_site = GadgetHubAdminSite(name="admin")


class CustomerProfileInline(admin.StackedInline):
    model = CustomerProfile
    can_delete = False
    extra = 1
    max_num = 1


class GadgetHubUserAdmin(UserAdmin):
    list_display = [
        "username", "email", "first_name", "last_name", "is_active",
        "is_staff", "date_joined",
    ]
    list_filter = ["is_active", "is_staff", "is_superuser", "groups", "date_joined"]
    search_fields = ["username", "email", "first_name", "last_name"]
    ordering = ["-date_joined"]
    readonly_fields = ["last_login", "date_joined"]
    inlines = [CustomerProfileInline]

class CategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "product_count"]
    search_fields = ["name"]

    @admin.display(description="Products")
    def product_count(self, category):
        return category.products.count()


class ProductAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "price", "is_active"]
    list_filter = ["category", "is_active"]
    search_fields = ["name", "description", "category__name"]
    list_editable = ["price", "is_active"]
    list_select_related = ["category"]
    list_per_page = 50


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = [
        "product", "product_name", "unit_price", "quantity", "line_total",
    ]
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Line total")
    def line_total(self, item):
        if item.unit_price is None or item.quantity is None:
            return "-"
        return item.unit_price * item.quantity


class OrderTrackingEventInline(admin.TabularInline):
    model = OrderTrackingEvent
    fields = ["status", "description", "updated_by", "created_at"]
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class PaymentTransactionInline(admin.TabularInline):
    model = PaymentTransaction
    fields = [
        "razorpay_order_id",
        "razorpay_payment_id",
        "status",
        "amount",
        "currency",
        "error_code",
        "error_description",
        "created_at",
    ]
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class OrderAdminForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        status_field = self.fields.get("status")
        if status_field is None or not self.instance.pk:
            return

        current_status = self.instance.status
        choices = [
            (current_status, f"{self.instance.get_status_display()} (current)")
        ]
        if self.instance.next_status:
            next_status_label = self.instance.Status(self.instance.next_status).label
            choices.append(
                (self.instance.next_status, f"Advance to {next_status_label}")
            )
        status_field.choices = choices
        status_field.label = "Order status / tracking"
        status_field.help_text = "Orders can only move forward one delivery stage at a time."

    def clean_status(self):
        status = self.cleaned_data["status"]
        if status not in (self.instance.status, self.instance.next_status):
            raise ValidationError("Move the order forward one delivery stage at a time.")
        return status


class OrderAdmin(admin.ModelAdmin):
    form = OrderAdminForm
    actions = ["cancel_selected_orders"]
    fieldsets = [
        (
            "Customer & contact",
            {
                "fields": [("user", "full_name"), ("email", "phone")],
                "classes": ["gadgethub-order-fieldset"],
            },
        ),
        (
            "Delivery destination",
            {
                "fields": [
                    "address",
                    ("location_latitude", "location_longitude"),
                    "delivery_map_link",
                ],
                "classes": ["gadgethub-order-fieldset", "wide"],
            },
        ),
        (
            "Order & payment",
            {
                "fields": [
                    ("status", "payment_status"),
                    ("payment_method", "total"),
                    "created_at",
                    "download_order_pdf",
                ],
                "classes": ["gadgethub-order-fieldset"],
            },
        ),
    ]
    list_display = [
        "id",
        "download_order_pdf",
        "customer_details",
        "order_items_summary",
        "delivery_location",
        "order_status_display",
        "payment_method_display",
        "payment_status_display",
        "total",
        "created_at",
    ]
    list_filter = ["status", "payment_method", "payment_status", "created_at"]
    search_fields = [
        "full_name",
        "email",
        "phone",
        "address",
        "user__username",
        "user__email",
    ]
    list_select_related = ["user"]
    readonly_fields = [
        "user", "full_name", "email", "phone", "address", "payment_method",
        "payment_status", "total",
        "location_latitude", "location_longitude", "delivery_map_link",
        "created_at", "download_order_pdf",
    ]
    inlines = [
        OrderItemInline,
        PaymentTransactionInline,
        OrderTrackingEventInline,
    ]
    date_hierarchy = "created_at"
    list_per_page = 50

    class Media:
        css = {"all": ("home/admin_orders.css?v=20261121-2",)}

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .exclude(status=Order.Status.CANCELLED)
            .filter(
                Q(payment_method=Order.PaymentMethod.CASH_ON_DELIVERY)
                | Q(payment_status=Order.PaymentStatus.PAID)
            )
            .prefetch_related("items", "payment_transactions")
        )

    @admin.display(description="Order PDF")
    def download_order_pdf(self, order):
        url = reverse("admin:order_details_pdf", args=[order.pk])
        return format_html(
            '<a href="{}">Download Order Details (PDF)</a>',
            url,
        )

    @admin.display(description="Customer & contact")
    def customer_details(self, order):
        return format_html(
            '<div class="order-customer-name">{}</div>'
            '<div>{}</div><div>{}</div>'
            '<small>Account: {}</small>',
            order.full_name,
            order.phone,
            order.email,
            order.user.get_username(),
        )

    @admin.display(description="Items")
    def order_items_summary(self, order):
        items = format_html_join(
            "",
            '<div class="order-item-summary"><span>{}</span>'
            '<small>Qty: {}</small></div>',
            (
                (item.product_name, item.quantity)
                for item in order.items.all()
            ),
        )
        return items or format_html(
            '<span class="order-empty-note">{}</span>',
            "No items recorded",
        )

    @admin.display(description="Delivery destination")
    def delivery_location(self, order):
        if order.location_latitude is None or order.location_longitude is None:
            return format_html(
                '<div class="order-delivery-address">{}</div>'
                '<small class="order-location-note">GPS pin not provided</small>',
                order.address,
            )

        map_url = (
            "https://www.google.com/maps/search/?api=1&query="
            f"{order.location_latitude},{order.location_longitude}"
        )
        return format_html(
            '<div class="order-delivery-address">{}</div>'
            '<a class="order-map-link" href="{}" target="_blank" '
            'rel="noopener noreferrer">Open delivery map ↗</a>'
            '<small class="order-location-note">{}, {}</small>',
            order.address,
            map_url,
            order.location_latitude,
            order.location_longitude,
        )

    @admin.display(description="Map")
    def delivery_map_link(self, order):
        if order.location_latitude is None or order.location_longitude is None:
            return "GPS pin not provided"

        map_url = (
            "https://www.google.com/maps/search/?api=1&query="
            f"{order.location_latitude},{order.location_longitude}"
        )
        return format_html(
            '<a class="order-map-link" href="{}" target="_blank" '
            'rel="noopener noreferrer">Open delivery map ↗</a>',
            map_url,
        )

    @admin.display(description="Order status", ordering="status")
    def order_status_display(self, order):
        return format_html(
            '<span class="order-status-badge">{}</span>',
            order.get_status_display(),
        )

    @admin.display(description="Payment method", ordering="payment_method")
    def payment_method_display(self, order):
        return order.get_payment_method_display()

    @admin.display(description="Payment status", ordering="payment_status")
    def payment_status_display(self, order):
        return format_html(
            '<span class="payment-status-badge">{}</span>',
            order.get_payment_status_display(),
        )

    def get_readonly_fields(self, request, obj=None):
        readonly_fields = list(super().get_readonly_fields(request, obj))
        if not self.has_change_permission(request, obj) or (
            obj and not obj.next_status
        ):
            readonly_fields.append("status")
        return readonly_fields

    def save_model(self, request, obj, form, change):
        previous_status = (
            Order.objects.only("status").get(pk=obj.pk).status if change else None
        )
        super().save_model(request, obj, form, change)
        if previous_status and obj.status != previous_status:
            record_order_status_change(obj, request.user)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        # Orders are kept as customer and payment history. Cancel them through
        # the explicit admin action instead of deleting their related records.
        return False

    @admin.action(description="Cancel selected orders")
    def cancel_selected_orders(self, request, queryset):
        cancelled_count = 0
        with transaction.atomic():
            orders = Order.objects.select_for_update().filter(pk__in=queryset.values("pk"))
            for order in orders:
                if order.status == Order.Status.CANCELLED:
                    continue
                order.status = Order.Status.CANCELLED
                order.save(update_fields=["status"])
                OrderTrackingEvent.objects.create(
                    order=order,
                    status=Order.Status.CANCELLED,
                    description="An administrator cancelled this order.",
                    updated_by=request.user,
                )
                cancelled_count += 1
        self.message_user(
            request,
            f"{cancelled_count} order(s) cancelled. Customers can see the cancellation in their order history.",
            messages.SUCCESS,
        )


class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "order",
        "razorpay_order_id",
        "razorpay_payment_id",
        "status",
        "amount",
        "currency",
        "created_at",
    ]
    list_filter = ["status", "currency", "created_at"]
    search_fields = [
        "razorpay_order_id",
        "razorpay_payment_id",
        "order__full_name",
        "order__email",
    ]
    list_select_related = ["order"]
    readonly_fields = [
        "order",
        "razorpay_order_id",
        "razorpay_payment_id",
        "signature",
        "amount",
        "currency",
        "status",
        "error_code",
        "error_description",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"
    list_per_page = 50

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


gadgethub_admin_site.register(get_user_model(), GadgetHubUserAdmin)
gadgethub_admin_site.register(Group, GroupAdmin)
gadgethub_admin_site.register(Category, CategoryAdmin)
gadgethub_admin_site.register(Product, ProductAdmin)
gadgethub_admin_site.register(Order, OrderAdmin)
gadgethub_admin_site.register(PaymentTransaction, PaymentTransactionAdmin)
