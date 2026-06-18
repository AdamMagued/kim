import { describe, it, expect } from 'vitest';
import { splitFences, isSafeLinkUrl, classifyImageSrc } from '../MessageBubble';

describe('splitFences (F2)', () => {
  it('keeps a blank line inside a fenced code block in ONE code segment', () => {
    const text = 'before\n\n```js\nconst a = 1;\n\nconst b = 2;\n```\n\nafter';
    const segs = splitFences(text);
    const code = segs.filter(s => s.type === 'code');
    expect(code).toHaveLength(1);
    // The blank line between the two statements survives intact.
    expect(code[0].content).toBe('const a = 1;\n\nconst b = 2;');
    // Surrounding prose stays as text segments.
    expect(segs.some(s => s.type === 'text' && s.content.includes('before'))).toBe(true);
    expect(segs.some(s => s.type === 'text' && s.content.includes('after'))).toBe(true);
  });

  it('handles a fence that is not at the start of a paragraph', () => {
    const segs = splitFences('text ```inline\ncode\n``` tail');
    expect(segs.filter(s => s.type === 'code')).toHaveLength(1);
  });

  it('treats plain text with no fences as a single text segment', () => {
    const segs = splitFences('just prose here');
    expect(segs).toEqual([{ type: 'text', content: 'just prose here' }]);
  });
});

describe('isSafeLinkUrl (F3)', () => {
  it('allows http/https/mailto', () => {
    expect(isSafeLinkUrl('https://example.com')).toBe(true);
    expect(isSafeLinkUrl('http://example.com')).toBe(true);
    expect(isSafeLinkUrl('mailto:a@b.com')).toBe(true);
  });
  it('rejects javascript: and other schemes', () => {
    expect(isSafeLinkUrl('javascript:alert(1)')).toBe(false);
    expect(isSafeLinkUrl('  JavaScript:alert(1)')).toBe(false);
    expect(isSafeLinkUrl('data:text/html,<script>')).toBe(false);
    expect(isSafeLinkUrl('file:///etc/passwd')).toBe(false);
  });
});

describe('classifyImageSrc (F3)', () => {
  it('inlines data:, asset:, and same-origin paths', () => {
    expect(classifyImageSrc('data:image/png;base64,AAAA')).toBe('inline');
    expect(classifyImageSrc('asset://localhost/x.png')).toBe('inline');
    expect(classifyImageSrc('/local/x.png')).toBe('inline');
  });
  it('treats remote https images as click-to-load', () => {
    expect(classifyImageSrc('https://tracker.example/pixel.png')).toBe('remote');
  });
  it('blocks other schemes (http, javascript)', () => {
    expect(classifyImageSrc('http://insecure/x.png')).toBe('blocked');
    expect(classifyImageSrc('javascript:alert(1)')).toBe('blocked');
  });
});
