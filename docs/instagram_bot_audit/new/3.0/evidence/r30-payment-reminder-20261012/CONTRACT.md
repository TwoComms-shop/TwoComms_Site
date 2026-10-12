# P4-3 · согласованное напоминание об оплате · 12.10.2026

Запрос владельца: одно напоминание в явно выбранное время, одна положительная кнопка, UK/RU/EN, реальная серверная очередь, читаемое состояние в карте и Gemini. Это business agreement для текущей покупки, отдельно от post-purchase marketing и native Meta future permission.

## Producer и подтверждение

Только собственное webhook-сообщение в sealed current revision предлагает время. Точная дата/час по Europe/Kyiv или целые минуты/часы отсчитываются от provider-created timestamp. Прошедшее/неоднозначное/смешанное/цитируемое/отрицательное обращение не создаёт согласия. Нет default 25m/4h/23h таймера. Уточнение просит полную новую фразу с временем; bare-time continuation отдельного диалога пока не реализован.

Исходное invitation привязано к ACTIVE commercial episode, current deal/proposal/revision, Monobank V2 generation/attempt и всем позициям исходного source-bound quote. Цена, наличие и оплата не выводятся из текста напоминания. Delivered receipt создаётся только для точного SENT native revision effect с MID, неизменными text/button/recipient/namespace/payload/plan digests. Молчание, свободное «да» и message.text с копией payload не создают задачи. Нажатие actual quick_reply с подписанным payload создаёт один existing PAYMENT task с immutable due/scope. Acknowledgement показывает отдельную кнопку отмены; отмена требует собственное signed source и native SENT acknowledgment card. Старые задачи заменяются только после нового подтверждения.

## Dispatch и деньги

Дата остаётся неизменной: интервал 10:00–21:30 по Киеву, максимум пять минут допустимого опоздания, затем явный skipped. Дата обязана быть раньше conservative original-source 23h window и исходного proposal deadline12h. Согласие не расширяет24h окно и не даёт native future grant. Generic follow-up scheduling/recovery исключает этот purpose: нет quiet-hour shift, следующей продажи или бонуса.

Свежие source/recipient/reset/namespace/permission/erasure/takeover/opt-out/service/no-buy/spam/current commerce guards проверяются при claim и непосредственно на физической HTTP границе. Canonical payment graph locks берутся в порядке Deal→Proposal→Generations→Attempts. Validation commit precedes one HTTP; запрет network-in-atomic сохранён. Это last committed admission, а не глобальная сериализация финансового settlement на всё время внешнего socket.

Invoice действует25минут. После естественного истечения того же исходного invoice напоминание остаётся только информационным: без старой ссылки, автоматического нового счёта, обещания цены/наличия. Допустимы тот же ещё не terminalized elapsed invoice либо exact invoice_expired terminal event + original stored expiry + cleared current pointers + same READY/VIEWED proposal. Actor expiry отдельно не выводится из event reason. Paid/winner/order/review/refund/cancel/reset/reissue/любая later generation либо новая/изменённая покупка блокируют. Историческая оплата другого episode не блокирует текущую неоплаченную повторную покупку.

Native single-part MID checkpoint коммитится до записи CRM history. Сбой history восстанавливается локально без HTTP; admitted UNKNOWN и stranded processing не переотправляются.

## Context, UI и privacy

Один замороженный payment.reminder snapshot переиспользуется turn/card/admin/finite manifest. Статус и время по Киеву читаемы; сырые JSON/HMAC/action tokens/MID/invoice URL не отображаются. Историческое или несовместимое состояние остаётся unknown. Admin capture сохраняет существующий read-only64SELECT budget. Три append-only ledger table: physical INSERT/source guards, unconditional UPDATE denial, DELETE только после persisted exact privacy fence; purge до protected source deletion. Никакого historical backfill или production fixtures.

Официальные [Meta quick replies](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api/quick-replies) через Context7 подтверждают title≤20 characters и actual webhook quick_reply.payload. MariaDB guard использует CHAR_LENGTH для Unicode; byte LENGTH ошибочно отклонял бы UK/RU+emoji. Quick replies native platform не поддерживает desktop; карточка проверяется transport fixture, production synthetic customer sends не выполняются.

## Остаток P4-3

Signed restock subscription/answer/revoke и account-specific native future grant, natural payment reminder cohort, полные27паспортов и72h+20 acceptance остаются отдельными gate. UGC unused10→15 и90дней не изменены.
