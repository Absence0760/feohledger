import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/l10n/coded_refusal_messages.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/l10n/gl_chart_refusal_messages.dart';
import 'package:feohledger_mobile/l10n/load_error_messages.dart';
import 'package:feohledger_mobile/models/action_verdict.dart';
import 'package:feohledger_mobile/models/audit_entry.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/utils/dates.dart';
import 'package:feohledger_mobile/utils/money.dart';
import 'package:feohledger_mobile/widgets/activity_timeline.dart';
import 'package:feohledger_mobile/widgets/erp_status_panel.dart';
import 'package:feohledger_mobile/widgets/invoice_edit_sheet.dart';
import 'package:feohledger_mobile/widgets/invoice_file_viewer.dart';
import 'package:feohledger_mobile/widgets/invoice_warnings_panel.dart';
import 'package:feohledger_mobile/widgets/load_error_state.dart';
import 'package:feohledger_mobile/widgets/reject_reason_dialog.dart';
import 'package:feohledger_mobile/widgets/status_badge.dart';

class InvoiceDetailScreen extends StatefulWidget {
  final String invoiceId;

  const InvoiceDetailScreen({super.key, required this.invoiceId});

  @override
  State<InvoiceDetailScreen> createState() => _InvoiceDetailScreenState();
}

class _InvoiceDetailScreenState extends State<InvoiceDetailScreen> {
  Invoice? _invoice;
  bool _loading = true;
  // The caught load error, worded at render time by [describeLoadError].
  Object? _error;
  // True while an approve/reject network call is in flight — guards against a
  // double-tap firing the money-path POST twice and disables the buttons.
  bool _submitting = false;

  // The server's verdicts on this user's workflow actions for the invoice
  // (`GET /review-actions`): `approve` / `reject` (`review.review_refusals`)
  // on an invoice in review, `recall_approval` (backend decisions §374) on
  // one holding an approval. Whether to offer Approve, Reject or Recall is
  // read from these alone, never a rule restated here; unanswered offers
  // nothing.
  ActionVerdict _approveVerdict = ActionVerdict.unanswered;
  ActionVerdict _rejectVerdict = ActionVerdict.unanswered;
  ActionVerdict _recall = ActionVerdict.unanswered;

  // Where the verdict read stands. A loading or failed read leaves every
  // verdict unanswered, exactly like a role that may do nothing — so the
  // action bar says which it is (a placeholder, or "couldn't check") instead
  // of showing the same empty bar for all three.
  _VerdictRead _verdictRead = _VerdictRead.idle;

  // Activity timeline (audit log) state — loaded independently of the invoice
  // body so a slow / failed trail never blocks the detail view.
  List<AuditEntry> _activity = [];
  bool _activityLoading = true;
  bool _activityFailed = false;

