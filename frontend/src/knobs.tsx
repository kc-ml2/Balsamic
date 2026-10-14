import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, errorText } from './api';
import type { Json } from './api';
import { TextContent } from './textContent';
import { Badge, ErrorNotice, Field, Icon, Modal } from './ui';
import './knobs.css';

export type Value = number | boolean | string;
export type ParameterKind = 'number' | 'integer' | 'boolean' | 'choice' | 'text';
export type Basis = 'given' | 'assumed' | 'to_measure';
/** A number or a choice of the problem: shown as a control, read by the notebook's code as P.name. */
export type Parameter = {
  id?: string; name: string; label: string; kind: ParameterKind; value: Value;
  minimum?: number | null; maximum?: number | null; step?: number | null; unit?: string | null; choices?: string[] | null;
  symbol?: string | null; description?: string | null; basis?: Basis; origin?: string | null; prominent?: boolean;
  revision?: number; value_author?: string; used_by?: string[];
};

const basisText: Record<Basis, string> = { given: 'given', assumed: 'assumed', to_measure: 'to be measured' };
const basisTone: Record<Basis, string> = { given: 'green', assumed: '', to_measure: 'amber' };
const numeric = (kind: ParameterKind) => kind === 'number' || kind === 'integer';
function writer(author?: string) { return author === 'lead' ? 'the lead agent' : (author || '').replaceAll('_', ' '); }

/** A JSON-schema property as a parameter, so that a problem's configuration can be shown with the same controls. */
export function controlFromSchema(name: string, property: Json, value: Value): Parameter {
  const kind: ParameterKind = property.enum ? 'choice' : property.type === 'boolean' ? 'boolean' : property.type === 'integer' ? 'integer'
    : property.type === 'number' ? 'number' : 'text';
  return { name, label: property.title || name, kind, value, minimum: property.minimum ?? null, maximum: property.maximum ?? null,
    choices: property.enum ? property.enum.map(String) : null, description: property.description ?? null };
}

