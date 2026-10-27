import { createContext, useContext, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { ApiUnavailableError, isPaApiConfigured, paApi, type SettingsHistoryItem, type SlaRun, type SlaSettings } from '@/lib/pa-api';
import { initialCases, initialDocuments, type AuditEvent, type Document, type PaCase } from '@/lib/pa-data';
import { useAuth } from '@/lib/auth-context';
import { formatDiagnosticForUser } from '@/lib/user-messages';

type NewCase = Pick<PaCase, 'patientReference' | 'payer' | 'service' | 'deadline' | 'coordinator'>;
type PaContextValue = { cases: PaCase[]; documents: Document[]; audit: AuditEvent[]; settings: SlaSettings; settingsHistory: SettingsHistoryItem[]; slaRun: SlaRun | null; isRunningSla: boolean; isLoading: boolean; isOfflineDemo: boolean; apiError: string | null; casesError: string | null; addCase: (item: NewCase) => Promise<void>; updateCase: (id: string, coordinator: string, expectedCoordinator: string) => Promise<void>; uploadDocument: (caseId: string, file: File, category: string, submissionId?: string) => Promise<Document>; uploadDocuments: (caseId: string, files: { file: File; category: string; submissionId: string }[]) => Promise<{ documents: Document[]; savedSubmissionIds: string[]; failures: { filename: string; submission_id: string; status_code: number; message: string }[] }>; retryDocumentProcessing: (documentId: number) => Promise<void>; verifyDocument: (id: number) => Promise<void>; escalate: (caseId: string, reason: string, explanation: string) => Promise<void>; setSettings: (settings: SlaSettings) => Promise<void>; runSlaAgent: () => Promise<void> };
const PaContext = createContext<PaContextValue | null>(null);

export function PaProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const identityKey = [user?.id ?? 'anonymous', user?.role ?? 'anonymous'];
  const casesQuery = useQuery({ queryKey: ['pa-cases', ...identityKey], queryFn: paApi.getCases, enabled: isPaApiConfigured && Boolean(user) });
  const documentsQuery = useQuery({ queryKey: ['pa-documents', ...identityKey], queryFn: paApi.getDocuments, enabled: isPaApiConfigured && Boolean(user) });
  const auditQuery = useQuery({ queryKey: ['pa-audit', ...identityKey], queryFn: paApi.getAudit, enabled: isPaApiConfigured && Boolean(user) });
  const settingsQuery = useQuery({ queryKey: ['pa-settings', ...identityKey], queryFn: paApi.getSettings, enabled: isPaApiConfigured && Boolean(user) });
  const settingsHistoryQuery = useQuery({ queryKey: ['pa-settings-history', ...identityKey], queryFn: paApi.getSettingsHistory, enabled: isPaApiConfigured && Boolean(user) });
  const refresh = async () => { await Promise.all([queryClient.invalidateQueries({ queryKey: ['pa-cases'] }), queryClient.invalidateQueries({ queryKey: ['pa-documents'] }), queryClient.invalidateQueries({ queryKey: ['pa-audit'] }), queryClient.invalidateQueries({ queryKey: ['pa-settings'] }), queryClient.invalidateQueries({ queryKey: ['pa-settings-history'] })]); };
  const mutation = useMutation({ mutationFn: async (action: () => Promise<void>) => action(), onSettled: refresh, onError: (error: Error) => toast.error(error.message) });
  const slaMutation = useMutation({ mutationFn: paApi.runSlaAgent, onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ['pa-audit'] }); toast.success('Deadline recommendations updated. Review each case before taking action.'); }, onError: (error: Error) => toast.error(error.message) });
  const run = async (action: () => Promise<void>, message: string) => { await mutation.mutateAsync(action); toast.success(message); };
  const errors = [casesQuery.error, documentsQuery.error, auditQuery.error, settingsQuery.error, settingsHistoryQuery.error].filter((error: Error | null) => Boolean(error));
  const apiError = errors[0] instanceof Error ? errors[0].message : null;
  const casesError = casesQuery.error instanceof Error ? casesQuery.error.message : null;
  const isOfflineDemo = !isPaApiConfigured || casesQuery.error instanceof ApiUnavailableError;
  return <PaContext.Provider value={{
    cases: casesQuery.data ?? (isOfflineDemo ? initialCases : []), documents: documentsQuery.data ?? (isOfflineDemo ? initialDocuments : []), audit: auditQuery.data ?? [], settingsHistory: settingsHistoryQuery.data ?? [], settings: settingsQuery.data ?? { warningHours: 24, urgentHours: 8, escalationOneHours: 4, escalationTwoHours: 0, remainingTimeWeight: 60, priorityWeight: 20, evidenceWeight: 20 },
    slaRun: slaMutation.data ?? null, isRunningSla: slaMutation.isPending, isLoading: !isOfflineDemo && (casesQuery.isLoading || documentsQuery.isLoading || auditQuery.isLoading || settingsQuery.isLoading || settingsHistoryQuery.isLoading), isOfflineDemo, apiError, casesError,
    addCase: async (item: NewCase) => run(async () => { await paApi.createCase(item); }, 'Case created and saved'),
    updateCase: async (id: string, coordinator: string, expectedCoordinator: string) => run(async () => { await paApi.updateCase(id, coordinator, expectedCoordinator); }, 'Coordinator updated and audited'),
    uploadDocument: async (caseId: string, file: File, category: string, submissionId?: string) => {
      const document = await paApi.uploadDocument(caseId, file, category, submissionId);
      await refresh();
      if (document.ocrStatus === 'FAILED') {
        toast.error(`Document saved, but its text could not be read. Review the file and try again. ${formatDiagnosticForUser(document.processingError, '')}`.trim());
      } else {
        toast.success('Document uploaded and ready for review.');
      }
      return document;
    },
    uploadDocuments: async (caseId: string, files: { file: File; category: string; submissionId: string }[]) => {
      const result = await paApi.uploadDocuments(caseId, files);
      try {
        await refresh();
      } catch (error) {
        const detail = formatDiagnosticForUser(error instanceof Error ? error.message : undefined, 'Try refreshing the case page.');
        toast.error(`Upload response received, but the case views could not be refreshed: ${detail}`);
      }
      if (result.failures.length) {
        toast.error(`${result.documents.length} document${result.documents.length === 1 ? '' : 's'} saved. ${result.failures.length} could not be uploaded. Review the failed files and try again.`);
      } else {
        toast.success(`${result.documents.length} document${result.documents.length === 1 ? '' : 's'} uploaded and ready for review.`);
      }
      return result;
    },
    retryDocumentProcessing: async (documentId: number) => run(async () => { await paApi.retryDocumentProcessing(documentId); }, 'Text extraction was retried. Review the document results.'),
    verifyDocument: async (id: number) => run(async () => { await paApi.verifyDocument(id); }, 'Document verified and audited'),
    escalate: async (caseId: string, reason: string, explanation: string) => { await paApi.escalate(caseId, reason, explanation); await refresh(); toast.success(`Case ${caseId} sent to the next reviewer. Check the case page for its updated status.`); },
    runSlaAgent: async () => { if (isOfflineDemo) { toast.info('Connect to PA Guard to review case deadlines.'); return; } await slaMutation.mutateAsync(); },
    setSettings: async (settings: SlaSettings) => run(async () => { await paApi.saveSettings(settings); }, 'SLA settings saved'),
  }}>{children}</PaContext.Provider>;
}
export function usePa() { const value = useContext(PaContext); if (!value) throw new Error('usePa must be used inside PaProvider'); return value; }
