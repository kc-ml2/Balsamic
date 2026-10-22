import { useEffect, useRef, useState } from 'react';

/** The digit of Ctrl+1 … Ctrl+9 (Cmd on a Mac), or null for any other key. */
export function shortcutDigit(e: { key: string; code: string; ctrlKey: boolean; metaKey: boolean; altKey: boolean; shiftKey: boolean }) {
  if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return null;
  const digit = /^[1-9]$/.test(e.key) ? e.key : /^(?:Digit|Numpad)([1-9])$/.exec(e.code)?.[1];
  return digit ? Number(digit) : null;
}

export function Composer({ onSend, disabled, why, placeholder, initial, testId = 'composer', suggestions = [], suggestionsNote, autoFocus }: { onSend: (text: string) => Promise<void>; disabled?: boolean; why?: string; placeholder?: string; initial?: string; testId?: string; suggestions?: { label: string; text: string }[]; suggestionsNote?: string; autoFocus?: boolean }) {
  const [text, setText] = useState(initial || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const form = useRef<HTMLFormElement>(null);
  const area = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (autoFocus && !disabled) area.current?.focus(); }, [autoFocus, disabled]);
  const shown = suggestions.length && !text ? suggestions : [];
  const pick = (s: { text: string }) => { setText(s.text); area.current?.focus(); };
  const submit = async () => {
    const value = text.trim();
    if (!value || busy || disabled) return;
    // Sending from the composer leaves the cursor in it, ready for the next message.
    const focused = !!form.current?.contains(document.activeElement);
    setBusy(true); setError(null);
    try { await onSend(value); setText(''); }
    catch (e) { setError(String((e as Error).message || e)); }
    finally { setBusy(false); if (focused) requestAnimationFrame(() => area.current?.focus()); }
  };
  return (
    <form ref={form} className="composer" onSubmit={e => { e.preventDefault(); void submit(); }}
      onKeyDown={e => { const n = shortcutDigit(e); if (n && shown[n - 1] && !disabled && !busy) { e.preventDefault(); pick(shown[n - 1]); } }}>
      {shown.length ? <div className="row wrap" data-testid="suggestions" data-live={suggestionsNote ? '1' : undefined}>{shown.map((s, i) => <button key={s.text} type="button" className="btn small" disabled={disabled || busy} onClick={() => pick(s)}
        title={s.text + (i < 9 ? `  (Ctrl+${i + 1})` : '')} data-shortcut={i < 9 ? i + 1 : undefined} aria-keyshortcuts={i < 9 ? `Control+${i + 1}` : undefined}>{s.label}</button>)}{suggestionsNote ? <span className="faint small">{suggestionsNote}</span> : null}</div> : null}
      {/* Read-only rather than disabled while sending: a disabled textarea drops the focus. */}
      <textarea ref={area} value={text} onChange={e => setText(e.target.value)} placeholder={placeholder || 'Ask the lead, or type / for commands'} disabled={disabled} readOnly={busy} aria-busy={busy || undefined}
        data-testid={testId} onKeyDown={e => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); void submit(); } }} />
      <div className="row">
        <span className="why grow">{disabled && why ? why : error ? <span className="pill bad">{error}</span> : shown.length ? `Ctrl+Enter sends · Ctrl+1${shown.length > 1 ? '–' + Math.min(shown.length, 9) : ''} picks a suggestion` : 'Ctrl+Enter sends'}</span>
        <button className="btn primary" type="submit" disabled={disabled || busy || !text.trim()} data-testid={testId === 'composer' ? 'send' : testId + '-send'}>{busy ? 'Sending…' : 'Send'}</button>
      </div>
    </form>
  );
}
