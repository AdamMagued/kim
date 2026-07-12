/**
 * PaneSchedule -- schedule management UI for the Settings modal.
 *
 * Calls the Tauri schedule_commands IPC bridge. All scheduling logic lives in
 * Python (orchestrator/cron_store.py + scheduled_runner.py).
 *
 * Provider choices are constrained to the executor allowlist to honour the
 * standing project constraint (never openai / gpt-5.5).
 */

import { useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { Settings } from '../../types';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ScheduledTask {
  id: string;
  task: string;
  schedule_expr: string;
  provider?: string | null;
  enabled: boolean;
  run_count?: number;
  next_run_at?: string | null;
  last_run_at?: string | null;
  created_at?: string;
}

export interface RunDueResponse {
  ok: boolean;
  launched?: boolean;
  recorded?: boolean;
  skipped?: boolean;
  skip_reason?: string;
  error?: string;
  reason?: string;
  task_id?: string;
  task?: string;
}

export interface ScheduleTimerStatus {
  running: boolean;
  interval_seconds: number;
  tick_count: number;
  last_result?: string | null;
  last_error?: string | null;
}

// ---------------------------------------------------------------------------
// Pure helpers (exported for tests)
// ---------------------------------------------------------------------------

/** Parse the JSON array returned by list_scheduled_tasks. */
export function parseTaskList(json: string): ScheduledTask[] {
  try {
    const data = JSON.parse(json);
    if (!Array.isArray(data)) return [];
    return data.filter(
      (t): t is ScheduledTask =>
        typeof t === 'object' &&
        t !== null &&
        typeof t.id === 'string' &&
        typeof t.task === 'string',
    );
  } catch {
    return [];
  }
}

/** Parse the JSON object returned by run_due_scheduled_task. */
export function parseRunDueResponse(json: string): RunDueResponse {
  try {
    const data = JSON.parse(json);
    if (typeof data !== 'object' || data === null || Array.isArray(data)) {
      return { ok: false, error: 'Invalid response from run-due' };
    }
    return data as RunDueResponse;
  } catch {
    return { ok: false, error: 'Failed to parse run-due response' };
  }
}

/** Parse the JSON object returned by get/start/stop schedule timer commands. */
export function parseTimerStatus(data: unknown): ScheduleTimerStatus {
  if (typeof data === 'string') {
    try {
      return parseTimerStatus(JSON.parse(data));
    } catch {
      return { running: false, interval_seconds: 0, tick_count: 0, last_error: 'Failed to parse timer status' };
    }
  }
  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    return { running: false, interval_seconds: 0, tick_count: 0, last_error: 'Invalid timer status' };
  }
  const raw = data as Record<string, unknown>;
  return {
    running: raw.running === true,
    interval_seconds: typeof raw.interval_seconds === 'number' ? raw.interval_seconds : 0,
    tick_count: typeof raw.tick_count === 'number' ? raw.tick_count : 0,
    last_result: typeof raw.last_result === 'string' ? raw.last_result : null,
    last_error: typeof raw.last_error === 'string' ? raw.last_error : null,
  };
}

/**
 * Extract a human-readable error from an invoke() rejection.
 * Tauri forwards the Err(String) from Rust directly as the rejection value.
 * kimctl also writes JSON {"ok":false,"error":"..."} to stdout on exit 1,
 * which run_kimctl surfaces as the rejection string -- try to parse it.
 */
export function extractInvokeError(err: unknown): string {
  if (typeof err !== 'string') return String(err);
  try {
    const parsed = JSON.parse(err);
    if (typeof parsed === 'object' && parsed !== null) {
      if (typeof parsed.error === 'string' && parsed.error) return parsed.error;
      if (typeof parsed.message === 'string' && parsed.message) return parsed.message;
    }
  } catch {
    // not JSON -- fall through
  }
  return err;
}

/** Format a next_run_at ISO string as a short human relative time. */
export function formatNextRun(nextRunAt: string | null | undefined): string {
  if (!nextRunAt) return '--';
  try {
    const d = new Date(nextRunAt);
    const diff = d.getTime() - Date.now();
    if (Number.isNaN(diff)) return '--';
    if (diff < -60_000) return 'overdue';
    if (diff < 60_000) return 'due now';
    if (diff < 3_600_000) return `in ${Math.floor(diff / 60_000)}m`;
    if (diff < 86_400_000) return `in ${Math.floor(diff / 3_600_000)}h`;
    return `in ${Math.floor(diff / 86_400_000)}d`;
  } catch {
    return '--';
  }
}

