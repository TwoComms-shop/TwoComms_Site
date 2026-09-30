"""Google Product Review Feed 2.4; shares identifiers with the product feed."""
from collections import defaultdict
from xml.etree import ElementTree as ET

from django.conf import settings
from django.urls import reverse

from reviews.models import Review
from reviews.services.content import has_contact_or_markup


def build_product_review_feed():
    from storefront.models import Product
    from storefront.services.marketplace_feeds import iter_feed_offers, SHOP_NAME, is_valid_gtin
    base = (getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
    rows = list(Review.objects.filter(status="approved", kind="review", rating__isnull=False,
                is_verified_purchase=True, product__status="published").select_related("product").order_by("pk"))
    offers = defaultdict(list)
    if rows:
        for offer in iter_feed_offers(base_url=base, products=Product.objects.filter(pk__in={r.product_id for r in rows})):
            offers[offer.product.pk].append(offer)
    root = ET.Element("feed", {"xmlns:vc": "http://www.w3.org/2007/XMLSchema-versioning", "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance", "xsi:noNamespaceSchemaLocation": "http://www.google.com/shopping/reviews/schema/product/2.4/product_reviews.xsd"})
    ET.SubElement(root, "version").text = "2.4"
    publisher = ET.SubElement(root, "publisher")
    ET.SubElement(publisher, "name").text = "TwoComms"
    reviews = ET.SubElement(root, "reviews")
    for row in rows:
        # Public moderation is the primary gate; a legacy contact is never exported.
        if has_contact_or_markup(" ".join((row.author_name, row.title, row.body, row.pros, row.cons))) or not offers[row.product_id]:
            continue
        review = ET.SubElement(reviews, "review")
        ET.SubElement(review, "review_id").text = f"twocomms-{row.pk}"
        reviewer = ET.SubElement(review, "reviewer")
        ET.SubElement(reviewer, "name").text = row.author_name
        ET.SubElement(review, "review_timestamp").text = row.created_at.isoformat()
        if row.title:
            ET.SubElement(review, "title").text = row.title
        ET.SubElement(review, "content").text = row.body
        if row.pros:
            ET.SubElement(ET.SubElement(review, "pros"), "pro").text = row.pros
        if row.cons:
            ET.SubElement(ET.SubElement(review, "cons"), "con").text = row.cons
        ET.SubElement(review, "review_url", {"type": "group"}).text = base + reverse("product", kwargs={"slug": row.product.slug}) + f"?review={row.pk}#review-{row.pk}"
        ratings = ET.SubElement(review, "ratings")
        ET.SubElement(ratings, "overall", {"min": "1", "max": "5"}).text = str(row.rating)
        products = ET.SubElement(review, "products")
        seen = set()
        for offer in offers[row.product_id]:
            mpn = f"{offer.article}-{row.product_id}"[:70]
            if mpn in seen:
                continue
            seen.add(mpn)
            product = ET.SubElement(products, "product")
            ids = ET.SubElement(product, "product_ids")
            if is_valid_gtin(offer.barcode):
                ET.SubElement(ET.SubElement(ids, "gtins"), "gtin").text = offer.barcode
            ET.SubElement(ET.SubElement(ids, "mpns"), "mpn").text = mpn
            ET.SubElement(ET.SubElement(ids, "brands"), "brand").text = SHOP_NAME
            ET.SubElement(product, "product_name").text = row.product.title
            ET.SubElement(product, "product_url").text = offer.product_url
        ET.SubElement(review, "is_spam").text = "false"
        ET.SubElement(review, "is_verified_purchase").text = "true"
        ET.SubElement(review, "is_incentivized_review").text = str(row.is_incentivized_review).lower()
        ET.SubElement(review, "collection_method").text = "unsolicited"
    if not len(reviews):
        root.remove(reviews)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