  @override
  void initState() {
    super.initState();
    _load();
    _loadActivity();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final invoice = await InvoiceApi.getById(widget.invoiceId);
      // The route can be popped while a slow GET (or its 10s timeout) is still
      // in flight — setState after dispose throws. Matches _loadActivity below.
      if (!mounted) return;
      setState(() {
        _invoice = invoice;
        _loading = false;
      });
      await _loadVerdicts(invoice);
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e;
        _loading = false;
      });
    }
  }

  /// Ask the server which workflow actions this user may take on [invoice].
  /// Only an invoice in review can be approved or rejected, and only an
  /// approved one, or a failed one (an approved invoice's failed ERP push),
  /// can have an approval recalled; for anything else nothing is asked. A
  /// failed read leaves every verdict unanswered, which offers nothing.
  Future<void> _loadVerdicts(Invoice invoice) async {
    final status = invoice.status;
    final asks = status == InvoiceStatus.readyForReview ||
        status == InvoiceStatus.approved ||
        status == InvoiceStatus.failed;
    // A verdict judged the version this screen held before; drop it until
    // the server answers for the one now on screen.
    if (mounted) {
      setState(() {
        _approveVerdict = ActionVerdict.unanswered;
        _rejectVerdict = ActionVerdict.unanswered;
        _recall = ActionVerdict.unanswered;
        _verdictRead = asks ? _VerdictRead.loading : _VerdictRead.idle;
      });
    }
    if (!asks) return;
    Map<String, dynamic> verdicts;
    try {
      verdicts = await InvoiceApi.reviewActions(invoice.id);
    } catch (_) {
      if (!mounted) return;
      setState(() => _verdictRead = _VerdictRead.failed);
      // Said aloud too: an action bar that silently stays empty reads as
      // "you may not act here", which is not what happened (WCAG 4.1.3).
      A11y.announce(
        context,
        AppLocalizations.of(context).reviewVerdictsUnavailable,
      );
      return;
    }
    if (!mounted) return;
    setState(() {
      _verdictRead = _VerdictRead.loaded;
      _approveVerdict = ActionVerdict.fromVerdicts(verdicts, 'approve');
      _rejectVerdict = ActionVerdict.fromVerdicts(verdicts, 'reject');
      _recall = ActionVerdict.fromVerdicts(verdicts, 'recall_approval');
    });
  }

  /// The reason a refused verdict states beside its disabled control: the
  /// localized sentence for its code, else the server's English.
  String? _refusalText(AppLocalizations l, ActionVerdict verdict) {
    if (verdict.allowed) return null;
    final message = verdict.refusal?['message'];
    return localizeCodedRefusal(l, verdict.refusal) ??
        (message is String && message.isNotEmpty ? message : null);
  }

  Future<void> _recallApproval() async {
    if (_submitting) return;
    final l = AppLocalizations.of(context);
    final reason = await showRejectReasonDialog(
      context,
      title: l.invoiceDetailRecallTitle,
      message: l.invoiceDetailRecallBody,
      reasonLabel: l.invoiceDetailRecallReason,
      confirmLabel: l.invoiceDetailRecall,
    );
    if (reason == null || _submitting || !mounted) return;

    setState(() => _submitting = true);
    try {
      final store = InvoiceStore.instance;
      final success = await store.recallApproval(widget.invoiceId, reason);
      if (!mounted) return;
      // Either way the invoice may have moved under the verdict this screen
      // holds (a run staged meanwhile): re-read both.
      await _load();
      await _loadActivity();
      _showSnack(
        success
            ? l.invoiceDetailRecalled
            : localizeCodedRefusal(l, store.recallErrorDetail) ??
                l.invoiceDetailRecallFailed,
      );
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  Future<void> _loadActivity() async {
    setState(() {
      _activityLoading = true;
      _activityFailed = false;
    });
    try {
      final entries =
          await InvoiceStore.instance.fetchAuditLog(widget.invoiceId);
      if (!mounted) return;
      setState(() {
        _activity = entries;
        _activityLoading = false;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _activityFailed = true;
        _activityLoading = false;
      });
    }
  }

  Future<void> _edit() async {
    final inv = _invoice;
    if (inv == null || _submitting) return;
    final l = AppLocalizations.of(context);
    final changes = await showInvoiceEditSheet(context, inv, recall: _recall);
    if (changes == null || !mounted) return; // cancelled / dismissed
    if (changes.isEmpty) {
      _showSnack(l.invoiceDetailNoChanges);
      return;
    }

    setState(() => _submitting = true);
    try {
      final updated = await InvoiceStore.instance.update(widget.invoiceId, changes);
      if (!mounted) return;
      if (updated != null) {
        // Reflect the edit + the new `invoice.edited` audit row.
        await _load();
        await _loadActivity();
        _showSnack(l.invoiceDetailUpdated);
      } else {
        // The server's sentence, not a generic "try again": an edit is refused
        // for a reason the user has to act on (a GL code outside the invoice's
        // chart, a field frozen by approval), and retrying changes nothing.
        // A GL-chart refusal is stated in the reader's language from its code
        // (`gl_chart_refusal_messages.dart`); anything else is the server's
        // own sentence.
        final store = InvoiceStore.instance;
        final reason = localizeGlChartRefusal(l, store.updateErrorDetail) ??
            store.actionError ??
            '';
        _showSnack(l.invoiceDetailUpdateFailed(reason));
      }
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  Future<void> _approve() async {
    if (_submitting) return;
    final l = AppLocalizations.of(context);
    setState(() => _submitting = true);
    try {
      final success = await InvoiceStore.instance.approve(
        widget.invoiceId,
        expectedUpdatedAt: _invoice?.updatedAt,
      );
      if (!mounted) return;
      if (success) {
        await _load();
        _showSnack(l.invoiceDetailApproved);
      } else {
        // Refused because the invoice changed since this screen loaded it:
        // show the current version so the approver reviews what is there now.
        if (InvoiceStore.isStaleApproval(
          InvoiceStore.instance.approveErrorDetail,
        )) {
          await _load();
          if (!mounted) return;
        }
        // A coded refusal (segregation of duties, the CFO / max-amount gate,
        // the named-approver gate) is the reason the approver has to act on —
        // retrying changes nothing — so it is stated in the reader's language
        // from its code. Anything else keeps the generic retry message.
        _showSnack(
          localizeCodedRefusal(l, InvoiceStore.instance.approveErrorDetail) ??
              l.invoiceDetailApproveFailed,
        );
      }
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  Future<void> _reject() async {
    final l = AppLocalizations.of(context);
    final reason = await showRejectReasonDialog(
      context,
      title: l.invoiceDetailRejectTitle,
    );

    if (reason == null || _submitting) return;

    setState(() => _submitting = true);
    try {
      final success =
          await InvoiceStore.instance.reject(widget.invoiceId, reason);
      if (!mounted) return;
      if (success) {
        await _load();
        _showSnack(l.invoiceDetailRejected);
      } else {
        _showSnack(l.invoiceDetailRejectFailed);
      }
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  void _showSnack(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(message)),
    );
    // Mirror the toast to assistive tech — a SnackBar is not reliably
    // announced on its own (WCAG 4.1.3).
    A11y.announce(context, message);
  }

  bool get _canEdit {
    final inv = _invoice;
    return inv != null && AuthStore.instance.canEditInvoiceRow(inv);
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text(l.invoiceDetailTitle),
        actions: [
          if (_canEdit)
            Semantics(
              label: l.invoiceDetailEditLabel,
              button: true,
              child: IconButton(
                tooltip: l.invoiceDetailEdit,
                icon: const Icon(Icons.edit),
                onPressed: _submitting ? null : _edit,
              ),
            ),
        ],
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _error != null
              ? _buildError()
              : _buildDetail(),
      bottomNavigationBar: _buildActions(),
    );
  }

  Widget _buildError() {
    final l = AppLocalizations.of(context);
    return LoadErrorState(
      message: describeLoadError(l, _error!, l.invoiceDetailErrorPrefix),
      onRetry: _load,
    );
  }

  Future<void> _refreshAll() async {
    await _load();
    await _loadActivity();
  }

  Widget _buildDetail() {
    final inv = _invoice!;
    final l = AppLocalizations.of(context);
    return RefreshIndicator(
      onRefresh: _refreshAll,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          // Header
          Row(
            children: [
              Expanded(
                child: Text(
                  inv.vendorName ?? l.invoiceDetailUnknownVendor,
                  style: const TextStyle(
                    fontSize: 20,
                    fontWeight: FontWeight.bold,
                  ),
                ),
              ),
              StatusBadge(status: inv.status),
            ],
          ),
          if (inv.amount != null) ...[
            const SizedBox(height: 8),
            Text(
              formatMoney(inv.amount, currency: inv.currency),
              style: const TextStyle(
                fontSize: 28,
                fontWeight: FontWeight.bold,
              ),
            ),
          ],

          // Warnings / fraud flags + PO match (parity with the web modal).
          if (inv.warnings.isNotEmpty ||
              (inv.poMatch != null && !inv.poMatch!.isNoPo)) ...[
            const SizedBox(height: 16),
            InvoiceWarningsPanel(
              warnings: inv.warnings,
              poMatch: inv.poMatch,
            ),
          ],

          // Invoice file preview (image thumbnail or PDF card → full viewer)
          if (inv.fileUrl != null && inv.fileUrl!.isNotEmpty) ...[
            const SizedBox(height: 16),
            _buildFilePreview(inv.fileUrl!),
          ],

          const SizedBox(height: 24),

          // Details
          _detailRow(l.invoiceDetailFieldInvoiceNumber, inv.invoiceNumber),
          _detailRow(l.invoiceDetailFieldPoNumber, inv.poNumber),
          _detailRow(l.invoiceDetailFieldCurrency, inv.currency),
          _detailRow(
            l.invoiceDetailFieldInvoiceDate,
            inv.invoiceDate != null
                ? formatDate(inv.invoiceDate!)
                : null,
          ),
          _detailRow(
            l.invoiceDetailFieldDueDate,
            inv.dueDate != null ? formatDate(inv.dueDate!) : null,
          ),
          _detailRow(l.invoiceDetailFieldDescription, inv.description),
          _detailRow(l.invoiceDetailFieldGlAccount, inv.glAccount),
          _detailRow(
            l.invoiceDetailFieldCreated,
            formatDate(inv.createdAt),
          ),

          // ERP status — derived from the loaded audit log (the activity is
          // fetched independently, so this fills in once the trail lands).
          if (!_activityLoading) ...[
            Builder(
              builder: (context) {
                final panel = ErpStatusPanel(
                  invoice: inv,
                  erpInfo: ErpInfo.fromAuditLog(_activity),
                );
                // Only add spacing when the panel actually renders.
                if (!panel.visible) return const SizedBox.shrink();
                return Padding(
                  padding: const EdgeInsets.only(top: 16),
                  child: panel,
                );
              },
            ),
          ],

          const SizedBox(height: 24),
          const Divider(),
          const SizedBox(height: 8),
          Text(
            l.invoiceDetailActivity,
            style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold),
          ),
          const SizedBox(height: 8),
          _buildActivity(),
        ],
      ),
    );
  }

  Widget _buildActivity() {
    if (_activityLoading) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 16),
        child: Center(child: CircularProgressIndicator()),
      );
    }
    final l = AppLocalizations.of(context);
    if (_activityFailed) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: Row(
          children: [
            Expanded(
              child: Text(
                l.invoiceDetailActivityError,
                style: TextStyle(color: Colors.grey.shade700),
              ),
            ),
            TextButton.icon(
              onPressed: _loadActivity,
              icon: const Icon(Icons.refresh, size: 18),
              label: Text(l.invoiceDetailRetry),
            ),
          ],
        ),
      );
    }
    return ActivityTimeline(entries: _activity);
  }

  Widget _detailRow(String label, String? value) {
    if (value == null || value.isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 120,
            child: Text(
              label,
              // onSurfaceVariant clears AA (~8.9:1 on the blue-seed surface);
              // grey.shade600 read ~4.4:1 there, under the 4.5:1 floor.
              style: TextStyle(
                color: Theme.of(context).colorScheme.onSurfaceVariant,
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
          Expanded(child: Text(value)),
        ],
      ),
    );
  }

  /// Inline preview tile — an image thumbnail for image files, a PDF card for
  /// PDFs (which can't render via [Image.network]). Tapping either opens the
  /// full [InvoiceFileViewer].
  Widget _buildFilePreview(String fileUrl) {
    final l = AppLocalizations.of(context);
    final isPdf = InvoiceFileViewer.isPdf(fileUrl);
    return Semantics(
      label: isPdf ? l.invoiceDetailFilePdfLabel : l.invoiceDetailFileLabel,
      button: true,
      child: GestureDetector(
        onTap: () => _openViewer(context, fileUrl),
        child: Container(
          constraints: const BoxConstraints(maxHeight: 200),
          width: double.infinity,
          decoration: BoxDecoration(
            border: Border.all(color: Colors.grey.shade300),
            borderRadius: BorderRadius.circular(8),
          ),
          clipBehavior: Clip.antiAlias,
          child: isPdf
              ? Padding(
                  padding: const EdgeInsets.all(24),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Icon(Icons.picture_as_pdf,
                          size: 40, color: Colors.redAccent),
                      const SizedBox(height: 8),
                      Text(
                        l.invoiceDetailTapToViewPdf,
                        style: TextStyle(color: Colors.grey.shade700),
                      ),
                    ],
                  ),
                )
              : Image.network(
                  InvoiceFileViewer.absoluteUrl(fileUrl),
                  headers: ApiClient().authHeaders,
                  fit: BoxFit.contain,
                  errorBuilder: (_, _, _) => Padding(
                    padding: const EdgeInsets.all(24),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Icon(Icons.description,
                            size: 40, color: Colors.grey.shade400),
                        const SizedBox(height: 8),
                        Text(
                          l.invoiceDetailTapToViewFile,
                          style: TextStyle(color: Colors.grey.shade700),
                        ),
                      ],
                    ),
                  ),
                ),
        ),
      ),
    );
  }

  /// "Recall approval" — offered from the server's verdict alone: hidden for
  /// a status it does not start from and for a role that cannot recall,
  /// otherwise shown, disabled with the server's reason when refused (a bill
  /// the ERP may hold, a live payment or virtual card).
  Widget? _buildRecallAction() {
    final verdict = _recall;
    if (!verdict.isOffered(hiddenCodes: const {'invoice_not_recallable'})) {
      return null;
    }
    final l = AppLocalizations.of(context);
    final refusal = _refusalText(l, verdict);
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (refusal != null) ...[
              Text(
                refusal,
                style: TextStyle(
                  color: Theme.of(context).colorScheme.onSurfaceVariant,
                ),
              ),
              const SizedBox(height: 8),
            ],
            OutlinedButton.icon(
              onPressed:
                  _submitting || !verdict.allowed ? null : _recallApproval,
              icon: const Icon(Icons.undo),
              label: Text(l.invoiceDetailRecall),
              style: OutlinedButton.styleFrom(
                padding: const EdgeInsets.symmetric(vertical: 14),
              ),
            ),
          ],
        ),
      ),
    );
  }

  void _openViewer(BuildContext context, String fileUrl) {
    Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => InvoiceFileViewer(fileUrl: fileUrl),
      ),
    );
  }

  /// The destructive outline for Reject: red (shade700, AA on the surface)
  /// while it can be pressed, the theme's disabled colours when a refused
  /// verdict or an in-flight submit disables it — a hard-coded red read as
  /// active either way.
  static ButtonStyle _rejectStyle(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final disabled = scheme.onSurface.withValues(alpha: 0.38);
    final red = Colors.red.shade700;
    return OutlinedButton.styleFrom(
      padding: const EdgeInsets.symmetric(vertical: 14),
    ).copyWith(
      foregroundColor: WidgetStateProperty.resolveWith(
        (states) => states.contains(WidgetState.disabled) ? disabled : red,
      ),
      iconColor: WidgetStateProperty.resolveWith(
        (states) => states.contains(WidgetState.disabled) ? disabled : red,
      ),
      side: WidgetStateProperty.resolveWith(
        (states) => BorderSide(
          color: states.contains(WidgetState.disabled)
              ? scheme.onSurface.withValues(alpha: 0.12)
              : red,
        ),
      ),
    );
  }

  /// The action bar while the verdict is being read: the bar's height held,
  /// with a progress line and a label a screen reader reads, so the buttons
  /// appearing a moment later do not look like a change of mind.
  Widget _buildVerdictPlaceholder() {
    final l = AppLocalizations.of(context);
    return SafeArea(
      child: Padding(
        key: const ValueKey('review-verdicts-loading'),
        padding: const EdgeInsets.all(16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const LinearProgressIndicator(),
            const SizedBox(height: 8),
            Text(
              l.reviewVerdictsChecking,
              style: TextStyle(
                color: Theme.of(context).colorScheme.onSurfaceVariant,
              ),
            ),
          ],
        ),
      ),
    );
  }

  /// The action bar when the verdict could not be read (offline, a failed
  /// request): one line saying so, which a pull-to-refresh re-asks.
  Widget _buildVerdictUnavailable() {
    final l = AppLocalizations.of(context);
    final muted = Theme.of(context).colorScheme.onSurfaceVariant;
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            ExcludeSemantics(
              child: Icon(Icons.cloud_off, size: 18, color: muted),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                l.reviewVerdictsUnavailable,
                key: const ValueKey('review-verdicts-unavailable'),
                style: TextStyle(color: muted),
              ),
            ),
          ],
        ),
      ),
    );
  }

  /// Approve / Reject on an invoice in review, each offered from the
  /// server's verdict alone (`frontend/docs/ui-patterns.md` § Whether to offer
  /// an action): hidden for a role that cannot review and for a status the
  /// action does not start from, otherwise shown, and disabled beside the
  /// server's reason when refused (segregation of duties, the CFO or
  /// max-amount gate, the named-approver gate).
  Widget? _buildActions() {
    final inv = _invoice;
    if (inv == null) return null;
    switch (_verdictRead) {
      case _VerdictRead.loading:
        return _buildVerdictPlaceholder();
      case _VerdictRead.failed:
        return _buildVerdictUnavailable();
      case _VerdictRead.idle:
      case _VerdictRead.loaded:
        break;
    }
    if (!inv.status.isActionable) return _buildRecallAction();
    const hidden = {'invoice_not_in_review'};
    final approveOffered = _approveVerdict.isOffered(hiddenCodes: hidden);
    final rejectOffered = _rejectVerdict.isOffered(hiddenCodes: hidden);
    if (!approveOffered && !rejectOffered) return null;
    final l = AppLocalizations.of(context);
    final refusals = <String>{
      if (approveOffered) ?_refusalText(l, _approveVerdict),
      if (rejectOffered) ?_refusalText(l, _rejectVerdict),
    };

    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (refusals.isNotEmpty) ...[
              Text(
                refusals.join(' '),
                key: const ValueKey('review-refusal'),
                style: TextStyle(
                  color: Theme.of(context).colorScheme.onSurfaceVariant,
                ),
              ),
              const SizedBox(height: 8),
            ],
            Row(
              children: [
                if (rejectOffered)
                  Expanded(
                    child: OutlinedButton.icon(
                      key: const ValueKey('review-reject'),
                      onPressed:
                          _submitting || !_rejectVerdict.allowed ? null : _reject,
                      icon: const Icon(Icons.close),
                      label: Text(l.invoiceDetailReject),
                      style: _rejectStyle(context),
                    ),
                  ),
                if (rejectOffered && approveOffered) const SizedBox(width: 16),
                if (approveOffered)
                  Expanded(
                    child: FilledButton.icon(
                      key: const ValueKey('review-approve'),
                      onPressed:
                          _submitting || !_approveVerdict.allowed
                          ? null
                          : _approve,
                      icon: const Icon(Icons.check),
                      label: Text(l.invoiceDetailApprove),
                      // green.shade800 under a white label is ~5.1:1; the
                      // full-saturation green was 2.78:1 (WCAG 1.4.3). Set
                      // for the enabled state only, so a refused verdict
                      // keeps the theme's disabled look.
                      style: FilledButton.styleFrom(
                        padding: const EdgeInsets.symmetric(vertical: 14),
                        backgroundColor: Colors.green.shade800,
                        foregroundColor: Colors.white,
                      ),
                    ),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

/// Where [_InvoiceDetailScreenState]'s verdict read stands: not asked (a
/// status with no review action), in flight, failed, or answered.
enum _VerdictRead { idle, loading, failed, loaded }
