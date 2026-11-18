from decimal import Decimal

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path

from .models import (
    Category,
    CustomerProfile,
    Order,
    OrderItem,
    OrderTrackingEvent,
    PaymentTransaction,
    Product,
)


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
        ]
        return custom_urls + super().get_urls()

    def index(self, request, extra_context=None):
        non_cancelled_orders = Order.objects.exclude(status=Order.Status.CANCELLED)
        store_summary = {
            "user_count": get_user_model().objects.count(),
            "order_count": Order.objects.count(),
            "pending_order_count": Order.objects.filter(
                status=Order.Status.PENDING
            ).count(),
            "product_count": Product.objects.count(),
            "active_product_count": Product.objects.filter(is_active=True).count(),
            "category_count": Category.objects.count(),
            "paid_total": non_cancelled_orders.filter(
                payment_status=Order.PaymentStatus.PAID
            ).aggregate(total=Sum("total"))["total"] or Decimal("0.00"),
            "outstanding_total": non_cancelled_orders.filter(
                payment_status__in=[
                    Order.PaymentStatus.UNPAID,
                    Order.PaymentStatus.PENDING,
                ]
            ).aggregate(total=Sum("total"))["total"] or Decimal("0.00"),
            "razorpay_payment_count": PaymentTransaction.objects.count(),
            "failed_payment_count": PaymentTransaction.objects.filter(
                status=PaymentTransaction.Status.FAILED
            ).count(),
        }
        context = {**(extra_context or {}), "store_summary": store_summary}
        return super().index(request, extra_context=context)

    def order_updates(self, request):
        orders = (
            Order.objects.select_related("user")
            .prefetch_related("items")
            .all()
        )
        context = {
            **self.each_context(request),
            "title": "Order updates",
            "orders": orders,
        }
        return render(request, "home/admin/order_updates.html", context)

    def advance_order(self, request, order_id):
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
    list_display = [
        "id", "full_name", "user", "total", "status", "payment_method",
        "payment_status", "created_at",
    ]
    list_filter = ["status", "payment_method", "payment_status", "created_at"]
    search_fields = ["full_name", "email", "phone", "user__username", "user__email"]
    list_select_related = ["user"]
    readonly_fields = [
        "user", "full_name", "email", "phone", "address", "payment_method",
        "payment_status", "total",
        "location_latitude", "location_longitude", "created_at",
    ]
    inlines = [
        OrderItemInline,
        PaymentTransactionInline,
        OrderTrackingEventInline,
    ]
    date_hierarchy = "created_at"
    list_per_page = 50

    def get_readonly_fields(self, request, obj=None):
        readonly_fields = list(super().get_readonly_fields(request, obj))
        if obj and not obj.next_status:
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
        return False


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
