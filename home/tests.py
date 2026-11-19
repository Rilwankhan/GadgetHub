import hashlib
import hmac
import json
import re
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
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
        tracking_event = order.tracking_events.get()
        self.assertEqual(tracking_event.status, Order.Status.PENDING)
        self.assertEqual(tracking_event.updated_by, None)

        another_user = get_user_model().objects.create_user(
            username="another", password="StrongPassword123!"
        )
        self.client.force_login(another_user)
        account_response = self.client.get(reverse("account"))
        self.assertNotContains(account_response, "Test Customer")

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

    def test_owner_can_cancel_an_order_within_48_hours_before_shipping(self):
        self.client.force_login(self.user)
        order = self.create_order()

        response = self.client.post(reverse("cancel_order", args=[order.pk]))

        self.assertRedirects(response, reverse("account"))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertEqual(order.payment_status, Order.PaymentStatus.UNPAID)
        event = order.tracking_events.get()
        self.assertEqual(event.status, Order.Status.CANCELLED)
        self.assertEqual(event.updated_by, self.user)

    def test_cancellation_closes_after_48_hours(self):
        self.client.force_login(self.user)
        order = self.create_order()
        order.created_at = timezone.now() - timedelta(hours=48, seconds=1)
        order.save(update_fields=["created_at"])

        account_response = self.client.get(reverse("account"))
        self.assertContains(account_response, "The 48-hour cancellation window has ended.")

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
        self.assertContains(response, f'datetime="{delivered_at.isoformat()}"')
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
        self.assertContains(response, f'datetime="{booked_at.isoformat()}"')
        self.assertContains(response, f'datetime="{cancelled_at.isoformat()}"')
        self.assertContains(response, "✕")
        self.assertContains(response, "Delete Order")
        self.assertNotContains(response, "Cancel order")
        tracking_order = self.client.get(reverse("order_tracking")).json()["orders"][0]
        self.assertEqual(tracking_order["cancelled_at"], cancelled_at.isoformat())

    def test_registration_creates_and_logs_in_customer(self):
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

        self.assertRedirects(response, reverse("account"))
        self.assertTrue(
            get_user_model().objects.filter(username="newcustomer").exists()
        )
        self.assertTrue(response.wsgi_request.user.is_authenticated)
        self.assertEqual(response.wsgi_request.user.profile.mobile, "9876543210")
        self.assertEqual(response.wsgi_request.user.profile.address, "1 New Street")

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

        self.assertRedirects(response, "/?checkout=1")
        self.assertTrue(response.wsgi_request.user.is_authenticated)

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

    def test_forgot_password_sends_a_working_reset_link(self):
        self.user.email = "customer@example.com"
        self.user.save(update_fields=["email"])

        response = self.client.get(reverse("login"))
        self.assertContains(response, reverse("password_reset"))

        response = self.client.post(
            reverse("password_reset"), {"email": self.user.email}
        )

        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        match = re.search(
            r"/accounts/reset/([^/\s]+)/([^/\s]+)", mail.outbox[0].body
        )
        self.assertIsNotNone(match)
        reset_url = reverse(
            "password_reset_confirm",
            kwargs={"uidb64": match.group(1), "token": match.group(2)},
        )
        confirm_page = self.client.get(reset_url, follow=True)
        self.assertEqual(confirm_page.status_code, 200)
        response = self.client.post(
            confirm_page.request["PATH_INFO"],
            {
                "new_password1": "ResetPassword123!More",
                "new_password2": "ResetPassword123!More",
            },
        )

        self.assertRedirects(response, reverse("password_reset_complete"))
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

        self.assertEqual(result["razorpay"]["key_id"], "rzp_test_public")
        self.assertEqual(result["razorpay"]["amount"], 499800)
        self.assertNotIn("test_secret", json.dumps(result))
        self.assertEqual(order.payment_status, Order.PaymentStatus.PENDING)
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
        self.assertEqual(payment.status, PaymentTransaction.Status.FAILED)
        self.assertEqual(payment.error_code, "PAYMENT_FAILED")

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
        self.assertContains(response, 'type="month"')

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
                "is_active": "on",
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(name="New Audio Product")
        self.assertEqual(product.original_price, 1500)

    def test_admin_can_only_advance_delivery_status_and_cannot_edit_payment_status(self):
        self.order.location_latitude = 40.7128
        self.order.location_longitude = -74.006
        self.order.save(update_fields=["location_latitude", "location_longitude"])
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
        update_url = reverse("admin:order_advance", args=[self.order.pk])

        response = self.client.post(
            update_url, {"expected_status": Order.Status.PENDING}
        )
        self.assertRedirects(response, reverse("admin:order_updates"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CONFIRMED)

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
