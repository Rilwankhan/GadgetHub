from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm, UserCreationForm
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import CustomerProfile
from .roles import STAFF_GROUP_NAME


def friendly_password_error(password, user=None, cleaned_data=None):
    if not password:
        return "Enter a password."

    if user is not None or cleaned_data:
        personal_values = []
        cleaned_data = cleaned_data or {}
        for value in (
            cleaned_data.get("username"),
            cleaned_data.get("email"),
            cleaned_data.get("first_name"),
            cleaned_data.get("last_name"),
        ):
            value = str(value or "").strip().lower()
            if value:
                personal_values.append(value)
                if "@" in value:
                    personal_values.append(value.split("@", 1)[0])

        if user is not None:
            user_values = (
                getattr(user, "username", ""),
                getattr(user, "email", ""),
                getattr(user, "first_name", ""),
                getattr(user, "last_name", ""),
            )
        else:
            user_values = ()
        for value in user_values:
            value = str(value).strip().lower()
            if value:
                personal_values.append(value)
                if "@" in value:
                    personal_values.append(value.split("@", 1)[0])

        password_normalized = password.strip().lower()
        if any(
            password_normalized == value
            or (
                len(value) >= 4
                and len(password_normalized) >= 4
                and (value in password_normalized or password_normalized in value)
            )
            for value in personal_values
        ):
            return "Choose a password that isn't based on your username or email address."

    if password.isdigit():
        return "Use letters and numbers, not only digits."
    if len(password) < 8:
        return "Use at least 8 characters for a strong password."
    return "Choose a less common and stronger password."


class FriendlyPasswordMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in ("password1", "password2", "new_password1", "new_password2"):
            if field_name in self.fields:
                self.fields[field_name].help_text = ""

    def add_error(self, field, error):
        if field not in (
            "password1",
            "password2",
            "new_password1",
            "new_password2",
        ):
            return super().add_error(field, error)

        errors = error.error_list if hasattr(error, "error_list") else [error]
        friendly_errors = []
        password = None
        cleaned_data = getattr(self, "cleaned_data", {})
        for candidate in ("password1", "password2", "new_password1", "new_password2"):
            if candidate in cleaned_data:
                password = cleaned_data[candidate]
                break

        for item in errors:
            code = getattr(item, "code", "")
            message = str(item)
            if code == "password_mismatch" or "didn't match" in message:
                friendly_errors.append("Passwords don't match. Enter the same password in both fields.")
                continue
            if password is None:
                password = ""
            user = getattr(self, "instance", None)
            if user is None or not hasattr(user, "username"):
                user = getattr(self, "user", None)
            friendly_errors.append(
                friendly_password_error(password, user, cleaned_data)
            )

        for message in dict.fromkeys(friendly_errors):
            super().add_error(field, ValidationError(message))


class FriendlyPasswordChangeForm(FriendlyPasswordMixin, PasswordChangeForm):
    pass


class FriendlySetPasswordForm(FriendlyPasswordMixin, SetPasswordForm):
    pass


def _clean_employee_id(employee_id, profile=None):
    employee_id = employee_id.strip()
    existing_profiles = CustomerProfile.objects.filter(
        employee_id__iexact=employee_id
    ).exclude(employee_id="")
    if profile is not None:
        existing_profiles = existing_profiles.exclude(pk=profile.pk)
    if existing_profiles.exists():
        raise ValidationError("This employee ID is already in use.")
    return employee_id


class CustomerRegistrationForm(FriendlyPasswordMixin, UserCreationForm):
    first_name = forms.CharField(max_length=150, label="First name")
    last_name = forms.CharField(max_length=150, label="Last name")
    email = forms.EmailField()
    mobile = forms.RegexField(
        regex=r"^[0-9+() -]{7,30}$",
        max_length=30,
        error_messages={"invalid": "Enter a valid mobile number."},
        label="Mobile number",
    )
    address = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Delivery address",
    )

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "first_name", "last_name", "email")

    def clean_email(self):
        email = self.cleaned_data["email"]
        User = get_user_model()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError(
                "An account with this email address already exists."
            )
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data["last_name"]
        user.email = self.cleaned_data["email"]
        user.is_active = False
        if commit:
            user.save()
            CustomerProfile.objects.create(
                user=user,
                mobile=self.cleaned_data["mobile"],
                address=self.cleaned_data["address"],
            )
        return user


