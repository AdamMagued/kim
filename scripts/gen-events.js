#!/usr/bin/env node
/**
 * Generates desktop/src/types/events.gen.ts from desktop/src/types/events.schema.json.
 * Run via: npm run gen:events  (from desktop/)  or  node scripts/gen-events.js (from repo root)
 *
 * CI drift check: node scripts/gen-events.js && git diff --exit-code desktop/src/types/events.gen.ts
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..');
const schemaPath = resolve(repoRoot, 'desktop/src/types/events.schema.json');
const outPath = resolve(repoRoot, 'desktop/src/types/events.gen.ts');

const schema = JSON.parse(readFileSync(schemaPath, 'utf8'));
const events = schema.events;

// ── helpers ────────────────────────────────────────────────────────────────

function toConstKey(event) {
  // "kim:run-done" → "RUN_DONE"
  return event.replace(/^kim:/, '').replace(/-/g, '_').toUpperCase();
}

function renderType(fieldType) {
  // The schema's "type" field is a verbatim TypeScript type string — pass it through.
  return fieldType;
}

function renderInterface(ev) {
  const lines = [`export interface ${ev.typeName}Payload {`];
  for (const [key, def] of Object.entries(ev.payload)) {
    if (def.description) lines.push(`  /** ${def.description} */`);
    lines.push(`  ${key}: ${renderType(def.type)};`);
  }
  lines.push('}');
  return lines.join('\n');
}

// ── generate ───────────────────────────────────────────────────────────────

const header = `\
// events.gen.ts — DO NOT HAND-EDIT
// Generated from desktop/src/types/events.schema.json via \`npm run gen:events\`.
// To add/change an event: edit events.schema.json, then re-run the script.
`;

const eventNames = events.map(ev =>
  `  ${toConstKey(ev.event)}: '${ev.event}' as const,`
).join('\n');

const constBlock = `\
/** All typed IPC event names emitted by the Kim agent. */
export const KimEventNames = {
${eventNames}
} as const;

export type KimEventName = (typeof KimEventNames)[keyof typeof KimEventNames];
`;

const interfaces = events.map(ev => {
  const comment = ev.description ? `/** ${ev.description} */\n` : '';
  return comment + renderInterface(ev);
}).join('\n\n');

const unionEntries = events.map(ev =>
  `  | { event: typeof KimEventNames.${toConstKey(ev.event)}; payload: ${ev.typeName}Payload }`
).join('\n');

const unionBlock = `\
/** Discriminated union of all typed IPC events. */
export type KimEvent =
${unionEntries};
`;

const out = [header, constBlock, interfaces, unionBlock].join('\n');
writeFileSync(outPath, out, 'utf8');
console.log(`✓ Generated ${outPath}`);
