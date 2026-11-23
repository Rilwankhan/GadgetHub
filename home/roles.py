from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden


STAFF_GROUP_NAME = "GadgetHub Staff"


def is_gadgethub_staff(user):
    return (
        user.is_authenticated
        and user.groups.filter(name=STAFF_GROUP_NAME).exists()
    )


def staff_member_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not is_gadgethub_staff(request.user):
            return HttpResponseForbidden(
                "You do not have permission to access the staff dashboard."
            )
        return view(request, *args, **kwargs)

    return wrapped
