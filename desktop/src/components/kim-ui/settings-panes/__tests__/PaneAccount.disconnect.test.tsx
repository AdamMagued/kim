import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import type { KimAccount } from '../../../../types';

// F-F-10: disconnecting GitHub must not report success if the keychain delete
// fails. Previously `invoke('delete_github_token').catch(() => {})` swallowed
// the rejection, so the UI toasted "GitHub disconnected" while the PAT stayed
// on disk — a security-relevant false confirmation.

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }));
vi.mock('@tauri-apps/plugin-dialog', () => ({ confirm: vi.fn(() => Promise.resolve(true)) }));
const toastMock = vi.fn();
vi.mock('../../../Toast', () => ({ toast: (...a: unknown[]) => toastMock(...a) }));

import { invoke } from '@tauri-apps/api/core';
import { PaneAccount } from '../PaneAccount';

const invokeMock = invoke as unknown as ReturnType<typeof vi.fn>;

function makeAccount(): KimAccount {
  return {
    display_name: 'Ada',
    github_token: 'ghp_secret',
    github_username: 'ada',
  } as KimAccount;
}

beforeEach(() => {
  toastMock.mockClear();
  invokeMock.mockReset();
});

describe('PaneAccount GitHub disconnect (F-F-10)', () => {
  it('surfaces an error toast (not a false "disconnected") when delete_github_token rejects', async () => {
    invokeMock.mockImplementation((name: string) => {
      if (name === 'delete_github_token') return Promise.reject(new Error('keychain locked'));
      if (name === 'google_oauth_status') return Promise.resolve({ connected: false });
      return Promise.resolve(undefined);
    });
    const onAccountChange = vi.fn(() => Promise.resolve());

    render(<PaneAccount account={makeAccount()} onAccountChange={onAccountChange} />);

    fireEvent.click(await screen.findByText('Disconnect'));

    await waitFor(() => {
      expect(invokeMock).toHaveBeenCalledWith('delete_github_token');
    });
    await waitFor(() => {
      // The failure must be reported…
      const called = toastMock.mock.calls.some(
        ([msg, kind]) => typeof msg === 'string' && /disconnect/i.test(msg) && kind === 'error',
      );
      expect(called).toBe(true);
    });
    // …and the misleading success toast must NOT fire.
    const falseSuccess = toastMock.mock.calls.some(
      ([msg, kind]) => msg === 'GitHub disconnected' && kind === 'success',
    );
    expect(falseSuccess).toBe(false);
  });

  it('reports success only when the keychain delete actually resolves', async () => {
    invokeMock.mockImplementation((name: string) => {
      if (name === 'google_oauth_status') return Promise.resolve({ connected: false });
      return Promise.resolve(undefined);
    });
    const onAccountChange = vi.fn(() => Promise.resolve());

    render(<PaneAccount account={makeAccount()} onAccountChange={onAccountChange} />);
    fireEvent.click(await screen.findByText('Disconnect'));

    await waitFor(() => {
      const ok = toastMock.mock.calls.some(
        ([msg, kind]) => msg === 'GitHub disconnected' && kind === 'success',
      );
      expect(ok).toBe(true);
    });
  });
});
