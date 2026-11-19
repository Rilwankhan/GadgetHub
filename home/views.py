import json
import logging
import math
import re
from decimal import Decimal
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .forms import AdminProfileForm, CustomerProfileForm, CustomerRegistrationForm
from .models import (
    Category,
    CustomerProfile,
    Order,
    OrderItem,
    OrderTrackingEvent,
    PaymentTransaction,
    Product,
)
from .pdf_reports import build_payment_receipt
from .payments import RazorpayError, create_order as create_razorpay_order
from .payments import fetch_order_payments as fetch_razorpay_order_payments
from .payments import fetch_payment as fetch_razorpay_payment
from .payments import verify_signature as verify_razorpay_signature


logger = logging.getLogger(__name__)


def role_dashboard_url(user):
    return reverse("admin:index") if user.is_staff else reverse("account")


class SharedLoginView(LoginView):
    template_name = "home/login.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(role_dashboard_url(request.user))
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        if (
            not self.request.user.is_staff
            and self.get_redirect_url() == f"{reverse('home')}?checkout=1"
        ):
            return self.get_redirect_url()
        return role_dashboard_url(self.request.user)


def home(request):
    products = list(
        Product.objects.filter(is_active=True)
        .select_related("category")
        .values(
            "id", "name", "category__name", "price", "badge", "description", "image"
        )
    )
    for product in products:
        product["category"] = product.pop("category__name")
        product["price"] = float(product["price"])
        product["url"] = reverse("product_detail", args=[product["id"]])
    categories = Category.objects.filter(products__is_active=True).distinct()
    profile = None
    if request.user.is_authenticated:
        profile, _ = CustomerProfile.objects.get_or_create(user=request.user)
    customer_name = (
        request.user.get_full_name().strip()
        if request.user.is_authenticated
        else ""
    )
    checkout_ready = bool(
        request.user.is_authenticated
        and customer_name
        and request.user.email.strip()
        and profile.mobile.strip()
        and profile.address.strip()
    )
    return render(
        request,
        "home/index.html",
        {
            "products": products,
            "categories": categories,
            "razorpay_enabled": settings.RAZORPAY_ENABLED,
            "razorpay_key_id": settings.RAZORPAY_KEY_ID,
            "checkout_ready": checkout_ready,
            "customer_name": customer_name,
            "customer_email": (
                request.user.email if request.user.is_authenticated else ""
            ),
            "customer_mobile": profile.mobile if profile else "",
            "customer_address": profile.address if profile else "",
        },
    )


def product_detail(request, product_id):
    product = get_object_or_404(
        Product.objects.select_related("category"),
        pk=product_id,
        is_active=True,
    )
    return render(request, "home/product_detail.html", {"product": product})


def register(request):
    if request.user.is_authenticated:
        return redirect(role_dashboard_url(request.user))
    form = CustomerRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        if request.POST.get("next") == f"{reverse('home')}?checkout=1":
            return redirect(f"{reverse('home')}?checkout=1")
        return redirect("account")
    return render(
        request,
        "home/register.html",
        {
            "form": form,
            "next": request.POST.get("next", request.GET.get("next", "")),
        },
    )


@login_required
def account(request):
    profile, _ = CustomerProfile.objects.get_or_create(user=request.user)
    is_admin = request.user.is_staff
    profile_form_class = AdminProfileForm if is_admin else CustomerProfileForm
    profile_form_kwargs = (
        {"user": request.user, "profile": profile}
        if is_admin
        else {"instance": profile, "user": request.user}
    )
    profile_form = profile_form_class(**profile_form_kwargs)
    password_form = PasswordChangeForm(request.user)
    show_password_form = False
    show_profile_details = False
    show_profile_edit = False
    if request.method == "POST":
        if request.POST.get("form_type") == "profile":
            show_profile_details = True
            show_profile_edit = True
            profile_form = profile_form_class(
                request.POST, **profile_form_kwargs
            )
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, "Your profile details were updated.")
                return redirect("account")
        elif request.POST.get("form_type") == "password":
            show_password_form = True
            password_form = PasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, "Your password was updated.")
                return redirect("account")
    if not is_admin:
        paid_transactions = PaymentTransaction.objects.filter(
            order_id=OuterRef("pk"),
            status=PaymentTransaction.Status.PAID,
        )
        orders = (
            request.user.orders.annotate(
                has_verified_payment=Exists(paid_transactions)
            )
            .filter(
                Q(
                    payment_method__in=[
                        Order.PaymentMethod.CASH_ON_DELIVERY,
                        Order.PaymentMethod.ARRANGE_WITH_STORE,
                    ]
                )
                | Q(payment_status=Order.PaymentStatus.PAID)
            )
            .prefetch_related("items", "tracking_events")
            .all()
        )
    else:
        orders = Order.objects.none()
    return render(
        request,
        "home/account.html",
        {
            "orders": orders,
            "profile": profile,
            "profile_form": profile_form,
            "password_form": password_form,
            "show_password_form": show_password_form,
            "show_profile_details": show_profile_details,
            "show_profile_edit": show_profile_edit,
            "is_admin": is_admin,
        },
    )


