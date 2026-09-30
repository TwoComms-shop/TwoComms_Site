from django import template

from storefront.services.product_rich_text import render_product_rich_text

register = template.Library()


@register.filter(name="product_rich_text")
def product_rich_text(value):
    return render_product_rich_text(value)
