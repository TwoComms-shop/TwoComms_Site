from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from social_core.exceptions import AuthAlreadyAssociated

from storefront.social_pipeline import social_user_with_authenticated_merge


class SocialPipelineMergeTests(TestCase):
    def test_existing_google_association_is_merged_into_authenticated_session_user(self):
        current_user = User.objects.create_user(username="telegram-user")
        old_google_user = User.objects.create_user(username="google-user")
        social = MagicMock(user=old_google_user, user_id=old_google_user.pk)
        backend = SimpleNamespace(name="google-oauth2")
        strategy = SimpleNamespace(
            request=SimpleNamespace(user=current_user),
            storage=SimpleNamespace(
                user=SimpleNamespace(
                    get_social_auth=lambda provider, uid: social,
                ),
            ),
        )

        with (
            patch(
                "social_core.pipeline.social_auth.social_user",
                side_effect=AuthAlreadyAssociated(backend),
            ),
            patch("storefront.social_pipeline._merge_user_data") as merge,
        ):
            result = social_user_with_authenticated_merge(
                strategy,
                {},
                backend,
                "google-uid",
                user=current_user,
            )

        merge.assert_called_once_with(source=old_google_user, target=current_user)
        self.assertIs(result["user"], current_user)
        self.assertIs(result["social"], social)
        self.assertFalse(result["is_new"])