@login_required
@require_GET
@never_cache
def payment_receipt(request, order_id):
    order = get_object_or_404(
        Order.objects.filter(
            user=request.user,
            payment_method=Order.PaymentMethod.RAZORPAY,
            payment_status=Order.PaymentStatus.PAID,
        ).prefetch_related("items"),
        pk=order_id,
    )
    payment = order.payment_transactions.filter(
        status=PaymentTransaction.Status.PAID
    ).first()
    if payment is None:
        raise Http404("A verified Razorpay payment was not found for this order.")

    response = HttpResponse(
        build_payment_receipt(order, payment).getvalue(),
        content_type="application/pdf",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="GadgetHub-Receipt-Order-{order.pk}.pdf"'
    )
    return response


@require_POST
@login_required
def cancel_order(request, order_id):
    with transaction.atomic():
        order = get_object_or_404(
            Order.objects.select_for_update().filter(user=request.user), pk=order_id
        )
        if not order.can_be_cancelled:
            messages.error(
                request,
                "This order can no longer be cancelled. Orders can be cancelled "
                "within 48 hours, before they are shipped.",
            )
            return redirect("account")

        order.status = Order.Status.CANCELLED
        order.save(update_fields=["status"])
        OrderTrackingEvent.objects.create(
            order=order,
            status=order.status,
            description="You cancelled this order.",
            updated_by=request.user,
        )

    messages.success(request, f"Order #{order.pk} has been cancelled.")
    return redirect("account")


@require_POST
@login_required
def delete_cancelled_order(request, order_id):
    order = get_object_or_404(
        Order.objects.filter(user=request.user, status=Order.Status.CANCELLED),
        pk=order_id,
    )
    order.delete()
    messages.success(request, "Cancelled order removed from your order history.")
    return redirect("account")


@never_cache
@require_GET
@login_required
def order_tracking(request):
    orders = request.user.orders.filter(
        Q(
            payment_method__in=[
                Order.PaymentMethod.CASH_ON_DELIVERY,
                Order.PaymentMethod.ARRANGE_WITH_STORE,
            ]
        )
        | Q(payment_status=Order.PaymentStatus.PAID)
    ).prefetch_related("items", "tracking_events")
    return JsonResponse(
        {
            "orders": [
                {
                    "id": order.pk,
                    "status": order.status,
                    "status_label": order.get_status_display(),
                    "can_cancel": order.can_be_cancelled,
                    "created_at": order.created_at.isoformat(),
                    "delete_url": reverse("delete_cancelled_order", args=[order.pk]),
                    "product_names": [
                        item.product_name for item in order.items.all()
                    ],
                    "delivered_at": (
                        order.delivered_at.isoformat()
                        if order.delivered_at
                        else None
                    ),
                    "cancelled_at": (
                        order.cancelled_at.isoformat()
                        if order.cancelled_at
                        else None
                    ),
                    "tracking_steps": order.tracking_steps,
                    "events": [
                        {
                            "status": event.status,
                            "status_label": event.get_status_display(),
                            "description": event.description,
                            "created_at": event.created_at.isoformat(),
                        }
                        for event in order.tracking_events.all()
                    ],
                }
                for order in orders
            ]
        }
    )


