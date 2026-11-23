import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.db import IntegrityError, transaction
from django.template import Context, Template
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from .models import (
    Category,
    CustomerProfile,
    EmailOTP,
    Order,
    OrderItem,
    OrderPaymentEvent,
    OrderTrackingEvent,
    PaymentTransaction,
    Product,
)
from .payments import RazorpayError


class StorefrontOrderTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="customer",
            password="StrongPassword123!",
            first_name="Test",
            last_name="Customer",
            email="customer@example.com",
        )
        CustomerProfile.objects.create(
            user=self.user,
            mobile="+91 98765 43210",
            address="1 Saved Street",
        )
        self.category, _ = Category.objects.get_or_create(name="Audio")
        self.product = Product.objects.create(
            name="Headphones",
            category=self.category,
            price="2499.00",
            description="Clear sound",
            image="https://example.com/headphones.jpg",
            stock_quantity=10,
        )
        self.order_payload = {
            "name": "Test Customer",
            "email": "customer@example.com",
            "mobile": "+91 98765 43210",
            "address": "1 Test Street",
            "payment_method": Order.PaymentMethod.CASH_ON_DELIVERY,
            "cart": [{"id": self.product.pk, "quantity": 2, "price": "0.01"}],
        }

    def create_order(self, user=None, status=Order.Status.PENDING):
        return Order.objects.create(
            user=user or self.user,
            full_name="Test Customer",
            email="customer@example.com",
            phone="9876543210",
            address="1 Test Street",
            status=status,
            total="99.00",
        )

    def test_guest_must_sign_in_to_place_order(self):
        response = self.client.post(
            reverse("place_order"),
            data=json.dumps(self.order_payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)
        self.assertIn("next=%2F%3Fcheckout%3D1", response.url)
        self.assertFalse(Order.objects.exists())

    def test_order_uses_database_price_and_is_listed_only_for_its_account(self):
        self.client.force_login(self.user)
        self.order_payload.update(
            name="Untrusted Checkout Name",
            email="not-the-account@example.com",
            mobile="1111111111",
            address="Untrusted Checkout Address",
        )
        response = self.client.post(
            reverse("place_order"),
            data=json.dumps(self.order_payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], "4998.00")
        order = Order.objects.get(user=self.user)
        self.assertEqual(order.full_name, "Test Customer")
        self.assertEqual(order.email, "customer@example.com")
        self.assertEqual(order.phone, "+91 98765 43210")
        self.assertEqual(order.address, "1 Saved Street")
        self.assertEqual(order.total, 4998)
        self.assertEqual(order.payment_status, Order.PaymentStatus.UNPAID)
        self.assertEqual(order.items.get().unit_price, 2499)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 8)
        self.assertTrue(order.stock_deducted)
        tracking_event = order.tracking_events.get()
        self.assertEqual(tracking_event.status, Order.Status.PENDING)
        self.assertEqual(tracking_event.updated_by, None)

        another_user = get_user_model().objects.create_user(
            username="another", password="StrongPassword123!"
        )
        self.client.force_login(another_user)
        account_response = self.client.get(reverse("account"))
        self.assertNotContains(account_response, "Test Customer")

    def test_out_of_stock_checkout_is_rejected_without_creating_an_order(self):
        self.client.force_login(self.user)
        self.product.stock_quantity = 1
        self.product.save(update_fields=["stock_quantity"])

        response = self.client.post(
            reverse("place_order"),
            data=json.dumps(self.order_payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(Order.objects.exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 1)

    def test_out_of_stock_is_visible_on_product_detail(self):
        self.product.stock_quantity = 0
        self.product.save(update_fields=["stock_quantity"])

        response = self.client.get(
            reverse("product_detail", args=[self.product.pk])
        )

        self.assertContains(response, "Out of Stock")
        self.assertNotContains(response, f"?order={self.product.pk}")

    def test_cancelling_order_restores_reserved_stock_only_once(self):
        self.client.force_login(self.user)
        create_response = self.client.post(
            reverse("place_order"),
            data=json.dumps(self.order_payload),
            content_type="application/json",
        )
        order_id = create_response.json()["order_id"]
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 8)

        first_cancel = self.client.post(reverse("cancel_order", args=[order_id]))
        second_cancel = self.client.post(reverse("cancel_order", args=[order_id]))

        self.assertRedirects(first_cancel, reverse("account"))
        self.assertRedirects(second_cancel, reverse("account"))
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertFalse(Order.objects.get(pk=order_id).stock_deducted)

    def test_cash_on_delivery_order_has_no_payment_receipt(self):
        self.client.force_login(self.user)
        order = self.create_order()

        response = self.client.get(
            reverse("payment_receipt", args=[order.pk])
        )
        account_response = self.client.get(reverse("account"))

        self.assertEqual(response.status_code, 404)
        self.assertNotContains(account_response, "Download payment receipt")

    @override_settings(RAZORPAY_ENABLED=False)
    def test_checkout_shows_saved_details_without_customer_entry_fields(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("home"))

        self.assertContains(response, "Your saved delivery details")
        self.assertContains(response, "1 Saved Street")
        self.assertNotContains(response, 'id="customer-name"')
        self.assertNotContains(response, 'id="customer-email"')
        self.assertNotContains(response, 'id="customer-mobile"')
        self.assertNotContains(response, 'id="customer-address"')
        self.assertNotContains(response, "Arrange with the store")
        self.assertContains(response, 'value="razorpay" disabled')

    def test_arrange_with_store_is_no_longer_an_available_payment_method(self):
        self.client.force_login(self.user)
        self.order_payload["payment_method"] = Order.PaymentMethod.ARRANGE_WITH_STORE

        response = self.client.post(
            reverse("place_order"),
            data=json.dumps(self.order_payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Order.objects.exists())

    def test_rejects_invalid_quantity_without_creating_order(self):
        self.client.force_login(self.user)
        self.order_payload["cart"][0]["quantity"] = 0

        response = self.client.post(
            reverse("place_order"),
            data=json.dumps(self.order_payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Order.objects.exists())

    def test_owner_can_cancel_an_order_within_24_hours_before_shipping(self):
        self.client.force_login(self.user)
        order = self.create_order()
        order.created_at = timezone.now() - timedelta(hours=23, minutes=59)
        order.save(update_fields=["created_at"])

        response = self.client.post(reverse("cancel_order", args=[order.pk]))

        self.assertRedirects(response, reverse("account"))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertEqual(order.payment_status, Order.PaymentStatus.UNPAID)
        event = order.tracking_events.get()
        self.assertEqual(event.status, Order.Status.CANCELLED)
        self.assertEqual(event.updated_by, self.user)

    def test_cancellation_closes_after_24_hours(self):
        self.client.force_login(self.user)
        order = self.create_order()
        order.created_at = timezone.now() - timedelta(hours=24, seconds=1)
        order.save(update_fields=["created_at"])

        account_response = self.client.get(reverse("account"))
        self.assertContains(
            account_response,
            "The 24-hour cancellation window has ended.",
        )

        response = self.client.post(reverse("cancel_order", args=[order.pk]))

        self.assertRedirects(response, reverse("account"))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertFalse(
            order.tracking_events.filter(status=Order.Status.CANCELLED).exists()
        )

    def test_cancellation_is_unavailable_after_shipping(self):
        self.client.force_login(self.user)
        order = self.create_order(status=Order.Status.SHIPPED)

        account_response = self.client.get(reverse("account"))
        self.assertContains(
            account_response,
            "This order can no longer be cancelled because it has shipped.",
        )

        response = self.client.post(reverse("cancel_order", args=[order.pk]))

        self.assertRedirects(response, reverse("account"))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SHIPPED)

    def test_only_cancelled_orders_can_be_deleted_by_their_owner(self):
        self.client.force_login(self.user)
        cancelled_order = self.create_order(status=Order.Status.CANCELLED)
        active_order = self.create_order()
        delivered_order = self.create_order(status=Order.Status.DELIVERED)
        another_user = get_user_model().objects.create_user(
            username="another", password="StrongPassword123!"
        )
        another_cancelled_order = self.create_order(
            user=another_user, status=Order.Status.CANCELLED
        )

        self.assertEqual(
            self.client.post(
                reverse("delete_cancelled_order", args=[active_order.pk])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("delete_cancelled_order", args=[delivered_order.pk])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("delete_cancelled_order", args=[another_cancelled_order.pk])
            ).status_code,
            404,
        )
        response = self.client.post(
            reverse("delete_cancelled_order", args=[cancelled_order.pk])
        )

        self.assertRedirects(response, reverse("account"))
        self.assertFalse(Order.objects.filter(pk=cancelled_order.pk).exists())
        self.assertTrue(Order.objects.filter(pk=active_order.pk).exists())
        self.assertTrue(Order.objects.filter(pk=delivered_order.pk).exists())

    def test_user_cannot_cancel_another_users_order_or_read_their_tracking(self):
        self.client.force_login(self.user)
        own_order = self.create_order()
        another_user = get_user_model().objects.create_user(
            username="another", password="StrongPassword123!"
        )
        another_order = self.create_order(user=another_user)

        cancel_response = self.client.post(
            reverse("cancel_order", args=[another_order.pk])
        )
        tracking_response = self.client.get(reverse("order_tracking"))

        self.assertEqual(cancel_response.status_code, 404)
        self.assertEqual(tracking_response.status_code, 200)
        self.assertEqual(
            [order["id"] for order in tracking_response.json()["orders"]],
            [own_order.pk],
        )

    def test_catalog_is_database_backed_and_register_page_is_available(self):
        response = self.client.get(reverse("home"))

        self.assertContains(response, self.product.name)
        self.assertContains(response, self.category.name)
        self.assertEqual(self.client.get(reverse("register")).status_code, 200)

    def test_product_detail_shows_description_and_struck_original_price(self):
        self.product.original_price = "2999.00"
        self.product.save(update_fields=["original_price"])

        response = self.client.get(
            reverse("product_detail", args=[self.product.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="navbar site-header"')
        self.assertContains(response, "Good tech, made easy.")
        self.assertContains(response, "About this product")
        self.assertContains(response, f'href="/?order={self.product.pk}"')
        self.assertContains(response, self.product.description)
        self.assertContains(response, "₹2499.00")
        self.assertContains(response, "<del class=\"product-original-price\">₹2999.00</del>")

    def test_inactive_product_detail_is_not_available(self):
        self.product.is_active = False
        self.product.save(update_fields=["is_active"])

        response = self.client.get(
            reverse("product_detail", args=[self.product.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_delivered_order_shows_only_product_and_delivery_confirmation(self):
        self.client.force_login(self.user)
        order = self.create_order(status=Order.Status.DELIVERED)
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name=self.product.name,
            unit_price=self.product.price,
            quantity=2,
        )
        delivered_at = timezone.now().replace(microsecond=0)
        OrderTrackingEvent.objects.create(
            order=order,
            status=Order.Status.DELIVERED,
            description="Your order is now delivered.",
            created_at=delivered_at,
        )

        response = self.client.get(reverse("account"))

        self.assertContains(response, self.product.name)
        self.assertContains(response, "Status: Delivered")
        self.assertContains(response, "Delivered Date &amp; Time:")
        self.assertContains(
            response,
            f'datetime="{timezone.localtime(delivered_at).isoformat()}"',
        )
        self.assertContains(response, "✓")
        self.assertNotContains(response, f"ORDER #{order.pk}")
        self.assertNotContains(response, "₹99.00")
        self.assertNotContains(response, "2 ×")
        self.assertNotContains(response, "Track your order")
        self.assertNotContains(response, "Tracking updates")

        tracking_response = self.client.get(reverse("order_tracking"))
        delivered_order = tracking_response.json()["orders"][0]
        self.assertEqual(delivered_order["product_names"], [self.product.name])
        self.assertEqual(delivered_order["delivered_at"], delivered_at.isoformat())

    def test_cancelled_order_uses_confirmation_design_and_shows_both_timestamps(self):
        self.client.force_login(self.user)
        order = self.create_order(status=Order.Status.CANCELLED)
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name=self.product.name,
            unit_price=self.product.price,
            quantity=1,
        )
        booked_at = timezone.now().replace(microsecond=0) - timedelta(days=1)
        cancelled_at = timezone.now().replace(microsecond=0)
        order.created_at = booked_at
        order.save(update_fields=["created_at"])
        OrderTrackingEvent.objects.create(
            order=order,
            status=Order.Status.CANCELLED,
            description="You cancelled this order.",
            created_at=cancelled_at,
        )

        response = self.client.get(reverse("account"))

        self.assertContains(response, self.product.name)
        self.assertContains(response, "Status: Cancelled")
        self.assertContains(response, "Booking Date &amp; Time:")
        self.assertContains(response, "Cancellation Date &amp; Time:")
        self.assertContains(
            response,
            f'datetime="{timezone.localtime(booked_at).isoformat()}"',
        )
        self.assertContains(
            response,
            f'datetime="{timezone.localtime(cancelled_at).isoformat()}"',
        )
        self.assertContains(response, "✕")
        self.assertContains(response, "Delete Order")
        self.assertNotContains(response, "Cancel order")
        tracking_order = self.client.get(reverse("order_tracking")).json()["orders"][0]
        self.assertEqual(tracking_order["cancelled_at"], cancelled_at.isoformat())

    def test_registration_requires_email_otp_verification(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "newcustomer",
                "first_name": "New",
                "last_name": "Customer",
                "email": "newcustomer@example.com",
                "mobile": "9876543210",
                "address": "1 New Street",
                "password1": "V3ryLongPassword!Test",
                "password2": "V3ryLongPassword!Test",
            },
        )

        user = get_user_model().objects.get(username="newcustomer")
        self.assertRedirects(
            response,
            reverse("verify_otp") + f"?uid={user.pk}",
        )
        self.assertFalse(user.is_active)
        self.assertTrue(EmailOTP.objects.filter(user=user).exists())
        self.assertEqual(user.profile.mobile, "9876543210")
        self.assertEqual(user.profile.address, "1 New Street")

        otp = EmailOTP.objects.get(user=user)
        otp_page = self.client.get(reverse("verify_otp"), {"uid": user.pk})
        self.assertContains(otp_page, "Verify your email")

        otp.otp_hash = EmailOTP._hash("123456")
        otp.save(update_fields=["otp_hash"])
        verify = self.client.post(
            reverse("verify_otp"),
            {"uid": user.pk, "otp": "123456"},
        )
        self.assertRedirects(verify, reverse("login"))
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertFalse(EmailOTP.objects.filter(user=user).exists())

    def test_registration_continues_to_checkout_when_requested(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "checkoutcustomer",
                "first_name": "Checkout",
                "last_name": "Customer",
                "email": "checkout@example.com",
                "mobile": "9876543210",
                "address": "1 Checkout Street",
                "password1": "V3ryLongPassword!Test",
                "password2": "V3ryLongPassword!Test",
                "next": "/?checkout=1",
            },
        )

        user = get_user_model().objects.get(username="checkoutcustomer")
        self.assertRedirects(
            response,
            reverse("verify_otp") + f"?uid={user.pk}&next=%2F%3Fcheckout%3D1",
        )
        self.assertFalse(user.is_active)

    def test_login_redirects_users_and_admins_to_their_roles(self):
        response = self.client.post(
            reverse("login"),
            {
                "username": "customer",
                "password": "StrongPassword123!",
                "next": "/admin/",
            },
        )
        self.assertRedirects(response, reverse("account"))

        self.client.logout()
        admin_user = get_user_model().objects.create_superuser(
            username="roleadmin",
            email="admin@example.com",
            password="StrongPassword123!",
        )
        response = self.client.post(
            reverse("login"),
            {"username": admin_user.username, "password": "StrongPassword123!"},
        )
        self.assertRedirects(response, reverse("admin:index"))

    def test_login_continues_to_checkout_when_requested(self):
        response = self.client.post(
            reverse("login"),
            {
                "username": "customer",
                "password": "StrongPassword123!",
                "next": "/?checkout=1",
            },
        )

        self.assertRedirects(response, "/?checkout=1")

    def test_regular_user_cannot_open_admin_dashboard(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("admin:index"))
        order_updates_response = self.client.get(reverse("admin:order_updates"))
        payment_transactions_response = self.client.get(
            reverse("admin:home_paymenttransaction_changelist")
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response.url)
        self.assertEqual(order_updates_response.status_code, 302)
        self.assertEqual(payment_transactions_response.status_code, 302)

    def test_account_updates_signed_in_users_profile(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("account"),
            {
                "form_type": "profile",
                "first_name": "Test",
                "last_name": "Customer",
                "email": "updated@example.com",
                "mobile": "+91 98765 43210",
                "address": "42 Example Road",
                "landmark": "Near the clock tower",
            },
        )

        self.assertRedirects(response, reverse("account"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "updated@example.com")
        self.assertEqual(self.user.profile.address, "42 Example Road")
        self.assertEqual(self.user.profile.landmark, "Near the clock tower")

    def test_customer_account_keeps_customer_profile_fields(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("account"))

        self.assertContains(response, "Personal Details")
        self.assertContains(response, 'aria-expanded="false"')
        self.assertContains(response, "First name")
        self.assertContains(response, "Address")
        self.assertNotContains(response, "Employee ID")
        self.assertNotContains(response, "Admin Profile")
        self.assertNotContains(response, reverse("admin:home_paymenttransaction_changelist"))
        self.assertContains(response, "Change password")
        self.assertRegex(
            response.content.decode(),
            r'<div\s+id="personal-details-panel"\s+class="personal-details-panel"\s+hidden\s*>',
        )
        self.assertRegex(
            response.content.decode(),
            r'<div\s+id="account-security-panel"\s+hidden\s*>',
        )

    def test_admin_profile_hides_details_and_updates_staff_contact_information(self):
        admin_user = get_user_model().objects.create_superuser(
            username="profileadmin",
            email="admin@example.com",
            password="StrongPassword123!",
            first_name="Admin",
            last_name="Operator",
        )
        CustomerProfile.objects.create(
            user=admin_user,
            employee_id="GH-1001",
            mobile="+91 98765 43210",
            address="Staff-only address",
            landmark="Staff-only landmark",
        )
        self.client.force_login(admin_user)

        response = self.client.get(reverse("account"))

        self.assertContains(response, "Personal Details")
        self.assertContains(response, "Admin Profile")
        self.assertContains(response, "Employee ID")
        self.assertNotContains(response, "First name")
        self.assertNotContains(response, "Last name")
        self.assertNotContains(response, "Address")
        self.assertNotContains(response, "Landmark")
        self.assertContains(response, 'aria-expanded="false"')
        self.assertNotContains(response, "Your GadgetHub History")
        self.assertRegex(
            response.content.decode(),
            r'<div\s+id="personal-details-panel"\s+class="personal-details-panel"\s+hidden\s*>',
        )
        self.assertRegex(
            response.content.decode(),
            r'<div\s+id="account-security-panel"\s+hidden\s*>',
        )

        response = self.client.post(
            reverse("account"),
            {
                "form_type": "profile",
                "username": "updatedadmin",
                "first_name": "Store",
                "last_name": "Manager",
                "email": "updated-admin@example.com",
                "employee_id": "GH-1042",
                "mobile": "+91 98765 43210",
                "address": "42 Example Road",
                "landmark": "Near the clock tower",
            },
        )

        self.assertRedirects(response, reverse("account"))
        admin_user.refresh_from_db()
        self.assertEqual(admin_user.username, "updatedadmin")
        self.assertEqual(admin_user.first_name, "Admin")
        self.assertEqual(admin_user.last_name, "Operator")
        self.assertEqual(admin_user.email, "updated-admin@example.com")
        self.assertEqual(admin_user.profile.employee_id, "GH-1042")
        self.assertEqual(admin_user.profile.mobile, "+91 98765 43210")
        self.assertEqual(admin_user.profile.address, "Staff-only address")
        self.assertEqual(admin_user.profile.landmark, "Staff-only landmark")

    def test_profile_edit_form_reopens_when_customer_input_is_invalid(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("account"),
            {
                "form_type": "profile",
                "first_name": "Test",
                "last_name": "Customer",
                "email": "not-an-email",
                "mobile": "",
                "address": "",
                "landmark": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-expanded="true"')
        self.assertContains(response, "Enter a valid email address.")
        self.assertRegex(
            response.content.decode(),
            r'<div\s+data-profile-editor\s*>',
        )
        self.assertRegex(
            response.content.decode(),
            r'<div\s+data-profile-summary\s+hidden\s*>',
        )

    def test_admin_password_panel_opens_again_when_password_form_has_errors(self):
        admin_user = get_user_model().objects.create_superuser(
            username="passwordadmin",
            email="admin@example.com",
            password="StrongPassword123!",
        )
        self.client.force_login(admin_user)

        response = self.client.post(
            reverse("account"),
            {
                "form_type": "password",
                "old_password": "incorrect",
                "new_password1": "EvenStrongerPassword123!",
                "new_password2": "EvenStrongerPassword123!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-expanded="true"')
        self.assertRegex(
            response.content.decode(),
            r'<div\s+id="account-security-panel"\s*>',
        )
        self.assertContains(response, "Your old password was entered incorrectly.")

    def test_forgot_password_sends_a_working_reset_otp(self):
        self.user.email = "customer@example.com"
        self.user.save(update_fields=["email"])

        response = self.client.get(reverse("login"))
        self.assertContains(response, reverse("password_reset"))

        response = self.client.post(
            reverse("password_reset"), {"email": self.user.email}
        )

        user = get_user_model().objects.get(pk=self.user.pk)
        otp = EmailOTP.objects.get(user=user)
        self.assertRedirects(
            response,
            reverse("password_reset_verify") + f"?uid={user.pk}",
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("6-digit verification code", mail.outbox[0].body)

        otp.otp_hash = EmailOTP._hash("654321")
        otp.save(update_fields=["otp_hash"])
        verify_page = self.client.post(
            reverse("password_reset_verify"),
            {"uid": user.pk, "otp": "654321"},
        )
        self.assertRedirects(verify_page, reverse("password_reset_complete"))

        confirm_page = self.client.get(reverse("password_reset_complete"))
        response = self.client.post(
            reverse("password_reset_complete"),
            {
                "new_password1": "ResetPassword123!More",
                "new_password2": "ResetPassword123!More",
            },
        )

        self.assertRedirects(response, reverse("login"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("ResetPassword123!More"))

    def test_user_can_change_password_without_losing_session(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("account"),
            {
                "form_type": "password",
                "old_password": "StrongPassword123!",
                "new_password1": "EvenStrongerPassword123!",
                "new_password2": "EvenStrongerPassword123!",
            },
        )

        self.assertRedirects(response, reverse("account"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("EvenStrongerPassword123!"))
        self.assertEqual(self.client.get(reverse("account")).status_code, 200)


@override_settings(
    RAZORPAY_ENABLED=True,
    RAZORPAY_KEY_ID="rzp_test_public",
    RAZORPAY_KEY_SECRET="test_secret",
)
class RazorpayPaymentTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="paymentcustomer",
            password="StrongPassword123!",
            first_name="Payment",
            last_name="Customer",
            email="payment@example.com",
        )
        CustomerProfile.objects.create(
            user=self.user,
            mobile="9876543210",
            address="1 Payment Street",
        )
        self.category = Category.objects.create(name="Payment Audio")
        self.product = Product.objects.create(
            name="Payment Headphones",
            category=self.category,
            price="2499.00",
            description="Payment test product",
            image="https://example.com/payment-headphones.jpg",
            stock_quantity=10,
        )
        self.client.force_login(self.user)
        self.payload = {
            "name": "Payment Customer",
            "email": "payment@example.com",
            "mobile": "9876543210",
            "address": "1 Payment Street",
            "payment_method": Order.PaymentMethod.RAZORPAY,
            "cart": [{"id": self.product.pk, "quantity": 2}],
        }

    def create_pending_payment(self):
        with patch(
            "home.views.create_razorpay_order",
            return_value={
                "id": "order_test123",
                "amount": 499800,
                "currency": "INR",
            },
        ) as create_order:
            response = self.client.post(
                reverse("place_order"),
                data=json.dumps(self.payload),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        create_order.assert_called_once_with(499800, f"gh-order-{response.json()['order_id']}")
        return response.json()

    def payment_signature(self, payment_id="pay_test123"):
        message = f"order_test123|{payment_id}".encode("utf-8")
        return hmac.new(
            b"test_secret",
            message,
            hashlib.sha256,
        ).hexdigest()

    def test_checkout_exposes_public_key_only_when_gateway_is_configured(self):
        response = self.client.get(reverse("home"))

        self.assertContains(response, 'value="razorpay"')
        self.assertContains(response, 'data-razorpay-key-id="rzp_test_public"')
        self.assertNotContains(response, "test_secret")
        self.assertContains(
            response, "https://checkout.razorpay.com/v1/checkout.js"
        )

    def verify_payload(self, order_id, payment_id="pay_test123", signature=None):
        return {
            "order_id": order_id,
            "razorpay_order_id": "order_test123",
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature or self.payment_signature(payment_id),
        }

    def test_creates_provider_order_from_server_price_and_exposes_only_key_id(self):
        result = self.create_pending_payment()
        order = Order.objects.get(pk=result["order_id"])
        payment = order.payment_transactions.get()
        account_response = self.client.get(reverse("account"))
        tracking_response = self.client.get(reverse("order_tracking"))

        self.assertEqual(result["razorpay"]["key_id"], "rzp_test_public")
        self.assertEqual(result["razorpay"]["amount"], 499800)
        self.assertNotIn("test_secret", json.dumps(result))
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertNotContains(account_response, f"ORDER #{order.pk}")
        self.assertNotIn(order.pk, [row["id"] for row in tracking_response.json()["orders"]])
        self.assertEqual(payment.amount, Decimal("4998.00"))
        self.assertEqual(payment.status, PaymentTransaction.Status.CREATED)

    @patch(
        "home.views.fetch_razorpay_payment",
        return_value={
            "id": "pay_test123",
            "order_id": "order_test123",
            "amount": 499800,
            "currency": "INR",
            "status": "captured",
            "captured": True,
        },
    )
    def test_signature_and_captured_provider_payment_mark_order_paid(self, _fetch):
        result = self.create_pending_payment()
        order_id = result["order_id"]
        response = self.client.post(
            reverse("verify_razorpay_payment"),
            data=json.dumps(self.verify_payload(order_id)),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(pk=order_id)
        payment = order.payment_transactions.get()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(payment.status, PaymentTransaction.Status.PAID)
        self.assertEqual(payment.razorpay_payment_id, "pay_test123")
        self.assertEqual(order.status, Order.Status.CONFIRMED)
        self.assertTrue(
            order.tracking_events.filter(
                status=Order.Status.CONFIRMED,
                description="Payment received and verified. Your order is confirmed.",
            ).exists()
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 8)
        self.assertTrue(order.stock_deducted)
        duplicate_response = self.client.post(
            reverse("verify_razorpay_payment"),
            data=json.dumps(self.verify_payload(order_id)),
            content_type="application/json",
        )
        self.assertEqual(duplicate_response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 8)
        self.assertContains(self.client.get(reverse("account")), f"ORDER #{order.pk}")

    @patch(
        "home.views.fetch_razorpay_payment",
        return_value={
            "id": "pay_test123",
            "order_id": "order_test123",
            "amount": 499800,
            "currency": "INR",
            "status": "captured",
            "captured": True,
        },
    )
    def test_server_fetch_confirms_payment_after_signature_check(self, fetch_payment):
        result = self.create_pending_payment()
        response = self.client.post(
            reverse("verify_razorpay_payment"),
            data=json.dumps(self.verify_payload(result["order_id"])),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        fetch_payment.assert_called_once_with("pay_test123")
        self.assertEqual(
            Order.objects.get(pk=result["order_id"]).payment_status,
            Order.PaymentStatus.PAID,
        )

    @patch(
        "home.views.fetch_razorpay_payment",
        return_value={
            "id": "pay_test123",
            "order_id": "order_test123",
            "amount": 499800,
            "currency": "INR",
            "status": "captured",
            "captured": True,
        },
    )
    def test_verified_razorpay_payment_can_download_receipt(self, _fetch):
        result = self.create_pending_payment()
        order_id = result["order_id"]
        verify_response = self.client.post(
            reverse("verify_razorpay_payment"),
            data=json.dumps(self.verify_payload(order_id)),
            content_type="application/json",
        )

        self.assertEqual(verify_response.status_code, 200)
        receipt = self.client.get(reverse("payment_receipt", args=[order_id]))
        account_response = self.client.get(reverse("account"))

        self.assertEqual(receipt.status_code, 200)
        self.assertEqual(receipt["Content-Type"], "application/pdf")
        self.assertIn("private", receipt["Cache-Control"])
        self.assertIn("no-store", receipt["Cache-Control"])
        self.assertEqual(
            receipt["Content-Disposition"],
            f'attachment; filename="GadgetHub-Receipt-Order-{order_id}.pdf"',
        )
        self.assertTrue(receipt.content.startswith(b"%PDF-"))
        self.assertNotIn(b"test_secret", receipt.content)
        self.assertContains(
            account_response,
            reverse("payment_receipt", args=[order_id]),
        )

        another_user = get_user_model().objects.create_user(
            username="receiptother",
            password="StrongPassword123!",
        )
        self.client.force_login(another_user)
        self.assertEqual(
            self.client.get(reverse("payment_receipt", args=[order_id])).status_code,
            404,
        )

    def test_unpaid_razorpay_order_has_no_receipt(self):
        result = self.create_pending_payment()

        response = self.client.get(
            reverse("payment_receipt", args=[result["order_id"]])
        )
        account_response = self.client.get(reverse("account"))

        self.assertEqual(response.status_code, 404)
        self.assertNotContains(account_response, "Download payment receipt")

    @patch(
        "home.views.fetch_razorpay_payment",
        return_value={
            "id": "pay_test123",
            "order_id": "order_test123",
            "amount": 499800,
            "currency": "INR",
            "status": "authorized",
            "captured": False,
        },
    )
    def test_authorized_but_uncaptured_payment_stays_pending(self, _fetch):
        result = self.create_pending_payment()
        response = self.client.post(
            reverse("verify_razorpay_payment"),
            data=json.dumps(self.verify_payload(result["order_id"])),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        order = Order.objects.get(pk=result["order_id"])
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
        self.assertEqual(
            order.payment_transactions.get().status,
            PaymentTransaction.Status.CREATED,
        )

    @patch("home.views.fetch_razorpay_payment")
    def test_invalid_signature_never_queries_provider_or_marks_order_paid(
        self, fetch_payment
    ):
        result = self.create_pending_payment()
        response = self.client.post(
            reverse("verify_razorpay_payment"),
            data=json.dumps(
                self.verify_payload(result["order_id"], signature="0" * 64)
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        fetch_payment.assert_not_called()
        order = Order.objects.get(pk=result["order_id"])
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
        self.assertEqual(
            order.payment_transactions.get().status,
            PaymentTransaction.Status.CREATED,
        )

    @patch(
        "home.views.fetch_razorpay_payment",
        return_value={
            "id": "pay_test123",
            "order_id": "order_test123",
            "amount": 499800,
            "currency": "INR",
            "status": "failed",
            "captured": False,
            "error_code": "PAYMENT_FAILED",
            "error_description": "Payment was declined.",
        },
    )
    def test_provider_failure_is_recorded_without_marking_order_paid(self, _fetch):
        result = self.create_pending_payment()
        response = self.client.post(
            reverse("verify_razorpay_payment"),
            data=json.dumps(self.verify_payload(result["order_id"])),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        order = Order.objects.get(pk=result["order_id"])
        payment = order.payment_transactions.get()
        self.assertEqual(order.payment_status, Order.PaymentStatus.FAILED)
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(payment.status, PaymentTransaction.Status.FAILED)
        self.assertEqual(payment.error_code, "PAYMENT_FAILED")
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertFalse(order.stock_deducted)
        self.assertNotContains(self.client.get(reverse("account")), f"ORDER #{order.pk}")

    @patch(
        "home.views.create_razorpay_order",
        side_effect=RazorpayError("provider unavailable"),
    )
    def test_razorpay_order_creation_failure_restores_stock(self, _create_order):
        response = self.client.post(
            reverse("place_order"),
            data=json.dumps(self.payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 502)
        order = Order.objects.get(user=self.user)
        self.assertEqual(order.payment_status, Order.PaymentStatus.FAILED)
        self.assertFalse(order.stock_deducted)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)

    @patch(
        "home.views.fetch_razorpay_order_payments",
        return_value={"items": []},
    )
    def test_cancelled_checkout_is_saved_and_owner_scoped(self, _fetch_payments):
        result = self.create_pending_payment()
        order_id = result["order_id"]
        another_user = get_user_model().objects.create_user(
            username="anotherpaymentcustomer",
            password="StrongPassword123!",
        )
        self.client.force_login(another_user)
        response = self.client.post(
            reverse("cancel_razorpay_payment"),
            data=json.dumps(
                {"order_id": order_id, "razorpay_order_id": "order_test123"}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            Order.objects.get(pk=order_id).payment_status,
            Order.PaymentStatus.PENDING,
        )

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("cancel_razorpay_payment"),
            data=json.dumps(
                {"order_id": order_id, "razorpay_order_id": "order_test123"}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(pk=order_id)
        self.assertEqual(order.payment_status, Order.PaymentStatus.CANCELLED)
        self.assertEqual(
            order.payment_transactions.get().status,
            PaymentTransaction.Status.CANCELLED,
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)

    @patch(
        "home.views.fetch_razorpay_order_payments",
        return_value={
            "items": [
                {
                    "id": "pay_failed123",
                    "order_id": "order_test123",
                    "amount": 499800,
                    "currency": "INR",
                    "status": "failed",
                    "error_code": "PAYMENT_FAILED",
                    "error_description": "Payment declined.",
                }
            ]
        },
    )
    def test_closed_checkout_records_provider_confirmed_failure(self, _fetch_payments):
        result = self.create_pending_payment()
        response = self.client.post(
            reverse("cancel_razorpay_payment"),
            data=json.dumps(
                {
                    "order_id": result["order_id"],
                    "razorpay_order_id": "order_test123",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payment_status"], PaymentTransaction.Status.FAILED)
        order = Order.objects.get(pk=result["order_id"])
        payment = order.payment_transactions.get()
        self.assertEqual(order.payment_status, Order.PaymentStatus.FAILED)
        self.assertEqual(payment.status, PaymentTransaction.Status.FAILED)
        self.assertEqual(payment.razorpay_payment_id, "pay_failed123")

    @patch(
        "home.views.fetch_razorpay_order_payments",
        return_value={
            "items": [
                {
                    "id": "pay_pending123",
                    "order_id": "order_test123",
                    "status": "authorized",
                }
            ]
        },
    )
    def test_checkout_cannot_be_cancelled_while_payment_is_authorized(
        self, _fetch_payments
    ):
        result = self.create_pending_payment()
        response = self.client.post(
            reverse("cancel_razorpay_payment"),
            data=json.dumps(
                {
                    "order_id": result["order_id"],
                    "razorpay_order_id": "order_test123",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        order = Order.objects.get(pk=result["order_id"])
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
        self.assertEqual(
            order.payment_transactions.get().status,
            PaymentTransaction.Status.CREATED,
        )

    @patch(
        "home.views.fetch_razorpay_payment",
        return_value={
            "id": "pay_test123",
            "order_id": "order_test123",
            "amount": 499800,
            "currency": "INR",
            "status": "captured",
            "captured": True,
        },
    )
    def test_client_cannot_mark_a_payment_failed_without_provider_confirmation(
        self, _fetch_payment
    ):
        result = self.create_pending_payment()
        response = self.client.post(
            reverse("fail_razorpay_payment"),
            data=json.dumps(
                {
                    "order_id": result["order_id"],
                    "razorpay_order_id": "order_test123",
                    "razorpay_payment_id": "pay_test123",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        order = Order.objects.get(pk=result["order_id"])
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
        self.assertEqual(
            order.payment_transactions.get().status,
            PaymentTransaction.Status.CREATED,
        )

    @patch(
        "home.views.fetch_razorpay_payment",
        return_value={
            "id": "pay_test123",
            "order_id": "order_test123",
            "amount": 499800,
            "currency": "INR",
            "status": "failed",
            "captured": False,
            "error_code": "PAYMENT_FAILED",
            "error_description": "Payment was declined.",
        },
    )
    def test_provider_confirmed_failure_is_saved(self, _fetch_payment):
        result = self.create_pending_payment()
        response = self.client.post(
            reverse("fail_razorpay_payment"),
            data=json.dumps(
                {
                    "order_id": result["order_id"],
                    "razorpay_order_id": "order_test123",
                    "razorpay_payment_id": "pay_test123",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(pk=result["order_id"])
        payment = order.payment_transactions.get()
        self.assertEqual(order.payment_status, Order.PaymentStatus.FAILED)
        self.assertEqual(payment.status, PaymentTransaction.Status.FAILED)
        self.assertEqual(payment.razorpay_payment_id, "pay_test123")

    @patch(
        "home.views.fetch_razorpay_payment",
        side_effect=[
            {
                "id": "pay_test123",
                "order_id": "order_test123",
                "amount": 499800,
                "currency": "INR",
                "status": "failed",
                "captured": False,
                "error_code": "PAYMENT_FAILED",
                "error_description": "Payment was declined.",
            },
            {
                "id": "pay_test123",
                "order_id": "order_test123",
                "amount": 499800,
                "currency": "INR",
                "status": "captured",
                "captured": True,
            },
        ],
    )
    def test_captured_payment_after_stock_release_is_flagged_without_overselling(
        self, fetch_payment
    ):
        result = self.create_pending_payment()
        failed_response = self.client.post(
            reverse("fail_razorpay_payment"),
            data=json.dumps(
                {
                    "order_id": result["order_id"],
                    "razorpay_order_id": "order_test123",
                    "razorpay_payment_id": "pay_test123",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(failed_response.status_code, 200)
        fetch_payment.assert_called_once_with("pay_test123")

        self.payload["payment_method"] = Order.PaymentMethod.CASH_ON_DELIVERY
        self.payload["cart"][0]["quantity"] = 10
        replacement_order = self.client.post(
            reverse("place_order"),
            data=json.dumps(self.payload),
            content_type="application/json",
        )
        self.assertEqual(replacement_order.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 0)

        captured_response = self.client.post(
            reverse("verify_razorpay_payment"),
            data=json.dumps(self.verify_payload(result["order_id"])),
            content_type="application/json",
        )

        self.assertEqual(captured_response.status_code, 409)
        self.assertEqual(fetch_payment.call_count, 2)
        self.assertIn("do not retry payment", captured_response.json()["error"])
        order = Order.objects.get(pk=result["order_id"])
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)
        self.assertFalse(order.stock_deducted)
        self.assertTrue(
            order.tracking_events.filter(
                description__contains="Administrator review is required."
            ).exists()
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 0)


class StaffOperationsTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin_user = user_model.objects.create_superuser(
            username="staffadmin",
            email="staffadmin@example.com",
            password="StrongPassword123!",
        )
        self.customer = user_model.objects.create_user(
            username="staffcustomer",
            email="customer@example.com",
            password="StrongPassword123!",
        )
        self.staff = user_model.objects.create_user(
            username="orderstaff",
            first_name="Order",
            last_name="Staff",
            password="StrongPassword123!",
        )
        self.staff_group, _ = Group.objects.get_or_create(
            name="GadgetHub Staff"
        )
        self.staff.groups.add(self.staff_group)
        self.category = Category.objects.create(name="Staff Operations")
        self.product = Product.objects.create(
            name="Staff Test Product",
            category=self.category,
            price="100.00",
            description="A product for staff tests.",
            image="https://example.com/staff-product.jpg",
            stock_quantity=9,
        )
        self.order = Order.objects.create(
            user=self.customer,
            full_name="Staff Customer",
            email="customer@example.com",
            phone="9876543210",
            address="1 Staff Street",
            payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
            payment_status=Order.PaymentStatus.UNPAID,
            total="100.00",
            stock_deducted=True,
        )
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            unit_price=self.product.price,
            quantity=1,
        )

    def test_staff_login_redirects_to_separate_dashboard(self):
        response = self.client.post(
            reverse("login"),
            {"username": "orderstaff", "password": "StrongPassword123!"},
        )

        self.assertRedirects(response, reverse("staff_dashboard"))
        dashboard = self.client.get(reverse("staff_dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "Staff Customer")
        self.assertContains(
            dashboard,
            reverse("staff_order_detail", args=[self.order.pk]),
        )

        PaymentTransaction.objects.create(
            order=self.order,
            amount="100.00",
            status=PaymentTransaction.Status.CREATED,
        )
        detail = self.client.get(
            reverse("staff_order_detail", args=[self.order.pk])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Staff Test Product")
        self.assertContains(detail, "1 Staff Street")
        self.assertContains(detail, "Back to staff dashboard")
        self.assertContains(detail, reverse("home"))
        self.assertContains(detail, '<details class="staff-order-history">')
        self.assertNotContains(detail, '<details class="staff-order-history" open>')
        self.assertEqual(
            detail.content.decode().count('class="staff-currency"'),
            3,
        )
        self.assertContains(dashboard, reverse("home"))

    def test_staff_personal_details_use_employee_fields_and_can_be_updated(self):
        CustomerProfile.objects.create(
            user=self.staff,
            employee_id="GH-STAFF-001",
            mobile="+91 98765 43210",
        )
        self.client.force_login(self.staff)

        profile_page = self.client.get(reverse("account"))

        self.assertEqual(profile_page.status_code, 200)
        self.assertContains(profile_page, "Staff Profile")
        self.assertContains(profile_page, "Username")
        self.assertContains(profile_page, "Employee ID")
        self.assertContains(profile_page, "Mobile number")
        self.assertContains(profile_page, "Email")
        self.assertContains(profile_page, "GH-STAFF-001")
        self.assertContains(profile_page, reverse("staff_dashboard"))
        self.assertNotContains(profile_page, "Admin Profile")
        self.assertNotContains(profile_page, "Your GadgetHub History")

        response = self.client.post(
            reverse("account"),
            {
                "form_type": "profile",
                "username": "updatedstaff",
                "email": "updated-staff@example.com",
                "employee_id": "GH-STAFF-002",
                "mobile": "+91 91234 56789",
            },
        )

        self.assertRedirects(response, reverse("account"))
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.username, "updatedstaff")
        self.assertEqual(self.staff.email, "updated-staff@example.com")
        self.assertEqual(self.staff.profile.employee_id, "GH-STAFF-002")
        self.assertEqual(self.staff.profile.mobile, "+91 91234 56789")

    def test_staff_list_excludes_cancelled_and_unpaid_online_orders(self):
        cancelled_order = Order.objects.create(
            user=self.customer,
            full_name="Cancelled Customer",
            email="cancelled@example.com",
            phone="9876543210",
            address="2 Staff Street",
            status=Order.Status.CANCELLED,
            payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
            total="200.00",
        )
        unpaid_online_order = Order.objects.create(
            user=self.customer,
            full_name="Unpaid Online Customer",
            email="unpaid@example.com",
            phone="9876543210",
            address="3 Staff Street",
            payment_method=Order.PaymentMethod.RAZORPAY,
            payment_status=Order.PaymentStatus.PENDING,
            total="300.00",
        )
        paid_online_order = Order.objects.create(
            user=self.customer,
            full_name="Paid Online Customer",
            email="paid@example.com",
            phone="9876543210",
            address="4 Staff Street",
            status=Order.Status.SHIPPED,
            payment_method=Order.PaymentMethod.RAZORPAY,
            payment_status=Order.PaymentStatus.PAID,
            total="400.00",
            stock_deducted=True,
        )
        self.client.force_login(self.staff)

        response = self.client.get(reverse("staff_dashboard"))

        self.assertEqual(response.status_code, 200)
        listed_ids = {
            order.pk for order in response.context["page_obj"].object_list
        }
        self.assertEqual(listed_ids, {self.order.pk, paid_online_order.pk})
        self.assertNotContains(response, f"staff/orders/{cancelled_order.pk}/")
        self.assertNotContains(response, f"staff/orders/{unpaid_online_order.pk}/")
        self.assertContains(response, "staff/orders/" + str(paid_online_order.pk) + "/")

    def test_staff_order_details_are_restricted_to_visible_orders(self):
        cancelled_order = Order.objects.create(
            user=self.customer,
            full_name="Cancelled Customer",
            email="cancelled@example.com",
            phone="9876543210",
            address="2 Staff Street",
            status=Order.Status.CANCELLED,
            payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
            total="200.00",
        )
        self.client.force_login(self.staff)

        self.assertEqual(
            self.client.get(
                reverse("staff_order_detail", args=[cancelled_order.pk])
            ).status_code,
            404,
        )

        self.client.force_login(self.customer)
        self.assertEqual(
            self.client.get(
                reverse("staff_order_detail", args=[self.order.pk])
            ).status_code,
            403,
        )

    def test_customers_and_staff_without_inventory_cannot_advance_orders(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse("staff_dashboard"))
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.staff)
        self.product.stock_quantity = 0
        self.product.save(update_fields=["stock_quantity"])
        self.order.stock_deducted = False
        self.order.save(update_fields=["stock_deducted"])
        response = self.client.post(
            reverse("staff_advance_order", args=[self.order.pk]),
            {
                "expected_status": Order.Status.PENDING,
                "status": Order.Status.CONFIRMED,
            },
        )
        self.assertRedirects(
            response,
            reverse("staff_order_detail", args=[self.order.pk]),
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertFalse(self.order.stock_deducted)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 0)
        self.assertFalse(self.order.tracking_events.exists())

    def test_legacy_order_with_available_stock_is_reserved_when_advanced(self):
        self.client.force_login(self.staff)
        self.order.stock_deducted = False
        self.order.save(update_fields=["stock_deducted"])

        detail = self.client.get(
            reverse("staff_order_detail", args=[self.order.pk])
        )
        self.assertContains(detail, "Stock is available")
        self.assertNotContains(
            detail,
            "Inventory needs administrator review before this order can be fulfilled.",
        )

        response = self.client.post(
            reverse("staff_advance_order", args=[self.order.pk]),
            {
                "expected_status": Order.Status.PENDING,
                "status": Order.Status.CONFIRMED,
            },
        )

        self.assertRedirects(
            response,
            reverse("staff_order_detail", args=[self.order.pk]),
        )
        self.order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CONFIRMED)
        self.assertTrue(self.order.stock_deducted)
        self.assertEqual(self.product.stock_quantity, 8)

    def test_delivered_legacy_order_does_not_need_inventory_review(self):
        self.order.status = Order.Status.DELIVERED
        self.order.stock_deducted = False
        self.order.save(update_fields=["status", "stock_deducted"])
        self.product.stock_quantity = 0
        self.product.save(update_fields=["stock_quantity"])
        self.client.force_login(self.staff)

        response = self.client.get(
            reverse("staff_order_detail", args=[self.order.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response,
            "Inventory needs administrator review before this order can be fulfilled.",
        )
        self.assertNotContains(response, "Next delivery status")

    def test_staff_can_only_advance_one_status_and_update_cod_with_audit(self):
        self.client.force_login(self.staff)
        skipped = self.client.post(
            reverse("staff_advance_order", args=[self.order.pk]),
            {
                "expected_status": Order.Status.PENDING,
                "status": Order.Status.SHIPPED,
            },
        )
        self.assertRedirects(
            skipped,
            reverse("staff_order_detail", args=[self.order.pk]),
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)

        advanced = self.client.post(
            reverse("staff_advance_order", args=[self.order.pk]),
            {
                "expected_status": Order.Status.PENDING,
                "status": Order.Status.CONFIRMED,
            },
        )
        self.assertRedirects(
            advanced,
            reverse("staff_order_detail", args=[self.order.pk]),
        )
        status_event = self.order.tracking_events.get()
        self.assertEqual(status_event.previous_status, Order.Status.PENDING)
        self.assertEqual(status_event.status, Order.Status.CONFIRMED)
        self.assertEqual(status_event.updated_by, self.staff)
        self.assertEqual(status_event.updated_by_name, "Order Staff")

        paid = self.client.post(
            reverse("staff_update_cod_payment", args=[self.order.pk]),
            {"payment_status": Order.PaymentStatus.PAID},
        )
        self.assertRedirects(
            paid,
            reverse("staff_order_detail", args=[self.order.pk]),
        )
        self.order.refresh_from_db()
        payment_event = self.order.payment_events.get()
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(payment_event.previous_status, Order.PaymentStatus.UNPAID)
        self.assertEqual(payment_event.new_status, Order.PaymentStatus.PAID)
        self.assertEqual(payment_event.updated_by, self.staff)
        self.assertEqual(payment_event.updated_by_name, "Order Staff")

        cannot_revert_paid = self.client.post(
            reverse("staff_update_cod_payment", args=[self.order.pk]),
            {"payment_status": Order.PaymentStatus.PENDING},
        )
        self.assertRedirects(
            cannot_revert_paid,
            reverse("staff_order_detail", args=[self.order.pk]),
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(OrderPaymentEvent.objects.count(), 1)

    def test_staff_cannot_change_online_payment_or_access_admin_product_tools(self):
        online_order = Order.objects.create(
            user=self.customer,
            full_name="Staff Customer",
            email="customer@example.com",
            phone="9876543210",
            address="1 Staff Street",
            payment_method=Order.PaymentMethod.RAZORPAY,
            payment_status=Order.PaymentStatus.PENDING,
            total="100.00",
        )
        transaction = PaymentTransaction.objects.create(
            order=online_order,
            amount="100.00",
            status=PaymentTransaction.Status.CREATED,
        )
        self.client.force_login(self.staff)

        payment_response = self.client.post(
            reverse("staff_update_cod_payment", args=[online_order.pk]),
            {"payment_status": Order.PaymentStatus.PAID},
        )
        product_admin_response = self.client.get(
            reverse("admin:home_product_changelist")
        )

        self.assertEqual(payment_response.status_code, 404)
        self.assertNotEqual(product_admin_response.status_code, 200)
        transaction.refresh_from_db()
        online_order.refresh_from_db()
        self.assertEqual(transaction.status, PaymentTransaction.Status.CREATED)
        self.assertEqual(online_order.payment_status, Order.PaymentStatus.PENDING)
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.is_staff)
        self.assertFalse(self.staff.is_superuser)

    def test_only_superusers_can_create_or_manage_staff_accounts(self):
        self.client.force_login(self.staff)
        self.assertNotEqual(
            self.client.get(reverse("admin:staff_management")).status_code,
            200,
        )

        self.client.force_login(self.admin_user)
        management_url = reverse("admin:staff_management")
        form_response = self.client.get(management_url)
        self.assertEqual(
            list(form_response.context["form"].fields),
            ["username", "employee_id", "email", "password1", "password2"],
        )
        self.assertEqual(
            form_response.context["form"].fields["password2"].label,
            "Confirm password",
        )
        self.assertNotContains(form_response, "Mobile number")
        response = self.client.post(
            management_url,
            {
                "username": "newstaff",
                "email": "newstaff@example.com",
                "employee_id": "GH-NEW-STAFF",
                "password1": "StrongPassword123!More",
                "password2": "StrongPassword123!More",
            },
        )
        self.assertRedirects(response, management_url)
        new_staff = get_user_model().objects.get(username="newstaff")
        self.assertTrue(new_staff.groups.filter(pk=self.staff_group.pk).exists())
        self.assertFalse(new_staff.is_staff)
        self.assertFalse(new_staff.is_superuser)
        self.assertEqual(new_staff.profile.employee_id, "GH-NEW-STAFF")
        self.assertEqual(new_staff.profile.mobile, "")

        toggle_url = reverse("admin:staff_toggle_active", args=[new_staff.pk])
        self.assertEqual(self.client.get(toggle_url).status_code, 405)
        self.assertEqual(self.client.post(toggle_url).status_code, 302)
        new_staff.refresh_from_db()
        self.assertFalse(new_staff.is_active)

    def test_employee_ids_are_unique_case_insensitively_on_staff_creation(self):
        CustomerProfile.objects.create(
            user=self.staff,
            employee_id="GH-UNIQUE-001",
        )
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin:staff_management"),
            {
                "username": "duplicateidstaff",
                "email": "duplicateid@example.com",
                "employee_id": "gh-unique-001",
                "password1": "StrongPassword123!More",
                "password2": "StrongPassword123!More",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This employee ID is already in use.")
        self.assertFalse(
            get_user_model().objects.filter(username="duplicateidstaff").exists()
        )

    def test_database_rejects_case_insensitive_duplicate_employee_ids(self):
        CustomerProfile.objects.create(
            user=self.staff,
            employee_id="GH-DATABASE-001",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CustomerProfile.objects.create(
                    user=self.customer,
                    employee_id="gh-database-001",
                )

    def test_admin_can_edit_staff_employee_id_and_mobile(self):
        CustomerProfile.objects.create(
            user=self.staff,
            employee_id="GH-EDIT-001",
            mobile="+91 90000 22222",
        )
        self.client.force_login(self.admin_user)
        edit_url = reverse("admin:staff_edit", args=[self.staff.pk])

        edit_page = self.client.get(edit_url)
        self.assertEqual(edit_page.status_code, 200)
        self.assertContains(edit_page, 'name="employee_id"')
        self.assertContains(edit_page, 'name="mobile"')

        response = self.client.post(
            edit_url,
            {
                "username": "orderstaff",
                "first_name": "Order",
                "last_name": "Staff",
                "email": "orderstaff@example.com",
                "employee_id": "GH-EDIT-002",
                "mobile": "+91 90000 33333",
            },
        )

        self.assertRedirects(response, reverse("admin:staff_management"))
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.profile.employee_id, "GH-EDIT-002")
        self.assertEqual(self.staff.profile.mobile, "+91 90000 33333")


class GadgetHubAdminTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="storeadmin",
            email="admin@example.com",
            password="StrongPassword123!",
        )
        self.client.force_login(self.admin_user)
        self.category = Category.objects.get(name="Audio")
        self.product = Product.objects.get(name="Wireless Headphones")
        self.order = Order.objects.create(
            user=self.admin_user,
            full_name="Admin Test Order",
            email="admin@example.com",
            phone="9876543210",
            address="1 Admin Street",
            total="99.00",
        )

    def test_dashboard_shows_store_totals_and_management_links(self):
        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Store overview")
        self.assertContains(response, reverse("account"))
        self.assertContains(response, reverse("admin:auth_user_changelist"))
        self.assertContains(response, reverse("admin:home_order_changelist"))
        self.assertContains(response, reverse("admin:order_updates"))
        self.assertContains(response, reverse("admin:home_product_changelist"))
        self.assertContains(response, reverse("admin:home_category_changelist"))
        self.assertContains(response, reverse("admin:monthly_sales_report"))
        self.assertContains(response, reverse("admin:staff_actions"))
        self.assertContains(response, 'type="month"')

    def test_gadgethub_admin_filter_loads_and_truncates_long_values(self):
        template = Template(
            "{% load gadgethub_admin_filters %}"
            "{{ value|to_object_display_value }}"
        )

        self.assertEqual(
            template.render(Context({"value": "x" * 61})),
            "x" * 57 + "…",
        )

    def test_dashboard_shows_staff_activity_with_employee_identity(self):
        staff_user = get_user_model().objects.create_user(
            username="dashboardstaff",
            first_name="Dashboard",
            last_name="Staff",
            password="StrongPassword123!",
        )
        staff_group, _ = Group.objects.get_or_create(name="GadgetHub Staff")
        staff_user.groups.add(staff_group)
        OrderTrackingEvent.objects.create(
            order=self.order,
            previous_status=Order.Status.PENDING,
            status=Order.Status.CONFIRMED,
            description="Order confirmed by staff.",
            updated_by=staff_user,
            updated_by_name="Dashboard Staff",
        )
        OrderPaymentEvent.objects.create(
            order=self.order,
            previous_status=Order.PaymentStatus.UNPAID,
            new_status=Order.PaymentStatus.PAID,
            updated_by=staff_user,
            updated_by_name="Dashboard Staff",
        )
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            unit_price=self.product.price,
            quantity=2,
        )

        cancelled_order = Order.objects.create(
            user=self.admin_user,
            full_name="Cancelled Audit Customer",
            email="cancelled-audit@example.com",
            phone="9876543210",
            address="Audit Street",
            status=Order.Status.CANCELLED,
            total="99.00",
        )
        OrderTrackingEvent.objects.create(
            order=cancelled_order,
            previous_status=Order.Status.PENDING,
            status=Order.Status.CANCELLED,
            description="Order cancelled.",
            updated_by=staff_user,
            updated_by_name="Dashboard Staff",
        )

        dashboard = self.client.get(reverse("admin:index"))
        self.assertContains(dashboard, "Staff actions")
        self.assertContains(dashboard, reverse("admin:staff_actions"))
        self.assertContains(dashboard, 'class="gh-quick-icon gh-icon-purple"')
        self.assertContains(dashboard, 'xmlns="http://www.w3.org/2000/svg"')
        self.assertNotContains(dashboard, "Dashboard Staff")

        response = self.client.get(reverse("admin:staff_actions"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"Order #{self.order.pk}")
        self.assertContains(response, "Wireless Headphones × 2")
        self.assertContains(response, "Dashboard Staff")
        self.assertContains(response, "Pending → Confirmed")
        self.assertContains(response, "Unpaid → Paid")
        self.assertContains(response, 'class="gh-audit-amount"')
        self.assertContains(response, 'class="gh-activity-mark is-add"')
        self.assertContains(response, 'class="gh-audit-currency"')
        self.assertContains(response, "₹")
        self.assertContains(response, "99.00")
        self.assertNotContains(response, f"Order #{cancelled_order.pk}")

    def test_payment_health_categories_show_only_matching_records(self):
        transactions = {
            status: PaymentTransaction.objects.create(
                order=self.order,
                razorpay_order_id=f"order_{status}",
                razorpay_payment_id=f"payment_{status}",
                amount="99.00",
                status=status,
            )
            for status in (
                PaymentTransaction.Status.PAID,
                PaymentTransaction.Status.CREATED,
                PaymentTransaction.Status.FAILED,
            )
        }
        cancelled_attempt = PaymentTransaction.objects.create(
            order=self.order,
            razorpay_order_id="order_cancelled_attempt",
            razorpay_payment_id="payment_cancelled_attempt",
            amount="99.00",
            status=PaymentTransaction.Status.CANCELLED,
        )
        cancelled_order = Order.objects.create(
            user=self.admin_user,
            full_name="Cancelled Customer",
            email="cancelled@example.com",
            phone="9876543210",
            address="2 Refund Street",
            status=Order.Status.CANCELLED,
            payment_method=Order.PaymentMethod.RAZORPAY,
            payment_status=Order.PaymentStatus.REFUNDED,
            total="99.00",
        )
        cancelled_transaction = PaymentTransaction.objects.create(
            order=cancelled_order,
            razorpay_order_id="order_cancelled",
            razorpay_payment_id="payment_cancelled",
            amount="99.00",
            status=PaymentTransaction.Status.FAILED,
        )
        cancelled_payment_order = Order.objects.create(
            user=self.admin_user,
            full_name="Cancelled Payment Customer",
            email="cancelled-payment@example.com",
            phone="9876543210",
            address="Cancelled Payment Street",
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.RAZORPAY,
            payment_status=Order.PaymentStatus.CANCELLED,
            total="99.00",
        )
        cancelled_payment_transaction = PaymentTransaction.objects.create(
            order=cancelled_payment_order,
            razorpay_order_id="order_payment_cancelled",
            razorpay_payment_id="payment_payment_cancelled",
            amount="99.00",
            status=PaymentTransaction.Status.FAILED,
        )
        refunded_order = Order.objects.create(
            user=self.admin_user,
            full_name="Refunded Customer",
            email="refunded@example.com",
            phone="9876543210",
            address="3 Refund Street",
            status=Order.Status.DELIVERED,
            payment_method=Order.PaymentMethod.RAZORPAY,
            payment_status=Order.PaymentStatus.REFUNDED,
            total="99.00",
        )

        dashboard = self.client.get(reverse("admin:index"))

        self.assertEqual(dashboard.context["store_summary"]["successful_payment_count"], 1)
        self.assertEqual(dashboard.context["store_summary"]["pending_payment_count"], 1)
        self.assertEqual(dashboard.context["store_summary"]["failed_payment_count"], 1)
        self.assertEqual(dashboard.context["store_summary"]["refunded_order_count"], 1)
        self.assertEqual(dashboard.context["store_summary"]["razorpay_payment_count"], 3)
        payment_list = self.client.get(
            reverse("admin:home_paymenttransaction_changelist")
        )
        visible_transaction_ids = {
            item.pk for item in payment_list.context["cl"].result_list
        }
        self.assertEqual(visible_transaction_ids, {item.pk for item in transactions.values()})
        self.assertNotIn(cancelled_attempt.pk, visible_transaction_ids)
        self.assertNotIn(cancelled_transaction.pk, visible_transaction_ids)
        self.assertNotIn(cancelled_payment_transaction.pk, visible_transaction_ids)
        order_change_page = self.client.get(
            reverse("admin:home_order_change", args=[self.order.pk])
        )
        self.assertNotContains(order_change_page, "payment_cancelled_attempt")
        for payment in (cancelled_attempt, cancelled_payment_transaction):
            self.assertRedirects(
                self.client.get(
                    reverse("admin:home_paymenttransaction_change", args=[payment.pk])
                ),
                reverse("admin:index"),
                fetch_redirect_response=False,
            )
        for status in (
            PaymentTransaction.Status.PAID,
            PaymentTransaction.Status.CREATED,
            PaymentTransaction.Status.FAILED,
        ):
            self.assertContains(dashboard, f"?status__exact={status}")
            response = self.client.get(
                reverse("admin:home_paymenttransaction_changelist"),
                {"status__exact": status},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                [item.pk for item in response.context["cl"].result_list],
                [transactions[status].pk],
            )
            self.assertContains(response, f"payment_{status}")

        refunded_response = self.client.get(
            reverse("admin:home_order_changelist"),
            {"payment_status__exact": Order.PaymentStatus.REFUNDED},
        )
        self.assertEqual(refunded_response.status_code, 200)
        self.assertEqual(
            [order.pk for order in refunded_response.context["cl"].result_list],
            [refunded_order.pk],
        )
        self.assertContains(dashboard, "?payment_status__exact=refunded")
        self.assertNotIn(
            cancelled_order.pk,
            [order.pk for order in refunded_response.context["cl"].result_list],
        )
        failed_payment_response = self.client.get(
            reverse("admin:home_paymenttransaction_changelist"),
            {"status__exact": PaymentTransaction.Status.FAILED},
        )
        self.assertNotIn(
            cancelled_transaction.pk,
            [
                payment.pk
                for payment in failed_payment_response.context["cl"].result_list
            ],
        )
        self.assertEqual(
            self.client.get(
                reverse("admin:order_details_pdf", args=[cancelled_order.pk])
            ).status_code,
            404,
        )

    def test_monthly_sales_report_excludes_cancelled_orders(self):
        cancelled_order = Order.objects.create(
            user=self.admin_user,
            full_name="Cancelled Report Customer",
            email="cancelled-report@example.com",
            phone="9876543210",
            address="4 Refund Street",
            status=Order.Status.CANCELLED,
            payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
            payment_status=Order.PaymentStatus.UNPAID,
            total="99.00",
        )
        cancelled_payment_order = Order.objects.create(
            user=self.admin_user,
            full_name="Cancelled Payment Report Customer",
            email="cancelled-payment-report@example.com",
            phone="9876543210",
            address="5 Refund Street",
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.RAZORPAY,
            payment_status=Order.PaymentStatus.CANCELLED,
            total="99.00",
        )
        with patch(
            "home.admin.build_monthly_sales_report",
            return_value=BytesIO(b"%PDF-"),
        ) as build_report:
            response = self.client.get(
                reverse("admin:monthly_sales_report"),
                {"month": timezone.localdate().strftime("%Y-%m")},
            )

        self.assertEqual(response.status_code, 200)
        reported_orders = list(build_report.call_args.args[1])
        self.assertNotIn(cancelled_order.pk, [order.pk for order in reported_orders])
        self.assertNotIn(
            cancelled_payment_order.pk,
            [order.pk for order in reported_orders],
        )

    def test_payment_health_categories_show_empty_messages(self):
        for status, message in (
            (PaymentTransaction.Status.PAID, "No successful payments found."),
            (PaymentTransaction.Status.CREATED, "No pending payments found."),
            (PaymentTransaction.Status.FAILED, "No failed payments found."),
        ):
            response = self.client.get(
                reverse("admin:home_paymenttransaction_changelist"),
                {"status__exact": status},
            )
            self.assertContains(response, message)

        response = self.client.get(
            reverse("admin:home_order_changelist"),
            {"payment_status__exact": Order.PaymentStatus.REFUNDED},
        )
        self.assertContains(response, "No refunded orders found.")

    def test_order_created_at_displays_in_india_standard_time(self):
        self.order.created_at = datetime(
            2025, 1, 2, 3, 45, tzinfo=datetime_timezone.utc
        )
        self.order.save(update_fields=["created_at"])

        response = self.client.get(reverse("admin:home_order_changelist"))

        local_created_at = timezone.localtime(self.order.created_at)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            local_created_at.utcoffset(),
            timedelta(hours=5, minutes=30),
        )
        self.assertContains(
            response,
            date_format(local_created_at, "DATETIME_FORMAT"),
        )

    def test_admin_can_download_monthly_sales_pdf_for_selected_month(self):
        month = timezone.localdate().strftime("%Y-%m")

        response = self.client.get(
            reverse("admin:monthly_sales_report"),
            {"month": month},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="gadgethub-monthly-sales-{month}.pdf"',
        )
        self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF-"))

    def test_monthly_sales_report_rejects_invalid_month(self):
        response = self.client.get(
            reverse("admin:monthly_sales_report"),
            {"month": "not-a-month"},
        )

        self.assertEqual(response.status_code, 400)

    def test_admin_can_download_individual_order_pdf_only_from_orders_section(self):
        item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            unit_price=self.product.price,
            quantity=2,
        )
        user_list = self.client.get(reverse("admin:auth_user_changelist"))
        order_list = self.client.get(reverse("admin:home_order_changelist"))
        change_page = self.client.get(
            reverse("admin:home_order_change", args=[self.order.pk])
        )
        response = self.client.get(
            reverse("admin:order_details_pdf", args=[self.order.pk])
        )

        self.assertNotContains(user_list, "Download PDF")
        self.assertContains(
            order_list,
            reverse("admin:order_details_pdf", args=[self.order.pk]),
        )
        self.assertContains(
            order_list,
            "Download Order Details (PDF)",
        )
        self.assertContains(
            change_page,
            reverse("admin:order_details_pdf", args=[self.order.pk]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="gadgethub-order-{self.order.pk}.pdf"',
        )
        self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF-"))
        self.assertEqual(item.quantity, 2)

    def test_admin_can_open_order_status_updates_page(self):
        response = self.client.get(reverse("admin:order_updates"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Order updates")
        self.assertContains(response, "Mark as Confirmed")
        self.assertContains(
            response,
            reverse("admin:order_advance", args=[self.order.pk]),
        )

    def test_order_changelist_summarizes_customer_items_and_delivery_location(self):
        self.order.location_latitude = 40.7128
        self.order.location_longitude = -74.006
        self.order.payment_method = Order.PaymentMethod.RAZORPAY
        self.order.payment_status = Order.PaymentStatus.PAID
        self.order.save(
            update_fields=[
                "location_latitude",
                "location_longitude",
                "payment_method",
                "payment_status",
            ]
        )
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            unit_price=self.product.price,
            quantity=2,
        )

        response = self.client.get(reverse("admin:home_order_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Customer &amp; contact")
        self.assertContains(response, "Admin Test Order")
        self.assertContains(response, "9876543210")
        self.assertContains(response, "admin@example.com")
        self.assertContains(response, "Wireless Headphones")
        self.assertContains(response, "1 Admin Street")
        self.assertContains(response, "Open delivery map")
        self.assertContains(response, "40.7128,-74.006")
        self.assertContains(response, "Payment status")
        self.assertContains(response, "Paid")
        self.assertContains(response, "home/admin_orders.css")

    def test_order_items_are_read_only_and_cannot_be_added_from_order_admin(self):
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            unit_price=self.product.price,
            quantity=1,
        )

        response = self.client.get(
            reverse("admin:home_order_change", args=[self.order.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.product.name)
        self.assertNotContains(response, 'class="add-row"')
        self.assertNotContains(response, 'name="items-0-quantity"')
        self.assertNotContains(response, 'name="items-0-product"')

    def test_staff_without_order_permissions_cannot_access_order_actions_or_reports(self):
        staff_user = get_user_model().objects.create_user(
            username="restrictedstaff",
            password="StrongPassword123!",
            is_staff=True,
        )
        self.client.force_login(staff_user)
        paths = [
            reverse("admin:home_order_changelist"),
            reverse("admin:home_order_change", args=[self.order.pk]),
            reverse("admin:order_updates"),
            reverse("admin:staff_actions"),
            reverse("admin:monthly_sales_report"),
            reverse("admin:order_details_pdf", args=[self.order.pk]),
        ]

        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 403)

        dashboard = self.client.get(reverse("admin:index"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotContains(
            dashboard,
            reverse("admin:monthly_sales_report"),
        )

        response = self.client.post(
            reverse("admin:order_advance", args=[self.order.pk]),
            {"expected_status": Order.Status.PENDING},
        )

        self.assertEqual(response.status_code, 403)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)

    def test_view_only_staff_cannot_update_order_status(self):
        staff_user = get_user_model().objects.create_user(
            username="viewonlystaff",
            password="StrongPassword123!",
            is_staff=True,
        )
        view_order_permission = Permission.objects.get(
            content_type__app_label="home",
            codename="view_order",
        )
        staff_user.user_permissions.add(view_order_permission)
        self.client.force_login(staff_user)

        change_page = self.client.get(
            reverse("admin:home_order_change", args=[self.order.pk])
        )
        response = self.client.post(
            reverse("admin:order_advance", args=[self.order.pk]),
            {"expected_status": Order.Status.PENDING},
        )

        self.assertEqual(change_page.status_code, 200)
        self.assertNotContains(change_page, 'name="status"')
        self.assertEqual(response.status_code, 403)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)

    def test_admin_can_open_user_order_and_catalog_management(self):
        paths = [
            reverse("admin:auth_user_changelist"),
            reverse("admin:home_order_changelist"),
            reverse("admin:home_paymenttransaction_changelist"),
            reverse("admin:home_product_add"),
            reverse("admin:home_product_change", args=[self.product.pk]),
            reverse("admin:home_category_add"),
            reverse("admin:home_category_change", args=[self.category.pk]),
        ]

        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_admin_account_has_management_links_without_purchase_history(self):
        response = self.client.get(reverse("account"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("admin:home_product_changelist"))
        self.assertContains(response, reverse("admin:home_category_changelist"))
        self.assertContains(response, reverse("admin:home_order_changelist"))
        self.assertContains(response, reverse("admin:order_updates"))
        self.assertContains(
            response, reverse("admin:home_paymenttransaction_changelist")
        )
        self.assertNotContains(response, "Your GadgetHub History")

    def test_admin_product_create_form_accepts_original_price(self):
        add_url = reverse("admin:home_product_add")
        response = self.client.get(add_url)

        self.assertContains(response, 'name="original_price"')
        self.assertContains(response, 'name="stock_quantity"')

        response = self.client.post(
            add_url,
            {
                "name": "New Audio Product",
                "category": self.category.pk,
                "price": "1200.00",
                "original_price": "1500.00",
                "description": "A useful product description.",
                "image": "https://example.com/new-product.jpg",
                "badge": "",
                "stock_quantity": 12,
                "is_active": "on",
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(name="New Audio Product")
        self.assertEqual(product.original_price, 1500)
        self.assertEqual(product.stock_quantity, 12)

    def test_admin_can_only_advance_delivery_status_and_cannot_edit_payment_status(self):
        self.order.stock_deducted = True
        self.order.location_latitude = 40.7128
        self.order.location_longitude = -74.006
        self.order.save(
            update_fields=[
                "stock_deducted",
                "location_latitude",
                "location_longitude",
            ]
        )
        change_url = reverse("admin:home_order_change", args=[self.order.pk])
        change_page = self.client.get(change_url)

        self.assertEqual(change_page.status_code, 200)
        self.assertContains(change_page, "Order status / tracking")
        self.assertContains(change_page, "Customer &amp; contact")
        self.assertContains(change_page, "Delivery destination")
        self.assertContains(change_page, "Order &amp; payment")
        self.assertContains(change_page, "Open delivery map")
        self.assertContains(change_page, "Payment status")
        self.assertNotContains(change_page, 'name="payment_method"')
        self.assertNotContains(change_page, 'name="payment_status"')

        post_data = {
            "payment_status": Order.PaymentStatus.PAID,
            "_save": "Save",
            "items-TOTAL_FORMS": "0",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            "payment_transactions-TOTAL_FORMS": "0",
            "payment_transactions-INITIAL_FORMS": "0",
            "payment_transactions-MIN_NUM_FORMS": "0",
            "payment_transactions-MAX_NUM_FORMS": "1000",
            "tracking_events-TOTAL_FORMS": "0",
            "tracking_events-INITIAL_FORMS": "0",
            "tracking_events-MIN_NUM_FORMS": "0",
            "tracking_events-MAX_NUM_FORMS": "1000",
            "payment_events-TOTAL_FORMS": "0",
            "payment_events-INITIAL_FORMS": "0",
            "payment_events-MIN_NUM_FORMS": "0",
            "payment_events-MAX_NUM_FORMS": "1000",
        }
        skipped_status_response = self.client.post(
            change_url, {**post_data, "status": Order.Status.SHIPPED}
        )
        self.assertEqual(skipped_status_response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.UNPAID)

        response = self.client.post(
            change_url, {**post_data, "status": Order.Status.CONFIRMED}
        )

        self.assertEqual(response.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CONFIRMED)
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.UNPAID)
        event = self.order.tracking_events.get()
        self.assertEqual(event.status, Order.Status.CONFIRMED)
        self.assertEqual(event.updated_by, self.admin_user)

    def test_order_updates_advance_one_step_and_refresh_customer_tracking(self):
        customer = get_user_model().objects.create_user(
            username="trackingcustomer", password="StrongPassword123!"
        )
        self.order.user = customer
        self.order.save(update_fields=["user"])
        self.product.stock_quantity = 5
        self.product.save(update_fields=["stock_quantity"])
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            unit_price=self.product.price,
            quantity=1,
        )
        update_url = reverse("admin:order_advance", args=[self.order.pk])

        response = self.client.post(
            update_url, {"expected_status": Order.Status.PENDING}
        )
        self.assertRedirects(response, reverse("admin:order_updates"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CONFIRMED)
        self.assertTrue(self.order.stock_deducted)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 4)

        stale_response = self.client.post(
            update_url, {"expected_status": Order.Status.PENDING}
        )
        self.assertRedirects(stale_response, reverse("admin:order_updates"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CONFIRMED)
        self.assertEqual(self.order.tracking_events.count(), 1)

        for expected_status in (
            Order.Status.SHIPPED,
            Order.Status.OUT_FOR_DELIVERY,
            Order.Status.DELIVERED,
        ):
            response = self.client.post(
                update_url, {"expected_status": self.order.status}
            )
            self.assertRedirects(response, reverse("admin:order_updates"))
            self.order.refresh_from_db()
            self.assertEqual(self.order.status, expected_status)

        events = list(self.order.tracking_events.order_by("pk"))
        self.assertEqual(
            [event.status for event in events],
            [
                Order.Status.CONFIRMED,
                Order.Status.SHIPPED,
                Order.Status.OUT_FOR_DELIVERY,
                Order.Status.DELIVERED,
            ],
        )
        self.assertTrue(all(event.updated_by == self.admin_user for event in events))

        self.client.force_login(customer)
        tracking_response = self.client.get(reverse("order_tracking"))
        self.assertEqual(
            tracking_response.json()["orders"][0]["status"],
            Order.Status.DELIVERED,
        )
        account_response = self.client.get(reverse("account"))
        self.assertContains(
            account_response,
            'class="account-order account-order-delivered"',
        )
        self.assertContains(account_response, "Delivered Date &amp; Time:")
        self.assertContains(account_response, "Status: Delivered")

    def test_admin_cannot_advance_order_without_sufficient_stock(self):
        self.product.stock_quantity = 0
        self.product.save(update_fields=["stock_quantity"])
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            product_name=self.product.name,
            unit_price=self.product.price,
            quantity=1,
        )

        response = self.client.post(
            reverse("admin:order_advance", args=[self.order.pk]),
            {"expected_status": Order.Status.PENDING},
        )

        self.assertRedirects(response, reverse("admin:order_updates"))
        self.order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertFalse(self.order.stock_deducted)
        self.assertEqual(self.product.stock_quantity, 0)
        self.assertFalse(self.order.tracking_events.exists())

    def test_completed_order_cannot_be_advanced_again(self):
        self.order.status = Order.Status.DELIVERED
        self.order.save(update_fields=["status"])

        response = self.client.get(reverse("admin:order_updates"))
        self.assertContains(response, "No further updates")

        response = self.client.post(
            reverse("admin:order_advance", args=[self.order.pk]),
            {"expected_status": Order.Status.DELIVERED},
        )

        self.assertRedirects(response, reverse("admin:order_updates"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.DELIVERED)
        self.assertFalse(self.order.tracking_events.exists())
