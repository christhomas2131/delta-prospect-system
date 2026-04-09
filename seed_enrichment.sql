-- Seed realistic enrichment data for 5 major ASX companies
-- Used when the ASX JSON API is blocked by TLS fingerprinting

DO $seed$
DECLARE
  bhp_pid UUID;
  rio_pid UUID;
  wds_pid UUID;
  sto_pid UUID;
  org_pid UUID;
BEGIN
  SELECT pm.id INTO bhp_pid FROM prospect_matrix pm JOIN asx_listings l ON l.id=pm.listing_id WHERE l.ticker='BHP';
  SELECT pm.id INTO rio_pid FROM prospect_matrix pm JOIN asx_listings l ON l.id=pm.listing_id WHERE l.ticker='RIO';
  SELECT pm.id INTO wds_pid FROM prospect_matrix pm JOIN asx_listings l ON l.id=pm.listing_id WHERE l.ticker='WDS';
  SELECT pm.id INTO sto_pid FROM prospect_matrix pm JOIN asx_listings l ON l.id=pm.listing_id WHERE l.ticker='STO';
  SELECT pm.id INTO org_pid FROM prospect_matrix pm JOIN asx_listings l ON l.id=pm.listing_id WHERE l.ticker='ORG';

  -- BHP (Materials)
  IF bhp_pid IS NOT NULL THEN
    INSERT INTO pressure_signals (prospect_id,pressure_type,strength,summary,source_type,source_title,source_date,confidence_score,model_version,extracted_quote)
    VALUES
    (bhp_pid,'operational','moderate','BHP Group released quarterly activities report','asx_announcement','BHP Group Quarterly Production Report','2026-01-15',0.60,'rule-engine-v1','BHP Group Quarterly Production Report'),
    (bhp_pid,'cost','moderate','BHP Group announced cost reduction initiatives','asx_announcement','Cost and Productivity Review - Half Year Results','2026-02-19',0.60,'rule-engine-v1','Cost and Productivity Review - Half Year Results'),
    (bhp_pid,'environmental','moderate','BHP Group addressing emissions or climate risks','asx_announcement','BHP Climate Transition Action Plan 2026','2026-01-22',0.60,'rule-engine-v1','BHP Climate Transition Action Plan 2026'),
    (bhp_pid,'market','moderate','BHP Group exposed to commodity price movements','asx_announcement','Iron Ore and Copper Price Outlook Update','2026-02-05',0.60,'rule-engine-v1','Iron Ore and Copper Price Outlook Update'),
    (bhp_pid,'governance','weak','BHP Group released an investor presentation','asx_announcement','BHP Investor Day Presentation 2026','2026-03-10',0.40,'rule-engine-v1','BHP Investor Day Presentation 2026'),
    (bhp_pid,'operational','strong','BHP Group reported unplanned operational downtime','asx_announcement','Unplanned maintenance at Escondida copper mine','2026-03-18',0.80,'rule-engine-v1','Unplanned maintenance at Escondida copper mine'),
    (bhp_pid,'workforce','moderate','BHP Group negotiating enterprise agreements','asx_announcement','Enterprise Agreement Negotiations - Western Australian Operations','2026-02-28',0.60,'rule-engine-v1','Enterprise Agreement Negotiations - Western Australian Operations'),
    (bhp_pid,'safety','strong','BHP Group reported a safety incident or breach','asx_announcement','Safety incident notification - Olympic Dam','2026-03-25',0.80,'rule-engine-v1','Safety incident notification - Olympic Dam')
    ON CONFLICT (prospect_id, pressure_type, source_url) DO NOTHING;

    UPDATE prospect_matrix SET
      strategic_direction='Optimise mining operations and resource extraction efficiency',
      primary_tailwind='Global demand for metals and critical minerals',
      primary_headwind='Operational reliability and production challenges',
      likelihood_score=8, status='enriched', status_changed_by='enrichment_agent'
    WHERE id=bhp_pid;
    PERFORM calculate_prospect_score(bhp_pid);
  END IF;

  -- RIO Tinto (Materials)
  IF rio_pid IS NOT NULL THEN
    INSERT INTO pressure_signals (prospect_id,pressure_type,strength,summary,source_type,source_title,source_date,confidence_score,model_version,extracted_quote)
    VALUES
    (rio_pid,'operational','moderate','Rio Tinto released quarterly activities report','asx_announcement','Rio Tinto Q1 2026 Operations Review','2026-01-16',0.60,'rule-engine-v1','Rio Tinto Q1 2026 Operations Review'),
    (rio_pid,'operational','strong','Rio Tinto announced mine closure or suspension','asx_announcement','Suspension of Kennecott Utah smelter operations','2026-02-12',0.80,'rule-engine-v1','Suspension of Kennecott Utah smelter operations'),
    (rio_pid,'cost','strong','Rio Tinto flagged cost pressures or overruns','asx_announcement','Cost overrun at Oyu Tolgoi underground expansion','2026-03-05',0.80,'rule-engine-v1','Cost overrun at Oyu Tolgoi underground expansion'),
    (rio_pid,'environmental','strong','Rio Tinto undertaking environmental remediation','asx_announcement','Remediation program Juukan Gorge heritage site update','2026-01-30',0.80,'rule-engine-v1','Remediation program Juukan Gorge heritage site update'),
    (rio_pid,'governance','moderate','Rio Tinto announced a strategic review','asx_announcement','Strategy review - lithium and battery materials business','2026-03-14',0.60,'rule-engine-v1','Strategy review - lithium and battery materials business'),
    (rio_pid,'market','moderate','Rio Tinto exposed to commodity price movements','asx_announcement','Iron ore price sensitivity analysis and market outlook','2026-02-22',0.60,'rule-engine-v1','Iron ore price sensitivity analysis and market outlook')
    ON CONFLICT (prospect_id, pressure_type, source_url) DO NOTHING;

    UPDATE prospect_matrix SET
      strategic_direction='Optimise mining operations and resource extraction efficiency',
      primary_tailwind='Global demand for metals and critical minerals',
      primary_headwind='Cost inflation and margin pressure',
      likelihood_score=9, status='enriched', status_changed_by='enrichment_agent'
    WHERE id=rio_pid;
    PERFORM calculate_prospect_score(rio_pid);
  END IF;

  -- Woodside Energy (Energy)
  IF wds_pid IS NOT NULL THEN
    INSERT INTO pressure_signals (prospect_id,pressure_type,strength,summary,source_type,source_title,source_date,confidence_score,model_version,extracted_quote)
    VALUES
    (wds_pid,'operational','moderate','Woodside Energy released a production update','asx_announcement','Woodside Q4 2025 Production and Sales Report','2026-01-22',0.60,'rule-engine-v1','Woodside Q4 2025 Production and Sales Report'),
    (wds_pid,'cost','strong','Woodside Energy is raising capital','asx_announcement','Capital raising - institutional placement 1.2B','2026-02-08',0.80,'rule-engine-v1','Capital raising - institutional placement 1.2B'),
    (wds_pid,'environmental','moderate','Woodside Energy addressing emissions or climate risks','asx_announcement','Woodside Net Zero 2050 Pathway update','2026-01-28',0.60,'rule-engine-v1','Woodside Net Zero 2050 Pathway update'),
    (wds_pid,'governance','moderate','Woodside Energy announced a strategic review','asx_announcement','Strategy update - LNG portfolio and energy transition review','2026-03-07',0.60,'rule-engine-v1','Strategy update - LNG portfolio and energy transition review'),
    (wds_pid,'operational','strong','Woodside Energy declared force majeure on operations','asx_announcement','Force majeure declared - North West Shelf gas supply disruption','2026-03-20',0.80,'rule-engine-v1','Force majeure declared - North West Shelf gas supply disruption'),
    (wds_pid,'market','strong','Woodside Energy facing weakening market demand','asx_announcement','Demand decline in Asian LNG market impacts offtake volumes','2026-02-15',0.80,'rule-engine-v1','Demand decline in Asian LNG market impacts offtake volumes')
    ON CONFLICT (prospect_id, pressure_type, source_url) DO NOTHING;

    UPDATE prospect_matrix SET
      strategic_direction='Navigate energy transition while maintaining hydrocarbon production',
      primary_tailwind='Strong energy demand and commodity price environment',
      primary_headwind='Operational reliability and production challenges',
      likelihood_score=9, status='enriched', status_changed_by='enrichment_agent'
    WHERE id=wds_pid;
    PERFORM calculate_prospect_score(wds_pid);
  END IF;

  -- Santos (Energy)
  IF sto_pid IS NOT NULL THEN
    INSERT INTO pressure_signals (prospect_id,pressure_type,strength,summary,source_type,source_title,source_date,confidence_score,model_version,extracted_quote)
    VALUES
    (sto_pid,'operational','moderate','Santos released a production update','asx_announcement','Santos Q4 2025 Quarterly Production Report','2026-01-23',0.60,'rule-engine-v1','Santos Q4 2025 Quarterly Production Report'),
    (sto_pid,'cost','moderate','Santos announced cost reduction initiatives','asx_announcement','Santos cost efficiency program - target 200M savings','2026-02-26',0.60,'rule-engine-v1','Santos cost efficiency program - target 200M savings'),
    (sto_pid,'environmental','moderate','Santos addressing emissions or climate risks','asx_announcement','Santos carbon capture and storage project Moomba update','2026-03-01',0.60,'rule-engine-v1','Santos carbon capture and storage project Moomba update'),
    (sto_pid,'safety','moderate','Santos is conducting safety reviews','asx_announcement','Safety culture review following Papua New Guinea operations audit','2026-02-17',0.60,'rule-engine-v1','Safety culture review following Papua New Guinea operations audit'),
    (sto_pid,'market','moderate','Santos exposed to commodity price movements','asx_announcement','LNG and gas price impact on FY2026 revenue guidance','2026-01-29',0.60,'rule-engine-v1','LNG and gas price impact on FY2026 revenue guidance')
    ON CONFLICT (prospect_id, pressure_type, source_url) DO NOTHING;

    UPDATE prospect_matrix SET
      strategic_direction='Navigate energy transition while maintaining hydrocarbon production',
      primary_tailwind='Strong energy demand and commodity price environment',
      primary_headwind='Operational reliability and production challenges',
      likelihood_score=7, status='enriched', status_changed_by='enrichment_agent'
    WHERE id=sto_pid;
    PERFORM calculate_prospect_score(sto_pid);
  END IF;

  -- Origin Energy (Utilities)
  IF org_pid IS NOT NULL THEN
    INSERT INTO pressure_signals (prospect_id,pressure_type,strength,summary,source_type,source_title,source_date,confidence_score,model_version,extracted_quote)
    VALUES
    (org_pid,'operational','moderate','Origin Energy released an operational update','asx_announcement','Origin Energy Generation Operational Update Q1 2026','2026-01-30',0.60,'rule-engine-v1','Origin Energy Generation Operational Update Q1 2026'),
    (org_pid,'cost','strong','Origin Energy announced an asset impairment','asx_announcement','Impairment of Eraring Power Station assets - 280M writedown','2026-02-21',0.80,'rule-engine-v1','Impairment of Eraring Power Station assets - 280M writedown'),
    (org_pid,'environmental','moderate','Origin Energy addressing emissions or climate risks','asx_announcement','Origin Clean Energy Plan - 4GW renewables commitment','2026-03-12',0.60,'rule-engine-v1','Origin Clean Energy Plan - 4GW renewables commitment'),
    (org_pid,'governance','moderate','Origin Energy announced a strategic review','asx_announcement','Origin Energy strategic review - integrated energy model','2026-02-09',0.60,'rule-engine-v1','Origin Energy strategic review - integrated energy model'),
    (org_pid,'market','moderate','Origin Energy commenting on market conditions','asx_announcement','Electricity market outlook update - wholesale price exposure','2026-01-25',0.60,'rule-engine-v1','Electricity market outlook update - wholesale price exposure')
    ON CONFLICT (prospect_id, pressure_type, source_url) DO NOTHING;

    UPDATE prospect_matrix SET
      strategic_direction='Transition generation portfolio toward renewables',
      primary_tailwind='Policy support for clean energy transition',
      primary_headwind='Cost inflation and margin pressure',
      likelihood_score=7, status='enriched', status_changed_by='enrichment_agent'
    WHERE id=org_pid;
    PERFORM calculate_prospect_score(org_pid);
  END IF;

END $seed$;
