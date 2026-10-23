"""Э1.5 — карточки каталогу і вибору розміру.

Contract: `docs/instagram_bot_audit/new/10_VISUAL_MESSAGING.md` §5–§6.

Кожен тест перевіряє щось, що без нього тихо ламається у клієнта на екрані:
урізаний набір розмірів, кнопку недоступного розміру, «три товари як увесь
асортимент», категорії тому, хто вже назвав категорію.
"""
from decimal import Decimal

from django.test import SimpleTestCase, TestCase, override_settings

from management.models import IgClient, InstagramBotMessage
from management.services import ig_catalog_cards as cards
from management.services import ig_message_templates as templates
from management.services.ig_catalog_candidates import rank_candidates
from management.services.ig_commerce_types import (
    CatalogCandidate,
    CatalogGraph,
    CatalogProduct,
    CommerceTurnRequest,
    PriceSnapshot,
    PricingConfiguration,
)


def _pricing(*, prices=("1250",), fits=("oversize",), sizes=("S", "M", "L")):
    amounts = tuple(Decimal(value) for value in prices)
    configurations = tuple(
        PricingConfiguration(
            variant_id=None,
            color_id=None,
            color_slug="",
            color_label="",
            fit_code=fit,
            option_values={},
            compatible_sizes=tuple(sizes),
            price=amount,
        )
        for fit in fits
        for amount in amounts
    )
    return PriceSnapshot(
        configurations=configurations,
        minimum=min(amounts),
        maximum=max(amounts),
        exact=len(set(amounts)) == 1,
        display="",
    )


def _candidate(product_id, title="Худі Vortex", *, pricing=None, slug=""):
    return CatalogCandidate(
        product_id=product_id,
        slug=slug or f"hoodie-{product_id}",
        title=title,
        category_id=5,
        category_slug="hoodie",
        category_label="Худі",
        garment_type="hoodie",
        catalog_priority=0,
        traits={},
        pricing=pricing or _pricing(),
    )


def _candidates(count, *, start=1):
    return tuple(
        _candidate(start + index, f"Худі номер {start + index}")
        for index in range(count)
    )


def _readiness(available, *, disabled=(), title="Худі Vortex", fit="oversize"):
    return {
        "has_product": True,
        "product": {"id": 7, "title": title, "published": True, "slug": "hoodie-7"},
        "fit": {
            "required": True,
            "selected": fit,
            "options": [{"code": "oversize", "label": "Оверсайз"}],
        },
        "size": {
            "required": True,
            "selected": "",
            "available": list(available),
            "disabled": list(disabled),
            "requested_unavailable": "",
        },
    }


