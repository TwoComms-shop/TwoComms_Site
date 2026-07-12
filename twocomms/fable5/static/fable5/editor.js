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
	}

	function updateSeoCounters() {
		$("#f-seo-title-count").textContent = ($("#f-seo-title").value || "").length + "/160";
		$("#f-seo-desc-count").textContent = ($("#f-seo-desc").value || "").length + "/320";
	}

	function updateSlugHint() {
		const slug = $("#f-slug").value.trim();
		$("#f-slug-hint").textContent = slug
			? "Посилання: /product/" + slug + "/"
			: "ч → ch, ш → sh, щ → shch, ї → yi… Лапки викидаються, пробіли → дефіси.";
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
		return $$("#f-fits .f5-fit-row").map((row) => ({
			code: row.dataset.code,
			label: row.dataset.label,
			is_enabled: $("[data-f=enabled]", row).checked,
			is_default: $("[data-f=default]", row).checked,
			reason: $("[data-f=reason]", row).value,
		}));
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
			if (!silent) toast(resp.created ? "Товар створено — працюємо далі без виходу" : "Збережено");
			return state.product;
		} catch (err) {
			toast("Помилка збереження: " + err.message, true);
			throw err;
		} finally {
			state.saving = false;
			$("#f5-save").disabled = false;
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
			? '<svg class="f5-dot__flame" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2c.6 3.2-1.1 4.9-2.6 6.5C7.8 10.2 6 12 6 15a6 6 0 0 0 12 0c0-2.1-.9-3.6-1.9-5-.4 1-.9 1.7-1.8 2.3.2-3.3-.9-7.6-2.3-10.3z" fill="#ff9f2e"/><path d="M12 22a4.2 4.2 0 0 1-4.2-4.2c0-1.7.9-2.7 1.8-3.7.6-.7 1.3-1.4 1.7-2.4 1.6 1.5 4.9 3.7 4.9 6.1A4.2 4.2 0 0 1 12 22z" fill="#ffd84d"/></svg>'
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
				<button type="button" class="f5-btn f5-btn--ghost f5-btn--small" data-act="cover" title="Зробити головною картинкою товару">⭐</button>
				<button type="button" class="f5-btn f5-btn--ghost f5-btn--small" data-act="home" title="Зробити карткою на головній">🏠</button>
				<button type="button" class="f5-btn f5-btn--danger f5-btn--small" data-act="del" title="Видалити">✕</button>
			</div>
			<input class="f5-input f5-thumb__alt" value="${esc(img.alt)}" placeholder="alt для SEO">
		</figure>`;
	}

	function renderGalleries() {
		const gallery = $("#f-product-gallery");
		const images = (state.product && state.product.images) || [];
		gallery.innerHTML = images.length
			? images.map((img, i) => thumbHtml(img, "product", null, i)).join("")
			: '<p class="f5-hint">Поки порожньо. Нові картинки завжди додаються в кінець і НЕ перезаписують вже завантажені.</p>';
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
				if (resp.main_image_url) $("#f-main-image").src = resp.main_image_url;
				if (resp.home_card_image_url) $("#f-home-image").src = resp.home_card_image_url;
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
			<div class="f5-fit-row" data-code="${esc(fit.code)}" data-label="${esc(fit.label)}">
				<label class="f5-switch" title="Доступність посадки"><input type="checkbox" data-f="enabled" ${fit.is_enabled ? "checked" : ""}><i></i></label>
				<strong>${esc(fit.label)}</strong>
				<label class="f5-check"><input type="radio" name="f5-fit-default" data-f="default" ${fit.is_default ? "checked" : ""}> за замовчуванням</label>
				<input class="f5-input" data-f="reason" value="${esc(fit.reason)}" placeholder="Причина, якщо вимкнена — показується в картці товару">
			</div>`).join("");
	}

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
			sizes: [], faqs: [],
			_open: true,
		};
	}

	function sizeRule(variant, fitCode, size) {
		return (variant.sizes || []).find((s) => s.fit_code === fitCode && s.size === size);
	}

	function sizeGridHtml(variant) {
		const enabledFits = state.fits.filter((f) => f.is_enabled);
		const rows = enabledFits.length ? enabledFits : [{ code: "", label: "Всі посадки" }];
		return rows.map((fit) => {
			const cells = sizesList().map((size) => {
				const rule = sizeRule(variant, fit.code, size) || { is_enabled: true, stock: null };
				const stockCls = rule.stock === 0 ? " f5-stock-zero" : (rule.stock != null && rule.stock <= 3 ? " f5-stock-low" : "");
				return `<div class="f5-size-cell${rule.is_enabled ? "" : " is-off"}${stockCls}" data-fit="${esc(fit.code)}" data-size="${esc(size)}">
					<button type="button" class="f5-btn f5-btn--ghost f5-btn--small" data-act="size-toggle" title="Увімкнути/вимкнути розмір для цього кольору">${esc(size)}</button>
					<input type="number" min="0" data-f="stock" value="${rule.stock != null ? rule.stock : ""}" placeholder="∞" title="Залишок на складі (порожньо — не ведеться)">
				</div>`;
			}).join("");
			return `<div class="f5-subsection"><div class="f5-hint">${esc(fit.label)}</div><div class="f5-size-grid">${cells}</div></div>`;
		}).join("");
	}

	function colorPickerHtml(variant) {
		const options = (dict.colors || []).map((c) => `
			<button type="button" class="f5-color-option${variant.color.id === c.id ? " is-selected" : ""}" data-act="pick-color" data-color='${esc(JSON.stringify(c))}'>
				${dotHtml(c, 16)} <span>${esc(c.name || c.primary_hex)}</span>${c.is_thermo ? " 🔥" : ""}
			</button>`).join("");
		return `<div class="f5-subsection">
			<div class="f5-hint">Бібліотека кольорів — один клік, без подвійного вводу назви. Зміна HEX нижче = новий колір.</div>
			<div class="f5-color-picker">${options || '<span class="f5-hint">Бібліотека порожня — створіть перший колір нижче</span>'}</div>
			<div class="f5-row">
				<label class="f5-field"><span>Назва кольору</span><input class="f5-input" data-f="color_name" value="${esc(variant.color.name)}" placeholder="Напр.: Койот"></label>
				<label class="f5-field"><span>HEX основний</span><span class="f5-row"><input type="color" data-f="color_pick" value="${/^#[0-9a-fA-F]{6}$/.test(variant.color.primary_hex || "") ? esc(variant.color.primary_hex) : "#222222"}"><input class="f5-input" data-f="color_hex" value="${esc(variant.color.primary_hex)}" placeholder="#000000"></span></label>
				<label class="f5-field"><span>HEX другий (двоколірний)</span><input class="f5-input" data-f="color_hex2" value="${esc(variant.color.secondary_hex)}" placeholder="порожньо = одноколірний"></label>
			</div>
			<div class="f5-row">
				<label class="f5-check"><input type="checkbox" data-f="is_thermo" ${variant.color.is_thermo ? "checked" : ""}> 🔥 Термохромний — кружечок із вогником у картці й на головній</label>
				<span data-role="dot-preview">${dotHtml(variant.color, 26)}</span>
				<label class="f5-field"><span>Примітка термо</span><input class="f5-input" data-f="thermo_note" value="${esc(variant.color.thermo_note)}" placeholder="Реагує на тепло — змінює відтінок"></label>
			</div>
			<label class="f5-field"><span>Опис тканини/кольору (SEO-дружній, показується в картці)</span><textarea class="f5-input" rows="2" data-f="color_description">${esc(variant.color.description)}</textarea></label>
		</div>`;
	}

	function variantHtml(variant, index) {
		const d = variant.details || {};
		const chips = [
			variant.is_default ? '<span class="f5-chip f5-chip--default">головний на вітрині</span>' : "",
			variant.color.is_thermo ? '<span class="f5-chip f5-chip--thermo">🔥 термо</span>' : "",
			`<span class="f5-chip">картинок: ${(variant.images || []).length}</span>`,
			d.price_delta ? `<span class="f5-chip">${d.price_delta > 0 ? "+" : ""}${d.price_delta} грн</span>` : "",
		].join("");
		const uploadBlock = variant.id
			? `<div class="f5-dropzone" data-role="variant-drop">Перетягніть картинки цього кольору або <button type="button" class="f5-btn f5-btn--ghost" data-act="variant-upload-btn">оберіть файли</button><input type="file" accept="image/*" multiple hidden data-role="variant-upload"></div><div class="f5-gallery" data-role="variant-gallery">${(variant.images || []).map((img, i) => thumbHtml(img, "variant", variant.id, i)).join("")}</div>`
			: '<p class="f5-hint">Щоб завантажити картинки цього кольору — спочатку натисніть «Зберегти колір».</p>';
		const fitRows = state.fits.map((f) => {
			const rule = (variant.fits || []).find((r) => r.fit_code === f.code) || { is_enabled: true, reason: "" };
			return `<div class="f5-fit-row" data-fit="${esc(f.code)}">
				<label class="f5-switch"><input type="checkbox" data-f="fit_enabled" ${rule.is_enabled ? "checked" : ""}><i></i></label>
				<strong>${esc(f.label)}</strong>
				<input class="f5-input" data-f="fit_reason" value="${esc(rule.reason)}" placeholder="Причина для покупця, якщо вимкнено (напр.: термо — лише оверсайз)">
			</div>`;
		}).join("");
		const variantFaqs = (variant.faqs || []).map(faqHtml).join("");
		return `<article class="f5-variant${variant._open ? " is-open" : ""}" data-index="${index}"${variant.id ? ` data-id="${variant.id}"` : ""}>
			<header class="f5-variant__head" data-act="variant-toggle">
				${dotHtml(variant.color, 22)}
				<span class="f5-variant__name">${esc(d.display_name || variant.color.name || "Новий колір")}</span>
				<span class="f5-variant__meta">${chips}</span>
				<span class="f5-variant__spacer"></span>
				<button type="button" class="f5-btn f5-btn--ghost f5-btn--small" data-act="variant-up" title="Вище">↑</button>
				<button type="button" class="f5-btn f5-btn--ghost f5-btn--small" data-act="variant-down" title="Нижче">↓</button>
				<button type="button" class="f5-btn f5-btn--ghost f5-btn--small" data-act="variant-toggle-btn" aria-expanded="${variant._open ? "true" : "false"}">${variant._open ? "Згорнути" : "Редагувати"}</button>
			</header>
			<div class="f5-variant__body">
				${colorPickerHtml(variant)}
				<div class="f5-subsection">
					<div class="f5-row">
						<label class="f5-field"><span>Артикул (SKU)</span><input class="f5-input" data-f="sku" value="${esc(variant.sku)}"></label>
						<label class="f5-field"><span>Ціна замість базової, грн</span><input type="number" min="0" class="f5-input" data-f="price_override" value="${variant.price_override != null ? variant.price_override : ""}" placeholder="порожньо = базова"></label>
						<label class="f5-check"><input type="checkbox" data-f="is_default" ${variant.is_default ? "checked" : ""}> головний колір на головній/вітрині</label>
					</div>
					<div class="f5-row">
						<label class="f5-field"><span>Надбавка до ціни за цей колір, грн</span><input type="number" class="f5-input" data-f="price_delta" value="${d.price_delta || 0}"></label>
						<label class="f5-field"><span>Причина надбавки (бачить покупець)</span><input class="f5-input" data-f="price_delta_reason" value="${esc(d.price_delta_reason)}" placeholder="термохромна тканина"></label>
						<label class="f5-field"><span>YouTube для цього кольору</span><input class="f5-input" data-f="youtube_url" value="${esc(d.youtube_url)}" placeholder="порожньо = спільне відео товару"></label>
					</div>
					<label class="f5-field"><span>Назва-вітрина цього кольору (як окремий товар)</span><input class="f5-input" data-f="display_name" value="${esc(d.display_name)}" placeholder="Напр.: Сіра футболка оверсайз — колір койот"></label>
					<label class="f5-field"><span>Маркетинговий опис тканини/кольору (HTML, показується в картці)</span><textarea class="f5-input" rows="3" data-f="marketing_html">${esc(d.marketing_html)}</textarea></label>
				</div>
				<div class="f5-subsection">
					<div class="f5-hint">SEO цього кольору — кожен колір як окремий товар у пошуку</div>
					<div class="f5-row">
						<label class="f5-field"><span>SEO Title</span><input class="f5-input" data-f="seo_title" maxlength="180" value="${esc(d.seo_title)}"></label>
						<label class="f5-field"><span>SEO Keywords</span><input class="f5-input" data-f="seo_keywords" maxlength="300" value="${esc(d.seo_keywords)}"></label>
					</div>
					<label class="f5-field"><span>SEO Description</span><textarea class="f5-input" rows="2" maxlength="320" data-f="seo_description">${esc(d.seo_description)}</textarea></label>
				</div>
				<div class="f5-subsection"><div class="f5-hint">Посадки для цього кольору</div>${fitRows}</div>
				<div class="f5-subsection"><div class="f5-hint">Розміри та склад (клік по розміру = увімк/вимк, число = залишок)</div><div data-role="size-grid">${sizeGridHtml(variant)}</div></div>
				<div class="f5-subsection"><div class="f5-hint">Картинки цього кольору (append + drag&drop; звідси теж можна ⭐ обрати головну картинку товару)</div>${uploadBlock}</div>
				<div class="f5-subsection"><div class="f5-colors-head"><div class="f5-hint">FAQ цього кольору (UA/RU/EN)</div><button type="button" class="f5-btn f5-btn--ghost f5-btn--small" data-act="variant-faq-add">＋ питання</button></div><div data-role="variant-faqs">${variantFaqs}</div></div>
				<div class="f5-colors-head">
					<button type="button" class="f5-btn f5-btn--primary" data-act="variant-save">💾 Зберегти колір</button>
					<button type="button" class="f5-btn f5-btn--danger" data-act="variant-delete">Видалити колір</button>
				</div>
			</div>
		</article>`;
	}

	function renderVariants() {
		const box = $("#f-variants");
		box.innerHTML = state.variants.length
			? state.variants.map(variantHtml).join("")
			: '<p class="f5-hint">Кольорів ще немає. Натисніть «＋ Додати колір» — все редагується тут же, без переходів на інші сторінки.</p>';
		renderStock();
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
		const fits = $$(".f5-fit-row[data-fit]", card).map((row) => ({
			fit_code: row.dataset.fit,
			is_enabled: $("[data-f=fit_enabled]", row).checked,
			reason: $("[data-f=fit_reason]", row).value,
		}));
		const faqs = $$("[data-role=variant-faqs] .f5-faq", card).map((node) => ({
			question_uk: $("[data-f=question_uk]", node).value,
			question_ru: $("[data-f=question_ru]", node).value,
			question_en: $("[data-f=question_en]", node).value,
			answer_uk: $("[data-f=answer_uk]", node).value,
			answer_ru: $("[data-f=answer_ru]", node).value,
			answer_en: $("[data-f=answer_en]", node).value,
			is_active: $("[data-f=is_active]", node).checked,
		}));
		return {
			id: variant.id,
			product_id: state.product.id,
			color: {
				id: variant.color.id,
				name: val("[data-f=color_name]"),
				primary_hex: val("[data-f=color_hex]").trim(),
				secondary_hex: val("[data-f=color_hex2]").trim(),
				is_thermo: checked("[data-f=is_thermo]"),
				thermo_note: val("[data-f=thermo_note]"),
				description: val("[data-f=color_description]"),
			},
			sku: val("[data-f=sku]"),
			price_override: intOrNull(val("[data-f=price_override]")),
			is_default: checked("[data-f=is_default]"),
			details: {
				display_name: val("[data-f=display_name]"),
				price_delta: intOrNull(val("[data-f=price_delta]")) || 0,
				price_delta_reason: val("[data-f=price_delta_reason]"),
				marketing_html: val("[data-f=marketing_html]"),
				youtube_url: val("[data-f=youtube_url]"),
				seo_title: val("[data-f=seo_title]"),
				seo_description: val("[data-f=seo_description]"),
				seo_keywords: val("[data-f=seo_keywords]"),
			},
			fits: fits,
			sizes: sizes,
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
		renderVariants();
		toast("Колір видалено");
	}

	async function moveVariant(index, delta) {
		const target = index + delta;
		if (target < 0 || target >= state.variants.length) return;
		const item = state.variants.splice(index, 1)[0];
		state.variants.splice(target, 0, item);
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
		if (preview) preview.innerHTML = dotHtml(color, 26);
		if (variant) {
			variant.color.primary_hex = color.primary_hex;
			variant.color.secondary_hex = color.secondary_hex;
			variant.color.is_thermo = color.is_thermo;
		}
	}

	/* події всередині списку кольорів */
	$("#f-variants").addEventListener("click", (e) => {
		const card = e.target.closest(".f5-variant");
		if (!card) return;
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
		if (act === "variant-toggle" || act === "variant-toggle-btn") {
			variant._open = !variant._open;
			card.classList.toggle("is-open", variant._open);
			const btn = $("[data-act=variant-toggle-btn]", card);
			if (btn) {
				btn.textContent = variant._open ? "Згорнути" : "Редагувати";
				btn.setAttribute("aria-expanded", variant._open ? "true" : "false");
			}
		}
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
		}
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
			btn.closest(".f5-size-cell").classList.toggle("is-off");
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
				<div class="f5-colors-head">
					<div>${dotHtml(v.color, 18)} <strong>${esc((v.details && v.details.display_name) || v.color.name || "Колір")}</strong></div>
					<button type="button" class="f5-btn f5-btn--ghost f5-btn--small" data-act="stock-save"${v.id ? "" : " disabled title='Спершу збережіть колір'"}>💾 Зберегти склад</button>
				</div>
				<div data-role="stock-grid">${sizeGridHtml(v)}</div>
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
					<button type="button" class="f5-btn f5-btn--primary f5-btn--small" data-act="feed-save">💾 Зберегти фід</button>
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
			$(imgSel).src = URL.createObjectURL(file);
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
	$("#f5-tabs").addEventListener("click", (e) => {
		const tab = e.target.closest(".f5-tab");
		if (!tab) return;
		$$(".f5-tab").forEach((t) => {
			t.classList.toggle("is-active", t === tab);
			t.setAttribute("aria-selected", t === tab ? "true" : "false");
		});
		$$(".f5-panel").forEach((p) => p.classList.toggle("is-active", p.dataset.panel === tab.dataset.tab));
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

	$("#f-add-variant").addEventListener("click", async () => {
		try { await ensureProduct(); } catch (err) { return; }
		state.variants.push(emptyVariant());
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
