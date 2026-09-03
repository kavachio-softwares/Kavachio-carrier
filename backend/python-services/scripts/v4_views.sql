-- ---------------------------------------------------------------------------
-- The six views, rebuilt against the v4 column names.
--
-- Postgres will not drop a column a view reads, and DROP ... CASCADE would
-- delete the view instead of fixing it. So drop_superseded_columns.py drops
-- these first, performs the ALTERs, then replays this file.
--
-- Two of them lose columns that the v4 model genuinely removed; that is noted
-- inline rather than faked with a NULL of the same name.
-- ---------------------------------------------------------------------------

-- Which carriers a broker seat may act for. auth_deps.py names this view as the
-- thing a broker's carrier access is checked against.
CREATE OR REPLACE VIEW v_broker_tenant_access AS
SELECT u.user_id,
       u.user_broker_party_id AS broker_party_id,
       pb.tenant_id,
       t.tenant_code AS tenant_name
  FROM app_user u
  JOIN program_broker pb
    ON pb.program_broker_party_id = u.user_broker_party_id
   AND COALESCE(pb.program_broker_status, 'active') = 'active'
  JOIN tenant t ON t.tenant_id = pb.tenant_id
 WHERE u.user_role = ANY (ARRAY['broker_admin', 'operator'])
 GROUP BY u.user_id, u.user_broker_party_id, pb.tenant_id, t.tenant_code;


-- One row per broker organisation, with its reach.
CREATE OR REPLACE VIEW v_broker_directory AS
SELECT p.party_id                                   AS broker_party_id,
       p.party_legal_name                           AS broker_name,
       count(DISTINCT pb.tenant_id)                 AS carrier_count,
       count(DISTINCT pb.program_broker_program_id) AS programme_count,
       count(DISTINCT c.contract_id)                AS contract_count,
       count(DISTINCT u.user_id) FILTER (WHERE u.user_role = 'operator')
                                                    AS operator_count
  FROM party p
  LEFT JOIN program_broker pb ON pb.program_broker_party_id = p.party_id
  LEFT JOIN contract      c  ON c.contract_broker_party_id  = p.party_id
  LEFT JOIN app_user      u  ON u.user_broker_party_id      = p.party_id
 WHERE p.party_type::text = 'broker'
 GROUP BY p.party_id, p.party_legal_name;


-- Every login on the platform, with the organisation it belongs to.
-- `invited_at` is NOT carried over: the v4 model has no such column.
CREATE OR REPLACE VIEW v_platform_users AS
SELECT u.user_id,
       u.user_full_name AS full_name,
       u.user_email     AS email,
       u.user_role      AS role,
       u.user_status    AS status,
       CASE
           WHEN u.user_role = 'kavachio_admin'        THEN 'Kavachio'
           WHEN u.user_broker_party_id IS NOT NULL    THEN bp.party_legal_name
           ELSE t.tenant_code
       END AS organisation_name,
       CASE
           WHEN u.user_role = 'kavachio_admin'     THEN 'kavachio'
           WHEN u.user_broker_party_id IS NOT NULL THEN 'broker'
           ELSE 'carrier'
       END AS organisation_kind,
       u.user_tenant_id       AS tenant_id,
       u.user_broker_party_id AS broker_party_id,
       u.user_invited_by_id   AS invited_by_user_id,
       inv.user_full_name     AS invited_by_name,
       u.accepted_at,
       u.created_at,
       u.last_login_at
  FROM app_user u
  LEFT JOIN tenant   t   ON t.tenant_id  = u.user_tenant_id
  LEFT JOIN party    bp  ON bp.party_id  = u.user_broker_party_id
  LEFT JOIN app_user inv ON inv.user_id  = u.user_invited_by_id;