@require_POST
def place_order(request):
    if not request.user.is_authenticated:
        checkout_url = f"{reverse('home')}?checkout=1"
        return redirect(
            f"{reverse('login')}?{urlencode({'next': checkout_url})}"
        )
    try:
        payload = json.loads(request.body)
        profile, _ = CustomerProfile.objects.get_or_create(user=request.user)
        name = request.user.get_full_name().strip()
        email = request.user.email.strip()
        phone = profile.mobile.strip()
        address = profile.address.strip()
        cart = payload.get("cart")
        payment_method = payload.get("payment_method", Order.PaymentMethod.CASH_ON_DELIVERY)
        location = payload.get("location")

        if not all((name, email, phone, address)):
            return JsonResponse(
                {
                    "error": (
                        "Complete your name, email, mobile number, and delivery "
                        "address in Personal Details before ordering."
                    )
                },
                status=400,
            )
        if len(name) > 160 or len(phone) > 30 or not re.fullmatch(r"[0-9+() -]{7,30}", phone):
            return JsonResponse(
                {"error": "Update your account with a valid name and phone number."},
                status=400,
            )
        try:
            validate_email(email)
        except ValidationError:
            return JsonResponse({"error": "Enter a valid email address."}, status=400)
        if isinstance(location, dict):
            latitude = location.get("latitude")
            longitude = location.get("longitude")
            if (
                not isinstance(latitude, (int, float))
                or isinstance(latitude, bool)
                or not isinstance(longitude, (int, float))
                or isinstance(longitude, bool)
                or not math.isfinite(latitude)
                or not math.isfinite(longitude)
                or not -90 <= latitude <= 90
                or not -180 <= longitude <= 180
            ):
                return JsonResponse({"error": "Invalid location coordinates."}, status=400)
        elif location is None:
            latitude = longitude = None
        else:
            return JsonResponse({"error": "Invalid location coordinates."}, status=400)
        if not isinstance(cart, list) or not cart:
            return JsonResponse({"error": "Your cart is empty."}, status=400)
        if payment_method not in (
            Order.PaymentMethod.CASH_ON_DELIVERY,
            Order.PaymentMethod.RAZORPAY,
        ):
            return JsonResponse({"error": "Choose a valid payment method."}, status=400)
        if (
            payment_method == Order.PaymentMethod.RAZORPAY
            and not settings.RAZORPAY_ENABLED
        ):
            return JsonResponse(
                {"error": "Online payment is not currently available."},
                status=503,
            )

        quantities = {}
        for item in cart:
            if not isinstance(item, dict):
                return JsonResponse({"error": "Invalid cart item."}, status=400)
            product_id = item.get("id")
            quantity = item.get("quantity")
            if (
                not isinstance(product_id, int)
                or isinstance(product_id, bool)
                or not isinstance(quantity, int)
                or isinstance(quantity, bool)
                or quantity < 1
                or quantity > 99
            ):
                return JsonResponse({"error": "Invalid cart quantity."}, status=400)
            quantities[product_id] = quantities.get(product_id, 0) + quantity
            if quantities[product_id] > 99:
                return JsonResponse({"error": "Maximum quantity is 99 per product."}, status=400)

        payment_transaction = None
        with transaction.atomic():
            products = {
                product.pk: product
                for product in Product.objects.select_for_update()
                .filter(pk__in=quantities, is_active=True)
            }
            if len(products) != len(quantities):
                return JsonResponse(
                    {"error": "A product is no longer available. Refresh the store and try again."},
                    status=400,
                )
            total = sum(
                (products[pk].price * quantity for pk, quantity in quantities.items()),
                Decimal("0.00"),
            )
            order = Order.objects.create(
                user=request.user,
                full_name=name,
                email=email,
                phone=phone,
                address=address,
                payment_method=payment_method,
                payment_status=(
                    Order.PaymentStatus.PENDING
                    if payment_method == Order.PaymentMethod.RAZORPAY
                    else Order.PaymentStatus.UNPAID
                ),
                total=total,
                location_latitude=latitude,
                location_longitude=longitude,
            )
            OrderItem.objects.bulk_create(
                [
                    OrderItem(
                        order=order,
                        product=products[pk],
                        product_name=products[pk].name,
                        unit_price=products[pk].price,
                        quantity=quantity,
                    )
                    for pk, quantity in quantities.items()
                ]
            )
            OrderTrackingEvent.objects.create(
                order=order,
                status=Order.Status.PENDING,
                description="We received your order. It is waiting for confirmation.",
            )
            if payment_method == Order.PaymentMethod.RAZORPAY:
                payment_transaction = PaymentTransaction.objects.create(
                    order=order,
                    amount=total,
                )

        if payment_transaction is None:
            return JsonResponse({"order_id": order.pk, "total": str(total)})

        try:
            razorpay_order = create_razorpay_order(
                int(total * 100),
                f"gh-order-{order.pk}",
            )
        except RazorpayError:
            logger.exception(
                "Could not create Razorpay order for GadgetHub order %s.", order.pk
            )
            payment_transaction.status = PaymentTransaction.Status.FAILED
            payment_transaction.error_description = (
                "Razorpay order creation did not complete."
            )
            payment_transaction.save(
                update_fields=["status", "error_description", "updated_at"]
            )
            order.payment_status = Order.PaymentStatus.FAILED
            order.save(update_fields=["payment_status"])
            return JsonResponse(
                {"error": "Could not start online payment. Please try again."},
                status=502,
            )

        payment_transaction.razorpay_order_id = razorpay_order["id"]
        payment_transaction.save(
            update_fields=["razorpay_order_id", "updated_at"]
        )
        return JsonResponse(
            {
                "order_id": order.pk,
                "total": str(total),
                "razorpay": {
                    "key_id": settings.RAZORPAY_KEY_ID,
                    "order_id": razorpay_order["id"],
                    "amount": razorpay_order["amount"],
                    "currency": razorpay_order["currency"],
                    "name": name,
                    "email": email,
                    "mobile": phone,
                },
            }
        )
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        AttributeError,
        TypeError,
        ValueError,
    ):
        return JsonResponse({"error": "Invalid order details."}, status=400)


