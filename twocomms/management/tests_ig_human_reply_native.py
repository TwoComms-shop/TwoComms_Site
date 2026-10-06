"""Disposable MariaDB contention proof; real row locks and thread connections."""
from datetime import timedelta
from queue import Queue
from threading import Barrier, Event, Thread
from time import monotonic
from unittest import skipUnless
import uuid

from django.contrib.auth import get_user_model
from django.db import connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.ig_bot_models import HumanReplyCommand
from management.ig_human_reply_models import HumanReplyPart, HumanReplyPrivateDocument
from management.models import AdminAuditLog, IgClient, InstagramBotMessage, InstagramBotSettings
from management.services.ig_human_reply import (
    HumanReplyRejected, create_human_reply_command, create_human_reply_command_from_draft,
)
from management.services.ig_human_reply_delivery import (
    PLAN_KEY, claim_next_human_part, create_private_document, update_private_document,
)
from management.services.instagram_bot import ingress_provider_namespace


@skipUnless(connection.vendor == "mysql", "requires disposable MariaDB row locks")
@override_settings(GOOGLE_INDEXING_ENABLED=False)
class HumanReplyNativeContentionTests(TransactionTestCase):
    """No dispatcher/provider invocation, receipt fabrication, or mocked locks."""
    WAIT_SECONDS = 5
    JOIN_SECONDS = 12

    def setUp(self):
        super().setUp()
        self.assertTrue(connection.mysql_is_mariadb, "this gate requires MariaDB")
        self.assertRegex(str(connection.settings_dict.get("NAME") or ""), r"^test_twocomms_[A-Za-z0-9_]+$",
            "native contention gate must never use production data")
        self.assertTrue(connection.features.has_select_for_update)
        self.assertFalse(connection.in_atomic_block)
        self.now = timezone.now()
        self.actor = get_user_model().objects.create_superuser(username="human-native-owner",
            email="native-human@example.test", password="x")
        self.settings_row = InstagramBotSettings.objects.create(pk=1, is_enabled=True,
            ig_user_id="90000010", page_id="90000010")
        self.customer = IgClient.objects.create(igsid="90000011")
        self.namespace = ingress_provider_namespace(self.settings_row)
        self.source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", status="done", text="Native source context",
            provider_namespace=self.namespace, provider_created_at=self.now - timedelta(minutes=2),
            mid="native-human-source-" + uuid.uuid4().hex)
        self.assertTrue(connection.get_autocommit())
        transaction.commit()

    def draft(self):
        result = create_private_document(self.customer.pk, actor=self.actor, kind="reply_draft",
            text="Native saved manager text", context_message_id=self.source.pk,
            provider_namespace=self.namespace, now=self.now)
        self.assertFalse(connection.in_atomic_block)
        transaction.commit()
        return result

    def _wait_for_engine_contention(self, worker_ids, holder_id, issued, completed):
        for event in issued:
            self.assertTrue(event.wait(self.WAIT_SECONDS), "worker did not issue a real FOR UPDATE statement")
        observer = connection.copy(alias="human_native_observer")
        try:
            observer.ensure_connection()
            self.assertTrue(observer.get_autocommit())
            self.assertFalse(observer.in_atomic_block)
            with observer.cursor() as cursor:
                cursor.execute("SELECT CONNECTION_ID()")
                self.assertNotIn(int(cursor.fetchone()[0]), (holder_id, *worker_ids))
            deadline = monotonic() + self.WAIT_SECONDS
            while True:
                # Include a chain through the other worker: claim's canonical
                # settings->client prefix can wait on the other worker's settings
                # lock while that worker waits on our deliberately held client row.
                with observer.cursor() as cursor:
                    cursor.execute("""
                        SELECT waiting.trx_mysql_thread_id, blocking.trx_mysql_thread_id
                        FROM information_schema.INNODB_LOCK_WAITS AS waits
                        JOIN information_schema.INNODB_TRX AS waiting
                          ON waiting.trx_id = waits.requesting_trx_id
                        JOIN information_schema.INNODB_TRX AS blocking
                          ON blocking.trx_id = waits.blocking_trx_id
                        WHERE waiting.trx_mysql_thread_id IN (%s, %s)
                          AND blocking.trx_mysql_thread_id IN (%s, %s, %s)
                    """, (*worker_ids, holder_id, *worker_ids))
                    graph = {(int(waiter), int(blocker)) for waiter, blocker in cursor.fetchall()}
                waiting = {waiter for waiter, _ in graph}
                if waiting == set(worker_ids) and any(blocker == holder_id for _, blocker in graph):
                    self.assertTrue(all(not event.is_set() for event in completed), "worker passed a held real row lock")
                    return graph
                self.assertTrue(all(not event.is_set() for event in completed), "worker completed before engine wait was observed")
                remaining = deadline - monotonic()
                self.assertGreater(remaining, 0, "MariaDB did not expose the expected bounded lock-wait graph")
                # MariaDB's shared I_S transaction cache updates only after
                # >100ms since its last READ; 25ms polling can pin a stale cache.
                # Primary source: CACHE_MIN_IDLE_TIME_NS / trx_i_s_cache_end_read
                # https://raw.githubusercontent.com/MariaDB/server/11.4/storage/innobase/trx/trx0i_s.cc
                # Progress wakes this bounded Event wait; the exact graph above
                # remains mandatory, with the original five-second deadline.
                completed[0].wait(min(0.150, remaining))
        finally:
            observer.close()

    def pair(self, actions, *, hold_client=False):
        start = Barrier(3)
        issued, completed = [Event(), Event()], [Event(), Event()]
        worker_ids, results = [None, None], Queue()

        def worker(index):
            connections.close_all()
            db = connections["default"]
            try:
                db.ensure_connection()
                with db.cursor() as cursor:
                    cursor.execute("SET SESSION innodb_lock_wait_timeout = 8")
                    cursor.execute("SELECT CONNECTION_ID()")
                    worker_ids[index] = int(cursor.fetchone()[0])
                actor = get_user_model().objects.get(pk=self.actor.pk)
                start.wait(timeout=self.WAIT_SECONDS)

                def observe(execute, sql, params, many, context):
                    if "FOR UPDATE" in sql.upper():
                        issued[index].set()
                    return execute(sql, params, many, context)

                with db.execute_wrapper(observe):
                    outcome = actions[index](actor)
                self.assertFalse(db.in_atomic_block)
                transaction.commit()
                results.put((index, outcome))
            except HumanReplyRejected as exc:
                results.put((index, {"ok": False, "reason": exc.code}))
            except Exception as exc:
                # No credentials, SQL values, customer text or exception dumps.
                code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
                results.put((index, {"unexpected": type(exc).__name__, "database_code": code}))
            finally:
                db.close()
                connections.close_all()
                completed[index].set()

        threads = [Thread(target=worker, args=(index,), name=f"human-native-{index}", daemon=True) for index in (0, 1)]
        try:
            if hold_client:
                with transaction.atomic():
                    IgClient.objects.select_for_update().get(pk=self.customer.pk)
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT CONNECTION_ID()")
                        holder_id = int(cursor.fetchone()[0])
                    for thread in threads:
                        thread.start()
                    start.wait(timeout=self.WAIT_SECONDS)
                    self.assertEqual(len(set(worker_ids)), 2)
                    self.assertNotIn(holder_id, worker_ids)
                    self._wait_for_engine_contention(worker_ids, holder_id, issued, completed)
                # Real commit releases the client row; no test lock is replaced.
                self.assertFalse(connection.in_atomic_block)
            else:
                for thread in threads:
                    thread.start()
                start.wait(timeout=self.WAIT_SECONDS)
                self.assertEqual(len(set(worker_ids)), 2)
            for event in completed:
                self.assertTrue(event.wait(self.JOIN_SECONDS), "worker exceeded finite operation deadline")
        finally:
            start.abort()
            # A failed assertion also exits the holder transaction above. Each
            # session has its own bounded InnoDB wait and closes in worker finally.
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=self.JOIN_SECONDS)
            alive = [index for index, thread in enumerate(threads) if thread.is_alive()]
            if alive:
                # Cancel only these test-created sessions on this guarded
                # disposable database, then let worker finally close resources.
                with connection.cursor() as cursor:
                    for index in alive:
                        if worker_ids[index] is not None:
                            cursor.execute("KILL CONNECTION %s", (worker_ids[index],))
                for index in alive:
                    threads[index].join(timeout=self.WAIT_SECONDS)
            self.assertTrue(all(not thread.is_alive() for thread in threads), "native worker resource did not close")
        self.assertEqual(results.qsize(), 2)
        ordered = dict(results.get_nowait() for _ in range(2))
        self.assertTrue(all("unexpected" not in result for result in ordered.values()), ordered)
        return [ordered[index] for index in (0, 1)]

    def assert_no_delivery(self):
        self.assertFalse(HumanReplyPart.objects.filter(provider_started_at__isnull=False).exists())
        self.assertFalse(HumanReplyPart.objects.filter(provider_message_id__isnull=False).exists())
        self.assertFalse(HumanReplyCommand.objects.filter(provider_started_at__isnull=False).exists())
        self.assertTrue(all(not row.provider_message_ids for row in HumanReplyCommand.objects.all()))
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())

    def test_same_document_cas_waits_on_real_row_lock_one_wins_one_stale(self):
        doc = self.draft()

        def action(text):
            def update(actor):
                updated = update_private_document(doc.document_id, actor=actor, expected_version=1,
                    expected_hash=doc.text_hash, text=text, now=self.now)
                return {"ok": True, "version": updated.version, "text": updated.text}
            return update

        results = self.pair([action("Winner candidate A"), action("Winner candidate B")], hold_client=True)
        self.assertEqual(sum(result["ok"] for result in results), 1)
        self.assertEqual([result["reason"] for result in results if not result["ok"]], ["private_document_stale"])
        winner = next(result for result in results if result["ok"])
        doc.refresh_from_db()
        self.assertEqual((doc.version, doc.text, doc.state), (2, winner["text"], "open"))
        self.assertFalse(HumanReplyCommand.objects.exists())
        self.assertFalse(AdminAuditLog.objects.filter(action="ig_bot.human_reply_command_created").exists())
        self.assert_no_delivery()

    def _consume_action(self, doc, operation_id):
        def consume(actor):
            accepted = create_human_reply_command_from_draft(self.customer.pk, actor=actor, operation_id=operation_id,
                document_id=doc.document_id, expected_version=1, expected_hash=doc.text_hash, now=self.now)
            return {"ok": True, "command_id": accepted.command.pk, "idempotent": accepted.idempotent,
                "operation_id": str(accepted.command.operation_id)}
        return consume

    def assert_single_accepted_plan(self, doc):
        self.customer.refresh_from_db()
        doc.refresh_from_db()
        command = HumanReplyCommand.objects.get(client=self.customer)
        self.assertEqual(self.customer.reply_permission_epoch, 1)
        self.assertTrue(self.customer.manager_takeover)
        self.assertTrue(self.customer.bot_paused)
        self.assertEqual((doc.state, doc.version, doc.consumed_command_id), ("consumed", 2, command.pk))
        self.assertEqual(command.state, "pending")
        self.assertEqual(AdminAuditLog.objects.filter(action="ig_bot.human_reply_command_created").count(), 1)
        parts = list(HumanReplyPart.objects.filter(command=command).order_by("ordinal"))
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].state, "planned")
        self.assertEqual(command.operation_context[PLAN_KEY]["digest"], parts[0].plan_digest)
        self.assertEqual(command.operation_context["private_draft"]["document_id"], str(doc.document_id))
        self.assert_no_delivery()
        return command

    def test_same_operation_two_connections_consume_one_command_epoch_audit_plan(self):
        doc = self.draft()
        operation = uuid.uuid4()
        results = self.pair([self._consume_action(doc, operation), self._consume_action(doc, operation)])
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertEqual(results[0]["command_id"], results[1]["command_id"])
        self.assertCountEqual([result["idempotent"] for result in results], [False, True])
        self.assertEqual(self.assert_single_accepted_plan(doc).operation_id, operation)

    def test_competing_operations_consume_one_draft_other_operation_conflicts(self):
        doc = self.draft()
        operations = [uuid.uuid4(), uuid.uuid4()]
        results = self.pair([self._consume_action(doc, operation) for operation in operations])
        self.assertEqual(sum(result["ok"] for result in results), 1)
        self.assertEqual([result["reason"] for result in results if not result["ok"]], ["operation_conflict"])
        command = self.assert_single_accepted_plan(doc)
        self.assertIn(command.operation_id, operations)

    def test_competing_claims_real_lock_wait_has_one_owner_token_without_provider(self):
        command = create_human_reply_command(self.customer.pk, actor=self.actor, text="A" * 1500,
            context_message_id=self.source.pk, now=self.now).command
        transaction.commit()

        def claim(actor):
            result = claim_next_human_part(command.pk, now=self.now)
            return {"ok": result.ready, "reason": result.reason, "token": result.token,
                "part_id": result.part.pk if result.part else None}

        results = self.pair([claim, claim], hold_client=True)
        self.assertEqual(sum(result["ok"] for result in results), 1)
        loser = next(result for result in results if not result["ok"])
        winner = next(result for result in results if result["ok"])
        self.assertEqual((loser["reason"], loser["token"]), ("human_part_already_claimed", ""))
        self.assertTrue(winner["token"])
        part = HumanReplyPart.objects.get(pk=winner["part_id"])
        self.assertEqual((part.ordinal, part.state, part.claim_token), (0, "claimed", winner["token"]))
        self.assertEqual(HumanReplyPart.objects.filter(command=command, state="claimed").count(), 1)
        self.assertEqual(HumanReplyPart.objects.get(command=command, ordinal=1).state, "planned")
        command.refresh_from_db()
        self.assertEqual(command.state, "claimed")
        self.assertEqual(command.operation_context[PLAN_KEY]["claim"]["token"], winner["token"])
        self.assert_no_delivery()
