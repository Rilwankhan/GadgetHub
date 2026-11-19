from django import template

register = template.Library()


@register.filter
def to_object_display_value(value):
    """Truncate long admin log object_repr strings for display."""
    value = str(value)
    if len(value) > 60:
        return value[:57] + "…"
    return value