@require_POST
@login_required
def verify_razorpay_payment(request):
    if not settings.RAZORPAY_ENABLED:
        return JsonResponse(
            {"error": "Online payment is not currently available."}, status=503
        )
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid payment response."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Invalid payment response."}, status=400)

    order_id = payload.get("order_id")
    payment_order_id = payload.get("razorpay_order_id")
    payment_id = payload.get("razorpay_payment_id")
    signature = payload.get("razorpay_signature")
    if (
        not isinstance(order_id, int)
        or isinstance(order_id, bool)
        or not isinstance(payment_order_id, str)
        or not re.fullmatch(r"order_[A-Za-z0-9]+", payment_order_id)
        or not isinstance(payment_id, str)
        or not re.fullmatch(r"pay_[A-Za-z0-9]+", payment_id)
        or not isinstance(signature, str)
        or not re.fullmatch(r"[a-fA-F0-9]{64}", signature)
    ):
        return JsonResponse({"error": "Invalid payment response."}, status=400)

    payment_transaction = get_object_or_404(
        PaymentTransaction.objects.select_related("order").filter(
            order__user=request.user,
            order__payment_method=Order.PaymentMethod.RAZORPAY,
        ),
        order_id=order_id,
        razorpay_order_id=payment_order_id,
    )
    order = payment_transaction.order

    if payment_transaction.status == PaymentTransaction.Status.PAID:
        if payment_transaction.razorpay_payment_id == payment_id:
            return JsonResponse({"order_id": order.pk, "payment_status": "paid"})
        return JsonResponse({"error": "This order has already been paid."}, status=409)

    if not verify_razorpay_signature(payment_order_id, payment_id, signature):
        return JsonResponse({"error": "Payment signature verification failed."}, status=400)

    try:
        provider_payment = fetch_razorpay_payment(payment_id)
    except RazorpayError:
        logger.exception(
            "Could not verify Razorpay payment for GadgetHub order %s.", order.pk
        )
        return JsonResponse(
            {"error": "Could not verify payment with Razorpay. Please try again."},
            status=502,
        )

    if (
        provider_payment.get("id") != payment_id
        or provider_payment.get("order_id") != payment_order_id
        or provider_payment.get("amount") != int(payment_transaction.amount * 100)
        or provider_payment.get("currency") != payment_transaction.currency
    ):
        return JsonResponse(
            {"error": "Razorpay payment details did not match this order."},
            status=400,
        )

    provider_status = provider_payment.get("status")
    if provider_status == "failed":
        with transaction.atomic():
            locked_order = Order.objects.select_for_update().get(pk=order.pk)
            locked_transaction = PaymentTransaction.objects.select_for_update().get(
                pk=payment_transaction.pk
            )
            if locked_order.payment_status != Order.PaymentStatus.PAID:
                locked_transaction.status = PaymentTransaction.Status.FAILED
                locked_transaction.razorpay_payment_id = payment_id
                locked_transaction.signature = signature
                locked_transaction.error_code = str(
                    provider_payment.get("error_code") or ""
                )[:100]
                locked_transaction.error_description = str(
                    provider_payment.get("error_description") or "Payment failed."
                )[:2000]
                locked_transaction.save(
                    update_fields=[
                        "status",
                        "razorpay_payment_id",
                        "signature",
                        "error_code",
                        "error_description",
                        "updated_at",
                    ]
                )
                locked_order.payment_status = Order.PaymentStatus.FAILED
                locked_order.save(update_fields=["payment_status"])
        return JsonResponse({"error": "Razorpay reported that payment failed."}, status=400)

    if (
        provider_status != "captured"
        or provider_payment.get("captured") is not True
    ):
        return JsonResponse(
            {"error": "Razorpay has not captured this payment yet."}, status=409
        )

    with transaction.atomic():
        locked_order = Order.objects.select_for_update().get(pk=order.pk)
        locked_transaction = PaymentTransaction.objects.select_for_update().get(
            pk=payment_transaction.pk
        )
        if locked_order.payment_status == Order.PaymentStatus.PAID:
            if locked_transaction.razorpay_payment_id == payment_id:
                return JsonResponse(
                    {"order_id": locked_order.pk, "payment_status": "paid"}
                )
            return JsonResponse(
                {"error": "This order has already been paid."}, status=409
            )
        locked_transaction.status = PaymentTransaction.Status.PAID
        locked_transaction.razorpay_payment_id = payment_id
        locked_transaction.signature = signature
        locked_transaction.error_code = ""
        locked_transaction.error_description = ""
        locked_transaction.save(
            update_fields=[
                "status",
                "razorpay_payment_id",
                "signature",
                "error_code",
                "error_description",
                "updated_at",
            ]
        )
        locked_order.payment_status = Order.PaymentStatus.PAID
        locked_order.status = Order.Status.CONFIRMED
        locked_order.save(update_fields=["payment_status", "status"])
        OrderTrackingEvent.objects.create(
            order=locked_order,
            status=Order.Status.CONFIRMED,
            description="Payment received and verified. Your order is confirmed.",
        )

    return JsonResponse({"order_id": order.pk, "payment_status": "paid"})


