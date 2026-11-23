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
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .activity import (
    record_order_status_change,
    record_payment_status_change,
    user_display_name,
)
from .forms import AdminProfileForm, CustomerProfileForm, CustomerRegistrationForm
from .forms import FriendlyPasswordChangeForm, FriendlySetPasswordForm
from .inventory import (
    StockUnavailable,
    order_stock_available,
    reserve_order_stock,
    reserve_products,
    restore_order_stock,
)
from .models import (
    Category,
    CustomerProfile,
    EmailOTP,
    OTP_MAX_ATTEMPTS,
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
from .roles import is_gadgethub_staff, staff_member_required


logger = logging.getLogger(__name__)


def role_dashboard_url(user):
    if user.is_staff:
        return reverse("admin:index")
    return (
        reverse("staff_dashboard")
        if is_gadgethub_staff(user)
        else reverse("account")
    )


class SharedLoginView(LoginView):
    template_name = "home/login.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(role_dashboard_url(request.user))
        if request.session.pop("password_reset_complete_notice", False):
            messages.success(
                request,
                "Your password has been changed. Sign in with your new password.",
            )
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.get_user()
        # Staff (GadgetHub Staff group) and Django admin (is_staff) are exempt
        # from email verification — they are always allowed through.
        if (
            not user.is_active
            and not user.is_staff
            and not is_gadgethub_staff(user)
        ):
            return redirect(
                reverse("verify_otp") + f"?uid={user.pk}"
            )
        return super().form_valid(form)

    def form_invalid(self, form):
        # Django's ModelBackend refuses to authenticate is_active=False users,
        # so form_valid is never reached for them. Detect that case here:
        # if the credentials match an inactive normal customer, redirect to
        # the OTP page instead of showing a generic error.
        username = form.data.get("username", "").strip()
        password = form.data.get("password", "")
        if username and password:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            try:
                user = User.objects.get(
                    username=username,
                    is_active=False,
                    is_staff=False,
                )
                if user.check_password(password) and not is_gadgethub_staff(user):
                    return redirect(
                        reverse("verify_otp") + f"?uid={user.pk}"
                    )
            except (User.DoesNotExist, User.MultipleObjectsReturned):
                pass
        return super().form_invalid(form)

    def get_success_url(self):
        if (
            not is_gadgethub_staff(self.request.user)
            and not self.request.user.is_staff
            and self.get_redirect_url() == f"{reverse('home')}?checkout=1"
        ):
            return self.get_redirect_url()
        return role_dashboard_url(self.request.user)


def home(request):
    products = list(
        Product.objects.filter(is_active=True)
        .select_related("category")
        .values(
            "id",
            "name",
            "category__name",
            "price",
            "badge",
            "description",
            "image",
            "stock_quantity",
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


def _send_otp_email(user, plain_otp):
    try:
        send_mail(
            subject=render_to_string(
                "home/verification_email_subject.txt",
                {"user": user},
            ).strip(),
            message=render_to_string(
                "home/verification_email.txt",
                {"user": user, "otp": plain_otp, "valid_minutes": 5},
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
        return True
    except Exception:
        logger.exception("Failed to send OTP email to user %s.", user.pk)
        return False


def register(request):
    if request.user.is_authenticated:
        return redirect(role_dashboard_url(request.user))
    form = CustomerRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = form.save()
            plain_otp = EmailOTP.generate_for_user(user)
        sent = _send_otp_email(user, plain_otp)
        if not sent:
            user.delete()
            messages.error(
                request,
                "We could not send a verification email to that address. "
                "Please check the email address and try again.",
            )
            return render(request, "home/register.html", {
                "form": form,
                "next": request.POST.get("next", request.GET.get("next", "")),
            })
        next_url = request.POST.get("next", "")
        otp_url = reverse("verify_otp") + f"?uid={user.pk}"
        if next_url:
            otp_url += "&" + urlencode({"next": next_url})
        return redirect(otp_url)
    return render(request, "home/register.html", {
        "form": form,
        "next": request.POST.get("next", request.GET.get("next", "")),
    })


@never_cache
def verify_otp(request):
    from django.contrib.auth import get_user_model
    from django.utils import timezone
    User = get_user_model()

    uid = request.GET.get("uid") or request.POST.get("uid", "")
    next_url = request.GET.get("next") or request.POST.get("next", "")
    user = None
    try:
        user = User.objects.get(pk=uid, is_active=False, is_staff=False)
    except (User.DoesNotExist, ValueError):
        messages.error(request, "Account not found or already verified.")
        login_url = reverse("login")
        if next_url:
            login_url += "?" + urlencode({"next": next_url})
        return redirect(login_url)

    try:
        otp_record = user.email_otp
    except EmailOTP.DoesNotExist:
        messages.error(request, "No verification code found. Please register again.")
        return redirect("register")

    # Compute resend countdown seconds for the template
    resend_wait = max(
        0,
        int((otp_record.resend_available_at - timezone.now()).total_seconds()),
    )

    if request.method == "POST":
        plain_otp = request.POST.get("otp", "").strip()

        if otp_record.is_locked:
            messages.error(
                request,
                "Too many incorrect attempts. Please request a new code.",
            )
            return render(request, "home/verify_otp.html", {
                "masked_email": _mask_email(user.email),
                "uid": uid,
                "locked": True,
                "resend_wait": 0,
                "next_url": next_url,
            })

        if otp_record.is_expired:
            messages.error(
                request,
                "Your verification code has expired. Please request a new one.",
            )
            return render(request, "home/verify_otp.html", {
                "masked_email": _mask_email(user.email),
                "uid": uid,
                "locked": False,
                "resend_wait": 0,
                "next_url": next_url,
            })

        if not plain_otp.isdigit() or len(plain_otp) != 6:
            messages.error(request, "Please enter the 6-digit code from your email.")
            return render(request, "home/verify_otp.html", {
                "masked_email": _mask_email(user.email),
                "uid": uid,
                "locked": False,
                "resend_wait": resend_wait,
                "next_url": next_url,
            })

        if not otp_record.check_otp(plain_otp):
            otp_record.attempts += 1
            otp_record.save(update_fields=["attempts"])
            remaining = OTP_MAX_ATTEMPTS - otp_record.attempts
            if remaining <= 0:
                messages.error(
                    request,
                    "Too many incorrect attempts. Please request a new code.",
                )
            else:
                messages.error(
                    request,
                    f"Incorrect code. {remaining} attempt{'s' if remaining != 1 else ''} remaining.",
                )
            return render(request, "home/verify_otp.html", {
                "masked_email": _mask_email(user.email),
                "uid": uid,
                "locked": remaining <= 0,
                "resend_wait": resend_wait,
                "next_url": next_url,
            })

        # OTP correct — activate account and clean up
        user.is_active = True
        user.save(update_fields=["is_active"])
        otp_record.delete()
        messages.success(
            request,
            "Your email has been verified. You can now sign in.",
        )
        return redirect("login")

    return render(request, "home/verify_otp.html", {
        "masked_email": _mask_email(user.email),
        "uid": uid,
        "locked": otp_record.is_locked,
        "resend_wait": resend_wait,
        "next_url": next_url,
    })


def _mask_email(email):
    """Return e.g. 'ab***@gmail.com' — never expose the full address."""
    local, _, domain = email.partition("@")
    visible = local[:2] if len(local) >= 2 else local[:1]
    return f"{visible}***@{domain}"


def _send_password_reset_otp_email(user, plain_otp):
    try:
        send_mail(
            subject="Your GadgetHub password reset code",
            message=render_to_string(
                "home/password_reset_email.txt",
                {"user": user, "otp": plain_otp, "valid_minutes": 5},
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
        return True
    except Exception:
        logger.exception("Failed to send password reset OTP to user %s.", user.pk)
        return False


@never_cache
def password_reset_request(request):
    if request.method == "POST":
        from django.contrib.auth import get_user_model

        email = request.POST.get("email", "").strip()
        User = get_user_model()
        user = User.objects.filter(
            email__iexact=email,
            is_active=True,
        ).first()
        if user:
            plain_otp = EmailOTP.generate_for_user(user)
            sent = _send_password_reset_otp_email(user, plain_otp)
            if sent:
                return redirect(
                    reverse("password_reset_verify") + f"?uid={user.pk}"
                )
            messages.error(
                request,
                "We could not send the reset code. Please try again later.",
            )
        else:
            return redirect("password_reset")

    return render(
        request,
        "home/password_reset.html",
        {
            "mode": "request",
            "page_title": "Reset your password",
            "page_description": (
                "Enter the email address on your account and we’ll send "
                "a 6-digit password reset code."
            ),
        },
    )


@never_cache
def password_reset_verify(request):
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    User = get_user_model()
    uid = request.GET.get("uid") or request.POST.get("uid", "")
    try:
        user = User.objects.get(pk=uid, is_active=True)
    except (User.DoesNotExist, ValueError):
        messages.error(request, "Account not found or already verified.")
        return redirect("password_reset")

    try:
        otp_record = user.email_otp
    except EmailOTP.DoesNotExist:
        messages.error(request, "No reset code found. Please request a new one.")
        return redirect("password_reset")

    resend_wait = max(
        0,
        int((otp_record.resend_available_at - timezone.now()).total_seconds()),
    )

    if request.method == "POST":
        plain_otp = request.POST.get("otp", "").strip()
        if otp_record.is_locked:
            messages.error(
                request,
                "Too many incorrect attempts. Please request a new code.",
            )
        elif otp_record.is_expired:
            messages.error(
                request,
                "Your reset code has expired. Please request a new one.",
            )
        elif not plain_otp.isdigit() or len(plain_otp) != 6:
            messages.error(request, "Please enter the 6-digit code from your email.")
        elif not otp_record.check_otp(plain_otp):
            otp_record.attempts += 1
            otp_record.save(update_fields=["attempts"])
            remaining = OTP_MAX_ATTEMPTS - otp_record.attempts
            if remaining <= 0:
                messages.error(
                    request,
                    "Too many incorrect attempts. Please request a new code.",
                )
            else:
                messages.error(
                    request,
                    f"Incorrect code. {remaining} attempt{'s' if remaining != 1 else ''} remaining.",
                )
        else:
            request.session["password_reset_user_id"] = user.pk
            otp_record.delete()
            return redirect("password_reset_complete")

    return render(
        request,
        "home/password_reset.html",
        {
            "mode": "verify",
            "page_title": "Enter reset code",
            "page_description": "Enter the 6-digit code sent to your email.",
            "masked_email": _mask_email(user.email),
            "uid": uid,
            "resend_wait": resend_wait,
            "locked": otp_record.is_locked,
        },
    )


@require_POST
@never_cache
def password_reset_resend(request):
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    User = get_user_model()
    uid = request.POST.get("uid", "")
    try:
        user = User.objects.get(pk=uid, is_active=True)
    except (User.DoesNotExist, ValueError):
        messages.error(request, "Account not found.")
        return redirect("password_reset")

    try:
        otp_record = user.email_otp
    except EmailOTP.DoesNotExist:
        otp_record = None

    if otp_record and not otp_record.can_resend:
        wait_seconds = int(
            (otp_record.resend_available_at - timezone.now()).total_seconds()
        )
        messages.warning(
            request,
            f"Please wait {wait_seconds} second{'s' if wait_seconds != 1 else ''} before requesting a new code.",
        )
        return redirect(reverse("password_reset_verify") + f"?uid={uid}")

    plain_otp = EmailOTP.generate_for_user(user)
    if not _send_password_reset_otp_email(user, plain_otp):
        messages.error(request, "We could not send the code. Please try again later.")
    return redirect(reverse("password_reset_verify") + f"?uid={uid}")


@never_cache
def password_reset_complete(request):
    user_id = request.session.get("password_reset_user_id")
    if not user_id:
        return redirect("password_reset")

    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        request.session.pop("password_reset_user_id", None)
        return redirect("password_reset")

    if request.method == "POST":
        form = FriendlySetPasswordForm(user, request.POST)
        if form.is_valid():
            form.save()
            request.session.pop("password_reset_user_id", None)
            request.session["password_reset_complete_notice"] = True
            return redirect("login")
    else:
        form = FriendlySetPasswordForm(user)

    return render(
        request,
        "home/password_reset.html",
        {
            "mode": "confirm",
            "page_title": "Choose a new password",
            "page_description": "Enter and confirm your new password.",
            "form": form,
        },
    )


@require_POST
def resend_otp(request):
    from django.contrib.auth import get_user_model
    from django.utils import timezone
    User = get_user_model()

    uid = request.POST.get("uid", "")
    next_url = request.POST.get("next", "")
    try:
        user = User.objects.get(pk=uid, is_active=False, is_staff=False)
    except (User.DoesNotExist, ValueError):
        messages.error(request, "Account not found or already verified.")
        return redirect("login")

    try:
        otp_record = user.email_otp
    except EmailOTP.DoesNotExist:
        otp_record = None

    if otp_record and not otp_record.can_resend:
        wait_secs = int(
            (otp_record.resend_available_at - timezone.now()).total_seconds()
        )
        messages.warning(
            request,
            f"Please wait {wait_secs} second{'s' if wait_secs != 1 else ''} before requesting a new code.",
        )
        verify_url = reverse("verify_otp") + f"?uid={uid}"
        if next_url:
            verify_url += "&" + urlencode({"next": next_url})
        return redirect(verify_url)

    plain_otp = EmailOTP.generate_for_user(user)
    sent = _send_otp_email(user, plain_otp)
    if sent:
        messages.success(request, "A new verification code has been sent to your email.")
    else:
        messages.error(request, "We could not send the code. Please try again later.")
    verify_url = reverse("verify_otp") + f"?uid={uid}"
    if next_url:
        verify_url += "&" + urlencode({"next": next_url})
    return redirect(verify_url)


@login_required
def account(request):
    profile, _ = CustomerProfile.objects.get_or_create(user=request.user)
    is_admin = request.user.is_staff
    is_staff_profile = is_gadgethub_staff(request.user)
    is_employee_profile = is_admin or is_staff_profile
    profile_form_class = (
        AdminProfileForm if is_employee_profile else CustomerProfileForm
    )
    profile_form_kwargs = (
        {"user": request.user, "profile": profile}
        if is_employee_profile
        else {"instance": profile, "user": request.user}
    )
    profile_form = profile_form_class(**profile_form_kwargs)
    password_form = FriendlyPasswordChangeForm(request.user)
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
            password_form = FriendlyPasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, "Your password was updated.")
                return redirect("account")
    if not is_employee_profile:
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
            "is_staff_profile": is_staff_profile,
            "is_employee_profile": is_employee_profile,
        },
    )


@staff_member_required
@require_GET
def staff_dashboard(request):
    paginator = Paginator(
        staff_visible_orders()
        .select_related("user")
        .prefetch_related("items", "payment_transactions", "tracking_events", "payment_events")
        .order_by("-created_at", "-pk"),
        25,
    )
    page_obj = paginator.get_page(request.GET.get("page"))
    order_rows = [
        {
            "order": order,
            "can_advance": (
                order.status != Order.Status.CANCELLED
                and order.next_status is not None
                and (
                    order.payment_method == Order.PaymentMethod.CASH_ON_DELIVERY
                    or order.payment_status == Order.PaymentStatus.PAID
                )
            ),
            "can_update_cod_payment": (
                order.payment_method == Order.PaymentMethod.CASH_ON_DELIVERY
                and order.status != Order.Status.CANCELLED
            ),
        }
        for order in page_obj.object_list
    ]
    return render(
        request,
        "home/staff/dashboard.html",
        {"page_obj": page_obj, "order_rows": order_rows},
    )


def staff_visible_orders():
    return Order.objects.exclude(
        Q(status=Order.Status.CANCELLED)
        | Q(payment_status=Order.PaymentStatus.CANCELLED)
    ).filter(
        Q(payment_method=Order.PaymentMethod.CASH_ON_DELIVERY)
        | Q(
            payment_method=Order.PaymentMethod.RAZORPAY,
            payment_status=Order.PaymentStatus.PAID,
        )
    )


@staff_member_required
@require_GET
def staff_order_detail(request, order_id):
    order = get_object_or_404(
        staff_visible_orders()
        .select_related("user")
        .prefetch_related(
            "items", "payment_transactions", "tracking_events", "payment_events"
        ),
        pk=order_id,
    )
    inventory_available = order_stock_available(order)
    order_row = {
        "order": order,
        "inventory_available": inventory_available,
        "can_advance": (
            order.next_status is not None
            and inventory_available
            and (
                order.payment_method == Order.PaymentMethod.CASH_ON_DELIVERY
                or order.payment_status == Order.PaymentStatus.PAID
            )
        ),
        "can_update_cod_payment": (
            order.payment_method == Order.PaymentMethod.CASH_ON_DELIVERY
        ),
    }
    return render(
        request,
        "home/staff/order_detail.html",
        {"row": order_row},
    )


@staff_member_required
@require_POST
def staff_advance_order(request, order_id):
    try:
        with transaction.atomic():
            order = get_object_or_404(
                Order.objects.select_for_update().prefetch_related("items"),
                pk=order_id,
            )
            if (
                order.payment_method != Order.PaymentMethod.CASH_ON_DELIVERY
                and order.payment_status != Order.PaymentStatus.PAID
            ):
                messages.error(request, "Only COD or paid orders can be processed.")
                return redirect("staff_order_detail", order_id=order.pk)
            if request.POST.get("expected_status") != order.status:
                messages.error(
                    request,
                    f"Order #{order.pk} changed. Refresh before updating it.",
                )
                return redirect("staff_order_detail", order_id=order.pk)
            new_status = request.POST.get("status")
            if new_status not in (
                Order.Status.CONFIRMED,
                Order.Status.SHIPPED,
                Order.Status.OUT_FOR_DELIVERY,
                Order.Status.DELIVERED,
            ) or new_status != order.next_status:
                messages.error(
                    request,
                    "Choose the next available order status.",
                )
                return redirect("staff_order_detail", order_id=order.pk)

            if not order.stock_deducted:
                reserve_order_stock(order)

            previous_status = order.status
            order.status = new_status
            order.save(update_fields=["status"])
            record_order_status_change(order, previous_status, request.user)
    except StockUnavailable as error:
        messages.error(
            request,
            f"Order #{order_id} cannot be progressed because inventory is unavailable: {error}",
        )
        return redirect("staff_order_detail", order_id=order_id)

    messages.success(
        request,
        f"Order #{order.pk} is now {order.get_status_display().lower()}.",
    )
    return redirect("staff_order_detail", order_id=order.pk)


@staff_member_required
@require_POST
def staff_update_cod_payment(request, order_id):
    with transaction.atomic():
        order = get_object_or_404(
            Order.objects.select_for_update(),
            pk=order_id,
            payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
        )
        if order.status == Order.Status.CANCELLED:
            messages.error(request, "Cancelled orders cannot be updated.")
            return redirect("staff_order_detail", order_id=order.pk)

        new_status = request.POST.get("payment_status")
        allowed_statuses = {
            Order.PaymentStatus.PENDING,
            Order.PaymentStatus.PAID,
            Order.PaymentStatus.FAILED,
        }
        if new_status not in allowed_statuses:
            messages.error(request, "Choose a valid COD payment status.")
            return redirect("staff_order_detail", order_id=order.pk)
        if (
            order.payment_status == Order.PaymentStatus.PAID
            and new_status != Order.PaymentStatus.PAID
        ):
            messages.error(
                request,
                "A paid COD order cannot be changed by staff.",
            )
            return redirect("staff_order_detail", order_id=order.pk)
        if new_status != order.payment_status:
            previous_status = order.payment_status
            order.payment_status = new_status
            order.save(update_fields=["payment_status"])
            record_payment_status_change(
                order,
                previous_status,
                new_status,
                request.user,
            )

    messages.success(
        request,
        f"Order #{order.pk} COD payment is {order.get_payment_status_display().lower()}.",
    )
    return redirect("staff_order_detail", order_id=order.pk)


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
                "within 24 hours, before they are shipped.",
            )
            return redirect("account")

        previous_status = order.status
        order.status = Order.Status.CANCELLED
        order.save(update_fields=["status"])
        restore_order_stock(order)
        record_order_status_change(
            order,
            previous_status,
            request.user,
            "You cancelled this order.",
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
            products = reserve_products(quantities)
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
                stock_deducted=True,
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
            with transaction.atomic():
                locked_order = Order.objects.select_for_update().get(pk=order.pk)
                locked_transaction = PaymentTransaction.objects.select_for_update().get(
                    pk=payment_transaction.pk
                )
                locked_transaction.status = PaymentTransaction.Status.FAILED
                locked_transaction.error_description = (
                    "Razorpay order creation did not complete."
                )
                locked_transaction.save(
                    update_fields=["status", "error_description", "updated_at"]
                )
                locked_order.payment_status = Order.PaymentStatus.FAILED
                locked_order.save(update_fields=["payment_status"])
                restore_order_stock(locked_order)
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
    except StockUnavailable as error:
        return JsonResponse({"error": str(error)}, status=409)
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
                restore_order_stock(locked_order)
        return JsonResponse({"error": "Razorpay reported that payment failed."}, status=400)

    if (
        provider_status != "captured"
        or provider_payment.get("captured") is not True
    ):
        return JsonResponse(
            {"error": "Razorpay has not captured this payment yet."}, status=409
        )

    try:
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
            reserve_order_stock(locked_order)
            previous_status = locked_order.status
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
            record_order_status_change(
                locked_order,
                previous_status,
                None,
                "Payment received and verified. Your order is confirmed.",
            )
    except StockUnavailable:
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
            locked_transaction.error_description = (
                "Payment was captured after stock became unavailable. "
                "Admin review is required."
            )
            locked_transaction.save(
                update_fields=[
                    "status",
                    "razorpay_payment_id",
                    "signature",
                    "error_description",
                    "updated_at",
                ]
            )
            locked_order.payment_status = Order.PaymentStatus.PAID
            locked_order.save(update_fields=["payment_status"])
            OrderTrackingEvent.objects.create(
                order=locked_order,
                previous_status=locked_order.status,
                status=locked_order.status,
                description=(
                    "Payment was captured, but inventory is unavailable. "
                    "Administrator review is required."
                ),
            )
        return JsonResponse(
            {
                "error": (
                    "Payment was confirmed, but the items are no longer in "
                    "stock. Contact the store; do not retry payment."
                )
            },
            status=409,
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
        restore_order_stock(order)
    return JsonResponse({"order_id": order.pk, "payment_status": effective_status})


@require_POST
@login_required
def fail_razorpay_payment(request):
    return _update_payment_attempt(request, PaymentTransaction.Status.FAILED)


@require_POST
@login_required
def cancel_razorpay_payment(request):
    return _update_payment_attempt(request, PaymentTransaction.Status.CANCELLED)
