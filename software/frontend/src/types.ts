export type AssetKind = "WHOLE_MACHINE" | "MODULE";
export type TargetPartCode =
  | "ARM"
  | "SARM"
  | "LEG"
  | "SLEG"
  | "SYS"
  | "UPPER"
  | "LOWER"
  | "CHEST"
  | "HEAD"
  | "BAT";

export type Freshness = {
  as_of_at: string;
  data_cutoff_at: string | null;
  last_received_at: string | null;
  lag_seconds: number | null;
  status: "FRESH" | "STALE" | "NO_DATA";
};

export type DurationTotals = {
  total_duration_seconds: number;
  effective_exposure_seconds: number;
  excluded_seconds: number;
  pending_seconds: number;
  active_elapsed_seconds: number;
};

export type HomeTestCase = {
  id: string;
  code: string;
  name: string;
  target_part_code: TargetPartCode;
  target_part_name: string;
  status: string;
  current_elapsed_seconds: number;
  cumulative_duration_seconds: number;
  effective_exposure_seconds: number;
  excluded_seconds: number;
  pending_seconds: number;
  execution_count: number;
  scope_asset_count: number;
  historical_execution_count: number;
  platform_execution_count: number;
  passed_execution_count: number;
  failed_execution_count: number;
  inconclusive_execution_count: number;
  not_evaluated_execution_count: number;
  exception_count: number;
  first_recorded_at: string | null;
  latest_recorded_at: string | null;
  station_codes: string[];
  target_duration_seconds: number | null;
  target_progress_percent: number | null;
};

export type HomeScope = {
  id: string;
  code: string;
  name: string;
  asset_kind: AssetKind;
  target_part_code: TargetPartCode;
  target_part_name: string;
  status: string;
  part_durations: DurationTotals;
  part_asset_count: number;
  active_execution_count: number;
  abnormal_asset_count: number;
  test_cases: HomeTestCase[];
};

export type SourceHealth = {
  id: string;
  code: string;
  source_type: string;
  configured_status: string;
  last_received_at: string | null;
  last_receipt_status: "ACCEPTED" | "DUPLICATE" | "REJECTED" | "PARTIAL" | null;
  last_success_at: string | null;
  lag_seconds: number | null;
  freshness_status: "FRESH" | "STALE" | "NO_DATA" | "DISABLED" | "DEGRADED" | "ERROR";
  last_error: string | null;
};

export type DataSourceHealth = {
  as_of_at: string;
  data_cutoff_at: string | null;
  sources: SourceHealth[];
  healthy_count: number;
  warning_count: number;
};

export type DashboardHome = {
  freshness: Freshness;
  data_sources: DataSourceHealth;
  anomaly_count: number;
  anomalies: string[];
  groups: Array<{ asset_kind: AssetKind; objects: HomeScope[] }>;
};

export type PageMeta = { limit: number; next_cursor: string | null };

export type TestCaseDurationItem = {
  id: string;
  code: string;
  name: string;
  asset_kind: AssetKind;
  asset_id: string;
  asset_code: string;
  asset_name: string;
  status: string;
  execution_count: number;
  durations: DurationTotals;
  data_cutoff_at: string | null;
};

export type TestCaseDurationList = {
  items: TestCaseDurationItem[];
  page: PageMeta;
  totals: DurationTotals;
  as_of_at: string;
  data_cutoff_at: string | null;
};

export type AssetSummary = {
  id: string;
  code: string;
  name: string;
  asset_kind: AssetKind;
  target_part_code: TargetPartCode | null;
  target_part_name: string | null;
  product_family: string;
  batch_code: string | null;
  serial_number: string;
  lifecycle_status: string;
  current_test_case_code: string | null;
  current_execution_id: string | null;
  current_execution_code: string | null;
  health_status: string | null;
  durations: DurationTotals;
  freshness: Freshness;
};

export type AssetList = {
  items: AssetSummary[];
  page: PageMeta;
  as_of_at: string;
  data_cutoff_at: string | null;
};

export type Configuration = {
  id: string | null;
  fingerprint: string | null;
  effective_from: string | null;
  reliability_impact: string | null;
  hardware_manifest: Record<string, unknown>;
  software_manifest: Record<string, unknown>;
  parameter_manifest: Record<string, unknown>;
};

export type CurrentContext = {
  campaign_code: string | null;
  cycle_code: string | null;
  execution_id: string | null;
  execution_code: string | null;
  test_case_code: string | null;
  stage_code: string | null;
  stage_name: string | null;
  status: string | null;
  started_at: string | null;
  current_elapsed_seconds: number;
  is_stale: boolean;
};

