import { useEffect } from 'react';
import { HashRouter as Router, Routes, Route } from 'react-router-dom';
import { QueryClientProvider } from '@tanstack/react-query';
import { Provider as JotaiProvider } from 'jotai';
import { initialize } from '@microsoft/power-apps/app';

import Layout from '@/pages/_layout';
import { queryClient } from '@/lib/query-client';
import { Toaster } from '@/components/ui/sonner';
import ErrorBoundary from '@/components/system/error-boundary';

import HomePage from '@/pages/index';
import NotFoundPage from '@/pages/not-found';
import CasesPage from '@/pages/cases';
import CaseDetailsPage from '@/pages/case-details';
import AgentCenterPage from '@/pages/agent-center';
import EvidencePage from '@/pages/evidence';
import EscalationsPage from '@/pages/escalations';
import EscalationPoliciesPage from '@/pages/escalation-policies';
import SettingsPage from '@/pages/settings';
import AuditPage from '@/pages/audit';
import PoliciesPage from '@/pages/policies';
import PayerRequirementsPage from '@/pages/payer-requirements';
import ApprovalsPage from '@/pages/approvals';
import StaffManagementPage from '@/pages/staff-management';
import CaseAllocationPage from '@/pages/case-allocation';
import SubmissionReviewPage from '@/pages/submission-review';
import { PaProvider } from '@/lib/pa-context';
import LoginPage from '@/pages/login';
import ProtectedRoute from '@/components/protected-route';
import { AuthProvider } from '@/lib/auth-context';

function AuthenticatedLayout() {
  return <PaProvider><Layout /></PaProvider>;
}

function App() {
  useEffect(() => {
    void initialize().catch(() => {
      // The standalone browser preview does not provide a Power Apps host.
    });
  }, []);
  return (
    <QueryClientProvider client={queryClient}>
      <ErrorBoundary resetQueryCache>
        <JotaiProvider>
          <Toaster richColors />
          <Router>
            <AuthProvider>
              <Routes>
                <Route path="/login" element={<LoginPage />} />
                <Route element={<ProtectedRoute />}>
                <Route path="/" element={<AuthenticatedLayout />}>
                  <Route index element={<HomePage />} />
                  <Route path="cases" element={<CasesPage />} />
                  <Route path="cases/:id" element={<CaseDetailsPage />} />
                  <Route path="cases/:id/submission-review" element={<SubmissionReviewPage />} />
                  <Route path="agent-center" element={<AgentCenterPage />} />
                  <Route path="evidence" element={<EvidencePage />} />
                  <Route path="escalation-policies" element={<EscalationPoliciesPage />} />
                  <Route path="escalations" element={<EscalationsPage />} />
                  <Route path="payer-requirements" element={<PayerRequirementsPage />} />
                  <Route path="policies" element={<PoliciesPage />} />
                  <Route path="settings" element={<SettingsPage />} />
                  <Route path="audit" element={<AuditPage />} />
                  <Route path="approvals" element={<ApprovalsPage />} />
                  <Route path="staff-management" element={<StaffManagementPage />} />
                  <Route path="case-allocation" element={<CaseAllocationPage />} />
                  <Route path="*" element={<NotFoundPage />} />
                </Route>
                </Route>
              </Routes>
            </AuthProvider>
          </Router>
        </JotaiProvider>
      </ErrorBoundary>
    </QueryClientProvider>
  );
}

export default App;
