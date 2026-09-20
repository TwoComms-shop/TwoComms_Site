from django.db import migrations
from django.utils import timezone


UVF_STORY_URL = "https://veteranfund.com.ua/stories-of-winner/artem-sinilo-istoriia-veterana/"
SUSPILNE_VIDEO_URL = "https://www.youtube.com/watch?v=7K2dkjNg3cQ"
RADIO_NAKYPILO_FIRST_URL = "https://radio.nakypilo.ua/podcast/futbolky-j-gudi-vid-veterana-z-harakterom-harkivczya/"
RADIO_NAKYPILO_SECOND_URL = "https://radio.nakypilo.ua/podcast/u-kozhnomu-prynti-ye-shyfry-pro-zhyttya-vijskovyh-yak-veteran-artem-synilo-stvoryuye-odyag-z-istoriyamy/"


RICH_TEXT = {
    "uk": """
<section class="article-lede-panel">
  <p><strong>Третій із чотирьох етапів реалізації проєктної заявки TwoComms у програмі Українського ветеранського фонду «Варто більше» завершено.</strong> На цьому етапі грантова підтримка допомогла нам зміцнити те, від чого залежить стабільність кожного замовлення: запас критичних витратних матеріалів, передбачуваність виробничого циклу та розвиток напряму кастомного одягу.</p>
</section>

<h2>Підтримка, яка перетворюється на стабільний процес</h2>
<p>Для TwoComms грант Українського ветеранського фонду став не формальною подією, а ресурсом для системної роботи. Власний DTF-друк складається з багатьох взаємопов'язаних компонентів і витратних матеріалів. Коли кожен з них є вчасно, команда може планувати виробництво, не зупинятися між замовленнями та швидше переходити від погодженого макета до готової речі.</p>
<p>У третьому етапі ми сформували більш надійну базу витратних матеріалів для друку, перенесення та обслуговування обладнання. Це дало змогу суттєво стабілізувати потік замовлень і вчасно виконувати кастомні роботи, де важливі і точність деталей, і узгоджені строки.</p>

<h2>Кастомний одяг: від разового запиту до окремого напряму</h2>
<p>Ми продовжили будувати основу для кастомного одягу як окремого напряму TwoComms. Удосконалили наявний функціонал та розвинули окремі цифрові інструменти для кастомізації, щоб шлях клієнта від ідеї до замовлення був зрозумілішим, а внутрішня підготовка макетів і виробництва - зібранішою.</p>
<p>Для нас це означає більше контролю над замовленням на кожному кроці: від обговорення ідеї та підготовки дизайну до друку, перенесення і фінальної перевірки. Для клієнта - можливість звернутися зі своїм символом, фразою, зображенням чи ідеєю та отримати зрозумілий процес без зайвих бар'єрів.</p>

<h2>Термопрес: рішення перенесено на четвертий етап</h2>
<p>У третьому етапі ми планували придбати автоматичний термопрес. Його дві робочі панелі та лазерне позиціонування мають скоротити час перенесення принтів і зробити розміщення ще точнішим. Це важливе наступне підсилення для якості, швидкості та повторюваності виробництва.</p>
<p>Постачання обраної моделі виявилося довшим, ніж дозволяли строки третього етапу. Тому ми свідомо перенесли закупівлю термопреса до четвертого етапу, не підміняючи заплановане обладнання компромісним рішенням. У межах третього етапу ресурс спрямували на те, що можна було використати одразу: стабілізацію витратних матеріалів і робочих процесів.</p>

<h2>Публічна розмова про бренд, Харків і сенси принтів</h2>
<p>Цей етап став помітним і поза виробництвом. Про TwoComms розповіло Суспільне, а в двох епізодах Радіо Накипіло ми говорили про бренд, Харків, ветеранський досвід і деталі, які закладені в принти. Це можливість показати, що за речами TwoComms стоїть не лише технологія, а й жива історія міста, досвід засновника та уважне ставлення до змісту кожної роботи.</p>

<h2>Що вже зроблено і що буде далі</h2>
<p>Три з чотирьох етапів реалізації проєкту вже завершено. Третій етап дав TwoComms більш стійку операційну основу: ми посилили запаси критичних матеріалів, розвинули кастомний напрям і вдосконалили цифрові інструменти для роботи з ним. Четвертий етап зосередимо на автоматичному термопресі - рішенні, яке має відчутно посилити швидкість і точність перенесення принтів.</p>
<p>Дякуємо Українському ветеранському фонду за довіру та підтримку ветеранського підприємництва. Для TwoComms це підтримка, яка працює в конкретних результатах: стабільнішому виробництві, якіснішій підготовці кастомних замовлень і можливості впевнено рухатися далі.</p>
""",
    "ru": """
<section class="article-lede-panel">
  <p><strong>Третий из четырех этапов реализации проектной заявки TwoComms в программе Украинского ветеранского фонда «Варто більше» завершен.</strong> Грантовая поддержка помогла укрепить основу стабильной работы: запас критически важных расходных материалов, предсказуемость производственного цикла и направление кастомной одежды.</p>
</section>
<h2>Поддержка, которая превращается в стабильный процесс</h2>
<p>Для TwoComms грант Украинского ветеранского фонда стал ресурсом для системной работы. Собственная DTF-печать зависит от многих взаимосвязанных компонентов и расходных материалов. Когда они доступны вовремя, команда может планировать производство, не останавливаться между заказами и быстрее переходить от согласованного макета к готовой вещи.</p>
<p>На третьем этапе мы сформировали более надежную базу материалов для печати, переноса и обслуживания оборудования. Это помогло стабилизировать поток заказов и вовремя выполнять кастомные работы, где важны точность деталей и согласованные сроки.</p>
<h2>Кастомная одежда как отдельное направление</h2>
<p>Мы продолжили развивать кастомную одежду как отдельное направление TwoComms: усовершенствовали существующий функционал и отдельные цифровые инструменты для кастомизации. Путь от идеи до заказа стал понятнее, а подготовка макетов и производства - собраннее.</p>
<h2>Термопресс перенесен на четвертый этап</h2>
<p>На третьем этапе планировалась покупка автоматического термопресса с двумя рабочими панелями и лазерным позиционированием. Из-за длительной поставки выбранной модели покупку перенесли на четвертый этап, не заменяя запланированное оборудование компромиссным решением. В текущем этапе ресурс направили на расходные материалы и процессы, которые дали результат сразу.</p>
<h2>Открытый разговор о бренде</h2>
<p>О TwoComms рассказало Суспільне, а в двух эпизодах Радио Накипіло мы говорили о бренде, Харькове, ветеранском опыте и смыслах принтов. Так мы показываем не только технологию, но и историю, которая стоит за каждой вещью.</p>
<h2>Результат третьего этапа</h2>
<p>Три из четырех этапов проекта завершены. Третий этап укрепил операционную основу TwoComms: запасы материалов, направление кастомизации и цифровые инструменты. На четвертом этапе планируем автоматический термопресс, который усилит скорость и точность переноса принтов.</p>
<p>Благодарим Украинский ветеранский фонд за доверие и поддержку ветеранского предпринимательства.</p>
""",
    "en": """
<section class="article-lede-panel">
  <p><strong>The third of four stages of the TwoComms project application under the Ukrainian Veterans Fund's Varto Bilse programme has been completed.</strong> This grant support strengthened the basis of stable work: critical consumables, a more predictable production cycle, and custom apparel development.</p>
</section>
<h2>Support that becomes a stable process</h2>
<p>For TwoComms, the Ukrainian Veterans Fund grant is a resource for systematic work. In-house DTF printing relies on many connected components and consumables. Keeping them available allows the team to plan production, avoid interruptions between orders, and move faster from an approved artwork to a finished garment.</p>
<p>During stage three, we built a more reliable supply base for printing, transferring, and equipment maintenance. This helped stabilize the order flow and deliver custom work on time, where both detail and agreed timelines matter.</p>
<h2>Custom apparel as a dedicated direction</h2>
<p>We continued to develop custom apparel as a dedicated TwoComms direction, improving the existing functionality and dedicated digital customization tools. The path from an idea to an order is clearer, while artwork preparation and production are more organized.</p>
<h2>Heat press moved to stage four</h2>
<p>Stage three included a planned purchase of an automatic heat press with two working platens and laser positioning. The selected model had a longer delivery time than the stage allowed, so we moved this purchase to stage four without replacing it with a compromise. Stage-three resources went to consumables and processes that could improve work immediately.</p>
<h2>A public conversation about the brand</h2>
<p>Suspilne featured TwoComms, and we discussed the brand, Kharkiv, veteran experience, and the meanings behind our prints in two Radio Nakypilo episodes. These publications show the story behind the technology and every TwoComms piece.</p>
<h2>Stage-three result</h2>
<p>Three of four project stages are complete. Stage three strengthened TwoComms' operational base through materials, customization, and digital tools. Stage four will focus on the automatic heat press to improve the speed and precision of print transfers.</p>
<p>We thank the Ukrainian Veterans Fund for its trust and support of veteran entrepreneurship.</p>
""",
}


