import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { ErrorBoundary } from '../ErrorBoundary';

// A component that always throws during render.
const ThrowingChild = (): React.ReactElement => {
  throw new Error('deliberate render error');
};

// Throws only while `shouldThrow` is true — lets a test flip a boundary from
// crashed back to healthy after calling `reset`.
function SometimesThrows({ shouldThrow }: { shouldThrow: boolean }): React.ReactElement {
  if (shouldThrow) throw new Error('deliberate render error');
  return <span>recovered</span>;
}

// reset() alone re-renders the SAME (already-thrown) children element and
// immediately re-crashes unless whatever made it throw also changes. This
// harness flips its own state in the same click handler as reset() so both
// updates land in one batch, mirroring how ChatView pairs `reset` with
// content that's actually different (e.g. the user picked a new session).
function RecoverableHarness(): React.ReactElement {
  const [shouldThrow, setShouldThrow] = React.useState(true);
  return (
    <ErrorBoundary
      fallback={(_message, reset) => (
        <button onClick={() => { setShouldThrow(false); reset(); }}>Try again</button>
      )}
    >
      <SometimesThrows shouldThrow={shouldThrow} />
    </ErrorBoundary>
  );
}

describe('ErrorBoundary.getDerivedStateFromError', () => {
  it('getDerivedStateFromError_error_instance', () => {
    const result = ErrorBoundary.getDerivedStateFromError(new Error('boom'));
    expect(result).toEqual({ hasError: true, message: 'boom' });
  });

  it('getDerivedStateFromError_non_error', () => {
    // undefined is not an Error instance; the boundary must still produce a
    // non-empty fallback message string.
    const result = ErrorBoundary.getDerivedStateFromError(undefined);
    expect(result.hasError).toBe(true);
    expect(typeof result.message).toBe('string');
    expect(result.message.length).toBeGreaterThan(0);
  });
});

describe('ErrorBoundary rendering', () => {
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    // Suppress React's own console.error output for expected boundary catches
    // so test output stays clean.
    consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    consoleErrorSpy.mockRestore();
  });

  it('renders_children_when_ok', () => {
    render(
      <ErrorBoundary>
        <span>hello world</span>
      </ErrorBoundary>
    );
    expect(screen.getByText('hello world')).toBeTruthy();
    expect(screen.queryByText('Something went wrong')).toBeNull();
  });

  it('renders_fallback_when_child_throws', () => {
    render(
      <ErrorBoundary>
        <ThrowingChild />
      </ErrorBoundary>
    );
    expect(screen.getByText('Something went wrong')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Reload' })).toBeTruthy();
  });
});

// V-audit #7: scoped fallback + auto-reset, used by ChatView to wrap the
// message-rendering subtree without falling back to the app-wide screen.
describe('ErrorBoundary scoped fallback (V-audit #7)', () => {
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    consoleErrorSpy.mockRestore();
  });

  it('uses the custom fallback render-prop instead of the default screen', () => {
    render(
      <ErrorBoundary fallback={(message, reset) => (
        <div>
          <span>scoped fallback: {message}</span>
          <button onClick={reset}>Try again</button>
        </div>
      )}>
        <ThrowingChild />
      </ErrorBoundary>
    );
    expect(screen.getByText(/scoped fallback: deliberate render error/)).toBeTruthy();
    expect(screen.queryByText('Something went wrong')).toBeNull();
  });

  it('reset() clears the crash so children render again once they stop throwing', () => {
    render(<RecoverableHarness />);
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(screen.getByText('recovered')).toBeTruthy();
  });

  it('changing resetKey while crashed auto-clears the fallback', () => {
    const { rerender } = render(
      <ErrorBoundary resetKey="session-a" fallback={() => <span>crashed</span>}>
        <ThrowingChild />
      </ErrorBoundary>
    );
    expect(screen.getByText('crashed')).toBeTruthy();

    // Simulate navigating to a different session (new resetKey) with content
    // that renders fine — the stale crash must not persist across it.
    rerender(
      <ErrorBoundary resetKey="session-b" fallback={() => <span>crashed</span>}>
        <span>session b content</span>
      </ErrorBoundary>
    );
    expect(screen.getByText('session b content')).toBeTruthy();
    expect(screen.queryByText('crashed')).toBeNull();
  });
});
