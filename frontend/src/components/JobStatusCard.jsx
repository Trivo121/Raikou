import { useEffect, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, CircleAlert, Clock3, LoaderCircle, RefreshCw, XCircle } from 'lucide-react';
import { useApi } from '../auth/AuthProvider';

const TERMINAL_STATUSES = new Set(['complete', 'completed', 'succeeded', 'ready', 'failed', 'cancelled', 'canceled']);

export function isTerminalJobStatus(status) {
  return TERMINAL_STATUSES.has(String(status || '').toLowerCase());
}

function labelForStatus(status) {
  return String(status || 'queued').replace(/_/g, ' ');
}

function iconForStatus(status) {
  const normalized = String(status || '').toLowerCase();
  if (normalized === 'complete' || normalized === 'completed' || normalized === 'succeeded' || normalized === 'ready') return <CheckCircle2 size={18} className="text-emerald-400" />;
  if (normalized === 'failed') return <CircleAlert size={18} className="text-red-400" />;
  if (normalized === 'cancelled' || normalized === 'canceled') return <XCircle size={18} className="text-zinc-400" />;
  if (normalized === 'queued') return <Clock3 size={18} className="text-amber-300" />;
  return <LoaderCircle size={18} className="animate-spin text-[#53b1ff]" />;
}

function toneForStatus(status) {
  const normalized = String(status || '').toLowerCase();
  if (normalized === 'complete' || normalized === 'completed' || normalized === 'succeeded' || normalized === 'ready') return 'border-emerald-900/60 bg-emerald-950/20';
  if (normalized === 'failed') return 'border-red-900/60 bg-red-950/20';
  if (normalized === 'cancelled' || normalized === 'canceled') return 'border-zinc-700 bg-zinc-900/50';
  return 'border-[#0088ff]/30 bg-[#0d1720]';
}

function jobProgress(job) {
  const value = Number(job?.progress ?? job?.progress_percent);
  return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : null;
}

/** Poll one owner-scoped durable job with increasing intervals until completion. */
export default function JobStatusCard({ jobId, initialJob, userId, onTerminal }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const terminalNotified = useRef(null);

  useEffect(() => {
    if (jobId && initialJob) {
      queryClient.setQueryData(['jobs', userId, jobId], initialJob);
    }
  }, [initialJob, jobId, queryClient, userId]);

  const jobQuery = useQuery({
    queryKey: ['jobs', userId, jobId],
    queryFn: ({ signal }) => api.jobs.get(jobId, { signal }),
    enabled: Boolean(userId && jobId),
    staleTime: 0,
    refetchInterval: (query) => {
      const currentJob = query.state.data;
      if (isTerminalJobStatus(currentJob?.status)) return false;
      // Start responsive for a newly queued job, then back off to 12 seconds.
      const updates = Math.max(0, (query.state.dataUpdateCount || 0) - 1);
      const failures = query.state.fetchFailureCount || 0;
      return Math.min(12_000, 1_500 * (2 ** Math.min(3, Math.max(updates, failures))));
    },
    refetchIntervalInBackground: false,
  });

  const job = jobQuery.data || initialJob;
  const status = job?.status || 'queued';
  const terminal = isTerminalJobStatus(status);

  useEffect(() => {
    if (terminal && job?.id && terminalNotified.current !== job.id) {
      terminalNotified.current = job.id;
      onTerminal?.(job);
    }
  }, [job, onTerminal, terminal]);

  if (!jobId) return null;

  const progress = jobProgress(job);
  const failureMessage = job?.error_detail || job?.error_message || job?.error || job?.detail;

  return (
    <section className={`mt-4 rounded-xl border p-4 ${toneForStatus(status)}`} aria-live="polite">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 shrink-0">{iconForStatus(status)}</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h3 className="text-sm font-semibold capitalize text-white">Processing job: {labelForStatus(status)}</h3>
              <p className="mt-1 text-xs text-zinc-400">{terminal ? 'This job has reached a terminal state.' : 'The job is durable and will continue after you leave this page.'}</p>
            </div>
            {!terminal && <span className="rounded-full border border-[#0088ff]/30 bg-[#0088ff]/10 px-2 py-1 text-[11px] font-medium capitalize text-[#80c4ff]">Polling</span>}
          </div>

          {progress !== null && (
            <div className="mt-3">
              <div className="mb-1 flex justify-between text-[11px] text-zinc-500"><span>Progress</span><span>{Math.round(progress)}%</span></div>
              <div className="h-1.5 overflow-hidden rounded-full bg-zinc-800">
                <div className="h-full rounded-full bg-[#0088ff] transition-[width] duration-300" style={{ width: `${progress}%` }} />
              </div>
            </div>
          )}

          {failureMessage && <p className="mt-3 text-xs leading-5 text-red-300">{failureMessage}</p>}
          {jobQuery.isError && (
            <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-amber-200">
              <span>Could not refresh the latest job state: {jobQuery.error.message}</span>
              <button type="button" onClick={() => jobQuery.refetch()} className="inline-flex items-center gap-1 font-medium text-[#80c4ff] hover:text-white"><RefreshCw size={12} /> Retry now</button>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
