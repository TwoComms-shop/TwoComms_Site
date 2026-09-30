"""Review-related URL routes.

Wired into the project at ``/reviews/`` via ``twocomms/urls.py``.
"""

from __future__ import annotations

from django.urls import path

from . import views


app_name = "reviews"


urlpatterns = [
    path("merchant.xml", views.merchant_feed, name="merchant_feed"),
    path("campaign-settings/", views.campaign_settings, name="campaign_settings"),
    path("state/<slug:product_slug>/", views.review_state, name="state"),
    path(
        "submit/<slug:product_slug>/",
        views.submit_review,
        name="submit",
    ),
    path(
        "vote/<int:review_id>/",
        views.vote_review,
        name="vote",
    ),
    # Phase 21 (R12) — personal cabinet «Мої відгуки» page.
    path("my/", views.my_reviews, name="my_reviews"),
]