class StaffCreationForm(FriendlyPasswordMixin, UserCreationForm):
    email = forms.EmailField()
    employee_id = forms.CharField(max_length=50, label="Employee ID")

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password2"].label = "Confirm password"
        self.order_fields(
            ("username", "employee_id", "email", "password1", "password2")
        )

    def clean_employee_id(self):
        return _clean_employee_id(self.cleaned_data["employee_id"])

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.is_active = True
        user.is_staff = False
        user.is_superuser = False
        if commit:
            with transaction.atomic():
                user.save()
                staff_group, _ = Group.objects.get_or_create(
                    name=STAFF_GROUP_NAME
                )
                user.groups.add(staff_group)
                CustomerProfile.objects.create(
                    user=user,
                    employee_id=self.cleaned_data["employee_id"],
                )
        return user


class StaffUpdateForm(forms.ModelForm):
    employee_id = forms.CharField(max_length=50, label="Employee ID")
    mobile = forms.RegexField(
        regex=r"^[0-9+() -]{7,30}$",
        max_length=30,
        required=False,
        error_messages={"invalid": "Enter a valid mobile number."},
        label="Mobile number",
    )

    class Meta:
        model = get_user_model()
        fields = ("username", "first_name", "last_name", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.profile, _ = CustomerProfile.objects.get_or_create(
            user=self.instance
        )
        self.fields["employee_id"].initial = self.profile.employee_id
        self.fields["mobile"].initial = self.profile.mobile

    def clean_employee_id(self):
        return _clean_employee_id(
            self.cleaned_data["employee_id"], profile=self.profile
        )

    def save(self, commit=True):
        if not commit:
            return super().save(commit=False)
        with transaction.atomic():
            user = super().save()
            self.profile.employee_id = self.cleaned_data["employee_id"]
            self.profile.mobile = self.cleaned_data["mobile"]
            self.profile.save(update_fields=["employee_id", "mobile"])
        return user


class AdminProfileForm(forms.Form):
    username = forms.CharField(max_length=150)
    email = forms.EmailField()
    employee_id = forms.CharField(max_length=50, label="Employee ID")
    mobile = forms.RegexField(
        regex=r"^[0-9+() -]{7,30}$",
        max_length=30,
        required=False,
        error_messages={"invalid": "Enter a valid mobile number."},
        label="Mobile number",
    )

    def __init__(self, *args, user, profile, **kwargs):
        self.user = user
        self.profile = profile
        initial = kwargs.setdefault("initial", {})
        initial.setdefault("username", user.username)
        initial.setdefault("email", user.email)
        initial.setdefault("employee_id", profile.employee_id)
        initial.setdefault("mobile", profile.mobile)
        super().__init__(*args, **kwargs)

    def clean_username(self):
        username = self.cleaned_data["username"]
        users = get_user_model()._default_manager.filter(username=username)
        if users.exclude(pk=self.user.pk).exists():
            raise ValidationError("This username is already in use.")
        return username

    def clean_employee_id(self):
        return _clean_employee_id(
            self.cleaned_data["employee_id"], profile=self.profile
        )

    def save(self):
        with transaction.atomic():
            self.user.username = self.cleaned_data["username"]
            self.user.email = self.cleaned_data["email"]
            self.user.save(update_fields=["username", "email"])
            self.profile.employee_id = self.cleaned_data["employee_id"]
            self.profile.mobile = self.cleaned_data["mobile"]
            self.profile.save(update_fields=["employee_id", "mobile"])
        return self.profile


class CustomerProfileForm(forms.ModelForm):
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField()
    mobile = forms.RegexField(
        regex=r"^[0-9+() -]{7,30}$",
        max_length=30,
        required=False,
        error_messages={"invalid": "Enter a valid mobile number."},
    )

    class Meta:
        model = CustomerProfile
        fields = ["first_name", "last_name", "email", "mobile", "address", "landmark"]
        widgets = {
            "address": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, user, **kwargs):
        self.user = user
        initial = kwargs.setdefault("initial", {})
        initial.setdefault("first_name", user.first_name)
        initial.setdefault("last_name", user.last_name)
        initial.setdefault("email", user.email)
        super().__init__(*args, **kwargs)

    def save(self, commit=True):
        profile = super().save(commit=False)
        profile.user = self.user
        if commit:
            profile.save()
            self.user.first_name = self.cleaned_data["first_name"]
            self.user.last_name = self.cleaned_data["last_name"]
            self.user.email = self.cleaned_data["email"]
            self.user.save(update_fields=["first_name", "last_name", "email"])
        return profile