/** Summarize a raw timer last_result JSON payload for compact display. */
export function formatTimerLastResult(json: string): string {
  const result = parseRunDueResponse(json);
  return result.reason || result.task || result.task_id || result.error || 'ok';
}

// ---------------------------------------------------------------------------
// Provider options (mirrors _ALLOWED_RE in scheduled_runner.py)
// ---------------------------------------------------------------------------

const PROVIDER_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'default (ollama)' },
  { value: 'ollama', label: 'ollama' },
  { value: 'ollama-cloud', label: 'ollama-cloud' },
  { value: 'browser', label: 'browser' },
  { value: 'browser:gemini', label: 'browser:gemini' },
  { value: 'browser:chatgpt', label: 'browser:chatgpt' },
  { value: 'browser:claude', label: 'browser:claude' },
];

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

const S = {
  row: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    background: 'var(--kim-surface)',
    border: '1px solid var(--kim-border)',
    borderRadius: 10,
    padding: '10px 13px',
    marginBottom: 8,
  } as CSSProperties,
  label: { fontSize: 13, color: 'var(--kim-text)' } as CSSProperties,
  sub: { fontSize: 11.5, color: 'var(--kim-text-3)' } as CSSProperties,
  badge: {
    fontSize: 11,
    color: 'var(--kim-text-3)',
    background: 'var(--kim-bg-2)',
    border: '1px solid var(--kim-border)',
    borderRadius: 5,
    padding: '2px 6px',
    fontFamily: 'monospace',
    whiteSpace: 'nowrap',
  } as CSSProperties,
  err: {
    color: 'var(--kim-red, #e57373)',
    fontSize: 12.5,
    padding: '8px 12px',
    background: 'rgba(229,115,115,0.08)',
    borderRadius: 8,
    marginBottom: 10,
  } as CSSProperties,
  ok: {
    color: 'var(--kim-green, #81c784)',
    fontSize: 12.5,
    padding: '8px 12px',
    background: 'rgba(129,199,132,0.08)',
    borderRadius: 8,
    marginBottom: 10,
  } as CSSProperties,
};