class CardPayloadContractTests(SimpleTestCase):
    """Э1.4 задала форму `<domain>:<gen>:<verb>:<value>`; версія лишається зовні."""

    def test_product_pick_payload_matches_the_documented_shape(self):
        self.assertEqual(cards.product_pick_payload(14, 7), "twc:1:product:14:pick:7")

    def test_size_set_payload_matches_the_documented_shape(self):
        self.assertEqual(cards.size_set_payload(14, "l"), "twc:1:size:14:set:L")

    def test_grid_payloads_are_distinct_actions(self):
        self.assertEqual(cards.size_reopen_payload(3), "twc:1:size:3:reopen")
        self.assertEqual(cards.size_question_payload(3), "twc:1:size:3:ask")
        self.assertEqual(cards.size_grid_payload(3), "twc:1:size:3:grid")

    def test_more_payload_carries_the_next_page(self):
        """Без номера сторінки друге натискання показало б ту саму трійку."""
        self.assertEqual(cards.catalog_more_payload(2, 1), "twc:1:catalog:2:more:1")
        with self.assertRaises(ValueError):
            cards.catalog_more_payload(2, 0)

    def test_payload_refuses_a_non_numeric_generation(self):
        with self.assertRaises(ValueError):
            cards.product_pick_payload("latest", 7)
        with self.assertRaises(ValueError):
            cards.product_pick_payload(1, 0)

    def test_parse_round_trips_and_keeps_the_generation_visible(self):
        action = cards.parse_card_action(cards.size_set_payload(9, "XXL"))
        self.assertEqual(action.domain, cards.ACTION_SIZE)
        self.assertEqual(action.generation, 9)
        self.assertEqual(action.verb, cards.VERB_SET)
        self.assertEqual(action.value, "XXL")

    def test_foreign_payloads_are_not_claimed(self):
        """Чужий payload — `None`, а не помилка: хід іде звичайним шляхом."""
        for raw in (
            "",
            "commerce:3:select:1",
            templates.build_payload("parcel", "got", "42"),
            "twc:1:product:pick",
            "twc:1:product:abc:pick:7",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(cards.parse_card_action(raw))


class CardinalityMatrixTests(SimpleTestCase):
    """§6: `0` / `1` / `2–3` / `4+`. Ліміт Meta у 10 елементів — не UX-ціль."""

    def test_page_is_three_not_the_provider_limit(self):
        self.assertEqual(cards.PAGE_SIZE, 3)
        self.assertLess(cards.PAGE_SIZE, templates.MAX_ELEMENTS)

    def test_zero_candidates_names_the_filter_and_offers_ways_out(self):
        plan = cards.plan_product_cards(
            (), constraints={"color": "чорний", "size": "XXL"}
        )
        self.assertEqual(plan.kind, cards.KIND_TEXT)
        self.assertIn("колір: чорний", plan.payload)
        self.assertIn("розмір: XXL", plan.payload)
        self.assertIn("інший колір", plan.payload)
        self.assertIn("інший розмір", plan.payload)

    def test_zero_candidates_does_not_invent_a_filter_that_was_not_asked(self):
        """«Спробуйте інший колір» у запиті без кольору — вигадка, і це видно."""
        plan = cards.plan_product_cards((), constraints={"size": "XXL"})
        self.assertNotIn("інший колір", plan.payload)
        self.assertIn("схожі принти", plan.payload)

    def test_one_candidate_is_a_single_card_without_a_more_button(self):
        plan = cards.plan_product_cards(_candidates(1))
        self.assertEqual(plan.kind, cards.KIND_SINGLE_CARD)
        self.assertEqual(len(plan.payload.cards), 1)
        self.assertEqual(plan.payload.quick_replies, ())
        self.assertFalse(plan.has_more)

    def test_three_candidates_are_one_page_without_a_more_button(self):
        plan = cards.plan_product_cards(_candidates(3))
        self.assertEqual(plan.kind, cards.KIND_CAROUSEL)
        self.assertEqual(len(plan.payload.cards), 3)
        self.assertEqual(plan.payload.quick_replies, ())
        self.assertFalse(plan.has_more)

    def test_four_candidates_give_three_plus_an_explicit_more(self):
        plan = cards.plan_product_cards(_candidates(4), generation=2)
        self.assertEqual(len(plan.payload.cards), 3)
        self.assertTrue(plan.has_more)
        self.assertEqual(plan.total_candidates, 4)
        self.assertEqual(
            [reply.payload for reply in plan.payload.quick_replies],
            [cards.catalog_more_payload(2, 1)],
        )

    def test_a_page_never_reads_as_the_whole_catalogue(self):
        """Три товари без згадки про залишок читаються як «це все» (§6)."""
        plan = cards.plan_product_cards(_candidates(7))
        self.assertIn("3", plan.fallback_text)
        self.assertIn("7", plan.fallback_text)

    def test_second_page_shows_the_remainder_with_page_local_positions(self):
        plan = cards.plan_product_cards(_candidates(7), page=1)
        self.assertEqual(plan.shown_product_ids, (4, 5, 6))
        self.assertEqual([entry[0] for entry in plan.shown], [1, 2, 3])
        self.assertTrue(plan.has_more)

    def test_last_page_stops_offering_more(self):
        plan = cards.plan_product_cards(_candidates(7), page=2)
        self.assertEqual(plan.shown_product_ids, (7,))
        self.assertFalse(plan.has_more)
        self.assertEqual(plan.payload.quick_replies, ())

    def test_page_beyond_the_list_answers_softly(self):
        """Клієнт натиснув — тиша тут гірша за застарілу відповідь."""
        plan = cards.plan_product_cards(_candidates(3), page=9)
        self.assertEqual(plan.kind, cards.KIND_TEXT)
        self.assertEqual(plan.reason, "page_out_of_range")


class CarouselElementTests(SimpleTestCase):
    """Структура елемента: фото варіанта, `display_short`, крій і ціна, дві кнопки."""

    def test_subtitle_is_fit_and_price(self):
        candidate = _candidate(
            7, pricing=_pricing(prices=("1250", "1490"), fits=("oversize", "classic"))
        )
        self.assertEqual(
            cards.product_subtitle(candidate), "Оверсайз/класика · від 1 250 ₴"
        )

    def test_exact_price_drops_the_from_prefix(self):
        candidate = _candidate(7, pricing=_pricing(prices=("990",), fits=("classic",)))
        self.assertEqual(cards.product_subtitle(candidate), "Класика · 990 ₴")

    def test_missing_price_leaves_the_subtitle_empty_instead_of_guessing(self):
        candidate = _candidate(7, pricing=PriceSnapshot())
        self.assertEqual(cards.product_subtitle(candidate), "")

    def test_title_uses_display_short(self):
        long_title = "Худі Vortex Tactical Edition Оверсайз Преміум"
        plan = cards.plan_product_cards((_candidate(7, long_title),))
        self.assertEqual(
            plan.payload.cards[0].title, templates.display_short(long_title)
        )
        self.assertNotIn("…", plan.payload.cards[0].title)

    def test_buttons_are_choose_postback_and_details_web_url(self):
        plan = cards.plan_product_cards((_candidate(7, slug="vortex"),), generation=4)
        button_select, button_details = plan.payload.cards[0].buttons
        self.assertEqual(button_select.kind, templates.BUTTON_POSTBACK)
        self.assertEqual(button_select.payload, "twc:1:product:4:pick:7")
        self.assertEqual(button_details.kind, templates.BUTTON_WEB_URL)
        self.assertTrue(button_details.url.endswith("/product/vortex/"))
        self.assertTrue(button_details.url.startswith("https://"))

    def test_card_photo_comes_from_the_caller_resolved_variant(self):
        url = "https://twocomms.shop/media/products/black.jpg"
        plan = cards.plan_product_cards(
            (_candidate(7),), images={7: url}, media_fallback_reason="variant_assets_missing"
        )
        self.assertEqual(plan.payload.cards[0].image_url, url)
        self.assertEqual(plan.media_fallback_reason, "variant_assets_missing")

    def test_the_plan_survives_the_meta_limit_validator(self):
        """План, який не проходить валідатор, у клієнта перетворився б у текст."""
        plan = cards.plan_product_cards(_candidates(5), generation=1)
        normalized = templates.normalize_template(plan.payload)
        self.assertEqual(len(normalized.cards), 3)
        for card in normalized.cards:
            self.assertEqual(len(card.buttons), 2)
        self.assertEqual(len(normalized.quick_replies), 1)


class CategoryVersusProductCarouselTests(SimpleTestCase):
    """Категорії тому, хто вже назвав категорію, — це меню, а не діалог."""

    def test_known_garment_forbids_the_category_carousel(self):
        self.assertEqual(
            cards.catalog_visual_kind(
                constraints={"garment_type": "tshirt"}, broad_browse=True
            ),
            cards.VISUAL_PRODUCT,
        )

    def test_known_category_also_forbids_it(self):
        self.assertEqual(
            cards.catalog_visual_kind(constraints={"category": "hoodie"}, broad_browse=True),
            cards.VISUAL_PRODUCT,
        )

    def test_broad_browse_without_a_garment_gets_categories(self):
        self.assertEqual(
            cards.catalog_visual_kind(constraints={}, broad_browse=True),
            cards.VISUAL_CATEGORY,
        )

    def test_a_narrow_query_without_a_garment_still_gets_products(self):
        self.assertEqual(
            cards.catalog_visual_kind(constraints={"color": "чорний"}, broad_browse=False),
            cards.VISUAL_PRODUCT,
        )

    def test_category_cards_carry_a_pick_postback(self):
        plan = cards.plan_category_carousel(
            (
                cards.CategoryOption(category_id=5, label="Футболки"),
                cards.CategoryOption(category_id=6, label="Худі"),
                cards.CategoryOption(category_id=7, label="Лонгсліви"),
            ),
            generation=1,
        )
        self.assertEqual(plan.kind, cards.KIND_CAROUSEL)
        self.assertEqual(
            [card.buttons[0].payload for card in plan.payload.cards],
            [cards.category_pick_payload(1, value) for value in (5, 6, 7)],
        )
        self.assertIn("Футболки", plan.fallback_text)

    def test_no_categories_degrades_to_text_not_an_empty_carousel(self):
        plan = cards.plan_category_carousel(())
        self.assertEqual(plan.kind, cards.KIND_TEXT)
        self.assertEqual(plan.reason, "no_categories")


class CandidateDigestAndCursorTests(TestCase):
    """Повний порядок зберігається ДО першої сторінки, `ще` — нова ревізія."""

    def setUp(self):
        self.ig_client = IgClient.get_or_create_for_sender("cards-cursor")

    def test_full_order_is_persisted_before_the_first_page(self):
        state = cards.open_candidate_page(
            self.ig_client, [11, 12, 13, 14, 15, 16, 17], generation=3
        )
        self.assertEqual(state["ordered"], [11, 12, 13, 14, 15, 16, 17])
        self.assertEqual(state["total"], 7)
        self.assertEqual(state["page"], 0)
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["generation"], 3)
        self.assertEqual(state["digest"], cards.candidate_digest([11, 12, 13, 14, 15, 16, 17]))
        self.ig_client.refresh_from_db()
        self.assertEqual(
            cards.candidate_page_state(self.ig_client)["ordered"],
            [11, 12, 13, 14, 15, 16, 17],
        )

    def test_more_advances_the_cursor_as_a_new_visual_revision(self):
        opened = cards.open_candidate_page(self.ig_client, [1, 2, 3, 4, 5], generation=3)
        self.assertEqual(cards.page_product_ids(opened), (1, 2, 3))
        advanced, reason = cards.advance_candidate_page(
            self.ig_client, digest=opened["digest"], generation=3
        )
        self.assertEqual(reason, "")
        self.assertEqual(advanced["page"], 1)
        self.assertEqual(advanced["revision"], 2)
        self.assertEqual(cards.page_product_ids(advanced), (4, 5))

    def test_a_stale_digest_is_refused_by_name(self):
        cards.open_candidate_page(self.ig_client, [1, 2, 3, 4], generation=1)
        state, reason = cards.advance_candidate_page(
            self.ig_client, digest="0" * 64, generation=1
        )
        self.assertEqual(state, {})
        self.assertEqual(reason, "stale_digest")

    def test_a_stale_generation_is_refused_by_name(self):
        opened = cards.open_candidate_page(self.ig_client, [1, 2, 3, 4], generation=1)
        state, reason = cards.advance_candidate_page(
            self.ig_client, digest=opened["digest"], generation=2
        )
        self.assertEqual(state, {})
        self.assertEqual(reason, "stale_generation")

    def test_an_exhausted_list_says_so_instead_of_repeating_the_page(self):
        opened = cards.open_candidate_page(self.ig_client, [1, 2, 3], generation=1)
        state, reason = cards.advance_candidate_page(
            self.ig_client, digest=opened["digest"], generation=1
        )
        self.assertEqual(state, {})
        self.assertEqual(reason, "no_more_candidates")

    def test_digest_is_sensitive_to_order_not_only_membership(self):
        self.assertNotEqual(
            cards.candidate_digest([7, 3]), cards.candidate_digest([3, 7])
        )

    def test_a_vanished_product_is_reported_not_silently_skipped(self):
        found, missing = cards.select_page(_candidates(2, start=4), (4, 5, 6))
        self.assertEqual([row.product_id for row in found], [4, 5])
        self.assertEqual(missing, (6,))