def _update_payment_attempt(request, target_status):
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid payment update."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Invalid payment update."}, status=400)

    order_id = payload.get("order_id")
    payment_order_id = payload.get("razorpay_order_id")
    if (
        not isinstance(order_id, int)
        or isinstance(order_id, bool)
        or not isinstance(payment_order_id, str)
        or not re.fullmatch(r"order_[A-Za-z0-9]+", payment_order_id)
    ):
        return JsonResponse({"error": "Invalid payment update."}, status=400)

    payment_id = payload.get("razorpay_payment_id")
    if target_status == PaymentTransaction.Status.FAILED:
        if not isinstance(payment_id, str) or not re.fullmatch(
            r"pay_[A-Za-z0-9]+", payment_id
        ):
            return JsonResponse(
                {"error": "Razorpay did not provide a verifiable payment reference."},
                status=400,
            )
    payment_transaction = get_object_or_404(
        PaymentTransaction.objects.select_related("order").filter(
            order__user=request.user,
            order__payment_method=Order.PaymentMethod.RAZORPAY,
        ),
        order_id=order_id,
        razorpay_order_id=payment_order_id,
    )
    order = payment_transaction.order
    if (
        order.payment_status == Order.PaymentStatus.PAID
        or payment_transaction.status == PaymentTransaction.Status.PAID
    ):
        return JsonResponse({"error": "This order has already been paid."}, status=409)

    provider_payments = None
    failure_record = None
    if target_status == PaymentTransaction.Status.FAILED:
        try:
            provider_payment = fetch_razorpay_payment(payment_id)
        except RazorpayError:
            logger.exception(
                "Could not verify failed Razorpay payment for order %s.", order_id
            )
            return JsonResponse(
                {"error": "Could not verify payment with Razorpay. Please try again."},
                status=502,
            )
        if (
            provider_payment.get("id") != payment_id
            or provider_payment.get("order_id") != payment_order_id
            or provider_payment.get("amount")
            != int(payment_transaction.amount * 100)
            or provider_payment.get("currency") != payment_transaction.currency
            or provider_payment.get("status") != "failed"
        ):
            return JsonResponse(
                {"error": "Razorpay did not confirm this payment failure."},
                status=409,
            )
        failure_record = provider_payment
    else:
        try:
            provider_order_payments = fetch_razorpay_order_payments(payment_order_id)
        except RazorpayError:
            logger.exception(
                "Could not verify Razorpay checkout closure for order %s.", order.pk
            )
            return JsonResponse(
                {
                    "error": (
                        "Could not confirm payment status with Razorpay. "
                        "Please check your order again."
                    )
                },
                status=502,
            )
        provider_payments = provider_order_payments.get("items")
        if not isinstance(provider_payments, list):
            return JsonResponse(
                {"error": "Razorpay returned invalid payment details."},
                status=502,
            )
        if any(
            not isinstance(payment, dict)
            or payment.get("status") in ("authorized", "captured")
            for payment in provider_payments
        ):
            return JsonResponse(
                {
                    "error": (
                        "A payment is still processing. Check your order "
                        "status before trying again."
                    )
                },
                status=409,
            )
        if provider_payments and any(
            payment.get("status") != "failed" for payment in provider_payments
        ):
            return JsonResponse(
                {"error": "Razorpay has not confirmed that checkout was cancelled."},
                status=409,
            )
        if provider_payments:
            for payment in provider_payments:
                if (
                    not isinstance(payment.get("id"), str)
                    or not re.fullmatch(r"pay_[A-Za-z0-9]+", payment["id"])
                    or payment.get("order_id") != payment_order_id
                    or payment.get("amount") != int(payment_transaction.amount * 100)
                    or payment.get("currency") != payment_transaction.currency
                ):
                    return JsonResponse(
                        {"error": "Razorpay payment details did not match this order."},
                        status=409,
                    )
            failure_record = provider_payments[-1]

    effective_status = target_status
    if target_status == PaymentTransaction.Status.CANCELLED and failure_record:
        effective_status = PaymentTransaction.Status.FAILED
    with transaction.atomic():
        order = Order.objects.select_for_update().get(
            pk=order_id,
            user=request.user,
            payment_method=Order.PaymentMethod.RAZORPAY,
        )
        payment_transaction = PaymentTransaction.objects.select_for_update().get(
            order=order,
            razorpay_order_id=payment_order_id,
        )
        if order.payment_status == Order.PaymentStatus.PAID:
            return JsonResponse({"error": "This order has already been paid."}, status=409)
        if payment_transaction.status == PaymentTransaction.Status.PAID:
            return JsonResponse({"error": "This payment is already complete."}, status=409)

        payment_transaction.status = effective_status
        if effective_status == PaymentTransaction.Status.FAILED:
            payment_transaction.razorpay_payment_id = failure_record["id"]
            payment_transaction.error_code = str(
                failure_record.get("error_code") or ""
            )[:100]
            payment_transaction.error_description = str(
                failure_record.get("error_description") or "Payment did not complete."
            )[:2000]
            order.payment_status = Order.PaymentStatus.FAILED
        else:
            payment_transaction.error_description = "Checkout was closed before payment."
            order.payment_status = Order.PaymentStatus.CANCELLED
        payment_transaction.save(
            update_fields=[
                "status",
                "error_code",
                "error_description",
                "razorpay_payment_id",
                "updated_at",
            ]
        )
        order.save(update_fields=["payment_status"])
    return JsonResponse({"order_id": order.pk, "payment_status": effective_status})


@require_POST
@login_required
def fail_razorpay_payment(request):
    return _update_payment_attempt(request, PaymentTransaction.Status.FAILED)


@require_POST
@login_required
def cancel_razorpay_payment(request):
    return _update_payment_attempt(request, PaymentTransaction.Status.CANCELLED)
