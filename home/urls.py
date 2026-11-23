from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("products/<int:product_id>/", views.product_detail, name="product_detail"),
    path("accounts/login/", views.SharedLoginView.as_view(), name="login"),
    path(
        "accounts/password-reset/",
        views.password_reset_request,
        name="password_reset",
    ),
    path(
        "accounts/password-reset/verify/",
        views.password_reset_verify,
        name="password_reset_verify",
    ),
    path(
        "accounts/password-reset/resend/",
        views.password_reset_resend,
        name="password_reset_resend",
    ),
    path(
        "accounts/password-reset/complete/",
        views.password_reset_complete,
        name="password_reset_complete",
    ),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("accounts/register/", views.register, name="register"),
    path("accounts/verify-otp/", views.verify_otp, name="verify_otp"),
    path("accounts/resend-otp/", views.resend_otp, name="resend_otp"),
    path("account/", views.account, name="account"),
    path("staff/", views.staff_dashboard, name="staff_dashboard"),
    path(
        "staff/orders/<int:order_id>/",
        views.staff_order_detail,
        name="staff_order_detail",
    ),
    path(
        "staff/orders/<int:order_id>/status/",
        views.staff_advance_order,
        name="staff_advance_order",
    ),
    path(
        "staff/orders/<int:order_id>/cod-payment/",
        views.staff_update_cod_payment,
        name="staff_update_cod_payment",
    ),
    path("orders/tracking/", views.order_tracking, name="order_tracking"),
    path("orders/<int:order_id>/cancel/", views.cancel_order, name="cancel_order"),
    path(
        "orders/<int:order_id>/receipt/",
        views.payment_receipt,
        name="payment_receipt",
    ),
    path(
        "orders/<int:order_id>/delete/",
        views.delete_cancelled_order,
        name="delete_cancelled_order",
    ),
    path("orders/create/", views.place_order, name="place_order"),
    path(
        "payments/razorpay/verify/",
        views.verify_razorpay_payment,
        name="verify_razorpay_payment",
    ),
    path(
        "payments/razorpay/fail/",
        views.fail_razorpay_payment,
        name="fail_razorpay_payment",
    ),
    path(
        "payments/razorpay/cancel/",
        views.cancel_razorpay_payment,
        name="cancel_razorpay_payment",
    ),
]