/** One parameter's control. `onChange` follows the hand; `onCommit` is called once a value is meant: on release, Enter, or leaving the field. */
export function Control({ parameter, value, onChange, onCommit, disabled }: { parameter: Parameter; value: Value;
  onChange: (value: Value) => void; onCommit: (value: Value) => void; disabled?: boolean }) {
  const id = `parameter-${parameter.name}`;
  const label = `${parameter.label}${parameter.unit ? ` (${parameter.unit})` : ''}`;
  const [text, setText] = useState(String(value));
  useEffect(() => { setText(String(value)); }, [value]);
  if (parameter.kind === 'boolean') return <label className="knob-switch"><input id={id} type="checkbox" role="switch" aria-label={label} checked={value === true}
    disabled={disabled} onChange={event => { onChange(event.target.checked); onCommit(event.target.checked); }} /><span>{value === true ? 'Yes' : 'No'}</span></label>;
  if (parameter.kind === 'choice') return <select id={id} aria-label={label} value={String(value)} disabled={disabled}
    onChange={event => { onChange(event.target.value); onCommit(event.target.value); }}>
    {(parameter.choices || []).map(choice => <option key={choice} value={choice}>{choice}</option>)}</select>;
  if (parameter.kind === 'text') return <input id={id} aria-label={label} value={text} disabled={disabled} maxLength={2000}
    onChange={event => { setText(event.target.value); onChange(event.target.value); }} onBlur={event => onCommit(event.target.value)}
    onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); onCommit(event.currentTarget.value); } }} />;
  const low = parameter.minimum ?? undefined, high = parameter.maximum ?? undefined, integer = parameter.kind === 'integer';
  const parse = (entered: string) => { const number = Number(entered); return entered.trim() === '' || !Number.isFinite(number) ? null : number; };
  const typed = (entered: string) => { const number = parse(entered); if (number !== null) onCommit(number); else setText(String(value)); };
  // Without a declared step, a slider moves in round steps of about a hundredth of its range, from a round start,
  // so that it shows 3,000,000 rather than 3,000,001; a value it reaches outside the range is brought back in.
  const bounded = low !== undefined && high !== undefined && high > low;
  const step = parameter.step ?? (bounded ? Math.max(integer ? 1 : 0, 10 ** Math.floor(Math.log10((high! - low!) / 100))) : undefined);
  const start = bounded && parameter.step == null && step ? Math.floor(low! / step) * step : low;
  const within = (number: number) => Math.min(high ?? number, Math.max(low ?? number, number));
  return <div className="knob-number">
    {bounded && <input type="range" aria-label={`${label}, slider`} min={start} max={high} disabled={disabled}
      step={step || 'any'} value={Number(value)}
      onChange={event => onChange(within(Number(event.target.value)))}
      onPointerUp={event => onCommit(within(Number(event.currentTarget.value)))}
      onKeyUp={event => { if (event.key.startsWith('Arrow') || ['Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) onCommit(within(Number(event.currentTarget.value))); }} />}
    <input id={id} type="number" aria-label={label} min={low} max={high} step={parameter.step ?? (integer ? 1 : 'any')} value={text} disabled={disabled}
      onChange={event => { setText(event.target.value); const number = parse(event.target.value); if (number !== null) onChange(number); }}
      onBlur={event => typed(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); typed(event.currentTarget.value); } }} />
  </div>;
}

type Form = { name: string; label: string; kind: ParameterKind; value: string; minimum: string; maximum: string; step: string; unit: string;
  choices: string; symbol: string; description: string; basis: Basis; prominent: boolean };
const text = (value: unknown) => value === null || value === undefined ? '' : String(value);

/** Add a parameter, or change one: its description, and its value. */
function ParameterForm({ parameter, onClose, onSave }: { parameter: Parameter | null; onClose: () => void; onSave: (operations: Json[]) => Promise<boolean> }) {
  const [form, setForm] = useState<Form>(() => ({ name: text(parameter?.name), label: text(parameter?.label), kind: parameter?.kind || 'number',
    value: text(parameter?.value ?? ''), minimum: text(parameter?.minimum), maximum: text(parameter?.maximum), step: text(parameter?.step),
    unit: text(parameter?.unit), choices: (parameter?.choices || []).join(', '), symbol: text(parameter?.symbol), description: text(parameter?.description),
    basis: parameter?.basis || 'given', prominent: !!parameter?.prominent }));
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const set = (changes: Partial<Form>) => setForm(current => ({ ...current, ...changes }));
  function value(): Value {
    if (form.kind === 'boolean') return form.value === 'true';
    if (numeric(form.kind)) {
      const number = Number(form.value);
      if (form.value.trim() === '' || !Number.isFinite(number)) throw new Error('Give the value as a number.');
      return number;
    }
    return form.value;
  }
  function bound(entered: string, label: string): number | null {
    if (!entered.trim()) return null;
    const number = Number(entered);
    if (!Number.isFinite(number)) throw new Error(`Give the ${label} as a number.`);
    return number;
  }
  async function save(event: FormEvent) {
    event.preventDefault(); setError('');
    try {
      const descriptor: Json = { name: form.name, label: form.label, kind: form.kind, basis: form.basis, prominent: form.prominent,
        minimum: numeric(form.kind) ? bound(form.minimum, 'minimum') : null, maximum: numeric(form.kind) ? bound(form.maximum, 'maximum') : null,
        step: numeric(form.kind) ? bound(form.step, 'step') : null, unit: form.unit.trim() || null,
        choices: form.kind === 'choice' ? form.choices.split(',').map(choice => choice.trim()).filter(Boolean) : null,
        symbol: form.symbol.trim() || null, description: form.description.trim() || null };
      let operations: Json[];
      if (!parameter) {
        operations = [{ op: 'parameter.declare', ...Object.fromEntries(Object.entries(descriptor).filter(([, entry]) => entry !== null)), value: value() }];
      } else {
        // Only what changed is sent; a field emptied is sent as null, which removes it.
        const changes = Object.fromEntries(Object.entries(descriptor).filter(([key, entry]) => (entry ?? null) !== ((parameter as Json)[key] ?? null)
          && !(key === 'prominent' && !entry && !parameter.prominent)));
        operations = [];
        if (Object.keys(changes).length) operations.push({ op: 'parameter.update', parameter: parameter.name, expected_revision: parameter.revision, ...changes });
        if (value() !== parameter.value) operations.push({ op: 'parameter.set', parameter: parameter.name, expected_revision: parameter.revision, value: value() });
        if (!operations.length) { onClose(); return; }
      }
      setBusy(true);
      if (await onSave(operations)) onClose();
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  return <Modal title={parameter ? `Parameter ${parameter.name}` : 'Add a parameter'} description="A number or a choice of the problem. Code reads it as P.name; on the page it is a control." onClose={onClose}>
    <form onSubmit={save} aria-label={parameter ? `Edit parameter ${parameter.name}` : 'New parameter'}><div className="form-grid">
      <Field label="Name in code" hint="Letters, digits and _; P.name in code."><input value={form.name} onChange={e => set({ name: e.target.value })} required pattern="[A-Za-z][A-Za-z0-9_]{0,47}" /></Field>
      <Field label="Label" hint="What it is, in words."><input value={form.label} onChange={e => set({ label: e.target.value })} required maxLength={120} /></Field>
      <Field label="Kind"><select value={form.kind} onChange={e => set({ kind: e.target.value as ParameterKind, value: e.target.value === 'boolean' ? 'false' : form.value })}>
        <option value="number">Number</option><option value="integer">Whole number</option><option value="boolean">Yes or no</option><option value="choice">Choice</option><option value="text">Text</option></select></Field>
      <Field label="Value">{form.kind === 'boolean' ? <select value={form.value} onChange={e => set({ value: e.target.value })}><option value="true">Yes</option><option value="false">No</option></select>
        : <input value={form.value} onChange={e => set({ value: e.target.value })} required inputMode={numeric(form.kind) ? 'decimal' : undefined} />}</Field>
      {numeric(form.kind) && <>
        <Field label="Minimum" hint="With a maximum, the control is a slider."><input value={form.minimum} onChange={e => set({ minimum: e.target.value })} inputMode="decimal" /></Field>
        <Field label="Maximum"><input value={form.maximum} onChange={e => set({ maximum: e.target.value })} inputMode="decimal" /></Field>
        <Field label="Step"><input value={form.step} onChange={e => set({ step: e.target.value })} inputMode="decimal" /></Field></>}
      {form.kind === 'choice' && <Field label="Choices" hint="Separated by commas." wide><input value={form.choices} onChange={e => set({ choices: e.target.value })} required /></Field>}
      <Field label="Unit"><input value={form.unit} onChange={e => set({ unit: e.target.value })} maxLength={40} /></Field>
      <Field label="Symbol" hint="As in the equations, for example c_{max}."><input value={form.symbol} onChange={e => set({ symbol: e.target.value })} maxLength={60} /></Field>
      <Field label="Source"><select value={form.basis} onChange={e => set({ basis: e.target.value as Basis })}><option value="given">Given</option><option value="assumed">Assumed</option><option value="to_measure">To be measured</option></select></Field>
      <Field label="Meaning" wide><textarea value={form.description} onChange={e => set({ description: e.target.value })} rows={2} maxLength={2000} /></Field>
      <label className="check-label wide"><input type="checkbox" checked={form.prominent} onChange={e => set({ prominent: e.target.checked })} />Show it first: it matters most</label>
    </div>
    <ErrorNotice text={error} />
    <div className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>Cancel</button><button className="button primary" disabled={busy}>{parameter ? 'Save' : 'Add'}</button></div></form>
  </Modal>;
}

/** The notebook's parameters as controls. A value is saved and the notebook runs again a moment after it is set; the outputs come back in place. */
export function ParametersPanel({ parameters, path, busy, onView, edit }: { parameters: Parameter[]; path: string; busy: boolean;
  onView: (view: Json) => void; edit: (operations: Json[], reason?: string) => Promise<boolean> }) {
  const [local, setLocal] = useState<Record<string, Value>>({});
  const [status, setStatus] = useState<{ text: string; error?: boolean } | null>(null);
  const [editing, setEditing] = useState<Parameter | 'new' | null>(null);
  // The folded parameters open when the researcher adds or edits one, so that it stays in view.
  const [unfolded, setUnfolded] = useState(false);
  const pending = useRef<Record<string, Value>>({}), inflight = useRef(false), timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  async function send(values: Record<string, Value>, run: boolean) {
    inflight.current = true; setStatus({ text: run ? 'Saving and running…' : 'Saving…' });
    try {
      const answer = await api<Json>(`${path}/values`, { values, run, wait: 3 }, 'PUT');
      onView(answer.notebook);
      const started = answer.run;
      setStatus({ text: answer.queued ? 'Saved. The notebook runs again when the current run ends.' : !started ? 'Saved.'
        : started.status === 'running' ? 'Saved. Running…' : `Saved and ran${typeof started.elapsed_seconds === 'number' ? ` in ${started.elapsed_seconds.toFixed(1)} s` : ''}.` });
    } catch (e) { setStatus({ text: errorText(e), error: true }); }
    finally {
      inflight.current = false;
      setLocal(current => Object.fromEntries(Object.entries(current).filter(([name, value]) => !(name in values) || values[name] !== value)));
      if (Object.keys(pending.current).length) void flush();
    }
  }
  function flush() {
    if (inflight.current) return;
    const values = pending.current;
    pending.current = {};
    if (Object.keys(values).length) void send(values, true);
  }
  function commit(parameter: Parameter, value: Value) {
    if (value === parameter.value && !(parameter.name in pending.current)) { setLocal(({ [parameter.name]: _, ...rest }) => rest); return; }
    pending.current[parameter.name] = value;
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(flush, 250);
  }
  async function remove(parameter: Parameter) {
    await edit([{ op: 'parameter.delete', parameter: parameter.name, expected_revision: parameter.revision }], 'Removed a parameter');
  }
  const counts = { given: 0, assumed: 0, to_measure: 0 };
  for (const parameter of parameters) counts[parameter.basis || 'assumed'] += 1;
  const unused = parameters.filter(parameter => !parameter.used_by?.length).length;
  const prominent = parameters.filter(parameter => parameter.prominent);
  const first = prominent.length ? prominent : parameters.slice(0, 6), rest = parameters.filter(parameter => !first.includes(parameter));
  const row = (parameter: Parameter) => <div className="knob" key={parameter.id || parameter.name}>
    <div className="knob-label"><label htmlFor={`parameter-${parameter.name}`}>{parameter.label}</label>
      {parameter.symbol && <span className="knob-symbol"><TextContent text={`$${parameter.symbol}$`} math /></span>}<code>{parameter.name}</code></div>
    <Control parameter={parameter} value={parameter.name in local ? local[parameter.name] : parameter.value} disabled={busy}
      onChange={value => setLocal(current => ({ ...current, [parameter.name]: value }))} onCommit={value => commit(parameter, value)} />
    <div className="knob-meta">{parameter.unit && <span className="knob-unit">{parameter.unit}</span>}
      <Badge tone={basisTone[parameter.basis || 'assumed']}>{basisText[parameter.basis || 'assumed']}</Badge>
      {parameter.value_author && parameter.value_author !== 'researcher' && <><Badge tone="amber">set by {writer(parameter.value_author)}</Badge>
        <button type="button" className="text-button" title="Keep this value as yours" onClick={() => void send({ [parameter.name]: parameter.value }, false)}>
          <Icon name="check" size={12} />Confirm</button></>}
      {!parameter.used_by?.length && <span className="knob-unused">not used by any cell yet</span>}
      <button type="button" className="text-button" aria-label={`Edit parameter ${parameter.name}`} onClick={() => setEditing(parameter)}>Edit</button>
      <button type="button" className="text-button danger" aria-label={`Remove parameter ${parameter.name}`} disabled={busy} onClick={() => void remove(parameter)}>Remove</button></div>
    {parameter.description && <p className="knob-description">{parameter.description}</p>}
  </div>;
  return <section className="knobs" aria-label="Parameters">
    <div className="knobs-heading"><div><h2>Parameters</h2>
      <p>{parameters.length ? [counts.given && `${counts.given} given`, counts.assumed && `${counts.assumed} assumed`, counts.to_measure && `${counts.to_measure} to be measured`,
        unused && `${unused} not used yet`].filter(Boolean).join(' · ') : 'Every number the model takes becomes a control here. The lead agent declares them; you can add one too.'}</p></div>
      <div className="inline-actions">{status && <span className={`knobs-status ${status.error ? 'error' : ''}`} role="status">{status.text}</span>}
        <button type="button" className="button small secondary" onClick={() => setEditing('new')}><Icon name="plus" size={13} />Add a parameter</button></div></div>
    {first.length > 0 && <div className="knob-list">{first.map(row)}</div>}
    {rest.length > 0 && <details className="knob-more" open={unfolded} onToggle={event => setUnfolded(event.currentTarget.open)}><summary>{rest.length} more {rest.length === 1 ? 'parameter' : 'parameters'}</summary><div className="knob-list">{rest.map(row)}</div></details>}
    {editing && <ParameterForm parameter={editing === 'new' ? null : editing} onClose={() => setEditing(null)}
      onSave={async operations => { const saved = await edit(operations, editing === 'new' ? 'Added a parameter' : `Changed parameter ${editing.name}`); if (saved) setUnfolded(true); return saved; }} />}
  </section>;
}
