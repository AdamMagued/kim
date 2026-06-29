import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import React from 'react';
import { render, screen } from '@testing-library/react';
import { ErrorBoundary } from '../ErrorBoundary';

// A component that always throws during render.
const ThrowingChild = (): React.ReactElement => {
  throw new Error('deliberate render error');
};

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
