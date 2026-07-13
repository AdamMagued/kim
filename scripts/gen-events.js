#!/usr/bin/env node
/** Generate TypeScript, Rust, and Python IPC bindings from events.schema.json. */

import { readFileSync, writeFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(scriptDir, '..');
const schemaPath = resolve(repoRoot, 'desktop/src/types/events.schema.json');
const tsOutPath = resolve(repoRoot, 'desktop/src/types/events.gen.ts');
const rustOutPath = resolve(repoRoot, 'desktop/src-tauri/src/events.gen.rs');
const pythonOutPath = resolve(repoRoot, 'orchestrator/events_gen.py');

const schema = JSON.parse(readFileSync(schemaPath, 'utf8'));
const events = schema.events;
const legacyTags = schema.legacyTags ?? [];

function toConstKey(event) {
  return event.replace(/^kim:/, '').replace(/-/g, '_').toUpperCase();
}

/** '[STATUS]' -> 'STATUS', 'TASK_COMPLETE:' -> 'TASK_COMPLETE' */
function tagConstKey(tag) {
  return tag.replace(/[\[\]:]/g, '');
}

function wireType(event) {
  return event.wireType ?? event.event.replace(/^kim:/, '').replace(/-/g, '_');
}

function renderInterface(event) {
  // Run-identity envelope: every run-scoped event carries the owning run_id +
  // session_id (stamped by the Python emitter from KIM_RUN_ID / KIM_SESSION_ID
  // and injected through Rust). Both optional so legacy/bridge streams that omit
  // them still decode. Consumers route/file by these instead of "current view".
  const lines = [`export interface ${event.typeName}Payload extends KimRunEnvelope {`];
  for (const [key, def] of Object.entries(event.payload)) {
    if (def.description) lines.push(`  /** ${def.description} */`);
    lines.push(`  ${key}${def.optional ? '?' : ''}: ${def.type};`);
  }
  lines.push('}');
  return lines.join('\n');
}

const tsHeader = `// events.gen.ts -- DO NOT HAND-EDIT
// Generated from desktop/src/types/events.schema.json via \`npm run gen:events\`.
// To add or change an event, edit the schema and rerun the generator.
`;
const tsNames = events.map(event =>
  `  ${toConstKey(event.event)}: '${event.event}' as const,`
).join('\n');
const tsInterfaces = events.map(event => {
  const comment = event.description ? `/** ${event.description} */\n` : '';
  return comment + renderInterface(event);
}).join('\n\n');
const tsUnion = events.map(event =>
  `  | { event: typeof KimEventNames.${toConstKey(event.event)}; payload: ${event.typeName}Payload }`
).join('\n');
const tsWireEntries = events.flatMap(event => {
  if (event.wireVariants) {
    return event.wireVariants.map(variant =>
      `  ${JSON.stringify(variant.type)}: { event: KimEventNames.${toConstKey(event.event)}, fixedPayload: ${JSON.stringify(variant.payload)} },`
    );
  }
  return [`  ${JSON.stringify(wireType(event))}: { event: KimEventNames.${toConstKey(event.event)} },`];
}).join('\n');
const tsOut = `${tsHeader}
/** All typed IPC event names emitted by the Kim agent. */
export const KimEventNames = {
${tsNames}
} as const;

export type KimEventName = (typeof KimEventNames)[keyof typeof KimEventNames];

/**
 * Run-identity envelope shared by every typed payload. \`run_id\` is minted at
 * task spawn (Rust \`send_task\`, forwarded to Python as KIM_RUN_ID); \`session_id\`
 * is the session the run belongs to (KIM_SESSION_ID). Both are optional so
 * events from legacy/bridge streams that predate the envelope still type-check.
 * The frontend routes and files run output by these fields, never by which
 * view happens to be mounted.
 */
export interface KimRunEnvelope {
  run_id?: string;
  session_id?: string;
}

/** Legacy markers retained for the uncontrolled Codex compatibility stream. */
export const LegacyLogTags = ${JSON.stringify(Object.fromEntries(legacyTags.map(item => [item.tag, item])), null, 2)} as const;

/**
 * K5: named bracket-tag constants — the single source of truth for the
 * text-protocol vocabulary. Hand parsers (chat/utils.ts, chat/parsers.ts)
 * must reference these instead of re-typing the literals.
 */
export const LogTags = {
${legacyTags.map(item => `  ${tagConstKey(item.tag)}: ${JSON.stringify(item.tag)},`).join('\n')}
} as const;

${tsInterfaces}

/** Discriminated union of all typed IPC events. */
export type KimEvent =
${tsUnion};

const KimWireEventMap = {
${tsWireEntries}
} as const;

/** Decode one Python stdout JSON event using the schema-generated wire map. */
export function decodeKimEventLine(raw: string): KimEvent | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    const record = parsed as Record<string, unknown>;
    if (typeof record.type !== 'string' || !(record.type in KimWireEventMap)) return null;
    const mapping = KimWireEventMap[record.type as keyof typeof KimWireEventMap];
    const { type: _type, ...payload } = record;
    const fixedPayload = 'fixedPayload' in mapping ? mapping.fixedPayload : {};
    return { event: mapping.event, payload: { ...payload, ...fixedPayload } } as KimEvent;
  } catch {
    return null;
  }
}
`;

function snakeToPascal(value) {
  return value.split('_').map(part => part.charAt(0).toUpperCase() + part.slice(1)).join('');
}

function rustType(def) {
  if (def.rustType) return def.rustType;
  if (def.type === 'string' || def.type.startsWith("'")) return 'String';
  if (def.type === 'boolean') return 'bool';
  if (def.type === 'number') return 'f64';
  if (def.type === 'unknown[]') return 'Vec<serde_json::Value>';
  return 'serde_json::Value';
}

function rustVariant(name, payload, fixed = false) {
  const variant = snakeToPascal(name);
  if (fixed || Object.keys(payload).length === 0) return `    ${variant},`;
  const fields = Object.entries(payload).map(([key, def]) => {
    // `optional` implies the wire message may legitimately omit the field, so
    // the Rust decoder needs #[serde(default)] just like an explicit default.
    const attrs = (def.default || def.optional) ? '        #[serde(default)]\n' : '';
    return `${attrs}        ${key}: ${rustType(def)},`;
  }).join('\n');
  return `    ${variant} {\n${fields}\n    },`;
}

const rustVariants = events.flatMap(event => {
  if (event.wireVariants) {
    return event.wireVariants.map(variant => rustVariant(variant.type, {}, true));
  }
  return [rustVariant(wireType(event), event.payload)];
}).join('\n');
const rustOut = `// events.gen.rs -- DO NOT HAND-EDIT
// Generated from desktop/src/types/events.schema.json via npm run gen:events.

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(crate) enum KimEvent {
${rustVariants}
}
`;

function pyName(event) {
  return event.replace(/^kim:/, '').replace(/-/g, '_');
}

function pyParam([key, def]) {
  const annotation = def.type === 'string' || def.type.startsWith("'") ? 'str'
    : def.type === 'boolean' ? 'bool'
    : def.type === 'number' ? 'float'
    : def.type === 'unknown[]' ? 'list[object]'
    : 'dict[str, object]';
  // Type-appropriate defaults for optional fields (an optional bool/number
  // must not silently receive a str default).
  const pyDefault = annotation === 'str' ? " = ''"
    : annotation === 'bool' ? ' = False'
    : annotation === 'float' ? ' = 0.0'
    : ' = None';
  const optAnnotation = def.optional && pyDefault === ' = None'
    ? `${annotation} | None` : annotation;
  return `${key}: ${optAnnotation}${def.optional ? pyDefault : ''}`;
}

function renderPyEmitter(event) {
  const name = pyName(event.event);
  const params = Object.entries(event.payload).map(pyParam).join(', ');
  if (event.wireVariants) {
    // Derive the discriminator variable from the schema instead of hardcoding
    // `action` — a second wireVariants event keyed differently would otherwise
    // generate Python referencing an undefined name.
    const discriminator = Object.keys(event.payload)[0];
    const choices = event.wireVariants.map(variant => {
      const value = Object.values(variant.payload)[0];
      return `        ${JSON.stringify(value)}: ${JSON.stringify(variant.type)},`;
    }).join('\n');
    return `def emit_${name}(${params}) -> None:\n    wire_type = {\n${choices}\n    }[${discriminator}]\n    emit_event(wire_type)`;
  }
  const fields = Object.keys(event.payload).map(key => `${key}=${key}`).join(', ');
  return `def emit_${name}(${params}) -> None:\n    emit_event(${JSON.stringify(wireType(event))}${fields ? `, ${fields}` : ''})`;
}

const pyConstants = events.map(event => `${toConstKey(event.event)} = ${JSON.stringify(event.event)}`).join('\n');
const pyTagConstants = legacyTags.map(item => `LOG_TAG_${tagConstKey(item.tag)} = ${JSON.stringify(item.tag)}`).join('\n');
const pyLegacy = legacyTags.map(item => `    ${JSON.stringify(item.tag)}: ${JSON.stringify(item)},`).join('\n');
const pyEmitters = events.map(renderPyEmitter).join('\n\n\n');
const pythonOut = `# flake8: noqa
# events_gen.py -- DO NOT HAND-EDIT
# Generated from desktop/src/types/events.schema.json via npm run gen:events.

from __future__ import annotations

import json
import os
import sys
from typing import Any

${pyConstants}

# K5: named bracket-tag constants — the single source of truth for the
# text-protocol vocabulary. Emitters (codex_engine, codex_bridge_service)
# must reference these instead of re-typing the literals.
${pyTagConstants}

LEGACY_LOG_TAGS = {
${pyLegacy}
}


def emit_event(event_type: str, **payload: Any) -> None:
    # Stamp the run-identity envelope onto every event so the desktop frontend
    # can route/file output by the run it belongs to instead of by whatever view
    # is currently mounted. KIM_RUN_ID / KIM_SESSION_ID are exported by the Rust
    # spawner (send_task). When unset (CLI, tests, legacy spawns) no envelope is
    # added and the wire shape is byte-for-byte identical to before.
    envelope = {"type": event_type, **payload}
    _run_id = os.environ.get("KIM_RUN_ID")
    if _run_id:
        envelope.setdefault("run_id", _run_id)
    _session_id = os.environ.get("KIM_SESSION_ID")
    if _session_id:
        envelope.setdefault("session_id", _session_id)
    line = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
    data = (line + "\\n").encode("utf-8", errors="replace")

    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        try:
            buffer.write(data)
            buffer.flush()
            return
        except OSError:
            pass

    try:
        os.write(sys.stdout.fileno(), data)
        return
    except (AttributeError, OSError, ValueError):
        pass

    try:
        sys.stdout.write(line + "\\n")
        sys.stdout.flush()
        return
    except Exception:
        pass

    fallback = getattr(sys.__stdout__, "buffer", None)
    if fallback is not None:
        fallback.write(data)
        fallback.flush()


${pyEmitters}
`;

writeFileSync(tsOutPath, tsOut, 'utf8');
writeFileSync(rustOutPath, rustOut, 'utf8');
writeFileSync(pythonOutPath, pythonOut, 'utf8');
console.log(`Generated ${tsOutPath}`);
console.log(`Generated ${rustOutPath}`);
console.log(`Generated ${pythonOutPath}`);
