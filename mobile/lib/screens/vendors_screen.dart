import 'package:material_ui/material_ui.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/coded_refusal_messages.dart';
import 'package:feohledger_mobile/l10n/enum_labels.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/l10n/load_error_messages.dart';
import 'package:feohledger_mobile/models/action_verdict.dart';
import 'package:feohledger_mobile/models/vendor.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/vendor_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/utils/debouncer.dart';
import 'package:feohledger_mobile/widgets/async_list_body.dart';
import 'package:feohledger_mobile/widgets/vendor_list_tile.dart';

/// Vendor management — list with status filters + search, verify / reject a
/// vendor, and pull vendors from the connected ERP. Verify / Reject are
/// offered from each row's `status_actions` verdict (the server's
/// `vendor.manage` check and status table), never a role or status check
/// here: a role or status refusal hides the action, any other refusal shows
/// it disabled with its reason, and a row with no verdict offers nothing. The
/// ERP sync is gated to admin / ap_manager (the backend `require_roles`).
class VendorsScreen extends StatefulWidget {
  const VendorsScreen({super.key});

  @override
  State<VendorsScreen> createState() => _VendorsScreenState();
}

class _VendorsScreenState extends State<VendorsScreen> {
  final _searchController = TextEditingController();
  final _searchDebouncer = Debouncer();
  bool _syncing = false;

  bool get _canManage => AuthStore.instance.canManageVendors;

  @override
  void initState() {
    super.initState();
    SchedulerBinding.instance.addPostFrameCallback((_) {
      VendorStore.instance.fetch();
    });
  }

