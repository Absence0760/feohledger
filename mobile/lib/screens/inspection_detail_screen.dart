import 'package:flutter/material.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/inspection.dart';
import 'package:feohledger_mobile/stores/inspection_store.dart';
import 'package:feohledger_mobile/utils/dates.dart';
import 'package:feohledger_mobile/widgets/inspection_result_badge.dart';

/// One quality inspection, read-only (`GET /api/inspections/{id}`, role-open).
///
/// There is no edit path: an inspection is a record of what was found at the
/// dock, and the backend exposes no update route — a correction is a new
/// inspection, which is also what `po_matching` re-reads (it takes the most
/// recent row for the receipt). So this screen explains the row's consequence
/// for the match instead of offering to change it.
class InspectionDetailScreen extends StatefulWidget {
  final String inspectionId;

  const InspectionDetailScreen({super.key, required this.inspectionId});

  @override
  State<InspectionDetailScreen> createState() => _InspectionDetailScreenState();
}

class _InspectionDetailScreenState extends State<InspectionDetailScreen> {
  Inspection? _inspection;
  bool _loading = true;

  /// The store's error, or null — in which case the screen falls back to its
  /// "not found" copy. `AppLocalizations.of(context)` isn't available from
  /// `initState`, so the fallback is resolved at render time.
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    final inspection = await InspectionStore.instance.getById(
      widget.inspectionId,
    );
    // The route can be popped while a slow GET (or its 10s timeout) is still in
    // flight — setState after dispose throws.
    if (!mounted) return;
    setState(() {
      _inspection = inspection;
      _error = inspection == null ? InspectionStore.instance.error : null;
      _loading = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(l.inspectionDetailTitle)),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _inspection == null
          ? _buildError(l)
          : _buildDetail(l, _inspection!),
    );
  }

  Widget _buildError(AppLocalizations l) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.error_outline, size: 48, color: Colors.red.shade700),
            const SizedBox(height: 12),
            Text(
              _error == null
                  ? l.inspectionDetailNotFound
                  : l.inspectionDetailErrorPrefix(_error!),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 16),
            FilledButton.icon(
              onPressed: _load,
              icon: const Icon(Icons.refresh),
              label: Text(l.commonRetry),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildDetail(AppLocalizations l, Inspection qi) {
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        // See the note in adaptive_screen: a short detail does not scroll, and a
        // RefreshIndicator that cannot be overscrolled is an inert gesture.
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.all(16),
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  qi.inspectionNumber,
                  style: const TextStyle(
                    fontSize: 20,
                    fontWeight: FontWeight.bold,
                  ),
                ),
              ),
              InspectionResultBadge(result: qi.result),
            ],
          ),
          const SizedBox(height: 12),
          // What this outcome means for the 4-way match — the consequence is the
          // reason the row exists, and it is not guessable from "Fail".
          _matchNote(l, qi),
          if (qi.isUnlinked) ...[
            const SizedBox(height: 12),
            _unlinkedNote(l),
          ],
          const SizedBox(height: 16),
          _detailRow(
            l.inspectionDetailFieldReceipt,
            qi.grNumber ??
                (qi.isUnlinked ? l.inspectionsNotLinked : l.inspectionsReceiptUnnamed),
          ),
          _detailRow(
            l.inspectionDetailFieldInspectedDate,
            qi.inspectedDate == null
                ? null
                : formatDate(qi.inspectedDate!),
          ),
          _detailRow(l.inspectionDetailFieldInspector, qi.inspector),
          _detailRow(
            l.inspectionDetailFieldAccepted,
            qi.acceptedQuantityDisplay,
          ),
          _detailRow(
            l.inspectionDetailFieldRejected,
            qi.rejectedQuantityDisplay,
          ),
          _detailRow(l.inspectionDetailFieldStatus, qi.status),
          _detailRow(
            l.inspectionDetailFieldCreated,
            qi.createdAt == null ? null : formatDateTime(qi.createdAt!),
          ),
          if (qi.deviationNotes != null && qi.deviationNotes!.isNotEmpty) ...[
            const SizedBox(height: 16),
            Text(
              l.inspectionDetailSectionNotes,
              style: const TextStyle(fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 6),
            Text(qi.deviationNotes!),
          ],
        ],
      ),
    );
  }

  Widget _matchNote(AppLocalizations l, Inspection qi) {
    final text = switch (qi.result) {
      InspectionResult.pass => l.inspectionsHintPass,
      InspectionResult.fail => l.inspectionsHintFail,
      InspectionResult.partial => l.inspectionsHintPartial,
      // A result outside the vocabulary is named rather than explained: we
      // cannot say what the matcher will make of a value it does not know.
      InspectionResult.unknown => l.inspectionsHintUnknown,
    };
    final scheme = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: scheme.secondaryContainer,
        borderRadius: BorderRadius.circular(8),
      ),
      child: Text(text, style: TextStyle(color: scheme.onSecondaryContainer)),
    );
  }

  /// An inspection naming neither a receipt nor a PO is one no match will ever
  /// read — the row exists, and it changes nothing. Saying so is the difference
  /// between a record and a false sense of a blocked invoice.
  Widget _unlinkedNote(AppLocalizations l) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Colors.orange.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: Colors.orange.withValues(alpha: 0.4)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ExcludeSemantics(
            child: Icon(
              Icons.link_off,
              size: 20,
              color: Colors.brown.shade800,
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Text(
              l.inspectionsNotLinkedHint,
              // brown.shade800 over the pale orange tint clears AA, where a
              // true orange does not.
              style: TextStyle(color: Colors.brown.shade800),
            ),
          ),
        ],
      ),
    );
  }

  Widget _detailRow(String label, String? value) {
    if (value == null || value.isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 130,
            child: Text(
              label,
              // shade700, not shade600: the a11y pass found the lighter greys
              // fail AA at label sizes.
              style: TextStyle(
                color: Colors.grey.shade700,
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
          Expanded(child: Text(value)),
        ],
      ),
    );
  }
}
