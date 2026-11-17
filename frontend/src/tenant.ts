/** Display names of the synthetic demo tenants (tenant id -> organisation name). */
const TENANT_NAMES: Record<string, string> = {
  northstar: 'Northstar Inc.',
  otherco: 'Otherco Ltd.',
};

/** Organisation name of the signed-in user's tenant; unknown ids are shown as-is, never as another tenant. */
export function tenantDisplayName(tenant: string | null | undefined): string {
  const id = (tenant ?? '').trim();
  if (!id) return 'Workspace';
  return TENANT_NAMES[id.toLowerCase()] ?? id.charAt(0).toUpperCase() + id.slice(1);
}

/** Short label for the breadcrumb root, e.g. "Otherco" for "Otherco Ltd.". */
export function tenantShortName(tenant: string | null | undefined): string {
  return tenantDisplayName(tenant).split(' ')[0];
}
