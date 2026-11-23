import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("home", "0011_emailverificationtoken_token_maxlength"),
    ]

    operations = [
        migrations.DeleteModel(
            name="EmailVerificationToken",
        ),
        migrations.CreateModel(
            name="EmailOTP",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("otp_hash", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("last_sent_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="email_otp",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
    ]
