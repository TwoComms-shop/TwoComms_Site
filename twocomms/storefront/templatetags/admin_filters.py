from django import template

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Получает элемент из словаря по ключу"""
    if isinstance(dictionary, dict):
        return dictionary.get(key)
    return None


@register.filter
def contains(values, item):
    try:
        return item in values
    except (TypeError, AttributeError):
        return False


@register.filter
def product_image_token(image_id):
    return f"product:{image_id}"


@register.filter
def variant_image_token(image_id):
    return f"variant:{image_id}"
