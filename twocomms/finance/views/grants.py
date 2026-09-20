"""Dedicated grant workspace for target funding and reviewable history."""
from __future__ import annotations

from django.shortcuts import render

from ..models import ClassificationReview, get_default_company
from ..permissions import finance_access_required
from ..services import ledger_v2


@finance_access_required
def grants(request):
    company = get_default_company()
    sources = []
    for source in company.funding_sources.filter(is_active=True).select_related('project').order_by('-received_at', 'name'):
        summary = ledger_v2.funding_summary(source)
        reviews = list(ClassificationReview.objects.filter(
            company=company, status='pending', proposal__funding_source_id=source.id,
        ).select_related('transaction', 'transaction__account').order_by('-created_at'))
        allocations = list(source.allocations.select_related('transaction', 'transaction__account')
                           .order_by('-created_at')[:50])
        sources.append({
            'id': source.id,
            'name': source.name,
            'source_type': source.get_source_type_display(),
            'received_at': source.received_at,
            'valid_until': source.valid_until,
            'project': source.project.name if source.project else '',
            'notes': source.notes,
            'received': summary['received'],
            'spent': summary['spent'],
            'reserved': summary['reserved'],
            'available': summary['available'],
            'reviews': reviews,
            'allocations': allocations,
        })
    return render(request, 'finance/grants.html', {
        'active_tab': 'grants',
        'sources': sources,
        'pending_reviews': sum(len(source['reviews']) for source in sources),
    })
