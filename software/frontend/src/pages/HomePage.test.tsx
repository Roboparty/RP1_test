// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { HomePage } from './HomePage';
import { getDashboardHome } from '../api';
import type { DashboardHome } from '../types';

vi.mock('../api', () => ({ getDashboardHome: vi.fn() }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it.each(['WHOLE_MACHINE', 'MODULE'] as const)('renders model and navigable test cases for %s with zero samples', async (kind) => {
  vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  const part = kind === 'MODULE' ? 'SLEG' : 'SYS';
  const zero = { total_duration_seconds: 0, effective_exposure_seconds: 0, excluded_seconds: 0, pending_seconds: 0, active_elapsed_seconds: 0 };
  const home = { freshness: { status: 'NO_DATA', as_of_at: '2026-10-08T00:00:00Z', data_cutoff_at: null }, data_sources: { sources: [], healthy_count: 0, warning_count: 0 }, anomaly_count: 0, anomalies: [], groups: [{ asset_kind: kind, objects: [{ id: part, code: part, name: '参考模型', target_part_name: '参考模型', target_part_code: part, asset_kind: kind, status: 'NO_DATA', part_asset_count: 0, active_execution_count: 0, abnormal_asset_count: 0, part_durations: zero, test_cases: [1, 2].map(n => ({ id: String(n), code: `QA-${part}-${n}`, name: `目录用例${n}`, domain: 'RELIABILITY', current_elapsed_seconds: 0, cumulative_duration_seconds: 0, effective_exposure_seconds: 0, excluded_seconds: 0, pending_seconds: 0, execution_count: 0, scope_asset_count: 0, historical_execution_count: 0, platform_execution_count: 0, passed_execution_count: 0, failed_execution_count: 0, inconclusive_execution_count: 0, not_evaluated_execution_count: 0, exception_count: 0, first_recorded_at: null, latest_recorded_at: null, station_codes: [], target_duration_seconds: null, target_progress_percent: null })) }] }] } as unknown as DashboardHome;
  if (kind === 'MODULE') {
    const scope = home.groups[0].objects[0];
    home.groups[0].objects.push({ ...scope, id: 'SARM', code: 'SARM', target_part_code: 'SARM', target_part_name: '单臂' });
    home.groups.push({ asset_kind: 'WHOLE_MACHINE', objects: [{ ...scope, id: 'SYS', code: 'SYS', target_part_code: 'SYS', target_part_name: '整机', asset_kind: 'WHOLE_MACHINE' }] });
  }
  vi.mocked(getDashboardHome).mockResolvedValue(home);
  const navigate = vi.fn();
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={cache}><HomePage assetKind={kind} navigate={navigate} /></QueryClientProvider>);
  const image = await screen.findByAltText(kind === 'MODULE' ? '单腿外观' : '参考模型外观');
  expect(image.getAttribute('src')).toBe(kind === 'MODULE' ? '/model-posters/single-leg-product-white.webp' : '/assets/humanoid-robot.webp');
  expect(screen.getByText('0 台样品 / 2 项用例')).toBeTruthy();
  expect(screen.queryByText(/尚无可访问/)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '下一个 Test Case' }));
  expect(document.querySelector('.case-flip-viewport')?.getAttribute('data-active-index')).toBe('1');
  if (kind === 'MODULE') {
    fireEvent.click(screen.getByRole('button', { name: /单臂\s*ARM · 0 台/ }));
    expect(document.querySelector('.object-identity > span')?.textContent).toContain('02 / 02');
    view.rerender(<QueryClientProvider client={cache}><HomePage assetKind="WHOLE_MACHINE" navigate={navigate} /></QueryClientProvider>);
    expect(screen.getByAltText('整机外观')).toBeTruthy();
    expect(document.querySelector('.object-identity > span')?.textContent).toContain('01 / 01');
    expect(document.querySelector('.asset-index > span')?.textContent).toContain('/ 01');
  }
  vi.unstubAllGlobals();
});

