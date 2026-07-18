"""Staff-only JSON API for the unified admin analytics dashboard."""

from __future__ import annotations

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema

from .services.admin_analytics import build_widget_by_name, parse_analytics_filters


@extend_schema(responses=OpenApiTypes.OBJECT)
class AdminAnalyticsViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def list(self, request):
        filters = parse_analytics_filters(request.query_params)
        payload = {
            "overview": build_widget_by_name("overview", filters),
            "timeseries": build_widget_by_name("timeseries", filters),
            "integration_status": build_widget_by_name("integration-status", filters),
        }
        return Response(payload)

    def _widget(self, request, widget_name: str):
        filters = parse_analytics_filters(request.query_params)
        return Response(build_widget_by_name(widget_name, filters))

    @action(detail=False, methods=["get"], url_path="overview")
    def overview(self, request):
        return self._widget(request, "overview")

    @action(detail=False, methods=["get"], url_path="timeseries")
    def timeseries(self, request):
        return self._widget(request, "timeseries")

    @action(detail=False, methods=["get"], url_path="acquisition")
    def acquisition(self, request):
        return self._widget(request, "acquisition")

    @action(detail=False, methods=["get"], url_path="sales")
    def sales(self, request):
        return self._widget(request, "sales")

    @action(detail=False, methods=["get"], url_path="cart")
    def cart(self, request):
        return self._widget(request, "cart")

    @action(detail=False, methods=["get"], url_path="products")
    def products(self, request):
        return self._widget(request, "products")

    @action(detail=False, methods=["get"], url_path="custom-print")
    def custom_print(self, request):
        return self._widget(request, "custom-print")

    @action(detail=False, methods=["get"], url_path="survey")
    def survey(self, request):
        return self._widget(request, "survey")

    @action(detail=False, methods=["get"], url_path="ux-health")
    def ux_health(self, request):
        return self._widget(request, "ux-health")

    @action(detail=False, methods=["get"], url_path="integration-status")
    def integration_status(self, request):
        return self._widget(request, "integration-status")
