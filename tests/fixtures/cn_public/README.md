Trimmed public responses captured on 2026-10-16 for deterministic parser contracts.
These are recorded historical observations, not current quotes or invented test completions.

- `financial.json`: Eastmoney `RPT_F10_FINANCE_MAINFINADATA`, security 002624,
  report 2026-06-30, published 2026-08-20. Amounts CNY; negative profits retained.
- `financial_sina.json`: Sina `CompanyFinanceService.getFinanceReport2022`,
  `paperCode=sz002624`, matching period and publication date. Only relevant metric
  rows retained; raw metric names and values unchanged.
- `announcements.json`: Eastmoney issuer-disclosure mirror, first three returned
  records for 002624. Includes a 2026-10-17 notice to verify as-of exclusion.
- `flow.json`: Eastmoney `stock/fflow/daykline/get`, security 002624. Column f52 is
  supplier-defined main-order net flow in CNY; this is not institutional-account data.

Provider URLs are defined in `data_provider/cn_public.py`. No user PDFs, credentials,
notification IDs, or private debug logs are included.
