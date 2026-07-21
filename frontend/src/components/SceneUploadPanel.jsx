import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { CircleAlert, FileUp, LoaderCircle, ShieldCheck, X } from 'lucide-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useApi } from '../auth/AuthProvider';
import JobStatusCard, { isTerminalJobStatus } from './JobStatusCard';
import {
  UploadCancelledError,
  isUploadCancelled,
  prepareUploadDescriptors,
  uploadMultipartPlan,
} from '../services/multipartUpload';
import { formatBytes, supportedInputDescription, validateUploadFiles } from '../utils/uploadValidation';

const PLAN_REVOKE_TIMEOUT_MS = 15_000;
const PENDING_COMPLETION_MAX_AGE_MS = 24 * 60 * 60_000;
const PENDING_INITIATION_MAX_AGE_MS = 24 * 60 * 60_000;
const INITIATION_RECOVERY_WINDOW_MS = 60_000;
const INITIATION_RECOVERY_POLL_MS = 2_000;
const RECONCILIATION_ESCAPE_AFTER_MS = 45_000;

function pendingCompletionStorageKey(userId, sceneId) {
  return `raikou:m2:pending-completion:${encodeURIComponent(userId)}:${encodeURIComponent(sceneId)}`;
}

function pendingInitiationStorageKey(userId, sceneId) {
  return `raikou:m2:pending-initiation:${encodeURIComponent(userId)}:${encodeURIComponent(sceneId)}`;
}

function readPendingCompletion(userId, sceneId) {
  if (!userId || !sceneId) return null;
  try {
    const storage = globalThis.sessionStorage;
    const key = pendingCompletionStorageKey(userId, sceneId);
    const value = JSON.parse(storage.getItem(key) || 'null');
    if (!value || typeof value.planId !== 'string' || !value.planId) return null;
    if (Number.isFinite(value.submittedAt) && Date.now() - value.submittedAt > PENDING_COMPLETION_MAX_AGE_MS) {
      storage.removeItem(key);
      return null;
    }
    return value;
  } catch {
    return null;
  }
}

function persistPendingCompletion(userId, sceneId, planId) {
  if (!userId || !sceneId || !planId) return;
  try {
    globalThis.sessionStorage.setItem(
      pendingCompletionStorageKey(userId, sceneId),
      JSON.stringify({ planId, submittedAt: Date.now() }),
    );
  } catch {
    // Private browsing/storage policies must not prevent the upload itself.
  }
}

function clearPendingCompletion(userId, sceneId) {
  if (!userId || !sceneId) return;
  try {
    globalThis.sessionStorage.removeItem(pendingCompletionStorageKey(userId, sceneId));
  } catch {
    // Best effort only; the exact plan-id filter prevents cross-plan recovery.
  }
}

function readPendingInitiation(userId, sceneId) {
  if (!userId || !sceneId) return null;
  try {
    const storage = globalThis.sessionStorage;
    const key = pendingInitiationStorageKey(userId, sceneId);
    const value = JSON.parse(storage.getItem(key) || 'null');
    if (!value || typeof value.requestId !== 'string' || !value.requestId) return null;
    if (Number.isFinite(value.startedAt) && Date.now() - value.startedAt > PENDING_INITIATION_MAX_AGE_MS) {
      storage.removeItem(key);
      return null;
    }
    return value;
  } catch {
    return null;
  }
}

function persistPendingInitiation(userId, sceneId, requestId, planId = null) {
  if (!userId || !sceneId || !requestId) return;
  try {
    const existing = readPendingInitiation(userId, sceneId);
    globalThis.sessionStorage.setItem(
      pendingInitiationStorageKey(userId, sceneId),
      JSON.stringify({
        requestId,
        planId: planId || existing?.planId || null,
        startedAt: existing?.requestId === requestId && Number.isFinite(existing?.startedAt)
          ? existing.startedAt
          : Date.now(),
      }),
    );
  } catch {
    // Storage is a recovery aid only; it must not block an upload.
  }
}

function clearPendingInitiation(userId, sceneId, { requestId, planId } = {}) {
  if (!userId || !sceneId) return;
  try {
    const pending = readPendingInitiation(userId, sceneId);
    if (!pending) return;
    if (requestId && pending.requestId !== requestId) return;
    if (planId && pending.planId !== planId) return;
    globalThis.sessionStorage.removeItem(pendingInitiationStorageKey(userId, sceneId));
  } catch {
    // Best effort only; an exact request ID prevents cross-upload recovery.
  }
}

function createClientRequestId() {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID();
  const randomHex = () => Math.floor(Math.random() * 0x100000000).toString(16).padStart(8, '0');
  const first = randomHex();
  const second = randomHex();
  const third = randomHex();
  const fourth = randomHex();
  return `${first}-${second.slice(0, 4)}-4${second.slice(5, 8)}-${((8 + Math.floor(Math.random() * 4)).toString(16))}${third.slice(1, 4)}-${third.slice(4)}${fourth}`;
}

function waitForRecoveryDelay(delayMs, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new UploadCancelledError());
      return;
    }
    const timer = globalThis.setTimeout(resolve, delayMs);
    signal?.addEventListener('abort', () => {
      globalThis.clearTimeout(timer);
      reject(new UploadCancelledError());
    }, { once: true });
  });
}