class ShownProductsSyncTests(TestCase):
    """Карусель замінює «фото + список», але фіксується тим самим контрактом."""

    def setUp(self):
        self.ig_client = IgClient.get_or_create_for_sender("cards-shown")
        self.plan = cards.plan_product_cards(_candidates(3), generation=1)

    class _Delivery:
        def __init__(self, ok=True, provider_message_id="mid-carousel"):
            self.ok = ok
            self.provider_message_id = provider_message_id

    def test_the_carousel_feeds_the_same_prompt_block_as_photos_did(self):
        from management.services.instagram_bot import shown_products_note

        cards.record_shown_cards(
            self.ig_client, self.ig_client.igsid, self.plan, self._Delivery()
        )
        note = shown_products_note(self.ig_client)
        self.assertIn("1) Худі номер 1 (id=1)", note)
        self.assertIn("2) Худі номер 2 (id=2)", note)
        self.assertIn("3) Худі номер 3 (id=3)", note)

    def test_one_carousel_is_one_history_row_with_the_real_message_id(self):
        cards.record_shown_cards(
            self.ig_client, self.ig_client.igsid, self.plan, self._Delivery()
        )
        rows = list(
            InstagramBotMessage.objects.filter(client=self.ig_client, source="catalog_media")
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].provider_message_id, "mid-carousel")
        self.assertIn("Худі номер 1", rows[0].text)
        self.assertIn("Худі номер 3", rows[0].text)

    def test_a_failed_delivery_records_nothing(self):
        recorded = cards.record_shown_cards(
            self.ig_client, self.ig_client.igsid, self.plan, self._Delivery(ok=False)
        )
        self.assertEqual(recorded, [])
        self.assertFalse(
            InstagramBotMessage.objects.filter(client=self.ig_client).exists()
        )


