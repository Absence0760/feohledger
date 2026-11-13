import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/config.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/adaptive_screen.dart';
import 'package:feohledger_mobile/screens/admin_users_screen.dart';
import 'package:feohledger_mobile/screens/inspections_screen.dart';
import 'package:feohledger_mobile/screens/org_settings_screen.dart';
import 'package:feohledger_mobile/screens/workflows_screen.dart';
import 'package:feohledger_mobile/services/biometric_service.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/locale_store.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  bool _bioAvailable = false;
  bool _bioEnabled = false;

  @override
  void initState() {
    super.initState();
    _loadBiometricState();
  }

  Future<void> _loadBiometricState() async {
    final available = await BiometricService.instance.isAvailable;
    final enabled = await BiometricService.instance.isEnabled;
    if (mounted) {
      setState(() {
        _bioAvailable = available;
        _bioEnabled = enabled;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final user = AuthStore.instance.user;
    final l = AppLocalizations.of(context);

    return Scaffold(
      appBar: AppBar(title: Text(l.settingsTitle)),
      body: ListView(
        children: [
          // User info
          Container(
            padding: const EdgeInsets.all(24),
            color: Colors.blue.withValues(alpha: 0.05),
            child: Column(
              children: [
                CircleAvatar(
                  radius: 32,
                  backgroundColor: Colors.blue,
                  child: Text(
                    user?.fullName.isNotEmpty == true
                        ? user!.fullName[0].toUpperCase()
                        : '?',
                    style: const TextStyle(
                      fontSize: 24,
                      color: Colors.white,
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                Text(
                  user?.fullName ?? 'Unknown',
                  style: const TextStyle(
                    fontSize: 18,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  user?.email ?? '',
                  style: TextStyle(color: Colors.grey.shade600),
                ),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 6,
                  children: (user?.roles ?? [])
                      .map(
                        (r) => Chip(
                          label:
                              Text(r, style: const TextStyle(fontSize: 12)),
                          visualDensity: VisualDensity.compact,
                        ),
                      )
                      .toList(),
                ),
              ],
            ),
          ),

          // Connection
          const SizedBox(height: 16),
          ListTile(
            leading: const Icon(Icons.business),
            title: Text(l.settingsTenant),
            subtitle: Text(AppConfig.tenantSlug ?? l.settingsTenantNotSet),
          ),
          ListTile(
            leading: const Icon(Icons.link),
            title: Text(l.settingsApiServer),
            subtitle: Text(AppConfig.apiBaseUrl),
          ),

          // Procurement — quality inspections (the 4th leg of 4-way matching).
          // NOT role-gated: `GET /api/inspections` and its detail are
          // `get_current_user`, and a clerk chasing a quality hold is exactly
          // who needs to read a failed inspection. The *record* affordance
          // inside the screen is gated instead (admin / ap_manager, mirroring
          // `POST /api/inspections`), so a clerk gets the queue and no button —
          // the same split the web `/goods-receipts` Inspections tab makes.
          const Divider(height: 32),
          _sectionHeader(l.settingsProcurement),
          ListTile(
            leading: const Icon(Icons.fact_check_outlined),
            title: Text(l.settingsInspections),
            subtitle: Text(l.settingsInspectionsHint),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => const InspectionsScreen()),
            ),
          ),

          // Administration — gated PER ENTRY against each surface's own backend
          // gate, not once for the whole group. `/api/admin/*` and
          // `PATCH /api/organization` are admin-only, while every
          // `/api/adaptive` read is admin / ap_manager / cfo (`_READ_ROLES`); a
          // group-level `isOrgAdmin` gate cannot express both, and would hide
          // the adaptive surface from the two roles the backend admits. (The
          // same per-entry correction the web nav took — see
          // `docs/followups.md`, round 28.) The section header renders whenever
          // at least one of its entries does.
          if (AuthStore.instance.isOrgAdmin ||
              AuthStore.instance.canViewAdaptive) ...[
            const Divider(height: 32),
            _sectionHeader(l.settingsAdministration),
            if (AuthStore.instance.isOrgAdmin) ...[
              ListTile(
                leading: const Icon(Icons.group),
                title: Text(l.settingsAdminUsers),
                subtitle: Text(l.settingsAdminUsersHint),
                trailing: const Icon(Icons.chevron_right),
                onTap: () => Navigator.of(context).push(
                  MaterialPageRoute(builder: (_) => const AdminUsersScreen()),
                ),
              ),
              ListTile(
                leading: const Icon(Icons.business_center),
                title: Text(l.settingsAdminOrg),
                subtitle: Text(l.settingsAdminOrgHint),
                trailing: const Icon(Icons.chevron_right),
                onTap: () => Navigator.of(context).push(
                  MaterialPageRoute(builder: (_) => const OrgSettingsScreen()),
                ),
              ),
              // Read-only workflow viewer (the no-code builder stays on the web).
              ListTile(
                leading: const Icon(Icons.account_tree_outlined),
                title: Text(l.settingsAdminWorkflows),
                subtitle: Text(l.settingsAdminWorkflowsHint),
                trailing: const Icon(Icons.chevron_right),
                onTap: () => Navigator.of(context).push(
                  MaterialPageRoute(builder: (_) => const WorkflowsScreen()),
                ),
              ),
            ],
            // Adaptive AI workflows — read-only on mobile plus the one advisory
            // write (dismiss a suggestion). The two apply paths change live
            // approval routing and the org-wide auto-approve threshold and stay
            // on the web.
            if (AuthStore.instance.canViewAdaptive)
              ListTile(
                leading: const Icon(Icons.insights_outlined),
                title: Text(l.settingsAdaptive),
                subtitle: Text(l.settingsAdaptiveHint),
                trailing: const Icon(Icons.chevron_right),
                onTap: () => Navigator.of(context).push(
                  MaterialPageRoute(builder: (_) => const AdaptiveScreen()),
                ),
              ),
          ],

          // Display language — a per-device choice (like the biometric toggle),
          // persisted locally via LocaleStore and never sent to the backend.
          const Divider(height: 32),
          _languagePicker(context, l),

          // Security
          if (_bioAvailable) ...[
            const Divider(height: 32),
            SwitchListTile(
              secondary: const Icon(Icons.fingerprint),
              title: Text(l.settingsBiometricUnlock),
              subtitle: Text(l.settingsBiometricHint),
              value: _bioEnabled,
              onChanged: (enabled) async {
                if (enabled) {
                  final ok = await BiometricService.instance.authenticate();
                  if (!ok) return;
                }
                await BiometricService.instance.setEnabled(enabled);
                if (!mounted) return;
                setState(() => _bioEnabled = enabled);
              },
            ),
          ],

          const Divider(height: 32),

          // Logout
          ListTile(
            leading: Icon(Icons.logout, color: Colors.red.shade700),
            title: Text(
              l.settingsSignOut,
              // shade700 keeps the destructive label at AA contrast.
              style: TextStyle(color: Colors.red.shade700),
            ),
            // No navigation here: `logout()` ends the session, which notifies
            // AuthStore, and the root AuthGate swaps the tree for the login
            // screen (and pops any route above it). Rebuilding the stack from
            // here would remove the gate itself — the same defect that stranded
            // a 401-forced logout on the home screen.
            onTap: () => AuthStore.instance.logout(),
          ),
        ],
      ),
    );
  }

  /// A group label above a run of navigation rows (Procurement /
  /// Administration) — the same shape the admin section has always used, pulled
  /// out now that two sections need it.
  Widget _sectionHeader(String label) => Padding(
    padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
    child: Text(
      label,
      style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13),
    ),
  );

  /// Device display-language picker. Endonyms (each language's own name) plus a
  /// "System default" option that clears the override (follow the OS locale).
  /// Mirrors the web profile language `<select>`.
  Widget _languagePicker(BuildContext context, AppLocalizations l) {
    // The current persisted choice; `null` => follow the system default.
    final current = LocaleStore.instance.locale;
    final currentTag =
        current == null ? null : LocaleStore.tagOf(current);

    return ListTile(
      leading: const Icon(Icons.language),
      title: Text(l.settingsLanguage),
      subtitle: Text(l.settingsLanguageHint),
      trailing: DropdownButton<String?>(
        value: currentTag,
        // System default is the null entry.
        hint: Text(l.settingsLanguageSystem),
        onChanged: (tag) {
          final locale = tag == null
              ? null
              : LocaleStore.supportedLocales.firstWhere(
                  (loc) => LocaleStore.tagOf(loc) == tag,
                );
          LocaleStore.instance.setLocale(locale);
        },
        items: [
          DropdownMenuItem<String?>(
            value: null,
            child: Text(l.settingsLanguageSystem),
          ),
          ...LocaleStore.supportedLocales.map((loc) {
            final tag = LocaleStore.tagOf(loc);
            return DropdownMenuItem<String?>(
              value: tag,
              child: Text(LocaleStore.endonyms[tag] ?? tag),
            );
          }),
        ],
      ),
    );
  }
}
