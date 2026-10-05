import 'dart:typed_data';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/models/adaptive.dart';
import 'package:feohledger_mobile/models/admin_user.dart';
import 'package:feohledger_mobile/models/audit_entry.dart';
import 'package:feohledger_mobile/models/cash_flow.dart';
import 'package:feohledger_mobile/models/contract.dart';
import 'package:feohledger_mobile/models/exception.dart';
import 'package:feohledger_mobile/models/goods_receipt.dart';
import 'package:feohledger_mobile/models/inspection.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/models/notification.dart';
import 'package:feohledger_mobile/models/organization.dart';
import 'package:feohledger_mobile/models/payment.dart';
import 'package:feohledger_mobile/models/payment_queue.dart';
import 'package:feohledger_mobile/models/user.dart';
import 'package:feohledger_mobile/models/vendor.dart';
import 'package:feohledger_mobile/models/workflow.dart';

class AuthApi {
  static final _api = ApiClient();

  /// `POST /api/auth/login`. Returns the raw response, which is EITHER a
  /// `TokenResponse` (`{access_token, ...}`) on a clean login OR an
  /// `MFAChallengeResponse` (`{mfa_required: true, mfa_challenge_token, ...}`)
  /// when a second factor is required. The caller (`AuthStore.login`) branches
  /// on `mfa_required` — see `MFAChallenge.isChallenge`.
  static Future<Map<String, dynamic>> login(
    String email,
    String password,
  ) async {
    return _api.post('/auth/login', {
      'email': email,
      'password': password,
    });
  }

  /// `POST /api/auth/mfa/verify` — trade the login-issued challenge token + a
  /// valid code for a real access token. [method] is `totp` or `email`.
  /// Returns the `{access_token, ...}` JWT on success; throws an [ApiException]
  /// (401 on a bad/expired code) otherwise.
  static Future<String> verifyMfa({
    required String challengeToken,
    required String code,
    required String method,
  }) async {
    final data = await _api.post('/auth/mfa/verify', {
      'challenge_token': challengeToken,
      'code': code,
      'method': method,
    });
    return data['access_token'] as String;
  }

  /// `POST /api/auth/mfa/challenge/email` — ask the backend to generate + email
  /// a one-time code (the email-OTP backup factor). The challenge token proves
  /// the password was already accepted, so codes aren't emailed to randoms.
  /// 204 on success.
  static Future<void> requestEmailOtp(String challengeToken) async {
    await _api.post('/auth/mfa/challenge/email', {
      'challenge_token': challengeToken,
    });
  }

  static Future<User> me() async {
    final data = await _api.get('/auth/me');
    return User.fromJson(data);
  }

  static Future<void> logout() async {
    try {
      await _api.post('/auth/logout');
    } catch (_) {
      // Ignore — clear local session regardless
    }
    await _api.clearSession();
  }
}

class InvoiceApi {
  static final _api = ApiClient();

