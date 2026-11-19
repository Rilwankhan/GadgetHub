from django.contrib.auth.views import LogoutView
from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("products/<int:product_id>/", views.product_detail, name="product_detail"),
    path("accounts/login/", views.SharedLoginView.as_view(), name="login"),
    path(
        "accounts/password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="home/password_reset.html",
            email_template_name="home/password_reset_email.txt",
            subject_template_name="home/password_reset_subject.txt",
            extra_context={
                "mode": "request",
                "page_title": "Reset your password",
                "page_description": "Enter the email address on your account and we’ll send password reset instructions.",
            },
        ),
        name="password_reset",
    ),
    path(
        "accounts/password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="home/password_reset.html",
            extra_context={
                "mode": "sent",
                "page_title": "Check your email",
                "page_description": "If an active account uses that email, password reset instructions have been sent.",
            },
        ),
        name="password_reset_done",
    ),
    path(
        "accounts/reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="home/password_reset.html",
            extra_context={
                "mode": "confirm",
                "page_title": "Choose a new password",
                "page_description": "Enter and confirm your new password.",
            },
        ),
        name="password_reset_confirm",
    ),
    path(
        "accounts/reset/done/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="home/password_reset.html",
            extra_context={
                "mode": "complete",
                "page_title": "Password updated",
                "page_description": "Your password has been changed. Sign in with your new password.",
            },
        ),
        name="password_reset_complete",
    ),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("accounts/register/", views.register, name="register"),
    path("account/", views.account, name="account"),
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