it('shows recorded executions by their own status without a historical label', async () => {
  vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  const durations = { total_duration_seconds: 36000, effective_exposure_seconds: 0, excluded_seconds: 0, pending_seconds: 0, active_elapsed_seconds: 0 };
  const testCase = {
    id: 'case-1', code: 'REL-SARM-001', name: '单臂长时老化', target_part_code: 'SARM', target_part_name: '单臂',
    status: 'RUNNING', current_elapsed_seconds: 0, cumulative_duration_seconds: 36000, effective_exposure_seconds: 0,
    excluded_seconds: 0, pending_seconds: 0, execution_count: 3, scope_asset_count: 1,
    historical_execution_count: 3, platform_execution_count: 0, passed_execution_count: 2,
    failed_execution_count: 1, inconclusive_execution_count: 0, not_evaluated_execution_count: 0,
    exception_count: 4, first_recorded_at: '2026-09-01T01:00:00Z', latest_recorded_at: '2026-09-03T02:00:00Z',
    station_codes: ['RD-01', 'RD-02'], target_duration_seconds: null, target_progress_percent: null
  };
  vi.mocked(getDashboardHome).mockResolvedValue({
    freshness: { status: 'STALE', as_of_at: '2026-10-12T00:00:00Z', data_cutoff_at: '2026-09-03T02:00:00Z', last_received_at: '2026-09-03T02:00:00Z', lag_seconds: 1 },
    data_sources: { as_of_at: '2026-10-12T00:00:00Z', data_cutoff_at: '2026-09-03T02:00:00Z', sources: [], healthy_count: 0, warning_count: 0 },
    anomaly_count: 0, anomalies: [],
    groups: [{ asset_kind: 'MODULE', objects: [{
      id: 'MODULE:SARM', code: 'SARM', name: '单臂', target_part_name: '单臂', target_part_code: 'SARM',
      asset_kind: 'MODULE', status: 'RUNNING', part_asset_count: 1, active_execution_count: 0,
      abnormal_asset_count: 0, part_durations: durations, test_cases: [testCase]
    }] }]
  } as DashboardHome);
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={cache}><HomePage assetKind="MODULE" navigate={vi.fn()} /></QueryClientProvider>);

  expect(await screen.findAllByText('RUNNING')).toHaveLength(2);
  expect(screen.getByText('3 条')).toBeTruthy();
  expect(screen.getByText('4 次')).toBeTruthy();
  expect(screen.getByText(/台架 RD-01 \/ RD-02 · 异常 4/)).toBeTruthy();
  expect(screen.queryByText(/历史/)).toBeNull();
  expect(screen.queryByText(/平台记录/)).toBeNull();
  vi.unstubAllGlobals();
});

it('hides and restores cases whose recorded duration is zero', async () => {
  vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  const durations = { total_duration_seconds: 3600, effective_exposure_seconds: 0, excluded_seconds: 0, pending_seconds: 0, active_elapsed_seconds: 0 };
  const base = {
    target_part_code: 'ARM', target_part_name: '7自由度单臂', status: 'COMPLETED', current_elapsed_seconds: 0,
    effective_exposure_seconds: 0, excluded_seconds: 0, pending_seconds: 0, scope_asset_count: 1,
    historical_execution_count: 0, platform_execution_count: 0, passed_execution_count: 0,
    failed_execution_count: 0, inconclusive_execution_count: 0, not_evaluated_execution_count: 0,
    exception_count: 0, first_recorded_at: null, latest_recorded_at: null, station_codes: [],
    target_duration_seconds: null, target_progress_percent: null
  };
  vi.mocked(getDashboardHome).mockResolvedValue({
    freshness: { status: 'FRESH', as_of_at: '2026-10-14T00:00:00Z', data_cutoff_at: null },
    data_sources: { sources: [], healthy_count: 0, warning_count: 0 },
    anomaly_count: 0, anomalies: [],
    groups: [{ asset_kind: 'MODULE', objects: [{
      id: 'MODULE:ARM', code: 'ARM', name: '7自由度单臂', target_part_name: '7自由度单臂', target_part_code: 'ARM',
      asset_kind: 'MODULE', status: 'COMPLETED', part_asset_count: 1, active_execution_count: 0,
      abnormal_asset_count: 0, part_durations: durations, test_cases: [
        { ...base, id: 'zero', code: 'REL-ARM-001', name: '空载', cumulative_duration_seconds: 0, execution_count: 0 },
        { ...base, id: 'recorded', code: 'REL-ARM-002', name: '带载', cumulative_duration_seconds: 3600, execution_count: 1 }
      ]
    }] }]
  } as unknown as DashboardHome);
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={cache}><HomePage assetKind="MODULE" navigate={vi.fn()} /></QueryClientProvider>);
  expect(await screen.findByText('REL-ARM-001', { selector: '.case-code' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '隐藏累计时长为 0 的用例' }));
  expect(screen.queryByText('REL-ARM-001', { selector: '.case-code' })).toBeNull();
  expect(screen.getByText('REL-ARM-002', { selector: '.case-code' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '显示累计时长为 0 的用例' }));
  expect(screen.getByText('REL-ARM-001', { selector: '.case-code' })).toBeTruthy();
  vi.unstubAllGlobals();
});