def localized(uk, ru, en):
    return {"uk": uk, "ru": ru, "en": en}


def publish_stage_three_report(apps, schema_editor):
    BlogCategory = apps.get_model("storefront", "BlogCategory")
    BlogPost = apps.get_model("storefront", "BlogPost")
    BlogPostBlock = apps.get_model("storefront", "BlogPostBlock")
    db = schema_editor.connection.alias

    category = BlogCategory.objects.using(db).get(slug="veteran-fund")
    title = "TwoComms і Український ветеранський фонд: третій етап реалізовано"
    defaults = {
        "category": category,
        "title": title,
        "title_uk": title,
        "title_ru": "TwoComms и Украинский ветеранский фонд: третий этап реализован",
        "title_en": "TwoComms and the Ukrainian Veterans Fund: stage three complete",
        "excerpt": "Третій з чотирьох етапів проєкту TwoComms у програмі «Варто більше» завершено: посилено базу витратних матеріалів, кастомний напрям і цифрові інструменти.",
        "excerpt_uk": "Третій з чотирьох етапів проєкту TwoComms у програмі «Варто більше» завершено: посилено базу витратних матеріалів, кастомний напрям і цифрові інструменти.",
        "excerpt_ru": "Третий из четырех этапов проекта TwoComms в программе «Варто більше» завершен: усилены запасы материалов, направление кастомизации и цифровые инструменты.",
        "excerpt_en": "The third of four TwoComms project stages under the Varto Bilse programme is complete, strengthening consumables, customization, and digital tools.",
        "content_html": RICH_TEXT["uk"],
        "content_html_uk": RICH_TEXT["uk"],
        "content_html_ru": RICH_TEXT["ru"],
        "content_html_en": RICH_TEXT["en"],
        "cover_alt": "Третій етап проєкту TwoComms за підтримки Українського ветеранського фонду",
        "cover_alt_uk": "Третій етап проєкту TwoComms за підтримки Українського ветеранського фонду",
        "cover_alt_ru": "Третий этап проекта TwoComms при поддержке Украинского ветеранского фонда",
        "cover_alt_en": "TwoComms project stage three with support from the Ukrainian Veterans Fund",
        "cover_caption": "TWOCOMMS / ВАРТО БІЛЬШЕ / ЕТАП 03 З 04",
        "cover_caption_uk": "TWOCOMMS / ВАРТО БІЛЬШЕ / ЕТАП 03 З 04",
        "cover_caption_ru": "TWOCOMMS / ВАРТО БІЛЬШЕ / ЭТАП 03 ИЗ 04",
        "cover_caption_en": "TWOCOMMS / VARTO BILSE / STAGE 03 OF 04",
        "source_url": UVF_STORY_URL,
        "cta_label": "Створити свій принт",
        "cta_label_uk": "Створити свій принт",
        "cta_label_ru": "Создать свой принт",
        "cta_label_en": "Create your print",
        "cta_url": "/custom-print/",
        "cta_text": "Власна ідея, символ або текст - зберемо її в кастомну річ TwoComms.",
        "cta_text_uk": "Власна ідея, символ або текст - зберемо її в кастомну річ TwoComms.",
        "cta_text_ru": "Своя идея, символ или текст - соберем ее в кастомную вещь TwoComms.",
        "cta_text_en": "Your idea, symbol, or text can become a custom TwoComms piece.",
        "seo_title": "TwoComms і Український ветеранський фонд: третій етап",
        "seo_title_uk": "TwoComms і Український ветеранський фонд: третій етап",
        "seo_title_ru": "TwoComms и Украинский ветеранский фонд: третий этап",
        "seo_title_en": "TwoComms and the Ukrainian Veterans Fund: stage three",
        "seo_description": "Звіт TwoComms про третій етап проєкту в програмі Українського ветеранського фонду «Варто більше»: стабілізація виробництва, кастомний одяг і цифрові інструменти.",
        "seo_description_uk": "Звіт TwoComms про третій етап проєкту в програмі Українського ветеранського фонду «Варто більше»: стабілізація виробництва, кастомний одяг і цифрові інструменти.",
        "seo_description_ru": "Отчет TwoComms о третьем этапе проекта в программе Украинского ветеранского фонда «Варто більше»: стабильное производство, кастомная одежда и цифровые инструменты.",
        "seo_description_en": "TwoComms' stage-three report for the Ukrainian Veterans Fund Varto Bilse programme: stable production, custom apparel, and digital tools.",
        "seo_keywords": "TwoComms, Український ветеранський фонд, Варто більше, ветеранське підприємництво, кастомний одяг, DTF-друк, Харків",
        "seo_keywords_uk": "TwoComms, Український ветеранський фонд, Варто більше, ветеранське підприємництво, кастомний одяг, DTF-друк, Харків",
        "seo_keywords_ru": "TwoComms, Украинский ветеранский фонд, Варто більше, ветеранское предпринимательство, кастомная одежда, DTF-печать, Харьков",
        "seo_keywords_en": "TwoComms, Ukrainian Veterans Fund, Varto Bilse, veteran entrepreneurship, custom apparel, DTF printing, Kharkiv",
        "is_published": True,
    }
    post, created = BlogPost.objects.using(db).get_or_create(
        slug="twocomms-veteran-fund-stage-three",
        defaults={**defaults, "published_at": timezone.now()},
    )
    if not created:
        BlogPost.objects.using(db).filter(pk=post.pk).update(**defaults)
        post.refresh_from_db()

    blocks = [
        {
            "block_type": "metric_cards",
            "sort_order": 0,
            "payload": {
                "cards": [
                    {"label": localized("Прогрес проєкту", "Прогресс проекта", "Project progress"), "value": "3/4", "caption": localized("етапи реалізовано", "этапа реализовано", "stages complete"), "status": "done"},
                    {"label": localized("Виробництво", "Производство", "Production"), "value": "DTF", "caption": localized("стабілізовано витратні матеріали", "стабилизированы расходные материалы", "consumables stabilized"), "status": "done"},
                    {"label": localized("Кастомізація", "Кастомизация", "Customization"), "value": "Digital", "caption": localized("посилено інструменти та процес", "усилены инструменты и процесс", "tools and workflow strengthened"), "status": "done"},
                    {"label": localized("Наступний крок", "Следующий шаг", "Next step"), "value": "04", "caption": localized("автоматичний термопрес", "автоматический термопресс", "automatic heat press"), "status": "next"},
                ]
            },
        },
        {"block_type": "rich_text", "sort_order": 10, "payload": {"html": RICH_TEXT}},
        {
            "block_type": "callout",
            "sort_order": 20,
            "payload": {
                "tone": "info",
                "title": localized("Результат грантової підтримки", "Результат грантовой поддержки", "Result of grant support"),
                "body": localized(
                    "<p>Третій етап не про разову закупівлю, а про виробничу стійкість: необхідні матеріали доступні вчасно, кастомні замовлення мають зрозуміліший шлях, а команда може точніше планувати роботу.</p>",
                    "<p>Третий этап - не о разовой закупке, а о производственной устойчивости: нужные материалы доступны вовремя, путь кастомного заказа понятнее, а команда точнее планирует работу.</p>",
                    "<p>Stage three is about production resilience: necessary materials are available on time, custom orders have a clearer path, and the team can plan work more precisely.</p>",
                ),
            },
        },
        {
            "block_type": "table_specs",
            "sort_order": 30,
            "payload": {
                "title": localized("Що реалізовано у третьому етапі", "Что реализовано на третьем этапе", "What stage three delivered"),
                "rows": [
                    {"label": localized("Витратні матеріали", "Расходные материалы", "Consumables"), "value": localized("Посилено базу для друку, перенесення та обслуговування обладнання.", "Усилена база для печати, переноса и обслуживания оборудования.", "Strengthened supplies for printing, transfer, and equipment maintenance.")},
                    {"label": localized("Стабільність замовлень", "Стабильность заказов", "Order stability"), "value": localized("Менше залежності від пауз між постачаннями та більше передбачуваності в роботі.", "Меньше зависимости от пауз между поставками и больше предсказуемости в работе.", "Less dependence on supply gaps and more predictable work.")},
                    {"label": localized("Кастомний напрям", "Кастомное направление", "Custom direction"), "value": localized("Удосконалено функціонал та цифрові інструменти для кастомізації одягу.", "Усовершенствованы функционал и цифровые инструменты для кастомизации одежды.", "Improved functionality and digital tools for custom apparel.")},
                    {"label": localized("Термопрес", "Термопресс", "Heat press"), "value": localized("Закупівлю автоматичної моделі з двома панелями та лазерним позиціонуванням перенесено на етап 04 через строк постачання.", "Закупка автоматической модели с двумя панелями и лазерным позиционированием перенесена на этап 04 из-за срока поставки.", "The automatic two-platen, laser-positioned model moved to stage 04 because of delivery timing.")},
                ],
            },
        },
        {
            "block_type": "youtube_video",
            "sort_order": 40,
            "payload": {
                "url": SUSPILNE_VIDEO_URL,
                "title": localized("Суспільне про TwoComms: бренд, Харків і ветеранське підприємництво", "Суспільне о TwoComms: бренд, Харьков и ветеранское предпринимательство", "Suspilne on TwoComms: the brand, Kharkiv, and veteran entrepreneurship"),
            },
        },
        {
            "block_type": "quote",
            "sort_order": 50,
            "payload": {
                "text": localized("<p>Підтримка Українського ветеранського фонду допомагає TwoComms перетворювати ідею бренда на стійкий процес: від уважної роботи з макетом до готової речі, за яку ми відповідаємо.</p>", "<p>Поддержка Украинского ветеранского фонда помогает TwoComms превращать идею бренда в устойчивый процесс: от внимательной работы с макетом до готовой вещи, за которую мы отвечаем.</p>", "<p>Support from the Ukrainian Veterans Fund helps TwoComms turn the brand idea into a resilient process, from careful artwork preparation to the finished piece we stand behind.</p>"),
                "cite": localized("Команда TwoComms", "Команда TwoComms", "The TwoComms team"),
            },
        },
        {
            "block_type": "source_list",
            "sort_order": 60,
            "payload": {
                "eyebrow": localized("Відкриті матеріали", "Открытые материалы", "Public materials"),
                "title": localized("Де прочитати й подивитися більше", "Где прочитать и посмотреть больше", "Read and watch more"),
                "description": localized("Публікації про TwoComms, ветеранський досвід засновника, Харків та історії, закладені в принтах.", "Публикации о TwoComms, ветеранском опыте основателя, Харькове и историях, заложенных в принтах.", "Publications about TwoComms, the founder's veteran experience, Kharkiv, and the stories behind the prints."),
                "sources": [
                    {"label": localized("Український ветеранський фонд: історія Артема Синіло і TwoComms", "Украинский ветеранский фонд: история Артема Синило и TwoComms", "Ukrainian Veterans Fund: Artem Synilo and TwoComms"), "url": UVF_STORY_URL},
                    {"label": localized("Суспільне: відеосюжет про TwoComms", "Суспільне: видеосюжет о TwoComms", "Suspilne: video feature on TwoComms"), "url": SUSPILNE_VIDEO_URL},
                    {"label": localized("Радіо Накипіло: «Футболки й худі від ветерана з характером»", "Радио Накипіло: «Футболки и худи от ветерана с характером»", "Radio Nakypilo: T-shirts and hoodies by a veteran with character"), "url": RADIO_NAKYPILO_FIRST_URL},
                    {"label": localized("Радіо Накипіло: «У кожному принті є шифри про життя військових»", "Радио Накипіло: «В каждом принте есть шифры о жизни военных»", "Radio Nakypilo: Every print carries codes about military life"), "url": RADIO_NAKYPILO_SECOND_URL},
                ],
            },
        },
        {
            "block_type": "cta_group",
            "sort_order": 70,
            "payload": {
                "layout": "full",
                "eyebrow": localized("Кастомний одяг TwoComms", "Кастомная одежда TwoComms", "TwoComms custom apparel"),
                "title": localized("Ідея може стати вашою річчю", "Идея может стать вашей вещью", "An idea can become your piece"),
                "body": localized("<p>Завантажте принт або опишіть задум - команда TwoComms допоможе перетворити його на кастомну річ.</p>", "<p>Загрузите принт или опишите идею - команда TwoComms поможет превратить ее в кастомную вещь.</p>", "<p>Upload a print or describe your idea, and the TwoComms team will help turn it into a custom piece.</p>"),
                "buttons": [
                    {"provider": "custom_print", "label": localized("Створити свій принт", "Создать свой принт", "Create your print"), "caption": localized("Кастомна річ від ідеї до замовлення", "Кастомная вещь от идеи до заказа", "A custom piece from idea to order"), "url": "/custom-print/"},
                ],
            },
        },
    ]
    BlogPostBlock.objects.using(db).filter(post=post).delete()
    BlogPostBlock.objects.using(db).bulk_create(
        [
            BlogPostBlock(
                post=post,
                block_type=block["block_type"],
                sort_order=block["sort_order"],
                payload=block["payload"],
            )
            for block in blocks
        ]
    )


class Migration(migrations.Migration):
    dependencies = [("storefront", "0098_sqlite_generated_fit_identity")]

    operations = [migrations.RunPython(publish_stage_three_report, migrations.RunPython.noop)]
