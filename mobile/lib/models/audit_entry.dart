/// One row of an invoice's activity timeline (audit log).
///
/// Mirrors the backend `AuditLogEntryResponse` returned by
/// `GET /api/invoices/{id}/audit-log` (operational per-invoice trail, open to
/// any authenticated user). The `details` JSONB is free-form; the field
/// before/after diff for edit / approve-with-corrections events lives under
/// `details.changes` as `{ field: { old, new } }` — surfaced via [changes].
class AuditEntry {
  final String id;
  final String? actorId;
  final String? actorName;
  final String action;
  final String? entityType;
  final String? entityId;
  final Map<String, dynamic>? details;
  final DateTime createdAt;

  AuditEntry({
    required this.id,
    this.actorId,
    this.actorName,
    required this.action,
    this.entityType,
    this.entityId,
    this.details,
    required this.createdAt,
  });

  factory AuditEntry.fromJson(Map<String, dynamic> json) {
    return AuditEntry(
      id: json['id'] as String,
      actorId: json['actor_id'] as String?,
      actorName: json['actor_name'] as String?,
      action: json['action'] as String,
      entityType: json['entity_type'] as String?,
      entityId: json['entity_id'] as String?,
      details: json['details'] as Map<String, dynamic>?,
      createdAt: DateTime.parse(json['created_at'] as String),
    );
  }

  /// Human-readable label for the audit action (falls back to the raw verb).
  String get actionLabel => _actionLabels[action] ?? action;

  /// Per-field before/after pairs from `details.changes`, in stable key order.
  /// Empty for non-edit events. Each value is `(field, old, new)` with `old` /
  /// `new` left dynamic — money arrives as string-Decimal, never a JS number.
  List<AuditFieldChange> get changes {
    final raw = details?['changes'];
    if (raw is! Map) return const [];
    final out = <AuditFieldChange>[];
    raw.forEach((key, value) {
      if (value is Map) {
        out.add(
          AuditFieldChange(
            field: key.toString(),
            oldValue: value['old'],
            newValue: value['new'],
          ),
        );
      }
    });
    return out;
  }

  /// A free-text detail line for non-change events (reject reason, extraction
  /// method/confidence, error), or null when there's nothing extra to show.
  String? get detailNote {
    final d = details;
    if (d == null) return null;
    final reason = d['reason'];
    if (reason is String && reason.isNotEmpty) return reason;
    final error = d['error'];
    if (error is String && error.isNotEmpty) return error;
    return null;
  }
}

class AuditFieldChange {
  final String field;
  final dynamic oldValue;
  final dynamic newValue;

  const AuditFieldChange({
    required this.field,
    required this.oldValue,
    required this.newValue,
  });

  String get oldDisplay => _displayValue(oldValue);
  String get newDisplay => _displayValue(newValue);
}

String _displayValue(dynamic v) {
  if (v == null) return '—';
  final s = v.toString();
  return s.isEmpty ? '—' : s;
}

/// Friendly labels for the audit verbs an invoice timeline surfaces. Mirrors
/// the web `ACTION_LABELS` map in `InvoiceModal.svelte` so both clients read
/// the same history the same way.
const Map<String, String> _actionLabels = {
  'invoice.uploaded': 'Uploaded invoice',
  'invoice.submitted_for_review': 'Submitted for review',
  'invoice.approved': 'Approved',
  'invoice.rejected': 'Rejected',
  'invoice.resubmitted': 'Resubmitted for review',
  'invoice.assigned_for_review': 'Assigned for review',
  'invoice.erp_submitted': 'Sent to ERP',
  'invoice.erp_auto_send_refused': 'Not sent to ERP automatically',
  'invoice.extraction_dispatched': 'Extraction started',
  'invoice.extraction_reset': 'Extraction reset',
  'invoice.extraction_triggered': 'Extraction triggered manually',
  'invoice.extraction_completed': 'Extraction completed',
  'invoice.extraction_failed': 'Extraction failed',
  'invoice.completed': 'Marked complete',
  'invoice.edited': 'Edited fields',
  'invoice.contract_linked': 'Linked to contract',
  'invoice.contract_unlinked': 'Unlinked from contract',
  'audit.viewed': 'Audit trail viewed',
  'audit.exported': 'Audit trail exported',
  'chat_message_posted': 'Posted a chat message',
  'chat_thread_resolved': 'Resolved chat thread',
  'chat_thread_reopened': 'Reopened chat thread',
};
