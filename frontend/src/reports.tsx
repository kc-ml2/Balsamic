import { useEffect, useState } from 'react';
import { api, errorText } from './api';
import type { State } from './api';
import { ErrorNotice } from './ui';

type Report = { id: string; title: string; url: string; parent_id: string | null; created_at: string; latest_submission_id: string | null };

export function ReportsView({ state }: { state: State }) {
  const [reports, setReports] = useState<Report[]>([]), [error, setError] = useState(''), [loading, setLoading] = useState(true);
  const campaignId = state.campaign?.id;
  useEffect(() => {
    let active = true;
    setLoading(true);
    api<{ reports: Report[] }>(`/api/reports${campaignId ? `?campaign_id=${encodeURIComponent(campaignId)}` : ''}`)
      .then(result => { if (active) { setReports(result.reports); setError(''); } })
      .catch(failure => { if (active) setError(errorText(failure)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [campaignId]);
  return <section aria-label="Reports">
    <div className="page-heading"><div><h1>Reports</h1><p>Read, mark, and refine a technical draft. Share the review as a single HTML file.</p></div></div>
    <p>Keep strong phrases in green, ask for improvements in yellow, and mark removals in red. Everything else stays flexible.</p>
    {error && <ErrorNotice text={error} />}
    {loading ? <p>Loading reports…</p> : reports.length ? <div style={{ display: 'grid', gap: 16, marginTop: 24 }}>
      {reports.map(report => <article key={report.id} style={{ padding: 24, background: 'white', border: '1px solid #d6e0e1', borderRadius: 10 }}>
        <p style={{ fontSize: 12, color: '#59727a' }}>{report.parent_id ? 'REVISED DRAFT' : 'ORIGINAL DRAFT'} · {new Date(report.created_at).toLocaleDateString()}</p>
        <h2>{report.title}</h2><p>{report.latest_submission_id ? 'Feedback submitted. Open the draft to continue reviewing or request a revision.' : 'Ready for highlights and short remarks.'}</p>
        <div style={{ display: 'flex', gap: 16, alignItems: 'center', flexWrap: 'wrap' }}><a className="button" href={report.url}>Open review →</a>
          <a href={`/api/reports/${report.id}/export?format=review`}>Download review HTML</a>
          {report.parent_id && <a href={`/reports/${report.parent_id}`}>Previous draft</a>}</div>
      </article>)}
    </div> : !error && <p>No reports have been added to this campaign yet.</p>}
  </section>;
}
