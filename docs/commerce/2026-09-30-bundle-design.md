# Комплекты: решение и проверяемая гипотеза

Предложение дополняет самостоятельную покупку худи. Одна обычная футболка получает цену комплекта на каждое обычное худи: тот же подтверждённый принт — 850 грн Classic / 1000 грн Oversize; другой принт — 900 / 1050 грн. Каждое худи в паре получает ещё50грнскидки:1995→1945. Фактическая общая выгода стандартной пары300грн за тот же принт,250грн за другой. Серия «Бригады», включая 225, и термо-модель 110 исключены. Скидка за тот же дизайн — осознанные дополнительные 50 грн выгоды клиенту; подтверждённого снижения производственной себестоимости на эту сумму нет.

## Почему именно такой интерфейс

На странице худи показывается одно основное предложение с изображением подходящей футболки, ценами обеих посадок и кнопкой выбора. Другие принты доступны внутри выбора вторым действием. Размер футболки не наследуется от худи и не выбирается молча. Итоговая цена и экономия меняются вместе с посадкой. Кнопка явно различает «добавить комплект» на странице худи и «добавить футболку» в корзине. При дополнении существующего худи отдельно показано увеличение суммы корзины: например футболка850 и уменьшениецены худи50 дают доплату800грн.

Это адаптация наблюдений [Baymard о релевантных допродажах в корзине](https://baymard.com/research-articles/product-recommendations-cart), [показе условий скидки рядом с ценой](https://baymard.com/research-articles/product-page-price-discounts), [видимых кнопках размеров](https://baymard.com/research-articles/use-buttons-for-size-selection) и принципа [постепенного раскрытия возможностей NNGroup](https://www.nngroup.com/articles/progressive-disclosure/). Эти источники не доказывают рост конверсии именно у TwoComms. [Метаанализ выбора](https://doi.org/10.1086/651235) также не позволяет утверждать, что любые два варианта перегружают покупателя.

На широком экране предложение стоит слева под фотографиями, описание справа раскрыто. Основная кнопка покупки расположена перед длинным описанием. На узком экране предложение остаётся дополнительным блоком; пользователь может купить только худи без открытия выбора футболки. В рекомендациях — одна краткая ссылка к тому же действию. В корзине показывается уже полученная экономия либо возможность дополнить оставшееся худи. Мини-корзина использует тёплый оранжевый акцент оформления и яркую оранжевую полоску доставки и одну отчётливую кнопку перехода к оформлению.

225 сохраняет самостоятельный оливковый блок и собственные условия. Заголовок «Комплект для 225 ОШП» относится к подразделению, а не к маркетинговому лозунгу бренда. Подробные условия раскрываются по запросу.

## Ограничения и защита цены

- Совпадение дизайнов проверяется по явно сверенным парам ID и slug, подтверждённым производственными связями. Общий логотип и похожее название не считаются одинаковым основным принтом.
- Разные количества, посадки и цвета рассчитываются на сервере. Один худи не используется дважды. Распределение выбирает наибольшую фактическую выгоду клиента.
- Цены фиксируются точными группами единиц, без умножения округлённой средней цены. Промокоды не добавляются к единицам комплекта, но могут действовать на остальные обычные товары.
- Выбор не создаёт заказ или платёж. Добавление двух вещей происходит целиком; ошибка доступности одного варианта не оставляет половину комплекта.
- Товары с неподдерживаемой дополнительной вариативностью не предлагаются в упрощённом выборе. Стандартная страница товара остаётся доступна.

## XML и одинаковые фотографии

Для BZ Classic и Oversize остаются одним предложением: «Футболка … [Classic / Oversize]». Описание содержит точные цены доступных посадок и порядок согласования с менеджером. Низкая цена Classic допустима только при фактической доступности Classic. Oversize-only не маскируется более дешёвым отсутствующим вариантом.

Причина объединения — одинаковые фотографии обеих посадок. Новые SKU по посадке не создаются ни в одном фиде. Разделение отложено до появления соответствующих отдельных изображений и согласованной миграции идентификаторов каталога. Существующие варианты размера и цвета сохраняют идентификаторы. BZ — динамический `/products_feed.xml`; остальные файловые выгрузки пересобираются штатной командой. BuyMe сохраняет отдельный оптовый ценовой контракт, а не автоматически превращается в розничную выгрузку.

## Как оценивать результат

Основной показатель — вклад в покрытие расходов на посетителя из рекламы и завершённый заказ. Дополнительно нужны доля заказов с футболкой к худи, средний чек после скидки, CPA завершённой покупки, отмены и обмены размера. Рост среднего чека сам по себе не доказывает прибыльность. Бюджет 5 долларов в день даёт слишком мало покупок для быстрого достоверного A/B-вывода; сначала проверяем понятность и ошибки, затем собираем достаточный объём реальных заказов.

## Mobile placement acceptance
Purchase controls and primary Add to cart precede the compact bundle offer; description follows it. This is the DOM reading/focus order as well as visual order, independent of description length. Both ordinary and 225 offers follow this rule below 1200px; desktop keeps its left-column offer. No duplicate sticky upsell is added. Exact conversion uplift is a hypothesis, not a proven outcome.
Sources: [NNGroup PDP](https://www.nngroup.com/articles/ecommerce-product-pages/), [Baymard relevant cross-sell information](https://baymard.com/research-articles/product-page-suggestions-information), [MDN flex ordering](https://developer.mozilla.org/en-US/docs/Web/CSS/CSS_flexible_box_layout/Ordering_flex_items) (also checked through Context7 /mdn/content), [W3C focus order](https://www.w3.org/WAI/WCAG21/Understanding/focus-order).

## CRO refinement after owner review
Keep the offer secondary to the standalone purchase. The first offer row identifies the collection and actual savings on goods; full totals remain visible in the chooser. The225offer uses its own olive treatment and explicit225ОШПcopy, with separate size selection. Missing hoodie size guides attention to existing controls before opening a modal. Secondary promo terms collapse; the size guide opens separately and does not discard the selection.
Mini-cart prioritizes readable size/fit12px and40px quantity controls. Generic benefit footnotes yield space to these details. Delivery messaging and payable totals must agree with the full cart, including promos and exclusion of unapproved custom estimates. Product titles link to their pages without falsely promising in-place editing.
Keep actual after-discount free-shipping threshold3000. Never describe the300/250goods discount as guaranteed identical savings including delivery. No timer, false scarcity, silent size choice, compulsory upsell, or fabricated conversion uplift is added.

Финальный цветовой выбор владельца: мини-корзина возвращена к исходной палитре до экспериментов, включая оформление и доставку; улучшения читаемости и расчётов сохранены. 225 сохраняет military olive. Обычный комплект использует тёплый песочный акцент, с явным сообщением: одинаковый принт выгоднее, другой тоже участвует.

Предложение учитывает направление: на футболке показывает худи и его цену в паре, на худи — футболку. Переход с футболки сохраняет её идентификатор для выбора комплекта. На мобильном блок расположен сразу после покупки, до описания.

Описание по умолчанию свёрнуто. На десктопе высота рассчитывается из естественной высоты левого блока без обратной зависимости от растянутой сетки; на телефоне применяется компактная высота. Кнопка раскрытия сохраняет выбор при переключении вкладок. Полный текст остаётся в HTML. Rich text проходит явный allowlist; обычные переносы строк сохраняются, legacy HTML больше не виден как текстовые теги.

Проверка: 13 тестов rich text/security и 14 каталога комплектов; браузер на 320/390/1440 px, раскрытие и сворачивание, направление предложения и выбор другого принта.

Ordinary offers now lead with adding a garment and saving, rather than a predefined set. Direction-specific headings, exact server savings and both print tiers remain explicit. A compact visible shipping row states 3000 UAH after discounts; it does not imply that every hoodie + tee reaches that threshold (1945 + 850 = 2795). The 225 collection keeps its separate identity and eligibility.

Category discovery refinement: both ordinary PDP CTA and the photo strip open the opposite garment category (tee → hoodies, hoodie → tees), localized to the current language. Real eligible catalog photos replace the arbitrary single partner visual. The teaser omits individual fit prices and the separate hoodie reduction; it shows only bounded total savings plus the after-discount free-shipping threshold. Exact quotes remain in the cart/chooser. The ordinary recommendation strip also links to all tees. 225 retains its dedicated flow.