  @override
  void dispose() {
    _searchDebouncer.cancel();
    _searchController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text(l.vendorsTitle),
        actions: [
          if (_canManage)
            Semantics(
              label: l.vendorsSyncErpLabel,
              button: true,
              child: IconButton(
                tooltip: l.vendorsSyncErp,
                icon: _syncing
                    ? const SizedBox(
                        width: 20,
                        height: 20,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.sync),
                onPressed: _syncing ? null : _syncErp,
              ),
            ),
        ],
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(56),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
            child: SearchBar(
              controller: _searchController,
              hintText: l.vendorsSearchHint,
              leading: const Icon(Icons.search, size: 20),
              onChanged: (q) => _searchDebouncer.run(
                () => VendorStore.instance.setSearch(q.isEmpty ? null : q),
              ),
              elevation: WidgetStateProperty.all(0),
            ),
          ),
        ),
      ),
      body: Column(
        children: [
          SizedBox(
            height: 48,
            child: ListenableBuilder(
              listenable: VendorStore.instance,
              builder: (context, _) {
                final current = VendorStore.instance.statusFilter;
                return ListView(
                  scrollDirection: Axis.horizontal,
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  children: [
                    _filterChip(l.commonAll, null, current),
                    _filterChip(
                      l.vendorsFilterUnverified,
                      'unverified',
                      current,
                    ),
                    _filterChip(l.vendorsFilterActive, 'active', current),
                    _filterChip(l.vendorsFilterInactive, 'inactive', current),
                    _filterChip(l.vendorsFilterRejected, 'rejected', current),
                  ],
                );
              },
            ),
          ),
          Expanded(
            child: ListenableBuilder(
              listenable: VendorStore.instance,
              builder: (context, _) {
                final store = VendorStore.instance;
                final loadError = store.loadError;

                return AsyncListBody<Vendor>(
                  items: store.vendors,
                  loading: store.loading,
                  // The fetch's failure only — a refused verify leaves the
                  // list live (actionError), so it raises no banner.
                  hasError: loadError != null,
                  fromCache: store.fromCache,
                  // Worded from the cause: the server's reason on a 403 / 500,
                  // the connection sentence only when no server answered.
                  errorMessage: loadError == null
                      ? ''
                      : describeLoadError(
                          l,
                          loadError,
                          l.vendorsLoadErrorPrefix,
                        ),
                  onRefresh: store.fetch,
                  empty: Text(l.vendorsEmpty, textAlign: TextAlign.center),
                  filteredEmpty: store.isFiltered
                      ? FilteredEmptyState(
                          message: l.vendorsFilteredEmpty,
                          onShowAll: _showAll,
                        )
                      : null,
                  itemBuilder: (context, vendor) => _buildRow(vendor),
                );
              },
            ),
          ),
        ],
      ),
    );
  }

  /// A status refusal (`vendor_status_not_allowed`: verifying an active
  /// vendor, anything on a rejected one) is about the row, not something the
  /// reader can act on, so it hides the action like a role refusal does.
  static const _hiddenRefusals = {'vendor_status_not_allowed'};

  Widget _buildRow(Vendor vendor) {
    final l = AppLocalizations.of(context);
    final verify = vendor.verifyVerdict;
    final reject = vendor.rejectVerdict;
    final offered =
        verify.isOffered(hiddenCodes: _hiddenRefusals) ||
        reject.isOffered(hiddenCodes: _hiddenRefusals);
    final tile = VendorListTile(
      vendor: vendor,
      onTap: offered ? () => _showActions(vendor) : null,
    );

    // Swipe only in the directions the server would accept now; a refused
    // action is explained in the sheet, never swiped into a 403 / 409.
    final direction = verify.allowed && reject.allowed
        ? DismissDirection.horizontal
        : verify.allowed
        ? DismissDirection.startToEnd
        : reject.allowed
        ? DismissDirection.endToStart
        : null;
    if (direction == null) {
      return tile;
    }

    return Dismissible(
      key: ValueKey(vendor.id),
      direction: direction,
      background: _swipeBackground(
        Colors.green.shade700,
        Colors.green.shade900,
        Icons.check,
        Alignment.centerLeft,
        l.vendorActionVerify,
      ),
      secondaryBackground: _swipeBackground(
        Colors.red.shade700,
        Colors.red.shade900,
        Icons.block,
        Alignment.centerRight,
        l.vendorActionReject,
      ),
      confirmDismiss: (direction) async {
        final verify = direction == DismissDirection.startToEnd;
        // A sideways swipe is easy to make by accident, and Reject is
        // accepted on an active vendor and cancels its virtual cards, so it
        // confirms first; cancelling leaves the row where it was.
        if (!verify && !await _confirmReject(vendor)) return false;
        final ok = verify
            ? await VendorStore.instance.verify(vendor.id)
            : await VendorStore.instance.reject(vendor.id);
        if (mounted) {
          A11y.announce(
            context,
            ok
                ? (verify ? l.vendorVerified : l.vendorRejected)
                : l.vendorActionFailed,
          );
          if (ok && !verify) _warnLiveCards();
        }
        // The list refetches on success, so consume the dismiss (return false)
        // and let the refetched list drop the row — avoids a stale gap if the
        // server rejected the action.
        return false;
      },
      child: tile,
    );
  }

  /// The swipe affordance: what the gesture will do, as text beside the
  /// icon so the icon is never an unlabelled glyph (WCAG 1.1.1).
  ///
  /// [tint] colours the 15% wash; [ink] the icon and the label, one shade
  /// darker so the copy stays readable on that wash: green.shade900 on the
  /// green.shade700 wash is 6.5:1 and red.shade900 on the red.shade700 wash
  /// 5.2:1, where the shade700 ink this used to share with the wash read
  /// 3.4:1 and 3.9:1 and failed AA (1.4.3).
  Widget _swipeBackground(
    Color tint,
    Color ink,
    IconData icon,
    Alignment alignment,
    String action,
  ) {
    return Container(
      color: tint.withValues(alpha: 0.15),
      alignment: alignment,
      padding: const EdgeInsets.symmetric(horizontal: 24),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, color: ink),
          const SizedBox(width: 8),
          Text(
            action,
            style: TextStyle(color: ink, fontWeight: FontWeight.w600),
          ),
        ],
      ),
    );
  }

  void _showActions(Vendor vendor) {
    final l = AppLocalizations.of(context);
    final verify = _sheetAction(
      vendor,
      vendor.verifyVerdict,
      verify: true,
      icon: const Icon(Icons.check, color: Colors.green),
      label: l.vendorActionVerify,
      hint: l.vendorVerifyHint,
    );
    final reject = _sheetAction(
      vendor,
      vendor.rejectVerdict,
      verify: false,
      icon: Icon(Icons.block, color: Colors.red.shade700),
      label: l.vendorActionReject,
      hint: l.vendorRejectHint,
    );
    showModalBottomSheet<void>(
      context: context,
      builder: (sheetContext) {
        return SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              ListTile(
                title: Text(
                  vendor.name,
                  style: const TextStyle(fontWeight: FontWeight.w600),
                ),
                subtitle: Text(vendorStatusLabel(l, vendor.status)),
              ),
              const Divider(height: 1),
              ?verify?.call(sheetContext),
              ?reject?.call(sheetContext),
            ],
          ),
        );
      },
    );
  }

  /// One sheet action from its [verdict]: absent when not offered, enabled
  /// when accepted, else disabled with the refusal in the reader's language.
  Widget Function(BuildContext sheetContext)? _sheetAction(
    Vendor vendor,
    ActionVerdict verdict, {
    required bool verify,
    required Widget icon,
    required String label,
    required String hint,
  }) {
    if (!verdict.isOffered(hiddenCodes: _hiddenRefusals)) return null;
    final l = AppLocalizations.of(context);
    final reason = verdict.allowed
        ? null
        : localizeCodedRefusal(l, verdict.refusal) ??
              (verdict.refusal?['message'] as String? ?? '');
    return (sheetContext) => ListTile(
      leading: icon,
      title: Text(label),
      subtitle: Text(reason ?? hint),
      enabled: verdict.allowed,
      onTap: verdict.allowed
          ? () => _runAction(sheetContext, vendor, verify: verify)
          : null,
    );
  }

  Future<void> _runAction(
    BuildContext sheetContext,
    Vendor vendor, {
    required bool verify,
  }) async {
    Navigator.of(sheetContext).pop();
    if (!verify && !await _confirmReject(vendor)) return;
    if (!mounted) return;
    final l = AppLocalizations.of(context);
    final store = VendorStore.instance;
    final ok = verify
        ? await store.verify(vendor.id)
        : await store.reject(vendor.id);
    if (!mounted) return;
    A11y.announce(
      context,
      ok
          ? (verify ? l.vendorVerified : l.vendorRejected)
          : l.vendorActionFailed,
    );
    if (ok && !verify) _warnLiveCards();
  }

  /// Asks before rejecting [vendor], naming it and the virtual cards the
  /// server cancels on reject (`revoke_vendor_cards`). True only on an
  /// explicit confirm; dismissing the dialog is a no.
  Future<bool> _confirmReject(Vendor vendor) async {
    final l = AppLocalizations.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(l.vendorRejectConfirmTitle(vendor.name)),
        content: Text(l.vendorRejectConfirmBody(vendor.name)),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop(false),
            child: Text(l.commonCancel),
          ),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Colors.red.shade700),
            onPressed: () => Navigator.of(dialogContext).pop(true),
            child: Text(l.vendorActionReject),
          ),
        ],
      ),
    );
    return confirmed == true;
  }

  /// Rejecting a vendor cancels its live virtual cards server-side. A card
  /// the provider did not confirm closed, or one behind a live payment, is
  /// still spendable — say so, never let the plain "rejected" stand alone.
  void _warnLiveCards() {
    final v = VendorStore.instance.lastRejected;
    if (v == null) return;
    final l = AppLocalizations.of(context);
    final lines = [
      if (v.cardsNotClosed > 0) l.vendorCardsNotClosed(v.cardsNotClosed),
      if (v.cardsRequireVoid > 0) l.vendorCardsRequireVoid(v.cardsRequireVoid),
    ];
    if (lines.isEmpty) return;
    final text = lines.join('\n');
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(text), duration: const Duration(seconds: 12)),
    );
    A11y.announce(context, text);
  }

  Future<void> _syncErp() async {
    final l = AppLocalizations.of(context);
    setState(() => _syncing = true);
    final message = await VendorStore.instance.syncErp();
    if (!mounted) return;
    setState(() => _syncing = false);
    final text =
        message ?? l.vendorSyncFailed(VendorStore.instance.actionError ?? '');
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
    A11y.announce(context, text);
  }

  /// The filtered-empty state's "Show all": clear the search box with the
  /// chip, or the field would keep a query the list no longer applies.
  void _showAll() {
    _searchDebouncer.cancel();
    _searchController.clear();
    VendorStore.instance.clearFilters();
  }

  Widget _filterChip(String label, String? value, String? current) {
    final selected = current == value;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4),
      child: FilterChip(
        label: Text(label),
        selected: selected,
        onSelected: (_) => VendorStore.instance.setStatusFilter(value),
      ),
    );
  }
}
