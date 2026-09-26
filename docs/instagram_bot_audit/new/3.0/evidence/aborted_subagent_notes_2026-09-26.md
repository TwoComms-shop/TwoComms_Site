# Aborted sub-agent notes · 26.09.2026

Eight audit sub-agents (A1–A8) were stopped: five hit an account credit 403, three were stopped by root to save tokens. None wrote its domain file. Below are ONLY their own narration lines (no tool output, no customer data). They are leads, not verified findings: re-verify before citing.

## YOUR AREA: A4 — prompting, instructions, published policy, knowledge sources (brand
- Now the 2.0 docs: D085 §5 and plan B02/B14/C08/C17.

## YOUR AREA: A8 — collaboration (designer, dropshipper, blogger/manager, physical shop/wholesale, photographer, videographer, other brand, competitor, employment), custom DTF orders, creator flow, prize orders, spam & prompt-injection detection, manager handoff / bot pause / unpause / manual reply, manager notifications (Telegram), attention queue
- Please run /login · API Error: 403 当前可用额度不足。 原因：您的可用额度（账户余额或订阅额度）不足，平台无法完成本次预扣或调用。 解决方案：请补充可用额度后重试。 request_id: 20260926201718558705588268d9d6he3YWsgF

## YOUR AREA: A2 — Overview tab ("Огляд"), console/ops texts, alerts, cron & Telegram noise, server load
- Now the task health module.
- Now the first prod probe: crontab, processes, load.
- Please run /login · API Error: 403 当前可用额度不足。 原因：您的可用额度（账户余额或订阅额度）不足，平台无法完成本次预扣或调用。 解决方案：请补充可用额度后重试。 request_id: 202609262016518565935668268d9d6uHygXUQd

## YOUR AREA: A7 — commerce & post-purchase: Meta ad referral → product mapping; catalogue/product cards/carousels; availability & stock; size advice; multi-product/changing mind; checkout/payment link generation; payment confirmation (webhook vs polling — Monobank?); Nova Poshta shipment tracking & pickup trigger; ordinary follow-ups (23h window), opt-in / marketing notification buttons (Meta "notification messages"/recurring opt-in), post-pickup thank-you → UGC/review invitation → reward; purchase points / ZSU donation idea
- Now the catalog cards / carousel wiring.

## YOUR AREA: A3 — Gemini API keys, per-project/per-model quotas, routing, thinking effort, error taxonomy, retries/hedging, latency, degradation/health
- Now the production probes. First one: attempt aggregates for 14 days.
- The production SSH probe was blocked by the permission classifier; I will not retry it and will use the existing read-only prod evidence recorded on 26.09 instead.

## YOUR AREA: A5 — memory & context assembly: what Gemini actually sees each turn about this customer's history, and the background analysis pipeline
- Now gemini_generate and assemble_system_instruction.
- Now bot_memory.py.
- Now funnel_journal, objection note, checkout readiness notes, and revision live / conversation context.
- Critical: production enables the revision lane. Let me check which path actually produces live replies.
- Please run /login · API Error: 403 当前可用额度不足。 原因：您的可用额度（账户余额或订阅额度）不足，平台无法完成本次预扣或调用。 解决方案：请补充可用额度后重试。 request_id: 20260926201722893363798268d9d6c2q5Tjzt

## YOUR AREA: A6 — media understanding: photos, story mentions, story replies, reposts/shares (ig_post/reel), videos, voice/audio, stickers; model selection; fallbacks
- Now the capture path (download_image, signature check).
- Please run /login · API Error: 403 当前可用额度不足。 原因：您的可用额度（账户余额或订阅额度）不足，平台无法完成本次预扣或调用。 解决方案：请补充可用额度后重试。 request_id: 202609262016566167531868268d9d6EVAT3IDZ

## YOUR AREA: A1 — funnel / customer-journey visualisation
- Now the git history for objection marker changes.
- Let me try plain separate git commands.
- Now let me view the D085 screenshots and the J1/V1 detail.
- Please run /login · API Error: 403 当前可用额度不足。 原因：您的可用额度（账户余额或订阅额度）不足，平台无法完成本次预扣或调用。 解决方案：请补充可用额度后重试。 request_id: 202609262016077774450568268d9d6g7OpBTgn
