from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class Category(models.Model):
    name = models.CharField(max_length=80, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Product(models.Model):
    name = models.CharField(max_length=160)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="products"
    )
    price = models.DecimalField(max_digits=10, decimal_places=2)
    original_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    description = models.TextField()
    image = models.URLField(max_length=500)
    badge = models.CharField(max_length=40, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class CustomerProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    employee_id = models.CharField(max_length=50, blank=True)
    mobile = models.CharField(max_length=30, blank=True)
    address = models.TextField(blank=True)
    landmark = models.CharField(max_length=160, blank=True)

    def __str__(self):
        return f"Profile for {self.user.username}"


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        SHIPPED = "shipped", "Shipped"
        OUT_FOR_DELIVERY = "out_for_delivery", "Out for delivery"
        DELIVERED = "delivered", "Delivered"
        CANCELLED = "cancelled", "Cancelled"

    class PaymentMethod(models.TextChoices):
        CASH_ON_DELIVERY = "cash_on_delivery", "Cash on delivery"
        ARRANGE_WITH_STORE = "arrange_with_store", "Arrange with store"
        RAZORPAY = "razorpay", "Razorpay"

    class PaymentStatus(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        REFUNDED = "refunded", "Refunded"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders"
    )
    full_name = models.CharField(max_length=160)
    email = models.EmailField()
    phone = models.CharField(max_length=30)
    address = models.TextField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    payment_method = models.CharField(
        max_length=30,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH_ON_DELIVERY,
    )
    payment_status = models.CharField(
        max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID
    )
    total = models.DecimalField(max_digits=12, decimal_places=2)
    location_latitude = models.FloatField(null=True, blank=True)
    location_longitude = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def next_status(self):
        return {
            self.Status.PENDING: self.Status.CONFIRMED,
            self.Status.CONFIRMED: self.Status.SHIPPED,
            self.Status.SHIPPED: self.Status.OUT_FOR_DELIVERY,
            self.Status.OUT_FOR_DELIVERY: self.Status.DELIVERED,
        }.get(self.status)

    @property
    def next_status_label(self):
        if self.next_status is None:
            return None
        return self.Status(self.next_status).label

    @property
    def cancellation_deadline(self):
        return self.created_at + timedelta(hours=48)

    @property
    def can_be_cancelled(self):
        return (
            self.status in (self.Status.PENDING, self.Status.CONFIRMED)
            and timezone.now() <= self.cancellation_deadline
        )

    @property
    def latest_tracking_event(self):
        events = list(self.tracking_events.all())
        return events[-1] if events else None

    @property
    def delivered_at(self):
        if self.status != self.Status.DELIVERED:
            return None
        event = self.latest_tracking_event
        if event and event.status == self.Status.DELIVERED:
            return event.created_at
        return None

    @property
    def cancelled_at(self):
        if self.status != self.Status.CANCELLED:
            return None
        events = list(self.tracking_events.all())
        return next(
            (
                event.created_at
                for event in reversed(events)
                if event.status == self.Status.CANCELLED
            ),
            None,
        )

    @property
    def tracking_steps(self):
        statuses = (
            self.Status.CONFIRMED,
            self.Status.SHIPPED,
            self.Status.OUT_FOR_DELIVERY,
            self.Status.DELIVERED,
        )
        current_index = statuses.index(self.status) if self.status in statuses else -1
        return [
            {
                "status": status,
                "label": self.Status(status).label,
                "complete": index <= current_index,
                "current": index == current_index,
            }
            for index, status in enumerate(statuses)
        ]

    def __str__(self):
        return f"Order #{self.pk} — {self.full_name}"


class PaymentTransaction(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", "Created"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="payment_transactions"
    )
    razorpay_order_id = models.CharField(max_length=100, blank=True)
    razorpay_payment_id = models.CharField(max_length=100, blank=True)
    signature = models.CharField(max_length=128, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="INR")
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.CREATED
    )
    error_code = models.CharField(max_length=100, blank=True)
    error_description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"Payment for order #{self.order_id}: {self.get_status_display()}"


class OrderTrackingEvent(models.Model):
    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="tracking_events"
    )
    status = models.CharField(max_length=24, choices=Order.Status.choices)
    description = models.CharField(max_length=240)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_tracking_events",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"Order #{self.order_id}: {self.get_status_display()}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True
    )
    product_name = models.CharField(max_length=160)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField()

    def __str__(self):
        return f"{self.quantity} × {self.product_name}"
