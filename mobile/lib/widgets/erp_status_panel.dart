import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/audit_entry.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/utils/dates.dart';
import 'package:feohledger_mobile/widgets/external_link.dart';

/// ERP-sync facts derived from an invoice's audit log, mirroring the web
/// modal's `erpInfo`: the latest `invoice.erp_*` / `invoice.completed` entry
/// carries the ERP reference / document id and any send error. Pure value
/// object — no network.
class ErpInfo {
  final String? erpReference;
  final String? erpDocumentId;
  final String? lastError;
  final String actionLabel;
  final String? actor;
  final DateTime time;

  const ErpInfo({
    this.erpReference,
    this.erpDocumentId,
    this.lastError,
    required this.actionLabel,
    this.actor,
    required this.time,
  });

  /// Build the ERP summary from an invoice's audit entries (oldest-first), or
  /// null when no ERP action has happened yet. Matches the web derivation:
  /// the most recent entry whose action starts with `invoice.erp_` or
  /// `invoice.completed` wins.
  static ErpInfo? fromAuditLog(List<AuditEntry> entries) {
    AuditEntry? latest;
    for (final e in entries) {
      if (e.action.startsWith('invoice.erp_') ||
          e.action.startsWith('invoice.completed')) {
        latest = e; // entries are oldest-first, so the last match is newest
      }
    }
    if (latest == null) return null;
    final details = latest.details ?? const {};
    return ErpInfo(
      erpReference: details['erp_reference'] as String?,
      erpDocumentId: details['erp_document_id'] as String?,
      lastError: details['error'] as String?,
      actionLabel: latest.actionLabel,
      actor: latest.actorName,
      time: latest.createdAt,
    );
  }
}

/// Detail-screen panel showing the invoice's ERP integration status — the
/// latest ERP action, reference / document id, and any send error. Shown when
/// the invoice is en route to / posted in the ERP (or failed with ERP context).
class ErpStatusPanel extends StatelessWidget {
  final Invoice invoice;
  final ErpInfo? erpInfo;

  /// Injected by tests; the reference link otherwise opens externally.
  final UrlOpener? opener;

  const ErpStatusPanel({
    super.key,
    required this.invoice,
    required this.erpInfo,
    this.opener,
  });

  static const _erpStatuses = {
    InvoiceStatus.sendingToErp,
    InvoiceStatus.sentToErp,
    InvoiceStatus.postedInErp,
  };

  /// The bill of record, from the invoice row. The audit trail cannot supply
  /// it: after a confirmed push the newest ERP entry is `invoice.completed`,
  /// which carries no reference, so a posted invoice showed none.
  String? get _reference {
    final id = invoice.erpDocumentId;
    return id != null && id.isNotEmpty ? id : null;
  }

  /// Whether to render at all — an ERP-bound status, an invoice that names its
  /// bill (it keeps it at `done`, and after a void), or a failure that carries
  /// ERP context (mirrors the web modal's condition).
  bool get visible =>
      _erpStatuses.contains(invoice.status) ||
      _reference != null ||
      (invoice.status == InvoiceStatus.failed && erpInfo != null);

  @override
  Widget build(BuildContext context) {
    if (!visible) return const SizedBox.shrink();
    final l = AppLocalizations.of(context);
    final info = erpInfo;

    final reference = _reference;
    // The bill's own page in the ERP, when the backend built one (decisions
    // §302, §354). A linked reference is its own semantics node so it stays
    // actionable, and the nodes around it keep the screen's order.
    final linked =
        reference != null && ExternalLink.safeUri(invoice.erpDocumentUrl) != null;
    // Each detail row, with what a screen reader announces for it. The two
    // are built together so the announcement cannot drop a row the screen
    // shows (the last update used to be visible and never read).
    final rows = <Widget>[];
    final announced = <String>[];
    void addRow(String label, String value, {bool error = false}) {
      rows.add(_kv(label, _text(value, error: error)));
      announced.add('$label $value');
    }

    final documentId = info?.erpDocumentId;
    if (documentId != null && documentId.isNotEmpty) {
      addRow(l.erpStatusDocumentId, documentId);
    }
    final lastError = info?.lastError;
    if (lastError != null && lastError.isNotEmpty) {
      addRow(l.erpStatusError, lastError, error: true);
    }
    if (info != null) {
      final actor = info.actor;
      final action = actor != null
          ? l.erpStatusActionBy(info.actionLabel, actor)
          : info.actionLabel;
      addRow(l.erpStatusLastUpdate, '$action · ${formatDateTime(info.time)}');
    }
    if (rows.isEmpty && reference == null) {
      // ERP-bound status but no audit detail yet (e.g. sending in flight).
      addRow(l.erpStatusStatus, invoice.status.label);
    }

    final header = Row(
      children: [
        Icon(Icons.sync_alt, size: 18, color: Colors.indigo.shade700),
        const SizedBox(width: 8),
        Text(
          l.erpStatusTitle,
          style: TextStyle(
            color: Colors.indigo.shade700,
            fontSize: 14,
            fontWeight: FontWeight.w700,
          ),
        ),
      ],
    );

    final Widget? referenceRow = reference == null
        ? null
        : _kv(
            l.erpStatusReference,
            center: linked,
            linked
                ? ExternalLink(
                    key: const ValueKey('erp-reference-link'),
                    text: reference,
                    url: invoice.erpDocumentUrl,
                    hint: l.erpStatusOpensInErp,
                    failureMessage: l.externalLinkOpenFailed,
                    style: _valueStyle,
                    opener: opener,
                  )
                : _text(reference),
          );

    final Widget body;
    if (linked) {
      // Three nodes in the order they sit on screen: the title, the reference
      // (its label, then the link with its own action), then the details
      // merged. One summary read before the reference put the details ahead
      // of the row that comes first visually (WCAG 1.3.2).
      body = Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Semantics(
            label: l.erpStatusTitle,
            excludeSemantics: true,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [header, const SizedBox(height: 8)],
            ),
          ),
          referenceRow!,
          if (rows.isNotEmpty)
            Semantics(
              key: const ValueKey('erp-status-details'),
              label: announced.join(', '),
              excludeSemantics: true,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: rows,
              ),
            ),
        ],
      );
    } else {
      // Nothing actionable: one merged announcement, in screen order.
      body = Semantics(
        label: [
          l.erpStatusTitle,
          if (reference != null) '${l.erpStatusReference} $reference',
          ...announced,
        ].join(', '),
        excludeSemantics: true,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            header,
            const SizedBox(height: 8),
            ?referenceRow,
            ...rows,
          ],
        ),
      );
    }

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Colors.indigo.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: Colors.indigo.withValues(alpha: 0.3)),
      ),
      child: body,
    );
  }

  static const _valueStyle = TextStyle(fontSize: 13);

  Widget _text(String value, {bool error = false}) => Text(
        value,
        style: TextStyle(
          fontSize: 13,
          color: error ? Colors.red.shade900 : null,
          fontWeight: error ? FontWeight.w600 : null,
        ),
      );

  /// A label/value row. [center] aligns the label with a 48-dp link target.
  Widget _kv(String label, Widget value, {bool center = false}) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        crossAxisAlignment:
            center ? CrossAxisAlignment.center : CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 110,
            child: Text(
              label,
              style: TextStyle(
                color: Colors.grey.shade700,
                fontSize: 13,
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
          Expanded(child: value),
        ],
      ),
    );
  }
}
