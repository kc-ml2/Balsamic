import { useCallback, useEffect, useState } from 'react';
import { api, errorText } from './api';
import type { Json } from './api';
import { DOCUMENTS, upload } from './problemImport';
import { ErrorNotice, Icon } from './ui';

export type FailedUpload = { file: File; error: string };
export const DOCUMENT_NOTICE = 'The text of each document is extracted on this machine and sent to the lead agent’s model provider when it reads it.';

const base = (campaignId: string) => `/api/campaigns/${encodeURIComponent(campaignId)}/materials`;

/** Attach documents to a campaign, one after another. Returns the ones that were refused, with why. */
export async function uploadMaterials(campaignId: string, files: File[], progress?: (text: string) => void): Promise<FailedUpload[]> {
  const failed: FailedUpload[] = [];
  for (const file of files) {
    progress?.(`Reading ${file.name}…`);
    try { await upload(`${base(campaignId)}/${encodeURIComponent(file.name)}`, file); }
    catch (error) { failed.push({ file, error: errorText(error) }); }
  }
  return failed;
}

/** The documents the researcher attached to the campaign, which its agents read as the problem's context. */
export function MaterialsPanel({ campaignId, failed, onFailed }: { campaignId: string; failed: FailedUpload[]; onFailed: (failed: FailedUpload[]) => void }) {
  const [documents, setDocuments] = useState<Json[] | null>(null), [error, setError] = useState(''), [progress, setProgress] = useState('');
  // Open while something needs the researcher, and as they leave it after that.
  const [expanded, setExpanded] = useState(failed.length > 0);
  useEffect(() => { if (failed.length) setExpanded(true); }, [failed.length]);
  const load = useCallback(async () => {
    try { setDocuments((await api<Json>(base(campaignId))).materials); setError(''); } catch (e) { setError(errorText(e)); }
  }, [campaignId]);
  useEffect(() => { void load(); }, [load]);
  async function attach(files: File[]) {
    setError('');
    const refused = await uploadMaterials(campaignId, files, setProgress);
    setProgress(''); onFailed(refused); await load();
  }
  async function remove(name: string) {
    try { await api(`${base(campaignId)}/${encodeURIComponent(name)}`, undefined, 'DELETE'); await load(); } catch (e) { setError(errorText(e)); }
  }
  const count = documents?.length || 0;
  return <details className="materials" open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary><Icon name="notebook" size={14} />Documents<span>{documents === null ? '' : count ? `${count} attached` : 'none attached'}</span></summary>
    <div className="materials-body">
      {failed.length > 0 && <div className="callout amber" role="alert"><strong>{failed.length === 1 ? 'A document was not attached.' : `${failed.length} documents were not attached.`}</strong>
        <ul>{failed.map(item => <li key={item.file.name}>{item.file.name}: {item.error}</li>)}</ul>
        <p>Try again, or go on without {failed.length === 1 ? 'it' : 'them'}.</p>
        <div className="inline-actions"><button type="button" className="button small primary" disabled={!!progress} onClick={() => void attach(failed.map(item => item.file))}>Try again</button>
          <button type="button" className="button small secondary" onClick={() => onFailed([])}>Dismiss</button></div></div>}
      {count > 0 && <ul className="materials-list">{documents!.map(item => <li key={item.id}>
        <strong>{item.name}</strong><span>{[item.pages ? `${item.pages} pages` : '', `${Number(item.characters || 0).toLocaleString()} characters`].filter(Boolean).join(' · ')}</span>
        {item.limitations?.length > 0 && <small>{item.limitations.join(' ')}</small>}
        <button type="button" className="text-button danger" aria-label={`Remove ${item.name}`} onClick={() => void remove(item.name)}>Remove</button></li>)}</ul>}
      <label className="button small secondary materials-add"><Icon name="plus" size={13} />Attach documents
        <input type="file" multiple accept={DOCUMENTS} className="sr-only" disabled={!!progress} onChange={event => { const files = [...(event.target.files || [])]; event.target.value = ''; if (files.length) void attach(files); }} /></label>
      {progress && <span className="help-text" role="status">{progress}</span>}
      <p className="help-text">PDF, Word, Markdown, TeX or text. The lead agent reads them when asked; figures and equations in a PDF may be lost. {DOCUMENT_NOTICE}</p>
      <ErrorNotice text={error} />
    </div></details>;
}
