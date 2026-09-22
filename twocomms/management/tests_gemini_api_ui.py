"""Focused static contracts for the Gemini Router V2 operations cockpit."""

from pathlib import Path

from django.test import SimpleTestCase


class GeminiV2PanelTemplateContractTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        base = Path(__file__).resolve().parent
        cls.template = (
            base / "templates" / "management" / "bot.html"
        ).read_text(encoding="utf-8")
        cls.script = (
            base / "static" / "management" / "gemini_v2_panel.js"
        ).read_text(encoding="utf-8")
        cls.styles = (
            base / "static" / "management" / "gemini_v2_panel.css"
        ).read_text(encoding="utf-8")
        start = cls.template.index('id="gemini-v2-panel"')
        end = cls.template.index("<!-- ===== Інструкції ===== -->", start)
        cls.panel = cls.template[start:end]

    def test_admin_api_tab_keeps_a_scoped_v2_application_panel(self):
        self.assertIn('data-tab="api">API</button>', self.template)
        self.assertIn('data-panel="api"', self.template)
        self.assertIn('id="gemini-v2-panel"', self.panel)
        self.assertIn('aria-labelledby="gemini-v2-title"', self.panel)
        self.assertIn("<h2 id=\"gemini-v2-title\">Gemini Router V2</h2>", self.panel)

        tab_start = self.template.index('data-tab="api"')
        tab_guard_start = self.template.rindex("{% if bot_is_admin %}", 0, tab_start)
        tab_guard_end = self.template.index("{% endif %}", tab_start)
        panel_start = self.template.index('id="gemini-v2-panel"')
        panel_guard_start = self.template.rindex("{% if bot_is_admin %}", 0, panel_start)
        panel_guard_end = self.template.index("{% endif %}", panel_start)
        self.assertLess(tab_guard_start, tab_start, tab_guard_end)
        self.assertLess(panel_guard_start, panel_start, panel_guard_end)

    def test_assets_are_external_and_old_hourly_health_ui_is_removed(self):
        self.assertIn(
            "{% static 'management/gemini_v2_panel.css' %}",
            self.template,
        )
        self.assertIn(
            "{% static 'management/gemini_v2_panel.js' %}",
            self.template,
        )
        self.assertNotIn("const GeminiHealth=(function(){", self.template)
        self.assertNotIn("gemini-health-rail", self.template)
        self.assertNotIn("gemini-health-segment", self.template)
        self.assertNotIn("Array.from({length:24}", self.script)
        self.assertNotIn("gemini-health-card", self.template)

    def test_semantic_subtabs_have_keyboard_contract_and_one_live_region(self):
        for name, label in (
            ("quotas", "Квоти"),
            ("routes", "Маршрути"),
            ("attempts", "Спроби"),
        ):
            self.assertIn(f'data-gemini-tab="{name}">{label}</button>', self.panel)
            self.assertIn(f'data-gemini-view="{name}"', self.panel)
            self.assertIn(
                f'aria-controls="gemini-v2-view-{name}"',
                self.panel,
            )
        self.assertIn('role="tablist"', self.panel)
        self.assertIn('role="tabpanel"', self.panel)
        self.assertEqual(self.panel.count('role="status"'), 1)
        self.assertEqual(self.panel.count('aria-live='), 1)
        for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
            self.assertIn(key, self.script)
        self.assertIn("tab.tabIndex=active?0:-1", self.script)

    def test_template_wires_only_same_origin_application_endpoints(self):
        endpoint_contracts = (
            (
                "data-quotas-url",
                "management_bot_gemini_v2_quotas_api",
            ),
            (
                "data-routes-url",
                "management_bot_gemini_v2_routes_api",
            ),
            (
                "data-attempts-url",
                "management_bot_gemini_v2_attempts_api",
            ),
            (
                "data-probe-url",
                "management_bot_gemini_health_probe_api",
            ),
        )
        for attribute, route_name in endpoint_contracts:
            self.assertIn(
                f'{attribute}="{{% url \'{route_name}\' %}}"',
                self.panel,
            )
        self.assertIn(
            "return parsed.origin===window.location.origin?parsed.href:''",
            self.script,
        )
        self.assertIn("fetch(endpoint,{method:'GET'", self.script)
        self.assertIn("fetch(urls.probe,{method:'POST'", self.script)
        self.assertNotIn("googleapis.com", self.script)
        self.assertNotIn("generativelanguage", self.script)
        self.assertNotIn("https://", self.script)

    def test_schema_one_is_strict_for_all_three_public_payloads(self):
        for contract in (
            "Number(data.schema_version)===EXPECTED_SCHEMA",
            "function validateQuotas(data)",
            "data.models.length!==MODEL_ORDER.length",
            "model.projects.length!==SLOT_ORDER.length",
            "project.slot_id===SLOT_ORDER[projectIndex]",
            "function validateRoutes(data)",
            "data.routes.length!==ROUTE_ORDER.length",
            "route.task_class===ROUTE_ORDER[index]",
            "function validateAttempts(data)",
            "data.items.length>50",
            "/^greq_[a-f0-9]{20}$/",
            "function validPublicAttempt(attempt)",
            "error.kind='schema'",
            "Непідтримувана схема",
        ):
            self.assertIn(contract, self.script)
        self.assertIn(
            "Панель не інтерпретує невідомі поля або версії.",
            self.script,
        )

    def test_quota_rails_are_model_first_and_exactly_six_slot_safe(self):
        for model in (
            "gemini-3.8-flash",
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
        ):
            self.assertIn(model, self.script)
        for slot in (
            "gslot_7f3a",
            "gslot_c921",
            "gslot_18de",
            "gslot_a604",
            "gslot_52bb",
            "gslot_e17c",
        ):
            self.assertIn(slot, self.panel)
            self.assertIn(slot, self.script)
        for contract in (
            "function modelRow(model,index,pacificReset)",
            "model.projects.forEach",
            "function projectRow(project,index,pacificReset)",
            "projectLabel(project.slot_id)",
            "details.addEventListener('toggle'",
            "other.open=false",
            "Input TPM",
            "Резерв",
            "In-flight",
            "p50",
            "p95",
        ):
            self.assertIn(contract, self.script)

    def test_unknown_and_incomplete_accounting_never_render_as_zero_capacity(self):
        for contract in (
            "metric.complete!==true",
            "return {value:'—',detail:'Облік невідомий',unknown:true}",
            "Облік вимкнений або невідомий.",
            "«—» не означає нульове використання.",
            "Доступність лише припускається",
            "Є ознака зовнішнього використання. Це попередження, а не доведена причина.",
        ):
            self.assertIn(contract, self.script)
        self.assertIn(
            "Невідоме або неповне значення завжди позначається «—», а не нулем.",
            self.panel,
        )

    def test_routes_are_read_only_and_explain_nonexclusive_pin_semantics(self):
        for route_class in (
            "no_model",
            "ordinary_live",
            "complex_live",
            "durable_analysis",
        ):
            self.assertIn(route_class, self.script)
        for contract in (
            "Базова черга",
            "Ефективна черга",
            "Модель переміщено на перше місце зі збереженням усіх резервних маршрутів.",
            "Це не ексклюзивне блокування.",
            "Вторинна ескалація аналізу:",
        ):
            self.assertIn(contract, self.script)
        route_renderer = self.script[
            self.script.index("function renderRoutes(data)") :
            self.script.index("function replyText(reply)")
        ]
        self.assertNotIn("document.createElement('form')", route_renderer)
        self.assertNotIn("fetch(", route_renderer)
        self.assertNotIn("addEventListener", route_renderer)

    def test_primary_operational_copy_is_ukrainian_without_enum_contract_changes(self):
        for contract in (
            "const ROUTE_ORDER=['no_model','ordinary_live','complex_live','durable_analysis']",
            "const ROUTE_PRESENTATION={",
            "title:'Без моделі'",
            "title:'Звичайна відповідь наживо'",
            "title:'Складна відповідь наживо'",
            "title:'Фоновий довгий аналіз'",
            "document.createTextNode(route.task_class.toUpperCase())",
            "node('h4','',presentation.title)",
            "node('p','gemini-v2-route-definition',presentation.definition)",
            "provider_success:'успішна відповідь провайдера'",
            "sent:'надіслано'",
            "квитанцію провайдера зафіксовано",
            "shadow:'тіньовий'",
            "accountingModeLabel(item.accounting_mode)",
            "mapLabel(RESOLUTION_REASON_LABELS,item.resolution.reason",
        ):
            self.assertIn(contract, self.script)

        for leaked_copy in (
            "node('h4','',route.title)",
            "node('p','gemini-v2-route-definition',route.definition)",
            "receipt є",
            "receipt немає",
            "codeLabel(item.resolution.reason)",
            "'Облік '+(item.accounting_mode||'unknown')",
        ):
            self.assertNotIn(leaked_copy, self.script)

        self.assertIn(
            "accountingValue.title='Технічний режим: '+mode",
            self.script,
        )

    def test_attempt_graph_preserves_winner_late_losers_and_cursor_errors(self):
        for contract in (
            "item.winner?modelLabel(item.winner.model)",
            "outcome.outcome==='succeeded_late'",
            "Пізній результат · не замінює переможця",
            "candidate.outcomes.forEach",
            "url.searchParams.set('cursor'",
            "url.searchParams.set('limit','25')",
            "Завантажити ще",
            "MAX_RENDERED_ATTEMPTS=100",
            "error.kind==='cursor'",
            "Уже завантажені графи залишено без змін.",
        ):
            haystack = self.panel if contract == "Завантажити ще" else self.script
            self.assertIn(contract, haystack)
        self.assertNotIn("dataset.request", self.script)
        self.assertNotIn("dataset.client", self.script)
        self.assertNotIn(".turn_ref", self.script)
        self.assertNotIn(".client_ref", self.script)
        self.assertNotIn("console.", self.script)

    def test_loading_error_empty_populated_and_stale_states_are_implemented(self):
        for contract in (
            "function showSkeleton(viewName)",
            "gemini-v2-empty is-error",
            "Запитів ще немає",
            "function renderData(viewName,data,append)",
            "state.snapshots[viewName]",
            "останні валідні",
            "root.classList.add('is-stale')",
            "AbortController",
            "controller.abort()",
            "document.hidden",
            "isOuterActive()",
            "visibilitychange",
            "window.setInterval",
            "loadView(state.active,{passive:true,force:true})",
        ):
            self.assertIn(contract, self.script)

    def test_manual_metadata_diagnostic_is_explicit_secondary_and_redacted(self):
        diagnostic_start = self.panel.index('id="gemini-v2-diagnostic"')
        diagnostic = self.panel[diagnostic_start:]
        self.assertIn('<details class="gemini-v2-diagnostic"', self.panel)
        self.assertIn("Діагностика capability/auth", diagnostic)
        self.assertIn("не доводить генераційну квоту", diagnostic)
        self.assertIn("Ручний metadata GET", diagnostic)
        self.assertIn(
            "probeButton.addEventListener('click',runProbe)",
            self.script,
        )
        self.assertIn("body.append('slot_id'", self.script)
        self.assertIn("body.append('model'", self.script)
        self.assertNotIn("provider_body", self.script)
        self.assertNotIn("custom_gemini_key", self.script)

    def test_no_environment_aliases_or_project_identity_fields_enter_panel_assets(self):
        combined = self.panel + self.script + self.styles
        for alias in (
            "GEMINI_API",
            "GEMINI_API2",
            "GEMINI_API3",
            "GEMINI_API4",
            "GEMINI_API5",
            "GEMINI_API6",
        ):
            self.assertNotIn(alias, combined)
        for private_field in (
            "project_identity",
            "project_group",
            "provider_body",
            "logical_turn_id",
            "source_message_id",
        ):
            self.assertNotIn(private_field, combined)
        self.assertIn("element.textContent", self.template)
        self.assertIn("element.textContent=String(text)", self.script)
        self.assertNotIn(".innerHTML", self.script)

    def test_mobile_accessibility_and_reduced_motion_contracts_are_external(self):
        for contract in (
            "min-height:44px",
            "@media(max-width:800px)",
            "@media(max-width:520px)",
            "@media(max-width:380px)",
            "@media(prefers-reduced-motion:reduce)",
            "grid-template-columns:1fr",
            "overflow-wrap:anywhere",
            ".gemini-v2-model>summary",
            ".gemini-v2-project-metrics",
        ):
            self.assertIn(contract, self.styles)
        self.assertIn("min-width:0", self.styles)
        self.assertNotIn("min-width:576px", self.styles)
