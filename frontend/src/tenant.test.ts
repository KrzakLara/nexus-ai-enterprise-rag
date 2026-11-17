// Regression test for the tenant shown in the breadcrumb / workspace switcher.
// Before the fix the breadcrumb was hardcoded to "Northstar" and the icon to "N", so an Otherco
// user saw "Northstar / ..." although no Northstar data was visible. Run: npm test (node --test).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { tenantDisplayName, tenantShortName } from './tenant.ts';

test('each demo tenant gets its own name', () => {
  assert.equal(tenantDisplayName('northstar'), 'Northstar Inc.');
  assert.equal(tenantDisplayName('otherco'), 'Otherco Ltd.');
});

test('breadcrumb root and icon follow the signed-in tenant', () => {
  assert.equal(tenantShortName('northstar'), 'Northstar');
  assert.equal(tenantShortName('otherco'), 'Otherco');
  assert.equal(tenantDisplayName('otherco').charAt(0), 'O');
  assert.notEqual(tenantShortName('otherco'), 'Northstar');
});

test('an unknown tenant is never shown as another tenant', () => {
  assert.equal(tenantDisplayName('acme'), 'Acme');
  assert.equal(tenantShortName('acme'), 'Acme');
  assert.equal(tenantDisplayName(''), 'Workspace');
  assert.equal(tenantDisplayName(undefined), 'Workspace');
});
