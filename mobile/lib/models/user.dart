/// `invoice.approve` — the permission `POST /api/invoices/{id}/approve` and
/// `/reject` are gated on (`PERM_INVOICE_APPROVE` in
/// `backend/app/api/permissions.py`).
const kPermInvoiceApprove = 'invoice.approve';

class User {
  final String id;
  final String email;
  final String fullName;
  final String organizationId;
  final List<String> roles;

  /// The caller's effective granular permissions, as `GET /api/auth/me`
  /// computes them — the union over every role they hold, system roles via
  /// `ROLE_DEFAULT_PERMISSIONS` and custom roles via their stored list
  /// (`backend/app/api/permissions.py`). Gate a control on [can] wherever its
  /// endpoint is `require_permission(...)`: a role list cannot express a custom
  /// role, and re-deriving the system map here would drift from the server's.
  /// Absent from the payload ⇒ empty ⇒ every such control hidden (fail closed).
  final List<String> permissions;

  User({
    required this.id,
    required this.email,
    required this.fullName,
    required this.organizationId,
    required this.roles,
    this.permissions = const [],
  });

  factory User.fromJson(Map<String, dynamic> json) {
    return User(
      id: json['id'] as String,
      email: json['email'] as String,
      fullName: json['full_name'] as String,
      organizationId: json['organization_id'] as String,
      roles: (json['roles'] as List<dynamic>).cast<String>(),
      permissions:
          (json['permissions'] as List<dynamic>?)?.cast<String>() ?? const [],
    );
  }

  bool hasRole(String role) => roles.contains(role);
  bool can(String permission) => permissions.contains(permission);
  bool get isAdmin => hasRole('admin');
  bool get isManager => hasRole('ap_manager');
  bool get isCfo => hasRole('cfo');
  bool get isClerkOnly =>
      roles.length == 1 && roles.first == 'ap_clerk';
}
