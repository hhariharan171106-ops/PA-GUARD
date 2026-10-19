import { createContext, useContext, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { isPaApiConfigured, paApi, type SettingsHistoryItem, type SlaRun, type SlaSettings } from '@/lib/pa-api';
import { initialCases, initialDocuments, type AuditEvent, type CaseStatus, type Document, type PaCase } from '@/lib/pa-data';

type NewCase = Pick<PaCase, 'patientReference' | 'payer' | 'service' | 'deadline' | 'coordinator'>;
type PaContextValue = { cases: PaCase[]; documents: Document[]; audit: AuditEvent[]; settings: SlaSettings; settingsHistory: SettingsHistoryItem[]; slaRun: SlaRun | null; isRunningSla: boolean; isLoading: boolean; isOfflineDemo: boolean; apiError: string | null; addCase: (item: NewCase) => Promise<void>; updateCase: (id: string, status: CaseStatus, coordinator: string) => Promise<void>; uploadDocument: (caseId: string, file: File, category: string) => Promise<void>; retryDocumentProcessing: (documentId: number) => Promise<void>; verifyDocument: (id: number) => Promise<void>; escalate: (caseId: string, detail: string) => Promise<void>; setSettings: (settings: SlaSettings) => Promise<void>; runSlaAgent: () => Promise<void> };
const PaContext = createContext<PaContextValue | null>(null);

export function PaProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const casesQuery = useQuery({ queryKey: ['pa-cases'], queryFn: paApi.getCases, enabled: isPaApiConfigured });
  const documentsQuery = useQuery({ queryKey: ['pa-documents'], queryFn: paApi.getDocuments, enabled: isPaApiConfigured });
  const auditQuery = useQuery({ queryKey: ['pa-audit'], queryFn: paApi.getAudit, enabled: isPaApiConfigured });
  const settingsQuery = useQuery({ queryKey: ['pa-settings'], queryFn: paApi.getSettings, enabled: isPaApiConfigured });
  const settingsHistoryQuery = useQuery({ queryKey: ['pa-settings-history'], queryFn: paApi.getSettingsHistory, enabled: isPaApiConfigured });
  const refresh = async () => { await Promise.all([queryClient.invalidateQueries({ queryKey: ['pa-cases'] }), queryClient.invalidateQueries({ queryKey: ['pa-documents'] }), queryClient.invalidateQueries({ queryKey: ['pa-audit'] }), queryClient.invalidateQueries({ queryKey: ['pa-settings'] }), queryClient.invalidateQueries({ queryKey: ['pa-settings-history'] })]); };
  const mutation = useMutation({ mutationFn: async (action: () => Promise<void>) => action(), onSettled: refresh, onError: (error: Error) => toast.error(error.message) });
  const slaMutation = useMutation({ mutationFn: paApi.runSlaAgent, onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ['pa-audit'] }); toast.success('LangGraph SLA recommendations refreshed'); }, onError: (error: Error) => toast.error(error.message) });
  const run = async (action: () => Promise<void>, message: string) => { await mutation.mutateAsync(action); toast.success(message); };
  const errors = [casesQuery.error, documentsQuery.error, auditQuery.error, settingsQuery.error, settingsHistoryQuery.error].filter((error: Error | null) => Boolean(error));
  const apiError = errors[0] instanceof Error ? errors[0].message : null;
  const isOfflineDemo = !isPaApiConfigured || Boolean(casesQuery.error);
  return <PaContext.Provider value={{
    cases: casesQuery.data ?? (isOfflineDemo ? initialCases : []), documents: documentsQuery.data ?? (isOfflineDemo ? initialDocuments : []), audit: auditQuery.data ?? [], settingsHistory: settingsHistoryQuery.data ?? [], settings: settingsQuery.data ?? { warningHours: 24, urgentHours: 8, escalationOneHours: 4, escalationTwoHours: 0, remainingTimeWeight: 60, priorityWeight: 20, evidenceWeight: 20 },
    slaRun: slaMutation.data ?? null, isRunningSla: slaMutation.isPending, isLoading: !isOfflineDemo && (casesQuery.isLoading || documentsQuery.isLoading || auditQuery.isLoading || settingsQuery.isLoading || settingsHistoryQuery.isLoading), isOfflineDemo, apiError,
    addCase: async (item: NewCase) => run(async () => { await paApi.createCase(item); }, 'Case created and saved'),
    updateCase: async (id: string, status: CaseStatus, coordinator: string) => run(async () => { await paApi.updateCase(id, status, coordinator); }, 'Case updated and audited'),
    uploadDocument: async (caseId: string, file: File, category: string) => run(async () => { await paApi.uploadDocument(caseId, file, category); }, 'Clinical document uploaded and OCR processed'),
    retryDocumentProcessing: async (documentId: number) => run(async () => { await paApi.retryDocumentProcessing(documentId); }, 'Document reprocessed for OCR and evidence review'),
    verifyDocument: async (id: number) => run(async () => { await paApi.verifyDocument(id); }, 'Document verified and audited'),
    escalate: async (caseId: string, detail: string) => run(async () => { await paApi.escalate(caseId, detail); }, 'Escalation recorded for follow-up'),
    runSlaAgent: async () => { if (isOfflineDemo) { toast.info('SLA agent requires the FastAPI backend'); return; } await slaMutation.mutateAsync(); },
    setSettings: async (settings: SlaSettings) => run(async () => { await paApi.saveSettings(settings); }, 'SLA settings saved'),
  }}>{children}</PaContext.Provider>;
}
export function usePa() { const value = useContext(PaContext); if (!value) throw new Error('usePa must be used inside PaProvider'); return value; }
