from __future__ import annotations

import hashlib
import logging
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


class NovaPoshtaLookupError(Exception):
    """Raised when Nova Poshta lookup data cannot be fetched or parsed."""


class NovaPoshtaLookupUnavailable(NovaPoshtaLookupError):
    """Raised when Nova Poshta lookup is temporarily unavailable."""


class NovaPoshtaDirectoryService:
    """
    Lookup adapter for Nova Poshta settlement and warehouse selection.

    This service intentionally uses the existing JSON API endpoint already wired in the
    project (`https://api.novaposhta.ua/v2.0/json/`) instead of switching checkout flows
    to the newer REST API family, because production is already configured around the
    legacy endpoint and we need a minimal-risk integration for cart/checkout UX.
    """

    API_URL = "https://api.novaposhta.ua/v2.0/json/"
    REQUEST_TIMEOUT = 10
    SETTLEMENT_CACHE_TTL = 15 * 60
    WAREHOUSE_CACHE_TTL = 5 * 60
    WAREHOUSE_DIRECTORY_CACHE_TTL = 15 * 60
    WAREHOUSE_TYPES_CACHE_TTL = 24 * 60 * 60
    FAST_WAREHOUSE_PAGE_SIZE = 50
    # Nova Poshta's legacy endpoint is Cyrillic-first. Keep common Latin
    # spellings working for checkout users and crawlers without changing the
    # canonical label returned in the response.
    SETTLEMENT_QUERY_ALIASES = {
        "kyiv": "Київ",
        "kiev": "Київ",
    }

    def __init__(self) -> None:
        self.api_key = getattr(settings, "NOVA_POSHTA_API_KEY", "") or ""
        self.api_url = getattr(settings, "NOVA_POSHTA_API_URL", self.API_URL) or self.API_URL
        self.api_url = self.api_url.rstrip("/") + "/"

    def is_available(self) -> bool:
        return bool(self.api_key)

    def search_settlements(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        normalized_query = " ".join(str(query or "").split())
        api_query = self.SETTLEMENT_QUERY_ALIASES.get(normalized_query.casefold(), normalized_query)
        normalized_limit = max(1, min(int(limit or 10), 20))
        if len(normalized_query) < 2:
            return []

        cache_key = self._cache_key(
            "settlements",
            {
                "query": api_query.casefold(),
                "limit": normalized_limit,
            },
        )
        return self._cached_lookup(
            cache_key,
            lambda: self._search_settlements_uncached(api_query, normalized_limit),
            self.SETTLEMENT_CACHE_TTL,
        )

    def search_warehouses(
        self,
        *,
        settlement_ref: str = "",
        city_ref: str = "",
        query: str = "",
        kind: str = "all",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        normalized_limit = max(1, min(int(limit or 20), 50))
        normalized_query = " ".join(str(query or "").split())
        normalized_kind = kind if kind in {"all", "branch", "postomat"} else "all"
        normalized_settlement_ref = str(settlement_ref or "").strip()
        normalized_city_ref = str(city_ref or "").strip()

        if not normalized_settlement_ref and not normalized_city_ref:
            return []

        cache_key = self._cache_key(
            "warehouses",
            {
                "settlement_ref": normalized_settlement_ref,
                "city_ref": normalized_city_ref,
                "query": normalized_query.lower(),
                "kind": normalized_kind,
                "limit": normalized_limit,
            },
        )
        return self._cached_lookup(
            cache_key,
            lambda: self._search_warehouses_uncached(
                settlement_ref=normalized_settlement_ref,
                city_ref=normalized_city_ref,
                query=normalized_query,
                kind=normalized_kind,
                limit=normalized_limit,
            ),
            self.WAREHOUSE_CACHE_TTL,
        )

    def _search_settlements_uncached(self, query: str, limit: int) -> list[dict[str, Any]]:
        payload = self._request(
            "Address",
            "searchSettlements",
            {
                "CityName": query,
                "Limit": str(limit),
                "Page": "1",
            },
        )

        addresses: list[dict[str, Any]] = []
        for chunk in payload:
            if isinstance(chunk, dict) and isinstance(chunk.get("Addresses"), list):
                addresses.extend(item for item in chunk["Addresses"] if isinstance(item, dict))

        seen: set[tuple[str, str, str]] = set()
        results: list[dict[str, Any]] = []

        for item in addresses:
            label = str(item.get("Present") or item.get("MainDescription") or "").strip()
            settlement_ref = str(item.get("SettlementRef") or item.get("Ref") or "").strip()
            city_ref = str(item.get("DeliveryCity") or item.get("CityRef") or "").strip()
            legacy_ref = str(item.get("Ref") or "").strip()
            if not label or not (settlement_ref or city_ref or legacy_ref):
                continue

            signature = (label.lower(), settlement_ref, city_ref or legacy_ref)
            if signature in seen:
                continue
            seen.add(signature)

            results.append(
                {
                    "label": label,
                    "main_description": str(item.get("MainDescription") or "").strip(),
                    "area": str(item.get("Area") or "").strip(),
                    "region": str(item.get("Region") or "").strip(),
                    "settlement_type": str(
                        item.get("SettlementTypeDescription")
                        or item.get("SettlementTypeCode")
                        or ""
                    ).strip(),
                    "settlement_ref": settlement_ref,
                    "city_ref": city_ref,
                    "legacy_ref": legacy_ref,
                    "warehouses": self._safe_int(item.get("Warehouses")),
                }
            )

        return results[:limit]

    def _search_warehouses_uncached(
        self,
        *,
        settlement_ref: str,
        city_ref: str,
        query: str,
        kind: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        type_map = self._get_warehouse_type_map()
        use_full_directory_lookup = bool(query)

        ref_candidates: list[dict[str, str]] = []
        if city_ref:
            ref_candidates.append({"CityRef": city_ref})
        if settlement_ref and settlement_ref != city_ref:
            ref_candidates.append({"SettlementRef": settlement_ref})

        last_unavailable: NovaPoshtaLookupUnavailable | None = None
        had_request_attempt = False

        for ref_payload in ref_candidates:
            for model_name in ("Address", "AddressGeneral"):
                had_request_attempt = True
                try:
                    if use_full_directory_lookup:
                        directory_items = self._get_warehouse_directory(
                            model_name=model_name,
                            ref_payload=ref_payload,
                            type_map=type_map,
                        )
                        results = self._filter_warehouse_items(
                            directory_items,
                            query=query,
                            kind=kind,
                            limit=limit,
                        )
                    else:
                        payload = self._request(
                            model_name,
                            "getWarehouses",
                            {
                                **self._build_fast_warehouse_request_properties(kind=kind, limit=limit),
                                **ref_payload,
                            },
                        )
                        results = self._filter_warehouse_items(
                            self._transform_warehouses(payload, type_map=type_map, kind="all"),
                            query="",
                            kind=kind,
                            limit=limit,
                        )
                except NovaPoshtaLookupUnavailable as exc:
                    last_unavailable = exc
                    continue
                except NovaPoshtaLookupError:
                    continue

                if results:
                    return results[:limit]

                if use_full_directory_lookup or kind != "postomat":
                    continue

                try:
                    directory_items = self._get_warehouse_directory(
                        model_name=model_name,
                        ref_payload=ref_payload,
                        type_map=type_map,
                    )
                except NovaPoshtaLookupUnavailable as exc:
                    last_unavailable = exc
                    continue
                except NovaPoshtaLookupError:
                    continue

                results = self._filter_warehouse_items(
                    directory_items,
                    query="",
                    kind=kind,
                    limit=limit,
                )
                if results:
                    return results[:limit]

        if had_request_attempt and last_unavailable is not None:
            raise last_unavailable
        return []

    def _build_fast_warehouse_request_properties(self, *, kind: str, limit: int) -> dict[str, str]:
        page_size = limit
        request_properties = {"Page": "1"}

        if kind == "postomat":
            page_size = max(limit, self.FAST_WAREHOUSE_PAGE_SIZE)
            request_properties["FindByString"] = "поштомат"

        request_properties["Limit"] = str(max(1, min(page_size, 500)))
        return request_properties

    def _get_warehouse_directory(
        self,
        *,
        model_name: str,
        ref_payload: dict[str, str],
        type_map: dict[str, str],
    ) -> list[dict[str, Any]]:
        cache_key = self._cache_key(
            "warehouse_directory",
            {
                "model_name": model_name,
                **ref_payload,
            },
        )
        return self._cached_lookup(
            cache_key,
            lambda: self._load_warehouse_directory(
                model_name=model_name,
                ref_payload=ref_payload,
                type_map=type_map,
            ),
            self.WAREHOUSE_DIRECTORY_CACHE_TTL,
        ) or []

    def _load_warehouse_directory(
        self,
        *,
        model_name: str,
        ref_payload: dict[str, str],
        type_map: dict[str, str],
    ) -> list[dict[str, Any]]:
        payload = self._request(model_name, "getWarehouses", ref_payload)
        return self._transform_warehouses(payload, type_map=type_map, kind="all")

    def _filter_warehouse_items(
        self,
        items: list[dict[str, Any]],
        *,
        query: str,
        kind: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        normalized_query = self._normalize_lookup_text(query)
        query_number = self._normalize_lookup_number(query)
        results: list[dict[str, Any]] = []

        for item in items:
            if kind != "all" and item.get("kind") != kind:
                continue
            if normalized_query and not self._warehouse_matches_query(item, normalized_query):
                continue
            results.append(item)

        if normalized_query:
            results.sort(
                key=lambda item: self._warehouse_sort_key(
                    item,
                    normalized_query=normalized_query,
                    query_number=query_number,
                )
            )

        return results[:limit]

    def _warehouse_matches_query(self, item: dict[str, Any], normalized_query: str) -> bool:
        haystacks = (
            self._normalize_lookup_text(item.get("label")),
            self._normalize_lookup_text(item.get("short_address")),
            self._normalize_lookup_text(item.get("description")),
            self._normalize_lookup_text(item.get("number")),
        )
        return any(normalized_query in haystack for haystack in haystacks if haystack)

    def _warehouse_sort_key(
        self,
        item: dict[str, Any],
        *,
        normalized_query: str,
        query_number: str,
    ) -> tuple[int, int, int, int, int, str]:
        label = self._normalize_lookup_text(item.get("label"))
        short_address = self._normalize_lookup_text(item.get("short_address"))
        description = self._normalize_lookup_text(item.get("description"))
        display_label = label or short_address or description
        raw_label = " ".join(
            str(value or "").lower()
            for value in (
                item.get("label"),
                item.get("short_address"),
                item.get("description"),
            )
        )

        starts_with_query = any(
            field.startswith(normalized_query)
            for field in (label, short_address, description)
            if field
        )
        contains_word_start = any(
            f" {normalized_query}" in field
            for field in (label, short_address, description)
            if field
        )
        exact_number_match = bool(query_number) and self._normalize_lookup_number(item.get("number")) == query_number
        number_marker_match = bool(query_number) and f"№{query_number}" in raw_label

        return (
            0 if exact_number_match else 1,
            0 if number_marker_match else 1,
            0 if starts_with_query else 1,
            0 if contains_word_start else 1,
            len(display_label),
            display_label,
        )

    def _transform_warehouses(
        self,
        payload: list[dict[str, Any]],
        *,
        type_map: dict[str, str],
        kind: str,
    ) -> list[dict[str, Any]]:
        seen: set[str] = set()
        results: list[dict[str, Any]] = []

        for item in payload:
            if not isinstance(item, dict):
                continue

            ref = str(item.get("Ref") or "").strip()
            label = str(
                item.get("ShortAddress")
                or item.get("Description")
                or item.get("DescriptionRu")
                or ""
            ).strip()
            if not ref or not label:
                continue

            warehouse_kind = self._detect_warehouse_kind(item, type_map)
            if kind != "all" and warehouse_kind != kind:
                continue
            if ref in seen:
                continue
            seen.add(ref)

            results.append(
                {
                    "ref": ref,
                    "label": label,
                    "kind": warehouse_kind,
                    "number": str(item.get("Number") or "").strip(),
                    "short_address": str(item.get("ShortAddress") or "").strip(),
                    "description": str(item.get("Description") or "").strip(),
                    "city_ref": str(item.get("CityRef") or item.get("SettlementRef") or "").strip(),
                }
            )

        return results

    def _detect_warehouse_kind(self, item: dict[str, Any], type_map: dict[str, str]) -> str:
        type_ref = str(item.get("TypeOfWarehouseRef") or item.get("TypeOfWarehouse") or "").strip()
        type_label = type_map.get(type_ref, "")
        haystack = " ".join(
            str(value or "")
            for value in (
                type_label,
                item.get("CategoryOfWarehouse"),
                item.get("TypeOfWarehouse"),
                item.get("Description"),
                item.get("ShortAddress"),
            )
        ).lower()

        if any(marker in haystack for marker in ("поштомат", "postomat", "parcel locker", "parcel shop")):
            return "postomat"
        return "branch"

    @staticmethod
    def _normalize_lookup_text(value: Any) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @staticmethod
    def _normalize_lookup_number(value: Any) -> str:
        return "".join(char for char in str(value or "") if char.isdigit())

    def _get_warehouse_type_map(self) -> dict[str, str]:
        cache_key = "nova_poshta_lookup:warehouse_types"

        def load_mapping() -> dict[str, str]:
            try:
                payload = self._request("Address", "getWarehouseTypes", {})
            except NovaPoshtaLookupError:
                return {}

            mapping: dict[str, str] = {}
            for item in payload:
                if not isinstance(item, dict):
                    continue
                ref = str(item.get("Ref") or "").strip()
                label = str(item.get("Description") or item.get("DescriptionRu") or "").strip()
                if ref and label:
                    mapping[ref] = label
            return mapping

        return cache.get_or_set(cache_key, load_mapping, self.WAREHOUSE_TYPES_CACHE_TTL) or {}

    def _request(
        self,
        model_name: str,
        called_method: str,
        method_properties: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            raise NovaPoshtaLookupUnavailable(
                "Довідник Нової пошти тимчасово недоступний. Спробуйте ще раз трохи пізніше."
            )

        payload = {
            "apiKey": self.api_key,
            "modelName": model_name,
            "calledMethod": called_method,
            "methodProperties": method_properties or {},
        }

        try:
            response = requests.post(self.api_url, json=payload, timeout=self.REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as exc:
            logger.warning("Nova Poshta lookup request failed for %s.%s: %s", model_name, called_method, exc)
            raise NovaPoshtaLookupUnavailable(
                "Довідник Нової пошти тимчасово недоступний. Спробуйте ще раз трохи пізніше."
            ) from exc
        except ValueError as exc:
            logger.warning("Nova Poshta lookup returned invalid JSON for %s.%s", model_name, called_method)
            raise NovaPoshtaLookupUnavailable(
                "Довідник Нової пошти тимчасово недоступний. Спробуйте ще раз трохи пізніше."
            ) from exc

        errors = [str(item).strip() for item in data.get("errors") or [] if str(item).strip()]
        if errors:
            raise NovaPoshtaLookupError("; ".join(errors))
        if not data.get("success"):
            raise NovaPoshtaLookupError("Не вдалося отримати довідник Нової пошти.")

        payload_data = data.get("data")
        if not isinstance(payload_data, list):
            return []
        return payload_data

    def _cache_key(self, prefix: str, payload: dict[str, Any]) -> str:
        digest = hashlib.sha1(repr(sorted(payload.items())).encode("utf-8")).hexdigest()
        return f"nova_poshta_lookup:{prefix}:{digest}"

    @staticmethod
    def _cached_lookup(cache_key: str, producer, ttl: int):
        """
        Cache-aside helper that NEVER caches empty results.

        Nova Poshta directory endpoints are eventually-consistent and occasionally
        answer a valid query with an empty list. Persisting that empty answer (as
        ``cache.get_or_set`` would) amplifies a single transient blip into a
        multi-minute outage where every lookup keeps returning nothing. We only
        cache truthy (non-empty) payloads so the next request retries the API.
        """
        cached = cache.get(cache_key)
        if cached:
            return cached
        result = producer()
        if result:
            cache.set(cache_key, result, ttl)
        return result

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None
