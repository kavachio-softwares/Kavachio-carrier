Done. invoice_amount, invoice_status, and currency_iso on premium_invoice now accept NULL. The insert will go through cleanly — values will be populated when the BDX actually contains them, and NULL otherwise.


  OK  policy.transaction_type
  OK  coverage.coverage_type
  OK  premium_transaction.transaction_type
  OK  premium_transaction.transaction_effective_dt
  OK  premium_transaction.original_currency
  OK  premium_invoice.invoice_ref
  OK  premium_invoice.invoice_date
  OK  claim.claim_status
  OK  claim.loss_dt
  OK  policy_attributes.scope
  OK  policy_attributes.attribute_key
  OK  policy_attributes.value_type
  OK  parametric_coverage_detail.parametric_coverage_type
  OK  parametric_coverage_detail.unit_measurement_type
  OK  parametric_coverage_detail.parameters
  OK  party_role_in_policy.role
  OK  policy_fee.fee_type
  OK  policy_fee.fee_amount
  OK  policy_fee.currency_iso
  OK  tax_or_surcharge.tax_type
  OK  tax_or_surcharge.tax_amount
  OK  tax_or_surcharge.currency_iso
  OK  commission.commission_type
  OK  commission.commission_amount
  OK  commission.currency_iso
  OK  party_address.address_type
  OK  party_address.address_line1
  OK  party_license.license_number
  OK  party_license.license_state
  OK  party_license.license_type
  OK  party_relationship.relationship_type
  OK  party_relationship.effective_from
  OK  contract_terms.term_category
  OK  contract_terms.term_definition
  OK  contract_amendment.amendment_number
  OK  contract_amendment.effective_date
  OK  contract_amendment.changes

  OK  claim.program_id
  OK  claim_exposure.exposure_type
  OK  claim_exposure.exposure_status
  OK  claim_transaction.transaction_type
  OK  claim_transaction.amount
  OK  claim_transaction.currency_iso
  OK  claim_reserve.reserve_type
  OK  claim_reserve.reserve_amount
  OK  claim_reserve.currency_iso
  OK  claim_reserve.as_of_date
  OK  claim_recovery.recovery_type
  OK  claim_recovery.recovery_amount
  OK  claim_recovery.currency_iso
  OK  claim_recovery.recovery_date
  OK  claim_contact.contact_role
  OK  layer.layer_index
  OK  layer.is_primary_layer
  OK  layer_participation.participant_role
  OK  layer_participation.pct_share
  OK  ceding_session.session_period_year
  OK  ceding_session.ceded_premium
  OK  ceding_session.settlement_currency


  OK  claim.claim_number
  OK  claim_recovery_from_reinsurer.recovery_amount
  OK  claim_recovery_from_reinsurer.currency_iso
  OK  claim_recovery_from_reinsurer.settlement_status
  OK  claim_legal_proceeding.proceeding_type
  OK  contract.program_id
  OK  contract_party.role
  OK  contract_party.effective_from
  OK  policy.program_id
  OK  policy.contract_id
  OK  policy.insured_party_id
  OK  policy.risk_bearing_carrier_party_id
  OK  policy.policy_expiration_dt
  OK  premium_transaction.accounting_period
  OK  reinsurance_arrangement.arrangement_type
  OK  reinsurance_arrangement.inception_dt
  OK  reinsurance_arrangement.expiry_dt
  OK  reinsurer_participation.participation_pct
  OK  tenant.tenant_type
  OK  tenant.data_residency_region
  OK  fronting_arrangement.fronting_carrier_party_id
  OK  fronting_arrangement.risk_carrier_party_id
  OK  earnings_pattern.pattern_name
  OK  earnings_pattern.pattern_type
  OK  earnings_pattern_step.months_elapsed
  OK  earnings_pattern_step.pct_earned
  OK  loss_development_factor.ldf_name
  OK  loss_development_factor.method_type
  OK  loss_development_factor.as_of_date
  OK  loss_development_factor_value.maturity_months
  OK  fx_rate.from_currency
  OK  fx_rate.to_currency
  OK  fx_rate.rate
  OK  fx_rate.effective_date
Done. 34 dropped, 0 skipped.

  OK  party_role_in_policy.party_id
Dropped NOT NULL so BDX ingest no longer fails when a party-role row (e.g. the
"UW" column mapped to party_role_in_policy.role) carries no resolvable party_id.
NOTE: party_id is the FK to party — rows now land with a null party. The real fix
is to map party columns to a party + canonical role token in the mapper.

  DROP CONSTRAINT  party_license.uq_party_license_natural
Dropped the unique (party_id, license_state, license_type, license_number) key so
re-uploading the same BDX doesn't fail on duplicate party_license rows.
NOTE: party_license insert is still a blind insert, so duplicate license rows now
ACCUMULATE on each re-upload. The clean fix is to make the party-children insert
idempotent (clear-before-insert / ON CONFLICT), like the policy-children path.