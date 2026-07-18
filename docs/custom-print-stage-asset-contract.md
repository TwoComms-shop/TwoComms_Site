# Custom Print Stage Asset Contract

> **Obsolete for new artwork.** This document describes the retired SVG-era
> stage. Use
> [`docs/plans/custom-print-audit/png-garment-asset-spec-2026-07-18.md`](plans/custom-print-audit/png-garment-asset-spec-2026-07-18.md)
> for all new garment renders. The current production canvas is `1200 x 1400`
> RGBA PNG; do not export new assets from the dimensions below.

Этот документ нужен, если силуэты для блока `custom-print` будет рисовать дизайнер, а не генерировать кодом.

## Что уже работает сейчас

- Сцена строится через inline SVG и отдельную metadata-конфигурацию якорей.
- Функциональность зон определяется не «по картинке», а по anchor-данным.
- Значит, дизайнерский ассет можно заменить без переписывания бизнес-логики, если сохранить единый контракт.

## Рекомендуемый формат

Предпочтительный формат:

- `SVG`
- один файл на один силуэт
- один силуэт = одна комбинация `product × fit × view`

Примеры:

- `hoodie-regular-front.svg`
- `hoodie-regular-back.svg`
- `hoodie-oversize-front.svg`
- `hoodie-oversize-back.svg`
- `tshirt-regular-front.svg`
- `tshirt-oversize-back.svg`

Допустимый fallback:

- `PNG` с прозрачным фоном

Но важно:

- PNG можно использовать только как визуальный слой
- функциональные зоны всё равно должны задаваться отдельной anchor-конфигурацией
- автоопределение «где рукав, где грудь, где спина» по пикселям не используется

## Базовый canvas

Для совместимости с текущей сценой:

- `viewBox="0 0 420 520"` для SVG
- если PNG, то экспорт в пропорции `420 × 520`
- production-ready размер для PNG: `2100 × 2600`

Это позволяет без пересчёта использовать те же anchor-координаты в процентах.

## Что должно быть внутри SVG

Нужно:

- только сам силуэт изделия
- отдельные path/group для основных частей изделия
- чистый контур без текстовых подписей зон
- без встроенных прямоугольников принта
- без baked-in badge, label, A2/A6 и т.п.

Желательно:

- отдельные группы для `base`, `top`, `shade`, `detail`
- спокойная детализация: швы, манжеты, горловина, капюшон, низ изделия
- oversize должен быть визуально шире, с более спущенным плечом и другим рукавом
- classic должен быть компактнее и собраннее

## Как определяется зона печати

Зона печати определяется anchor-метаданными.

Для каждой комбинации `product × fit × view` нужны anchor-описания:

```json
{
  "view_box": "0 0 420 520",
  "anchors": {
    "front": {
      "button": { "x": 50, "y": 35 },
      "presets": {
        "A6": { "x": 50, "y": 44, "width": 16, "height": 11, "rotate": 0, "radius": 18, "shape": "panel" },
        "A5": { "x": 50, "y": 45, "width": 23, "height": 15, "rotate": 0, "radius": 20, "shape": "panel" },
        "A4": { "x": 50, "y": 46, "width": 31, "height": 21, "rotate": 0, "radius": 22, "shape": "panel" }
      }
    },
    "back": {
      "button": { "x": 50, "y": 34 },
      "presets": {
        "A4": { "x": 50, "y": 47, "width": 27, "height": 34, "rotate": 0, "radius": 22, "shape": "panel" },
        "A3": { "x": 50, "y": 50, "width": 33, "height": 41, "rotate": 0, "radius": 23, "shape": "panel" },
        "A2": { "x": 50, "y": 53, "width": 40, "height": 49, "rotate": 0, "radius": 24, "shape": "panel" }
      }
    },
    "sleeve_left": {
      "button": { "x": 23, "y": 46 },
      "modes": {
        "a6": { "x": 25, "y": 52, "width": 11, "height": 17, "rotate": -20, "radius": 17, "shape": "sleeve_patch" },
        "full_text": { "x": 21, "y": 60, "width": 8.4, "height": 39, "rotate": -18, "radius": 18, "shape": "sleeve_text" }
      }
    },
    "sleeve_right": {
      "button": { "x": 77, "y": 46 },
      "modes": {
        "a6": { "x": 75, "y": 52, "width": 11, "height": 17, "rotate": 20, "radius": 17, "shape": "sleeve_patch" },
        "full_text": { "x": 79, "y": 60, "width": 8.4, "height": 39, "rotate": 18, "radius": 18, "shape": "sleeve_text" }
      }
    }
  }
}
```

## Что означают координаты

Все координаты сейчас задаются в процентах внутри canvas:

- `x`, `y` — центр зоны
- `width`, `height` — размер рабочей области принта
- `rotate` — угол наклона
- `radius` — скругление
- `shape` — тип пластины

Поддерживаемые `shape`:

- `panel`
- `sleeve_patch`
- `sleeve_text`
- `custom_panel`

## Как дизайнеру готовить силуэт правильно

Для каждого вида изделия:

1. Нарисовать силуэт без текста и без зоны принта.
2. Сохранить чистый `SVG`.
3. Проверить, что изделие оптически центрировано в `420 × 520`.
4. Оставить поля вокруг изделия, чтобы влезали pin/hotspot и badge.
5. Отдельно прислать anchor-карту:
   - front A6/A5/A4
   - back A4/A3/A2
   - sleeve left A6/full_text
   - sleeve right A6/full_text

## Важные ограничения

- Не вшивать подписи прямо в изображение.
- Не делать зону принта частью самого силуэта.
- Не рисовать принт до края рукава или до шва.
- Для oversize не копировать classic и просто масштабировать.
- Для back A2 зона должна быть реально ниже и крупнее, а не просто чуть больше A3.

## Если нужен PNG вместо SVG

Минимальный контракт такой:

- прозрачный фон
- размер `2100 × 2600`
- тот же центр композиции, что у `420 × 520`
- отдельно обязательно прислать anchor-карту в процентах

Иначе картинка будет только декоративной, но не функциональной.

## Как подключать новый ассет в код

Нужно обновить:

- `twocomms/storefront/custom_print_config.py`

Что меняется:

- `svg_markup` соответствующего профиля
- `view_box`, если дизайнер сознательно меняет координатную систему
- anchors для `front/back/sleeve_left/sleeve_right/custom`

Если сохранить `viewBox 0 0 420 520`, достаточно заменить только SVG path’ы и координаты anchor’ов.