class SizeChoiceTests(SimpleTestCase):
    """Найдорожча помилка розділу — кнопка розміру, якого немає."""

    def test_a_disabled_size_never_becomes_a_button(self):
        """Другий замок: навіть якщо вимкнений розмір просочиться в `available`."""
        plan = cards.plan_size_choice(
            _readiness(("S", "M", "L"), disabled=("M",)), generation=1
        )
        self.assertEqual(plan.available_sizes, ("S", "L"))
        self.assertEqual(plan.disabled_sizes, ("M",))
        payloads = [button.payload for button in plan.payload.cards[0].buttons]
        self.assertEqual(payloads, ["twc:1:size:1:set:S", "twc:1:size:1:set:L"])

    def test_two_or_three_sizes_are_card_buttons_with_the_variant_photo(self):
        url = "https://twocomms.shop/media/products/black.jpg"
        plan = cards.plan_size_choice(
            _readiness(("S", "M", "L")), generation=8, image_url=url
        )
        self.assertEqual(plan.kind, cards.KIND_SINGLE_CARD)
        card = plan.payload.cards[0]
        self.assertEqual(card.image_url, url)
        self.assertEqual(card.subtitle, "Оберіть розмір із каталогу: S, M, L")
        self.assertNotIn("в наявності", card.subtitle)
        self.assertEqual(len(card.buttons), 3)
        self.assertLessEqual(len(card.buttons), templates.MAX_BUTTONS_PER_ELEMENT)

    def test_a_single_size_gets_no_buttons_at_all(self):
        """Один розмір — це факт, а не вибір (§6)."""
        plan = cards.plan_size_choice(_readiness(("L",)))
        self.assertEqual(plan.kind, cards.KIND_TEXT)
        self.assertIn("L", plan.payload)

    def test_no_available_sizes_names_the_disabled_ones_and_promises_a_check(self):
        plan = cards.plan_size_choice(_readiness((), disabled=("S", "M")))
        self.assertEqual(plan.kind, cards.KIND_TEXT)
        self.assertIn("S, M", plan.payload)
        self.assertEqual(plan.reason, "no_available_sizes")

    def test_four_sizes_become_quick_replies_with_the_size_chart(self):
        plan = cards.plan_size_choice(_readiness(("S", "M", "L", "XL")), generation=5)
        self.assertEqual(plan.kind, cards.KIND_QUICK_REPLIES)
        titles = [reply.title for reply in plan.payload.quick_replies]
        self.assertEqual(titles, ["S", "M", "L", "XL", "Таблиця розмірів"])
        self.assertEqual(
            plan.payload.quick_replies[-1].payload, cards.size_grid_payload(5)
        )

    def test_all_seven_catalogue_sizes_reach_the_client(self):
        """Три з семи — найгірший варіант: клієнт вирішить, що решти немає."""
        every = ("XS", "S", "M", "L", "XL", "XXL", "XXXL")
        plan = cards.plan_size_choice(_readiness(every))
        offered = tuple(
            reply.title for reply in plan.payload.quick_replies
            if reply.title in every
        )
        self.assertEqual(offered, every)

    def test_thirteen_sizes_keep_every_size_and_move_the_chart_into_the_text(self):
        """Ліміт на quick replies не має права урізати НАБІР розмірів."""
        many = tuple(f"S{index}" for index in range(1, 14))
        grid_url = "https://twocomms.shop/media/size_grids/classic.png"
        plan = cards.plan_size_choice(
            _readiness(many), grid={"image_url": grid_url, "columns": ("Груди",)}
        )
        self.assertEqual(plan.kind, cards.KIND_QUICK_REPLIES)
        self.assertEqual(len(plan.payload.quick_replies), templates.MAX_QUICK_REPLIES)
        self.assertEqual(
            tuple(reply.title for reply in plan.payload.quick_replies), many
        )
        self.assertIn(grid_url, plan.payload.text)

    def test_more_than_thirteen_sizes_becomes_a_grid_card(self):
        many = tuple(f"S{index}" for index in range(1, 15))
        plan = cards.plan_size_choice(
            _readiness(many),
            grid={"image_url": "https://twocomms.shop/media/size_grids/c.png", "columns": ()},
        )
        self.assertEqual(plan.kind, cards.KIND_SIZE_GRID_CARD)

    def test_requested_measurements_go_straight_to_the_grid(self):
        plan = cards.plan_size_choice(
            _readiness(("S", "M", "L")),
            grid={"image_url": "https://twocomms.shop/media/size_grids/c.png", "columns": ()},
            needs_measurements=True,
        )
        self.assertEqual(plan.kind, cards.KIND_SIZE_GRID_CARD)
        self.assertEqual(plan.reason, "measurements_requested")


