from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from .models import CustomerProfile


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
        employee_id = self.cleaned_data["employee_id"].strip()
        profiles = CustomerProfile.objects.exclude(user=self.user)
        if profiles.filter(employee_id__iexact=employee_id).exclude(employee_id="").exists():
            raise ValidationError("This employee ID is already in use.")
        return employee_id

    def save(self):
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
