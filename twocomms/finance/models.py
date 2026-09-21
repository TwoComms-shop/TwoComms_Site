"""Finance app models — агрегатор.

Доменні моделі розкладені по тематичних модулях у межах застосунку
(``models_core``, ``models_txn``, ``models_billing``, ``models_ops``) і
ре-експортуються тут, щоб Django бачив їх як ``finance.models.*``.
Усі моделі належать застосунку ``finance`` (app_label визначається
автоматично за розташуванням файлів у пакеті).
"""
from __future__ import annotations

from .models_core import (  # noqa: F401
    Account,
    Category,
    Company,
    Counterparty,
    CounterpartyCard,
    CurrencyRate,
    Project,
    Tag,
    get_default_company,
)
from .models_txn import (  # noqa: F401
    Attachment,
    ObligationSettlement,
    RecurrenceRule,
    Transaction,
)
from .models_billing import (  # noqa: F401
    Invoice,
    InvoiceItem,
)
from .models_ops import (  # noqa: F401
    AuditLog,
    AutomationRule,
    BudgetPlan,
    FinancialMetric,
    IntegrationConnection,
    RuleApplication,
)
from .models_settings import (  # noqa: F401
    UserSettings,
    PushSubscription,
    NotificationLog,
)
from .models_consignment import (  # noqa: F401
    Reseller,
    ConsignmentShipment,
    ConsignmentItem,
    ResellerPayment,
    ConsignmentSale,
)
from .models_finance_v2 import (  # noqa: F401
    BalanceReconciliation,
    CounterpartyClassificationPolicy,
    ClassificationReview,
    LedgerClassificationEvent,
    CounterpartyAlias,
    FundingAllocation,
    FundingSource,
    InternalTransferMatch,
    LedgerClassification,
    ObligationComponent,
    ObligationComponentSettlement,
    ObligationGroup,
    PaymentIntent,
    PaymentIntentEvent,
    RefundLink,
)

__all__ = [
    'Account', 'Category', 'Company', 'Counterparty', 'CounterpartyCard',
    'CurrencyRate', 'Project', 'Tag', 'get_default_company',
    'Attachment', 'ObligationSettlement', 'RecurrenceRule', 'Transaction',
    'Invoice', 'InvoiceItem',
    'AuditLog', 'AutomationRule', 'BudgetPlan', 'FinancialMetric',
    'IntegrationConnection', 'RuleApplication',
    'UserSettings', 'PushSubscription', 'NotificationLog',
    'Reseller', 'ConsignmentShipment', 'ConsignmentItem',
    'ResellerPayment', 'ConsignmentSale',
    'BalanceReconciliation', 'CounterpartyClassificationPolicy', 'ClassificationReview', 'CounterpartyAlias',
    'FundingAllocation', 'FundingSource', 'InternalTransferMatch',
    'LedgerClassification', 'ObligationComponent',
    'ObligationComponentSettlement', 'ObligationGroup', 'PaymentIntent',
    'PaymentIntentEvent',
    'LedgerClassificationEvent', 'RefundLink',
]