-- Contracts that are live: approved, and the current SCD-2 version.
-- The v2 version listed ~65 columns by name, most of which the v4 model
-- removed (estimated_total_premium, commission_pct, the carrier_pct_* band,
-- the loss-ratio set, earnings_pattern_id …). Selecting * states the same
-- intent without naming columns that no longer exist.
CREATE OR REPLACE VIEW v_contract_live AS
SELECT *
  FROM contract
 WHERE contract_approval_status = 'approved'
   AND is_current_version;


-- Premium written against the agreed cap, per contract.
CREATE OR REPLACE VIEW v_premium_vs_cap AS
SELECT c.tenant_id,
       c.contract_program_id       AS program_id,
       c.contract_id,
       c.contract_premium_cap_amount AS premium_cap_amount,
       COALESCE(sum(pt.premium_transaction_total_gross_written_premium_amount), 0)
           AS premium_written,
       CASE
           WHEN c.contract_premium_cap_amount > 0
           THEN round(
                  COALESCE(sum(pt.premium_transaction_total_gross_written_premium_amount), 0)
                  / c.contract_premium_cap_amount * 100, 1)
           ELSE NULL
       END AS pct_of_cap
  FROM contract c
  LEFT JOIN policy p
    ON p.policy_contract_id = c.contract_id AND p.is_current_version
  LEFT JOIN premium_transaction pt
    ON pt.premium_transaction_policy_id = p.policy_id AND pt.is_current_version
 WHERE c.is_current_version
 GROUP BY c.tenant_id, c.contract_program_id, c.contract_id,
          c.contract_premium_cap_amount;


-- Recent validation runs, one row per run, for the runs list.
CREATE OR REPLACE VIEW v_recent_runs AS
SELECT vr.run_id,
       vr.tenant_id,
       t.tenant_code      AS carrier_name,
       pr.program_id,
       pr.program_name    AS programme_name,
       c.contract_id,
       c.contract_name,
       bp.party_legal_name AS broker_name,
       bu.bdx_upload_id,
       COALESCE(oe.filename, bu.filename::varchar) AS output_file,
       bu.reporting_period_start,
       bu.reporting_period_end,
       bu.uploaded_by_user_id,
       au.user_full_name  AS run_by,
       vr.rows_validated  AS policies,
       vr.violations_count,
       vr.critical_count,
       vr.warning_count,
       count(vv.violation_id) FILTER (WHERE vv.resolution_status = 'open')
           AS open_exceptions,
       vr.completed_at    AS generated_at,
       oe.id              AS output_export_id,
       CASE
           WHEN vr.status <> 'completed' THEN 'running'
           WHEN count(vv.violation_id) FILTER (WHERE vv.resolution_status = 'open') > 0
                THEN 'review'
           WHEN oe.id IS NOT NULL THEN 'download'
           ELSE 'review'
       END AS row_action
  FROM validation_run vr
  JOIN bdx_upload bu ON bu.bdx_upload_id = vr.bdx_upload_id
  JOIN contract   c  ON c.contract_id    = bu.contract_id
  LEFT JOIN program  pr ON pr.program_id = c.contract_program_id
  LEFT JOIN tenant   t  ON t.tenant_id   = c.tenant_id
  LEFT JOIN party    bp ON bp.party_id   = c.contract_broker_party_id
  LEFT JOIN app_user au ON au.user_id    = bu.uploaded_by_user_id
  LEFT JOIN validation_violation vv ON vv.run_id = vr.run_id
  LEFT JOIN output_exports oe ON oe.validation_run_id = vr.run_id
 GROUP BY vr.run_id, vr.tenant_id, t.tenant_code, pr.program_id, pr.program_name,
          c.contract_id, c.contract_name, bp.party_legal_name, bu.bdx_upload_id,
          oe.filename, bu.filename, bu.reporting_period_start,
          bu.reporting_period_end, bu.uploaded_by_user_id, au.user_full_name,
          vr.rows_validated, vr.violations_count, vr.critical_count,
          vr.warning_count, vr.completed_at, vr.status, oe.id;