  static Future<List<Invoice>> list({
    String? status,
    String? search,
    String? vendor,
    String? poNumber,
    double? amountMin,
    double? amountMax,
    DateTime? dueDateFrom,
    DateTime? dueDateTo,
    int page = 1,
    int pageSize = 20,
  }) async {
    final params = <String, String>{
      'page': page.toString(),
      // `page_size` — the name `app/api/pagination.py::pagination_params`
      // declares. FastAPI silently drops an unknown `per_page`, so the old
      // spelling meant every one of these lists was served at the server's
      // default size no matter what the caller asked for.
      'page_size': pageSize.toString(),
    };
    if (status != null) params['status'] = status;
    if (search != null) params['search'] = search;
    // Advanced-search filters — backend query-param names in
    // `backend/app/api/invoices.py::list_invoices`.
    if (vendor != null && vendor.isNotEmpty) params['vendor'] = vendor;
    if (poNumber != null && poNumber.isNotEmpty) {
      params['po_number'] = poNumber;
    }
    if (amountMin != null) params['amount_min'] = amountMin.toString();
    if (amountMax != null) params['amount_max'] = amountMax.toString();
    if (dueDateFrom != null) {
      params['due_date_from'] = _isoDate(dueDateFrom);
    }
    if (dueDateTo != null) params['due_date_to'] = _isoDate(dueDateTo);

    final items = await _api.getList('/invoices', params);
    return items
        .map((e) => Invoice.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// `YYYY-MM-DD` — the backend parses these into `date` (FastAPI `date`
  /// query params).
  static String _isoDate(DateTime d) =>
      '${d.year.toString().padLeft(4, '0')}-'
      '${d.month.toString().padLeft(2, '0')}-'
      '${d.day.toString().padLeft(2, '0')}';

  static Future<Invoice> getById(String id) async {
    final data = await _api.get('/invoices/$id');
    return Invoice.fromJson(data);
  }

  static Future<Invoice> approve(String id) async {
    final data = await _api.post('/invoices/$id/approve');
    return Invoice.fromJson(data);
  }

  static Future<Invoice> reject(String id, String reason) async {
    final data = await _api.post('/invoices/$id/reject', {
      'reason': reason,
    });
    return Invoice.fromJson(data);
  }

  /// Edit invoice fields via `PATCH /api/invoices/{id}` (admin/ap_manager/cfo;
  /// 409 if the invoice is in an immutable status). [changes] is the partial
  /// body — only the keys present are updated. Money fields MUST be passed as
  /// string-Decimal (never a lossy float); the backend's Pydantic `Decimal`
  /// parses the string exactly. The backend maps `vendor` → `vendor_name`.
  static Future<Invoice> update(String id, Map<String, dynamic> changes) async {
    final data = await _api.patch('/invoices/$id', changes);
    return Invoice.fromJson(data);
  }

  /// Per-invoice activity timeline (audit log) via
  /// `GET /api/invoices/{id}/audit-log` — the operational trail, open to any
  /// authenticated user. Returns a bare JSON array, oldest-first.
  static Future<List<AuditEntry>> auditLog(String id) async {
    final items = await _api.getList('/invoices/$id/audit-log');
    return items
        .map((e) => AuditEntry.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// Bulk-delete invoices — `POST /api/invoices/bulk/delete`
  /// (admin / ap_manager / cfo). The backend skips rows in an immutable status
  /// (en route to / posted in ERP, scheduled, paid, done) rather than failing
  /// the batch, returning `{deleted, skipped}` so the UI can report partials.
  static Future<BulkResult> bulkDelete(List<String> ids) async {
    final data = await _api.post('/invoices/bulk/delete', {'ids': ids});
    return BulkResult.fromJson(data, countKey: 'deleted');
  }

  /// Bulk status change — `POST /api/invoices/bulk/status`
  /// (admin / ap_manager / cfo). [status] is the target invoice status value
  /// (e.g. `approved`); immutable rows are skipped. The transition runs through
  /// the normal workflow chokepoint (audited), so it's not a money-moving write.
  ///
  /// [reason] is REQUIRED by the backend when [status] is `rejected` — it 422s
  /// a reasonless bulk rejection, because `review.reject_invoice` records the
  /// reason on the audit row and on the `review_rejected` exception the
  /// supplier corrects from. Omitted from the body when null.
  static Future<BulkResult> bulkStatus(
    List<String> ids,
    String status, {
    String? reason,
  }) async {
    final data = await _api.post('/invoices/bulk/status', {
      'ids': ids,
      'status': status,
      'reason': ?reason,
    });
    return BulkResult.fromJson(data, countKey: 'updated');
  }

  /// Bulk export — `POST /api/invoices/bulk/export` (any authenticated role).
  /// [format] is `csv` or `xml`; the backend streams the rendered file (with a
  /// `Content-Disposition` filename) rather than JSON, so this reads raw bytes.
  /// Returns the bytes plus a filename — the server's suggested name, or a
  /// `invoices-export.<format>` fallback when the header is absent.
  static Future<({Uint8List bytes, String filename})> bulkExport(
    List<String> ids,
    String format,
  ) async {
    final result = await _api.postBytes('/invoices/bulk/export', {
      'ids': ids,
      'format': format,
    });
    return (
      bytes: result.bytes,
      filename: result.filename ?? 'invoices-export.$format',
    );
  }
}

/// Result of a bulk invoice mutation (`{deleted|updated, skipped}`). [count] is
/// the number actually mutated; [skipped] lists ids the backend refused
/// (immutable status). Reused for both bulk-delete and bulk-status.
class BulkResult {
  final int count;
  final List<String> skipped;

  const BulkResult({required this.count, required this.skipped});

  factory BulkResult.fromJson(
    Map<String, dynamic> json, {
    required String countKey,
  }) {
    final skipped = json['skipped'];
    return BulkResult(
      count: (json[countKey] as num?)?.toInt() ?? 0,
      skipped: skipped is List
          ? skipped.map((e) => e.toString()).toList()
          : const [],
    );
  }
}

class ContractApi {
  static final _api = ApiClient();

  static Future<List<Contract>> list({
    String? status,
    String? contractType,
    String? search,
    int page = 1,
    int pageSize = 20,
  }) async {
    final params = <String, String>{
      'page': page.toString(),
      'page_size': pageSize.toString(),
    };
    if (status != null) params['status'] = status;
    if (contractType != null) params['contract_type'] = contractType;
    if (search != null) params['search'] = search;

    final items = await _api.getList('/contracts', params);
    return items
        .map((e) => Contract.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  static Future<Contract> getById(String id) async {
    final data = await _api.get('/contracts/$id');
    return Contract.fromJson(data);
  }

  static Future<Contract> activate(String id) async {
    final data = await _api.post('/contracts/$id/activate');
    return Contract.fromJson(data);
  }

  static Future<Contract> terminate(String id) async {
    final data = await _api.post('/contracts/$id/terminate');
    return Contract.fromJson(data);
  }

  static Future<Contract> cancel(String id) async {
    final data = await _api.post('/contracts/$id/cancel');
    return Contract.fromJson(data);
  }
}

class ExceptionApi {
  static final _api = ApiClient();

  static Future<List<ApException>> list({
    String? status,
    int page = 1,
    int pageSize = 20,
  }) async {
    final params = <String, String>{
      'page': page.toString(),
      // `page_size` — the name `app/api/pagination.py::pagination_params`
      // declares. FastAPI silently drops an unknown `per_page`, so the old
      // spelling meant every one of these lists was served at the server's
      // default size no matter what the caller asked for.
      'page_size': pageSize.toString(),
    };
    if (status != null) params['status'] = status;

    final items = await _api.getList('/exceptions', params);
    return items
        .map((e) => ApException.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// `GET /api/exceptions/{id}` — single-exception detail (+ its invoice), for
  /// the detail screen. Same `_exception_dict` shape as the list rows, with the
  /// detail-only fields (resolved_by/at, due_at, time_to_resolution) populated.
  static Future<ApException> getById(String id) async {
    final data = await _api.get('/exceptions/$id');
    return ApException.fromJson(data);
  }

  /// Resolve / escalate / dismiss an exception. The backend route is
  /// `POST /exceptions/{id}/resolve` with `{action, resolution}` (action is one
  /// of resolve | escalate | dismiss); a missing resolution is rejected.
  static Future<void> act(
    String id, {
    required String action,
    required String resolution,
  }) async {
    await _api.post('/exceptions/$id/resolve', {
      'action': action,
      'resolution': resolution,
    });
  }

  /// `POST /api/exceptions/{id}/assign` with `{user_id}` (null to unassign).
  /// Returns the updated exception (the backend echoes the full
  /// `_exception_dict`, with the new assignee resolved server-side).
  static Future<ApException> assign(String id, {String? userId}) async {
    final data = await _api.post('/exceptions/$id/assign', {'user_id': userId});
    return ApException.fromJson(data);
  }

  /// `POST /api/exceptions/bulk/resolve` with `{ids, action, resolution}`.
  /// Parses the partial-success envelope `{updated, skipped:[{id,reason}]}`.
  static Future<BulkResolveResult> bulkResolve(
    List<String> ids, {
    required String action,
    required String resolution,
  }) async {
    final data = await _api.post('/exceptions/bulk/resolve', {
      'ids': ids,
      'action': action,
      'resolution': resolution,
    });
    return BulkResolveResult.fromJson(data);
  }
}

/// The result of a bulk exception resolve/escalate/dismiss — how many rows were
/// updated plus the per-row skips (already-terminal / not-found), each carrying
/// the offending id + a reason. Mirrors the backend `BulkResolveResponse`.
class BulkResolveResult {
  final int updated;
  final List<({String id, String reason})> skipped;

  const BulkResolveResult({required this.updated, required this.skipped});

  int get skippedCount => skipped.length;

  factory BulkResolveResult.fromJson(Map<String, dynamic> json) {
    final raw = json['skipped'];
    return BulkResolveResult(
      updated: (json['updated'] as num?)?.toInt() ?? 0,
      skipped: raw is List
          ? raw.map((e) {
              final m = e as Map<String, dynamic>;
              return (
                id: m['id']?.toString() ?? '',
                reason: m['reason']?.toString() ?? 'skipped',
              );
            }).toList()
          : const [],
    );
  }
}

/// One page of the notification list plus the user's total unread count.
/// The backend returns `{items, total, unread, page, page_size}`; `unread` is
/// the user's total unread count regardless of the page window, so the same
/// response feeds both the list and the app-bar badge.
class NotificationPage {
  final List<AppNotification> items;
  final int total;
  final int unread;

  NotificationPage({
    required this.items,
    required this.total,
    required this.unread,
  });
}

class NotificationApi {
  static final _api = ApiClient();

  /// `GET /api/notifications` — the current user's notifications, newest first.
  /// `unreadOnly` filters to unread rows; `unread` in the result is always the
  /// total unread count.
  static Future<NotificationPage> list({
    bool unreadOnly = false,
    int page = 1,
    int pageSize = 20,
  }) async {
    final params = <String, String>{
      'page': page.toString(),
      'page_size': pageSize.toString(),
    };
    if (unreadOnly) params['unread_only'] = 'true';

    final data = await _api.get('/notifications', params);
    final items = (data['items'] as List? ?? [])
        .map((e) => AppNotification.fromJson(e as Map<String, dynamic>))
        .toList();
    return NotificationPage(
      items: items,
      total: (data['total'] as num?)?.toInt() ?? items.length,
      unread: (data['unread'] as num?)?.toInt() ?? 0,
    );
  }

  /// `GET /api/notifications/unread-count` — cheap badge count.
  static Future<int> unreadCount() async {
    final data = await _api.get('/notifications/unread-count');
    return (data['unread'] as num?)?.toInt() ?? 0;
  }

  /// `POST /api/notifications/{id}/read` — mark one read (idempotent server-side).
  static Future<void> markRead(String id) async {
    await _api.post('/notifications/$id/read');
  }

  /// `POST /api/notifications/read-all` — mark every unread row read; returns
  /// the number updated.
  static Future<int> markAllRead() async {
    final data = await _api.post('/notifications/read-all');
    return (data['updated'] as num?)?.toInt() ?? 0;
  }

  /// `POST /api/notifications/device-token` — register (or re-register) this
  /// device's push token for [platform] (`ios` or `android`). Registration
  /// only — there is no push-SENDING backend yet; this just gives that a
  /// token to read later. Upsert: replaces any token already stored for the
  /// same platform on this account.
  static Future<void> registerDeviceToken(String token, String platform) async {
    await _api.post('/notifications/device-token', {
      'token': token,
      'platform': platform,
    });
  }
}

class DashboardApi {
  static final _api = ApiClient();

  static Future<DashboardData> get() async {
    final data = await _api.get('/dashboard');
    return DashboardData.fromJson(data);
  }
}

/// Predictive cash-flow forecasting (CFO / admin) — combines the two
/// read-only analytics endpoints into one [CashFlowData]:
///   - `GET /api/analytics/cashflow_forecast` (projected AP outflows per period)
///   - `GET /api/analytics/cash_position` (running balance + low-balance alert)
///
/// Both share `granularity` + `horizon_days`; we fetch them with the same
/// horizon so the forecast periods and the running-balance periods line up.
/// Money arrives as JSON numbers (the backend `float(...)`s the dicts) and is
/// carried straight through as display strings — never summed on the device.
class CashFlowApi {
  static final _api = ApiClient();

  /// Fetch the forecast + cash position for the given [horizonDays].
  /// [granularity] is one of `day` | `week` | `month` (backend default
  /// `week`). The cash-position call lets the backend resolve the opening
  /// balance (provider auto-sync / persisted setting / 0) and the alert
  /// threshold from the org's persisted `cash-position-settings`.
  static Future<CashFlowData> get({
    int horizonDays = 90,
    String granularity = 'week',
  }) async {
    final params = <String, String>{
      'horizon_days': horizonDays.toString(),
      'granularity': granularity,
    };
    final forecast = await _api.get('/analytics/cashflow_forecast', params);
    final position = await _api.get('/analytics/cash_position', params);
    return CashFlowData.fromJson(forecast: forecast, position: position);
  }
}

class PaymentApi {
  static final _api = ApiClient();

  static Future<List<Payment>> list({
    String? status,
    int page = 1,
    int pageSize = 20,
  }) async {
    final params = <String, String>{
      'page': page.toString(),
      // `page_size` — the name `app/api/pagination.py::pagination_params`
      // declares. FastAPI silently drops an unknown `per_page`, so the old
      // spelling meant every one of these lists was served at the server's
      // default size no matter what the caller asked for.
      'page_size': pageSize.toString(),
    };
    if (status != null) params['status'] = status;

    final items = await _api.getList('/payments', params);
    return items
        .map((e) => Payment.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  static Future<PaymentSummary> summary() async {
    final data = await _api.get('/payments/summary');
    return PaymentSummary.fromJson(data);
  }

  /// Approved invoices awaiting payment — `GET /api/payments/queue`.
  /// Response is `{items, total, total_amount, total_savings}`; `getList`
  /// unwraps the `items` array.
  static Future<List<PaymentQueueItem>> queue() async {
    final items = await _api.getList('/payments/queue');
    return items
        .map((e) => PaymentQueueItem.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// Payment runs (batches) — `GET /api/payments/runs/` (the trailing slash
  /// matters; the backend route is declared that way). `{items, total, ...}`.
  static Future<List<PaymentRun>> runs({String? status}) async {
    final params = <String, String>{};
    if (status != null) params['status'] = status;
    final items = await _api.getList('/payments/runs/', params);
    return items
        .map((e) => PaymentRun.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// Create a draft payment run from selected invoices + per-row method.
  /// `POST /api/payments/runs`. Returns the raw `{id, status, ...}` payload
  /// (includes `requires_cfo_approval` + a human message).
  static Future<Map<String, dynamic>> createRun(
    List<PaymentRunSelection> selections,
  ) async {
    return _api.post('/payments/runs', {
      'items': selections.map((s) => s.toJson()).toList(),
    });
  }

  /// CFO sign-off on a draft run that trips the org's approval threshold —
  /// `POST /api/payments/runs/{id}/approve`. Backend gate is
  /// `require_roles(ROLE_CFO)` (deliberately NOT the shared
  /// `payment_run.approve` permission, which admin/ap_manager also hold — that
  /// would let the same person who drafted the run sign it off). Refuses 409
  /// outside `draft` / when sign-off isn't required / when already approved,
  /// and 403 on the maker-checker check (the run's creator can't approve it).
  /// Returns `{id, status, cfo_approved_by, cfo_approved_at, message}`.
  /// Authorizes execution; it does not move money.
  static Future<Map<String, dynamic>> approveRun(String id) async {
    return _api.post('/payments/runs/$id/approve');
  }

  /// Execute a draft run via the configured payment adapter.
  /// `POST /api/payments/runs/{id}/execute`.
  static Future<Map<String, dynamic>> executeRun(String id) async {
    return _api.post('/payments/runs/$id/execute');
  }

  /// Cancel a draft run (releases the invoices back to the queue).
  /// `POST /api/payments/runs/{id}/cancel`.
  static Future<Map<String, dynamic>> cancelRun(String id) async {
    return _api.post('/payments/runs/$id/cancel');
  }
}

/// Admin user-management surface (`/api/admin/*`, admin-only on the backend).
/// Control-plane data; no tenant header needed beyond the usual one.
class AdminApi {
  static final _api = ApiClient();

  /// `GET /api/admin/users` — org users, newest first. `search` filters by
  /// name/email. The envelope is `{items, total, page, page_size}`; `getList`
  /// unwraps `items`.
  static Future<List<AdminUser>> listUsers({
    String? search,
    int page = 1,
    int pageSize = 50,
  }) async {
    final params = <String, String>{
      'page': page.toString(),
      'page_size': pageSize.toString(),
    };
    if (search != null && search.isNotEmpty) params['search'] = search;
    final items = await _api.getList('/admin/users', params);
    return items
        .map((e) => AdminUser.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// `GET /api/admin/roles` — system + this org's custom roles.
  static Future<List<AdminRole>> listRoles() async {
    final items = await _api.getList('/admin/roles');
    return items
        .map((e) => AdminRole.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// `PATCH /api/admin/users/{id}` with the partial body. Returns the updated
  /// user. The backend force-logs-out the target on a role change or
  /// deactivation (the prior JWT was signed before the change).
  static Future<AdminUser> updateUser(
    String id,
    Map<String, dynamic> changes,
  ) async {
    final data = await _api.patch('/admin/users/$id', changes);
    return AdminUser.fromJson(data);
  }

  /// Replace the user's full set of role names (`PATCH .../users/{id}`).
  static Future<AdminUser> setRoles(String id, List<String> roleNames) =>
      updateUser(id, {'role_names': roleNames});

  /// Activate / deactivate the user (`PATCH .../users/{id}`).
  static Future<AdminUser> setActive(String id, bool active) =>
      updateUser(id, {'is_active': active});

  /// `POST /api/admin/users` — create a user. The backend generates a
  /// temporary password (the user is forced to change it on first login) and
  /// returns it EXACTLY once in the response, so the result carries it for the
  /// admin to hand over. 409 if the email is already in use.
  static Future<CreateUserResult> createUser({
    required String email,
    required String fullName,
    required List<String> roleNames,
  }) async {
    final data = await _api.post('/admin/users', {
      'email': email,
      'full_name': fullName,
      'role_names': roleNames,
    });
    return CreateUserResult.fromJson(data);
  }

  /// `DELETE /api/admin/users/{id}` — delete a user. 204 on success; the
  /// backend 409s on self-delete or when the user is still referenced by
  /// in-flight work (open assignments / pending approvals / active-workflow
  /// approver), surfacing the reason in the response body.
  static Future<void> deleteUser(String id) => _api.delete('/admin/users/$id');
}

/// Organization settings (`/api/organization`). GET is readable by any authed
/// user; the PATCH that edits settings is admin-only on the backend.
class OrganizationApi {
  static final _api = ApiClient();

  /// `GET /api/organization` — the org + the settings its caller's role may
  /// read. The backend projects the JSONB by role
  /// (`backend/app/services/org_settings_view.py`): a non-admin gets an
  /// allow-list that keeps `company` and `invoice_defaults` — the two blocks
  /// this screen renders — and drops the tenant's third-party credentials,
  /// which every role could read before. We narrow further to the safe
  /// editable subset here.
  static Future<OrgSettings> get() async {
    final data = await _api.get('/organization');
    return OrgSettings.fromJson(data);
  }

  /// `PATCH /api/organization` with `{name, settings: {company, invoice_defaults}}`.
  /// The backend shallow-merges the settings keys, so untouched keys (erp,
  /// cards, payments, sso …) are preserved. Returns the refreshed settings.
  static Future<OrgSettings> update(OrgSettingsUpdate body) async {
    final data = await _api.patch('/organization', body.toJson());
    return OrgSettings.fromJson(data);
  }
}

class VendorApi {
  static final _api = ApiClient();

  static Future<List<Vendor>> list({
    String? status,
    String? search,
    int page = 1,
    int pageSize = 20,
  }) async {
    final params = <String, String>{
      'page': page.toString(),
      // `page_size` — the name `app/api/pagination.py::pagination_params`
      // declares. FastAPI silently drops an unknown `per_page`, so the old
      // spelling meant every one of these lists was served at the server's
      // default size no matter what the caller asked for.
      'page_size': pageSize.toString(),
    };
    if (status != null) params['status'] = status;
    if (search != null) params['search'] = search;

    final items = await _api.getList('/vendors', params);
    return items
        .map((e) => Vendor.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// Verify an unverified vendor (admin / ap_manager). 409 if not unverified.
  static Future<Vendor> verify(String id) async {
    final data = await _api.post('/vendors/$id/verify');
    return Vendor.fromJson(data);
  }

  /// Reject an unverified vendor (admin / ap_manager).
  static Future<Vendor> reject(String id) async {
    final data = await _api.post('/vendors/$id/reject');
    return Vendor.fromJson(data);
  }

  /// Pull vendors from the connected ERP (admin / ap_manager). Returns the
  /// `{success, message, created, updated, unchanged}` summary; 400 when no
  /// ERP is configured.
  static Future<Map<String, dynamic>> syncErp() async {
    return _api.post('/vendors/sync-erp');
  }
}

class WorkflowApi {
  static final _api = ApiClient();

  /// List workflow definitions — `GET /api/workflows` (any authenticated role;
  /// the mobile surface is admin-gated in the UI to mirror the web nav). The
  /// backend auto-creates a default definition if the org has none, so the list
  /// is never empty. Read-only — mobile never creates/edits definitions.
  static Future<List<WorkflowDefinition>> list() async {
    final items = await _api.getList('/workflows');
    return items
        .map((e) => WorkflowDefinition.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// Fetch one workflow definition — `GET /api/workflows/{id}`. 404 cross-org.
  static Future<WorkflowDefinition> getById(String id) async {
    final data = await _api.get('/workflows/$id');
    return WorkflowDefinition.fromJson(data);
  }
}

/// Quality inspections — the 4th leg of 4-way matching (`/api/inspections`).
///
/// RBAC mirrors `backend/app/api/inspections.py`:
///   * `GET /inspections` + `GET /inspections/{id}` — any authenticated user
///     (`get_current_user`). The list is entity-scoped; the detail is not.
///   * `POST /inspections` — admin / ap_manager (`require_roles`). The screen
///     gates the record affordance on the same split; the backend is
///     authoritative regardless.
///
/// `POST /inspections/sync` (pull from the org's QMS) is deliberately NOT
/// exposed on mobile: it is an operator action against org-level configuration
/// that 409s unless `settings.qms` is set, and the place to configure it — and
/// therefore to run it — is the web `/goods-receipts` Inspections tab.
class InspectionApi {
  static final _api = ApiClient();

  /// One page of inspections, newest first.
  ///
  /// [result] and [grId] are **server-side** filters (`?result=` / `?gr_id=`).
  /// Filtering a page on the device would hide every matching row past the page
  /// boundary — the defect the approvals tab already shipped once.
  static Future<List<Inspection>> list({
    String? result,
    String? grId,
    int page = 1,
    int pageSize = 20,
  }) async {
    final params = <String, String>{
      'page': page.toString(),
      'page_size': pageSize.toString(),
    };
    if (result != null && result.isNotEmpty) params['result'] = result;
    if (grId != null && grId.isNotEmpty) params['gr_id'] = grId;

    final items = await _api.getList('/inspections', params);
    return items
        .map((e) => Inspection.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// `GET /api/inspections/{id}` — 404 when it isn't this tenant's.
  static Future<Inspection> getById(String id) async {
    final data = await _api.get('/inspections/$id');
    return Inspection.fromJson(data);
  }

  /// `POST /api/inspections` → 201 with the created row (`gr_number` included,
  /// so the list can render it without a re-read). Admin / ap_manager only.
  static Future<Inspection> create(InspectionDraft draft) async {
    final data = await _api.post('/inspections', draft.toJson());
    return Inspection.fromJson(data);
  }
}

/// Goods receipts — `GET /api/goods-receipts` (auth-gated, role-open).
///
/// Mobile reads them only to populate the record-inspection picker: an
/// inspection has to name the delivery it covers.
class GoodsReceiptApi {
  static final _api = ApiClient();

  /// One page of receipts plus the whole-set `total`.
  ///
  /// The envelope's `total` is read rather than discarded (so this goes through
  /// `get`, not `getList`): the picker shows one page and the form has to be
  /// able to say when there are more receipts than it is offering, instead of
  /// implying the list is everything.
  static Future<({List<GoodsReceipt> items, int total})> list({
    int page = 1,
    int pageSize = 20,
  }) async {
    final data = await _api.get('/goods-receipts', {
      'page': page.toString(),
      'page_size': pageSize.toString(),
    });
    final items = ((data['items'] as List?) ?? const [])
        .map((e) => GoodsReceipt.fromJson(e as Map<String, dynamic>))
        .toList();
    return (items: items, total: (data['total'] as num?)?.toInt() ?? items.length);
  }
}

/// Adaptive AI workflows — `/api/adaptive` (admin / ap_manager / cfo read,
/// mirroring the backend `_READ_ROLES`).
///
/// **Read-first by design.** The three read models plus the one safe write
/// (dismiss a suggestion, `_WRITE_ROLES` = admin / ap_manager) are here. The two
/// APPLY paths — `routing-suggestion/apply` and `threshold-recommendation/apply`
/// — are deliberately absent: they reassign a live approval and raise the
/// org-wide auto-approve threshold respectively, which is a money-path control
/// surface, and the threshold apply also carries a stale-value optimistic guard
/// whose 409 needs a real "the recommendation changed, nothing was applied"
/// state to land safely. Those stay on the web `/adaptive` page.
class AdaptiveApi {
  static final _api = ApiClient();

  /// `GET /api/adaptive/approval-patterns` — deterministic statistics over the
  /// tenant's own approval history. [days] is the lookback (backend default
  /// 180, bounded 1-730).
  static Future<ApprovalPatterns> approvalPatterns({int days = 180}) async {
    final data = await _api.get('/adaptive/approval-patterns', {
      'days': days.toString(),
    });
    return ApprovalPatterns.fromJson(data);
  }

  /// `GET /api/adaptive/anomalies` (batch) — in-review invoices sitting outside
  /// their own vendor's baseline. Read-only: it raises nothing and blocks
  /// nothing.
  static Future<AnomalyBatch> anomalies() async {
    final data = await _api.get('/adaptive/anomalies');
    return AnomalyBatch.fromJson(data);
  }

  /// `GET /api/adaptive/suggestions` — advisory suggestions. [status] is one of
  /// `open` / `dismissed` / `applied` / `stale` / `all` (backend default
  /// `open`).
  ///
  /// The endpoint performs an idempotent upsert on read (it recomputes the
  /// derived set and re-opens / stales rows), which is why a dismissal is
  /// durable across recomputation. It moves no money and is not an auditable
  /// status change.
  static Future<List<WorkflowSuggestion>> suggestions({
    String status = 'open',
  }) async {
    final items = await _api.getList('/adaptive/suggestions', {
      'status': status,
    });
    return items
        .map((e) => WorkflowSuggestion.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// `POST /api/adaptive/suggestions/{id}/dismiss` — idempotent; dismissing an
  /// already-dismissed row is a no-op. Returns the single dismissed row (the
  /// endpoint answers with a `SuggestionListResponse` carrying just it).
  static Future<WorkflowSuggestion?> dismissSuggestion(String id) async {
    final data = await _api.post('/adaptive/suggestions/$id/dismiss');
    final rows = (data['suggestions'] as List?) ?? const [];
    if (rows.isEmpty) return null;
    return WorkflowSuggestion.fromJson(rows.first as Map<String, dynamic>);
  }
}
