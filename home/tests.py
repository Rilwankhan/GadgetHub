import json
import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Category, Order, OrderItem, Product


class StorefrontOrderTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="customer", password="StrongPassword123!"
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
        self.assertFalse(Order.objects.exists())

    def test_order_uses_database_price_and_is_listed_only_for_its_account(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("place_order"),
            data=json.dumps(self.order_payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], "4998.00")
        order = Order.objects.get(user=self.user)
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

    def test_registration_creates_and_logs_in_customer(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "newcustomer",
                "password1": "V3ryLongPassword!Test",
                "password2": "V3ryLongPassword!Test",
            },
        )

        self.assertRedirects(response, reverse("account"))
        self.assertTrue(
            get_user_model().objects.filter(username="newcustomer").exists()
        )
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

    def test_regular_user_cannot_open_admin_dashboard(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response.url)

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

        self.assertContains(response, "First name")
        self.assertContains(response, "Address")
        self.assertNotContains(response, "Employee ID")
        self.assertContains(response, "Change password")
        self.assertRegex(
            response.content.decode(),
            r'<div\s+id="account-security-panel"\s+hidden\s*>',
        )

    def test_admin_can_update_only_admin_profile_fields(self):
        admin_user = get_user_model().objects.create_superuser(
            username="profileadmin",
            email="admin@example.com",
            password="StrongPassword123!",
        )
        self.client.force_login(admin_user)

        response = self.client.get(reverse("account"))

        self.assertContains(response, "Employee ID")
        self.assertContains(response, 'aria-expanded="false"')
        self.assertNotContains(response, "First name")
        self.assertNotContains(response, "Address")
        self.assertRegex(
            response.content.decode(),
            r'<div\s+id="account-security-panel"\s+hidden\s*>',
        )

        response = self.client.post(
            reverse("account"),
            {
                "form_type": "profile",
                "username": "updatedadmin",
                "email": "updated-admin@example.com",
                "employee_id": "GH-1042",
                "mobile": "+91 98765 43210",
            },
        )

        self.assertRedirects(response, reverse("account"))
        admin_user.refresh_from_db()
        self.assertEqual(admin_user.username, "updatedadmin")
        self.assertEqual(admin_user.email, "updated-admin@example.com")
        self.assertEqual(admin_user.profile.employee_id, "GH-1042")
        self.assertEqual(admin_user.profile.mobile, "+91 98765 43210")

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
        self.assertContains(response, reverse("admin:home_product_changelist"))
        self.assertContains(response, reverse("admin:home_category_changelist"))

    def test_admin_can_open_user_order_and_catalog_management(self):
        paths = [
            reverse("admin:auth_user_changelist"),
            reverse("admin:home_order_changelist"),
            reverse("admin:home_product_add"),
            reverse("admin:home_product_change", args=[self.product.pk]),
            reverse("admin:home_category_add"),
            reverse("admin:home_category_change", args=[self.category.pk]),
        ]

        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_admin_can_only_advance_delivery_status_and_cannot_edit_payment_status(self):
        change_url = reverse("admin:home_order_change", args=[self.order.pk])
        change_page = self.client.get(change_url)

        self.assertEqual(change_page.status_code, 200)
        self.assertContains(change_page, "Order status / tracking")
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
