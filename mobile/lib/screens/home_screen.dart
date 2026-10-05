import 'package:flutter/material.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/approvals_screen.dart';
import 'package:feohledger_mobile/screens/contracts_screen.dart';
import 'package:feohledger_mobile/screens/dashboard_screen.dart';
import 'package:feohledger_mobile/screens/exceptions_screen.dart';
import 'package:feohledger_mobile/screens/invoices_screen.dart';
import 'package:feohledger_mobile/screens/payment_queue_screen.dart';
import 'package:feohledger_mobile/screens/payments_screen.dart';
import 'package:feohledger_mobile/screens/settings_screen.dart';
import 'package:feohledger_mobile/screens/vendors_screen.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  int _currentIndex = 0;

  List<Widget> get _screens {
    final screens = <Widget>[
      const DashboardScreen(),
      const InvoicesScreen(),
      const ContractsScreen(),
    ];
    // Approvals follows the invoice-approve permission (the CFO holds it); the
    // exception queue is require_roles(ROLE_ADMIN, ROLE_AP_MANAGER). Two gates,
    // not one — keep [_buildNavItems] in step.
    if (AuthStore.instance.canApproveInvoice) {
      screens.add(const ApprovalsScreen());
    }
    if (AuthStore.instance.canApprove) {
      screens.add(const ExceptionsScreen());
    }
    if (AuthStore.instance.canViewPayments) {
      // Vendor reads + payment queue/runs are admin/ap_manager/cfo on the
      // backend — same gate as the payments tab.
      screens.add(const VendorsScreen());
      screens.add(const PaymentQueueScreen());
      screens.add(const PaymentsScreen());
    }
    screens.add(const SettingsScreen());
    return screens;
  }

  List<BottomNavigationBarItem> _buildNavItems(AppLocalizations l) {
    final items = <BottomNavigationBarItem>[
      BottomNavigationBarItem(
        icon: const Icon(Icons.dashboard),
        label: l.navDashboard,
      ),
      BottomNavigationBarItem(
        icon: const Icon(Icons.receipt_long),
        label: l.navInvoices,
      ),
      BottomNavigationBarItem(
        icon: const Icon(Icons.description),
        label: l.navContracts,
      ),
    ];
    if (AuthStore.instance.canApproveInvoice) {
      items.add(BottomNavigationBarItem(
        icon: const Icon(Icons.check_circle),
        label: l.navApprovals,
      ));
    }
    if (AuthStore.instance.canApprove) {
      items.add(BottomNavigationBarItem(
        icon: const Icon(Icons.error_outline),
        label: l.navExceptions,
      ));
    }
    if (AuthStore.instance.canViewPayments) {
      items.add(BottomNavigationBarItem(
        icon: const Icon(Icons.store),
        label: l.navVendors,
      ));
      items.add(BottomNavigationBarItem(
        icon: const Icon(Icons.account_balance_wallet),
        label: l.navPay,
      ));
      items.add(BottomNavigationBarItem(
        icon: const Icon(Icons.payments),
        label: l.navPayments,
      ));
    }
    items.add(BottomNavigationBarItem(
      icon: const Icon(Icons.settings),
      label: l.navSettings,
    ));
    return items;
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final screens = _screens;
    final safeIndex = _currentIndex.clamp(0, screens.length - 1);

    return Scaffold(
      body: IndexedStack(
        index: safeIndex,
        children: screens,
      ),
      bottomNavigationBar: BottomNavigationBar(
        currentIndex: safeIndex,
        onTap: (i) => setState(() => _currentIndex = i),
        type: BottomNavigationBarType.fixed,
        selectedItemColor: Colors.blue,
        unselectedItemColor: Colors.grey,
        items: _buildNavItems(l),
      ),
    );
  }
}
