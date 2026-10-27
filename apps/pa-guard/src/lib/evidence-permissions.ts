export function canVerifyEvidenceDocument(role: string | undefined): boolean {
  return role === 'SPECIALIST' || role === 'SUPERVISOR';
}
