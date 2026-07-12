"""
Шаблонні теги Fable 5 для публічної частини:

{% load fable5_tags %}
{% fable5_color_dot variant %}          -> кружечок кольору (з огоником, якщо термо)
{% fable5_color_dot variant size=22 %}  -> розмір у пікселях
{% fable5_variant_ctx variant as vctx %} -> контекст кольору (надбавка ціни, термо-опис...)

Не забудьте підключити CSS: static/fable5/thermo-dot.css (див. INTEGRATION.md).
"""
from django import template

from ..services import color_is_thermo, variant_public_context

register = template.Library()


@register.inclusion_tag("fable5/_color_dot.html")
def fable5_color_dot(variant, size=18):
    color = variant.color
    return {
        "primary": color.primary_hex,
        "secondary": color.secondary_hex or "",
        "is_thermo": color_is_thermo(color),
        "size": size,
        "title": color.name or "",
    }


@register.simple_tag
def fable5_variant_ctx(variant):
    return variant_public_context(variant)
