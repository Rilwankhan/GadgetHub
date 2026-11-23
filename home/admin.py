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
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import CharField, F, Q, Sum, Value
from django.db.models.functions import Coalesce, NullIf
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

from .activity import record_order_status_change
from .forms import StaffCreationForm, StaffUpdateForm
from .inventory import (
    StockUnavailable,
    order_stock_available,
    reserve_order_stock,
    restore_order_stock,
)
from .models import (
    Category,
    CustomerProfile,
    Order,
    OrderItem,
    OrderPaymentEvent,
    OrderTrackingEvent,
    PaymentTransaction,
    Product,
)
from .pdf_reports import build_monthly_sales_report, build_order_details_report
from .roles import STAFF_GROUP_NAME

def can_view_order_data(user):
    return user.has_perm("home.view_order") or user.has_perm("home.change_order")


def admin_visible_payment_transactions(queryset):
    return queryset.exclude(
        Q(status=PaymentTransaction.Status.CANCELLED)
        | Q(order__status=Order.Status.CANCELLED)
        | Q(order__payment_status=Order.PaymentStatus.CANCELLED)
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
                "staff-actions/",
                self.admin_view(self.staff_actions),
                name="staff_actions",
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
            path(
                "staff/",
                self.admin_view(self.staff_management),
                name="staff_management",
            ),
            path(
                "staff/<int:user_id>/edit/",
                self.admin_view(self.edit_staff),
                name="staff_edit",
            ),
            path(
                "staff/<int:user_id>/toggle-active/",
                self.admin_view(self.toggle_staff_active),
                name="staff_toggle_active",
            ),
        ]
        return custom_urls + super().get_urls()

    def index(self, request, extra_context=None):
        dashboard_orders = Order.objects.exclude(
            Q(status=Order.Status.CANCELLED)
            | Q(payment_status=Order.PaymentStatus.CANCELLED)
        ).filter(
            Q(payment_method=Order.PaymentMethod.CASH_ON_DELIVERY)
            | Q(payment_status=Order.PaymentStatus.PAID)
        )
        payment_transactions = admin_visible_payment_transactions(
            PaymentTransaction.objects.all()
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
            "razorpay_payment_count": payment_transactions.count(),
            "failed_payment_count": payment_transactions.filter(
                status=PaymentTransaction.Status.FAILED
            ).count(),
            "successful_payment_count": payment_transactions.filter(
                status=PaymentTransaction.Status.PAID
            ).count(),
            "pending_payment_count": payment_transactions.filter(
                status=PaymentTransaction.Status.CREATED
            ).count(),
            "refunded_order_count": Order.objects.filter(
                payment_status=Order.PaymentStatus.REFUNDED
            )
            .exclude(
                Q(status=Order.Status.CANCELLED)
                | Q(payment_status=Order.PaymentStatus.CANCELLED)
            )
            .count(),
            "current_month": timezone.localdate().strftime("%Y-%m"),
        }
        context = {
            **(extra_context or {}),
            "store_summary": store_summary,
            "can_view_orders": can_view_order_data(request.user),
            "dashboard_section": request.GET.get("section", "home"),
        }
        return super().index(request, extra_context=context)

    def staff_management(self, request):
        if not request.user.is_superuser:
            return HttpResponseForbidden(
                "Only administrators can manage staff accounts."
            )
        form = StaffCreationForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            staff_user = form.save()
            messages.success(
                request,
                f"Staff account for {staff_user.get_full_name()} was created.",
            )
            return redirect("admin:staff_management")
        staff_members = (
            get_user_model()
            .objects.filter(groups__name=STAFF_GROUP_NAME)
            .order_by("first_name", "last_name", "username")
        )
        return render(
            request,
            "home/admin/staff_management.html",
            {
                **self.each_context(request),
                "title": "Staff management",
                "form": form,
                "staff_members": staff_members,
            },
        )

    def edit_staff(self, request, user_id):
        if not request.user.is_superuser:
            return HttpResponseForbidden(
                "Only administrators can manage staff accounts."
            )
        staff_user = get_object_or_404(
            get_user_model().objects.filter(groups__name=STAFF_GROUP_NAME),
            pk=user_id,
        )
        form = StaffUpdateForm(request.POST or None, instance=staff_user)
        if request.method == "POST" and form.is_valid():
            form.save()
            messages.success(request, "Staff account details were updated.")
            return redirect("admin:staff_management")
        return render(
            request,
            "home/admin/staff_edit.html",
            {
                **self.each_context(request),
                "title": "Edit staff account",
                "form": form,
                "staff_user": staff_user,
            },
        )

    def toggle_staff_active(self, request, user_id):
        if not request.user.is_superuser:
            return HttpResponseForbidden(
                "Only administrators can manage staff accounts."
            )
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        staff_user = get_object_or_404(
            get_user_model().objects.filter(groups__name=STAFF_GROUP_NAME),
            pk=user_id,
        )
        staff_user.is_active = not staff_user.is_active
        staff_user.save(update_fields=["is_active"])
        state = "activated" if staff_user.is_active else "deactivated"
        messages.success(
            request,
            f"Staff account for {staff_user.get_full_name()} was {state}.",
        )
        return redirect("admin:staff_management")

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
            .exclude(
                Q(status=Order.Status.CANCELLED)
                | Q(payment_status=Order.PaymentStatus.CANCELLED)
            )
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
            Order.objects.exclude(
                Q(status=Order.Status.CANCELLED)
                | Q(payment_status=Order.PaymentStatus.CANCELLED)
            )
            .select_related("user")
            .prefetch_related("items", "payment_transactions", "tracking_events"),
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
            .exclude(
                Q(status=Order.Status.CANCELLED)
                | Q(payment_status=Order.PaymentStatus.CANCELLED)
            )
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

    def staff_actions(self, request):
        if not can_view_order_data(request.user):
            return HttpResponseForbidden(
                "You do not have permission to view staff activity."
            )

        actor_name = Coalesce(
            NullIf("updated_by_name", Value("")),
            "updated_by__username",
            Value("Former staff member"),
            output_field=CharField(),
        )
        status_events = (
            OrderTrackingEvent.objects.filter(
                updated_by__groups__name=STAFF_GROUP_NAME
            )
            .exclude(
                Q(order__status=Order.Status.CANCELLED)
                | Q(order__payment_status=Order.PaymentStatus.CANCELLED)
            )
            .order_by()
            .annotate(
                activity_type=Value("order_status", output_field=CharField()),
                actor_name=actor_name,
                event_status=F("status"),
            )
            .values(
                "pk",
                "order_id",
                "created_at",
                "activity_type",
                "actor_name",
                "previous_status",
                "event_status",
            )
        )
        payment_events = (
            OrderPaymentEvent.objects.filter(
                updated_by__groups__name=STAFF_GROUP_NAME
            )
            .exclude(
                Q(order__status=Order.Status.CANCELLED)
                | Q(order__payment_status=Order.PaymentStatus.CANCELLED)
            )
            .order_by()
            .annotate(
                activity_type=Value("cod_payment", output_field=CharField()),
                actor_name=actor_name,
                event_status=F("new_status"),
            )
            .values(
                "pk",
                "order_id",
                "created_at",
                "activity_type",
                "actor_name",
                "previous_status",
                "event_status",
            )
        )
        activities_page = Paginator(
            status_events.union(payment_events).order_by("-created_at", "-pk"),
            30,
        ).get_page(request.GET.get("page"))
        activities = list(activities_page.object_list)
        orders_by_id = {
            order.pk: order
            for order in Order.objects.filter(
                pk__in={activity["order_id"] for activity in activities}
            ).prefetch_related("items")
        }
        order_status_labels = dict(Order.Status.choices)
        payment_status_labels = dict(Order.PaymentStatus.choices)
        for activity in activities:
            activity["order"] = orders_by_id[activity["order_id"]]
            labels = (
                payment_status_labels
                if activity["activity_type"] == "cod_payment"
                else order_status_labels
            )
            activity["previous_status_label"] = labels.get(
                activity["previous_status"],
                activity["previous_status"] or "Order placed",
            )
            activity["event_status_label"] = labels.get(
                activity["event_status"], activity["event_status"]
            )

        return render(
            request,
            "home/admin/staff_actions.html",
            {
                **self.each_context(request),
                "title": "Staff actions",
                "activities": activities,
                "activities_page": activities_page,
            },
        )

    def advance_order(self, request, order_id):
        if not request.user.has_perm("home.change_order"):
            return HttpResponseForbidden(
                "You do not have permission to update orders."
            )
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])

        expected_status = request.POST.get("expected_status")
        try:
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

                if not order.stock_deducted:
                    reserve_order_stock(order)

                previous_status = order.status
                order.status = next_status
                order.save(update_fields=["status"])
                record_order_status_change(order, previous_status, request.user)
        except StockUnavailable as error:
            messages.error(
                request,
                f"Order #{order_id} cannot be progressed because inventory is unavailable: {error}",
            )
            return redirect("admin:order_updates")

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
    list_display = ["name", "category", "price", "stock_quantity", "is_active"]
    list_filter = ["category", "is_active"]
    search_fields = ["name", "description", "category__name"]
    list_editable = ["price", "stock_quantity", "is_active"]
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
    fields = [
        "previous_status",
        "status",
        "description",
        "updated_by_name",
        "updated_by",
        "created_at",
    ]
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class OrderPaymentEventInline(admin.TabularInline):
    model = OrderPaymentEvent
    fields = [
        "previous_status",
        "new_status",
        "updated_by_name",
        "updated_by",
        "created_at",
    ]
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

    def get_queryset(self, request):
        return admin_visible_payment_transactions(super().get_queryset(request))


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
        if (
            status != self.instance.status
            and not self.instance.stock_deducted
            and not order_stock_available(self.instance)
        ):
            raise ValidationError(
                "There is not enough available stock to advance this order."
            )
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
        OrderPaymentEventInline,
    ]
    date_hierarchy = "created_at"
    list_per_page = 50

    class Media:
        css = {"all": ("home/admin_orders.css?v=20261121-2",)}

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .exclude(
                Q(status=Order.Status.CANCELLED)
                | Q(payment_status=Order.PaymentStatus.CANCELLED)
            )
            .filter(
                Q(payment_status=Order.PaymentStatus.REFUNDED)
                | Q(payment_method=Order.PaymentMethod.CASH_ON_DELIVERY)
                | Q(payment_status=Order.PaymentStatus.PAID)
            )
            .prefetch_related("items", "payment_transactions")
        )

    def changelist_view(self, request, extra_context=None):
        if (
            request.method == "GET"
            and request.GET.get("payment_status__exact")
            == Order.PaymentStatus.REFUNDED
            and not self.get_queryset(request)
            .filter(payment_status=Order.PaymentStatus.REFUNDED)
            .exists()
        ):
            self.message_user(request, "No refunded orders found.", messages.INFO)
        return super().changelist_view(request, extra_context=extra_context)

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
        if previous_status and obj.status != previous_status and not obj.stock_deducted:
            locked_order = Order.objects.select_for_update().get(pk=obj.pk)
            reserve_order_stock(locked_order)
            obj.stock_deducted = locked_order.stock_deducted
        super().save_model(request, obj, form, change)
        if previous_status and obj.status != previous_status:
            record_order_status_change(obj, previous_status, request.user)

    def changeform_view(
        self, request, object_id=None, form_url="", extra_context=None
    ):
        try:
            return super().changeform_view(
                request,
                object_id=object_id,
                form_url=form_url,
                extra_context=extra_context,
            )
        except StockUnavailable as error:
            self.message_user(
                request,
                f"Order stock could not be reserved: {error}",
                messages.ERROR,
            )
            return redirect(request.path)

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
            orders = (
                Order.objects.select_for_update()
                .filter(pk__in=queryset.values("pk"))
                .order_by("pk")
            )
            for order in orders:
                if order.status == Order.Status.CANCELLED:
                    continue
                previous_status = order.status
                order.status = Order.Status.CANCELLED
                order.save(update_fields=["status"])
                restore_order_stock(order)
                record_order_status_change(
                    order,
                    previous_status,
                    request.user,
                    "An administrator cancelled this order.",
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

    def get_queryset(self, request):
        return admin_visible_payment_transactions(
            super().get_queryset(request)
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        empty_messages = {
            PaymentTransaction.Status.PAID: "No successful payments found.",
            PaymentTransaction.Status.CREATED: "No pending payments found.",
            PaymentTransaction.Status.FAILED: "No failed payments found.",
        }
        status = request.GET.get("status__exact")
        if (
            request.method == "GET"
            and status in empty_messages
            and not self.get_queryset(request).filter(status=status).exists()
        ):
            self.message_user(request, empty_messages[status], messages.INFO)
        return super().changelist_view(request, extra_context=extra_context)


gadgethub_admin_site.register(get_user_model(), GadgetHubUserAdmin)
gadgethub_admin_site.register(Group, GroupAdmin)
gadgethub_admin_site.register(Category, CategoryAdmin)
gadgethub_admin_site.register(Product, ProductAdmin)
gadgethub_admin_site.register(Order, OrderAdmin)
gadgethub_admin_site.register(PaymentTransaction, PaymentTransactionAdmin)