function displayError(error) {
  return error?.message || 'The upload could not be completed. Please try again.';
}

function percent(loaded, total) {
  if (!total) return 0;
  return Math.min(100, Math.max(0, Math.round((loaded / total) * 100)));
}

function phaseLabel(phase) {
  const labels = {
    hashing_files: 'Checking selected file checksums',
    initiating: 'Creating secure upload plan',
    hashing_parts: 'Verifying upload parts',
    signing_parts: 'Requesting signed upload URLs',
    uploading_parts: 'Uploading directly to object storage',
    completing: 'Verifying upload and queueing processing',
    reconciling: 'Confirming the durable processing job',
    reconciliation_action_required: 'Completion needs a safe recovery action',
    releasing_reconciliation: 'Releasing the stalled upload plan',
    recovering_initiation: 'Recovering an interrupted upload setup',
    initiation_recovery_needed: 'Interrupted upload setup needs recovery',
    cancelling: 'Cancelling upload and revoking access',
    cancelled: 'Upload cancelled',
    completed: 'Upload verified and processing queued',
  };
  return labels[phase] || null;
}

/**
 * Per-scene direct-upload flow. Product API calls only create/sign/complete a
 * plan; all selected file bytes go from the browser to object storage.
 */
export default function SceneUploadPanel({ projectId, scene, userId, defaultOpen = false, onComplete, onTerminal }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const apiRef = useRef(api);
  const inputRef = useRef(null);
  const mountedRef = useRef(true);
  const controllerRef = useRef(null);
  const runIdRef = useRef(0);
  const planRef = useRef(null);
  const revokePromisesRef = useRef(new Map());
  const revokeControllersRef = useRef(new Map());
  const completionInFlightRef = useRef(false);
  const completionPlanRef = useRef(null);
  const [isOpen, setIsOpen] = useState(() => (
    defaultOpen
    || Boolean(readPendingCompletion(userId, scene?.id))
    || Boolean(readPendingInitiation(userId, scene?.id))
  ));
  const [entries, setEntries] = useState([]);
  const [validationErrors, setValidationErrors] = useState([]);
  const [phase, setPhase] = useState(() => (
    readPendingCompletion(userId, scene?.id)
      ? 'reconciling'
      : (readPendingInitiation(userId, scene?.id) ? 'recovering_initiation' : 'idle')
  ));
  const [phaseDetail, setPhaseDetail] = useState('');
  const [error, setError] = useState(null);
  const [uploadProgress, setUploadProgress] = useState(null);
  const [checksumProgress, setChecksumProgress] = useState(null);
  const [activeJob, setActiveJob] = useState(null);
  const [reconcilingPlanId, setReconcilingPlanId] = useState(() => (
    readPendingCompletion(userId, scene?.id)?.planId || null
  ));
  const [reconcilingStartedAt, setReconcilingStartedAt] = useState(() => (
    readPendingCompletion(userId, scene?.id)?.submittedAt || null
  ));
  const [initiationRecoveryAttempt, setInitiationRecoveryAttempt] = useState(0);

  const sceneJobsQuery = useQuery({
    queryKey: ['scenes', userId, scene?.id, 'jobs'],
    queryFn: ({ signal }) => api.jobs.listForScene(scene.id, { signal }),
    enabled: Boolean(userId && scene?.id),
    refetchInterval: phase === 'reconciling' ? 3000 : false,
  });
  const artifactsQuery = useQuery({
    queryKey: ['scenes', userId, scene?.id, 'artifacts'],
    queryFn: ({ signal }) => api.artifacts.listForScene(scene.id, { signal }),
    enabled: Boolean(userId && scene?.id),
  });
  const uploadPlanStatusQuery = useQuery({
    queryKey: ['upload-plans', userId, reconcilingPlanId, 'status'],
    queryFn: ({ signal }) => api.uploads.getStatus(reconcilingPlanId, { signal }),
    enabled: Boolean(userId && reconcilingPlanId && phase === 'reconciling'),
    refetchInterval: phase === 'reconciling' ? 3000 : false,
  });

  const isBusy = !['idle', 'cancelled', 'error', 'completed'].includes(phase);
  const totalSelectedBytes = useMemo(
    () => entries.reduce((total, entry) => total + entry.file.size, 0),
    [entries],
  );
  const persistedJob = useMemo(() => {
    const jobs = sceneJobsQuery.data || [];
    return jobs.find((job) => !isTerminalJobStatus(job?.status)) || jobs[0] || null;
  }, [sceneJobsQuery.data]);
  const reconciledJob = useMemo(() => {
    if (!reconcilingPlanId) return null;
    return (sceneJobsQuery.data || []).find((job) => job?.upload_plan_id === reconcilingPlanId) || null;
  }, [reconcilingPlanId, sceneJobsQuery.data]);
  const visibleJob = reconcilingPlanId ? (uploadPlanStatusQuery.data?.job || reconciledJob) : activeJob || persistedJob;
  const sourceArtifacts = useMemo(
    () => (artifactsQuery.data || []).filter((artifact) => (
      artifact?.kind === 'source_archive' || artifact?.kind === 'source_raster' || artifact?.kind === 'metadata'
    )),
    [artifactsQuery.data],
  );

  useEffect(() => {
    apiRef.current = api;
  }, [api]);

  useEffect(() => {
    const pending = readPendingCompletion(userId, scene?.id);
    if (!pending?.planId) return;
    completionPlanRef.current = pending.planId;
    setReconcilingPlanId(pending.planId);
    setReconcilingStartedAt(pending.submittedAt || Date.now());
    setPhase((current) => (current === 'idle' ? 'reconciling' : current));
    setIsOpen(true);
  }, [scene?.id, userId]);

  useEffect(() => {
    if (phase !== 'reconciling' || !reconciledJob?.id) return;
    setActiveJob(reconciledJob);
    setReconcilingPlanId(null);
    setReconcilingStartedAt(null);
    completionPlanRef.current = null;
    clearPendingCompletion(userId, scene?.id);
    setError(null);
    setPhase('completed');
    onComplete?.({
      scene,
      job: reconciledJob,
      artifacts: artifactsQuery.data || [],
      dispatch_status: 'recovered',
    });
  }, [artifactsQuery.data, onComplete, phase, reconciledJob, scene, userId]);

  const revokeActivePlan = useCallback(async (requestedPlanId = planRef.current?.id) => {
    const planId = requestedPlanId;
    if (!planId) return;
    const existing = revokePromisesRef.current.get(planId);
    if (existing) return existing;

    const revokeController = new AbortController();
    revokeControllersRef.current.set(planId, revokeController);
    const revokePromise = apiRef.current.uploads.abort(planId, {
      signal: revokeController.signal,
      timeoutMs: PLAN_REVOKE_TIMEOUT_MS,
    })
      .then((result) => {
        // Only clear the matching persisted record. A late cleanup from a
        // previous upload must never erase a newer upload's recovery key.
        clearPendingInitiation(userId, scene?.id, { planId });
        return result;
      })
      .finally(() => {
        revokePromisesRef.current.delete(planId);
        if (revokeControllersRef.current.get(planId) === revokeController) {
          revokeControllersRef.current.delete(planId);
        }
        if (planRef.current?.id === planId) planRef.current = null;
      });
    revokePromisesRef.current.set(planId, revokePromise);
    return revokePromise;
  }, [scene?.id, userId]);

  const findInitiatedPlan = useCallback(async (requestId, { signal, waitForCreation = false } = {}) => {
    const deadline = Date.now() + (waitForCreation ? INITIATION_RECOVERY_WINDOW_MS : 0);
    let lastError = null;
    do {
      try {
        return await apiRef.current.uploads.getInitiation(requestId, { signal });
      } catch (caught) {
        lastError = caught;
        if (caught?.status !== 404 || !waitForCreation || Date.now() >= deadline) break;
        await waitForRecoveryDelay(INITIATION_RECOVERY_POLL_MS, signal);
      }
    } while (!signal?.aborted && Date.now() < deadline);

    if (lastError?.status === 404) return null;
    throw lastError || new Error('Could not recover the interrupted upload plan.');
  }, []);

  const recoverAndRevokeInitiation = useCallback(async ({ requestId, initiatePayload }) => {
    if (!requestId) return null;
    let plan = null;
    let initiateError = null;
    // Retrying with the same durable request ID either creates the plan (when
    // the original request never reached FastAPI) or returns the exact plan
    // already created by the original request. It never creates a second plan.
    if (initiatePayload) {
      try {
        plan = await apiRef.current.uploads.initiate(initiatePayload, {
          timeoutMs: INITIATION_RECOVERY_WINDOW_MS,
        });
      } catch (caught) {
        initiateError = caught;
        if (
          caught?.status
          && caught.status < 500
          && caught.status !== 409
          && caught.status !== 408
          && !caught?.isTimeout
        ) {
          clearPendingInitiation(userId, scene?.id, { requestId });
          return null;
        }
      }
    }
    if (!plan) plan = await findInitiatedPlan(requestId, { waitForCreation: true });
    if (!plan) {
      // A definitive 4xx response plus an owner-scoped 404 proves this
      // request ID never produced a plan. In particular, a 409 can describe
      // a different active plan for this scene, which must not leave this
      // panel's unrelated recovery key blocking the user indefinitely.
      if (
        initiateError?.status
        && initiateError.status >= 400
        && initiateError.status < 500
        && initiateError.status !== 408
        && !initiateError?.isTimeout
      ) {
        clearPendingInitiation(userId, scene?.id, { requestId });
      }
      return null;
    }
    persistPendingInitiation(userId, scene?.id, requestId, plan.id);
    if (['initiated', 'uploading'].includes(plan.status)) {
      await revokeActivePlan(plan.id);
    } else if (plan.status === 'completing' || plan.status === 'completed') {
      // The request crossed into durable completion, so the completion record
      // becomes the single recovery source rather than an initiation record.
      persistPendingCompletion(userId, scene?.id, plan.id);
      clearPendingInitiation(userId, scene?.id, { requestId, planId: plan.id });
    } else {
      clearPendingInitiation(userId, scene?.id, { requestId, planId: plan.id });
    }
    return plan;
  }, [findInitiatedPlan, revokeActivePlan, scene?.id, userId]);

  useEffect(() => {
    const pendingCompletion = readPendingCompletion(userId, scene?.id);
    const pendingInitiation = readPendingInitiation(userId, scene?.id);
    if (pendingCompletion?.planId || !pendingInitiation?.requestId) return undefined;

    let disposed = false;
    const recoveryController = new AbortController();
    const recover = async () => {
      if (mountedRef.current) {
        setIsOpen(true);
        setPhase('recovering_initiation');
        setPhaseDetail('');
      }
      try {
        const plan = await findInitiatedPlan(pendingInitiation.requestId, {
          signal: recoveryController.signal,
          waitForCreation: true,
        });
        if (disposed || recoveryController.signal.aborted) return;
        if (!plan) {
          if (mountedRef.current) {
            setError('We could not confirm whether an interrupted upload plan was created. Retry recovery before starting another upload.');
            setPhase('initiation_recovery_needed');
          }
          return;
        }
        persistPendingInitiation(userId, scene?.id, pendingInitiation.requestId, plan.id);
        if (plan.status === 'completing' || plan.status === 'completed') {
          persistPendingCompletion(userId, scene?.id, plan.id);
          clearPendingInitiation(userId, scene?.id, { requestId: pendingInitiation.requestId, planId: plan.id });
          completionPlanRef.current = plan.id;
          if (mountedRef.current) {
            setReconcilingPlanId(plan.id);
            setReconcilingStartedAt(Date.now());
            setPhase('reconciling');
          }
          return;
        }
        if (['initiated', 'uploading'].includes(plan.status)) {
          await revokeActivePlan(plan.id);
          if (!disposed && mountedRef.current) {
            setError(null);
            setPhase('cancelled');
          }
          return;
        }
        clearPendingInitiation(userId, scene?.id, { requestId: pendingInitiation.requestId, planId: plan.id });
        if (mountedRef.current) {
          setError(plan.failure_detail || 'The interrupted upload plan is no longer active. You can start a new upload.');
          setPhase('error');
        }
      } catch (caught) {
        if (disposed || recoveryController.signal.aborted || isUploadCancelled(caught)) return;
        if (mountedRef.current) {
          setError(`${displayError(caught)} Retry recovery before starting another upload.`);
          setPhase('initiation_recovery_needed');
        }
      }
    };
    void recover();
    return () => {
      disposed = true;
      recoveryController.abort();
    };
  }, [findInitiatedPlan, initiationRecoveryAttempt, revokeActivePlan, scene?.id, userId]);

  useEffect(() => {
    const plan = uploadPlanStatusQuery.data;
    if (phase !== 'reconciling' || !reconcilingPlanId || !plan) return;
    if (plan.job?.id) {
      setActiveJob(plan.job);
      setReconcilingPlanId(null);
      setReconcilingStartedAt(null);
      completionPlanRef.current = null;
      clearPendingCompletion(userId, scene?.id);
      setError(null);
      setPhase('completed');
      onComplete?.({
        scene,
        job: plan.job,
        artifacts: artifactsQuery.data || [],
        dispatch_status: 'recovered',
      });
      return;
    }
    if (['aborted', 'expired', 'failed'].includes(plan.status)) {
      setReconcilingPlanId(null);
      setReconcilingStartedAt(null);
      completionPlanRef.current = null;
      clearPendingCompletion(userId, scene?.id);
      setError(plan.failure_detail || 'The upload did not finish processing. You can start a new upload.');
      setPhase('error');
      return;
    }
    if (
      ['initiated', 'uploading'].includes(plan.status)
      && Date.now() - (reconcilingStartedAt || Date.now()) >= RECONCILIATION_ESCAPE_AFTER_MS
    ) {
      setError('The completion request did not start on the server. You can safely release this stalled upload plan and try again.');
      setPhase('reconciliation_action_required');
    }
  }, [artifactsQuery.data, onComplete, phase, reconcilingPlanId, reconcilingStartedAt, scene, uploadPlanStatusQuery.data, userId]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (!completionInFlightRef.current) {
        const planId = planRef.current?.id;
        const pendingInitiation = readPendingInitiation(userId, scene?.id);
        const controller = controllerRef.current;
        runIdRef.current += 1;
        controller?.abort();
        if (controllerRef.current === controller) controllerRef.current = null;
        // A best-effort revoke prevents abandoned direct-upload URLs from being
        // usable until their short expiry when the user navigates away mid-upload.
        // Do not abort an already-running revoke: reusing that promise lets
        // the bounded DELETE finish instead of silently losing its cleanup.
        if (planId) {
          void revokeActivePlan(planId).catch(() => undefined);
        } else if (pendingInitiation?.requestId) {
          void recoverAndRevokeInitiation({ requestId: pendingInitiation.requestId }).catch(() => undefined);
        }
      }
    };
  }, [recoverAndRevokeInitiation, revokeActivePlan, scene?.id, userId]);

  function selectFiles(fileList) {
    if (isBusy) return;
    const result = validateUploadFiles(fileList);
    setEntries(result.files);
    setValidationErrors(result.errors);
    setError(null);
    setPhase('idle');
    setUploadProgress(null);
    setChecksumProgress(null);
    setReconcilingPlanId(null);
    setReconcilingStartedAt(null);
  }

  function handleFileInput(event) {
    selectFiles(event.target.files);
    // Retain the File objects in component state while allowing a user to pick
    // the same file again after removing/retrying it.
    event.target.value = '';
  }

  async function startUpload() {
    // React state updates are asynchronous, so a synchronous lock also stops
    // a fast double-click from starting two plans before ``isBusy`` rerenders.
    const pendingCompletion = readPendingCompletion(userId, scene.id);
    if (completionInFlightRef.current || completionPlanRef.current || pendingCompletion?.planId) {
      const planId = completionPlanRef.current || pendingCompletion?.planId;
      if (planId) {
        setReconcilingPlanId(planId);
        setReconcilingStartedAt(pendingCompletion?.submittedAt || Date.now());
      }
      setPhase('reconciling');
      return;
    }
    if (isBusy || controllerRef.current) return;
    if (readPendingInitiation(userId, scene.id)) {
      setIsOpen(true);
      setError('An earlier upload setup is still being recovered. Retry recovery before starting another upload.');
      setPhase('initiation_recovery_needed');
      return;
    }
    const currentValidation = validateUploadFiles(entries.map((entry) => entry.file));
    if (currentValidation.errors.length) {
      setValidationErrors(currentValidation.errors);
      return;
    }

    const controller = new AbortController();
    controllerRef.current = controller;
    const runId = runIdRef.current + 1;
    runIdRef.current = runId;
    planRef.current = null;
    completionInFlightRef.current = false;
    completionPlanRef.current = null;
    clearPendingCompletion(userId, scene.id);
    clearPendingInitiation(userId, scene.id);
    setError(null);
    setValidationErrors([]);
    setUploadProgress(null);
    setChecksumProgress(null);
    setReconcilingPlanId(null);
    setReconcilingStartedAt(null);
    let completionSubmitted = false;
    let initiatedPlanId = null;
    let initiationRequestId = null;
    let initiatePayload = null;

    const isCurrentRun = () => (
      runIdRef.current === runId && controllerRef.current === controller
    );

    try {
      setPhase('hashing_files');
      const descriptors = await prepareUploadDescriptors(entries, {
        signal: controller.signal,
        onProgress: (progress) => {
          if (!mountedRef.current || !isCurrentRun()) return;
          setChecksumProgress(progress);
          setPhaseDetail(progress.filename);
        },
      });
      if (controller.signal.aborted) throw new UploadCancelledError();

      setPhase('initiating');
      setPhaseDetail('');
      initiationRequestId = createClientRequestId();
      initiatePayload = {
        project_id: projectId,
        scene_id: scene.id,
        files: descriptors,
        client_request_id: initiationRequestId,
      };
      // Keep this key before the request starts. If the response is lost, a
      // later request can discover and revoke only this exact upload plan.
      persistPendingInitiation(userId, scene.id, initiationRequestId);
      const plan = await api.uploads.initiate(initiatePayload, { signal: controller.signal });
      initiatedPlanId = plan.id;
      persistPendingInitiation(userId, scene.id, initiationRequestId, plan.id);
      planRef.current = plan;
      if (controller.signal.aborted || !isCurrentRun()) throw new UploadCancelledError();

      const completePayload = await uploadMultipartPlan({
        plan,
        entries,
        signParts: api.uploads.signParts,
        signal: controller.signal,
        onProgress: (progress) => {
          if (mountedRef.current && isCurrentRun()) setUploadProgress(progress);
        },
        onPhase: (nextPhase) => {
          if (!mountedRef.current || !isCurrentRun()) return;
          setPhase(nextPhase.type);
          setPhaseDetail(nextPhase.filename || '');
        },
      });
      if (controller.signal.aborted || !isCurrentRun()) throw new UploadCancelledError();

      setPhase('completing');
      setPhaseDetail('');
      // Once the completion request has been sent, its outcome can be
      // ambiguous (the database may commit after a network timeout). Do not
      // let unmount cleanup abort a plan that could already be durable.
      completionSubmitted = true;
      completionInFlightRef.current = true;
      completionPlanRef.current = plan.id;
      persistPendingCompletion(userId, scene.id, plan.id);
      clearPendingInitiation(userId, scene.id, { requestId: initiationRequestId, planId: plan.id });
      setReconcilingPlanId(plan.id);
      setReconcilingStartedAt(Date.now());
      planRef.current = null;
      const result = await api.uploads.complete(plan.id, completePayload, { signal: controller.signal });
      if (isCurrentRun()) completionInFlightRef.current = false;
      if (controller.signal.aborted) throw new UploadCancelledError();

      const job = result?.job || result?.data?.job;
      if (job?.id) {
        queryClient.setQueryData(['jobs', userId, job.id], job);
      }
      queryClient.invalidateQueries({ queryKey: ['scenes', userId, scene.id, 'jobs'] });
      queryClient.invalidateQueries({ queryKey: ['scenes', userId, scene.id, 'artifacts'] });
      completionPlanRef.current = null;
      clearPendingCompletion(userId, scene.id);
      if (!mountedRef.current || !isCurrentRun()) return;
      if (job?.id) setActiveJob(job);
      setEntries([]);
      setUploadProgress(null);
      setChecksumProgress(null);
      setReconcilingPlanId(null);
      setReconcilingStartedAt(null);
      setPhase('completed');
      onComplete?.(result);
    } catch (caught) {
      if (completionSubmitted && isCurrentRun()) completionInFlightRef.current = false;
      const cancelled = isUploadCancelled(caught) || controller.signal.aborted;
      const completionNeedsReconciliation = completionSubmitted && (
        cancelled
        || caught?.isTimeout
        || !caught?.status
        || caught?.status >= 500
        || caught?.payload?.detail?.code === 'completion_reconciliation_required'
      );
      if (completionNeedsReconciliation) {
        queryClient.invalidateQueries({ queryKey: ['scenes', userId, scene.id, 'jobs'] });
        queryClient.invalidateQueries({ queryKey: ['scenes', userId, scene.id, 'artifacts'] });
        if (!mountedRef.current || !isCurrentRun()) return;
        setError(`${displayError(caught)} We are checking whether processing was queued; this upload will not be revoked automatically.`);
        setPhase('reconciling');
        setPhaseDetail('');
        return;
      }
      if (!completionSubmitted) {
        // Stop any remaining local work before asking FastAPI to revoke the
        // plan. The revocation has its own short timeout, so the interface is
        // never held hostage by an unavailable control plane.
        controller.abort();
        const revocation = initiatedPlanId
          ? revokeActivePlan(initiatedPlanId)
          : recoverAndRevokeInitiation({ requestId: initiationRequestId, initiatePayload });
        if (mountedRef.current && isCurrentRun()) {
          if (cancelled) {
            setPhase('cancelled');
            setPhaseDetail('');
          } else {
            setError(displayError(caught));
            setPhase('error');
          }
        }
        void Promise.resolve(revocation).catch((revokeError) => {
          if (!mountedRef.current || !isCurrentRun()) return;
          setError((current) => `${current || 'Upload stopped.'} Temporary storage cleanup could not be confirmed: ${displayError(revokeError)}`);
        });
        return;
      }

      completionPlanRef.current = null;
      clearPendingCompletion(userId, scene.id);
      if (!mountedRef.current || !isCurrentRun()) return;
      setReconcilingPlanId(null);
      setReconcilingStartedAt(null);
      setError(displayError(caught));
      setPhase('error');
    } finally {
      if (controllerRef.current === controller) controllerRef.current = null;
    }
  }

  function retryInitiationRecovery() {
    if (!readPendingInitiation(userId, scene.id)) {
      setError(null);
      setPhase('idle');
      return;
    }
    setError(null);
    setPhase('recovering_initiation');
    setInitiationRecoveryAttempt((attempt) => attempt + 1);
  }

  function dismissInitiationRecovery() {
    // This only drops local recovery state after the bounded, owner-scoped
    // lookup found no plan for this exact request ID. If an old request later
    // appears, the server's one-active-plan invariant still protects the
    // scene and returns an actionable conflict instead of accepting overlap.
    clearPendingInitiation(userId, scene.id);
    setError('The earlier upload setup was not found. You can try again; the server will prevent overlapping uploads.');
    setPhase('error');
  }

  async function releaseStalledCompletion() {
    const planId = reconcilingPlanId;
    if (!planId) return;
    setError(null);
    setPhase('releasing_reconciliation');
    try {
      const plan = await api.uploads.getStatus(planId);
      if (plan.job?.id) {
        setActiveJob(plan.job);
        setReconcilingPlanId(null);
        setReconcilingStartedAt(null);
        completionPlanRef.current = null;
        clearPendingCompletion(userId, scene.id);
        setPhase('completed');
        onComplete?.({
          scene,
          job: plan.job,
          artifacts: artifactsQuery.data || [],
          dispatch_status: 'recovered',
        });
        return;
      }
      if (['aborted', 'expired', 'failed'].includes(plan.status)) {
        setReconcilingPlanId(null);
        setReconcilingStartedAt(null);
        completionPlanRef.current = null;
        clearPendingCompletion(userId, scene.id);
        setError(plan.failure_detail || 'The upload did not finish processing. You can start a new upload.');
        setPhase('error');
        return;
      }
      if (['initiated', 'uploading'].includes(plan.status)) {
        await api.uploads.abort(planId, { timeoutMs: PLAN_REVOKE_TIMEOUT_MS });
        setReconcilingPlanId(null);
        setReconcilingStartedAt(null);
        completionPlanRef.current = null;
        clearPendingCompletion(userId, scene.id);
        setError('The stalled upload plan was released. You can start the upload again.');
        setPhase('cancelled');
        queryClient.invalidateQueries({ queryKey: ['scenes', userId, scene.id, 'jobs'] });
        queryClient.invalidateQueries({ queryKey: ['scenes', userId, scene.id, 'artifacts'] });
        return;
      }
      // The status changed after the bounded recovery check. A completion
      // lease is deliberately not cancellable, so return to durable polling.
      setPhase('reconciling');
      queryClient.invalidateQueries({ queryKey: ['upload-plans', userId, planId, 'status'] });
    } catch (caught) {
      if (caught?.status === 409) {
        setPhase('reconciling');
        queryClient.invalidateQueries({ queryKey: ['upload-plans', userId, planId, 'status'] });
        return;
      }
      setError(`${displayError(caught)} The upload was not released; retry the recovery action.`);
      setPhase('reconciliation_action_required');
    }
  }

  function cancelUpload() {
    const pendingCompletion = readPendingCompletion(userId, scene.id);
    if (completionInFlightRef.current || completionPlanRef.current || pendingCompletion?.planId) {
      const planId = completionPlanRef.current || pendingCompletion?.planId;
      if (planId) {
        setReconcilingPlanId(planId);
        setReconcilingStartedAt(pendingCompletion?.submittedAt || Date.now());
      }
      setPhase('reconciling');
      setPhaseDetail('');
      return;
    }
    if (!isBusy || [
      'completing',
      'reconciling',
      'recovering_initiation',
      'initiation_recovery_needed',
      'reconciliation_action_required',
      'releasing_reconciliation',
    ].includes(phase)) return;
    const planId = planRef.current?.id;
    const controller = controllerRef.current;
    const cancellationRunId = runIdRef.current + 1;
    runIdRef.current = cancellationRunId;
    controller?.abort();
    if (controllerRef.current === controller) controllerRef.current = null;
    const revocation = revokeActivePlan(planId);
    setPhase('cancelled');
    setPhaseDetail('');
    void Promise.resolve(revocation).catch((revokeError) => {
      // Do not let cleanup for cancelled plan A overwrite the state of a
      // freshly started plan B.
      if (!mountedRef.current || runIdRef.current !== cancellationRunId) return;
      setError(`Upload cancelled. Temporary storage cleanup could not be confirmed: ${displayError(revokeError)}`);
    });
  }

  function clearSelection() {
    if (isBusy) return;
    setEntries([]);
    setValidationErrors([]);
    setError(null);
    setUploadProgress(null);
    setChecksumProgress(null);
    setReconcilingPlanId(null);
    setReconcilingStartedAt(null);
    completionPlanRef.current = null;
    clearPendingCompletion(userId, scene.id);
    setPhase('idle');
  }

  const statusText = phaseLabel(phase);
  const isUploading = phase === 'uploading_parts';
  const isHashingFullFile = phase === 'hashing_files';

  return (
    <section className="mt-3">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-zinc-800 bg-[#101014] px-4 py-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-white">Source data</p>
          <p className="mt-1 text-xs text-zinc-500">{supportedInputDescription()}</p>
        </div>
        <button
          type="button"
          onClick={() => setIsOpen((open) => !open)}
          disabled={isBusy}
          className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm font-medium text-white transition hover:border-zinc-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <FileUp size={15} /> {isOpen ? 'Hide upload' : 'Upload input'}
        </button>
      </div>

      {isOpen && (
        <div className="mt-3 rounded-xl border border-zinc-700 bg-[#111114] p-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h3 className="text-sm font-medium text-white">Add source files</h3>
              <p className="mt-1 max-w-2xl text-xs leading-5 text-zinc-500">Files upload straight from this browser to the storage provider. The API only creates, signs, verifies, and revokes the upload plan.</p>
            </div>
            <input
              ref={inputRef}
              type="file"
              multiple
              accept=".zip,.tif,.tiff,.json,application/zip,image/tiff,application/json"
              onChange={handleFileInput}
              disabled={isBusy}
              className="block max-w-full text-xs text-zinc-400 file:mr-3 file:rounded-md file:border-0 file:bg-zinc-800 file:px-3 file:py-2 file:text-xs file:font-medium file:text-white hover:file:bg-zinc-700 disabled:opacity-50"
            />
          </div>

          {entries.length > 0 && (
            <div className="mt-4 overflow-hidden rounded-lg border border-zinc-800">
              <ul className="divide-y divide-zinc-800">
                {entries.map((entry) => {
                  const progress = uploadProgress?.files?.find((file) => file.filename === entry.filename);
                  return (
                    <li key={entry.filename} className="bg-zinc-950/40 px-3 py-2.5">
                      <div className="flex items-center justify-between gap-3 text-xs">
                        <span className="min-w-0 truncate font-medium text-zinc-200">{entry.filename}</span>
                        <span className="shrink-0 text-zinc-500">{formatBytes(entry.file.size)}</span>
                      </div>
                      {progress && (
                        <div className="mt-2">
                          <div className="mb-1 flex justify-between text-[11px] text-zinc-500"><span>{formatBytes(progress.loaded_bytes)} / {formatBytes(progress.total_bytes)}</span><span>{percent(progress.loaded_bytes, progress.total_bytes)}%</span></div>
                          <div className="h-1 overflow-hidden rounded-full bg-zinc-800"><div className="h-full rounded-full bg-[#0088ff]" style={{ width: `${percent(progress.loaded_bytes, progress.total_bytes)}%` }} /></div>
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}

          {validationErrors.length > 0 && (
            <div className="mt-4 rounded-lg border border-amber-900/60 bg-amber-950/20 p-3 text-xs leading-5 text-amber-100">
              <div className="mb-1 flex items-center gap-2 font-medium"><CircleAlert size={14} /> Fix these files before uploading</div>
              <ul className="list-disc space-y-1 pl-5">{validationErrors.map((message) => <li key={message}>{message}</li>)}</ul>
            </div>
          )}
          {error && <div className="mt-4 rounded-lg border border-red-900/60 bg-red-950/20 p-3 text-xs leading-5 text-red-200">{error}</div>}

          {statusText && (
            <div className="mt-4 rounded-lg border border-[#0088ff]/20 bg-[#0088ff]/5 p-3">
              <div className="flex items-center gap-2 text-xs font-medium text-[#a5d5ff]">
                {isBusy && !['initiation_recovery_needed', 'reconciliation_action_required'].includes(phase) && <LoaderCircle size={14} className="animate-spin" />} {statusText}{phaseDetail ? `: ${phaseDetail}` : ''}
              </div>
              {isHashingFullFile && checksumProgress?.totalBytes > 0 && (
                <p className="mt-1 text-[11px] text-zinc-500">Local full-file checks: {formatBytes(checksumProgress.completedBytes)} / {formatBytes(checksumProgress.totalBytes)}. Larger files are fully verified by the server after completion.</p>
              )}
              {isUploading && uploadProgress && (
                <div className="mt-2">
                  <div className="mb-1 flex justify-between text-[11px] text-zinc-400"><span>{formatBytes(uploadProgress.loaded_bytes)} / {formatBytes(uploadProgress.total_bytes)}</span><span>{percent(uploadProgress.loaded_bytes, uploadProgress.total_bytes)}%</span></div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-zinc-800"><div className="h-full rounded-full bg-[#0088ff] transition-[width] duration-150" style={{ width: `${percent(uploadProgress.loaded_bytes, uploadProgress.total_bytes)}%` }} /></div>
                </div>
              )}
            </div>
          )}

          <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
            <p className="text-[11px] text-zinc-600">{entries.length ? `${entries.length} file${entries.length === 1 ? '' : 's'} / ${formatBytes(totalSelectedBytes)}` : 'No files selected'}</p>
            <div className="flex items-center gap-2">
              {entries.length > 0 && !isBusy && <button type="button" onClick={clearSelection} className="inline-flex items-center gap-1 rounded-lg px-3 py-2 text-xs font-medium text-zinc-400 hover:text-white"><X size={14} /> Clear</button>}
              {isBusy ? (
                phase === 'initiation_recovery_needed' ? (
                  <>
                    <button type="button" onClick={dismissInitiationRecovery} className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-xs font-semibold text-zinc-200 hover:bg-zinc-800">Dismiss recovery</button>
                    <button type="button" onClick={retryInitiationRecovery} className="rounded-lg border border-amber-800/70 bg-amber-950/30 px-3 py-2 text-xs font-semibold text-amber-100 hover:bg-amber-950/60">Retry recovery</button>
                  </>
                ) : phase === 'reconciliation_action_required' ? (
                  <button type="button" onClick={releaseStalledCompletion} className="rounded-lg border border-amber-800/70 bg-amber-950/30 px-3 py-2 text-xs font-semibold text-amber-100 hover:bg-amber-950/60">Release stalled upload</button>
                ) : (phase === 'completing' || phase === 'reconciling' || phase === 'recovering_initiation' || phase === 'releasing_reconciliation') ? (
                  <span className="text-[11px] text-zinc-500">Completion is being confirmed and cannot be cancelled safely.</span>
                ) : (
                  <button type="button" onClick={cancelUpload} className="rounded-lg border border-red-800/70 bg-red-950/30 px-3 py-2 text-xs font-semibold text-red-200 hover:bg-red-950/60">Cancel upload</button>
                )
              ) : (
                <button type="button" onClick={startUpload} disabled={entries.length === 0 || validationErrors.length > 0} className="inline-flex items-center gap-2 rounded-lg bg-[#0088ff] px-3 py-2 text-xs font-semibold text-white transition hover:bg-[#007cdb] disabled:cursor-not-allowed disabled:opacity-50"><ShieldCheck size={14} /> Start secure upload</button>
              )}
            </div>
          </div>
        </div>
      )}

      {sourceArtifacts.length > 0 && (
        <div className="mt-3 rounded-lg border border-zinc-800 bg-zinc-950/30 px-3 py-2.5 text-xs text-zinc-400">
          <p className="font-medium text-zinc-300">Durable source artifacts</p>
          <ul className="mt-1 space-y-1">
            {sourceArtifacts.map((artifact) => (
              <li key={artifact.id} className="truncate">
                {artifact.metadata?.original_filename || artifact.kind} · {formatBytes(Number(artifact.size_bytes || 0))}
              </li>
            ))}
          </ul>
        </div>
      )}

      {visibleJob?.id && <JobStatusCard jobId={visibleJob.id} initialJob={visibleJob} userId={userId} onTerminal={onTerminal} />}
    </section>
  );
}
