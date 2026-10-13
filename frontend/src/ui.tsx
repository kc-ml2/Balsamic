import { Children, cloneElement, isValidElement, useEffect, useId, useRef } from 'react';
import type { ReactElement, ReactNode } from 'react';

export function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    overview: <><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></>,
    problem: <><path d="M4 7h16M4 17h16" /><circle cx="9" cy="7" r="3" fill="currentColor" /><circle cx="16" cy="17" r="3" fill="currentColor" /></>,
    hypothesis: <><path d="M9 18h6m-5 3h4M8 14a7 7 0 1 1 8 0c-1 1-1 2-1 2H9s0-1-1-2" /><path d="m10 8 2 3 2-3" /></>,
    experiments: <><path d="M9 3h6m-5 0v6L4 19a1.5 1.5 0 0 0 1 2h14a1.5 1.5 0 0 0 1-2L14 9V3M7 15h10" /></>,
    comparison: <><path d="M4 3v17h17M8 15l4-5 4 2 5-7" /></>,
    decisions: <><path d="M9 12l2 2 4-4" /><rect x="4" y="4" width="16" height="17" rx="3" /><path d="M9 4V2h6v2" /></>,
    sigma: <path d="M18 6V4H6l7 8-7 8h12v-2" />,
    notebook: <><path d="M5 3h14a1 1 0 0 1 1 1v17H6a3 3 0 0 1-3-3V5a2 2 0 0 1 2-2Zm-2 15a3 3 0 0 1 3-3h14M8 7h8m-8 4h5" /></>,
    plus: <path d="M12 5v14M5 12h14" />,
    arrow: <path d="M5 12h14m-6-6 6 6-6 6" />,
    send: <><path d="m21 3-7 18-4-7-7-4L21 3Zm0 0L10 14" /></>,
    close: <path d="m6 6 12 12M6 18 18 6" />,
    chevron: <path d="m9 5 7 7-7 7" />,
    download: <><path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" /></>,
    spark: <><path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4L12 3ZM21 2v4m-2-2h4" /></>,
    play: <path d="m7 4 14 8-14 8V4Z" />,
    pause: <><path d="M8 4v16M16 4v16" strokeWidth="4" /></>,
    stop: <rect x="5" y="5" width="14" height="14" rx="2" />,
    check: <path d="m5 12 4 4L19 6" />,
    clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
    chat: <path d="M21 11a8 8 0 0 1-8 8H6l-4 3V11a9 9 0 0 1 19 0Z" />,
    external: <><path d="M14 3h7v7m0-7L10 14M10 3H4a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1v-6" /></>,
    refresh: <><path d="M20 7a9 9 0 0 0-16 2m0-5v5h5M4 17a9 9 0 0 0 16-2m0 5v-5h-5" /></>,
    warning: <><path d="m12 3 10 18H2L12 3Z" /><path d="M12 9v5m0 3v.1" /></>,
    menu: <path d="M4 6h16M4 12h16M4 18h16" />,
    branch: <><circle cx="6" cy="5" r="2" /><circle cx="18" cy="5" r="2" /><circle cx="6" cy="19" r="2" /><path d="M6 7v10m12-10c0 6-12 1-12 8" /></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.spark}</svg>;
}

export function Badge({ children, tone = '' }: { children: ReactNode; tone?: string }) { return <span className={`badge ${tone}`}>{children}</span>; }
export function Status({ status }: { status: string }) { const tone = ['running', 'completed', 'validated', 'resolved', 'active'].includes(status) ? 'green' : ['failed', 'error', 'stopped'].includes(status) ? 'red' : ['paused', 'pending', 'queued', 'proposed', 'pausing'].includes(status) ? 'amber' : ''; return <Badge tone={tone}><i className={status === 'running' ? 'pulse' : ''} />{status?.replaceAll('_', ' ') || 'unknown'}</Badge>; }
export function Empty({ icon = 'experiments', title, children, action }: { icon?: string; title: string; children: ReactNode; action?: ReactNode }) { return <div className="empty"><span className="empty-icon"><Icon name={icon} size={26} /></span><h3>{title}</h3><p>{children}</p>{action}</div>; }
export function Panel({ title, eyebrow, action, children, className = '' }: { title: string; eyebrow?: string; action?: ReactNode; children: ReactNode; className?: string }) { return <section className={`panel ${className}`}><div className="panel-heading"><div>{eyebrow && <span className="eyebrow">{eyebrow}</span>}<h2>{title}</h2></div>{action}</div>{children}</section>; }
export function Field({ label, hint, children, wide }: { label: string; hint?: string; children: ReactNode; wide?: boolean }) {
  const id = useId();
  return <div className={`field ${wide ? 'wide' : ''}`}><label htmlFor={id}>{label}</label>{Children.map(children, child => isValidElement(child) && ['input', 'select', 'textarea'].includes(String(child.type)) ? cloneElement(child as ReactElement<Record<string, unknown>>, { id, 'aria-describedby': hint ? `${id}-hint` : undefined }) : child)}{hint && <small id={`${id}-hint`}>{hint}</small>}</div>;
}
export function Modal({ title, description, onClose, children, wide = false }: { title: string; description?: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const el = ref.current; el?.showModal(); return () => el?.close(); }, []);
  return <dialog ref={ref} className={`modal ${wide ? 'wide-modal' : ''}`} onCancel={onClose} onClick={e => { if (e.target === ref.current) { const r = ref.current.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) onClose(); } }}><div className="modal-title"><div><h2>{title}</h2>{description && <p>{description}</p>}</div><button type="button" className="icon-button" aria-label="Close dialog" onClick={onClose}><Icon name="close" /></button></div>{children}</dialog>;
}
export function ErrorNotice({ text }: { text: string }) { return text ? <div className="error-notice" role="alert"><Icon name="warning" size={18} /><span>{text}</span></div> : null; }

export function Design({ cells, small = false }: { cells?: number[]; small?: boolean }) { if (!cells?.length) return <div className={`design-empty ${small ? 'small' : ''}`}>No evaluated design yet</div>; return <div className={`design-wrap ${small ? 'small' : ''}`}><div className="binary-design" role="img" aria-label={`${cells.length}-cell binary grating: ${cells.join('')}`}>{cells.map((cell, i) => <i key={i} className={cell ? 'silicon' : 'air'} />)}</div>{!small && <div className="design-legend"><span><i /> Silicon</span><span><i className="air" /> Air</span><span>{cells.length} binary cells · one period</span></div>}</div>; }

export function SourceLink({ source }: { source: string | Record<string, any> }) {
  const url = typeof source === 'string' ? source : source.url || source.href || '';
  const label = typeof source === 'string' ? source : source.title || source.label || url;
  return /^https?:\/\//.test(url) ? <a className="source-link" href={url} target="_blank" rel="noreferrer">{label}<Icon name="external" size={13} /></a> : <span className="source-link">{label || JSON.stringify(source)}</span>;
}
