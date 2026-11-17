from decimal import Decimal

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group
from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Sum

from .models import (
    Category,
    CustomerProfile,
    Order,
    OrderItem,
    OrderTrackingEvent,
    Product,
)


class GadgetHubAdminSite(admin.AdminSite):
    site_header = "GadgetHub Administration"
    site_title = "GadgetHub Admin"
    index_title = "Store management"
    index_template = "home/admin/index.html"
    login_template = "home/login.html"

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
        }
        context = {**(extra_context or {}), "store_summary": store_summary}
        return super().index(request, extra_context=context)


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
    inlines = [OrderItemInline, OrderTrackingEventInline]
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
            OrderTrackingEvent.objects.create(
                order=obj,
                status=obj.status,
                description=f"Your order is now {obj.get_status_display().lower()}.",
                updated_by=request.user,
            )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


gadgethub_admin_site.register(get_user_model(), GadgetHubUserAdmin)
gadgethub_admin_site.register(Group, GroupAdmin)
gadgethub_admin_site.register(Category, CategoryAdmin)
gadgethub_admin_site.register(Product, ProductAdmin)
gadgethub_admin_site.register(Order, OrderAdmin)
