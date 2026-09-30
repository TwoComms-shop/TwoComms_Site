"""Recommendation outputs remain distinct across source and cache overlaps."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from storefront.recommendations import ProductRecommendationEngine


class DistinctRecommendationTests(SimpleTestCase):
    def setUp(self):
        self.current = SimpleNamespace(pk=1, id=1)
        self.first = SimpleNamespace(pk=2, id=2)
        self.second = SimpleNamespace(pk=3, id=3)
        self.third = SimpleNamespace(pk=4, id=4)
        self.cache = Mock()
        with patch('storefront.recommendations.get_cache', return_value=self.cache):
            self.engine = ProductRecommendationEngine()
        version = patch('storefront.services.catalog_helpers.get_public_product_order_version', return_value=7)
        version.start()
        self.addCleanup(version.stop)

    def test_existing_cached_overlap_and_current_product_are_removed_in_order(self):
        duplicate_instance = SimpleNamespace(pk=2, id=2)
        self.cache.get.return_value = [self.first, self.current, duplicate_instance, self.second]
        self.assertEqual(
            self.engine.get_recommendations(self.current),
            [self.first, self.second],
        )

    def test_fresh_output_and_cached_value_are_distinct(self):
        self.cache.get.return_value = None
        with patch.object(self.engine, '_get_product_recommendations', return_value=[
            self.current, self.first, self.second, self.first,
        ]):
            result = self.engine.get_recommendations(self.current)
        self.assertEqual(result, [self.first, self.second])
        self.assertEqual(self.cache.set.call_args.args[1], result)

    def test_limit_is_applied_after_stable_deduplication_of_cached_candidates(self):
        self.cache.get.return_value = [self.current, self.first, self.first, self.second, self.third]
        self.assertEqual(
            self.engine.get_recommendations(self.current, limit=2),
            [self.first, self.second],
        )

    def test_home_overlap_preserves_first_occurrence_order(self):
        self.cache.get.return_value = [self.second, self.first, self.second, self.third]
        self.assertEqual(
            self.engine.get_recommendations(),
            [self.second, self.first, self.third],
        )
