# Instagram Payment Review Order Evidence

## Goal

Turn a customer payment claim or receipt into one auditable management review
that preserves conversation evidence, separates provider payment truth, and
prepares an editable order draft without guessing catalog identity or price.

## Decisions

- Only customer evidence (explicit payment claim or customer attachment) can
  trigger a review. Manager payment instructions are context, never proof.
- Amounts are extracted with their message role and message id. The manager's
  quoted conversation amount is shown as the negotiated total; catalog prices
  never overwrite it.
- Simple deterministic extraction records explicit fit/size/quantity lines.
  Product identity is linked only when an existing deal item or an unambiguous
  catalog match exists. Otherwise the review stores a Ukrainian uncertainty
  reason and the manager must choose the product.
- A payment review sends one idempotent management Telegram alert through the
  existing notification outbox. It contains the evidence, draft lines,
  conversation amount, uncertainty reasons, and a Management link. It never
  messages the customer or calls a payment provider.
- Confirming the review remains an audited manager authorization. The order
  form stays editable and the final submit remains the only order materializing
  action.

## Data flow

`InstagramBotMessage` -> deterministic evidence/order extraction ->
`IgPaymentConfirmationReview.evidence` JSON -> `IgBotNotification` outbox ->
Management review UI -> editable manual order form.

## Acceptance

- Manager instructions such as payment account details do not create a review
  by themselves.
- The target conversation yields two separate lines (basic S and oversize XS),
  one quoted total of 2100 UAH, the customer receipt attachment, and a clear
  unresolved catalog-identity reason.
- The Telegram alert is deduplicated and contains a Management URL.
- Existing deal items still take precedence; their negotiated prices are kept.
- Hidden clients remain excluded and provider payment projection is unchanged.