class SizeGridCardTests(SimpleTestCase):
    """Сітка — факт про товар, а не розмова про клієнта (`size_confidence`)."""

    def _grid(self, **overrides):
        grid = {
            "image_url": "https://twocomms.shop/media/size_grids/oversize.png",
            "columns": ("Груди", "Довжина"),
            "sizes": ("S", "M", "L"),
        }
        grid.update(overrides)
        return grid

    def test_card_is_fit_specific_with_choose_and_question_buttons(self):
        plan = cards.plan_size_grid_card(
            generation=6,
            grid=self._grid(),
            available_sizes=("S", "M", "L"),
            fit_label="Оверсайз",
        )
        card = plan.payload.cards[0]
        self.assertEqual(card.title, "Розмірна сітка · Оверсайз")
        self.assertEqual(card.subtitle, "Заміри: Груди, Довжина")
        self.assertEqual(card.image_url, self._grid()["image_url"])
        self.assertEqual(
            [(button.title, button.payload) for button in card.buttons],
            [
                ("Обрати розмір", cards.size_reopen_payload(6)),
                ("Питання", cards.size_question_payload(6)),
            ],
        )

    def test_a_missing_grid_never_invents_measurements(self):
        plan = cards.plan_size_grid_card(
            grid=self._grid(image_url="", columns=()), available_sizes=("S", "M")
        )
        self.assertEqual(plan.kind, cards.KIND_TEXT)
        self.assertEqual(plan.reason, "grid_unavailable")
        self.assertFalse(any(char.isdigit() for char in plan.payload))

    def test_the_copy_never_mentions_the_customers_body(self):
        """Будь-який намёк на комплекцію недопустимий (01_FINDINGS, size_confidence)."""
        copy_blocks = (
            cards._GRID_TITLE, cards._GRID_TITLE_NO_FIT, cards._GRID_SUBTITLE,
            cards._GRID_FALLBACK, cards._GRID_FALLBACK_NO_FIT, cards._GRID_UNAVAILABLE,
            cards._SIZE_SUBTITLE, cards._SIZE_QUICK_TEXT, cards._SIZE_ONE_ONLY,
            cards._SIZE_NONE, cards._SIZE_NONE_WITH_DISABLED,
        )
        for block in copy_blocks:
            for lang, text in block.items():
                for forbidden in cards.FORBIDDEN_BODY_WORDS:
                    with self.subTest(lang=lang, forbidden=forbidden):
                        self.assertNotIn(forbidden, text.casefold())

    def test_grid_labels_are_within_the_provider_limit_in_three_languages(self):
        for key in ("grid_open", "grid_pick", "grid_question"):
            with self.subTest(key=key):
                langs = templates.BUTTON_LABELS[key]
                self.assertEqual(set(langs), {"uk", "ru", "en"})
                for label in langs.values():
                    self.assertLessEqual(
                        len(label), templates.MAX_BUTTON_TITLE_CHARS
                    )

    def test_grid_keys_did_not_pollute_the_size_value_namespace(self):
        """Тест на сім розмірів має лишитись правдою: сітка — не восьмий розмір."""
        sizes = [
            key for key in templates.BUTTON_LABELS
            if key.startswith("size_") and key != "size_help"
        ]
        self.assertEqual(len(sizes), 7)


