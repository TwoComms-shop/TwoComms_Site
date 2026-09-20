"""Dedicated grant workspace for target funding and reviewable history."""
from __future__ import annotations

from django.shortcuts import render

from ..models import ClassificationReview, Transaction, get_default_company
from ..permissions import finance_access_required
from ..services import ledger_v2


@finance_access_required
def grants(request):
    company = get_default_company()
    unclassified_incomes = list(Transaction.objects.filter(
        company=company, status=Transaction.STATUS_ACTUAL,
        type=Transaction.TYPE_INCOME, economic_kind='unknown',
    ).select_related('account', 'category').order_by('-date_actual', '-id')[:30])
    sources = []
    for source in company.funding_sources.filter(is_active=True).select_related('project').order_by('-received_at', 'name'):
        summary = ledger_v2.funding_summary(source)
        reviews = list(ClassificationReview.objects.filter(
            company=company, status='pending', proposal__funding_source_id=source.id,
        ).select_related('transaction', 'transaction__account').order_by('-created_at'))
        allocations = list(source.allocations.select_related('transaction', 'transaction__account')
                           .order_by('-created_at')[:50])
        received_transactions = list(Transaction.objects.filter(
            company=company, funding_source=source, type=Transaction.TYPE_INCOME,
            status=Transaction.STATUS_ACTUAL,
        ).select_related('account').order_by('-date_actual', '-id')[:30])
        allocated_ids = [allocation.transaction_id for allocation in allocations]
        pending_expenses = list(Transaction.objects.filter(
            company=company, account__name__iexact='Грантова',
            type=Transaction.TYPE_EXPENSE, status=Transaction.STATUS_ACTUAL,
        ).exclude(id__in=allocated_ids).select_related('account').order_by('-date_actual')[:50])
        sources.append({
            'id': source.id,
            'name': source.name,
            'source_type': source.get_source_type_display(),
            'received_at': source.received_at,
            'program_total_amount': source.program_total_amount,
            'stage_label': source.stage_label,
            'receipt_transaction_id': source.receipt_transaction_id,
            'valid_until': source.valid_until,
            'project': source.project.name if source.project else '',
            'notes': source.notes,
            'received': summary['received'],
            'spent': summary['spent'],
            'reserved': summary['reserved'],
            'available': summary['available'],
            'reviews': reviews,
            'allocations': allocations,
            'received_transactions': received_transactions,
            'pending_expenses': pending_expenses if source.name == 'УВФ' else [],
        })
    return render(request, 'finance/grants.html', {
        'active_tab': 'grants',
        'sources': sources,
        'pending_reviews': sum(len(source['reviews']) for source in sources),
        'unclassified_incomes': unclassified_incomes,
    })
