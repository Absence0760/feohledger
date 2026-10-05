/// The effective permissions `GET /api/auth/me` returns for a user holding
/// only system roles — `ROLE_DEFAULT_PERMISSIONS` in
/// `backend/app/api/permissions.py`, unioned over [roles].
///
/// For test fixtures only: the app never derives permissions from roles, it
/// reads the server's list (`User.permissions`), which is the only place a
/// custom role's grants appear. Fixtures that fake `/auth/me` use this so the
/// payload looks like the real one.
List<String> systemRolePermissions(List<String> roles) {
  const byRole = <String, Set<String>>{
    'admin': {
      'invoice.approve',
      'payment_run.approve',
      'payment.execute',
      'payment.void',
      'vendor.bank_change.approve',
      'vendor.block',
      'vendor.manage',
      'user.manage',
    },
    'ap_manager': {
      'invoice.approve',
      'payment_run.approve',
      'payment.execute',
      'vendor.bank_change.approve',
      'vendor.block',
      'vendor.manage',
    },
    'cfo': {
      'invoice.approve',
      'payment_run.approve',
      'payment.execute',
      'payment.void',
    },
    'ap_clerk': {},
  };
  return {for (final r in roles) ...?byRole[r]}.toList()..sort();
}