class CardFeatureFlagTests(SimpleTestCase):
    """Откат розділу — флаг на кожну карточку окремо."""

    def test_cards_are_off_by_default(self):
        for flag_name in cards.CARD_FLAGS:
            with self.subTest(flag=flag_name):
                self.assertFalse(cards.card_enabled(flag_name))
        self.assertIsNone(
            cards.plan_catalog_visual(candidates=_candidates(3), constraints={})
        )
        self.assertIsNone(cards.plan_size_visual(_readiness(("S", "M"))))

    @override_settings(IG_CARD_PRODUCT_CAROUSEL=True)
    def test_product_carousel_flag_enables_only_products(self):
        plan = cards.plan_catalog_visual(
            candidates=_candidates(3), constraints={"garment_type": "hoodie"}
        )
        self.assertEqual(plan.kind, cards.KIND_CAROUSEL)
        self.assertIsNone(
            cards.plan_catalog_visual(
                categories=(cards.CategoryOption(category_id=5, label="Худі"),),
                constraints={},
                broad_browse=True,
            )
        )

    @override_settings(IG_CARD_CATEGORY_CAROUSEL=True)
    def test_category_carousel_has_its_own_flag(self):
        plan = cards.plan_catalog_visual(
            categories=(cards.CategoryOption(category_id=5, label="Худі"),),
            constraints={},
            broad_browse=True,
        )
        self.assertEqual(plan.kind, cards.KIND_CAROUSEL)

    @override_settings(IG_CARD_SIZE_CHOICE=True)
    def test_size_choice_flag_does_not_enable_the_grid(self):
        self.assertIsNotNone(cards.plan_size_visual(_readiness(("S", "M"))))
        many = tuple(f"S{index}" for index in range(1, 15))
        self.assertIsNone(cards.plan_size_visual(_readiness(many)))

    @override_settings(IG_CARD_SIZE_GRID=True)
    def test_grid_flag_does_not_enable_the_size_choice(self):
        self.assertIsNone(cards.plan_size_visual(_readiness(("S", "M"))))
        self.assertIsNotNone(
            cards.plan_size_grid_visual(
                grid={"image_url": "https://twocomms.shop/media/size_grids/o.png"},
                fit_label="Оверсайз",
            )
        )