export type AssetDetail = {
  asset: AssetSummary;
  current_context: CurrentContext;
  configuration: Configuration;
  execution_count: number;
  event_count: number;
  health_summary: Record<string, number>;
  evidence_summary: { total_count: number; available_count: number; missing_count: number };
  as_of_at: string;
  data_cutoff_at: string | null;
};

export type PerformanceTrendPoint = {
  observed_at: string;
  elapsed_seconds: number;
  progress_percent: number;
  value: number;
  quality_status: string;
};

export type PerformanceTrendSeries = {
  stage_code: string;
  stage_name: string;
  sequence_no: number;
  status: string;
  points: PerformanceTrendPoint[];
  min_value: number;
  max_value: number;
  avg_value: number;
};

export type PerformanceTrendMetric = {
  metric_code: string;
  metric_name: string;
  unit: string;
  series: PerformanceTrendSeries[];
};

export type AssetPerformanceTrends = {
  asset_id: string;
  asset_code: string;
  asset_name: string;
  execution_id: string | null;
  execution_code: string | null;
  test_case_code: string | null;
  metrics: PerformanceTrendMetric[];
  as_of_at: string;
  data_cutoff_at: string | null;
};

export type ExecutionListItem = {
  id: string;
  code: string;
  asset_id: string;
  asset_code: string;
  test_case_id: string;
  test_case_code: string;
  test_case_name: string;
  campaign_code: string;
  cycle_code: string;
  stage_count: number;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  closed_duration_seconds: number;
  active_elapsed_seconds: number;
  data_quality: string;
  clock_quality: string;
  station_code?: string | null;
};

export type ExecutionList = {
  items: ExecutionListItem[];
  page: PageMeta;
  as_of_at: string;
  data_cutoff_at: string | null;
};

export type EventListItem = {
  id: string;
  event_type: string;
  normalized_time: string;
  execution_id: string | null;
  data_quality: string;
  clock_quality: string;
  payload: Record<string, unknown>;
  interruption_classification: string | null;
  interruption_review_status: string | null;
};

export type EventList = {
  items: EventListItem[];
  page: PageMeta;
  as_of_at: string;
  data_cutoff_at: string | null;
};

export type MtbfObservation = {
  scope: string;
  method: string | null;
  data_cutoff_at: string | null;
  effective_exposure_seconds: number;
  relevant_failure_count: number;
  pending_failure_count: number;
  pending_exposure_seconds: number;
  point_estimate_hours: number | null;
  point_estimate_status: string;
  lower_bounds_hours: Record<string, number>;
};

export type TestCaseDetail = {
  id: string;
  code: string;
  name: string;
  asset_kind: AssetKind;
  target_part_code: TargetPartCode | null;
  target_part_name: string | null;
  domain: string;
  evidence_type: string;
  enabled: boolean;
  latest_version: string | null;
  status_counts: Record<string, number>;
  durations: DurationTotals;
  mtbf_observation: MtbfObservation | null;
  executions: ExecutionList;
  as_of_at: string;
  data_cutoff_at: string | null;
};

export type MetricObservation = {
  id: string;
  metric_code: string;
  metric_name: string;
  joint: string | null;
  value: number | null;
  text_value: string | null;
  unit: string | null;
  observed_at: string;
  quality_status: string;
  formal_eligible: boolean;
};

export type ExecutionResult = {
  source_status: "passed" | "failed" | "blocked" | "scheduled" | "running";
  outcome: "PASSED" | "FAILED" | "INCONCLUSIVE" | "NOT_EVALUATED";
  termination_kind:
    | "NORMAL"
    | "MANUAL_STOP"
    | "SAFETY_WATCHDOG"
    | "DATA_TIMEOUT"
    | "SYSTEM_CRASH"
    | "SCHEDULED"
    | "RUNNING"
    | "UNKNOWN";
  summary: string;
  issues: string;
  exception_count: number;
  reported_duration_seconds: number;
  completed_cycles?: number | null;
  executor_display: string;
  environment_label: string;
  last_data_at: string | null;
  telemetry_source: string;
  archive_status: "PENDING" | "IMPORTED" | "ARCHIVED" | "MISSING";
  archive_path: string;
  report_reference: string;
  normalization_flags: Array<Record<string, unknown>>;
};

