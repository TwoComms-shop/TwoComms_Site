"""Validated forms for the custom marketplace-feed control center."""

from __future__ import annotations

from django import forms

from storefront.models import (
    Category,
    MarketplaceFeed,
    MarketplaceFeedProductRule,
    Product,
)
from storefront.services.feed_registry import FEED_ADAPTERS


class MarketplaceFeedForm(forms.ModelForm):
    categories = forms.ModelMultipleChoiceField(
        queryset=Category.objects.none(), required=False, widget=forms.CheckboxSelectMultiple
    )
    include_products = forms.ModelMultipleChoiceField(
        queryset=Product.objects.none(), required=False, widget=forms.CheckboxSelectMultiple
    )
    exclude_products = forms.ModelMultipleChoiceField(
        queryset=Product.objects.none(), required=False, widget=forms.CheckboxSelectMultiple
    )
    price_min = forms.DecimalField(required=False, min_value=0, decimal_places=2)
    price_max = forms.DecimalField(required=False, min_value=0, decimal_places=2)
    min_image_count = forms.IntegerField(required=False, min_value=0, max_value=100)
    search_keywords = forms.CharField(required=False)
    availability_mode = forms.ChoiceField(
        required=False,
        choices=(
            ("", "Успадкувати"),
            ("force_in_stock", "Завжди в наявності"),
            ("force_out_of_stock", "Завжди sold out"),
        ),
    )
    availability_quantity = forms.IntegerField(required=False, min_value=0, max_value=1_000_000)
    image_mode = forms.ChoiceField(
        required=False,
        choices=(
            ("", "За замовчуванням адаптера"),
            ("main_first", "Головне фото першим"),
            ("variant_first", "Фото кольору першим"),
            ("newest_first", "Найновіше першим"),
            ("selected", "Лише вибрані фото товарів"),
        ),
    )
    image_max_count = forms.IntegerField(required=False, min_value=1, max_value=50)

    class Meta:
        model = MarketplaceFeed
        fields = ("name", "slug", "adapter", "language", "parent", "description", "is_active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["categories"].queryset = Category.objects.filter(is_active=True).order_by("name")
        products = Product.objects.filter(status="published").order_by("title", "pk")
        self.fields["include_products"].queryset = products
        self.fields["exclude_products"].queryset = products
        parents = MarketplaceFeed.objects.all().order_by("name", "pk")
        if self.instance and self.instance.pk:
            parents = parents.exclude(pk=self.instance.pk)
        self.fields["parent"].queryset = parents

        if not self.is_bound and self.instance and self.instance.pk:
            rules = self.instance.rules or {}
            filters = rules.get("filters", {})
            availability = rules.get("availability", {})
            images = rules.get("images", {})
            self.initial.update(
                {
                    "categories": filters.get("category_ids", []),
                    "include_products": filters.get("include_product_ids", []),
                    "exclude_products": filters.get("exclude_product_ids", []),
                    "price_min": filters.get("price_min"),
                    "price_max": filters.get("price_max"),
                    "min_image_count": filters.get("min_image_count"),
                    "search_keywords": ", ".join(filters.get("search_keywords", [])),
                    "availability_mode": availability.get("mode", ""),
                    "availability_quantity": availability.get("quantity"),
                    "image_mode": images.get("mode", ""),
                    "image_max_count": images.get("max_count"),
                }
            )

    def clean(self):
        cleaned = super().clean()
        adapter = cleaned.get("adapter")
        language = cleaned.get("language")
        definition = FEED_ADAPTERS.get(adapter)
        if definition and language not in definition.languages:
            self.add_error("language", "Ця мова не підтримується вибраним форматом фіда.")
        parent = cleaned.get("parent")
        if parent and adapter and parent.adapter != adapter:
            self.add_error("parent", "Батьківський фід має використовувати той самий формат.")
        price_min = cleaned.get("price_min")
        price_max = cleaned.get("price_max")
        if price_min is not None and price_max is not None and price_min > price_max:
            self.add_error("price_max", "Максимальна ціна не може бути меншою за мінімальну.")
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        filters = {}
        for form_name, rule_name in (
            ("categories", "category_ids"),
            ("include_products", "include_product_ids"),
            ("exclude_products", "exclude_product_ids"),
        ):
            values = self.cleaned_data.get(form_name)
            if values:
                filters[rule_name] = list(values.values_list("pk", flat=True))
        for form_name, rule_name in (
            ("price_min", "price_min"),
            ("price_max", "price_max"),
            ("min_image_count", "min_image_count"),
        ):
            value = self.cleaned_data.get(form_name)
            if value is not None:
                filters[rule_name] = float(value) if form_name.startswith("price_") else int(value)
        keywords = [value.strip() for value in (self.cleaned_data.get("search_keywords") or "").split(",") if value.strip()]
        if keywords:
            filters["search_keywords"] = keywords

        rules = {}
        if filters:
            rules["filters"] = filters
        availability = {}
        if self.cleaned_data.get("availability_mode"):
            availability["mode"] = self.cleaned_data["availability_mode"]
        if self.cleaned_data.get("availability_quantity") is not None:
            availability["quantity"] = self.cleaned_data["availability_quantity"]
        if availability:
            rules["availability"] = availability
        images = {}
        if self.cleaned_data.get("image_mode"):
            images["mode"] = self.cleaned_data["image_mode"]
        if self.cleaned_data.get("image_max_count") is not None:
            images["max_count"] = self.cleaned_data["image_max_count"]
        if images:
            rules["images"] = images
        instance.rules = rules
        instance.full_clean()
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class MarketplaceFeedProductRuleForm(forms.ModelForm):
    product_id = forms.ModelChoiceField(
        queryset=Product.objects.filter(status="published").order_by("title", "pk"),
        label="Товар",
    )
    image_tokens = forms.MultipleChoiceField(required=False, choices=())

    class Meta:
        model = MarketplaceFeedProductRule
        fields = ("inclusion", "availability", "quantity", "image_tokens")

    def __init__(self, *args, feed: MarketplaceFeed, **kwargs):
        self.feed = feed
        super().__init__(*args, **kwargs)
        product = None
        product_value = self.data.get("product_id") if self.is_bound else None
        if product_value:
            product = Product.objects.filter(pk=product_value).first()
        elif self.instance and self.instance.pk:
            product = self.instance.product
            self.initial["product_id"] = product.pk
        choices = []
        if product:
            if product.main_image:
                choices.append(("main", "Головне фото"))
            choices.extend((f"product:{image.pk}", f"Загальне фото #{image.pk}") for image in product.images.all())
            for variant in product.color_variants.all():
                choices.extend(
                    (f"variant:{image.pk}", f"{variant.color.name}: фото #{image.pk}")
                    for image in variant.images.all()
                )
        self.fields["image_tokens"].choices = choices

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.feed = self.feed
        instance.product = self.cleaned_data["product_id"]
        instance.image_tokens = self.cleaned_data.get("image_tokens", [])
        instance.full_clean()
        if commit:
            instance.save()
        return instance