class RankedOrderIsNotTruncatedTests(SimpleTestCase):
    """`Показати ще` неможливе, якщо ранжування викидає хвіст.

    `rank_candidates` показує трійку, але порядок решти вже відомий у момент
    ранжування. Раніше він там і вмирав.
    """

    def _graph(self, count):
        return CatalogGraph(
            products=tuple(
                CatalogProduct(
                    product_id=index,
                    slug=f"hoodie-{index}",
                    title=f"Худі номер {index}",
                    category_id=5,
                    category_slug="hoodie",
                    category_label="Худі",
                    garment_type="hoodie",
                    catalog_priority=100 - index,
                    pricing=_pricing(),
                )
                for index in range(1, count + 1)
            ),
            digest="digest",
            canonical_json="[]",
        )

    def test_full_order_survives_the_visible_page(self):
        decision = rank_candidates(
            self._graph(5), CommerceTurnRequest(garment_type="hoodie")
        )
        self.assertEqual(len(decision.candidates), 3)
        self.assertEqual(decision.ordered_product_ids, (1, 2, 3, 4, 5))

    def test_the_stored_digest_matches_the_ranked_order(self):
        decision = rank_candidates(
            self._graph(4), CommerceTurnRequest(garment_type="hoodie")
        )
        plan = cards.plan_product_cards(
            _candidates(4), generation=1
        )
        self.assertEqual(
            plan.digest, cards.candidate_digest(decision.ordered_product_ids)
        )


class ExactVariantMediaTests(SimpleTestCase):
    """`NEW-CAT-002`: фото карточки має бути фото ТОГО варіанта."""

    def test_the_resolved_variant_is_passed_to_media_selection(self):
        from unittest.mock import patch

        from management.services.ig_catalog_media import (
            CatalogMediaItem,
            CatalogMediaSelection,
            CatalogMediaState,
        )

        selection = CatalogMediaSelection(
            CatalogMediaState.READY,
            items=(
                CatalogMediaItem(
                    url="https://twocomms.shop/media/products/black.jpg",
                    title="Худі",
                    alt="Худі",
                    product_id=7,
                ),
            ),
        )
        with patch(
            "management.services.ig_catalog_media.select_catalog_media",
            return_value=selection,
        ) as mocked:
            url, reason = cards.variant_image_url(
                7, color_variant_id=42, fit_code="classic", size="L",
                selection_revision="r2", expected_revision="r2",
            )
        self.assertEqual(url, "https://twocomms.shop/media/products/black.jpg")
        self.assertEqual(reason, "")
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["color_variant_id"], 42)
        self.assertEqual(kwargs["fit_code"], "classic")
        self.assertEqual(kwargs["size"], "L")
        self.assertEqual(kwargs["expected_revision"], "r2")

    def test_a_generic_photo_carries_its_reason_into_the_plan(self):
        from unittest.mock import patch

        from management.services.ig_catalog_media import (
            CatalogMediaSelection,
            CatalogMediaState,
        )

        with patch(
            "management.services.ig_catalog_media.select_catalog_media",
            return_value=CatalogMediaSelection(
                CatalogMediaState.EMPTY, fallback_reason="color_match_ambiguous"
            ),
        ):
            images, reason = cards.card_images(((7, None),))
        self.assertEqual(images, {})
        self.assertEqual(reason, "color_match_ambiguous")
        plan = cards.plan_product_cards(
            (_candidate(7),), images=images, media_fallback_reason=reason
        )
        self.assertEqual(plan.media_fallback_reason, "color_match_ambiguous")


