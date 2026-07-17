/**
 * Lightweight orchestrator for third-party analytics.
 * Loads GA4, Microsoft Clarity and Meta Pixel after initial rendering
 * to minimise impact on FCP/TTI.
 */
(function (win, doc) {
  'use strict';

  if (!doc || !doc.documentElement) {
    return;
  }

  var root = doc.documentElement;
  var GA_ID = root.getAttribute('data-ga-id');
  var CLARITY_ID = root.getAttribute('data-clarity-id');
  var PIXEL_ID = root.getAttribute('data-meta-pixel-id');
  var TIKTOK_PIXEL_ID = root.getAttribute('data-tiktok-pixel-id');
  var TIKTOK_TEST_EVENT_CODE = root.getAttribute('data-tiktok-test-event-code') || 
                                 (function() {
                                   // Также проверяем URL параметр для быстрого тестирования
                                   var params = new URLSearchParams(win.location.search);
                                   return params.get('ttq_test') || null;
                                 })();
  var YM_ID = root.getAttribute('data-ym-id');

  function schedule(fn, timeout) {
    if (!fn) {
      return;
    }
    if ('requestIdleCallback' in win) {
      win.requestIdleCallback(fn, { timeout: timeout || 2000 });
      return;
    }
    var invoked = false;
    function run() {
      if (invoked) {
        return;
      }
      invoked = true;
      fn();
    }
    if (doc.readyState === 'complete') {
      setTimeout(run, timeout || 0);
    } else {
      win.addEventListener('load', run, { once: true });
      setTimeout(run, timeout || 2000);
    }
  }

  // Event buffer to queue events before pixel loads
  win._fbqBuffer = win._fbqBuffer || [];
  win._fbqLoaded = false;
  win._ttqBuffer = win._ttqBuffer || [];
  win._ttqLoaded = false;
  win._ttqScriptLoaded = false; // Флаг реальной загрузки скрипта TikTok

  function getCookieValue(name) {
    if (!name || !doc || !doc.cookie) {
      return '';
    }
    var cookies = doc.cookie.split(';');
    for (var i = 0; i < cookies.length; i++) {
      var cookie = cookies[i].trim();
      if (!cookie) continue;
      if (cookie.indexOf(name + '=') === 0) {
        return decodeURIComponent(cookie.substring(name.length + 1));
      }
    }
    return '';
  }
  
  function setCookieValue(name, value, days) {
    if (!name || !doc) {
      return;
    }
    var expires = '';
    if (days) {
      var date = new Date();
      date.setTime(date.getTime() + (days * 24 * 60 * 60 * 1000));
      expires = '; expires=' + date.toUTCString();
    }
    var cookieString = name + '=' + encodeURIComponent(value) + expires + '; path=/; SameSite=Lax';
    if (win.location && win.location.protocol === 'https:') {
      cookieString += '; Secure';
    }
    doc.cookie = cookieString;
  }
  
  function generateEventId() {
    // Генерирует уникальный event_id для дедупликации Pixel ↔ CAPI
    // Format: timestamp_random
    var timestamp = Date.now();
    var random = Math.random().toString(36).substring(2, 11);
    return timestamp + '_' + random;
  }
  
  function ensureFbpCookie() {
    // Создает _fbp cookie если его нет (Meta Pixel Browser ID)
    var fbp = getCookieValue('_fbp');
    if (!fbp) {
      // Format: fb.1.timestamp.random
      var timestamp = Date.now();
      var random = Math.random().toString(36).substring(2, 15);
      fbp = 'fb.1.' + timestamp + '.' + random;
      setCookieValue('_fbp', fbp, 90); // 90 дней
    }
    return fbp;
  }
  
  function ensureFbcCookie() {
    // Создает _fbc cookie из fbclid параметра URL (Meta Pixel Click ID)
    var fbc = getCookieValue('_fbc');
    if (fbc) {
      return fbc; // Уже есть
    }
    
    // Парсим fbclid из URL
    try {
      var params = new URLSearchParams(win.location.search);
      var fbclid = params.get('fbclid');
      if (fbclid) {
        // Format: fb.1.timestamp.fbclid
        var timestamp = Date.now();
        fbc = 'fb.1.' + timestamp + '.' + fbclid;
        setCookieValue('_fbc', fbc, 90); // 90 дней
        return fbc;
      }
    } catch (e) {
      // Fallback для старых браузеров
      var search = win.location.search;
      if (search) {
        var match = search.match(/[?&]fbclid=([^&]+)/);
        if (match && match[1]) {
          var timestamp = Date.now();
          fbc = 'fb.1.' + timestamp + '.' + match[1];
          setCookieValue('_fbc', fbc, 90);
          return fbc;
        }
      }
    }
    
    return null;
  }

  // Экспортируем функции для использования в других скриптах
  win.generateEventId = generateEventId;
  win.getTrackingContext = function() {
    return {
      fbp: ensureFbpCookie(),
      fbc: ensureFbcCookie() || getCookieValue('_fbc') || null
    };
  };
  // event_id НЕ передается в tracking context — он генерируется при отправке событий
  // (Purchase/Lead) для корректной дедупликации между браузером и сервером
  
  function setupGlobalEventBridge() {
    // FIX 2026-06-12: base.html ставит ранний стаб trackEvent с очередью
    // (__queueStub) — его нужно заменить реальной функцией. Выходим только
    // если установлен уже НЕ-стабовый trackEvent (повторная загрузка loader).
    if (typeof win.trackEvent === 'function' && !win.trackEvent.__queueStub) {
      return;
    }
    win.YM_ID = YM_ID ? parseInt(YM_ID, 10) || 0 : 0;
    
    // Инициализация fbp/fbc cookies при загрузке
    ensureFbpCookie();
    ensureFbcCookie();
    
    win.trackEvent = function (eventName, payload) {
      if (!eventName) {
        return;
      }
      payload = payload || {};

      var metaConfig = (payload.__meta && typeof payload.__meta === 'object') ? payload.__meta : {};
      
      // Генерируем event_id если не передан (КРИТИЧНО для дедупликации)
      var eventId = metaConfig.event_id || payload.event_id || null;
      if (!eventId) {
        eventId = generateEventId();
      }
      
      var externalId = metaConfig.external_id || payload.external_id || null;
      var fbUserData = metaConfig.user_data || payload.fb_user_data || null;
      
      // Обновляем fbp/fbc при каждом событии (могли измениться)
      var fbpValue = metaConfig.fbp || payload.fbp || ensureFbpCookie();
      var fbcValue = metaConfig.fbc || payload.fbc || ensureFbcCookie() || getCookieValue('_fbc');

      // Обновляем meta для дальнейшего использования (например, сервер)
      metaConfig.event_id = eventId;
      if (fbpValue) {
        metaConfig.fbp = fbpValue;
      }
      if (fbcValue) {
        metaConfig.fbc = fbcValue;
      }
      if (externalId && !metaConfig.external_id) {
        metaConfig.external_id = externalId;
      }
      payload.__meta = metaConfig;
      payload.event_id = eventId;
      if (fbpValue && !payload.fbp) {
        payload.fbp = fbpValue;
      }
      if (fbcValue && !payload.fbc) {
        payload.fbc = fbcValue;
      }

      var cleanPayload = {};
      for (var originalKey in payload) {
        if (!payload.hasOwnProperty(originalKey)) {
          continue;
        }
        if (originalKey === '__meta' || originalKey === 'fb_user_data' || originalKey === 'fb_user_data_hashed') {
          continue;
        }
        if (originalKey === 'external_id' || originalKey === 'fbp' || originalKey === 'fbc') {
          // Уже извлекли выше
          continue;
        }
        cleanPayload[originalKey] = payload[originalKey];
      }
      payload = cleanPayload;
      
      // Meta Pixel standard e-commerce events that support value/currency
      var ecommerceEvents = [
        'ViewContent', 'AddToCart', 'InitiateCheckout', 'Purchase',
        'AddPaymentInfo', 'AddToWishlist', 'RemoveFromWishlist', 'Lead', 
        'CompleteRegistration', 'Subscribe', 'StartTrial', 'Search', 'CustomizeProduct'
      ];
      var isEcommerceEvent = ecommerceEvents.indexOf(eventName) !== -1;
      var standardMetaEvents = {
        PageView: true,
        AddPaymentInfo: true,
        AddToCart: true,
        AddToWishlist: true,
        CompleteRegistration: true,
        Contact: true,
        CustomizeProduct: true,
        Donate: true,
        FindLocation: true,
        InitiateCheckout: true,
        Lead: true,
        Purchase: true,
        Schedule: true,
        Search: true,
        StartTrial: true,
        SubmitApplication: true,
        Subscribe: true,
        ViewContent: true
      };
      var metaTrackMethod = standardMetaEvents[eventName] ? 'track' : 'trackCustom';
      
      // Validate and sanitize payload for Meta Pixel
      var fbPayload = {};
      var fbSkipKeys = {
        '__meta': true,
        'event_id': true,
        'external_id': true,
        'fbp': true,
        'fbc': true,
        'fb_user_data': true,
        'fb_user_data_hashed': true
      };
      for (var key in payload) {
        if (payload.hasOwnProperty(key)) {
          var value = payload[key];
          if (fbSkipKeys[key]) {
            continue;
          }
          // Skip undefined/null values
          if (value === undefined || value === null) {
            continue;
          }
          // Only include value/currency for e-commerce events
          if (key === 'value' || key === 'currency') {
            if (!isEcommerceEvent) {
              continue; // Skip these params for non-ecommerce events
            }
          }
          // Validate numeric values
          if (key === 'value') {
            var numValue = parseFloat(value);
            if (!isNaN(numValue) && numValue >= 0) {
              fbPayload[key] = numValue;
            }
          }
          // Validate currency
          else if (key === 'currency') {
            if (typeof value === 'string' && value.length > 0) {
              fbPayload[key] = value.toUpperCase();
            }
          }
          // Copy other values as-is
          else {
            fbPayload[key] = value;
          }
        }
      }
      
      // Buffer Meta Pixel events until loaded
      // Защита от блокировки: проверяем что Pixel действительно доступен
      try {
        if (PIXEL_ID && win.fbq && typeof win.fbq === 'function') {
          // Проверяем что Pixel инициализирован
          if (win._fbqLoaded) {
            try {
              var metaOptions = {};
              if (eventId) {
                metaOptions.eventID = String(eventId);
              }
              if (externalId) {
                metaOptions.external_id = String(externalId);
              }
              if (fbpValue) {
                metaOptions.fbp = String(fbpValue);
              }
              if (fbcValue) {
                metaOptions.fbc = String(fbcValue);
              }
              if (fbUserData && typeof fbUserData === 'object') {
                metaOptions.user_data = Object.assign({}, fbUserData);
              }
              var hasOptions = Object.keys(metaOptions).length > 0;
              if (hasOptions) {
                win.fbq(metaTrackMethod, eventName, fbPayload, metaOptions);
              } else {
                win.fbq(metaTrackMethod, eventName, fbPayload);
              }
            } catch (fbErr) {
              if (console && console.debug) {
                console.debug('Meta Pixel track error (possible blocker):', fbErr);
              }
              // Fallback: буферизуем событие на случай если это временная ошибка
              if (win._fbqBuffer) {
                win._fbqBuffer.push({ event: eventName, data: fbPayload, options: metaOptions, custom: metaTrackMethod === 'trackCustom' });
              }
            }
        } else if (PIXEL_ID) {
            // Pixel еще не загружен - буферизуем
            if (!win._fbqBuffer) {
              win._fbqBuffer = [];
            }
          win._fbqBuffer.push({ event: eventName, data: fbPayload, custom: metaTrackMethod === 'trackCustom', options: {
            eventID: eventId ? String(eventId) : undefined,
            external_id: externalId ? String(externalId) : undefined,
            fbp: fbpValue ? String(fbpValue) : undefined,
            fbc: fbcValue ? String(fbcValue) : undefined,
            user_data: fbUserData && typeof fbUserData === 'object' ? Object.assign({}, fbUserData) : undefined
          }});
          }
        }
      } catch (err1) {
        if (console && console.debug) {
          console.debug('Meta Pixel track error (possible blocker):', err1);
        }
      }
      
      // Отправка в Google Analytics через dataLayer (работает с GTM и GA4)
      try {
        // ИСПРАВЛЕНИЕ: Используем существующий dataLayer от GTM
        win.dataLayer = win.dataLayer || [];
        
        // Если есть gtag - используем его (GA4 direct)
        if (win.gtag && typeof win.gtag === 'function') {
          win.gtag('event', eventName, payload);
        } else {
          // Fallback: отправляем напрямую в dataLayer (GTM подхватит)
          win.dataLayer.push({
            'event': eventName,
            'eventParameters': payload
          });
        }
      } catch (err2) {
        if (console && console.debug) {
          console.debug('GA/GTM track error (possible blocker):', err2);
        }
      }
      // Отправка в Yandex Metrika (если подключена)
      try {
        if (win.ym && typeof win.ym === 'function' && win.YM_ID) {
          win.ym(win.YM_ID, 'reachGoal', eventName, payload);
        }
      } catch (err3) {
        if (console && console.debug) {
          console.debug('YM track error (possible blocker):', err3);
        }
      }
      
      // Отправка в TikTok Pixel (с полной структурой contents)
      try {
        if (TIKTOK_PIXEL_ID) {
          // W2-6 (AN-020): TikTok-цели видят только СТАНДАРТНЫЕ имена
          // событий. Meta-имена маппим: Purchase → CompletePayment,
          // Lead → PlaceAnOrder. event_id НЕ меняется — серверный слой
          // (tiktok_events_service.py) маппит так же → дедуп сохраняется.
          var ttqEventName = mapTikTokEventName(eventName);
          // Проверяем что пиксель реально загружен (не только очередь)
          var isTikTokReady = win.ttq && 
                             typeof win.ttq.track === 'function' && 
                             win._ttqLoaded && 
                             win._ttqScriptLoaded; // Дополнительная проверка что скрипт загрузился
          
          if (isTikTokReady) {
            try {
              // Преобразуем payload в формат TikTok
              var ttqPayload = buildTikTokPayload(eventName, payload, isEcommerceEvent);
              if (eventId) {
                ttqPayload.event_id = String(eventId);
              }
              
              // Логируем отправку события для отладки
              if (console && console.log) {
                console.log('[TikTok Pixel] Sending event:', ttqEventName, ttqPayload);
              }
              
              // Отправляем событие напрямую (пиксель уже готов)
              // Используем try-catch для безопасности
              win.ttq.track(ttqEventName, ttqPayload);
              
              if (console && console.log) {
                console.log('[TikTok Pixel] Event sent successfully:', ttqEventName);
              }
            } catch (ttqErr) {
              if (console && console.debug) {
                console.debug('TikTok Pixel track error (possible blocker):', ttqErr);
              }
              // Если ошибка - буферизуем для повторной отправки
              var ttqBufferedPayload = buildTikTokPayload(eventName, payload, isEcommerceEvent);
              if (eventId) {
                ttqBufferedPayload.event_id = String(eventId);
              }
              if (!win._ttqBuffer) {
                win._ttqBuffer = [];
              }
              win._ttqBuffer.push({ event: ttqEventName, data: ttqBufferedPayload });
            }
          } else {
            // TikTok Pixel еще не загружен - буферизуем
            var ttqBufferedPayload = buildTikTokPayload(eventName, payload, isEcommerceEvent);
            if (eventId) {
              ttqBufferedPayload.event_id = String(eventId);
            }
            if (!win._ttqBuffer) {
              win._ttqBuffer = [];
            }
            win._ttqBuffer.push({ event: ttqEventName, data: ttqBufferedPayload });
            
            if (console && console.log) {
              console.log('[TikTok Pixel] Event buffered (pixel not ready):', ttqEventName, 'Total buffered:', win._ttqBuffer.length);
            }
            
            // Пытаемся загрузить пиксель если он еще не начал загружаться
            if (!win.__ttqPixelLoaded) {
              if (console && console.log) {
                console.log('[TikTok Pixel] Pixel not loaded yet, initializing...');
              }
              loadTikTokPixel();
            }
          }
        }
      } catch (err4) {
        if (console && console.debug) {
          console.debug('TikTok Pixel track error (possible blocker):', err4);
        }
      }
    };

    // FIX 2026-06-12: дренируем очередь вызовов, накопленных стабом из
    // base.html до загрузки loader-а (ViewContent/AddToCart/Lead и т.д.).
    try {
      var pendingCalls = win._trackEventQueue;
      if (pendingCalls && pendingCalls.length) {
        for (var pq = 0; pq < pendingCalls.length; pq++) {
          try {
            win.trackEvent.apply(null, pendingCalls[pq]);
          } catch (drainErr) {
            if (console && console.debug) {
              console.debug('trackEvent queue drain error:', drainErr);
            }
          }
        }
        win._trackEventQueue = [];
      }
    } catch (_) { }
  }
  
  // W2-6 (AN-020): Meta-имена → стандартные имена TikTok (цели TikTok
  // распознают только стандартные). Остальные имена (ViewContent,
  // AddToCart, InitiateCheckout, Search, CompleteRegistration, Contact)
  // совпадают в обеих системах и проходят как есть.
  function mapTikTokEventName(eventName) {
    var map = {
      'Purchase': 'CompletePayment',
      'Lead': 'PlaceAnOrder'
    };
    return map[eventName] || eventName;
  }

  // Функция для преобразования payload в формат TikTok
  function buildTikTokPayload(eventName, payload, isEcommerceEvent) {
    var ttqPayload = {};
    
    // Базовые поля для e-commerce событий
    if (isEcommerceEvent) {
      // КРИТИЧНО: TikTok требует чтобы value был непустым
      var calculatedValue = 0;
      
      // Если value явно указан - используем его
      if (payload.value !== undefined && payload.value !== null && payload.value !== '') {
        calculatedValue = parseFloat(payload.value);
      }
      
      // Если value не указан или 0, но есть contents - вычисляем из contents
      if ((calculatedValue === 0 || isNaN(calculatedValue)) && payload.contents && Array.isArray(payload.contents)) {
        for (var calcIdx = 0; calcIdx < payload.contents.length; calcIdx++) {
          var item = payload.contents[calcIdx];
          var itemPrice = parseFloat(item.item_price || item.price || 0);
          var itemQty = parseInt(item.quantity || item.num_items || 1, 10);
          calculatedValue += itemPrice * itemQty;
        }
      }
      
      // Если все еще 0 - используем минимальное значение (TikTok требует непустое)
      if (calculatedValue === 0 || isNaN(calculatedValue)) {
        calculatedValue = 0.01; // Минимальное значение чтобы TikTok не ругался
      }
      
      // Всегда передаем value как строку для TikTok (TikTok требует строку)
      ttqPayload.value = String(calculatedValue);
      
      // Currency обязателен для e-commerce событий
      ttqPayload.currency = (payload.currency || 'UAH').toUpperCase();
    }
    
    // Преобразуем contents array в формат TikTok
    if (payload.contents && Array.isArray(payload.contents)) {
      ttqPayload.contents = [];
      for (var i = 0; i < payload.contents.length; i++) {
        var content = payload.contents[i];
        var ttqContent = {
          content_id: content.id || content.content_id || '',
          content_type: payload.content_type || 'product',
          content_name: payload.content_name || content.item_name || '',
          content_category: payload.content_category || content.item_category || '',
          price: content.item_price || content.price || 0,
          brand: 'TwoComms'
        };
        
        // Добавляем количество если есть
        if (content.quantity !== undefined) {
          ttqContent.num_items = content.quantity;
        }
        
        ttqPayload.contents.push(ttqContent);
      }
    } else if (payload.content_ids && Array.isArray(payload.content_ids)) {
      // Fallback: если нет contents но есть content_ids
      ttqPayload.contents = [];
      for (var j = 0; j < payload.content_ids.length; j++) {
        ttqPayload.contents.push({
          content_id: payload.content_ids[j],
          content_type: payload.content_type || 'product',
          content_name: payload.content_name || '',
          content_category: payload.content_category || '',
          price: payload.value || 0,
          brand: 'TwoComms'
        });
      }
    }
    
    // Добавляем search_string для Search события
    if (payload.search_string) {
      ttqPayload.search_string = payload.search_string;
    }
    
    // Добавляем description (опционально)
    if (payload.description) {
      ttqPayload.description = payload.description;
    }
    
    // Копируем другие параметры если они есть
    var keysToSkip = ['value', 'currency', 'contents', 'content_ids', 'content_name', 'content_category', 'content_type', 'search_string', 'description', 'items'];
    for (var key in payload) {
      if (payload.hasOwnProperty(key) && keysToSkip.indexOf(key) === -1) {
        ttqPayload[key] = payload[key];
      }
    }
    
    return ttqPayload;
  }
  
  // Функция для TikTok identify (Advanced Matching)
  function ttqIdentify() {
    if (!TIKTOK_PIXEL_ID || !win.ttq || typeof win.ttq.identify !== 'function') {
      return;
    }
    
    try {
      var amEl = doc.getElementById('am');
      if (!amEl) return;
      
      var identifyData = {};
      
      // Email (хешированный) - только если валидный
      if (amEl.dataset.em) {
        var email = String(amEl.dataset.em).trim();
        if (isValidEmail(email)) {
          var hashedEmail = hashSHA256(email);
          if (hashedEmail) {
            identifyData.email = hashedEmail;
          }
        } else {
          if (console && console.debug) {
            console.debug('[TikTok Pixel] Invalid email skipped:', email);
          }
        }
      }
      
      // Phone (хешированный) - только если валидный
      if (amEl.dataset.ph) {
        var phoneRaw = String(amEl.dataset.ph);
        // Только цифры
        var phoneDigits = normalizePhoneDigits(phoneRaw);
        if (isValidPhone(phoneDigits)) {
          var hashedPhone = hashSHA256(phoneDigits);
          if (hashedPhone) {
            identifyData.phone_number = hashedPhone;
          }
        } else {
          if (console && console.debug) {
            console.debug('[TikTok Pixel] Invalid phone skipped (length:', phoneDigits.length, '):', phoneRaw);
          }
        }
      }
      
      // External ID (хешированный) - только если есть
      if (amEl.dataset.externalId) {
        var externalId = String(amEl.dataset.externalId).trim();
        if (externalId) {
          var hashedExternalId = hashSHA256(externalId);
          if (hashedExternalId) {
            identifyData.external_id = hashedExternalId;
          }
        }
      }
      
      // КРИТИЧНО: Отправляем только если есть хотя бы одно валидное поле
      // События TikTok/Meta работают БЕЗ Advanced Matching - это только улучшает матчинг
      if (Object.keys(identifyData).length > 0) {
        win.ttq.identify(identifyData);
        if (console && console.log) {
          console.log('[TikTok Pixel] identify sent with fields:', Object.keys(identifyData).join(', '));
        }
      } else {
        if (console && console.debug) {
          console.debug('[TikTok Pixel] identify skipped - no valid data (guest user or invalid data)');
        }
      }
    } catch (err) {
      if (console && console.debug) {
        console.debug('TikTok identify error:', err);
      }
    }
  }
  
  // Простая реализация SHA-256 (https://en.wikipedia.org/wiki/SHA-2)
  function sha256(ascii) {
    var mathPow = Math.pow;
    var maxWord = mathPow(2, 32);
    var result = '';
    var words = [];
    var asciiBitLength = ascii.length * 8;
    var hash = sha256.h = sha256.h || [];
    var k = sha256.k = sha256.k || [];
    var primeCounter = k.length;
    var isComposite = {};
    for (var candidate = 2; primeCounter < 64; candidate++) {
      if (!isComposite[candidate]) {
        for (var i = 0; i < 313; i += candidate) {
          isComposite[i] = candidate;
        }
        hash[primeCounter] = (mathPow(candidate, 0.5) * maxWord) | 0;
        k[primeCounter++] = (mathPow(candidate, 1 / 3) * maxWord) | 0;
      }
    }
    ascii += '\u0080';
    while (ascii.length % 64 - 56) {
      ascii += '\u0000';
    }
    for (var i = 0; i < ascii.length; i++) {
      var j = ascii.charCodeAt(i);
      words[i >> 2] |= j << ((3 - i) % 4) * 8;
    }
    words[words.length] = ((asciiBitLength / maxWord) | 0);
    words[words.length] = (asciiBitLength);
    for (var j = 0; j < words.length;) {
      var w = words.slice(j, j += 16);
      var oldHash = hash.slice(0);
      for (var i = 0; i < 64; i++) {
        var w15 = w[i - 15];
        var w2 = w[i - 2];
        var s0 = w15 ? ((w15 >>> 7) | (w15 << 25)) ^ ((w15 >>> 18) | (w15 << 14)) ^ (w15 >>> 3) : 0;
        var s1 = w2 ? ((w2 >>> 17) | (w2 << 15)) ^ ((w2 >>> 19) | (w2 << 13)) ^ (w2 >>> 10) : 0;
        w[i] = (i < 16 ? w[i] : (w[i - 16] + s0 + w[i - 7] + s1) | 0);
        var a = hash[0];
        var e = hash[4];
        var temp1 = (hash[7] + (((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7))) + ((e & hash[5]) ^ ((~e) & hash[6])) + k[i] + w[i]) | 0;
        var temp2 = ((((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10))) + ((a & hash[1]) ^ (a & hash[2]) ^ (hash[1] & hash[2]))) | 0;
        hash = [(temp1 + temp2) | 0].concat(hash);
        hash[4] = (hash[4] + temp1) | 0;
      }
      for (var i = 0; i < 8; i++) {
        hash[i] = (hash[i] + oldHash[i]) | 0;
      }
    }
    for (var i = 0; i < 8; i++) {
      for (var j = 3; j + 1; j--) {
        var b = (hash[i] >> (j * 8)) & 255;
        result += ((b < 16 ? 0 : '') + b.toString(16));
      }
    }
    return result;
  }

  // Функция для хеширования SHA-256
  function hashSHA256(str) {
    if (!str || typeof str !== 'string') {
      return null;
    }
    var cleaned = str.trim().toLowerCase();
    if (!cleaned) {
      return null;
    }
    
    try {
      return sha256(cleaned);
    } catch (err) {
      if (console && console.debug) {
        console.debug('SHA-256 hashing failed, fallback to raw value', err);
      }
      return cleaned;
    }
  }
  win.__hashSHA256 = hashSHA256;
  var GUEST_STORAGE_PREFIX = '_twc_guest_';
  var GUEST_DATA_FIELDS = ['full_name', 'phone', 'city', 'email'];

  function readSessionStorageValue(key) {
    if (!key) {
      return '';
    }
    try {
      if (win.sessionStorage) {
        return win.sessionStorage.getItem(key) || '';
      }
    } catch (_) {}
    return '';
  }

  function getGuestDataFromStorage() {
    var data = {};
    GUEST_DATA_FIELDS.forEach(function(field) {
      var value = readSessionStorageValue(GUEST_STORAGE_PREFIX + field);
      if (value) {
        data[field] = value;
      }
    });
    return data;
  }

  function normalizeCityValue(raw) {
    if (!raw) {
      return '';
    }
    return String(raw).trim().toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
  }

  function getFallbackSessionId() {
    try {
      if (win.localStorage) {
        var key = '_twc_temp_session_id';
        var existing = win.localStorage.getItem(key);
        if (existing) {
          return existing;
        }
        var generated = 'temp_' + Date.now() + '_' + Math.random().toString(36).substring(2, 11);
        win.localStorage.setItem(key, generated);
        return generated;
      }
    } catch (_) {}
    return null;
  }

  function buildUserDataForEvent() {
    var result = {
      user_data: {},
      fbp: ensureFbpCookie() || null,
      fbc: ensureFbcCookie() || getCookieValue('_fbc') || null,
      external_id: null
    };
    var hashFn = win.__hashSHA256;
    if (!hashFn || typeof hashFn !== 'function') {
      return result;
    }
    var amEl = doc.getElementById('am');
    var attrs = (amEl && amEl.dataset) ? amEl.dataset : {};
    var guestData = getGuestDataFromStorage();

    var emailValue = (attrs.em || guestData.email || '').trim().toLowerCase();
    if (emailValue && emailValue.indexOf('@') > -1) {
      var hashedEmail = hashFn(emailValue);
      if (hashedEmail) {
        result.user_data.em = hashedEmail;
      }
    }

    var phoneValue = attrs.ph || guestData.phone || '';
    if (phoneValue) {
      var phoneDigits = normalizePhoneDigits(phoneValue);
      if (phoneDigits && phoneDigits.length >= 7) {
        var hashedPhone = hashFn(phoneDigits);
        if (hashedPhone) {
          result.user_data.ph = hashedPhone;
        }
      }
    }

    var fullNameValue = attrs.fn || guestData.full_name || '';
    if (fullNameValue) {
      var nameParts = String(fullNameValue).trim().split(/\s+/);
      if (nameParts.length >= 1 && nameParts[0]) {
        var first = hashFn(nameParts[0].toLowerCase());
        if (first) {
          result.user_data.fn = first;
        }
      }
      if (nameParts.length >= 2) {
        var lastPart = nameParts[nameParts.length - 1];
        if (lastPart) {
          var last = hashFn(lastPart.toLowerCase());
          if (last) {
            result.user_data.ln = last;
          }
        }
      }
    }

    var cityValue = normalizeCityValue(attrs.ct || guestData.city || '');
    if (cityValue) {
      var hashedCity = hashFn(cityValue);
      if (hashedCity) {
        result.user_data.ct = hashedCity;
      }
    }

    var externalSource = null;
    if (attrs.externalId) {
      var ext = String(attrs.externalId).trim();
      if (ext) {
        externalSource = ext.indexOf(':') === -1 ? 'user:' + ext : ext;
      }
    }
    if (!externalSource) {
      var sessionId = getCookieValue('sessionid');
      if (sessionId) {
        externalSource = 'session:' + sessionId;
      }
    }
    if (!externalSource) {
      var fallback = getFallbackSessionId();
      if (fallback) {
        externalSource = 'session:' + fallback;
      }
    }
    if (externalSource) {
      var hashedExternal = hashFn(externalSource.toLowerCase());
      if (hashedExternal) {
        result.external_id = hashedExternal;
      }
    }

    return result;
  }
  win.buildUserDataForEvent = buildUserDataForEvent;
  
  // Валидация email
  function isValidEmail(email) {
    if (!email || typeof email !== 'string') {
      return false;
    }
    // Простая валидация email
    var emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    return emailRegex.test(email.trim().toLowerCase());
  }
  
  // Валидация телефона - минимум 10 цифр, максимум 15
  function isValidPhone(phone) {
    if (!phone || typeof phone !== 'string') {
      return false;
    }
    // Удаляем все нецифровые символы
    var digits = phone.replace(/\D/g, '');
    // Проверяем длину: минимум 10 цифр, максимум 15 (стандарт E.164)
    return digits.length >= 10 && digits.length <= 15;
  }

  function normalizePhoneDigits(phone) {
    var digits = String(phone || '').replace(/\D/g, '');
    if (digits.length === 10 && digits.charAt(0) === '0') {
      return '380' + digits.substring(1);
    }
    return digits;
  }

  function loadGoogleAnalytics() {
    if (!GA_ID || win.__gaLoaded) {
      return;
    }
    
    // ИСПРАВЛЕНИЕ: Используем существующий dataLayer от GTM (не создаем новый)
    // GTM уже создает dataLayer в base.html, используем его
    win.dataLayer = win.dataLayer || [];
    
    // Создаем gtag функцию если её еще нет (GTM может её не создать)
    if (!win.gtag) {
      win.gtag = function () {
        win.dataLayer.push(arguments);
      };
    }

    // Защита от блокировки: проверяем что скрипт действительно загрузился
    var script = doc.createElement('script');
    script.async = true;
    script.src = 'https://www.googletagmanager.com/gtag/js?id=' + GA_ID;
    script.dataset.loader = 'ga4';
    
    var loadTimeout = setTimeout(function() {
      if (!win.__gaLoaded) {
        console.debug('GA4 script load timeout - possible ad blocker');
      }
    }, 5000);
    
    script.onload = function () {
      clearTimeout(loadTimeout);
      // Проверяем что gtag действительно работает
      try {
      win.__gaLoaded = true;
      win.gtag('js', new Date());
      win.gtag('config', GA_ID, {
        send_page_view: false,
      });
      win.gtag('event', 'page_view', {
        page_title: doc.title,
        page_location: win.location.href,
      });
      } catch (err) {
        if (console && console.debug) {
          console.debug('GA4 initialization error (possible blocker):', err);
        }
      }
    };
    
    script.onerror = function() {
      clearTimeout(loadTimeout);
      if (console && console.debug) {
        console.debug('GA4 script failed to load - possible ad blocker');
      }
    };
    
    try {
    doc.head.appendChild(script);
    } catch (err) {
      if (console && console.debug) {
        console.debug('Failed to append GA4 script:', err);
      }
    }
  }

  function loadClarity() {
    if (!CLARITY_ID || win.__clarityLoaded) {
      return;
    }
    
    // Защита от блокировки: инициализируем Clarity с проверками
    try {
    win.__clarityLoaded = true;
    (function (c, l, a, r, i, t, y) {
      c[a] =
        c[a] ||
        function () {
          (c[a].q = c[a].q || []).push(arguments);
        };
      t = l.createElement(r);
      t.async = true;
      t.src = 'https://www.clarity.ms/tag/' + i;
        
        // Защита от блокировки: обработчик ошибок
        t.onerror = function() {
          if (console && console.debug) {
            console.debug('Clarity script failed to load - possible ad blocker');
          }
          c.__clarityLoaded = false;
        };
        
      y = l.getElementsByTagName(r)[0];
        if (y && y.parentNode) {
      y.parentNode.insertBefore(t, y);
        } else {
          // Fallback: добавляем в head
          (l.head || l.getElementsByTagName('head')[0]).appendChild(t);
        }
    })(win, doc, 'clarity', 'script', CLARITY_ID);
    } catch (err) {
      if (console && console.debug) {
        console.debug('Clarity initialization error:', err);
      }
      win.__clarityLoaded = false;
    }
  }

  function loadClarityOnFirstInteraction() {
    if (!CLARITY_ID || win.__clarityArmed) {
      return;
    }
    win.__clarityArmed = true;
    interactionEvents.forEach(function (evt) {
      win.addEventListener(evt, loadClarity, { passive: true, capture: true, once: true });
    });
  }

  function buildAdvancedMatchingMap() {
    var amEl = doc.getElementById('am');
    if (!amEl) {
      return {};
    }
    var attrs = amEl.dataset || {};
    var match = {};
    
    // ФИО (разбиваем на имя и фамилию)
    var fullName = (attrs.fn || '').trim();
    if (fullName) {
      var parts = fullName.split(/\s+/);
      if (parts.length > 1) {
        match.fn = parts[0].toLowerCase();
        match.ln = parts[parts.length - 1].toLowerCase();
      } else {
        match.fn = fullName.toLowerCase();
      }
    }
    
    // Email - только если валидный
    if (attrs.em) {
      var email = String(attrs.em).trim();
      if (isValidEmail(email)) {
        match.em = email.toLowerCase();
      } else {
        if (console && console.debug) {
          console.debug('[Meta Pixel] Invalid email skipped:', email);
    }
      }
    }
    
    // Phone - только если валидный (только цифры для Meta)
    if (attrs.ph) {
      var phoneRaw = String(attrs.ph);
      var phoneDigits = normalizePhoneDigits(phoneRaw);
      if (isValidPhone(phoneDigits)) {
        match.ph = phoneDigits;
      } else {
        if (console && console.debug) {
          console.debug('[Meta Pixel] Invalid phone skipped (length:', phoneDigits.length, '):', phoneRaw);
    }
      }
    }
    
    // External ID (нехешированный для Meta, но валидируем)
    if (attrs.externalId) {
      var externalId = String(attrs.externalId).trim();
      if (externalId) {
        match.external_id = externalId;
      }
    }
    
    // City (lowercase для Meta)
    if (attrs.ct) {
      var city = String(attrs.ct).trim();
      if (city) {
        match.ct = city.toLowerCase();
      }
    }
    
    return match;
  }

  function loadMetaPixel() {
    if (!PIXEL_ID || (win.fbq && win.__fbPixelLoaded)) {
      return;
    }
    
    // Защита от блокировки: инициализируем Pixel с проверками
    try {
    !function (f, b, e, v, n, t, s) {
      if (f.fbq) {
        return;
      }
      n = f.fbq = function () {
        n.callMethod ? n.callMethod.apply(n, arguments) : n.queue.push(arguments);
      };
      if (!f._fbq) {
        f._fbq = n;
      }
      n.push = n;
      n.loaded = true;
      n.version = '2.0';
      n.queue = [];
      t = b.createElement(e);
      t.async = true;
      t.src = v;
        
        // Защита от блокировки: добавляем onerror обработчик
        t.onerror = function() {
          if (console && console.debug) {
            console.debug('Meta Pixel script failed to load - possible ad blocker');
          }
          // Помечаем что Pixel не загружен, но продолжаем работу
          f._fbqLoaded = false;
        };
        
      s = b.getElementsByTagName(e)[0];
        if (s && s.parentNode) {
      s.parentNode.insertBefore(t, s);
        } else {
          // Fallback: добавляем в head если script[0] не найден
          (b.head || b.getElementsByTagName('head')[0]).appendChild(t);
        }
    }(win, doc, 'script', 'https://connect.facebook.net/en_US/fbevents.js');
      
    win.__fbPixelLoaded = true;
    } catch (err) {
      if (console && console.debug) {
        console.debug('Meta Pixel initialization error:', err);
      }
      win.__fbPixelLoaded = false;
    }
    // ADS-1: если инлайн-сниппет в <head> уже сделал init+PageView
    // (window.__fbPixelInline), здесь только ДОПОЛНЯЕМ advanced matching
    // повторным init (fbq дедуплицирует по pixel id) и НЕ шлём второй
    // PageView — иначе Meta посчитает страницу дважды.
    try {
      var advancedMatching = buildAdvancedMatchingMap();
      win.fbq('init', PIXEL_ID, advancedMatching);
    } catch (err) {
      if (console && console.debug) {
        console.debug('Meta Pixel init error', err);
      }
      try {
        win.fbq('init', PIXEL_ID);
      } catch (err2) {
        if (console && console.debug) {
          console.debug('Meta Pixel fallback error', err2);
        }
      }
    }
    if (!win.__fbPixelInline) {
      try {
        win.fbq('track', 'PageView');
      } catch (errTrack) {
        if (console && console.debug) {
          console.debug('Meta Pixel track error', errTrack);
        }
      }
    }
    
    // Mark pixel as loaded and process buffered events
    win._fbqLoaded = true;
    if (win._fbqBuffer && win._fbqBuffer.length > 0) {
      if (console && console.log) {
        console.log('Meta Pixel: Processing ' + win._fbqBuffer.length + ' buffered events');
      }
      win._fbqBuffer.forEach(function(buffered) {
        try {
          if (!buffered) {
            return;
          }
          var method = buffered.custom ? 'trackCustom' : 'track';
          if (buffered && buffered.options) {
            win.fbq(method, buffered.event, buffered.data, buffered.options);
          } else {
            win.fbq(method, buffered.event, buffered.data);
          }
        } catch (err) {
          if (console && console.debug) {
            console.debug('Meta Pixel buffered event error', err);
          }
        }
      });
      win._fbqBuffer = []; // Clear buffer
    }
  }

  function loadTikTokPixel() {
    if (!TIKTOK_PIXEL_ID || (win.ttq && win.__ttqPixelLoaded)) {
      return;
    }
    
    // Защита от блокировки: инициализируем TikTok Pixel с проверками
    try {
      !function (w, d, t) {
        if (w[t]) {
          return;
        }
        w.TiktokAnalyticsObject = t;
        var ttq = w[t] = w[t] || [];
        ttq.methods = ["page","track","identify","instances","debug","on","off","once","ready","alias","group","enableCookie","disableCookie","holdConsent","revokeConsent","grantConsent"];
        ttq.setAndDefer = function(t,e){t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}};
        for(var i=0;i<ttq.methods.length;i++)ttq.setAndDefer(ttq,ttq.methods[i]);
        ttq.instance = function(t){
          var e=ttq._i[t]||[],n=0;
          for(n=0;n<ttq.methods.length;n++)ttq.setAndDefer(e,ttq.methods[n]);
          return e
        };
        ttq.load = function(e,n){
          var r="https://analytics.tiktok.com/i18n/pixel/events.js",o=n&&n.partner;
          ttq._i=ttq._i||{};
          ttq._i[e]=[];
          ttq._i[e]._u=r;
          ttq._t=ttq._t||{};
          ttq._t[e]=+new Date;
          ttq._o=ttq._o||{};
          ttq._o[e]=n||{};
          n=doc.createElement("script");
          n.type="text/javascript";
          n.async=!0;
          n.src=r+"?sdkid="+e+"&lib="+t;
          
          // Защита от блокировки: обработчик ошибок
          n.onerror = function() {
            if (console && console.debug) {
              console.debug('TikTok Pixel script failed to load - possible ad blocker');
            }
            w.__ttqPixelLoaded = false;
            w._ttqScriptLoaded = false;
          };
          
          // КРИТИЧНО: Обработчик успешной загрузки скрипта
          n.onload = function() {
            // Устанавливаем флаг что скрипт реально загрузился
            w._ttqScriptLoaded = true;
            if (console && console.log) {
              console.log('[TikTok Pixel] Script loaded successfully');
            }
            
            // КРИТИЧНО: Вызываем ttq.page() сразу после загрузки скрипта
            try {
              if (w.ttq && typeof w.ttq.page === 'function') {
                w.ttq.page();
                if (console && console.log) {
                  console.log('[TikTok Pixel] PageView event sent');
                }
              }
            } catch (pageErr) {
              if (console && console.debug) {
                console.debug('TikTok Pixel page() error:', pageErr);
              }
            }
            
            // Вызываем identify для Advanced Matching
            try {
              if (typeof ttqIdentify === 'function') {
                ttqIdentify();
              }
            } catch (identifyErr) {
              if (console && console.debug) {
                console.debug('TikTok Pixel identify error:', identifyErr);
              }
            }
            
            // Проверяем что ttq.track доступен и реально работает (не только очередь)
            var checkReady = setInterval(function() {
              // Проверяем что ttq существует и track это функция
              if (w.ttq && typeof w.ttq.track === 'function') {
                // Пробуем проверить что это не просто очередь, а реальная функция
                var trackStr = String(w.ttq.track);
                var isRealFunction = trackStr.indexOf('[native code]') !== -1 || 
                                    trackStr.indexOf('function') !== -1 ||
                                    (w.ttq.track.length !== undefined); // Если это функция, у неё есть length
                
                // Дополнительная проверка: проверяем что есть instance или _i (внутренние структуры TikTok)
                var hasInternalStructures = (w.ttq._i && typeof w.ttq._i === 'object') || 
                                           (w.ttq.instance && typeof w.ttq.instance === 'function');
                
                if (isRealFunction || hasInternalStructures) {
                  clearInterval(checkReady);
                  w._ttqLoaded = true;
                  if (console && console.log) {
                    console.log('[TikTok Pixel] Pixel ready, track function available');
                  }
                  
                  // Обрабатываем буферизованные события
                  if (w._ttqBuffer && w._ttqBuffer.length > 0) {
                    if (console && console.log) {
                      console.log('[TikTok Pixel] Processing ' + w._ttqBuffer.length + ' buffered events');
                    }
                    w._ttqBuffer.forEach(function(buffered) {
                      try {
                        if (console && console.log) {
                          console.log('[TikTok Pixel] Sending buffered event:', buffered.event, buffered.data);
                        }
                        w.ttq.track(buffered.event, buffered.data);
                      } catch (err) {
                        if (console && console.debug) {
                          console.debug('TikTok Pixel buffered event error', err);
                        }
                      }
                    });
                    w._ttqBuffer = []; // Clear buffer
                  }
                }
              }
              
              // Также проверяем ready callback если доступен (более надежный способ)
              if (w.ttq && typeof w.ttq.ready === 'function') {
                clearInterval(checkReady);
                w.ttq.ready(function() {
                  w._ttqLoaded = true;
                  if (console && console.log) {
                    console.log('[TikTok Pixel] Pixel ready via ready callback');
                  }
                  
                  // Обрабатываем буферизованные события
                  if (w._ttqBuffer && w._ttqBuffer.length > 0) {
                    if (console && console.log) {
                      console.log('[TikTok Pixel] Processing ' + w._ttqBuffer.length + ' buffered events (via ready callback)');
                    }
                    w._ttqBuffer.forEach(function(buffered) {
                      try {
                        if (console && console.log) {
                          console.log('[TikTok Pixel] Sending buffered event:', buffered.event, buffered.data);
                        }
                        w.ttq.track(buffered.event, buffered.data);
                      } catch (err) {
                        if (console && console.debug) {
                          console.debug('TikTok Pixel buffered event error', err);
                        }
                      }
                    });
                    w._ttqBuffer = []; // Clear buffer
                  }
                });
              }
            }, 100); // Проверяем каждые 100ms (немного реже для производительности)
            
            // Таймаут для проверки готовности (макс 5 секунд)
            setTimeout(function() {
              clearInterval(checkReady);
              if (!w._ttqLoaded) {
                // Если через 5 секунд все еще не готово - помечаем как загруженное и пытаемся отправить события
                w._ttqLoaded = true;
                w._ttqScriptLoaded = true;
                if (console && console.warn) {
                  console.warn('[TikTok Pixel] Timeout waiting for ready state, processing events anyway');
                }
                
                if (w._ttqBuffer && w._ttqBuffer.length > 0) {
                  w._ttqBuffer.forEach(function(buffered) {
                    try {
                      if (w.ttq && typeof w.ttq.track === 'function') {
                        w.ttq.track(buffered.event, buffered.data);
                      }
                    } catch (err) {
                      if (console && console.debug) {
                        console.debug('TikTok Pixel timeout event error', err);
                      }
                    }
                  });
                  w._ttqBuffer = [];
                }
              }
            }, 5000);
          };
          
          e=doc.getElementsByTagName("script")[0];
          if (e && e.parentNode) {
            e.parentNode.insertBefore(n,e);
          } else {
            // Fallback: добавляем в head
            (doc.head || doc.getElementsByTagName("head")[0]).appendChild(n);
          }
        };
      }(win, doc, 'ttq');
      
      win.__ttqPixelLoaded = true;
    } catch (err) {
      if (console && console.debug) {
        console.debug('TikTok Pixel initialization error:', err);
      }
      win.__ttqPixelLoaded = false;
      return;
    }
    
    try {
      // Загружаем TikTok Pixel с test_event_code если есть
      var loadOptions = {};
      if (TIKTOK_TEST_EVENT_CODE) {
        loadOptions.test_event_code = TIKTOK_TEST_EVENT_CODE;
        if (console && console.log) {
          console.log('[TikTok Pixel] Test mode enabled with code:', TIKTOK_TEST_EVENT_CODE);
        }
      }
      
      win.ttq.load(TIKTOK_PIXEL_ID, loadOptions);
      if (console && console.log) {
        console.log('[TikTok Pixel] Pixel load() called, waiting for script to load...');
      }
    } catch (err) {
      if (console && console.debug) {
        console.debug('TikTok Pixel load error', err);
      }
      win.__ttqPixelLoaded = false;
      return;
    }
    
    // НЕ вызываем ttq.page() здесь - скрипт еще не загрузился!
    // ttq.page() будет вызван в onload обработчике после реальной загрузки скрипта
    
    // НЕ устанавливаем _ttqLoaded = true здесь!
    // Это сделается в onload обработчике скрипта после реальной загрузки
    // Флаг _ttqLoaded устанавливается в n.onload обработчике выше
  }

  setupGlobalEventBridge();
  
  // PERF OPTIMIZATION: Defer pixel loading to improve PageSpeed TBT score
  // Pixels will be loaded on user interaction OR after idle window (whichever first).
  // Mobile-safe: skip mousemove (too noisy), prefer pointerdown.
  var pixelsLoaded = false;
  var routeName = (doc.documentElement.getAttribute('data-route-name') || '').toLowerCase();
  var analyticsMode = (doc.documentElement.getAttribute('data-analytics-mode') || '').toLowerCase();
  var isPassiveAnalytics = analyticsMode === 'passive';
  var interactionEvents = routeName === 'home'
    ? ['click', 'pointerdown', 'keydown']
    : (isPassiveAnalytics
      ? ['click', 'pointerdown', 'keydown']
      : ['scroll', 'click', 'touchstart', 'pointerdown', 'keydown']);
  var deviceClass = (doc.documentElement.dataset.deviceClass || '').toLowerCase();
  var isLowDevice = deviceClass === 'low';
  var isPaidLanding = /[?&](gclid|fbclid|ttclid|wbraid|gbraid|msclkid|utm_source|utm_medium|utm_campaign)=/i.test(win.location.search);

  function initializePixelsDeferred() {
    if (pixelsLoaded) return;
    pixelsLoaded = true;

    // Remove interaction listeners
    interactionEvents.forEach(function(evt) {
      win.removeEventListener(evt, initializePixelsDeferred, {passive: true, capture: true});
    });

    if (console && console.log) {
      console.log('[Analytics] Initializing pixels (deferred, deviceClass=' + (deviceClass || 'unknown') + ')');
    }

    loadMetaPixel();
    // Paid bounces need TikTok attribution even on low-end devices. Organic
    // low-end traffic keeps the existing ~250 KB suppression.
    if (!isLowDevice || isPaidLanding) {
      loadTikTokPixel();
    }
    
    // Проверяем что пиксели загрузились (для отладки)
    setTimeout(function() {
      if (win.fbq && typeof win.fbq === 'function') {
        if (console && console.log) {
          console.log('[Analytics] ✓ Meta Pixel initialized');
        }
      } else {
        if (console && console.warn) {
          console.warn('[Analytics] ⚠ Meta Pixel not detected');
        }
      }
      
      if (win.ttq && typeof win.ttq.track === 'function') {
        if (console && console.log) {
          console.log('[Analytics] ✓ TikTok Pixel initialized');
        }
      } else {
        if (console && console.warn) {
          console.warn('[Analytics] ⚠ TikTok Pixel not detected');
        }
      }
    }, 1000);
  }
  
  // Loading the orchestrator early is not enough for paid-bounce attribution:
  // invoke the idempotent initializer before waiting for interaction or idle.
  if (isPaidLanding) {
    initializePixelsDeferred();
  } else {
    // Organic traffic keeps the deferred PageSpeed path.
    interactionEvents.forEach(function(evt) {
      win.addEventListener(evt, initializePixelsDeferred, {passive: true, capture: true, once: true});
    });

    // Fallback: Load after idle window to ensure pixels work if no interaction.
    // Раньше был setTimeout(..., 3000) — на mobile это давало post-load long tasks.
    // Используем requestIdleCallback с большим timeout, fallback на 10s setTimeout.
    var idleDelay = isPassiveAnalytics
      ? (isLowDevice ? 22000 : 14000)
      : (isLowDevice ? 15000 : 8000);
    if (typeof win.requestIdleCallback === 'function') {
      win.requestIdleCallback(initializePixelsDeferred, { timeout: idleDelay });
    } else {
      setTimeout(initializePixelsDeferred, idleDelay);
    }
  }

  function handleBFCacherestore(event) {
    if (!event || !event.persisted) {
      return;
    }
    if (console && console.debug) {
      console.debug('[Analytics] BFCache restore detected, re-initializing pixels');
    }
    // The deferred initializer is the single idempotent entry point.  It
    // loads pixels when BFCache restored the page before the idle trigger,
    // and safely becomes a no-op when they are already active.
    initializePixelsDeferred();
  }

  if (doc && doc.addEventListener) {
    doc.addEventListener('twc:pageshow', function(evt) {
      if (evt && evt.detail && evt.detail.persisted) {
        handleBFCacherestore({ persisted: true });
      }
    });
  }
  win.addEventListener('pageshow', handleBFCacherestore);
  
  // Остальные скрипты загружаем c большей задержкой + предпочтительно в idle-окно.
  // GA4 — важнее для атрибуции, поэтому раньше; Clarity — рекординг, поздно.
  // На low-device класс Clarity не грузится вообще до явной интеракции через pixelsDeferred trigger.
  var gaDelay = isPassiveAnalytics
    ? (isLowDevice ? 18000 : 10000)
    : (isLowDevice ? 12000 : 6000);
  var clarityDelay = isPassiveAnalytics
    ? null
    : (isLowDevice ? 30000 : 15000);
  if (typeof win.requestIdleCallback === 'function') {
    win.requestIdleCallback(function () { schedule(loadGoogleAnalytics, 0); }, { timeout: gaDelay });
    if (clarityDelay !== null && !isLowDevice && routeName !== 'home') {
      win.requestIdleCallback(function () { schedule(loadClarity, 0); }, { timeout: clarityDelay });
    }
  } else {
    schedule(loadGoogleAnalytics, gaDelay);
    if (clarityDelay !== null && !isLowDevice && routeName !== 'home') {
      schedule(loadClarity, clarityDelay);
    }
  }
  if (!isLowDevice && (routeName === 'home' || isPassiveAnalytics)) {
    loadClarityOnFirstInteraction();
  }
})(window, document);
