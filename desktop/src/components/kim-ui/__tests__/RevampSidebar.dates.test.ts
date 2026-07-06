import { describe, it, expect, vi, afterEach } from 'vitest';
import type { SessionInfo } from '../../../types';
import { groupByDate, formatTime } from '../RevampSidebar';

// Helper: build a minimal SessionInfo with only the fields groupByDate/formatTime care about.
function makeSession(date: string): SessionInfo {
  return {
    session_id: 'test-' + date,
    session_type: 'kim',
    date,
    message_count: 1,
    has_summary: false,
  };
}

// Helper: format a local date as 'YYYY-MM-DD' without UTC shift.
function localDateStr(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

afterEach(() => {
  vi.useRealTimers();
});

describe('today_buckets_as_today_local', () => {
  it('a session whose date matches local today lands in the Today bucket', () => {
    vi.useFakeTimers();
    // Pin "now" to a fixed local date: 2025-03-15
    vi.setSystemTime(new Date(2025, 2, 15, 14, 0, 0)); // month is 0-indexed

    const todayStr = localDateStr(new Date()); // '2025-03-15'
    const groups = groupByDate([makeSession(todayStr)]);

    expect(groups.length).toBeGreaterThan(0);
    expect(groups[0].label).toBe('Today');
    expect(groups[0].items).toHaveLength(1);
    expect(groups[0].items[0].date).toBe(todayStr);
  });

  it('a session dated one day in the future (edge) still lands in Today (ts >= startOfToday)', () => {
    vi.useFakeTimers();
    // "now" is 23:59 on 2025-03-15; tomorrow is 2025-03-16
    vi.setSystemTime(new Date(2025, 2, 15, 23, 59, 0));

    const todayStr = localDateStr(new Date()); // '2025-03-15'
    const groups = groupByDate([makeSession(todayStr)]);

    expect(groups[0].label).toBe('Today');
    expect(groups[0].items[0].date).toBe(todayStr);
  });

  it('does not bucket a two-days-ago session as Today', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2025, 2, 15, 12, 0, 0));

    const twoDaysAgo = localDateStr(new Date(2025, 2, 13));
    const groups = groupByDate([makeSession(twoDaysAgo)]);

    // Must not be Today
    const todayGroup = groups.find(g => g.label === 'Today');
    expect(todayGroup).toBeUndefined();
  });
});

describe('yesterday_bucket_correct', () => {
  it('a session dated local yesterday lands in the Yesterday bucket', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2025, 2, 15, 10, 0, 0)); // 2025-03-15

    const yesterdayStr = localDateStr(new Date(2025, 2, 14)); // '2025-03-14'
    const groups = groupByDate([makeSession(yesterdayStr)]);

    const yGroup = groups.find(g => g.label === 'Yesterday');
    expect(yGroup).toBeDefined();
    expect(yGroup!.items).toHaveLength(1);
    expect(yGroup!.items[0].date).toBe(yesterdayStr);
  });

  it('yesterday does not bleed into Today when the date string is parsed as local midnight', () => {
    vi.useFakeTimers();
    // Use a time early in the day to highlight any UTC-shift issue.
    vi.setSystemTime(new Date(2025, 5, 1, 1, 0, 0)); // 2025-06-01 01:00 local

    const yesterdayStr = localDateStr(new Date(2025, 4, 31)); // '2025-05-31'
    const groups = groupByDate([makeSession(yesterdayStr)]);

    const todayGroup = groups.find(g => g.label === 'Today');
    const yGroup = groups.find(g => g.label === 'Yesterday');

    expect(todayGroup).toBeUndefined();
    expect(yGroup).toBeDefined();
    expect(yGroup!.items[0].date).toBe(yesterdayStr);
  });

  it('sessions 3 days ago land in This week, not Yesterday', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2025, 2, 15, 12, 0, 0)); // 2025-03-15

    const threeDaysAgo = localDateStr(new Date(2025, 2, 12)); // '2025-03-12'
    const groups = groupByDate([makeSession(threeDaysAgo)]);

    const yGroup = groups.find(g => g.label === 'Yesterday');
    const weekGroup = groups.find(g => g.label === 'This week');

    expect(yGroup).toBeUndefined();
    expect(weekGroup).toBeDefined();
    expect(weekGroup!.items[0].date).toBe(threeDaysAgo);
  });
});

describe('formatTime_local_parse', () => {
  it('returns a weekday short name for a date 2 days ago (not same-day format)', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2025, 2, 15, 12, 0, 0)); // Saturday 2025-03-15

    const twoDaysAgo = localDateStr(new Date(2025, 2, 13)); // '2025-03-13' (Thursday)
    const result = formatTime(twoDaysAgo);

    // Within 7 days and not same-day → weekday short name (e.g. "Thu").
    // Positive shape assertion: mutation testing showed "not a time" +
    // "non-empty" passed even when the wrong branch returned a month-day.
    expect(result).toMatch(/^[A-Za-z]{2,}\.?$/); // letters only — a weekday name
    expect(result).not.toMatch(/\d/); // no digits: not a time, not a month-day
  });

  it('returns a locale time string for a today date string (same-day path)', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2025, 2, 15, 14, 30, 0)); // 2025-03-15 14:30 local

    // For same-day, formatTime uses Date.toLocaleTimeString which returns a time string.
    // Since date-only strings parse to midnight, same-day = today midnight = today.
    const todayStr = localDateStr(new Date()); // '2025-03-15'
    const result = formatTime(todayStr);

    // Same-day path: MUST be a time string like "12:00 AM" / "12:00" —
    // positive HH:MM shape, not merely "contains a digit" (which a wrong
    // month-day branch also satisfies).
    expect(result).toMatch(/\d{1,2}:\d{2}/);
  });

  it('returns empty string for an invalid date', () => {
    const result = formatTime('not-a-date');
    expect(result).toBe('');
  });

  it('returns a month+day string for a date older than 7 days', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2025, 2, 15, 12, 0, 0)); // 2025-03-15

    const oldDate = localDateStr(new Date(2025, 2, 1)); // '2025-03-01' (14 days ago)
    const result = formatTime(oldDate);

    // Older than 7 days: month short name + day number, e.g. "Mar 1".
    expect(result).toMatch(/[A-Za-z]{3}/); // month name present
    expect(result).toMatch(/\d{1,2}/); // day number present
    expect(result).not.toMatch(/\d{1,2}:\d{2}/); // and it is NOT a time
  });

  it('correctly parses YYYY-MM-DD as local midnight (not UTC)', () => {
    vi.useFakeTimers();
    // Set timezone context: use a fixed date where UTC offset matters.
    // If date-only is parsed as UTC midnight, a negative-UTC-offset machine
    // would see "2025-03-14T00:00Z" = "2025-03-13T19:00-05:00" (yesterday local).
    // Our fix always constructs new Date(y, m-1, d) so it stays 2025-03-14 local.
    vi.setSystemTime(new Date(2025, 2, 15, 12, 0, 0)); // 2025-03-15 local

    const yesterdayStr = '2025-03-14';
    const groups = groupByDate([makeSession(yesterdayStr)]);

    // With local-midnight parsing, 2025-03-14 must be Yesterday, not Earlier/This-week.
    const yGroup = groups.find(g => g.label === 'Yesterday');
    expect(yGroup).toBeDefined();
    expect(yGroup!.items[0].date).toBe(yesterdayStr);
  });
});
