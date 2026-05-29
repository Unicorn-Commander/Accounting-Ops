"""IRS form pre-fill exports package.

Fills official IRS PDF forms with data from the tax_review_queue and the
cpa-export JSON package. Every filled form carries a DRAFT watermark and the
message "DRAFT — CPA review required — do not file as-is" on page 1.

Supported forms (TY 2024; TY 2023 field names are identical):
  - Form 1120 (U.S. Corporation Income Tax Return)
  - Schedule L (Balance Sheet — embedded in Form 1120)
  - Form 1125-E (Compensation of Officers)

Entry-point:
  app.exports.form_prefill.generate_form_pdf(...)
"""
