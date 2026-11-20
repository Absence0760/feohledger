import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/cash_flow_screen.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';

/// App-bar action that opens the predictive cash-flow forecast. Gated to
/// CFO / admin (mirrors the backend `_CFO_ROLES` gate on the analytics
/// endpoints) — renders nothing for everyone else. Drop it into the Dashboard
/// `AppBar.actions`, the same way [NotificationBell] is wired.
class CashFlowButton extends StatelessWidget {
  const CashFlowButton({super.key});

  @override
  Widget build(BuildContext context) {
    if (!AuthStore.instance.canViewCashFlow) return const SizedBox.shrink();
    final l = AppLocalizations.of(context);
    return Semantics(
      label: l.cashFlowButtonLabel,
      button: true,
      excludeSemantics: true,
      child: IconButton(
        tooltip: l.cashFlowButtonLabel,
        icon: const Icon(Icons.show_chart),
        onPressed: () => Navigator.of(context).push(
          MaterialPageRoute(builder: (_) => const CashFlowScreen()),
        ),
      ),
    );
  }
}
