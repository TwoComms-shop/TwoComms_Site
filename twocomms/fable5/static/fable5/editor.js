/*
 * Fable 5 — єдиний редактор товару (додавання = редагування).
 * Без залежностей. Працює з API із fable5/views.py.
 */
(function () {
	"use strict";

	/* ---------------- helpers ---------------- */
	const $ = (sel, root) => (root || document).querySelector(sel);
	const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

	function esc(value) {
		return String(value == null ? "" : value)
			.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
			.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
	}

	function getCsrf() {
		const input = document.querySelector("input[name=csrfmiddlewaretoken]");
		if (input && input.value) return input.value;
		const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
		return match ? decodeURIComponent(match[1]) : "";
	}

	async function handleResponse(res) {
		let json = {};
		try { json = await res.json(); } catch (e) { /* ignore */ }
		if (!res.ok || json.ok === false) throw new Error(json.error || ("HTTP " + res.status));
		return json;
	}

	function postJSON(url, data) {
		return fetch(url, {
			method: "POST",
			headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrf() },
			body: JSON.stringify(data || {}),
		}).then(handleResponse);
	}

	function postForm(url, formData) {
		return fetch(url, { method: "POST", headers: { "X-CSRFToken": getCsrf() }, body: formData }).then(handleResponse);
	}

	function getJSON(url) {
		return fetch(url, { headers: { "X-CSRFToken": getCsrf() } }).then(handleResponse);
	}

	let toastTimer = null;
	function toast(message, isError) {
		const el = $("#f5-toast");
		el.textContent = message;
		el.className = "f5-toast " + (isError ? "f5-toast--error" : "f5-toast--ok");
		el.hidden = false;
		clearTimeout(toastTimer);
		toastTimer = setTimeout(() => { el.hidden = true; }, 3800);
	}

	const intOrNull = (v) => {
		if (v === "" || v == null) return null;
		const n = parseInt(v, 10);
		return Number.isFinite(n) ? n : null;
	};

	const DEFAULTS = {
		thermoNote: "Реагує на тепло — змінює відтінок",
		priceReason: "Термохромна тканина",
		fitReason: "Для цього кольору доступний лише оверсайз",
	};
	const defaultFitReason = (code) => code === "classic"
		? DEFAULTS.fitReason
		: "Ця посадка недоступна для цього кольору";

	function flameHtml(className) {
		return `<svg class="${className || "f5-flame-icon"}" viewBox="0 0 24 24" aria-hidden="true">
			<path d="M12 2c.7 3.4-1 5.2-2.5 6.8C7.8 10.6 6 12.5 6 15.3a6 6 0 0 0 12 0c0-2.2-.9-3.8-2-5.2-.4 1.1-1 1.9-1.9 2.5.3-3.5-.8-7.9-2.1-10.6Z" fill="currentColor"/>
			<path d="M12 21.2a3.7 3.7 0 0 1-3.7-3.7c0-1.5.8-2.4 1.7-3.4.6-.6 1.2-1.3 1.6-2.2 1.4 1.4 4.1 3.3 4.1 5.6a3.7 3.7 0 0 1-3.7 3.7Z" fill="#ffd166"/>
		</svg>`;
	}

	/* ---------------- транслітерація (дзеркало fable5/translit.py, КМУ-2010) ---------------- */
	const f5Translit = (function () {
		const UK = {
			"а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e",
			"є": "ie", "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i",
			"к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
			"с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
			"ч": "ch", "ш": "sh", "щ": "shch", "ь": "", "ю": "iu", "я": "ia",
		};
		const UK_START = { "є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya" };
		const RU = {
			"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
			"ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
			"н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
			"ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
			"ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
		};
		const APOSTROPHES = /[\u2019\u02bc'`]/g;
		const CYR_WORD = /[а-яёіїєґ0-9a-z]/;

		function detectLang(text) {
			if (/[іїєґ]/.test(text)) return "uk";
			if (/[ыэъё]/.test(text)) return "ru";
			return "uk";
		}

		function transliterate(raw) {
			const text = String(raw || "").toLowerCase().replace(APOSTROPHES, "");
			const lang = detectLang(text);
			const map = lang === "ru" ? RU : UK;
			let out = "";
			let wordStart = true;
			for (let i = 0; i < text.length; i++) {
				const ch = text[i];
				if (lang === "uk" && ch === "з" && text[i + 1] === "г") {
					out += "zgh"; // зг -> zgh (КМУ-2010)
					i += 1;
					wordStart = false;
					continue;
				}
				if (lang === "uk" && wordStart && UK_START[ch] !== undefined) {
					out += UK_START[ch];
					wordStart = false;
					continue;
				}
				if (map[ch] !== undefined) {
					out += map[ch];
					wordStart = false;
					continue;
				}
				out += ch;
				wordStart = !CYR_WORD.test(ch);
			}
			return out;
		}

		function slugify(raw) {
			const QUOTES = /[\u00ab\u00bb\u201e\u201c\u201d\u2018\u2019\u02bc"'`]/g;
			const lat = transliterate(String(raw || "").replace(QUOTES, ""));
			return lat
				.replace(/[^a-z0-9]+/g, "-")
				.replace(/-+/g, "-")
				.replace(/^-+|-+$/g, "")
				.slice(0, 80)
				.replace(/-+$/g, "");
		}

		return { transliterate, slugify, detectLang };
	})();
	window.f5Translit = f5Translit;

	/* ---------------- стан ---------------- */
	const boot = JSON.parse($("#f5-bootstrap").textContent || "null") || {};
	const dict = boot.dictionaries || {};
	const urls = boot.urls || {};

	const state = {
		product: boot.product || null,
		variants: (boot.product && boot.product.variants) || [],
		faqs: ((boot.product && boot.product.faqs) || []).map((f) => Object.assign({}, f)),
		fits: null,
		files: { main_image: null, home_card_image: null },
		feedRules: {},
		feedOnly: [],
		feeds: (dict.feeds || []).slice(),
		dirty: false,
		slugTouched: !!(boot.product && boot.product.slug),
		saving: false,
		selectedVariantIndex: 0,
	};

	function fitDefaults() {
		if (state.product && state.product.fits && state.product.fits.length) {
			return state.product.fits.map((f) => Object.assign({}, f));
		}
		return (dict.fit_presets || []).map((p, i) => ({
			code: p.code, label: p.label, is_enabled: true, is_default: i === 0, reason: "",
		}));
	}
	state.fits = fitDefaults();

	const sizesList = () => (state.product && state.product.sizes && state.product.sizes.length
		? state.product.sizes
		: (dict.default_sizes || ["S", "M", "L", "XL", "XXL"]));

	function setDirty(value) {
		state.dirty = value;
		$("#f5-dirty").hidden = !value;
		const mobile = $("#f5-mobile-state");
		if (mobile) mobile.textContent = value ? "Є незбережені зміни" : "Зміни збережено";
		$$('.f5-rail-save').forEach((dot) => dot.classList.toggle("is-dirty", value));
		updateReadiness();
	}

	function setSaveVisual(mode) {
		const button = $("#f5-save");
		const label = $("#f5-save-label");
		if (!button || !label) return;
		button.classList.toggle("is-saving", mode === "saving");
		if (mode === "saving") label.textContent = "Зберігаємо…";
		else if (mode === "saved") label.textContent = "Збережено";
		else if (mode === "error") label.textContent = "Спробувати ще";
		else label.textContent = "Зберегти";
		if (mode === "saved") setTimeout(() => { if (!state.dirty && !state.saving) label.textContent = "Зберегти"; }, 1400);
	}

	/* ---------------- шапка ---------------- */
	function renderHeader() {
		const p = state.product;
		$("#f5-header-title").textContent = p && p.title ? p.title : "Новий товар";
		const badge = $("#f5-mode-badge");
		badge.textContent = p ? "редагування" : "новий";
		badge.classList.toggle("f5-badge--new", !p);
		const link = $("#f5-view-link");
		if (p && p.public_url) { link.href = p.public_url; link.hidden = false; }
		else { link.hidden = true; }
	}

	/* ---------------- словники в select ---------------- */
	function fillSelect(select, items, valueKey, labelKey, emptyLabel) {
		const options = [];
		if (emptyLabel !== undefined) options.push(`<option value="">${esc(emptyLabel)}</option>`);
		for (const item of items || []) {
			options.push(`<option value="${esc(item[valueKey])}">${esc(item[labelKey])}</option>`);
		}
		select.innerHTML = options.join("");
	}

	/* ---------------- форма основних полів ---------------- */
	function fillForm() {
		const p = state.product || {};
		$("#f-title").value = p.title || "";
		$("#f-slug").value = p.slug || "";
		if (p.category_id != null) $("#f-category").value = String(p.category_id);
		$("#f-catalog").value = p.catalog_id != null ? String(p.catalog_id) : "";
		$("#f-size-grid").value = p.size_grid_id != null ? String(p.size_grid_id) : "";
		$("#f-price").value = p.price != null ? p.price : "";
		$("#f-discount").value = p.discount_percent != null ? p.discount_percent : "";
		$("#f-points").value = p.points_reward != null ? p.points_reward : "";
		$("#f-featured").checked = !!p.featured;
		$("#f-priority").value = p.priority != null ? p.priority : "";
		$("#f-video").value = p.video_url || "";
		$("#f-short-desc").value = p.short_description || "";
		$("#f-full-desc").value = p.full_description || "";
		$("#f-details").value = p.details_text || "";
		$("#f-audience").value = p.target_audience || "";
		$("#f-care").value = p.care_instructions || "";
		$("#f-seo-title").value = p.seo_title || "";
		$("#f-seo-desc").value = p.seo_description || "";
		$("#f-seo-keywords").value = p.seo_keywords || "";
		$("#f-main-alt").value = p.main_image_alt || "";
		$("#f-fit-selector").checked = p.fit_selector_enabled !== false;
		if (p.status) $("#f5-status").value = p.status;
		if (p.main_image_url) $("#f-main-image").src = p.main_image_url;
		if (p.home_card_image_url) $("#f-home-image").src = p.home_card_image_url;
		updateSeoCounters();
		updateSlugHint();
		updateCoverState();
	}

	function updateSeoCounters() {
		$("#f-seo-title-count").textContent = ($("#f-seo-title").value || "").length + "/160";
		$("#f-seo-desc-count").textContent = ($("#f-seo-desc").value || "").length + "/320";
		updateBaseSeoPreview();
	}

	function updateBaseSeoPreview() {
		const title = $("#f-google-title");
		const description = $("#f-google-description");
		const slug = $("#f-google-slug");
		if (!title || !description || !slug) return;
		title.textContent = $("#f-seo-title").value.trim() || (($("#f-title").value.trim() || "Назва товару") + " — TwoComms");
		description.textContent = $("#f-seo-desc").value.trim() || $("#f-short-desc").value.trim() || "Опис основної сторінки товару буде показано тут.";
		slug.textContent = $("#f-slug").value.trim() || "slug";
	}

	function updateCoverState() {
		const main = $("#f-main-image");
		const home = $("#f-home-image");
		const mainSource = $("#f-main-image-source");
		const homeSource = $("#f-home-image-source");
		if (home && main && !home.getAttribute("src") && main.getAttribute("src")) {
			home.src = main.src;
			home.dataset.fallback = "true";
		}
		if (home && home.dataset.fallback === "true" && main && main.getAttribute("src") && home.src !== main.src) home.src = main.src;
		const coverSource = (state.product && state.product.cover_source) || {};
		const coverLabels = {
			upload: "Джерело: окремий файл",
			color_image: "Джерело: фото кольору",
			product_image: "Джерело: галерея товару",
		};
		if (mainSource) {
			mainSource.textContent = main && main.getAttribute("src")
				? (coverLabels[coverSource.source_type] || "Обкладинка обрана")
				: "Обкладинка не обрана";
		}
		if (homeSource) homeSource.textContent = home && home.getAttribute("src") && home.dataset.fallback !== "true" ? "Власний override" : "Fallback: обкладинка";
	}

	function updateReadiness() {
		const scoreEl = $("#f5-readiness-score");
		if (!scoreEl) return;
		const titleReady = !!($("#f-title") && $("#f-title").value.trim());
		const priceReady = !!($("#f-price") && Number($("#f-price").value) > 0);
		const variantReady = state.variants.length > 0;
		const coverReady = !!(state.files.main_image || (state.product && state.product.main_image_url));
		const fitReady = (state.fits || []).some((fit) => fit.is_enabled);
		const checks = [
			{ ok: titleReady, label: "Додайте назву", tab: "main" },
			{ ok: priceReady, label: "Вкажіть ціну", tab: "main" },
			{ ok: variantReady, label: "Додайте колір", tab: "colors" },
			{ ok: coverReady, label: "Оберіть обкладинку", tab: "media" },
			{ ok: fitReady, label: "Увімкніть посадку", tab: "fits" },
		];
		const score = Math.round((checks.filter((item) => item.ok).length / checks.length) * 100);
		scoreEl.textContent = score + "%";
		const navProgress = $("#f5-nav-progress");
		if (navProgress) navProgress.textContent = score + "% готово";
		const bar = $("#f5-readiness-bar");
		if (bar) bar.style.width = score + "%";
		const issues = $("#f5-readiness-issues");
		if (issues) issues.innerHTML = checks.filter((item) => !item.ok).slice(0, 3).map((item) => `<button type="button" data-readiness-tab="${item.tab}">• ${esc(item.label)}</button>`).join("") || '<span class="f5-hint">Критичні поля заповнено</span>';
		const count = $("#f5-variant-count");
		if (count) count.textContent = String(state.variants.length);
		const mainTab = $('.f5-tab[data-tab="main"]');
		const colorTab = $('.f5-tab[data-tab="colors"]');
		const mediaTab = $('.f5-tab[data-tab="media"]');
		if (mainTab) mainTab.classList.toggle("is-complete", titleReady && priceReady);
		if (colorTab) colorTab.classList.toggle("is-complete", variantReady);
		if (mediaTab) mediaTab.classList.toggle("is-complete", coverReady);
	}

	function updateSlugHint() {
		const slug = $("#f-slug").value.trim();
		$("#f-slug-hint").textContent = slug
			? "Посилання: /product/" + slug + "/"
			: "ч → ch, ш → sh, щ → shch, ї → yi… Лапки викидаються, пробіли → дефіси.";
		updateBaseSeoPreview();
	}

	function autoSlug() {
		if (state.slugTouched) return;
		$("#f-slug").value = f5Translit.slugify($("#f-title").value);
		updateSlugHint();
	}

	/* ---------------- збір payload та збереження ---------------- */
	function collectProductFaqs() {
		return $$("#f-faqs .f5-faq").map((node) => ({
			id: node.dataset.id ? parseInt(node.dataset.id, 10) : null,
			question_uk: $("[data-f=question_uk]", node).value,
			question_ru: $("[data-f=question_ru]", node).value,
			question_en: $("[data-f=question_en]", node).value,
			answer_uk: $("[data-f=answer_uk]", node).value,
			answer_ru: $("[data-f=answer_ru]", node).value,
			answer_en: $("[data-f=answer_en]", node).value,
			is_active: $("[data-f=is_active]", node).checked,
		})).filter((f) => (f.question_uk || f.question_ru || f.question_en || "").trim());
	}

	function collectFits() {
		return $$("#f-fits .f5-fit-row").map((row) => {
			const enabled = $("[data-f=enabled]", row).checked;
			return {
				code: row.dataset.code,
				label: row.dataset.label,
				is_enabled: enabled,
				is_default: $("[data-f=default]", row).checked,
				reason: $("[data-f=reason]", row).value || (enabled ? "" : defaultFitReason(row.dataset.code)),
			};
		});
	}

	function collectPayload() {
		return {
			id: state.product ? state.product.id : null,
			title: $("#f-title").value.trim(),
			slug: $("#f-slug").value.trim(),
			category_id: intOrNull($("#f-category").value),
			catalog_id: intOrNull($("#f-catalog").value),
			size_grid_id: intOrNull($("#f-size-grid").value),
			price: intOrNull($("#f-price").value) || 0,
			discount_percent: intOrNull($("#f-discount").value),
			points_reward: intOrNull($("#f-points").value) || 0,
			featured: $("#f-featured").checked,
			priority: intOrNull($("#f-priority").value) || 0,
			fit_selector_enabled: $("#f-fit-selector").checked,
			status: $("#f5-status").value,
			video_url: $("#f-video").value.trim(),
			short_description: $("#f-short-desc").value,
			full_description: $("#f-full-desc").value,
			details_text: $("#f-details").value,
			target_audience: $("#f-audience").value,
			care_instructions: $("#f-care").value,
			seo_title: $("#f-seo-title").value,
			seo_description: $("#f-seo-desc").value,
			seo_keywords: $("#f-seo-keywords").value,
			main_image_alt: $("#f-main-alt").value,
			faqs: collectProductFaqs(),
			fits: collectFits(),
		};
	}

	async function saveAll(silent) {
		if (state.saving) return state.product;
		const payload = collectPayload();
		const pendingVariantDrafts = $$(".f5-variant").map((card, index) => {
			const variant = state.variants[index];
			return variant ? { index, data: collectVariantData(card, variant) } : null;
		}).filter(Boolean);
		$$("#f-stock [data-variant-index][data-dirty=\"true\"]").forEach((block) => {
			const index = parseInt(block.dataset.variantIndex, 10);
			const draft = pendingVariantDrafts.find((item) => item.index === index);
			if (draft) draft.data.sizes = collectStockSizes(block);
		});
		const pendingFeedDrafts = $$("#f-feeds .f5-feed[data-dirty=\"true\"]").map((card) => ({
			card: card,
			payload: collectFeedPayload(card),
		}));
		if (!payload.title) {
			toast("Вкажіть назву товару", true);
			throw new Error("no title");
		}
		state.saving = true;
		$("#f5-save").disabled = true;
		$("#f5-mobile-save").disabled = true;
		setSaveVisual("saving");
		try {
			const fd = new FormData();
			fd.append("payload", JSON.stringify(payload));
			if (state.files.main_image) fd.append("main_image", state.files.main_image);
			if (state.files.home_card_image) fd.append("home_card_image", state.files.home_card_image);
			const resp = await postForm(urls.product_save, fd);
			const wasNew = !state.product;
			const savedVariants = [];
			for (const draft of pendingVariantDrafts) {
				draft.data.product_id = resp.product.id;
				const variantResp = await postJSON(urls.variant_save, draft.data);
				// Persist the returned ID immediately. If a later variant request fails,
				// retrying the global save updates this variant instead of duplicating it.
				state.variants[draft.index] = variantResp.variant;
				draft.data.id = variantResp.variant.id;
				if (variantResp.variant.is_default) {
					savedVariants.forEach((variant) => { variant.is_default = false; });
				}
				savedVariants.push(variantResp.variant);
			}
			for (const draft of pendingFeedDrafts) {
				draft.payload.product_id = resp.product.id;
				await persistFeedPayload(draft.payload);
				draft.card.dataset.dirty = "false";
			}
			if (pendingVariantDrafts.length) resp.product.variants = savedVariants;
			state.product = resp.product;
			state.variants = resp.product.variants || [];
			state.faqs = (resp.product.faqs || []).map((f) => Object.assign({}, f));
			state.fits = fitDefaults();
			state.files.main_image = null;
			state.files.home_card_image = null;
			if (resp.created && resp.edit_url) {
				history.replaceState(null, "", resp.edit_url); // add -> edit без перезавантаження
			}
			renderHeader();
			fillForm();
			renderFits();
			renderFaqs();
			renderGalleries();
			renderVariants();
			if (wasNew) loadFeeds();
			setDirty(false);
			setSaveVisual("saved");
			if (!silent) toast(resp.created ? "Товар створено — працюємо далі без виходу" : "Збережено");
			return state.product;
		} catch (err) {
			setSaveVisual("error");
			toast("Помилка збереження: " + err.message, true);
			throw err;
		} finally {
			state.saving = false;
			$("#f5-save").disabled = false;
			$("#f5-mobile-save").disabled = false;
		}
	}

	async function ensureProduct() {
		if (state.product && state.product.id) return state.product;
		toast("Спочатку збережемо чернетку товару…");
		return saveAll(true);
	}

	/* ---------------- кружечок кольору ---------------- */
	function dotHtml(color, size) {
		const s = size || 18;
		const primary = (color && color.primary_hex) || "#888888";
		const secondary = color && color.secondary_hex;
		const bg = secondary
			? `background:linear-gradient(135deg, ${esc(primary)} 0%, ${esc(primary)} 49%, ${esc(secondary)} 51%, ${esc(secondary)} 100%);`
			: `background:${esc(primary)};`;
		const flame = color && color.is_thermo
			? flameHtml("f5-dot__flame")
			: "";
		const cls = "f5-dot" + (color && color.is_thermo ? " f5-dot--thermo" : "");
		return `<span class="${cls}" style="width:${s}px;height:${s}px;${bg}" title="${esc((color && color.name) || "")}">${flame}</span>`;
	}

	/* ---------------- галереї (append + drag&drop + вибір головної) ---------------- */
	function thumbHtml(img, kind, variantId, index) {
		return `<figure class="f5-thumb" draggable="true" data-id="${img.id}" data-kind="${kind}"${variantId ? ` data-variant="${variantId}"` : ""}>
			<span class="f5-thumb__order">${index + 1}</span>
			<img src="${esc(img.url)}" alt="" loading="lazy">
			<div class="f5-thumb__bar">
				<button type="button" class="f5-btn f5-btn--ghost f5-btn--small" data-act="cover" aria-label="Зробити обкладинкою" title="Зробити обкладинкою">Обкладинка</button>
				<button type="button" class="f5-btn f5-btn--ghost f5-btn--small" data-act="home" aria-label="Зробити карткою на головній" title="Картка на головній">Головна</button>
				<button type="button" class="f5-btn f5-btn--danger f5-btn--small" data-act="del" aria-label="Видалити зображення" title="Видалити">×</button>
			</div>
			<input class="f5-input f5-thumb__alt" value="${esc(img.alt)}" placeholder="alt для SEO">
		</figure>`;
	}

	function renderGalleries() {
		const gallery = $("#f-product-gallery");
		const images = (state.product && state.product.images) || [];
		gallery.innerHTML = images.length
			? images.map((img, i) => thumbHtml(img, "product", null, i)).join("")
			: '<p class="f5-hint">Галерея поки порожня. Додайте перший кадр — нові фото завжди додаються в кінець.</p>';
	}

	async function uploadImages(kind, variantId, fileList) {
		const files = Array.from(fileList || []).filter((f) => f && f.type.indexOf("image/") === 0);
		if (!files.length) return;
		await ensureProduct();
		const fd = new FormData();
		fd.append("product_id", state.product.id);
		fd.append("target", kind);
		if (variantId) fd.append("variant_id", variantId);
		for (const f of files) fd.append("files", f);
		try {
			const resp = await postForm(urls.images_upload, fd);
			if (kind === "variant") {
				const variant = state.variants.find((v) => String(v.id) === String(variantId));
				if (variant) variant.images = (variant.images || []).concat(resp.images);
				renderVariants();
			} else {
				state.product.images = (state.product.images || []).concat(resp.images);
				renderGalleries();
			}
			toast("Додано картинок: " + resp.images.length + " (append, без перезапису; оптимізація у фоні)");
		} catch (err) {
			toast("Помилка завантаження: " + err.message, true);
		}
	}

	function galleryImagesRef(kind, variantId) {
		if (kind === "variant") {
			const variant = state.variants.find((v) => String(v.id) === String(variantId));
			return variant ? (variant.images || []) : [];
		}
		return (state.product && state.product.images) || [];
	}

	async function handleThumbAction(btn) {
		const fig = btn.closest(".f5-thumb");
		const kind = fig.dataset.kind;
		const variantId = fig.dataset.variant || null;
		const imageId = parseInt(fig.dataset.id, 10);
		const act = btn.dataset.act;
		try {
			if (act === "del") {
				if (!confirm("Видалити картинку?")) return;
				await postJSON(urls.image_update, { product_id: state.product.id, kind: kind, id: imageId, delete: true });
				const images = galleryImagesRef(kind, variantId);
				const idx = images.findIndex((im) => im.id === imageId);
				if (idx >= 0) images.splice(idx, 1);
				if (kind === "variant") renderVariants(); else renderGalleries();
				toast("Картинку видалено");
			} else if (act === "cover" || act === "home") {
				const resp = await postJSON(urls.set_cover, {
					product_id: state.product.id, kind: kind, image_id: imageId,
					target: act === "home" ? "home_card" : "main",
				});
				state.product.main_image_url = resp.main_image_url;
				state.product.home_card_image_url = resp.home_card_image_url;
				if (resp.cover_source) state.product.cover_source = resp.cover_source;
				if (resp.main_image_url) $("#f-main-image").src = resp.main_image_url;
				if (resp.home_card_image_url) { $("#f-home-image").src = resp.home_card_image_url; delete $("#f-home-image").dataset.fallback; }
				updateCoverState();
				toast(act === "home" ? "Встановлено карткою на головній" : "Встановлено головною картинкою");
			}
		} catch (err) {
			toast("Помилка: " + err.message, true);
		}
	}

	let draggedThumb = null;
	document.addEventListener("dragstart", (e) => {
		const fig = e.target.closest && e.target.closest(".f5-thumb");
		if (!fig) return;
		draggedThumb = fig;
		fig.classList.add("is-dragging");
		e.dataTransfer.effectAllowed = "move";
	});
	document.addEventListener("dragover", (e) => {
		if (!draggedThumb) return;
		const over = e.target.closest && e.target.closest(".f5-thumb");
		if (!over || over === draggedThumb || over.parentElement !== draggedThumb.parentElement) return;
		e.preventDefault();
		const rect = over.getBoundingClientRect();
		const after = (e.clientX - rect.left) > rect.width / 2;
		over.parentElement.insertBefore(draggedThumb, after ? over.nextSibling : over);
	});
	document.addEventListener("dragend", async () => {
		if (!draggedThumb) return;
		const fig = draggedThumb;
		draggedThumb = null;
		fig.classList.remove("is-dragging");
		const container = fig.parentElement;
		const kind = fig.dataset.kind;
		const variantId = fig.dataset.variant || null;
		const ids = $$(".f5-thumb", container).map((el) => parseInt(el.dataset.id, 10));
		try {
			await postJSON(urls.images_reorder, { product_id: state.product.id, kind: kind, variant_id: variantId, ids: ids });
			const images = galleryImagesRef(kind, variantId);
			images.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));
			images.forEach((im, i) => { im.order = i; });
			$$(".f5-thumb__order", container).forEach((el, i) => { el.textContent = i + 1; });
			toast("Порядок картинок збережено");
		} catch (err) {
			toast("Помилка сортування: " + err.message, true);
		}
	});

	/* ---------------- посадки товару ---------------- */
	function renderFits() {
		$("#f-fits").innerHTML = state.fits.map((fit) => `
			<div class="f5-fit-row${fit.is_enabled ? "" : " is-disabled"}" data-code="${esc(fit.code)}" data-label="${esc(fit.label)}">
				<label class="f5-switch" title="Доступність посадки"><input type="checkbox" data-f="enabled" ${fit.is_enabled ? "checked" : ""}><i></i></label>
				<strong>${esc(fit.label)}</strong>
				<label class="f5-check"><input type="radio" name="f5-fit-default" data-f="default" ${fit.is_default ? "checked" : ""}> за замовчуванням</label>
				<input class="f5-input" data-f="reason" value="${esc(fit.reason)}" placeholder="${esc(defaultFitReason(fit.code))}">
			</div>`).join("");
	}

	$("#f-fits").addEventListener("change", (e) => {
		if (!e.target.matches("[data-f=enabled]")) return;
		const row = e.target.closest(".f5-fit-row");
		const code = row.dataset.code;
		const enabled = e.target.checked;
		row.classList.toggle("is-disabled", !enabled);
		const fit = state.fits.find((item) => item.code === code);
		if (fit) fit.is_enabled = enabled;
		$$(`[data-fit-cluster="${code}"]`).forEach((cluster) => {
			const checkbox = $("[data-f=fit_enabled]", cluster);
			if (checkbox) checkbox.disabled = !enabled;
			const variantEnabled = !checkbox || checkbox.checked;
			const effectiveEnabled = enabled && variantEnabled;
			cluster.dataset.productEnabled = enabled ? "true" : "false";
			cluster.classList.toggle("is-disabled", !effectiveEnabled);
			const grid = $("[data-f=variant_size_grid]", cluster);
			const reasonWrap = $("[data-role=fit-reason]", cluster);
			const reason = $("[data-f=fit_reason]", cluster);
			const globalNote = $("[data-role=fit-global-note]", cluster);
			const sourceBadge = $(".f5-fit-row .f5-source-badge", cluster);
			if (grid) grid.disabled = !effectiveEnabled;
			if (reasonWrap) reasonWrap.hidden = !enabled || variantEnabled;
			if (reason) reason.disabled = !enabled || variantEnabled;
			if (globalNote) globalNote.hidden = enabled;
			if (sourceBadge) sourceBadge.textContent = enabled ? "Для цього кольору" : "Вимкнено в товарі";
			$$('.f5-size-cell', cluster).forEach((cell) => {
				const button = $("[data-act=size-toggle]", cell);
				const stock = $("[data-f=stock]", cell);
				if (button) { button.disabled = !effectiveEnabled; button.setAttribute("aria-pressed", effectiveEnabled && !cell.classList.contains("is-off") ? "true" : "false"); }
				if (stock) stock.disabled = !effectiveEnabled;
			});
			const card = cluster.closest(".f5-variant");
			if (card) syncCombinationAvailability($(`[data-combination-fit="${code}"]`, card), effectiveEnabled);
		});
		$$(`#f-stock .f5-size-cell[data-fit="${code}"]`).forEach((cell) => {
			if (!enabled) cell.classList.add("is-off");
			const button = $("[data-act=size-toggle]", cell);
			if (button) { button.disabled = !enabled; button.setAttribute("aria-pressed", enabled && !cell.classList.contains("is-off") ? "true" : "false"); }
		});
		setDirty(true);
	});

	/* ---------------- FAQ ---------------- */
	function faqHtml(faq) {
		faq = faq || {};
		return `<div class="f5-faq"${faq.id ? ` data-id="${faq.id}"` : ""}>
			<div class="f5-faq__head">
				<label class="f5-check"><input type="checkbox" data-f="is_active" ${faq.is_active !== false ? "checked" : ""}> активне</label>
				<button type="button" class="f5-btn f5-btn--danger f5-btn--small" data-act="faq-del" aria-label="Видалити питання" title="Видалити питання">✕</button>
			</div>
			<div class="f5-faq__langs">
				<label class="f5-field"><span>Питання UA</span><input class="f5-input" data-f="question_uk" value="${esc(faq.question_uk)}"></label>
				<label class="f5-field"><span>Питання RU</span><input class="f5-input" data-f="question_ru" value="${esc(faq.question_ru)}"></label>
				<label class="f5-field"><span>Питання EN</span><input class="f5-input" data-f="question_en" value="${esc(faq.question_en)}"></label>
				<label class="f5-field"><span>Відповідь UA</span><textarea class="f5-input" rows="2" data-f="answer_uk">${esc(faq.answer_uk)}</textarea></label>
				<label class="f5-field"><span>Відповідь RU</span><textarea class="f5-input" rows="2" data-f="answer_ru">${esc(faq.answer_ru)}</textarea></label>
				<label class="f5-field"><span>Відповідь EN</span><textarea class="f5-input" rows="2" data-f="answer_en">${esc(faq.answer_en)}</textarea></label>
			</div>
		</div>`;
	}

	function renderFaqs() {
		const box = $("#f-faqs");
		box.innerHTML = state.faqs.length
			? state.faqs.map(faqHtml).join("")
			: '<p class="f5-hint">FAQ ще немає. Доступно вже при створенні товару — зберігається разом із товаром кнопкою «Зберегти».</p>';
	}

	/* ---------------- кольори (inline-редагування) ---------------- */
	function emptyVariant() {
		return {
			id: null, order: state.variants.length, is_default: state.variants.length === 0,
			sku: "", price_override: null,
			color: { id: null, name: "", primary_hex: "#222222", secondary_hex: "", is_thermo: false, thermo_note: "", description: "" },
			images: [],
			details: { display_name: "", price_delta: 0, price_delta_reason: "", marketing_html: "", youtube_url: "", seo_title: "", seo_description: "", seo_keywords: "" },
			fits: state.fits.map((f) => ({ fit_code: f.code, is_enabled: true, reason: "" })),
			sizes: [], size_grids: [], blank_links: [], combinations: [], faqs: [],
			_open: true,
		};
	}

	function sizeRule(variant, fitCode, size) {
		return (variant.sizes || []).find((s) => s.fit_code === fitCode && s.size === size);
	}

	function sizeGridHtml(variant, onlyFit) {
		const enabledFits = state.fits.filter((f) => f.is_enabled && (!onlyFit || f.code === onlyFit));
		const rows = enabledFits.length ? enabledFits : (onlyFit ? state.fits.filter((f) => f.code === onlyFit) : [{ code: "", label: "Всі посадки" }]);
		return rows.map((fit) => {
			const fitRule = (variant.fits || []).find((rule) => rule.fit_code === fit.code);
			const fitEnabled = fit.is_enabled && (!fitRule || fitRule.is_enabled);
			const cells = sizesList().map((size) => {
				const rule = sizeRule(variant, fit.code, size) || { is_enabled: true, stock: null };
				const stockCls = rule.stock === 0 ? " f5-stock-zero" : (rule.stock != null && rule.stock <= 3 ? " f5-stock-low" : "");
				const enabled = fitEnabled && rule.is_enabled;
				return `<div class="f5-size-cell${enabled ? "" : " is-off"}${stockCls}" data-fit="${esc(fit.code)}" data-size="${esc(size)}">
					<button type="button" data-act="size-toggle" aria-pressed="${enabled ? "true" : "false"}" title="${enabled ? "Вимкнути" : "Увімкнути"} розмір ${esc(size)}"${fitEnabled ? "" : " disabled"}>${esc(size)}</button>
					<input type="number" min="0" data-f="stock" value="${rule.stock != null ? rule.stock : ""}" placeholder="∞" aria-label="Залишок ${esc(size)}"${fitEnabled ? "" : " disabled"}>
				</div>`;
			}).join("");
			return `<div class="f5-size-grid" data-role="fit-sizes" data-fit="${esc(fit.code)}">${cells}</div>`;
		}).join("");
	}

	function colorPickerHtml(variant) {
		const options = (dict.colors || []).map((c) => `
			<button type="button" class="f5-color-option${variant.color.id === c.id ? " is-selected" : ""}" data-act="pick-color" data-color='${esc(JSON.stringify(c))}'>
				${dotHtml(c, 18)} <span>${esc(c.name || c.primary_hex)}</span>
			</button>`).join("");
		return `<div class="f5-color-builder">
			<div class="f5-swatch-stage"><div><span data-role="dot-preview">${dotHtml(variant.color, 112)}</span><p data-role="swatch-name">${esc(variant.color.name || "Новий колір")}</p></div></div>
			<div>
				<div class="f5-variant-pane__head"><div><h3>Матеріал і колір</h3><p>Оберіть готовий колір або створіть власний. Preview оновлюється одразу.</p></div><span class="f5-source-badge">Рівень: колір</span></div>
				<div class="f5-color-picker">${options || '<span class="f5-hint">Бібліотека порожня — створіть перший колір</span>'}</div>
				<div class="f5-row">
					<label class="f5-field"><span>Назва кольору</span><input class="f5-input" data-f="color_name" value="${esc(variant.color.name)}" placeholder="Напр.: Термо-зелена"></label>
					<label class="f5-field"><span>Основний HEX</span><span class="f5-row"><input class="f5-color-native" type="color" data-f="color_pick" value="${/^#[0-9a-fA-F]{6}$/.test(variant.color.primary_hex || "") ? esc(variant.color.primary_hex) : "#222222"}" aria-label="Основний колір"><input class="f5-input" data-f="color_hex" value="${esc(variant.color.primary_hex)}" placeholder="#000000"></span></label>
					<label class="f5-field"><span>Другий HEX</span><input class="f5-input" data-f="color_hex2" value="${esc(variant.color.secondary_hex)}" placeholder="Для split-свотча"></label>
				</div>
				<div class="f5-thermo-toggle">
					<label class="f5-switch" title="Термохромна тканина"><input type="checkbox" data-f="is_thermo" ${variant.color.is_thermo ? "checked" : ""}><i></i></label>
					<span><strong>Термохромна тканина</strong><small>Додає анімований SVG-вогонь у preview та публічний swatch</small></span>
					<span class="f5-flame-mark">${flameHtml()}</span>
				</div>
				<label class="f5-field"><span>Коротка примітка про термо</span><input class="f5-input" data-f="thermo_note" value="${esc(variant.color.thermo_note)}" placeholder="${esc(DEFAULTS.thermoNote)}"><small data-role="thermo-fallback">${variant.color.thermo_note ? "Власний текст" : "Порожньо — автоматично: “" + esc(DEFAULTS.thermoNote) + "”"}</small></label>
				<label class="f5-field"><span>Опис тканини / кольору</span><textarea class="f5-input" rows="3" data-f="color_description" placeholder="Що відрізняє цей матеріал і як поводиться колір">${esc(variant.color.description)}</textarea></label>
			</div>
		</div>`;
	}

	function effectiveVariantPrice(variant) {
		const base = variant.price_override != null ? Number(variant.price_override) : Number($("#f-price").value || (state.product && state.product.price) || 0);
		return Math.max(0, base + Number((variant.details && variant.details.price_delta) || 0));
	}

	function variantRailHtml(variant, index) {
		const details = variant.details || {};
		const image = (variant.images || [])[0] || null;
		const enabledFits = state.fits.filter((fit) => {
			const rule = (variant.fits || []).find((item) => item.fit_code === fit.code);
			return fit.is_enabled && (!rule || rule.is_enabled);
		});
		const seoReady = !!(details.seo_title && details.seo_description);
		return `<button type="button" id="f5-variant-tab-${index}" class="f5-rail-item${index === state.selectedVariantIndex ? " is-active" : ""}" data-variant-select="${index}" role="tab" aria-controls="f5-variant-panel-${index}" aria-selected="${index === state.selectedVariantIndex ? "true" : "false"}" tabindex="${index === state.selectedVariantIndex ? "0" : "-1"}">
			<span class="f5-rail-media">${image ? `<img src="${esc(image.url)}" alt="" loading="lazy">` : '<span class="f5-rail-placeholder"><svg class="f5-icon"><use href="#f5-i-media"/></svg></span>'}${dotHtml(variant.color, 18)}</span>
			${variant.color.is_thermo ? `<span class="f5-rail-thermo" title="Термохромна тканина">${flameHtml()}</span>` : ""}
			<span class="f5-rail-copy"><span class="f5-rail-title">${esc(details.display_name || variant.color.name || "Новий колір")}</span><span class="f5-rail-price">${effectiveVariantPrice(variant)} грн</span><span class="f5-rail-meta">${enabledFits.map((fit) => `<span>${esc(fit.label)}</span>`).join("") || "Посадки вимкнено"}</span><span class="f5-rail-health"><span class="${seoReady ? "is-ok" : ""}">SEO ${seoReady ? "готово" : "неповне"}</span><span>${(variant.images || []).length} фото</span></span></span>
			<span class="f5-rail-save${state.dirty ? " is-dirty" : ""}" title="${state.dirty ? "Є незбережені зміни" : "Збережено"}"></span>
		</button>`;
	}

	function fitOptionKey(fitCode) {
		return fitCode ? "fit=" + fitCode : "";
	}

	function selectOptions(items, selected, emptyLabel) {
		const rows = items || [];
		const missing = selected && !rows.some((item) => String(item.id) === String(selected));
		return `<option value="">${esc(emptyLabel || "— успадкувати —")}</option>`
			+ rows.map((item) => `<option value="${esc(item.id)}"${String(item.id) === String(selected || "") ? " selected" : ""}>${esc(item.name)}</option>`).join("")
			+ (missing ? `<option value="${esc(selected)}" selected>Недоступний ресурс #${esc(selected)} — збережено</option>` : "");
	}

	function storageBlankHtml(variant) {
		const enabledFits = state.fits.filter((fit) => {
			const rule = (variant.fits || []).find((item) => item.fit_code === fit.code);
			return fit.is_enabled && (!rule || rule.is_enabled);
		});
		return `<div class="f5-storage-list">${(enabledFits.length ? enabledFits : state.fits.slice(0, 1)).map((fit) => {
			const key = fitOptionKey(fit.code);
			const link = (variant.blank_links || []).find((item) => item.option_key === key) || {};
			return `<div class="f5-storage-slot" data-role="warehouse-blank" data-option-key="${esc(key)}" data-state="${link.storage_subcategory_id ? "selected" : "empty"}"><span class="f5-storage-slot__icon"><svg class="f5-icon"><use href="#f5-i-core"/></svg></span><span class="f5-storage-slot__copy"><strong>${esc(fit.label)}</strong><small>Списання за кольором, посадкою та розміром після покупки</small></span><label class="f5-field"><span>Заготовка</span><select class="f5-input" data-f="storage_blank" aria-label="Заготовка ${esc(fit.label)}">${selectOptions(dict.storage_blanks || [], link.storage_subcategory_id, "— не прив’язано —")}</select></label><label class="f5-field"><span>Примітка</span><input class="f5-input" data-f="storage_note" value="${esc(link.note || "")}" placeholder="Напр.: CRC термо-зелена"></label></div>`;
		}).join("")}</div>`;
	}

	function fitWorkspaceHtml(variant) {
		return state.fits.map((fit) => {
			const rule = (variant.fits || []).find((item) => item.fit_code === fit.code) || { is_enabled: true, reason: "" };
			const productEnabled = fit.is_enabled !== false;
			const effectiveEnabled = productEnabled && rule.is_enabled;
			const key = fitOptionKey(fit.code);
			const assignment = (variant.size_grids || []).find((item) => item.option_key === key) || {};
			return `<section class="f5-fit-cluster${effectiveEnabled ? "" : " is-disabled"}" data-fit-cluster="${esc(fit.code)}" data-product-enabled="${productEnabled ? "true" : "false"}">
				<div class="f5-fit-row" data-fit="${esc(fit.code)}">
					<label class="f5-switch" title="Доступність ${esc(fit.label)}"><input type="checkbox" data-f="fit_enabled" ${rule.is_enabled ? "checked" : ""}${productEnabled ? "" : " disabled"}><i></i></label>
					<strong>${esc(fit.label)}</strong>
					<span class="f5-source-badge">${productEnabled ? "Для цього кольору" : "Вимкнено в товарі"}</span>
					<label class="f5-field f5-fit-reason" data-role="fit-reason"${productEnabled && !rule.is_enabled ? "" : " hidden"}><span>Причина для покупця</span><input class="f5-input" data-f="fit_reason" value="${esc(rule.reason)}" placeholder="${esc(defaultFitReason(fit.code))}" aria-label="Причина недоступності ${esc(fit.label)}"${productEnabled && !rule.is_enabled ? "" : " disabled"}></label>
				</div>
				<div class="f5-fit-global-note" data-role="fit-global-note"${productEnabled ? " hidden" : ""}><svg class="f5-icon"><use href="#f5-i-warning"/></svg><span>Спочатку увімкніть цю посадку в розділі «Посадки й розміри» товару.</span></div>
				<div class="f5-source-row"><label class="f5-field"><span>Розмірна сітка ${esc(fit.label)}</span><select class="f5-input" data-f="variant_size_grid" data-option-key="${esc(key)}"${effectiveEnabled ? "" : " disabled"}>${selectOptions(dict.size_grids || [], assignment.size_grid_id, "Успадкувати спільну сітку")}</select><small>${assignment.size_grid_id ? "Окрема сітка цього кольору" : "Порожньо — використовується сітка посадки або товару"}</small></label><span class="f5-source-badge">${assignment.size_grid_id ? "Override кольору" : "Успадковано"}</span></div>
				<div data-role="size-grid">${sizeGridHtml(variant, fit.code)}</div>
				${!productEnabled || rule.is_enabled || rule.reason ? "" : `<div class="f5-fallback-preview" data-role="fit-fallback">${flameHtml()}<span>Порожньо — покупець побачить: “${esc(defaultFitReason(fit.code))}”</span></div>`}
			</section>`;
		}).join("");
	}

	function combinationForFit(variant, fitCode) {
		return (variant.combinations || []).find((item) => {
			const values = item.option_values || {};
			return values.fit === fitCode || values.fit_code === fitCode || item.combination_key === `fit=${fitCode}`;
		}) || null;
	}

	function combinationWorkspaceHtml(variant) {
		const color = variant.details || {};
		return `<div class="f5-combination-section">
			<div class="f5-variant-pane__head"><div><h3>Контент за посадкою</h3><p>За замовчуванням класика й оверсайз успадковують дані кольору. Власний режим створює точний профіль «колір × посадка».</p></div><span class="f5-source-badge">Колір → посадка</span></div>
			<div class="f5-combination-list">${state.fits.map((fit) => {
				const rule = (variant.fits || []).find((item) => item.fit_code === fit.code) || { is_enabled: true };
				const fitEnabled = fit.is_enabled !== false && rule.is_enabled !== false;
				const profile = combinationForFit(variant, fit.code);
				const custom = !!profile;
				const content = (profile && profile.content) || {};
				const disabled = !fitEnabled || !custom;
				return `<section class="f5-combination${custom ? " is-custom" : " is-inherited"}${fitEnabled ? "" : " is-unavailable"}" data-combination-fit="${esc(fit.code)}"${profile && profile.id ? ` data-combination-id="${profile.id}"` : ""} data-youtube-url="${esc((profile && profile.youtube_url) || "")}">
					<header class="f5-combination__head"><span class="f5-combination__fit"><strong>${esc(fit.label)}</strong><small>${fitEnabled ? "Окремий контент лише коли він справді відрізняється" : "Посадка зараз недоступна для цього кольору"}</small></span><label class="f5-inherit-toggle"><input type="checkbox" data-f="combo_custom" ${custom ? "checked" : ""}${fitEnabled ? "" : " disabled"}><span aria-hidden="true"><i>Успадкувати</i><i>Власні</i></span><b data-role="combo-state">${custom ? "Власні" : "Успадковано"}</b></label></header>
					<fieldset data-role="combination-fields"${disabled ? " disabled" : ""}>
						<div class="f5-row"><label class="f5-field"><span>Назва для ${esc(fit.label)}</span><input class="f5-input" data-c="display_name" value="${esc(content.display_name || "")}" placeholder="${esc(color.display_name || variant.color.name || "Назва кольору")}"></label><label class="f5-field"><span>Надбавка посадки</span><input type="number" class="f5-input" data-c="price_delta" value="${profile && profile.price_delta != null ? profile.price_delta : ""}" placeholder="Успадкувати"></label></div>
						<label class="f5-field"><span>Маркетинговий опис</span><textarea class="f5-input" rows="3" data-c="marketing_text" placeholder="Успадкувати опис кольору">${esc(content.marketing_text || "")}</textarea></label>
						<label class="f5-field"><span>Причина надбавки</span><input class="f5-input" data-c="price_delta_reason" value="${esc((profile && profile.price_delta_reason) || "")}" placeholder="${esc(color.price_delta_reason || DEFAULTS.priceReason)}"></label>
						<div class="f5-row"><label class="f5-field"><span>SEO Title</span><input class="f5-input" data-c="seo_title" maxlength="180" value="${esc(content.seo_title || "")}" placeholder="Успадкувати SEO кольору"></label><label class="f5-field"><span>SEO Keywords</span><input class="f5-input" data-c="seo_keywords" maxlength="300" value="${esc(content.seo_keywords || "")}" placeholder="Успадкувати ключі кольору"></label></div>
						<label class="f5-field"><span>SEO Description</span><textarea class="f5-input" rows="3" maxlength="320" data-c="seo_description" placeholder="Успадкувати SEO-опис кольору">${esc(content.seo_description || "")}</textarea></label>
					</fieldset>
				</section>`;
			}).join("")}</div>
		</div>`;
	}

	function syncCombinationAvailability(row, fitEnabled) {
		if (!row) return;
		const toggle = $("[data-f=combo_custom]", row);
		const fields = $("[data-role=combination-fields]", row);
		const stateLabel = $("[data-role=combo-state]", row);
		const custom = !!(toggle && toggle.checked);
		if (toggle) toggle.disabled = !fitEnabled;
		if (fields) fields.disabled = !fitEnabled || !custom;
		row.classList.toggle("is-unavailable", !fitEnabled);
		row.classList.toggle("is-custom", custom);
		row.classList.toggle("is-inherited", !custom);
		if (stateLabel) stateLabel.textContent = !fitEnabled ? "Недоступна" : (custom ? "Власні" : "Успадковано");
	}

	function variantHtml(variant, index) {
		const d = variant.details || {};
		const selected = index === state.selectedVariantIndex;
		const basePrice = variant.price_override != null ? variant.price_override : Number($("#f-price").value || (state.product && state.product.price) || 0);
		const finalPrice = Number(basePrice) + Number(d.price_delta || 0);
		const previewImage = ((variant.images || [])[0] || {}).url || (state.product && state.product.main_image_url) || "";
		const chips = [
			variant.is_default ? '<span class="f5-chip f5-chip--default">головний на вітрині</span>' : "",
			variant.color.is_thermo ? `<span class="f5-chip f5-chip--thermo">${flameHtml()} термо</span>` : "",
			`<span class="f5-chip">${(variant.images || []).length} фото</span>`,
			d.price_delta ? `<span class="f5-chip">${d.price_delta > 0 ? "+" : ""}${d.price_delta} грн</span>` : "",
		].join("");
		const uploadBlock = variant.id
			? `<div class="f5-dropzone" data-role="variant-drop"><svg class="f5-icon"><use href="#f5-i-upload"/></svg><span><strong>Додайте фото цього кольору</strong><small>Drag & drop · нові кадри додаються в кінець</small></span><button type="button" class="f5-btn f5-btn--ghost" data-act="variant-upload-btn">Обрати файли</button><input type="file" accept="image/*" multiple hidden data-role="variant-upload"></div><div class="f5-gallery" data-role="variant-gallery">${(variant.images || []).map((img, i) => thumbHtml(img, "variant", variant.id, i)).join("") || '<p class="f5-hint">Фото цього кольору ще немає.</p>'}</div>`
			: '<div class="f5-fallback-preview"><svg class="f5-icon"><use href="#f5-i-warning"/></svg><span>Збережіть колір один раз — після цього відкриється завантаження та оптимізація фото.</span></div>';
		const variantFaqs = (variant.faqs || []).map(faqHtml).join("");
		return `<article id="f5-variant-panel-${index}" class="f5-variant${selected ? " is-selected" : ""}" data-index="${index}" role="tabpanel" aria-labelledby="f5-variant-tab-${index}"${selected ? "" : " hidden"}${variant.id ? ` data-id="${variant.id}"` : ""}>
			<header class="f5-variant__head">
				${dotHtml(variant.color, 42)}
				<span class="f5-variant__identity"><span class="f5-variant__name">${esc(d.display_name || variant.color.name || "Новий колір")}</span><span class="f5-variant__meta">${chips}</span></span>
				<span class="f5-variant__spacer"></span>
				<button type="button" class="f5-btn f5-btn--ghost f5-btn--small f5-variant-move" data-act="variant-up" title="Перемістити вище" aria-label="Перемістити варіант вище">↑</button>
				<button type="button" class="f5-btn f5-btn--ghost f5-btn--small f5-variant-move" data-act="variant-down" title="Перемістити нижче" aria-label="Перемістити варіант нижче">↓</button>
			</header>
			<div class="f5-variant__body">
				<nav class="f5-variant-subnav" role="tablist" aria-label="Налаштування ${esc(variant.color.name || "кольору")}">${[["overview","Огляд"],["content","Контент"],["seo","SEO"],["photos","Фото"],["fits","Посадки й розміри"],["faq","FAQ"]].map((tab, tabIndex) => `<button type="button" id="f5-variant-${index}-tab-${tab[0]}" class="f5-variant-subtab${tabIndex === 0 ? " is-active" : ""}" data-variant-pane="${tab[0]}" role="tab" aria-controls="f5-variant-${index}-pane-${tab[0]}" aria-selected="${tabIndex === 0 ? "true" : "false"}" tabindex="${tabIndex === 0 ? "0" : "-1"}">${tab[1]}</button>`).join("")}</nav>
				<section id="f5-variant-${index}-pane-overview" class="f5-variant-pane is-active" data-pane="overview" role="tabpanel" aria-labelledby="f5-variant-${index}-tab-overview"><div class="f5-overview-grid"><div class="f5-store-card"><div class="f5-store-card__media">${previewImage ? `<img src="${esc(previewImage)}" alt="">` : '<span class="f5-store-card__placeholder">Фото варіанта з’явиться тут</span>'}${variant.color.is_thermo ? `<span class="f5-store-card__thermo">${flameHtml()} Термотканина</span>` : ""}</div><div class="f5-store-card__body"><h4 data-role="preview-title">${esc(d.display_name || ((state.product && state.product.title) || "Назва товару") + " · " + (variant.color.name || "колір"))}</h4><div class="f5-store-card__price"><strong data-role="preview-price">${finalPrice} грн</strong>${d.price_delta ? `<span data-role="preview-delta">+${d.price_delta} за матеріал</span>` : ""}</div><div class="f5-store-card__colors">${dotHtml(variant.color, 18)}<span>Так покупець розпізнає варіант</span></div></div></div><div class="f5-overview-stack"><div class="f5-merch-block"><div class="f5-merch-block__head"><strong>Ціна цього кольору</strong><span class="f5-source-badge">Результат для вітрини</span></div><div class="f5-price-equation"><label class="f5-field"><span>База / override</span><input type="number" min="0" class="f5-input" data-f="price_override" value="${variant.price_override != null ? variant.price_override : ""}" placeholder="${basePrice}"></label><span>+</span><label class="f5-field"><span>Надбавка</span><input type="number" class="f5-input" data-f="price_delta" value="${d.price_delta || 0}"></label><span>=</span><output class="f5-effective-price" data-role="effective-price">${finalPrice} грн</output></div><label class="f5-field"><span>Чому дорожче — бачить покупець</span><input class="f5-input" data-f="price_delta_reason" value="${esc(d.price_delta_reason)}" placeholder="${esc(DEFAULTS.priceReason)}"></label><div class="f5-fallback-preview" data-role="price-fallback"${d.price_delta && !d.price_delta_reason ? "" : " hidden"}><svg class="f5-icon"><use href="#f5-i-warning"/></svg><span>Порожньо — автоматично буде використано: “${esc(DEFAULTS.priceReason)}”</span></div></div><div class="f5-merch-block"><div class="f5-merch-block__head"><strong>Ідентифікація</strong><span class="f5-source-badge">Варіант</span></div><div class="f5-row"><label class="f5-field"><span>SKU кольору</span><input class="f5-input" data-f="sku" value="${esc(variant.sku)}" placeholder="Напр.: CRC-THERMO-GREEN"></label><label class="f5-check f5-check--tile"><input type="checkbox" data-f="is_default" ${variant.is_default ? "checked" : ""}><span><strong>Головний колір</strong><small>Перший на вітрині</small></span></label></div></div><div class="f5-merch-block"><div class="f5-merch-block__head"><strong>Заготовка зі складу</strong><span class="f5-source-badge">На посадку цього кольору</span></div>${storageBlankHtml(variant)}</div></div></div></section>
				<section id="f5-variant-${index}-pane-content" class="f5-variant-pane" data-pane="content" role="tabpanel" aria-labelledby="f5-variant-${index}-tab-content" hidden>${colorPickerHtml(variant)}<div class="f5-subsection"><div class="f5-source-row"><strong>Текст для цього кольору</strong><span class="f5-source-badge">Порожньо = з товару</span></div><label class="f5-field"><span>Назва на вітрині</span><input class="f5-input" data-f="display_name" value="${esc(d.display_name)}" placeholder="${esc(((state.product && state.product.title) || "Назва товару") + " · " + (variant.color.name || "колір"))}"></label><label class="f5-field"><span>Маркетинговий опис кольору</span><textarea class="f5-input" rows="5" data-f="marketing_html" placeholder="Порожньо — використовується спільний опис товару">${esc(d.marketing_html)}</textarea></label><label class="f5-field"><span>YouTube для кольору</span><input class="f5-input" data-f="youtube_url" value="${esc(d.youtube_url)}" placeholder="Порожньо — спільне відео товару"></label></div></section>
				<section id="f5-variant-${index}-pane-seo" class="f5-variant-pane" data-pane="seo" role="tabpanel" aria-labelledby="f5-variant-${index}-tab-seo" hidden><div class="f5-variant-pane__head"><div><h3>SEO саме цього кольору</h3><p>Цей блок головний для кольорової URL. Порожні поля успадковуються з товару.</p></div><span class="f5-source-badge">Колір → товар</span></div><div class="f5-row"><label class="f5-field"><span>SEO Title <small data-role="variant-seo-title-count">${(d.seo_title || "").length}/60</small></span><input class="f5-input" data-f="seo_title" maxlength="180" value="${esc(d.seo_title)}"></label><label class="f5-field"><span>SEO Keywords</span><input class="f5-input" data-f="seo_keywords" maxlength="300" value="${esc(d.seo_keywords)}"></label></div><label class="f5-field"><span>SEO Description <small data-role="variant-seo-desc-count">${(d.seo_description || "").length}/160</small></span><textarea class="f5-input" rows="3" maxlength="320" data-f="seo_description">${esc(d.seo_description)}</textarea></label><div class="f5-google-preview" data-role="variant-google"><span>twocomms.shop › product › ${esc((state.product && state.product.slug) || "slug")}</span><strong>${esc(d.seo_title || d.display_name || (state.product && state.product.title) || "Назва кольорового варіанта")}</strong><p>${esc(d.seo_description || (state.product && state.product.seo_description) || "Опис буде успадковано з основної сторінки товару.")}</p></div>${combinationWorkspaceHtml(variant)}</section>
				<section id="f5-variant-${index}-pane-photos" class="f5-variant-pane" data-pane="photos" role="tabpanel" aria-labelledby="f5-variant-${index}-tab-photos" hidden><div class="f5-variant-pane__head"><div><h3>Галерея кольору</h3><p>Порядок = порядок у каруселі. «Обкладинка» робить обране фото канонічним для товару.</p></div><span class="f5-source-badge">${(variant.images || []).length} фото</span></div>${uploadBlock}</section>
				<section id="f5-variant-${index}-pane-fits" class="f5-variant-pane" data-pane="fits" role="tabpanel" aria-labelledby="f5-variant-${index}-tab-fits" hidden><div class="f5-variant-pane__head"><div><h3>Посадки, сітки та доступні розміри</h3><p>Вимкнена посадка деактивує її сітку й розміри. Окрема сітка перевизначає спільну лише для цього кольору й посадки; порожньо — успадкувати.</p></div><span class="f5-source-badge">Цей колір</span></div>${fitWorkspaceHtml(variant)}</section>
				<section id="f5-variant-${index}-pane-faq" class="f5-variant-pane" data-pane="faq" role="tabpanel" aria-labelledby="f5-variant-${index}-tab-faq" hidden><div class="f5-variant-pane__head"><div><h3>FAQ кольору</h3><p>Відповіді, що стосуються лише матеріалу або відтінку.</p></div><button type="button" class="f5-btn f5-btn--ghost" data-act="variant-faq-add">Додати питання</button></div><div data-role="variant-faqs">${variantFaqs || '<p class="f5-hint">Спеціальних питань для кольору ще немає.</p>'}</div></section>
				<footer class="f5-variant-footer"><button type="button" class="f5-btn f5-btn--danger" data-act="variant-delete">Видалити колір</button><button type="button" class="f5-btn f5-btn--primary" data-act="variant-save"><svg class="f5-icon"><use href="#f5-i-save"/></svg>Зберегти колір</button></footer>
			</div>
		</article>`;
	}

	function renderVariants() {
		const box = $("#f-variants");
		if (state.selectedVariantIndex >= state.variants.length) state.selectedVariantIndex = Math.max(0, state.variants.length - 1);
		const rail = $("#f-variant-rail");
		if (rail) rail.innerHTML = state.variants.length ? state.variants.map(variantRailHtml).join("") : '<p class="f5-hint">Додайте перший колір — тут з’явиться його preview, ціна та стан готовності.</p>';
		box.innerHTML = state.variants.length
			? state.variants.map(variantHtml).join("")
			: '<div class="f5-card"><h2 class="f5-card__title">Ще немає варіантів</h2><p class="f5-hint">Додайте перший колір. Він отримає власну ціну, SEO, термо-стан, посадки, розміри та фото.</p></div>';
		renderStock();
		updateReadiness();
	}

	function collectVariantData(card, variant) {
		const val = (sel) => { const el = $(sel, card); return el ? el.value : ""; };
		const checked = (sel) => { const el = $(sel, card); return el ? el.checked : false; };
		const sizes = [];
		$$("[data-role=size-grid] .f5-size-cell", card).forEach((cell) => {
			sizes.push({
				fit_code: cell.dataset.fit || "",
				size: cell.dataset.size,
				is_enabled: !cell.classList.contains("is-off"),
				stock: intOrNull($("[data-f=stock]", cell).value),
				note: "",
			});
		});
		const fits = $$(".f5-fit-row[data-fit]", card).map((row) => {
			const enabled = $("[data-f=fit_enabled]", row).checked;
			return {
				fit_code: row.dataset.fit,
				is_enabled: enabled,
					reason: $("[data-f=fit_reason]", row).value || (enabled ? "" : defaultFitReason(row.dataset.fit)),
			};
		});
		const faqs = $$("[data-role=variant-faqs] .f5-faq", card).map((node) => ({
			question_uk: $("[data-f=question_uk]", node).value,
			question_ru: $("[data-f=question_ru]", node).value,
			question_en: $("[data-f=question_en]", node).value,
			answer_uk: $("[data-f=answer_uk]", node).value,
			answer_ru: $("[data-f=answer_ru]", node).value,
			answer_en: $("[data-f=answer_en]", node).value,
			is_active: $("[data-f=is_active]", node).checked,
		}));
		const sizeGrids = $$("[data-f=variant_size_grid]", card).map((select) => ({
			option_key: select.dataset.optionKey || "",
			size_grid_id: intOrNull(select.value),
		})).filter((item) => item.size_grid_id);
		const blankLinks = $$("[data-role=warehouse-blank]", card).map((row) => ({
			option_key: row.dataset.optionKey || "",
			storage_subcategory_id: intOrNull($("[data-f=storage_blank]", row).value),
			note: $("[data-f=storage_note]", row).value,
		})).filter((item) => item.storage_subcategory_id);
		const combinations = $$("[data-combination-fit]", card).filter((row) => {
			const custom = $("[data-f=combo_custom]", row);
			return custom && custom.checked;
		}).map((row) => {
			const value = (name) => { const input = $(`[data-c="${name}"]`, row); return input ? input.value : ""; };
			const combinationDelta = intOrNull(value("price_delta"));
			return {
				id: intOrNull(row.dataset.combinationId),
				option_values: { fit: row.dataset.combinationFit },
				is_active: true,
				price_delta: combinationDelta,
				price_delta_reason: value("price_delta_reason") || (combinationDelta ? (val("[data-f=price_delta_reason]") || DEFAULTS.priceReason) : ""),
				youtube_url: row.dataset.youtubeUrl || "",
				content: {
					display_name: value("display_name"),
					marketing_text: value("marketing_text"),
					seo_title: value("seo_title"),
					seo_description: value("seo_description"),
					seo_keywords: value("seo_keywords"),
				},
			};
		});
		const thermoEnabled = checked("[data-f=is_thermo]");
		const priceDelta = intOrNull(val("[data-f=price_delta]")) || 0;
		return {
			id: variant.id,
			product_id: state.product.id,
			color: {
				id: variant.color.id,
				name: val("[data-f=color_name]"),
				primary_hex: val("[data-f=color_hex]").trim(),
				secondary_hex: val("[data-f=color_hex2]").trim(),
				is_thermo: thermoEnabled,
				thermo_note: val("[data-f=thermo_note]") || (thermoEnabled ? DEFAULTS.thermoNote : ""),
				description: val("[data-f=color_description]"),
			},
			sku: val("[data-f=sku]"),
			price_override: intOrNull(val("[data-f=price_override]")),
			is_default: checked("[data-f=is_default]"),
			details: {
				display_name: val("[data-f=display_name]"),
				price_delta: priceDelta,
				price_delta_reason: val("[data-f=price_delta_reason]") || (priceDelta ? DEFAULTS.priceReason : ""),
				marketing_html: val("[data-f=marketing_html]"),
				youtube_url: val("[data-f=youtube_url]"),
				seo_title: val("[data-f=seo_title]"),
				seo_description: val("[data-f=seo_description]"),
				seo_keywords: val("[data-f=seo_keywords]"),
			},
			fits: fits,
			sizes: sizes,
			size_grids: sizeGrids,
			blank_links: blankLinks,
			combinations: combinations,
			faqs: faqs,
		};
	}
	async function saveVariant(card, index) {
		await ensureProduct();
		const variant = state.variants[index];
		const data = collectVariantData(card, variant);
		if (!data.color.id && !/^#?[0-9a-fA-F]{6}$/.test(data.color.primary_hex)) {
			toast("Вкажіть коректний HEX кольору (#RRGGBB) або оберіть з бібліотеки", true);
			return;
		}
		try {
			const resp = await postJSON(urls.variant_save, data);
			resp.variant._open = true;
			state.variants[index] = resp.variant;
			if (resp.variant.is_default) {
				state.variants.forEach((v, i) => { if (i !== index) v.is_default = false; });
			}
			refreshColorLibrary(resp.variant.color);
			renderVariants();
			toast("Колір збережено");
		} catch (err) {
			toast("Помилка збереження кольору: " + err.message, true);
		}
	}

	function refreshColorLibrary(color) {
		if (!color || !color.id) return;
		dict.colors = dict.colors || [];
		const existing = dict.colors.find((c) => c.id === color.id);
		if (existing) Object.assign(existing, color);
		else dict.colors.push(Object.assign({}, color));
	}

	async function deleteVariant(index) {
		const variant = state.variants[index];
		if (variant.id) {
			if (!confirm("Видалити колір разом із його картинками та правилами?")) return;
			try {
				await postJSON(urls.variant_delete, { product_id: state.product.id, id: variant.id });
			} catch (err) {
				toast("Помилка видалення: " + err.message, true);
				return;
			}
		}
		state.variants.splice(index, 1);
		if (variant.is_default && state.variants.length) state.variants[0].is_default = true;
		state.selectedVariantIndex = Math.min(state.selectedVariantIndex, Math.max(0, state.variants.length - 1));
		renderVariants();
		toast("Колір видалено");
	}

	async function moveVariant(index, delta) {
		const target = index + delta;
		if (target < 0 || target >= state.variants.length) return;
		const item = state.variants.splice(index, 1)[0];
		state.variants.splice(target, 0, item);
		state.selectedVariantIndex = target;
		renderVariants();
		const ids = state.variants.filter((v) => v.id).map((v) => v.id);
		if (state.product && ids.length > 1) {
			try {
				await postJSON(urls.variant_reorder, { product_id: state.product.id, ids: ids });
			} catch (err) {
				toast("Помилка порядку: " + err.message, true);
			}
		}
	}

	function updateDotPreview(card, variant) {
		const color = {
			primary_hex: $("[data-f=color_hex]", card).value.trim() || "#888888",
			secondary_hex: $("[data-f=color_hex2]", card).value.trim(),
			is_thermo: $("[data-f=is_thermo]", card).checked,
			name: $("[data-f=color_name]", card).value,
		};
		const preview = $("[data-role=dot-preview]", card);
		if (preview) preview.innerHTML = dotHtml(color, 112);
		const swatchName = $("[data-role=swatch-name]", card);
		if (swatchName) swatchName.textContent = color.name || "Новий колір";
		const headerDot = $(".f5-variant__head > .f5-dot", card);
		if (headerDot) headerDot.outerHTML = dotHtml(color, 42);
		const rail = $(`[data-variant-select="${card.dataset.index}"]`);
		const railDot = rail && $(".f5-rail-media .f5-dot", rail);
		if (railDot) railDot.outerHTML = dotHtml(color, 18);
		if (rail) {
			const railThermo = $(".f5-rail-thermo", rail);
			if (color.is_thermo && !railThermo) rail.insertAdjacentHTML("beforeend", `<span class="f5-rail-thermo" title="Термохромна тканина">${flameHtml()}</span>`);
			if (!color.is_thermo && railThermo) railThermo.remove();
		}
		const storeMedia = $(".f5-store-card__media", card);
		const storeThermo = storeMedia && $(".f5-store-card__thermo", storeMedia);
		if (storeMedia && color.is_thermo && !storeThermo) storeMedia.insertAdjacentHTML("beforeend", `<span class="f5-store-card__thermo">${flameHtml()} Термотканина</span>`);
		if (!color.is_thermo && storeThermo) storeThermo.remove();
		const meta = $(".f5-variant__meta", card);
		const thermoChip = meta && $(".f5-chip--thermo", meta);
		if (meta && color.is_thermo && !thermoChip) meta.insertAdjacentHTML("beforeend", `<span class="f5-chip f5-chip--thermo">${flameHtml()} термо</span>`);
		if (!color.is_thermo && thermoChip) thermoChip.remove();
		if (variant) {
			variant.color.primary_hex = color.primary_hex;
			variant.color.secondary_hex = color.secondary_hex;
			variant.color.is_thermo = color.is_thermo;
			variant.color.name = color.name;
		}
		refreshVariantPreview(card, variant);
	}

	function refreshVariantPreview(card, variant) {
		if (!card || !variant) return;
		const read = (name) => { const el = $(`[data-f="${name}"]`, card); return el ? el.value : ""; };
		const override = intOrNull(read("price_override"));
		const base = override != null ? override : Number($("#f-price").value || (state.product && state.product.price) || 0);
		const delta = intOrNull(read("price_delta")) || 0;
		const finalPrice = Math.max(0, base + delta);
		const title = read("display_name") || ((state.product && state.product.title) || $("#f-title").value || "Назва товару") + " · " + (read("color_name") || variant.color.name || "колір");
		const priceOut = $("[data-role=effective-price]", card);
		const previewPrice = $("[data-role=preview-price]", card);
		const previewTitle = $("[data-role=preview-title]", card);
		if (priceOut) priceOut.textContent = finalPrice + " грн";
		if (previewPrice) previewPrice.textContent = finalPrice + " грн";
		if (previewTitle) previewTitle.textContent = title;
		const fallback = $("[data-role=price-fallback]", card);
		if (fallback) fallback.hidden = !(delta && !read("price_delta_reason").trim());
		const titleCount = $("[data-role=variant-seo-title-count]", card);
		const descCount = $("[data-role=variant-seo-desc-count]", card);
		if (titleCount) titleCount.textContent = read("seo_title").length + "/60";
		if (descCount) descCount.textContent = read("seo_description").length + "/160";
		const google = $("[data-role=variant-google]", card);
		if (google) {
			$("strong", google).textContent = read("seo_title") || title;
			$("p", google).textContent = read("seo_description") || (state.product && state.product.seo_description) || "Опис буде успадковано з основної сторінки товару.";
		}
		const rail = $(`[data-variant-select="${card.dataset.index}"]`);
		if (rail) {
			const railTitle = $(".f5-rail-title", rail);
			const railPrice = $(".f5-rail-price", rail);
			if (railTitle) railTitle.textContent = title;
			if (railPrice) railPrice.textContent = finalPrice + " грн";
		}
		variant.price_override = override;
		variant.details = Object.assign({}, variant.details, { display_name: read("display_name"), price_delta: delta, price_delta_reason: read("price_delta_reason"), seo_title: read("seo_title"), seo_description: read("seo_description") });
	}

	$("#f-variant-rail").addEventListener("click", (e) => {
		const item = e.target.closest("[data-variant-select]");
		if (!item) return;
		state.selectedVariantIndex = parseInt(item.dataset.variantSelect, 10);
		$$('.f5-rail-item').forEach((node) => {
			const active = node === item;
			node.classList.toggle("is-active", active);
			node.setAttribute("aria-selected", active ? "true" : "false");
			node.tabIndex = active ? 0 : -1;
		});
		$$('.f5-variant').forEach((node) => {
			const active = parseInt(node.dataset.index, 10) === state.selectedVariantIndex;
			node.classList.toggle("is-selected", active);
			node.hidden = !active;
		});
		const workspace = $("#f-variants");
		if (workspace && window.matchMedia("(max-width: 760px)").matches) workspace.scrollIntoView({ behavior: "smooth", block: "start" });
	});

	$("#f-variant-rail").addEventListener("keydown", (e) => {
		if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
		const tabs = $$('.f5-rail-item', e.currentTarget);
		const current = tabs.indexOf(document.activeElement);
		if (current < 0 || !tabs.length) return;
		e.preventDefault();
		let next = current;
		if (e.key === "Home") next = 0;
		else if (e.key === "End") next = tabs.length - 1;
		else next = (current + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
		tabs[next].focus();
		tabs[next].click();
	});

	/* події всередині списку кольорів */
	$("#f-variants").addEventListener("click", (e) => {
		const card = e.target.closest(".f5-variant");
		if (!card) return;
		const paneButton = e.target.closest("[data-variant-pane]");
		if (paneButton) {
			$$('.f5-variant-subtab', card).forEach((node) => { const active = node === paneButton; node.classList.toggle("is-active", active); node.setAttribute("aria-selected", active ? "true" : "false"); node.tabIndex = active ? 0 : -1; });
			$$('.f5-variant-pane', card).forEach((node) => { const active = node.dataset.pane === paneButton.dataset.variantPane; node.classList.toggle("is-active", active); node.hidden = !active; });
			return;
		}
		const index = parseInt(card.dataset.index, 10);
		const variant = state.variants[index];
		const actEl = e.target.closest("[data-act]");
		if (!actEl) return;
		const act = actEl.dataset.act;
		if (act === "pick-color") {
			const picked = JSON.parse(actEl.dataset.color);
			variant.color = Object.assign({}, picked);
			$("[data-f=color_name]", card).value = picked.name || "";
			$("[data-f=color_hex]", card).value = picked.primary_hex || "";
			const pickInput = $("[data-f=color_pick]", card);
			if (pickInput && /^#[0-9a-fA-F]{6}$/.test(picked.primary_hex || "")) pickInput.value = picked.primary_hex;
			$("[data-f=color_hex2]", card).value = picked.secondary_hex || "";
			$("[data-f=is_thermo]", card).checked = !!picked.is_thermo;
			$("[data-f=thermo_note]", card).value = picked.thermo_note || "";
			$("[data-f=color_description]", card).value = picked.description || "";
			$$(".f5-color-option", card).forEach((el) => el.classList.toggle("is-selected", el === actEl));
			updateDotPreview(card, variant);
			variant.color.id = picked.id;
			setDirty(true);
			return;
		}
		if (act === "variant-up") { moveVariant(index, -1); return; }
		if (act === "variant-down") { moveVariant(index, 1); return; }
		if (act === "variant-save") { saveVariant(card, index); return; }
		if (act === "variant-delete") { deleteVariant(index); return; }
		if (act === "variant-upload-btn") {
			const inp = $("[data-role=variant-upload]", card);
			if (inp) inp.click();
			return;
		}
		if (act === "variant-faq-add") {
			$("[data-role=variant-faqs]", card).insertAdjacentHTML("beforeend", faqHtml({ is_active: true }));
			setDirty(true);
			return;
		}
	});

	$("#f-variants").addEventListener("keydown", (e) => {
		const current = e.target.closest && e.target.closest(".f5-variant-subtab");
		if (!current || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
		const nav = current.closest(".f5-variant-subnav");
		const tabs = $$('.f5-variant-subtab', nav);
		const index = tabs.indexOf(current);
		if (index < 0) return;
		e.preventDefault();
		let next = index;
		if (e.key === "Home") next = 0;
		else if (e.key === "End") next = tabs.length - 1;
		else next = (index + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
		tabs[next].focus();
		tabs[next].click();
	});

	$("#f-variants").addEventListener("input", (e) => {
		const card = e.target.closest(".f5-variant");
		if (!card || !e.target.dataset.f) return;
		const variant = state.variants[parseInt(card.dataset.index, 10)];
		refreshVariantPreview(card, variant);
	});

	$("#f-variants").addEventListener("change", (e) => {
		const card = e.target.closest(".f5-variant");
		if (!card) return;
		const variant = state.variants[parseInt(card.dataset.index, 10)];
		if (e.target.matches("[data-role=variant-upload]")) {
			if (variant && variant.id) uploadImages("variant", variant.id, e.target.files);
			e.target.value = "";
			return;
		}
		const f = e.target.dataset.f;
		if (!f) return;
		if (f === "color_pick") {
			$("[data-f=color_hex]", card).value = e.target.value;
			if (variant) variant.color.id = null; // зміна HEX = інший/новий колір
			$$(".f5-color-option", card).forEach((el) => el.classList.remove("is-selected"));
			updateDotPreview(card, variant);
		} else if (f === "color_hex" || f === "color_hex2") {
			if (variant) variant.color.id = null;
			$$(".f5-color-option", card).forEach((el) => el.classList.remove("is-selected"));
			const hex = $("[data-f=color_hex]", card).value.trim();
			const pick = $("[data-f=color_pick]", card);
			if (pick && /^#[0-9a-fA-F]{6}$/.test(hex)) pick.value = hex;
			updateDotPreview(card, variant);
		} else if (f === "is_thermo") {
			updateDotPreview(card, variant);
			const fallback = $("[data-role=thermo-fallback]", card);
			if (fallback) fallback.textContent = $("[data-f=thermo_note]", card).value ? "Власний текст" : `Порожньо — автоматично: “${DEFAULTS.thermoNote}”`;
		} else if (f === "combo_custom") {
			const combination = e.target.closest("[data-combination-fit]");
			const fitRule = combination && $(`[data-fit-cluster="${combination.dataset.combinationFit}"]`, card);
			const fitToggle = fitRule && $("[data-f=fit_enabled]", fitRule);
			const fitEnabled = !!(fitToggle && !fitToggle.disabled && fitToggle.checked);
			syncCombinationAvailability(combination, fitEnabled);
		} else if (f === "fit_enabled") {
			const row = e.target.closest(".f5-fit-row");
			const cluster = e.target.closest("[data-fit-cluster]");
			const enabled = e.target.checked && (!cluster || cluster.dataset.productEnabled !== "false");
			if (cluster) {
				cluster.classList.toggle("is-disabled", !enabled);
				const grid = $("[data-f=variant_size_grid]", cluster);
				const reasonWrap = $("[data-role=fit-reason]", cluster);
				const reason = $("[data-f=fit_reason]", cluster);
				if (grid) grid.disabled = !enabled;
				if (reasonWrap) reasonWrap.hidden = enabled;
				if (reason) reason.disabled = enabled;
				$$(`.f5-size-cell[data-fit="${row.dataset.fit}"]`, cluster).forEach((cell) => {
					if (!enabled) cell.classList.add("is-off");
					const button = $("[data-act=size-toggle]", cell);
					const stock = $("[data-f=stock]", cell);
					if (button) { button.disabled = !enabled; button.setAttribute("aria-pressed", enabled && !cell.classList.contains("is-off") ? "true" : "false"); }
					if (stock) stock.disabled = !enabled;
				});
				syncCombinationAvailability($(`[data-combination-fit="${row.dataset.fit}"]`, card), enabled);
			}
		}
		refreshVariantPreview(card, variant);
	});

	$("#f-variants").addEventListener("dragover", (e) => {
		if (draggedThumb) return;
		const zone = e.target.closest && e.target.closest("[data-role=variant-drop]");
		if (!zone) return;
		e.preventDefault();
		zone.classList.add("is-over");
	});
	$("#f-variants").addEventListener("drop", (e) => {
		if (draggedThumb) return;
		const zone = e.target.closest && e.target.closest("[data-role=variant-drop]");
		if (!zone) return;
		e.preventDefault();
		zone.classList.remove("is-over");
		const card = zone.closest(".f5-variant");
		const variant = state.variants[parseInt(card.dataset.index, 10)];
		if (variant && variant.id) uploadImages("variant", variant.id, e.dataTransfer.files);
	});

	/* глобальні кліки: кнопки мініатюр, видалення FAQ, тогл розмірів */
	document.addEventListener("click", (e) => {
		const btn = e.target.closest && e.target.closest("[data-act]");
		if (!btn) return;
		const act = btn.dataset.act;
		if ((act === "cover" || act === "home" || act === "del") && btn.closest(".f5-thumb")) {
			handleThumbAction(btn);
		} else if (act === "faq-del") {
			const node = btn.closest(".f5-faq");
			if (node && confirm("Видалити це питання FAQ?")) { node.remove(); setDirty(true); }
		} else if (act === "size-toggle") {
			const cell = btn.closest(".f5-size-cell");
			cell.classList.toggle("is-off");
			btn.setAttribute("aria-pressed", cell.classList.contains("is-off") ? "false" : "true");
			const stockBlock = btn.closest("#f-stock [data-variant-index]");
			if (stockBlock) stockBlock.dataset.dirty = "true";
			setDirty(true);
		}
	});

	/* alt мініатюр — зберігається одразу */
	document.addEventListener("change", async (e) => {
		if (!e.target.matches || !e.target.matches(".f5-thumb__alt")) return;
		const fig = e.target.closest(".f5-thumb");
		try {
			await postJSON(urls.image_update, {
				product_id: state.product.id, kind: fig.dataset.kind,
				id: parseInt(fig.dataset.id, 10), alt: e.target.value,
			});
			const images = galleryImagesRef(fig.dataset.kind, fig.dataset.variant || null);
			const img = images.find((im) => String(im.id) === fig.dataset.id);
			if (img) img.alt = e.target.value;
			toast("Alt збережено");
		} catch (err) {
			toast("Помилка alt: " + err.message, true);
		}
	});

	/* ---------------- склад ---------------- */
	function renderStock() {
		const box = $("#f-stock");
		if (!box) return;
		if (!state.variants.length) {
			box.innerHTML = '<p class="f5-hint">Додайте кольори на вкладці «Кольори» — тут з’явиться складська матриця за розмірами.</p>';
			return;
		}
		box.innerHTML = state.variants.map((v, i) => `
			<div class="f5-subsection" data-variant-index="${i}">
				<div class="f5-card-head">
					<div>${dotHtml(v.color, 18)} <strong>${esc((v.details && v.details.display_name) || v.color.name || "Колір")}</strong></div>
					<button type="button" class="f5-btn f5-btn--ghost f5-btn--small" data-act="stock-save"${v.id ? "" : " disabled title='Спершу збережіть колір'"}>Зберегти доступність</button>
				</div>
				<div data-role="stock-grid">${state.fits.filter((fit) => fit.is_enabled).map((fit) => `<div class="f5-subsection"><div class="f5-source-row"><strong>${esc(fit.label)}</strong><span class="f5-source-badge">Всі кольори → цей колір</span></div>${sizeGridHtml(v, fit.code)}</div>`).join("")}</div>
			</div>`).join("");
	}

	function collectStockSizes(block) {
		return $$(".f5-size-cell", block).map((cell) => ({
			fit_code: cell.dataset.fit || "",
			size: cell.dataset.size,
			is_enabled: !cell.classList.contains("is-off"),
			stock: intOrNull($("[data-f=stock]", cell).value),
			note: "",
		}));
	}

	$("#f-stock").addEventListener("input", (e) => {
		const block = e.target.closest("[data-variant-index]");
		if (block) block.dataset.dirty = "true";
	});

	$("#f-stock").addEventListener("click", async (e) => {
		const btn = e.target.closest("[data-act=stock-save]");
		if (!btn) return;
		const block = btn.closest("[data-variant-index]");
		const index = parseInt(block.dataset.variantIndex, 10);
		const variant = state.variants[index];
		if (!variant || !variant.id) return;
		const sizes = collectStockSizes(block);
		try {
			const resp = await postJSON(urls.variant_save, {
				id: variant.id, product_id: state.product.id,
				color: { id: variant.color.id }, sizes: sizes,
			});
			resp.variant._open = variant._open;
			state.variants[index] = resp.variant;
			renderVariants();
			toast("Склад збережено");
		} catch (err) {
			toast("Помилка складу: " + err.message, true);
		}
	});

	/* ---------------- фіди («Селекція з фід») ---------------- */
	function feedRuleFor(feedId) {
		return state.feedRules[String(feedId)] || { is_included: undefined, custom_title: "", custom_description: "", image_rules: [] };
	}

	function feedCandidates() {
		const items = [{ key: "main", label: "Головна картинка", url: state.product.main_image_url, payload: { use_main_image: true } }];
		for (const img of (state.product.images || [])) {
			items.push({ key: "p" + img.id, label: "Галерея", url: img.url, payload: { product_image_id: img.id } });
		}
		for (const v of state.variants) {
			for (const img of (v.images || [])) {
				items.push({ key: "c" + img.id, label: v.color.name || "Колір", url: img.url, payload: { color_image_id: img.id } });
			}
		}
		return items;
	}

	function ruleKey(rule) {
		if (rule.use_main_image) return "main";
		if (rule.product_image_id) return "p" + rule.product_image_id;
		if (rule.color_image_id) return "c" + rule.color_image_id;
		return "";
	}

	function renderFeeds() {
		const box = $("#f-feeds");
		if (!state.product) {
			box.innerHTML = '<p class="f5-hint">Спершу збережіть товар — потім тут можна керувати його участю у фідах.</p>';
			return;
		}
		if (!state.feeds.length) {
			box.innerHTML = '<p class="f5-hint">Фідів ще немає. Створіть, наприклад, «Google Merchant» чи «Meta DS фід версія 1».</p>';
			return;
		}
		box.innerHTML = state.feeds.map((feed) => {
			const rule = feedRuleFor(feed.id);
			const included = rule.is_included !== undefined ? rule.is_included : !!feed.default_include;
			const allowedKeys = (rule.image_rules || []).filter((r) => r.is_allowed).map(ruleKey);
			const imgs = feedCandidates().map((c) => `
				<button type="button" class="f5-feed-img${allowedKeys.indexOf(c.key) >= 0 ? " is-allowed" : ""}" data-key="${c.key}" aria-pressed="${allowedKeys.indexOf(c.key) >= 0 ? "true" : "false"}" title="Дозволити або заборонити в цьому фіді">
					${c.url ? `<img src="${esc(c.url)}" alt="" loading="lazy">` : '<span class="f5-hint">немає</span>'}
					<span class="f5-feed-img__tag">${esc(c.label)}</span>
				</button>`).join("");
			const feedOnly = state.feedOnly.filter((im) => !im.feed_id || im.feed_id === feed.id).map((im) => `
				<figure class="f5-feed-img is-allowed" data-feed-only="${im.id}">
					<img src="${esc(im.url)}" alt="" loading="lazy">
					<figcaption class="f5-feed-img__tag">тільки фід</figcaption>
					<button type="button" class="f5-btn f5-btn--danger f5-btn--small" data-act="feed-only-del" title="Видалити">✕</button>
				</figure>`).join("");
			return `<article class="f5-feed is-open" data-feed="${feed.id}">
				<header class="f5-feed__head">
					<label class="f5-switch" title="Товар у цьому фіді"><input type="checkbox" data-f="is_included" ${included ? "checked" : ""}><i></i></label>
					<strong>${esc(feed.name)}</strong>
					<span class="f5-chip">${esc(feed.feed_type)}</span>
					<span class="f5-variant__spacer"></span>
					<button type="button" class="f5-btn f5-btn--primary f5-btn--small" data-act="feed-save"><svg class="f5-icon"><use href="#f5-i-save"/></svg>Зберегти фід</button>
				</header>
				<div class="f5-feed__body">
					<label class="f5-field"><span>Тайтл для фіда (порожньо = звичайний)</span><input class="f5-input" data-f="custom_title" value="${esc(rule.custom_title)}"></label>
					<label class="f5-field"><span>Опис для фіда</span><textarea class="f5-input" rows="2" data-f="custom_description">${esc(rule.custom_description)}</textarea></label>
					<div class="f5-hint">Клікайте картинки, дозволені в цьому фіді. Якщо не обрано жодної — фід бере картинки як звичайно.</div>
					<div class="f5-feed-imgs">${imgs}</div>
					<div class="f5-hint">Картинки ТІЛЬКИ для фіда (не показуються в картці товару):</div>
					<div class="f5-feed-imgs">${feedOnly}
						<button type="button" class="f5-btn f5-btn--ghost" data-act="feed-only-add">＋ додати</button>
						<input type="file" accept="image/*" multiple hidden data-role="feed-only-input">
					</div>
				</div>
			</article>`;
		}).join("");
	}

	async function loadFeeds() {
		if (!state.product) { renderFeeds(); return; }
		try {
			const resp = await getJSON(urls.feeds + "?product_id=" + state.product.id);
			state.feeds = resp.feeds || [];
			state.feedRules = resp.rules || {};
			state.feedOnly = resp.feed_only_images || [];
		} catch (err) { /* не критично */ }
		renderFeeds();
	}

	function collectFeedPayload(feedCard) {
		const feedId = parseInt(feedCard.dataset.feed, 10);
		const candidates = feedCandidates();
		const imageRules = [];
		$$(".f5-feed-img.is-allowed", feedCard).forEach((fig, i) => {
			if (fig.dataset.feedOnly) return;
			const cand = candidates.find((candidate) => candidate.key === fig.dataset.key);
			if (cand) imageRules.push(Object.assign({ is_allowed: true, order: i }, cand.payload));
		});
		return {
			product_id: state.product ? state.product.id : null,
			feed_id: feedId,
			is_included: $("[data-f=is_included]", feedCard).checked,
			custom_title: $("[data-f=custom_title]", feedCard).value,
			custom_description: $("[data-f=custom_description]", feedCard).value,
			image_rules: imageRules,
		};
	}

	async function persistFeedPayload(payload) {
		await postJSON(urls.feed_rule_save, payload);
		state.feedRules[String(payload.feed_id)] = {
			is_included: payload.is_included,
			custom_title: payload.custom_title,
			custom_description: payload.custom_description,
			image_rules: payload.image_rules,
		};
	}

	$("#f-feeds").addEventListener("click", async (e) => {
		const feedCard = e.target.closest(".f5-feed");
		if (!feedCard) return;
		const actEl = e.target.closest("[data-act]");
		if (!actEl) {
			const img = e.target.closest(".f5-feed-img");
			if (img && !img.dataset.feedOnly) {
				const allowed = img.classList.toggle("is-allowed");
				img.setAttribute("aria-pressed", allowed ? "true" : "false");
				feedCard.dataset.dirty = "true";
				setDirty(true);
			}
			return;
		}
		const act = actEl.dataset.act;
		if (act === "feed-only-add") {
			$("[data-role=feed-only-input]", feedCard).click();
			return;
		}
		if (act === "feed-only-del") {
			if (!confirm("Видалити фід-картинку?")) return;
			const fig = actEl.closest("[data-feed-only]");
			const id = parseInt(fig.dataset.feedOnly, 10);
			try {
				await postJSON(urls.feed_image_delete, { id: id });
				state.feedOnly = state.feedOnly.filter((im) => im.id !== id);
				renderFeeds();
				toast("Фід-картинку видалено");
			} catch (err) { toast("Помилка: " + err.message, true); }
			return;
		}
		if (act === "feed-save") {
			const payload = collectFeedPayload(feedCard);
			try {
				await persistFeedPayload(payload);
				feedCard.dataset.dirty = "false";
				toast("Налаштування фіда збережено");
			} catch (err) { toast("Помилка фіда: " + err.message, true); }
		}
	});

	$("#f-feeds").addEventListener("input", (e) => {
		const feedCard = e.target.closest(".f5-feed");
		if (feedCard) {
			feedCard.dataset.dirty = "true";
			setDirty(true);
		}
	});

	$("#f-feeds").addEventListener("change", (e) => {
		const feedCard = e.target.closest(".f5-feed");
		if (feedCard && !e.target.matches("[data-role=feed-only-input]")) {
			feedCard.dataset.dirty = "true";
			setDirty(true);
		}
	});

	$("#f-feeds").addEventListener("change", async (e) => {
		if (!e.target.matches("[data-role=feed-only-input]")) return;
		const feedCard = e.target.closest(".f5-feed");
		const fd = new FormData();
		fd.append("product_id", state.product.id);
		fd.append("feed_id", feedCard.dataset.feed);
		for (const f of e.target.files) fd.append("files", f);
		e.target.value = "";
		try {
			const resp = await postForm(urls.feed_image_upload, fd);
			state.feedOnly = state.feedOnly.concat(resp.images);
			renderFeeds();
			toast("Фід-картинки додано (append)");
		} catch (err) { toast("Помилка: " + err.message, true); }
	});

	$("#f-add-feed").addEventListener("click", async () => {
		const name = prompt("Назва фіда (напр.: Meta DS фід версія 1):");
		if (!name) return;
		let type = (prompt("Тип фіда: google_merchant / meta_ds / custom", "custom") || "custom").trim();
		if (["google_merchant", "meta_ds", "custom"].indexOf(type) < 0) type = "custom";
		try {
			const resp = await postJSON(urls.feed_create, { name: name, feed_type: type, default_include: false });
			state.feeds.push(resp.feed);
			renderFeeds();
			toast("Фід створено: " + resp.feed.name);
		} catch (err) { toast("Помилка: " + err.message, true); }
	});

	/* ---------------- головні зображення (файлами) ---------------- */
	function bindCover(btnSel, inputSel, imgSel, fileKey) {
		$(btnSel).addEventListener("click", () => $(inputSel).click());
		$(inputSel).addEventListener("change", (e) => {
			const file = e.target.files[0];
			if (!file) return;
			state.files[fileKey] = file;
			if (fileKey === "main_image" && state.product) {
				state.product.cover_source = { source_type: "upload", source_missing: false };
			}
			$(imgSel).src = URL.createObjectURL(file);
			if (fileKey === "home_card_image") delete $(imgSel).dataset.fallback;
			updateCoverState();
			setDirty(true);
			toast("Зображення буде завантажено разом із «Зберегти»");
		});
	}
	bindCover("#f-main-image-btn", "#f-main-image-file", "#f-main-image", "main_image");
	bindCover("#f-home-image-btn", "#f-home-image-file", "#f-home-image", "home_card_image");

	/* ---------------- галерея товару: dropzone ---------------- */
	const productDz = $("#f-product-dropzone");
	productDz.addEventListener("dragover", (e) => {
		if (draggedThumb) return;
		e.preventDefault();
		productDz.classList.add("is-over");
	});
	productDz.addEventListener("dragleave", () => productDz.classList.remove("is-over"));
	productDz.addEventListener("drop", (e) => {
		if (draggedThumb) return;
		e.preventDefault();
		productDz.classList.remove("is-over");
		uploadImages("product", null, e.dataTransfer.files);
	});
	$("#f-product-upload-btn").addEventListener("click", () => $("#f-product-upload").click());
	$("#f-product-upload").addEventListener("change", (e) => {
		uploadImages("product", null, e.target.files);
		e.target.value = "";
	});

	/* ---------------- вкладки, збереження, гарячі клавіші ---------------- */
	function activateTab(tabName, focusTab) {
		const tab = $(`.f5-tab[data-tab="${tabName}"]`);
		if (!tab) return;
		$$('.f5-tab').forEach((node) => {
			const active = node === tab;
			node.classList.toggle("is-active", active);
			node.setAttribute("aria-selected", active ? "true" : "false");
		});
		$$('.f5-panel').forEach((panel) => panel.classList.toggle("is-active", panel.dataset.panel === tabName));
		if (focusTab) tab.focus();
	}

	$("#f5-tabs").addEventListener("click", (e) => {
		const tab = e.target.closest(".f5-tab");
		if (!tab) return;
		activateTab(tab.dataset.tab, false);
	});
	$("#f5-tabs").addEventListener("keydown", (e) => {
		if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
		const tabs = $$(".f5-tab");
		const index = tabs.indexOf(document.activeElement);
		if (index < 0) return;
		e.preventDefault();
		const delta = e.key === "ArrowRight" ? 1 : -1;
		const next = tabs[(index + delta + tabs.length) % tabs.length];
		next.focus();
		next.click();
	});

	$("#f5-save").addEventListener("click", () => { saveAll().catch(() => {}); });
	$("#f5-mobile-save").addEventListener("click", () => { saveAll().catch(() => {}); });
	$("#f5-readiness-issues").addEventListener("click", (e) => {
		const target = e.target.closest("[data-readiness-tab]");
		if (target) activateTab(target.dataset.readinessTab, true);
	});
	document.addEventListener("click", (e) => {
		const action = e.target.closest('[data-act="show-variant-photos"]');
		if (!action) return;
		activateTab("colors", true);
		const selected = $(`.f5-variant[data-index="${state.selectedVariantIndex}"]`);
		const photos = selected && $('[data-variant-pane="photos"]', selected);
		if (photos) photos.click();
		if (selected) selected.scrollIntoView({ behavior: "smooth", block: "start" });
	});
	document.addEventListener("keydown", (e) => {
		if ((e.ctrlKey || e.metaKey) && String(e.key).toLowerCase() === "s") {
			e.preventDefault();
			saveAll().catch(() => {});
		}
	});

	document.addEventListener("input", (e) => {
		if (e.target.closest && (e.target.closest(".f5-main") || e.target.closest(".f5-topbar"))) setDirty(true);
	});

	$("#f-title").addEventListener("input", () => {
		$("#f5-header-title").textContent = $("#f-title").value.trim() || "Новий товар";
		autoSlug();
		updateBaseSeoPreview();
	});
	$("#f-slug").addEventListener("input", () => { state.slugTouched = true; updateSlugHint(); });
	$("#f-slug-auto").addEventListener("click", () => {
		state.slugTouched = false;
		$("#f-slug").value = f5Translit.slugify($("#f-title").value);
		updateSlugHint();
		setDirty(true);
	});
	$("#f-seo-title").addEventListener("input", updateSeoCounters);
	$("#f-seo-desc").addEventListener("input", updateSeoCounters);
	$("#f-short-desc").addEventListener("input", updateBaseSeoPreview);
	$("#f-price").addEventListener("input", () => {
		$$('.f5-variant').forEach((card) => refreshVariantPreview(card, state.variants[parseInt(card.dataset.index, 10)]));
	});

	$("#f-add-variant").addEventListener("click", async () => {
		try { await ensureProduct(); } catch (err) { return; }
		state.variants.push(emptyVariant());
		state.selectedVariantIndex = state.variants.length - 1;
		renderVariants();
		const cards = $$(".f5-variant");
		if (cards.length) cards[cards.length - 1].scrollIntoView({ behavior: "smooth", block: "start" });
	});

	$("#f-add-faq").addEventListener("click", () => {
		const box = $("#f-faqs");
		if (!$(".f5-faq", box)) box.innerHTML = "";
		box.insertAdjacentHTML("beforeend", faqHtml({ is_active: true }));
		setDirty(true);
	});

	window.addEventListener("beforeunload", (e) => {
		if (state.dirty) { e.preventDefault(); e.returnValue = ""; }
	});

	/* ---------------- старт ---------------- */
	function init() {
		fillSelect($("#f5-status"), dict.statuses || [], "value", "label");
		fillSelect($("#f-category"), dict.categories || [], "id", "name", "— оберіть —");
		fillSelect($("#f-catalog"), dict.catalogs || [], "id", "name", "—");
		fillSelect($("#f-size-grid"), dict.size_grids || [], "id", "name", "—");
		if (!state.product && dict.statuses && dict.statuses.length) {
			$("#f5-status").value = dict.statuses[0].value;
		}
		renderHeader();
		fillForm();
		renderFits();
		renderFaqs();
		renderGalleries();
		renderVariants();
		loadFeeds();
		setDirty(false);
	}
	init();
})();