function TaskRow({
  task,
  onToggle,
  onDelete,
}: {
  task: ScheduledTask;
  onToggle: (id: string, enabled: boolean) => void;
  onDelete: (id: string) => void;
}) {
  const providerLabel = task.provider || 'ollama';
  const shortTask = task.task.length > 55 ? task.task.slice(0, 54) + '...' : task.task;
  return (
    <div style={{ ...S.row, opacity: task.enabled ? 1 : 0.6 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={S.label} title={task.task}>{shortTask}</div>
        <div style={{ display: 'flex', gap: 6, marginTop: 4, flexWrap: 'wrap' }}>
          <span style={S.badge}>{task.schedule_expr}</span>
          <span style={S.badge}>{providerLabel}</span>
          {typeof task.run_count === 'number' && (
            <span style={S.sub}>runs: {task.run_count}</span>
          )}
          {task.next_run_at && (
            <span style={S.sub}>{formatNextRun(task.next_run_at)}</span>
          )}
        </div>
      </div>
      <button
        type="button"
        className={`kr-toggle${task.enabled ? ' kr-on' : ''}`}
        onClick={() => onToggle(task.id, !task.enabled)}
        aria-pressed={task.enabled}
        title={task.enabled ? 'Disable' : 'Enable'}
      />
      <button
        type="button"
        className="kr-icon-btn"
        onClick={() => onDelete(task.id)}
        aria-label="Delete task"
        style={{ width: 28, height: 28, color: 'var(--kim-text-3)' }}
        title="Delete"
      >
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
          <path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      </button>
    </div>
  );
}

interface AddFormState {
  task: string;
  expr: string;
  provider: string;
  disabled: boolean;
}

function AddForm({
  onAdd,
  onCancel,
  adding,
  addError,
}: {
  onAdd: (form: AddFormState) => void;
  onCancel: () => void;
  adding: boolean;
  addError: string | null;
}) {
  const [form, setForm] = useState<AddFormState>({
    task: '',
    expr: '',
    provider: '',
    disabled: false,
  });

  function set<K extends keyof AddFormState>(k: K, v: AddFormState[K]) {
    setForm((f) => ({ ...f, [k]: v }));
  }

  return (
    <div
      style={{
        background: 'var(--kim-surface)',
        border: '1px solid var(--kim-border)',
        borderRadius: 12,
        padding: '14px 16px',
        marginBottom: 14,
      }}
    >
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 10 }}>
        <div>
          <div style={{ fontSize: 11.5, color: 'var(--kim-text-3)', marginBottom: 5 }}>Task</div>
          <input
            className="kr-input"
            placeholder="e.g. check disk usage"
            value={form.task}
            onChange={(e) => set('task', e.target.value)}
            disabled={adding}
            autoFocus
            aria-label="Task"
          />
        </div>
        <div>
          <div style={{ fontSize: 11.5, color: 'var(--kim-text-3)', marginBottom: 5 }}>Schedule</div>
          <input
            className="kr-input"
            placeholder="@hourly, @daily, @every 30m"
            value={form.expr}
            onChange={(e) => set('expr', e.target.value)}
            disabled={adding}
            aria-label="Schedule expression"
          />
        </div>
      </div>
      <div style={{ marginBottom: 10 }}>
        <div style={{ fontSize: 11.5, color: 'var(--kim-text-3)', marginBottom: 5 }}>Provider</div>
        <select
          className="kr-input"
          value={form.provider}
          onChange={(e) => set('provider', e.target.value)}
          disabled={adding}
          style={{ cursor: 'pointer' }}
          aria-label="Provider"
        >
          {PROVIDER_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <button
          type="button"
          className={`kr-toggle${form.disabled ? ' kr-on' : ''}`}
          onClick={() => set('disabled', !form.disabled)}
          aria-pressed={form.disabled}
        />
        <span style={{ fontSize: 13, color: 'var(--kim-text-2)' }}>Start disabled</span>
      </div>
      {addError && <div style={S.err}>{addError}</div>}
      <div style={{ display: 'flex', gap: 8 }}>
        <button
          type="button"
          className="kr-btn kr-btn-primary"
          onClick={() => onAdd(form)}
          disabled={adding || !form.task.trim() || !form.expr.trim()}
        >
          {adding ? 'Adding...' : 'Add task'}
        </button>
        <button type="button" className="kr-btn" onClick={onCancel} disabled={adding}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function RunDueResult({ result }: { result: RunDueResponse & { dryRun: boolean } }) {
  if (!result.ok) {
    return <div style={S.err}>Error: {result.error || 'Unknown error'}</div>;
  }
  if (!result.launched && !result.skipped) {
    return <div style={{ ...S.ok }}>{result.reason ?? 'No tasks due.'}</div>;
  }
  if (result.skipped) {
    return (
      <div style={{ ...S.ok, color: 'var(--kim-text-2)' }}>
        {result.dryRun ? 'Dry run -- would launch: ' : 'Skipped: '}
        {result.task || result.task_id || '--'}
        {result.skip_reason ? ` (${result.skip_reason})` : ''}
      </div>
    );
  }
  return (
    <div style={S.ok}>
      Launched: {result.task || result.task_id || '--'}
      {result.recorded ? ' -- run recorded' : ''}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main pane
// ---------------------------------------------------------------------------

export function PaneSchedule({
  settings,
  onChange,
}: {
  settings: Settings;
  onChange: (settings: Settings) => void;
}) {
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [runResult, setRunResult] = useState<(RunDueResponse & { dryRun: boolean }) | null>(null);
  const [timerStatus, setTimerStatus] = useState<ScheduleTimerStatus | null>(null);
  const [timerBusy, setTimerBusy] = useState(false);
  const [timerInterval, setTimerInterval] = useState(
    String(settings.schedule_timer.interval_seconds || 300),
  );

  useEffect(() => {
    setTimerInterval(String(settings.schedule_timer.interval_seconds || 300));
  }, [settings.schedule_timer.interval_seconds]);

  const mountedRef = useRef(true);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const json = await invoke<string>('list_scheduled_tasks', { enabledOnly: false });
      if (mountedRef.current) setTasks(parseTaskList(json));
    } catch (e) {
      if (mountedRef.current) setError(extractInvokeError(e));
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }

  // L16: was a verbatim duplicate of refresh() with a cancelled flag bolted
  // on; the mountedRef keeps one implementation for both paths.
  useEffect(() => {
    mountedRef.current = true;
    void refresh();
    return () => { mountedRef.current = false; };
  }, []);

  async function refreshTimerStatus() {
    try {
      const status = await invoke<ScheduleTimerStatus>('get_schedule_timer_status');
      setTimerStatus(parseTimerStatus(status));
    } catch (e) {
      setTimerStatus({
        running: false,
        interval_seconds: 0,
        tick_count: 0,
        last_error: extractInvokeError(e),
      });
    }
  }

  useEffect(() => {
    let cancelled = false;
    async function run() {
      try {
        const status = await invoke<ScheduleTimerStatus>('get_schedule_timer_status');
        if (!cancelled) setTimerStatus(parseTimerStatus(status));
      } catch (e) {
        if (!cancelled) setTimerStatus({
          running: false,
          interval_seconds: 0,
          tick_count: 0,
          last_error: extractInvokeError(e),
        });
      }
    }
    void run();
    return () => { cancelled = true; };
  }, []);

  async function handleAdd(form: AddFormState) {
    setAdding(true);
    setAddError(null);
    try {
      await invoke('add_scheduled_task', {
        task: form.task.trim(),
        expr: form.expr.trim(),
        provider: form.provider || null,
        disabled: form.disabled,
      });
      setShowAdd(false);
      await refresh();
    } catch (e) {
      setAddError(extractInvokeError(e));
    } finally {
      setAdding(false);
    }
  }

  async function handleToggle(id: string, enabled: boolean) {
    try {
      await invoke('update_scheduled_task', {
        id,
        task: null,
        expr: null,
        provider: null,
        enabled,
      });
      setTasks((ts) =>
        ts.map((t) => (t.id === id ? { ...t, enabled } : t)),
      );
    } catch (e) {
      setError(extractInvokeError(e));
    }
  }

  async function handleDelete(id: string) {
    try {
      await invoke('delete_scheduled_task', { id });
      setTasks((ts) => ts.filter((t) => t.id !== id));
    } catch (e) {
      setError(extractInvokeError(e));
    }
  }

  async function handleRunDue(dryRun: boolean) {
    setRunning(true);
    setRunResult(null);
    try {
      const json = await invoke<string>('run_due_scheduled_task', { dryRun });
      const result = parseRunDueResponse(json);
      setRunResult({ ...result, dryRun });
      if (result.launched) await refresh();
    } catch (e) {
      // kimctl exits 1 with JSON in stdout; run_kimctl surfaces that as Err(json_string).
      // Parse the raw rejection directly — extractInvokeError would unwrap the .error field
      // to a bare string that parseRunDueResponse can no longer re-parse as structured data.
      const raw = typeof e === 'string' ? e : String(e);
      let result: RunDueResponse;
      try {
        const direct = JSON.parse(raw);
        if (typeof direct === 'object' && direct !== null && !Array.isArray(direct)) {
          result = direct as RunDueResponse;
        } else {
          result = { ok: false, error: extractInvokeError(e) };
        }
      } catch {
        // Non-JSON rejection (e.g. Rust "Failed to start kimctl: …" or "Executor error: …")
        result = { ok: false, error: extractInvokeError(e) };
      }
      setRunResult({ ...result, ok: result.ok ?? false, dryRun });
    } finally {
      setRunning(false);
    }
  }

  async function handleStartTimer() {
    setTimerBusy(true);
    try {
      const parsed = Number.parseInt(timerInterval, 10);
      // L16: clamp to >=60s BEFORE starting the live timer — only the persisted
      // settings value was clamped, so the running timer could tick faster.
      const intervalSeconds = Math.max(60, Number.isFinite(parsed) ? parsed : 300);
      const status = await invoke<ScheduleTimerStatus>('start_schedule_timer', { intervalSeconds });
      const nextStatus = parseTimerStatus(status);
      setTimerStatus(nextStatus);
      onChange({
        ...settings,
        schedule_timer: {
          enabled: true,
          interval_seconds: nextStatus.interval_seconds || Math.max(intervalSeconds, 60),
        },
      });
    } catch (e) {
      setTimerStatus({
        running: false,
        interval_seconds: 0,
        tick_count: timerStatus?.tick_count ?? 0,
        last_error: extractInvokeError(e),
      });
    } finally {
      setTimerBusy(false);
    }
  }

  async function handleStopTimer() {
    setTimerBusy(true);
    try {
      const status = await invoke<ScheduleTimerStatus>('stop_schedule_timer');
      setTimerStatus(parseTimerStatus(status));
      onChange({
        ...settings,
        schedule_timer: {
          ...settings.schedule_timer,
          enabled: false,
        },
      });
    } catch (e) {
      setTimerStatus({
        running: timerStatus?.running ?? false,
        interval_seconds: timerStatus?.interval_seconds ?? 0,
        tick_count: timerStatus?.tick_count ?? 0,
        last_error: extractInvokeError(e),
      });
    } finally {
      setTimerBusy(false);
    }
  }

  return (
    <div>
      {/* Toolbar */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 16, alignItems: 'center' }}>
        <button
          type="button"
          className="kr-btn"
          onClick={() => void refresh()}
          disabled={loading}
          style={{ minWidth: 80 }}
        >
          {loading ? 'Loading...' : 'Refresh'}
        </button>
        {!showAdd && (
          <button
            type="button"
            className="kr-btn"
            onClick={() => { setShowAdd(true); setAddError(null); }}
          >
            + Add task
          </button>
        )}
      </div>

      {/* Global error */}
      {error && <div style={S.err}>{error}</div>}

      {/* Add form */}
      {showAdd && (
        <AddForm
          onAdd={(f) => void handleAdd(f)}
          onCancel={() => setShowAdd(false)}
          adding={adding}
          addError={addError}
        />
      )}

      {/* Task list */}
      {!loading && tasks.length === 0 && !error && (
        <div style={{ color: 'var(--kim-text-3)', fontSize: 13, marginBottom: 16 }}>
          No scheduled tasks yet.
        </div>
      )}
      {tasks.map((t) => (
        <TaskRow
          key={t.id}
          task={t}
          onToggle={(id, en) => void handleToggle(id, en)}
          onDelete={(id) => void handleDelete(id)}
        />
      ))}

      {/* Run-due section */}
      <div
        style={{
          marginTop: 20,
          paddingTop: 16,
          borderTop: '1px solid var(--kim-border)',
        }}
      >
        <div
          style={{
            fontSize: 11,
            color: 'var(--kim-text-3)',
            textTransform: 'uppercase',
            letterSpacing: '0.07em',
            marginBottom: 10,
          }}
        >
          run due
        </div>
        <div style={{ display: 'flex', gap: 8, marginBottom: runResult ? 10 : 0 }}>
          <button
            type="button"
            className="kr-btn"
            onClick={() => void handleRunDue(false)}
            disabled={running}
            title="Find the next due task and launch it"
          >
            {running ? 'Running...' : 'Run due'}
          </button>
          <button
            type="button"
            className="kr-btn"
            onClick={() => void handleRunDue(true)}
            disabled={running}
            title="Show what would run without spawning"
          >
            Dry run
          </button>
        </div>
        {runResult && <RunDueResult result={runResult} />}
      </div>

      {/* Timer section */}
      <div
        style={{
          marginTop: 20,
          paddingTop: 16,
          borderTop: '1px solid var(--kim-border)',
        }}
      >
        <div
          style={{
            fontSize: 11,
            color: 'var(--kim-text-3)',
            textTransform: 'uppercase',
            letterSpacing: '0.07em',
            marginBottom: 10,
          }}
        >
          timer
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 10 }}>
          <input
            className="kr-input"
            type="number"
            min={60}
            step={60}
            value={timerInterval}
            onChange={(e) => setTimerInterval(e.target.value)}
            style={{ width: 110 }}
            aria-label="Timer interval seconds"
          />
          <button
            type="button"
            className="kr-btn"
            onClick={() => void handleStartTimer()}
            disabled={timerBusy || timerStatus?.running === true}
          >
            Start
          </button>
          <button
            type="button"
            className="kr-btn"
            onClick={() => void handleStopTimer()}
            disabled={timerBusy || timerStatus?.running !== true}
          >
            Stop
          </button>
          <button
            type="button"
            className="kr-btn"
            onClick={() => void refreshTimerStatus()}
            disabled={timerBusy}
          >
            Status
          </button>
        </div>
        <div style={{ color: 'var(--kim-text-3)', fontSize: 11.5, marginTop: -4, marginBottom: 10 }}>
          Start persists this timer for future Kim launches. Stop disables that saved preference.
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: timerStatus?.last_error ? 10 : 0 }}>
          <span style={S.badge}>{timerStatus?.running ? 'running' : 'stopped'}</span>
          <span style={S.badge}>interval: {timerStatus?.interval_seconds || 0}s</span>
          <span style={S.badge}>ticks: {timerStatus?.tick_count || 0}</span>
        </div>
        {timerStatus?.last_error && <div style={S.err}>{timerStatus.last_error}</div>}
        {timerStatus?.last_result && !timerStatus.last_error && (
          <div style={S.ok}>Last tick: {formatTimerLastResult(timerStatus.last_result)}</div>
        )}
      </div>
    </div>
  );
}