export type JointRecordSummary = {
  joint: string;
  source_kind: string;
  sample_count: number;
  mean_abs_torque_nm: number | null;
  peak_abs_torque_nm: number | null;
  bus_voltage_mean_v: number | null;
  bus_voltage_min_v: number | null;
  bus_voltage_max_v: number | null;
  bus_current_mean_abs_a: number | null;
  bus_current_peak_abs_a: number | null;
  estimated_mean_power_w: number | null;
  temp_start_c: number | null;
  temp_end_c: number | null;
  temp_rise_c: number | null;
  temp_max_c: number | null;
  tracking_rms_deg: number | null;
  formal_eligible: boolean;
};

export type ExecutionDetail = {
  execution: ExecutionListItem;
  configuration: Configuration;
  station_code: string | null;
  station_name: string | null;
  operator_name: string | null;
  source_code: string | null;
  result: ExecutionResult | null;
  peak_summary: Array<{
    metric_code: string;
    metric_name: string;
    value: number;
    unit: string | null;
    observed_at: string;
  }>;
  metrics: MetricObservation[];
  joint_records?: JointRecordSummary[];
  data_quality_summary: Record<string, unknown>;
  evidence: Array<{
    id: string;
    kind: string;
    file_name: string;
    mime_type: string;
    size_bytes: number | null;
    availability_status: string;
    sha256: string | null;
  }>;
  as_of_at: string;
  data_cutoff_at: string | null;
};

export type MetricQualityStatus = "VALID" | "PARTIAL" | "INVALID" | "MISSING";

export type JointAnalysisSubject = {
  subject_code: string;
  name: string;
  subject_kind: "JOINT" | "BATTERY_CHANNEL" | "FRAME_POINT";
  target_part_code: string | null;
  display_order: number;
  enabled: boolean;
};

export type JointAnalysisStage = {
  id: string;
  stage_code: string;
  stage_name: string;
  sequence_no: number;
  status: string;
};

export type JointAnalysisCycle = {
  cycle_index: number;
  started_at: string | null;
  ended_at: string | null;
  quality_status: MetricQualityStatus;
  series_count: number;
};

export type JointAnalysisMetric = {
  metric_code: string;
  metric_name: string;
  unit: string;
};

export type JointAnalysisSummaryItem = {
  code: string;
  label: string;
  source_metric_code: string | null;
  value: number | null;
  unit: string;
  sample_count: number;
  quality_status: "VALID" | "PARTIAL" | "NO_DATA" | "NOT_CALCULABLE";
};

export type JointAnalysisPoint = {
  sample_index: number;
  elapsed_seconds: number;
  progress_percent: number | null;
  value: number | null;
  observed_at: string;
  quality_status: MetricQualityStatus;
};

export type JointAnalysisSeries = {
  id: string;
  metric_code: string;
  metric_name: string;
  unit: string;
  subject_code: string;
  stage_code: string;
  series_kind: string;
  cycle_index: number;
  point_count: number;
  display_point_count: number;
  started_at: string | null;
  ended_at: string | null;
  sampling_interval_ms: number | null;
  downsample_method: string | null;
  quality_status: MetricQualityStatus;
  points: JointAnalysisPoint[];
};

export type ExecutionJointAnalysis = {
  execution_id: string;
  execution_code: string;
  subjects: JointAnalysisSubject[];
  stages: JointAnalysisStage[];
  cycles: JointAnalysisCycle[];
  metrics: JointAnalysisMetric[];
  resolved_selection: {
    subject_code: string | null;
    stage_code: string | null;
    cycle_index: number | null;
  };
  summary: JointAnalysisSummaryItem[];
  series: JointAnalysisSeries[];
  as_of_at: string;
  data_cutoff_at: string | null;
};

export type MtbfConclusionItem = {
  campaign_id: string;
  campaign_code: string;
  campaign_name: string;
  asset_kind: AssetKind;
  scope: string;
  scope_name: string;
  target_duration_seconds: number | null;
  observed: {
    effective_exposure_seconds: number;
    relevant_failure_count: number;
    pending_failure_count: number;
    pending_exposure_seconds: number;
    excluded_seconds: number;
    data_cutoff_at: string | null;
  };
  statistical: {
    method: string;
    point_estimate_hours: number | null;
    point_estimate_status: string;
    lower_bounds_hours: Record<string, number>;
    calculation_status: string;
    run_id: string | null;
    calculated_at: string | null;
  };
  verified: {
    status: string;
    conclusion: Record<string, unknown>;
    evidence_completeness: Record<string, unknown>;
    published_at: string | null;
  };
  blockers: string[];
};

export type MtbfConclusionList = {
  items: MtbfConclusionItem[];
  page: PageMeta;
  as_of_at: string;
  data_cutoff_at: string | null;
};