class RealCatalogSizeTruthTests(TestCase):
    """Розміри беруться там само, де їх бере чекаут — інакше «обрав і відмова»."""

    def setUp(self):
        from product_catalog.models import VariantSizeRule
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import (
            Catalog,
            Category,
            Product,
            ProductFitOption,
            ProductStatus,
            SizeGrid,
        )

        self.catalog = Catalog.objects.create(name="Худі", slug="cards-hoodies")
        self.category = Category.objects.create(name="Худі", slug="cards-hoodie-cat")
        self.product = Product.objects.create(
            title="Худі Vortex",
            slug="cards-hoodie",
            category=self.category,
            catalog=self.catalog,
            price=1250,
            status=ProductStatus.PUBLISHED,
        )
        ProductFitOption.objects.create(
            product=self.product, code="classic", label="Класика", is_active=True
        )
        SizeGrid.objects.create(
            catalog=self.catalog,
            name="Класика",
            guide_data={
                "columns": [
                    {"key": "size", "label": "Розмір"},
                    {"key": "chest", "label": "Груди"},
                ],
                "rows": [
                    {"size": value, "chest": "50"}
                    for value in ("S", "M", "L", "XL")
                ],
            },
        )
        # Ім'я файлу ставимо UPDATE-ом: `save()` тягне за собою хук стиснення
        # оригіналів, який шукає файл на диску і засипає лог помилками, а нам
        # потрібен лише URL.
        SizeGrid.objects.filter(catalog=self.catalog).update(
            image="size_grids/classic.png"
        )
        color = Color.objects.create(name="Чорний", primary_hex="#111111")
        self.variant = ProductColorVariant.objects.create(
            product=self.product, color=color, stock=3, sku="CARDS-BLK"
        )
        # Менеджер вимкнув M для цього кольору. Саме це знає `_disabled_sizes()`.
        VariantSizeRule.objects.create(
            variant=self.variant, fit_code="", size="M", is_enabled=False
        )
        self.ig_client = IgClient.get_or_create_for_sender("cards-readiness")

    def _readiness(self):
        from management.services.ig_checkout_readiness import checkout_readiness

        return checkout_readiness(
            self.ig_client, product_id=self.product.pk, requested_fit="classic"
        )

    def test_readiness_reports_the_disabled_size_separately(self):
        readiness = self._readiness()
        self.assertEqual(readiness["size"]["available"], ["S", "L", "XL"])
        self.assertEqual(readiness["size"]["disabled"], ["M"])
        self.assertEqual(cards.readiness_sizes(readiness), (("S", "L", "XL"), ("M",)))

    def test_the_disabled_size_never_reaches_a_button(self):
        plan = cards.plan_size_choice(self._readiness(), generation=2)
        payloads = [button.payload for button in plan.payload.cards[0].buttons]
        self.assertEqual(
            payloads,
            [
                cards.size_set_payload(2, "S"),
                cards.size_set_payload(2, "L"),
                cards.size_set_payload(2, "XL"),
            ],
        )
        self.assertNotIn(cards.size_set_payload(2, "M"), payloads)

    def test_the_card_names_the_selected_fit_from_real_options(self):
        plan = cards.plan_size_grid_card(
            grid=cards.size_grid_for_fit(self.product, "classic", variant=self.variant),
            available_sizes=("S", "L", "XL"),
            fit_label="Класика",
        )
        self.assertEqual(plan.payload.cards[0].title, "Розмірна сітка · Класика")

    def test_grid_image_and_columns_come_from_the_real_per_fit_grid(self):
        grid = cards.size_grid_for_fit(self.product, "classic", variant=self.variant)
        self.assertTrue(grid["resolved"])
        self.assertTrue(grid["image_url"].startswith("https://"))
        self.assertIn("size_grids/classic.png", grid["image_url"])
        self.assertEqual(grid["columns"], ("Груди",))
        self.assertEqual(grid["sizes"], ("S", "L", "XL"))

    def test_a_fit_without_a_grid_returns_nothing_instead_of_a_guess(self):
        grid = cards.size_grid_for_fit(self.product, "oversize", variant=self.variant)
        self.assertEqual(grid["image_url"], "")
        self.assertEqual(grid["columns"], ())

    def test_card_generation_reads_the_open_session_without_creating_one(self):
        from management.ig_bot_models import IgCommerceSelectionSession

        self.assertEqual(cards.card_generation(self.ig_client), 0)
        self.assertFalse(
            IgCommerceSelectionSession.objects.filter(client=self.ig_client).exists()
        )
        IgCommerceSelectionSession.objects.create(
            client=self.ig_client, generation=1, candidate_generation=4
        )
        self.assertEqual(cards.card_generation(self.ig_client), 4)


