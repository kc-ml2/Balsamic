/** The formulation step still required before experiments can run, shown on every page until the charter is confirmed. */
import type { NextStep as Step } from '../api/types';
import { withThread } from '../router/routes';

export function NextStep({ step, threadId, page }: { step: Step | null | undefined; threadId: string | null; page: string }) {
  if (!step) return null;
  const here = step.page.replace(/^#/, '') === page;
  return (
    <div className={'callout' + (step.blocking ? ' attention' : '')} role="status" data-testid="next-step" data-kind={step.kind}>
      <span className="callout-mark" aria-hidden="true">{step.blocking ? '!' : '→'}</span>
      <div className="grow">
        <strong>{step.blocking ? `${step.proposals} proposal${step.proposals === 1 ? '' : 's'} wait for this: ${step.title.toLowerCase()}` : `Next: ${step.title.toLowerCase()}`}</strong>
        <div className="small">{step.text}</div>
      </div>
      {!here && step.page ? <a className="btn small primary" href={withThread(step.page, threadId)} data-testid="next-step-open">{step.title}</a> : null}
    </div>
  );
}
