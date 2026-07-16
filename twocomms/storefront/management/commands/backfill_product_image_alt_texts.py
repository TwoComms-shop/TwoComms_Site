from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from storefront.models import ProductImage


def _limit_text(value, max_length=200):
    value = ' '.join(str(value or '').split())
    if len(value) <= max_length:
        return value
    shortened = value[: max_length - 3]
    boundary = shortened.rfind(' ')
    if boundary >= int(max_length * 0.75):
        shortened = shortened[:boundary]
    return shortened + '...'


def _collect_plan(*, lock=False):
    queryset = ProductImage.objects.select_related('product').order_by(
        'product_id', 'order', 'id'
    )
    if lock:
        queryset = queryset.select_for_update()

    positions = {}
    current_product_id = None
    position = 0
    rows = list(queryset)
    for row in rows:
        if row.product_id != current_product_id:
            current_product_id = row.product_id
            position = 0
        position += 1
        positions[row.pk] = position

    plan = {}
    for row in rows:
        if str(row.alt_text or '').strip():
            continue
        plan[row.pk] = _limit_text(
            f'{row.product.title} - фото {positions[row.pk]} у галереї TwoComms'
        )
    return plan


class Command(BaseCommand):
    help = 'Dry-run or backfill only empty ProductImage alt_text values.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Persist the guarded backfill. Default is read-only.',
        )
        parser.add_argument('--expect-images', type=int)

    def _write_report(self, count, *, dry_run, updated=None):
        parts = [f'images={count}']
        if updated is not None:
            parts.append(f'updated_images={updated}')
        parts.append(f'dry_run={dry_run}')
        self.stdout.write(' '.join(parts))

    def handle(self, *args, **options):
        if not options['apply']:
            plan = _collect_plan()
            self._write_report(len(plan), dry_run=True)
            return

        if options.get('expect_images') is None:
            raise CommandError('Apply requires exact dry-run guard: --expect-images')

        with transaction.atomic():
            plan = _collect_plan(lock=True)
            count = len(plan)
            if count != options['expect_images']:
                raise CommandError(
                    f"Candidate count changed; expected {options['expect_images']}, found {count}"
                )

            rows = ProductImage.objects.in_bulk(plan)
            if len(rows) != count:
                raise CommandError('Candidate rows changed while locking; aborting')
            updates = []
            for pk, alt_text in plan.items():
                row = rows[pk]
                row.alt_text = alt_text
                updates.append(row)
            if updates:
                ProductImage.objects.bulk_update(updates, ['alt_text'])

            self._write_report(count, dry_run=False, updated=count)
